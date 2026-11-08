// DS18B20 temperature sensors on one 1-Wire pin (TEMP_ENABLE / PIN_TEMP in config.h). Own small 1-Wire
// driver: no extra library to install. Shared by both firmwares (firmware/tools/check_shared.py).
#pragma once
#include <Arduino.h>
#include "../config.h"

#ifndef TEMP_ENABLE
#define TEMP_ENABLE 0
#endif

namespace temp {
#if TEMP_ENABLE
void begin();                // finds the sensors (sorted by serial number: T1, T2 ... stay the same)
void poll();                 // call often (control loop): converts every TEMP_READ_MS, reads one sensor per call
uint8_t count();             // sensors found
float value(uint8_t i);      // degC; NAN = not readable
bool takeFresh();            // true once after every complete round of readings (record them)
String summary();            // "T1 28-0316a2794e1f 24.5 C, T2 ..."
#else
inline void begin() {}
inline void poll() {}
inline uint8_t count() { return 0; }
inline float value(uint8_t) { return NAN; }
inline bool takeFresh() { return false; }
inline String summary() { return "off"; }
#endif
}  // namespace temp
