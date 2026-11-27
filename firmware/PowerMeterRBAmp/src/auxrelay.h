// AUX relays: relay outputs that no measured channel uses (see hwmap.h). Each one is
//   - manual   : ON / OFF from the server or the meter's own page
//   - schedule : up to 14 time windows (local time; chosen week days or one date) with ON or OFF, otherwise a
//                default state
//   - event    : ON / OFF while a condition holds (temperature, humidity, current, power, kWh balance, session,
//                tamper, over-current trip, voltage protection, no Wi-Fi, no server, UPS input, battery %); per channel or any channel
// "Override" lets a manual ON / OFF win over the automation until the automation changes state again.
// Runs on the meter, so it keeps working offline. Shared by both firmwares (firmware/tools/check_shared.py).
#pragma once
#include <Arduino.h>
#include <ArduinoJson.h>

namespace auxrelay {

static const uint8_t MAX = 16;      // AUX relays handled (names / automation)
static const uint8_t MAX_CH = 16;   // measured channels seen by event rules
static const uint8_t SCHED_MAX = 14;

// Live values for event rules, filled by control every pass.
struct Inputs {
  uint8_t nch = 0;
  bool readOk = false;
  float a[MAX_CH] = {0}, w[MAX_CH] = {0};
  float balance[MAX_CH];   // kWh left in the session that governs the channel (NAN = no limit / no session)
  bool running[MAX_CH] = {false};  // that session is running (relay allowed by the session)
  bool tripped[MAX_CH] = {false};  // over-current trip latched on the channel
  bool tamper = false;     // tamper input active
  bool voltOut = false;    // voltage outside the allowed range
  bool noWifi = false;     // Wi-Fi not connected
  bool noServer = false;   // paired, but no link to the server (also: internet problem)
  bool ups = false;        // UPS input active: running on the UPS (UPS_ENABLE)
  bool reboot = false;     // a restart for a new hardware profile is counting down (hwprofile.h)
  float batPct = NAN;      // battery % remaining (BATTERY_ENABLE), NAN = not measured
  bool disabled = false;   // meter disabled by the server: every AUX relay off
  Inputs() {
    for (uint8_t i = 0; i < MAX_CH; i++) balance[i] = NAN;
  }
};

void begin();                       // load names, automation, manual states (NVS)
uint8_t count();                    // AUX relays on this meter
const char *name(uint8_t i);

// Server config "aux": [...] (+ "tz_min"). parse() checks and stages; commit() applies and saves.
bool parse(JsonArrayConst list, int tzMin, const char *&err);
void commit();

void tick(const Inputs &in);        // every control pass: runs the automation
uint32_t outputMask();              // relay bits (bit k = relay k+1) that are ON now
bool set(uint8_t i, bool on, const char *src, const char *&err);  // manual ON / OFF
void stateJson(JsonArray a);        // state "aux": [{"i", "relay", "on", "mode", "override"?}]
void configJson(JsonArray a);       // the active automation (local page)
void wipe();                        // factory reset

}  // namespace auxrelay
