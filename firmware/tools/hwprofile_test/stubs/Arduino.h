#pragma once
#include <cstdint>
#include <cstdio>
#include <cstring>
#include <cstddef>
extern uint32_t g_ms;
inline uint32_t millis() { return g_ms; }
struct SerialT { template <class... A> void printf(const char *f, A... a) { ::printf(f, a...); } };
extern SerialT Serial;
