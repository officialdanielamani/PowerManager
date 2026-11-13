// PC stand-ins for the Arduino core, just enough for mqttlink.cpp (see run.py).
#pragma once
#include <chrono>
#include <cmath>
#include <cstdint>
#include <cstdio>
#include <cstring>
#include <string>
typedef std::string String;
inline uint32_t millis() {
  using namespace std::chrono;
  return (uint32_t)duration_cast<milliseconds>(steady_clock::now().time_since_epoch()).count();
}
struct SerialT {
  template <class... A> void printf(const char *f, A... a) { ::printf(f, a...); }
  void println(const char *s) { ::printf("%s\n", s); }
};
extern SerialT Serial;
