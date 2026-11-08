#pragma once
#include <cstdint>
#include <cstring>
#include <cmath>
using std::isnan;
#include <cstdio>
#include <string>
typedef std::string String;
extern uint32_t g_ms;
inline uint32_t millis() { return g_ms; }
struct SerialT { template <class... A> void printf(const char *f, A... a) { ::printf(f, a...); } };
extern SerialT Serial;
inline size_t strlcpy(char *d, const char *s, size_t n) { size_t l = strlen(s); if (n) { size_t c = l < n - 1 ? l : n - 1; memcpy(d, s, c); d[c] = 0; } return l; }
