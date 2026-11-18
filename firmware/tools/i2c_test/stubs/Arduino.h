// Arduino stub for the I2C host tests: a fake GPIO model, millis from the test, Serial to stdout.
#pragma once
#include <cstdint>
#include <cstdio>
#include <cstring>
#include <cmath>
#include <cstdlib>
#include <string>
#include "freertos/FreeRTOS.h"
#include "freertos/semphr.h"
#define HIGH 1
#define LOW 0
#define INPUT 0
#define OUTPUT 1
#define INPUT_PULLUP 2
#define IRAM_ATTR
extern uint32_t g_ms;
inline uint32_t millis() { return g_ms; }
inline void delay(uint32_t) {}
inline void delayMicroseconds(uint32_t) {}
void pinMode(int pin, int mode);          // defined by the tests (fake GPIO)
void digitalWrite(int pin, int level);
int digitalRead(int pin);
struct String {
  std::string s;
  String() {}
  String(const char *c) : s(c) {}
  String(int v) : s(std::to_string(v)) {}
  const char *c_str() const { return s.c_str(); }
  size_t length() const { return s.size(); }
  String &operator+=(const String &o) { s += o.s; return *this; }
  String &operator+=(const char *o) { s += o; return *this; }
};
inline String operator+(const String &a, const String &b) { String r = a; r += b; return r; }
inline String operator+(const String &a, const char *b) { String r = a; r += b; return r; }
struct SerialT { template <class... A> void printf(const char *f, A... a) { ::printf(f, a...); } void println(const char *s) { puts(s); } };
extern SerialT Serial;
