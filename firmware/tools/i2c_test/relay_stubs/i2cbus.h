#pragma once
#include <Arduino.h>
namespace i2cbus {
void begin();
bool writeReg(uint8_t addr, uint8_t reg, uint8_t val);
bool writeRaw(uint8_t addr, const uint8_t *data, size_t n);
}
