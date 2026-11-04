// PZEM-004T v3/v4 (Modbus-RTU over TTL, 9600 8N1) without external libraries.
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

class PzemSensor : public Sensor {
 public:
  PzemSensor(HardwareSerial &port, int rxPin, int txPin, uint8_t address)
      : port_(port), rx_(rxPin), tx_(txPin), addr_(address) {}
  void begin() override;
  uint8_t channels() const override { return 1; }
  const char *type() const override { return "pzem004t"; }
  bool read(uint8_t ch, Reading &out) override;
  uint8_t lastError() const override { return err_; }

 private:
  HardwareSerial &port_;
  int rx_, tx_;
  uint8_t addr_;
  uint8_t err_ = 0;
};

uint16_t modbusCrc(const uint8_t *buf, size_t len);
