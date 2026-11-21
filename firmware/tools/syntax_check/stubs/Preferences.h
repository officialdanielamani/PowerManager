#pragma once
#include <Arduino.h>
struct Preferences {
  bool begin(const char *, bool = false) { return true; }
  void end() {}
  bool clear() { return true; }
  bool remove(const char *) { return true; }
  bool isKey(const char *) { return false; }
  size_t getBytesLength(const char *) { return 0; }
  size_t getBytes(const char *, void *, size_t) { return 0; }
  size_t putBytes(const char *, const void *, size_t n) { return n; }
  String getString(const char *, const String &d = String()) { return d; }
  size_t putString(const char *, const String &) { return 0; }
  uint32_t getUInt(const char *, uint32_t d = 0) { return d; }
  size_t putUInt(const char *, uint32_t) { return 4; }
  uint16_t getUShort(const char *, uint16_t d = 0) { return d; }
  size_t putUShort(const char *, uint16_t) { return 2; }
  uint8_t getUChar(const char *, uint8_t d = 0) { return d; }
  size_t putUChar(const char *, uint8_t) { return 1; }
  bool getBool(const char *, bool d = false) { return d; }
  size_t putBool(const char *, bool) { return 1; }
  double getDouble(const char *, double d = 0) { return d; }
  size_t putDouble(const char *, double) { return 8; }
  float getFloat(const char *, float d = 0) { return d; }
  size_t putFloat(const char *, float) { return 4; }
  uint64_t getULong64(const char *, uint64_t d = 0) { return d; }
  size_t putULong64(const char *, uint64_t) { return 8; }
};
