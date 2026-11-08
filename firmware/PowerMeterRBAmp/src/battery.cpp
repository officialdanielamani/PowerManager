#include "battery.h"

#include "../config.h"

namespace battery {

bool enabled() { return BATTERY_ENABLE != 0; }

float volts() {
#if BATTERY_ENABLE
  uint32_t mv = 0;
  for (int i = 0; i < 16; i++) mv += analogReadMilliVolts(PIN_BATTERY_ADC);  // calibrated by the core
  return (mv / 16.0f) / 1000.0f * BATTERY_DIVIDER;
#else
  return 0;
#endif
}

int percent(float v) {
  float p = (v - BATTERY_V_EMPTY) / (BATTERY_V_FULL - BATTERY_V_EMPTY) * 100.0f;
  return (int)constrain(p, 0.0f, 100.0f);
}

}  // namespace battery
