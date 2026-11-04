#include "led.h"

#include "../config.h"
#include "app_state.h"
#include "system.h"

namespace led {

static volatile Flash pending;
static volatile bool hasPending = false;
static volatile bool holding = false;
static uint32_t bootMs;

struct RGB {
  uint8_t r, g, b;
};
static const RGB OFF = {0, 0, 0}, RED = {255, 0, 0}, GREEN = {0, 255, 0}, BLUE = {0, 0, 255},
                 YELLOW = {255, 160, 0}, ORANGE = {255, 70, 0}, PURPLE = {150, 0, 255},
                 CYAN = {0, 200, 200}, WHITE = {255, 255, 255};

static void show(RGB c, uint16_t level = 255) {
  uint32_t k = (uint32_t)LED_BRIGHTNESS * level / 255;
#if ESP_ARDUINO_VERSION_MAJOR >= 3
  rgbLedWrite(PIN_LED, c.r * k / 255, c.g * k / 255, c.b * k / 255);
#else  // core 2.0.x calls it neopixelWrite()
  neopixelWrite(PIN_LED, c.r * k / 255, c.g * k / 255, c.b * k / 255);
#endif
}

static bool blink(uint32_t t, uint32_t periodMs) { return (t % periodMs) < periodMs / 2; }

static uint16_t breathe(uint32_t t, uint32_t periodMs) {
  uint32_t p = t % periodMs, half = periodMs / 2;
  return 20 + 235 * (p < half ? p : periodMs - p) / half;
}

void begin() {
  bootMs = millis();
  show(OFF);
}

void flash(Flash f) {
  pending = f;
  hasPending = true;
}

void resetHold(bool h) { holding = h; }

// Plays one-shot pattern; returns false when finished.
static bool oneShot(Flash f, uint32_t dt) {
  switch (f) {
    case FLASH_ACK:  // green x2
      if (dt >= 600) return false;
      show((dt % 300) < 150 ? GREEN : OFF);
      return true;
    case FLASH_CONFIG:  // one long green
      if (dt >= 700) return false;
      show(dt < 500 ? GREEN : OFF);
      return true;
    case FLASH_REJECT:  // red x3 fast
      if (dt >= 450) return false;
      show((dt % 150) < 75 ? RED : OFF);
      return true;
    case FLASH_PAIRED:  // green x5 fast
      if (dt >= 1000) return false;
      show((dt % 200) < 100 ? GREEN : OFF);
      return true;
  }
  return false;
}

void task(void *) {
  Flash current = FLASH_ACK;
  uint32_t shotStart = 0;
  bool inShot = false;
  sys::crumb(sys::CR_LED, "running");
  for (;;) {
    uint32_t t = millis();
    if (hasPending) {
      current = pending;
      hasPending = false;
      shotStart = t;
      inShot = true;
    }
    if (holding || g.factoryResetPending) {
      show(blink(t, 100) ? WHITE : OFF);  // 1. factory reset
    } else if (t - bootMs < 1500) {
      uint32_t p = (t - bootMs) / 500;  // 2. boot sweep R -> G -> B
      show(p == 0 ? RED : p == 1 ? GREEN : BLUE);
    } else if (inShot && oneShot(current, t - shotStart)) {
      // one-shot pattern running
    } else {
      inShot = false;
      bool tripped, tamper, ac, relay, paired, apOnly, voltOut, disabled;
      SessStatus st;
      NetStatus net;
      {
        StateLock l;
        tripped = g.tripped;
        st = g.sess.status;
        tamper = g.tamper;
        ac = g.ac;
        net = g.net;
        relay = g.relayOn;
        paired = g.paired;
        apOnly = g.apActive;
        voltOut = g.voltOut;
        disabled = g.disabled;
      }
      if (disabled) show(RED, 20);                               // disabled by the server: very dim red
      else if (tripped) show(RED);                               // 3. over-current trip
      else if (st != SESS_ACTIVE) show(blink(t, 1000) ? YELLOW : OFF);  // 4. quota / ended
      else if (tamper) show(blink(t, 1000) ? PURPLE : OFF);      // 5. panel open
      else if (!ac || voltOut) show((t % 3000) < 300 ? ORANGE : OFF);  // 6. AC lost / voltage out of range
      else if (net == NET_SETUP_AP || (apOnly && net == NET_WIFI_CONNECTING)) show(BLUE, breathe(t, 3000));  // 7
      else if (net == NET_WIFI_CONNECTING) show(blink(t, 500) ? BLUE : OFF);  // 8. Wi-Fi connecting
      else if (net == NET_NOT_PAIRED || !paired) show(CYAN, breathe(t, 3000));  // 9. not paired
      else if (net != NET_ONLINE) show(blink(t, 1000) ? RED : OFF);  // 10. offline
      else show(relay ? GREEN : OFF, relay ? 25 : 0);            // idle
    }
    vTaskDelay(pdMS_TO_TICKS(20));
  }
}

}  // namespace led
