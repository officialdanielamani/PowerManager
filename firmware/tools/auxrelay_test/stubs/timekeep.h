#pragma once
#include <cstdint>
extern uint32_t g_now;
namespace timekeep { inline bool valid() { return g_now != 0; } inline uint32_t now() { return g_now; } }
