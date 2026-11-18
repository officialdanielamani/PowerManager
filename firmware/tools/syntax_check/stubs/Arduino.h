#pragma once
#include <cstdint>
#include <cstdio>
#include <cstring>
#include <cmath>
#include <string>
#include <cstdlib>
#include <algorithm>
#include <sys/time.h>
#include "freertos/FreeRTOS.h"
#include "freertos/semphr.h"
#define HIGH 1
#define LOW 0
#define INPUT 0
#define OUTPUT 1
#define INPUT_PULLUP 2
#define IRAM_ATTR
inline uint32_t millis() { return 0; }
inline void delay(uint32_t) {}
inline void delayMicroseconds(uint32_t) {}
inline void digitalWrite(int, int) {}
inline int digitalRead(int) { return 0; }
inline void pinMode(int, int) {}
using std::isfinite;
using std::isnan;
using std::min;
using std::max;
using std::abs;
#define FALLING 2
#define INPUT_PULLDOWN 3
#define digitalPinToInterrupt(p) (p)
inline void attachInterrupt(int, void (*)(), int) {}
#define PROGMEM
#define constrain(x, lo, hi) ((x) < (lo) ? (lo) : ((x) > (hi) ? (hi) : (x)))
inline uint32_t analogReadMilliVolts(int) { return 0; }
#include "HardwareSerial.h"
inline size_t strlcat(char *d, const char *s, size_t n) { size_t dl = strlen(d); if (dl < n - 1) strncat(d, s, n - dl - 1); return dl + strlen(s); }
struct String {
  std::string s;
  String() {}
  String(const char *c) : s(c) {}
  String(int v) : s(std::to_string(v)) {}
  String(unsigned v) : s(std::to_string(v)) {}
  String(unsigned long v) : s(std::to_string(v)) {}
  String(float v, int d = 2) { char b[32]; snprintf(b, 32, "%.*f", d, v); s = b; }
  String(const std::string &x) : s(x) {}
  const char *c_str() const { return s.c_str(); }
  size_t length() const { return s.size(); }
  String &operator+=(const String &o) { s += o.s; return *this; }
  String &operator+=(const char *o) { s += o; return *this; }
  String operator+(const String &o) const { return String(s + o.s); }
  String operator+(const char *o) const { return String(s + o); }
  String operator+(int o) const { return String(s + std::to_string(o)); }
  String operator+(unsigned o) const { return String(s + std::to_string(o)); }
  bool equalsIgnoreCase(const char *) const { return false; }
  bool startsWith(const char *) const { return false; }
  String substring(int a) const { return String(s.substr(a)); }
  String substring(int a, int b) const { return String(s.substr(a, b - a)); }
  int indexOf(char c) const { auto p = s.find(c); return p == std::string::npos ? -1 : (int)p; }
  void trim() {}
  size_t write(uint8_t c) { s += (char)c; return 1; }
  size_t write(const uint8_t *b, size_t n) { s.append((const char *)b, n); return n; }
  bool reserve(size_t n) { s.reserve(n); return true; }
  bool concat(const char *c, size_t n) { s.append(c, n); return true; }
  bool concat(const char *c) { s += c; return true; }
  bool concat(char c) { s += c; return true; }
  char operator[](size_t i) const { return s[i]; }
  bool operator==(const String &o) const { return s == o.s; }
  bool operator!=(const String &o) const { return s != o.s; }
  bool operator==(const char *o) const { return s == o; }
  bool operator!=(const char *o) const { return s != o; }
  bool isEmpty() const { return s.empty(); }
  void toLowerCase() {}
  int toInt() const { return atoi(s.c_str()); }
};
inline String operator+(const char *a, const String &b) { return String(std::string(a) + b.s); }
struct SerialT { template <class... A> void printf(const char *f, A... a) { ::printf(f, a...); } void println(const char *s) { puts(s); } void print(const char *s) { fputs(s, stdout); } };
extern SerialT Serial;
struct EspT { uint64_t getEfuseMac() { return 0; } void restart() {} uint32_t getFreeHeap() { return 0; } uint32_t getMinFreeHeap() { return 0; } uint32_t getFlashChipSize() { return 0; } };
extern EspT ESP;
// Serial / Wire are only declared here: each check that needs them defines SerialT Serial / WireT Wire itself,
// the others never reference them (syntax check only, nothing is linked).
