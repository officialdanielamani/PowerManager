// Multi-channel control: sensor (rbAmp), per-channel relays and limits, shared / independent sessions.
// Runs in its own task on core 1 and never waits on the network.
#pragma once
#include <Arduino.h>
#include <ArduinoJson.h>
#include "app_state.h"

namespace control {

void begin();
void task(void *);

// Commands (local page or server). ch: 0..channels-1; -1 = all channels / the shared session.
bool setRelay(uint8_t ch, bool on, const char *src, const char *&err);
void renew(int8_t ch, const char *reason);  // shared: the session; independent: that channel / all
void endSessions(int8_t ch);                // End / End all (relay off until renewed)
bool resetTrip(int8_t ch, const char *src, const char *by);
void applyConfig(const MeterConfig &c);
void setDisabled(bool disabled);  // server "disable" / "enable"
void reportNow();           // record a reading now and send everything now ("Get Data Now")
bool takeSendRequest();     // net: true once when it is time to send the buffered items

// Event into the outbox. `fill` adds extra fields.
void emitEvent(const char *e, void (*fill)(JsonObject, void *) = nullptr, void *ctx = nullptr);

uint32_t nextReset(uint8_t slot);  // epoch of the next session reset of a slot, 0 = none / unknown
double slotKwh(uint8_t slot);      // kWh used in the session of a slot
void stateJson(JsonObject o);      // protocol "state" body
uint32_t stateVersion();           // changes whenever the state changes
uint32_t relaySwitches();          // relay output changes since boot (diagnostics)

}  // namespace control
