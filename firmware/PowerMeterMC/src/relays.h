// I2C relay boards: PCF8574 (+ SSRs / relay module) and / or XL9535 relay boards (RELAY_BOARDS in config.h).
// Relays are numbered across the boards; which relay switches which channel is set in CHANNELS.
#pragma once
#include <Arduino.h>

namespace relays {
bool begin();                  // all relays OFF; false if a board does not answer
bool write(uint32_t onMask);   // bit k = relay k+1 ON. Re-sent periodically by control (board glitch recovery)
bool ok();                     // the last write to every board was acknowledged
String summary();              // "XL9535 @0x20 (2) OK, PCF8574 @0x21 (8) NOT ANSWERING"
}  // namespace relays
