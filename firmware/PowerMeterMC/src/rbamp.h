// rbAmp BASIC power module over I2C (no external library). See docs/multichannel.md and
// https://github.com/rb-amp/rbamp-docs (BASIC 8-bit register map).
//   - variant (channels, voltage input) from HW_VARIANT 0x55 / CAPABILITY 0x57
//   - real-time block 0x86..0xBD (one burst, coherent) for ch 0..2, channel window 0x32/0x3C for ch 3..6
//   - energy: CMD_LATCH_PERIOD (0x27) closes a period; average power x master dt (the module keeps no kWh)
#pragma once
#include <Arduino.h>
#include "app_state.h"

namespace rbamp {

struct Sample {
  float v = 0;
  uint8_t hz = 0;
  float a[MC_MAX_CH] = {0};
  float w[MC_MAX_CH] = {0};
  float pf[MC_MAX_CH] = {0};
};

// Finds the module, reads its variant and writes the configured SCT-013 models. Safe to call again
// (e.g. when the module was missing at boot). Returns false if the module does not answer.
bool begin();
bool present();
uint8_t channels();
bool hasVoltage();
const char *variantName();  // "UI2", "I3", ...
float rating(uint8_t ch);   // nominal A of the clamp model on that channel (0 = unknown)

// Reads the latest 100 ms window. On failure returns false and sets lastError() (SensorErr).
bool read(Sample &out);
uint8_t lastError();

// Energy period: start() closes the running period on the module; ~50 ms later finish() reads the
// average power of every channel over it. `valid` = false for a primer / restarted / stale period
// (discard it). avgW[] is >= 0 (BASIC counts consumption only).
bool periodStart();
bool periodFinish(float avgW[MC_MAX_CH], bool &valid);

}  // namespace rbamp
