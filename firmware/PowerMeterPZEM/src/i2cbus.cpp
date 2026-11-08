#include "i2cbus.h"

#include "../config.h"

namespace i2cbus {

static SemaphoreHandle_t mtx = nullptr;
static bool started = false;

void begin() {
  if (started) return;
  if (!mtx) mtx = xSemaphoreCreateRecursiveMutex();
  Wire.begin(PIN_I2C_SDA, PIN_I2C_SCL, I2C_FREQ_HZ);
  Wire.setTimeOut(50);
  started = true;
}

bool ready() { return started; }
SemaphoreHandle_t mutex() {
  if (!mtx) mtx = xSemaphoreCreateRecursiveMutex();
  return mtx;
}

bool readRegs(uint8_t addr, uint8_t reg, uint8_t *buf, size_t n) {
  Lock l;
  Wire.beginTransmission(addr);
  Wire.write(reg);
  if (Wire.endTransmission(false) != 0) return false;  // repeated start
  if (Wire.requestFrom((uint16_t)addr, (uint8_t)n) != n) return false;
  for (size_t i = 0; i < n; i++) buf[i] = Wire.read();
  return true;
}

bool readRaw(uint8_t addr, uint8_t *buf, size_t n) {
  Lock l;
  if (Wire.requestFrom((uint16_t)addr, (uint8_t)n) != n) return false;
  for (size_t i = 0; i < n; i++) buf[i] = Wire.read();
  return true;
}

bool probe(uint8_t addr) {
  Lock l;
  Wire.beginTransmission(addr);
  return Wire.endTransmission() == 0;
}

bool writeReg(uint8_t addr, uint8_t reg, uint8_t val) {
  uint8_t d[2] = {reg, val};
  return writeRaw(addr, d, 2);
}

bool writeRaw(uint8_t addr, const uint8_t *data, size_t n) {
  Lock l;
  Wire.beginTransmission(addr);
  Wire.write(data, n);
  return Wire.endTransmission() == 0;
}

}  // namespace i2cbus
