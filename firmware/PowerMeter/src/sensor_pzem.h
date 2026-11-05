// Power sensors over Modbus-RTU without external libraries:
//   PzemSensor    PZEM-004T v3/v4 (AC, TTL, 9600 8N1)
//   Pzem017Sensor PZEM-017 (DC, RS485, 9600 8N2) through an RS485-TTL module
// Behind a tiny interface so other sensors / multi-channel boards can be added later.
#pragma once
#include <Arduino.h>
#include "app_state.h"

class Sensor {
 public:
  virtual ~Sensor() {}
  virtual void begin() = 0;
  virtual uint8_t channels() const = 0;
  virtual const char *type() const = 0;
  virtual bool read(uint8_t ch, Reading &out) = 0;  // false = no reply / bad reply
  virtual uint8_t lastError() const { return 0; }     // SensorErr of the last read()
};

// Shared Modbus-RTU transport (one request, one reply).
class ModbusSensor : public Sensor {
 public:
  ModbusSensor(HardwareSerial &port, int rxPin, int txPin, uint8_t address, int dePin = -1)
      : port_(port), rx_(rxPin), tx_(txPin), addr_(address), de_(dePin) {}
  uint8_t channels() const override { return 1; }
  uint8_t lastError() const override { return err_; }

 protected:
  // Sends req (CRC added here, len without CRC) and reads exactly respLen bytes (CRC checked).
  // Sets err_ to SENS_NO_REPLY / SENS_BAD on failure.
  bool transact(uint8_t *req, size_t len, uint8_t *resp, size_t respLen);
  HardwareSerial &port_;
  int rx_, tx_;
  uint8_t addr_;
  int de_;
  uint8_t err_ = 0;
};

class PzemSensor : public ModbusSensor {
 public:
  using ModbusSensor::ModbusSensor;
  void begin() override;
  const char *type() const override { return "pzem004t"; }
  bool read(uint8_t ch, Reading &out) override;
};

class Pzem017Sensor : public ModbusSensor {
 public:
  Pzem017Sensor(HardwareSerial &port, int rxPin, int txPin, uint8_t address, int dePin, uint16_t shuntA)
      : ModbusSensor(port, rxPin, txPin, address, dePin), shuntA_(shuntA) {}
  void begin() override;
  const char *type() const override { return "pzem017"; }
  bool read(uint8_t ch, Reading &out) override;

 private:
  uint16_t shuntA_;
};

uint16_t modbusCrc(const uint8_t *buf, size_t len);
