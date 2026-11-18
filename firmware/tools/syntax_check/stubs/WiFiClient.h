#pragma once
#include <Arduino.h>
struct WiFiClient {
  int available() { return 0; }
  size_t readBytes(uint8_t *, size_t) { return 0; }
  bool connected() { return false; }
};
