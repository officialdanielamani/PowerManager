// I2C relay board: PCF8574 (+ SSRs / relay module) or XL9535 relay board. Relay i = channel i.
#pragma once
#include <Arduino.h>

namespace relays {
bool begin();                 // all relays OFF; false if the board does not answer
bool write(uint8_t onMask);   // bit i = relay i ON. Re-sent periodically by control (board glitch recovery)
bool ok();                    // the last write was acknowledged
const char *boardName();
}  // namespace relays
