// Relay map of the single-channel (PZEM) meter: CHANNEL_RELAY switches the measured line, every other
// relay output (RELAY_BOARDS in config.h) is an AUX relay (manual / scheduled / event-driven).
#pragma once
#include <Arduino.h>
#include "../config.h"
#include "relaymap.h"

#ifndef CHANNEL_RELAY
#define CHANNEL_RELAY 1
#endif

namespace hw {

static_assert(CHANNEL_RELAY >= 0 && CHANNEL_RELAY <= RELAYS, "CHANNEL_RELAY: 0 (none) or a relay number from RELAY_BOARDS");

constexpr bool relayUsed(uint8_t relay) { return relay == CHANNEL_RELAY; }
constexpr uint8_t countAux(uint8_t r = 1) { return r > RELAYS ? 0 : (relayUsed(r) ? 0 : 1) + countAux(r + 1); }
static constexpr uint8_t AUX = countAux();
// Relay number (1-based) of AUX relay j (0-based).
inline uint8_t auxRelay(uint8_t j) {
  for (uint8_t r = 1; r <= RELAYS; r++)
    if (!relayUsed(r) && j-- == 0) return r;
  return 0;
}

}  // namespace hw
