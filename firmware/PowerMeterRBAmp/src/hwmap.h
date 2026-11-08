// What is wired where, from config.h: rbAmp boards and the CHANNELS table (relay boards: relaymap.h)
// (channel -> board + input + clamp model + relay). Everything here is fixed at compile time.
#pragma once
#include <Arduino.h>
#include "../config.h"
#include "relaymap.h"

namespace hw {

struct ChannelMap {
  uint8_t board;    // 1 = first entry of RBAMP_BOARDS
  uint8_t input;    // current input on that board, 1..7
  uint8_t clamp;    // SCT_* model code written to the module (SCT_KEEP = leave as it is)
  uint8_t relay;    // relay number (1 = first relay of the first relay board ...); 0 = no relay
};

static constexpr uint8_t BOARD_ADDR[] = RBAMP_BOARDS;
static constexpr ChannelMap CHANNEL[] = CHANNELS;

static constexpr uint8_t BOARDS = sizeof(BOARD_ADDR) / sizeof(BOARD_ADDR[0]);
static constexpr uint8_t NCH = sizeof(CHANNEL) / sizeof(CHANNEL[0]);

static_assert(BOARDS >= 1 && BOARDS <= 8, "RBAMP_BOARDS: 1 to 8 rbAmp boards");
static_assert(NCH >= 1 && NCH <= MC_MAX_CH, "CHANNELS: 1 to MC_MAX_CH (16) channels");

constexpr bool channelsValid(uint8_t i = 0) {
  return i >= NCH ? true
                  : (CHANNEL[i].board >= 1 && CHANNEL[i].board <= BOARDS && CHANNEL[i].input >= 1 &&
                     CHANNEL[i].input <= 7 && CHANNEL[i].relay <= RELAYS && channelsValid(i + 1));
}
static_assert(channelsValid(), "CHANNELS: board must be 1..number of RBAMP_BOARDS, input 1..7, relay 0..total relays");

constexpr bool noDuplicate(uint8_t i = 0, uint8_t j = 1) {
  return i >= NCH ? true
         : j >= NCH ? noDuplicate(i + 1, i + 2)
         : ((CHANNEL[i].board == CHANNEL[j].board && CHANNEL[i].input == CHANNEL[j].input) ||
            (CHANNEL[i].relay && CHANNEL[i].relay == CHANNEL[j].relay))
             ? false
             : noDuplicate(i, j + 1);
}
static_assert(noDuplicate(), "CHANNELS: the same board input or the same relay is used twice");

// AUX relays: relays that no channel uses (manual ON / OFF only), in relay-number order.
constexpr bool relayUsed(uint8_t relay, uint8_t i = 0) {
  return i >= NCH ? false : (CHANNEL[i].relay == relay || relayUsed(relay, i + 1));
}
constexpr uint8_t countAux(uint8_t r = 1) { return r > RELAYS ? 0 : (relayUsed(r) ? 0 : 1) + countAux(r + 1); }
static constexpr uint8_t AUX = countAux();
// Relay number (1-based) of AUX relay j (0-based).
inline uint8_t auxRelay(uint8_t j) {
  for (uint8_t r = 1; r <= RELAYS; r++)
    if (!relayUsed(r) && j-- == 0) return r;
  return 0;
}

}  // namespace hw
