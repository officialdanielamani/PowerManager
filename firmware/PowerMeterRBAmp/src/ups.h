// UPS signal input (config.h: UPS_ENABLE, PIN_UPS, UPS_ACTIVE_LEVEL): a contact / logic output of a UPS that
// tells "mains failed, running on the UPS". Only reported (event + notification on the server) and usable
// as an AUX relay event; it does not switch the measured relays. Shared by both firmwares
// (firmware/tools/check_shared.py).
#pragma once
#include <Arduino.h>
#include "../config.h"

#ifndef UPS_ENABLE
#define UPS_ENABLE 0
#endif
#ifndef PIN_UPS
#define PIN_UPS 2
#endif
#ifndef UPS_ACTIVE_LEVEL
#define UPS_ACTIVE_LEVEL LOW   // level of PIN_UPS that means "on UPS"
#endif
#ifndef UPS_DEBOUNCE_MS
#define UPS_DEBOUNCE_MS 2000
#endif

namespace ups {
bool enabled();
void begin();    // pin mode + first state (no debounce)
bool poll();     // every control pass; true when the debounced state changed
bool active();   // running on the UPS
}
