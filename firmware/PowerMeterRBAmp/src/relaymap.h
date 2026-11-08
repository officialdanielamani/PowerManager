// Relay outputs from config.h (RELAY_BOARDS, RELAY_GPIO_PINS), fixed at compile time. Shared by both
// firmwares (firmware/tools/check_shared.py). Relays are numbered 1..RELAYS across the entries, in order.
#pragma once
#include <Arduino.h>
#include "../config.h"

#define RELAY_PCF8574 1  // PCF8574 / PCF8574A I/O expander (up to 8 outputs)
#define RELAY_XL9535 2   // XL9535 / TCA9535 relay board (up to 16 outputs: port 0 = 1..8, port 1 = 9..16)
#define RELAY_GPIO 3     // ESP32 pins, taken in order from RELAY_GPIO_PINS (the address is not used)

#ifndef RELAY_GPIO_PINS
#define RELAY_GPIO_PINS { -1 }
#endif

namespace hw {

struct RelayBoard {
  uint8_t driver;   // RELAY_PCF8574 / RELAY_XL9535 / RELAY_GPIO
  uint8_t address;  // I2C address (RELAY_GPIO: 0)
  uint8_t relays;   // outputs used, from output 0 (PCF8574 0..7, XL9535 0..15, GPIO: pins from RELAY_GPIO_PINS)
  uint8_t activeHigh;
};

static constexpr RelayBoard RELAY_BOARD[] = RELAY_BOARDS;
static constexpr int8_t GPIO_PIN[] = RELAY_GPIO_PINS;
static constexpr uint8_t RELAY_BOARD_COUNT = sizeof(RELAY_BOARD) / sizeof(RELAY_BOARD[0]);
static constexpr uint8_t RELAY_GPIO_COUNT = GPIO_PIN[0] < 0 ? 0 : sizeof(GPIO_PIN) / sizeof(GPIO_PIN[0]);

constexpr uint8_t sumRelays(uint8_t i = 0) {
  return i >= RELAY_BOARD_COUNT ? 0 : RELAY_BOARD[i].relays + sumRelays(i + 1);
}
constexpr uint8_t sumGpio(uint8_t i = 0) {
  return i >= RELAY_BOARD_COUNT ? 0 : (RELAY_BOARD[i].driver == RELAY_GPIO ? RELAY_BOARD[i].relays : 0) + sumGpio(i + 1);
}
constexpr bool boardsValid(uint8_t i = 0) {
  return i >= RELAY_BOARD_COUNT ? true
         : (RELAY_BOARD[i].driver == RELAY_PCF8574 ? RELAY_BOARD[i].relays <= 8
            : RELAY_BOARD[i].driver == RELAY_XL9535 ? RELAY_BOARD[i].relays <= 16
            : RELAY_BOARD[i].driver == RELAY_GPIO) && boardsValid(i + 1);
}
static constexpr uint8_t RELAYS = sumRelays();  // all relay outputs

static_assert(RELAYS <= 32, "RELAY_BOARDS: at most 32 relays in total");
static_assert(boardsValid(), "RELAY_BOARDS: driver must be RELAY_PCF8574 (up to 8), RELAY_XL9535 (up to 16) or RELAY_GPIO");
static_assert(sumGpio() <= RELAY_GPIO_COUNT, "RELAY_GPIO: list a pin in RELAY_GPIO_PINS for every GPIO relay");

}  // namespace hw
