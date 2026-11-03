#include "system.h"

#include "app_state.h"
#include "outbox.h"
#include "storage.h"

namespace sys {

static volatile uint32_t beats[HB_COUNT];
static const uint32_t HB_LIMIT_MS[HB_COUNT] = {180000, 60000};  // net may block on TLS/HTTP

void factoryReset(const char *why) {
  Serial.printf("[system] FACTORY RESET (%s)\n", why);
  {
    StateLock l;
    g.factoryResetPending = true;
  }
  outbox::clear();
  store::factoryReset();
  delay(1500);  // let the LED show white flashing
  ESP.restart();
}

void reboot(uint32_t delayMs) {
  delay(delayMs);
  ESP.restart();
}

void heartbeat(uint8_t task) { beats[task] = millis(); }

void checkHeartbeats() {
  uint32_t now = millis();
  for (int i = 0; i < HB_COUNT; i++) {
    if (beats[i] && now - beats[i] > HB_LIMIT_MS[i]) {
      Serial.printf("[system] task %d stuck for %lu ms, restarting\n", i, (unsigned long)(now - beats[i]));
      delay(100);
      ESP.restart();
    }
  }
}

}  // namespace sys
