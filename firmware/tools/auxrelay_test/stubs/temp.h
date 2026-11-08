#pragma once
extern float g_temp[4];
namespace temp { inline float value(uint8_t i) { return g_temp[i]; } inline float humidity(uint8_t) { return NAN; } }
