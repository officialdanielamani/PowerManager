// Local rules: readings, over-current trip, kWh quota, session expiry, offline relay-off.
// Runs in its own task and never waits on the network (docs/firmware.md §6).
#pragma once
#include <Arduino.h>
#include <ArduinoJson.h>
#include "app_state.h"
#include "sensor_pzem.h"

namespace control {

void begin(Sensor *sensor);
void task(void *);

// Called from the net / web tasks. Return false + error code when refused.
bool setRelay(bool on, const char *src, const char *&err);
void renew(const char *reason, bool autoRenew);  // reason: "manual" / "expired"
bool resetTrip(const char *src, const char *by);
void applyConfig(const MeterConfig &c);
void endSession();               // End session (server): relay off until Renew
void setDisabled(bool disabled);  // server "disable" / "enable"
void reportNow();           // record a reading now and send everything now ("Get Data Now")
bool takeSendRequest();     // net: true once when it is time to send the buffered items

// Event into the outbox. `fill` adds extra fields.
void emitEvent(const char *e, void (*fill)(JsonObject, void *) = nullptr, void *ctx = nullptr);

uint32_t nextReset();      // epoch of the next session reset, 0 = none / unknown
double sessionKwh();       // raw (as measured)
double sessionKwhTared();  // minus own consumption (= sessionKwh when no tare)
double sessionKwhLimit();  // the value the kWh limit uses (tared if "tare_limits")
void stateJson(JsonObject o);   // protocol "state" body
uint32_t stateVersion();        // changes whenever the state changes
uint32_t relaySwitches();       // relay output changes since boot (diagnostics)
void bumpState();
void relayTest(bool on);    // serial "relay test": the rules stop writing the relays while it runs               // the state changed (also used by auxrelay)

}  // namespace control
