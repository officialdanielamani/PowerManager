// What is wired where: rbAmp boards, relay boards (relaymap.h) and the channel table
// (channel -> board + input + clamp model + relay). The compiled tables of config.h are the default hardware
// profile; the server can send another one (hwprofile.h, docs/protocol.md §12). hw::load() copies the profile in use
// into the run-time tables below, once at start-up (a new profile is used after a restart; clamp models are reloaded
// live by hw::reload()). Everything else reads these tables as before.
#pragma once
#include <Arduino.h>
#include "../config.h"
#include "hwprofile.h"
#include "relaymap.h"

namespace hw {

// A channel as written in config.h (CHANNELS): the relay is a board of the relay table + an output on it.
struct ChannelCfg {
  uint8_t board;       // 1 = first entry of RBAMP_BOARDS
  uint8_t input;       // current input on that board, 1..7
  uint8_t clamp;       // SCT_* model code written to the module (SCT_KEEP = leave as it is)
  uint8_t relayBoard;  // 1 = first entry of RELAY_BOARDS; 0 = no relay
  uint8_t relayOut;    // output on that relay board, 1..its "relays used"; 0 when relayBoard is 0
};

// A channel as the firmware uses it: the relay as a number across all relay boards (1 = first output of the first
// board, the board's outputs follow, then the next board ...). This is also the number of the AUX relays.
struct ChannelMap {
  uint8_t board;
  uint8_t input;
  uint8_t clamp;
  uint8_t relay;    // 0 = no relay
};

// ---- compile-time default (config.h), checked here
static constexpr uint8_t DEFAULT_BOARD_ADDR[] = RBAMP_BOARDS;
static constexpr ChannelCfg DEFAULT_CHANNEL[] = CHANNELS;
static constexpr uint8_t DEFAULT_BOARDS = sizeof(DEFAULT_BOARD_ADDR) / sizeof(DEFAULT_BOARD_ADDR[0]);
static constexpr uint8_t DEFAULT_NCH = sizeof(DEFAULT_CHANNEL) / sizeof(DEFAULT_CHANNEL[0]);

static_assert(DEFAULT_BOARDS >= 1 && DEFAULT_BOARDS <= HWP_MAX_BOARDS, "RBAMP_BOARDS: 1 to 8 rbAmp boards");
static_assert(DEFAULT_NCH >= 1 && DEFAULT_NCH <= MC_MAX_CH && MC_MAX_CH <= HWP_MAX_CH, "CHANNELS: 1 to MC_MAX_CH (16) channels");

constexpr uint8_t relaysOfBoard(uint8_t rb) {  // outputs used on relay board rb (1-based), 0 if there is none
  return rb >= 1 && rb <= DEFAULT_RELAY_BOARD_COUNT ? DEFAULT_RELAY_BOARD[rb - 1].relays : 0;
}
constexpr bool relayValid(const ChannelCfg &c) {
  return c.relayBoard == 0 ? c.relayOut == 0 : (c.relayBoard <= DEFAULT_RELAY_BOARD_COUNT && c.relayOut >= 1 && c.relayOut <= relaysOfBoard(c.relayBoard));
}
constexpr bool channelsValid(uint8_t i = 0) {
  return i >= DEFAULT_NCH ? true
                          : (DEFAULT_CHANNEL[i].board >= 1 && DEFAULT_CHANNEL[i].board <= DEFAULT_BOARDS &&
                             DEFAULT_CHANNEL[i].input >= 1 && DEFAULT_CHANNEL[i].input <= 7 &&
                             relayValid(DEFAULT_CHANNEL[i]) && channelsValid(i + 1));
}
static_assert(channelsValid(), "CHANNELS: board 1..number of RBAMP_BOARDS, input 1..7, relay board 0..number of RELAY_BOARDS and output 1..its relays (both 0 = no relay)");

constexpr bool noDuplicate(uint8_t i = 0, uint8_t j = 1) {
  return i >= DEFAULT_NCH ? true
         : j >= DEFAULT_NCH ? noDuplicate(i + 1, i + 2)
         : ((DEFAULT_CHANNEL[i].board == DEFAULT_CHANNEL[j].board && DEFAULT_CHANNEL[i].input == DEFAULT_CHANNEL[j].input) ||
            (DEFAULT_CHANNEL[i].relayBoard && DEFAULT_CHANNEL[i].relayBoard == DEFAULT_CHANNEL[j].relayBoard &&
             DEFAULT_CHANNEL[i].relayOut == DEFAULT_CHANNEL[j].relayOut))
             ? false
             : noDuplicate(i, j + 1);
}
static_assert(noDuplicate(), "CHANNELS: the same board input or the same relay output is used twice");

// ---- run-time tables
inline uint8_t BOARDS = 0;
inline uint8_t BOARD_ADDR[HWP_MAX_BOARDS];
inline uint8_t NCH = 0;
inline ChannelMap CHANNEL[HWP_MAX_CH];
inline uint8_t AUX = 0;  // relays that no channel uses

// AUX relays: relays that no channel uses (manual ON / OFF only), in relay-number order.
inline bool relayUsed(uint8_t relay) {
  for (uint8_t i = 0; i < NCH; i++)
    if (CHANNEL[i].relay == relay) return true;
  return false;
}
// Relay number (1-based) of AUX relay j (0-based).
inline uint8_t auxRelay(uint8_t j) {
  for (uint8_t r = 1; r <= RELAYS; r++)
    if (!relayUsed(r) && j-- == 0) return r;
  return 0;
}

// The compiled default as a profile (version 0).
inline hwp::Profile defaultProfile() {
  hwp::Profile p;
  p.nBoards = DEFAULT_BOARDS;
  for (uint8_t i = 0; i < DEFAULT_BOARDS; i++) p.addr[i] = DEFAULT_BOARD_ADDR[i];
  p.nRelayBoards = DEFAULT_RELAY_BOARD_COUNT;
  for (uint8_t i = 0; i < DEFAULT_RELAY_BOARD_COUNT; i++)
    p.relay[i] = {(uint8_t)(i + 1), DEFAULT_RELAY_BOARD[i].driver, DEFAULT_RELAY_BOARD[i].address,
                  DEFAULT_RELAY_BOARD[i].relays, DEFAULT_RELAY_BOARD[i].activeHigh};  // Board A, B, C ... = position in config.h
  p.nCh = DEFAULT_NCH;
  for (uint8_t i = 0; i < DEFAULT_NCH; i++)
    p.ch[i] = {DEFAULT_CHANNEL[i].board, DEFAULT_CHANNEL[i].input, DEFAULT_CHANNEL[i].clamp, DEFAULT_CHANNEL[i].relayBoard,
               DEFAULT_CHANNEL[i].relayOut};
  return p;
}

// Copies the profile in use into the tables above.
inline void reload() {
  const hwp::Profile &p = hwp::active();
  BOARDS = p.nBoards;
  for (uint8_t i = 0; i < p.nBoards; i++) BOARD_ADDR[i] = p.addr[i];
  RELAY_BOARD_COUNT = p.nRelayBoards;
  RELAYS = 0;
  for (uint8_t i = 0; i < p.nRelayBoards; i++) {
    RELAY_BOARD[i] = {p.relay[i].driver, p.relay[i].address, p.relay[i].relays, p.relay[i].activeHigh};
    RELAYS += p.relay[i].relays;
  }
  NCH = p.nCh;
  for (uint8_t i = 0; i < p.nCh; i++) {
    uint8_t relay = hwp::relayNumber(p, p.ch[i].rb, p.ch[i].ro);  // (relay board id, output) -> number across all boards
    CHANNEL[i] = {p.ch[i].board, p.ch[i].input, p.ch[i].clamp, relay};
  }
  uint8_t aux = 0;
  for (uint8_t r = 1; r <= RELAYS; r++)
    if (!relayUsed(r)) aux++;
  AUX = aux;
}

}  // namespace hw
