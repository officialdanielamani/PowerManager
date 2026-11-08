// rbAmp BASIC power modules over I2C (no external library). See docs/multichannel.md and
// https://github.com/rb-amp/rbamp-docs (BASIC 8-bit register map).
//   - one or more modules (RBAMP_BOARDS); channels come from the CHANNELS table (src/hwmap.h)
//   - variant (inputs, voltage input) of each module from HW_VARIANT 0x55 / CAPABILITY 0x57
//   - real-time block 0x86..0xBD (one burst, coherent) for inputs 1..3, channel window 0x32/0x3C for 4..7
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

// Finds the boards that did not answer yet, checks the CHANNELS table against their variants and
// writes the configured clamp models. Safe to call again. Returns true when every board is ready.
bool begin();
bool present();             // every board found and the CHANNELS table fits them
uint8_t channels();         // channels in the CHANNELS table
bool hasVoltage();
const char *variantName();  // "UI2", or "UI5+I7" with several boards
float rating(uint8_t ch);   // nominal A of the clamp model on that channel (0 = unknown)
String clampSummary();      // "CH1 SCT-013 100 A (code 5), CH2 ..." as applied on the modules
String boardSummary();      // "B1 UI5 @0x50 OK, B2 I7 @0x51 NOT FOUND"

// Reads the latest 100 ms window of every board. On failure returns false and sets lastError() (SensorErr).
bool read(Sample &out);
uint8_t lastError();

// Energy period: start() closes the running period on every module; ~50 ms later finish() reads the
// average power of every channel over it. `valid` = false for a primer / restarted / stale period
// (discard it). avgW[] is >= 0 (BASIC counts consumption only).
bool periodStart();
bool periodFinish(float avgW[MC_MAX_CH], bool &valid);

// One-time I2C address change of a module (two-phase commit + reset). Connect only that module.
bool changeAddress(uint8_t from, uint8_t to, String &msg);

}  // namespace rbamp
