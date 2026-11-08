// Relay outputs: ESP32 pins, PCF8574 and / or XL9535 boards, mixed (RELAY_BOARDS in config.h).
// Relays are numbered 1..n across the entries. Shared by both firmwares.
#pragma once
#include <Arduino.h>

namespace relays {
bool begin();                  // all relays OFF; false if an I2C board does not answer
bool write(uint32_t onMask);   // bit k = relay k+1 ON. Re-sent periodically (I2C board glitch recovery)
bool ok();                     // the last write to every board was acknowledged
String summary();              // "GPIO 7 (1) OK, XL9535 @0x20 (2) OK, PCF8574 @0x21 (8) NOT ANSWERING"
}  // namespace relays
