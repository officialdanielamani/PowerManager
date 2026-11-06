// One I2C bus shared by the rbAmp module, the DS3231 and the relay board, used from several tasks.
#pragma once
#include <Arduino.h>
#include <Wire.h>

namespace i2cbus {
void begin();  // Wire on PIN_I2C_SDA / PIN_I2C_SCL at I2C_FREQ_HZ
bool ready();
SemaphoreHandle_t mutex();
// Register helpers (take the bus lock themselves).
bool readRegs(uint8_t addr, uint8_t reg, uint8_t *buf, size_t n);
bool writeReg(uint8_t addr, uint8_t reg, uint8_t val);
bool writeRaw(uint8_t addr, const uint8_t *data, size_t n);

struct Lock {
  Lock() { xSemaphoreTakeRecursive(mutex(), portMAX_DELAY); }
  ~Lock() { xSemaphoreGiveRecursive(mutex()); }
};
}  // namespace i2cbus
