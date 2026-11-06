// WS2812 status LED using the core's built-in rgbLedWrite() (no library).
#pragma once
#include <Arduino.h>

namespace led {
enum Flash : uint8_t { FLASH_ACK, FLASH_CONFIG, FLASH_REJECT, FLASH_PAIRED };
void begin();
void task(void *);
void flash(Flash f);  // one-shot pattern shown over the current state
void resetHold(bool holding);  // BOOT button is being held
}  // namespace led
