#pragma once
#include <Arduino.h>
#define U_FLASH 0
struct UpdateClass {
  bool begin(size_t, int = U_FLASH) { return true; }
  size_t write(uint8_t *, size_t n) { return n; }
  bool end(bool = false) { return true; }
  void abort() {}
};
static UpdateClass Update;
