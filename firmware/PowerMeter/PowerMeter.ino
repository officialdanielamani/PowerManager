// PowerManager meter firmware for ESP32-S3 Super Mini + PZEM-004T + SSR + WS2812.
// Arduino IDE 2.x, board package "esp32" by Espressif 3.x. See firmware/README.md.
//
// FreeRTOS tasks (docs/firmware.md §5):
//   control (core 1): sensor, limits, relay - never waits on the network
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
#include "src/sensor_pzem.h"
#include "src/storage.h"
#include "src/system.h"
#include "src/timekeep.h"
#include "src/web.h"

static PzemSensor pzem(Serial1, PIN_PZEM_RX, PIN_PZEM_TX, PZEM_ADDRESS);

static void loadState() {
  StateLock l;
  if (!store::loadConfig(g.cfg)) {
    g.cfg = MeterConfig();
    g.cfg.report_s = DEFAULT_REPORT_S;
  }
  store::loadSession(g.sess);
  store::loadFlags(g.wantOn, g.tripped);
}

void setup() {
  Serial.begin(115200);
  delay(300);
  state_init();
  store::begin();
  loadState();
  outbox::begin();
  timekeep::begin();
  led::begin();
  control::begin(&pzem);
  net::begin();
  web::begin();

  Serial.println();
  Serial.println("==============================================");
  Serial.printf(" PowerMeter %s   Device ID: %s\n", FW_VERSION, store::deviceId().c_str());
  Serial.printf(" Setup Wi-Fi: %s   password: %s\n", net::apSsid().c_str(), store::apPassword().c_str());
  Serial.printf(" Boot #%u, %u item(s) waiting to send\n", (unsigned)store::bootCount(), outbox::count());
  Serial.println("==============================================");

  xTaskCreatePinnedToCore(control::task, "control", 6144, nullptr, 3, nullptr, 1);
  xTaskCreatePinnedToCore(led::task, "led", 3072, nullptr, 1, nullptr, 1);
  xTaskCreatePinnedToCore(net::task, "net", 12288, nullptr, 2, nullptr, 0);  // TLS needs stack
  xTaskCreatePinnedToCore(web::task, "web", 8192, nullptr, 1, nullptr, 0);
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
  sys::checkHeartbeats();
  vTaskDelay(pdMS_TO_TICKS(50));
}
