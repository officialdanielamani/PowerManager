// Temperature (and humidity) sensors, shared by both firmwares (firmware/tools/check_shared.py):
//   - SHT20 / SHT3x / SHT4x on the I2C bus (SHT_SENSORS in config.h): temperature + humidity
//   - DS18B20 on one 1-Wire pin (PIN_TEMP): temperature; own small driver, no library to install
// Numbering: T1, T2 ... = the SHT sensors in config order, then the DS18B20s sorted by serial number,
// so a sensor keeps its number over a restart.
#pragma once
#include <Arduino.h>
#include "../config.h"

#define SHT_20 20  // SHT20 / HTU21D, address 0x40
#define SHT_3X 30  // SHT30 / SHT31 / SHT35, address 0x44 or 0x45
#define SHT_4X 40  // SHT40 / SHT41 / SHT45, address 0x44 (0x45 / 0x46 variants)

#ifndef TEMP_DS18B20
#define TEMP_DS18B20 0
#endif
#ifndef TEMP_SHT
#define TEMP_SHT 0
#endif
#ifndef TEMP_ENABLE
#define TEMP_ENABLE (TEMP_DS18B20 || TEMP_SHT)
#endif

namespace temp {
#if TEMP_ENABLE
void begin();                // finds the sensors
void poll();                 // call often (control loop): measures every TEMP_READ_MS, one sensor per call
uint8_t count();             // sensors (SHT configured + DS18B20 found)
float value(uint8_t i);      // degC; NAN = not readable
float humidity(uint8_t i);   // %RH (SHT only); NAN = none
bool hasHumidity();          // at least one SHT sensor configured
bool takeFresh();            // true once after every complete round of readings (record them)
String summary();            // "T1 SHT40 @0x44 24.5 C 55 %RH, T2 DS18B20 28-0316a2794e1f 24.5 C"
#else
inline void begin() {}
inline void poll() {}
inline uint8_t count() { return 0; }
inline float value(uint8_t) { return NAN; }
inline float humidity(uint8_t) { return NAN; }
inline bool hasHumidity() { return false; }
inline bool takeFresh() { return false; }
inline String summary() { return "off"; }
#endif
}  // namespace temp
