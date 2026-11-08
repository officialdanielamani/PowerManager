// Optional battery voltage via an ADC + resistor divider (config.h: BATTERY_ENABLE).
#pragma once
#include <Arduino.h>

namespace battery {
bool enabled();
float volts();    // averaged reading in V (0 when disabled)
int percent(float v);  // rough state of charge, 0-100 (linear between empty and full)
}
