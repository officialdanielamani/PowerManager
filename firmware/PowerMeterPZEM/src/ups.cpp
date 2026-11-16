#include "ups.h"

namespace ups {

static bool state = false;
static uint32_t changeSince = 0;

bool enabled() { return UPS_ENABLE != 0; }
bool active() { return state; }

#if UPS_ENABLE
static bool read() { return digitalRead(PIN_UPS) == UPS_ACTIVE_LEVEL; }

void begin() {
  // Active LOW: contact to GND with the internal pull-up. Active HIGH: 3.3 V signal with the pull-down.
  pinMode(PIN_UPS, UPS_ACTIVE_LEVEL == LOW ? INPUT_PULLUP : INPUT_PULLDOWN);
  delay(2);
  state = read();
}

bool poll() {
  bool now = read();
  if (now == state) {
    changeSince = 0;
    return false;
  }
  if (!changeSince) changeSince = millis() | 1;
  if (millis() - changeSince < UPS_DEBOUNCE_MS) return false;
  changeSince = 0;
  state = now;
  return true;
}
#else
void begin() {}
bool poll() { return false; }
#endif

}  // namespace ups
