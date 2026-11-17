// Relay outputs of the rbAmp meter. Relays are numbered 1..RELAYS across the entries of the relay table.
// The table comes from the hardware profile (hwprofile.h): the compiled RELAY_BOARDS of config.h unless the server
// sent another one (hw::load() in hwmap.h fills the run-time tables below). Pins (RELAY_GPIO_PINS) stay compile-time.
// NOT shared with the PZEM sketch any more (it keeps its compile-time table).
#pragma once
#include <Arduino.h>
#include "../config.h"
#include "hwprofile.h"

#define RELAY_PCF8574 1  // PCF8574 / PCF8574A I/O expander (up to 8 outputs)
#define RELAY_XL9535 2   // XL9535 / TCA9535 relay board (up to 16 outputs: port 0 = 1..8, port 1 = 9..16)
#define RELAY_GPIO 3     // ESP32 pins, taken in order from RELAY_GPIO_PINS (the address is not used)
#define RELAY_MCP23017 4 // MCP23017 I/O expander, 16 push-pull outputs: GPA0..7 = 1..8, GPB0..7 = 9..16

static_assert(RELAY_PCF8574 == HWP_DRV_PCF8574 && RELAY_XL9535 == HWP_DRV_XL9535 && RELAY_GPIO == HWP_DRV_GPIO &&
                  RELAY_MCP23017 == HWP_DRV_MCP23017,
              "relay driver numbers must match hwprofile.h");

#ifndef RELAY_GPIO_PINS
#define RELAY_GPIO_PINS { -1 }
#endif

namespace hw {

struct RelayBoard {
  uint8_t driver;   // RELAY_MCP23017 / RELAY_PCF8574 / RELAY_XL9535 / RELAY_GPIO
  uint8_t address;  // I2C address (RELAY_GPIO: 0)
  uint8_t relays;   // outputs used, from output 0 (MCP23017 / XL9535 0..15, PCF8574 0..7, GPIO: RELAY_GPIO_PINS)
  uint8_t activeHigh;
};

// ---- compile-time default (config.h), checked here
static constexpr RelayBoard DEFAULT_RELAY_BOARD[] = RELAY_BOARDS;
static constexpr int8_t GPIO_PIN[] = RELAY_GPIO_PINS;
static constexpr uint8_t DEFAULT_RELAY_BOARD_COUNT = sizeof(DEFAULT_RELAY_BOARD) / sizeof(DEFAULT_RELAY_BOARD[0]);
static constexpr uint8_t RELAY_GPIO_COUNT = GPIO_PIN[0] < 0 ? 0 : sizeof(GPIO_PIN) / sizeof(GPIO_PIN[0]);

constexpr uint8_t sumRelays(uint8_t i = 0) {
  return i >= DEFAULT_RELAY_BOARD_COUNT ? 0 : DEFAULT_RELAY_BOARD[i].relays + sumRelays(i + 1);
}
constexpr uint8_t sumGpio(uint8_t i = 0) {
  return i >= DEFAULT_RELAY_BOARD_COUNT ? 0 : (DEFAULT_RELAY_BOARD[i].driver == RELAY_GPIO ? DEFAULT_RELAY_BOARD[i].relays : 0) + sumGpio(i + 1);
}
constexpr bool boardsValid(uint8_t i = 0) {
  return i >= DEFAULT_RELAY_BOARD_COUNT ? true
         : (DEFAULT_RELAY_BOARD[i].driver == RELAY_PCF8574 ? DEFAULT_RELAY_BOARD[i].relays <= 8
            : DEFAULT_RELAY_BOARD[i].driver == RELAY_XL9535 ? DEFAULT_RELAY_BOARD[i].relays <= 16
            : DEFAULT_RELAY_BOARD[i].driver == RELAY_MCP23017 ? DEFAULT_RELAY_BOARD[i].relays <= 16
            : DEFAULT_RELAY_BOARD[i].driver == RELAY_GPIO) && boardsValid(i + 1);
}
static_assert(DEFAULT_RELAY_BOARD_COUNT <= HWP_MAX_RELAY_BOARDS, "RELAY_BOARDS: at most 8 entries");
static_assert(sumRelays() <= HWP_MAX_RELAYS, "RELAY_BOARDS: at most 32 relays in total");
static_assert(boardsValid(), "RELAY_BOARDS: driver must be RELAY_MCP23017 (up to 16), RELAY_XL9535 (up to 16), RELAY_PCF8574 (up to 8) or RELAY_GPIO");
static_assert(sumGpio() <= RELAY_GPIO_COUNT, "RELAY_GPIO: list a pin in RELAY_GPIO_PINS for every GPIO relay");

// ---- run-time tables (filled by hw::load() before anything else uses them)
inline RelayBoard RELAY_BOARD[HWP_MAX_RELAY_BOARDS];
inline uint8_t RELAY_BOARD_COUNT = 0;
inline uint8_t RELAYS = 0;  // all relay outputs

}  // namespace hw
