// PowerManager multi-channel AC meter: ESP32-S3 Super Mini + rbAmp BASIC (SCT-013, I2C) + DS3231
// + tamper switch + I2C relay board (PCF8574 / XL9535) + WS2812. See docs/multichannel.md.
// Arduino IDE 2.x, board package "esp32" by Espressif 3.x. See firmware/README.md.
//
// FreeRTOS tasks (docs/firmware.md §5):
//   control (core 1): sensor (every 100 ms window with RDY), limits, relays - never waits on the network
//   net     (core 0): Wi-Fi, pairing, WebSocket, offline buffer flush
//   web     (core 0): local web page + captive portal
//   led     (core 1): status LED
//   loop()          : BOOT button (factory reset) + soft watchdog

#include <esp_task_wdt.h>

#include "config.h"
#include "src/app_state.h"
#include "src/control.h"
#include "src/led.h"
#include "src/net.h"
#include "src/outbox.h"
#include "src/relays.h"
#include "src/storage.h"
#include "src/system.h"
#include "src/timekeep.h"
#include "src/web.h"

static void loadState() {
  StateLock l;
  if (!store::loadConfig(g.cfg)) {
    g.cfg = MeterConfig();
    g.cfg.report_s = DEFAULT_REPORT_S;
    g.cfg.read_s = DEFAULT_READ_S;
  }
  for (uint8_t i = 0; i < MC_MAX_CH; i++) store::loadSession(i, g.sess[i]);
  g.sidCounter = store::loadSidCounter();
  g.everStarted = store::loadEverStarted();
  uint8_t want, trip;
  store::loadFlags(want, trip);
  double e[MC_MAX_CH];
  store::loadEnergy(e);
  for (uint8_t i = 0; i < MC_MAX_CH; i++) {
    g.ch[i].wantOn = want & (1 << i);
    g.ch[i].tripped = trip & (1 << i);
    g.ch[i].e = e[i];
  }
  g.disabled = store::loadDisabled();
}

static void printBanner() {
  bool ap;
  NetStatus st;
  {
    StateLock l;
    ap = g.apActive;
    st = g.net;
  }
  Serial.println();
  Serial.println("==============================================");
  Serial.printf(" PowerMeter %s (%s)   Device ID: %s\n", FW_VERSION, SENSOR_NAME, store::deviceId().c_str());
  {
    StateLock l;
    Serial.printf(" Sensor: rbAmp %s, %u channel(s)%s   Relays: %s at 0x%02X %s   Mode: %s\n", g.variant, g.nch,
                  g.nch ? "" : " (NOT FOUND)", relays::boardName(), RELAY_ADDRESS, relays::ok() ? "OK" : "NOT ANSWERING",
                  g.cfg.mode == MODE_INDEPENDENT ? "independent" : "shared");
  }
  Serial.printf(" Setup Wi-Fi: %s   password: %s   (%s)\n", net::apSsid().c_str(),
                store::apPassword().c_str(), ap ? "ON now, open http://192.168.4.1" : "off");
  String ip = net::ipAddress();
  if (ip.length()) Serial.printf(" On your network: http://%s/\n", ip.c_str());
  static const char *NET[] = {"setup", "Wi-Fi connecting", "not paired", "server offline", "online"};
  Serial.printf(" Last reset: %s%s\n", sys::resetReason(), sys::safeMode ? "   ** SAFE MODE: mesh + DS3231 off **" : "");
  Serial.printf(" Status: %s   Boot #%u   %u item(s) waiting to send\n", NET[st],
                (unsigned)store::bootCount(), outbox::count());
  Serial.printf(" Free heap %u B, lowest %u B. %s\n", (unsigned)ESP.getFreeHeap(), (unsigned)ESP.getMinFreeHeap(), sys::stackReport().c_str());
  Serial.println(" Type 'info' + Enter to show this again.");
  Serial.println("==============================================");
}

void setup() {
  Serial.begin(115200);
  // With "USB CDC On Boot", the PC needs a moment to reopen the port after a reset.
  // Wait up to 3 s for the Serial Monitor so the banner is not lost (no wait without USB).
  uint32_t t0 = millis();
  while (!Serial && millis() - t0 < 3000) delay(10);
  delay(200);
  // Boot steps are printed so a crash during start-up shows how far it got.
  sys::bootCheck();
  auto step = [](const char *what) {
    sys::crumb(sys::CR_BOOT, what);
    Serial.printf("[boot] %s\n", what);
    Serial.flush();
  };
  step("state");
  state_init();
  step("storage (NVS)");
  store::begin();
  loadState();
  step("offline buffer (LittleFS)");
  outbox::begin();
  step(HAS_DS3231 ? "clock (DS3231)" : "clock");
  timekeep::begin();
  step("LED");
  led::begin();
  step("sensor (rbAmp) + relay board");
  control::begin();
  step(MESH_ENABLE ? "Wi-Fi + mesh" : "Wi-Fi");
  net::begin();
  step("web server");
  web::begin();
  step("start tasks");

  printBanner();
  sys::bootDone();

  xTaskCreatePinnedToCore(control::task, "control", STACK_CONTROL, nullptr, 3, &sys::taskHandles[0], 1);
  xTaskCreatePinnedToCore(led::task, "led", STACK_LED, nullptr, 1, &sys::taskHandles[1], 1);
  xTaskCreatePinnedToCore(net::task, "net", STACK_NET, nullptr, 2, &sys::taskHandles[2], 0);  // TLS needs stack
  xTaskCreatePinnedToCore(web::task, "web", STACK_WEB, nullptr, 1, &sys::taskHandles[3], 0);
}

void loop() {
  // BOOT button: hold 10 s for factory reset (LED flashes white while held).
  static uint32_t pressedMs = 0;
  pinMode(PIN_BOOT_BUTTON, INPUT_PULLUP);
  if (digitalRead(PIN_BOOT_BUTTON) == LOW) {
    if (!pressedMs) pressedMs = millis();
    uint32_t held = millis() - pressedMs;
    led::resetHold(held > 2000);
    if (held >= FACTORY_RESET_HOLD_MS) sys::factoryReset("BOOT button");
  } else {
    if (pressedMs) led::resetHold(false);
    pressedMs = 0;
  }
  sys::crumb(sys::CR_MAIN, "loop");
  sys::tick();
  sys::checkHeartbeats();
  sys::checkStacks();

  // Serial command "info" prints the banner again.
  static String line;
  while (Serial.available()) {
    char c = Serial.read();
    if (c == '\n' || c == '\r') {
      line.trim();
      if (line.equalsIgnoreCase("info")) printBanner();
      line = "";
    } else if (line.length() < 32) {
      line += c;
    }
  }
  // Repeat the banner every 30 s while the setup hotspot is on (easy to catch late).
  static uint32_t lastBanner = 0;
  if (millis() - lastBanner > 30000) {
    lastBanner = millis();
    if (g.apActive) printBanner();
  }
  vTaskDelay(pdMS_TO_TICKS(50));
}
