#pragma once
#include <Arduino.h>
struct HardwareSerial {
  HardwareSerial(int = 0) {}
  void begin(unsigned long, uint32_t = 0, int = -1, int = -1) {}
  void end() {}
  int available() { return 0; }
  int read() { return -1; }
  size_t write(const uint8_t *, size_t n) { return n; }
  size_t write(uint8_t) { return 1; }
  void flush() {}
  void setTimeout(unsigned long) {}
  void setRxBufferSize(size_t) {}
  void setRxTimeout(uint8_t) {}
  size_t readBytes(uint8_t *, size_t) { return 0; }
};
#define SERIAL_8N1 0x800001c
#define SERIAL_8N2 0x800003c
