// Wall-clock time. Sources by priority: server > NTP > DS3231 > browser.
#pragma once
#include <Arduino.h>

enum TimeSource : uint8_t { TS_NONE = 0, TS_BROWSER = 1, TS_RTC = 2, TS_NTP = 3, TS_SERVER = 4 };

namespace timekeep {
void begin();
void loop();  // call from the net task: detects NTP sync
bool valid();
uint32_t now();  // epoch seconds (meaningless if !valid())
void set(uint32_t epoch, TimeSource src);
TimeSource source();
const char *sourceName();
String format(uint32_t epoch);  // "YYYY-MM-DD HH:MM:SS" UTC
}  // namespace timekeep
