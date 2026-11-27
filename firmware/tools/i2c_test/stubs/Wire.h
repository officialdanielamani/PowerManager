// Fake Wire: the tests script what a transaction returns (see test_bus.cpp).
#pragma once
#include <cstddef>
#include <cstdint>
struct WireT {
  void begin(int sda, int scl, uint32_t hz);
  void end();
  void setTimeOut(int) {}
  void beginTransmission(uint8_t addr);
  void write(uint8_t) {}
  void write(const uint8_t *, size_t) {}
  uint8_t endTransmission(bool stop = true);
  size_t requestFrom(uint16_t addr, uint8_t n);
  int read() { return 0; }
};
extern WireT Wire;
