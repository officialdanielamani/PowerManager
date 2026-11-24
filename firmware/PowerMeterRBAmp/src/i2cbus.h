// One I2C bus shared by the rbAmp board(s), DS3231, relay boards and SHT sensors (both firmwares), used from several tasks.
#pragma once
#include <Arduino.h>
#include <Wire.h>

namespace i2cbus {
void begin();  // Wire on PIN_I2C_SDA / PIN_I2C_SCL at I2C_FREQ_HZ
bool ready();
void tick();                // every ~50 ms from loop(): frees a stuck bus (SDA held low while nothing is being sent)
uint32_t recoveries();      // how often the bus had to be freed since start (info)
SemaphoreHandle_t mutex();
// Register helpers (take the bus lock themselves).
bool readRegs(uint8_t addr, uint8_t reg, uint8_t *buf, size_t n);
bool writeReg(uint8_t addr, uint8_t reg, uint8_t val);
bool writeRaw(uint8_t addr, const uint8_t *data, size_t n);
bool present(uint8_t addr);  // ACK, and a byte back from a read when the bus ACKs everywhere (an rbAmp module does)
bool probe(uint8_t addr);
bool readRaw(uint8_t addr, uint8_t *buf, size_t n);  // read without a register (SHT sensors)  // something answers (ACK) at this address

struct Lock {
  Lock() { xSemaphoreTakeRecursive(mutex(), portMAX_DELAY); }
  ~Lock() { xSemaphoreGiveRecursive(mutex()); }
};
}  // namespace i2cbus
