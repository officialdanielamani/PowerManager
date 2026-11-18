#pragma once
#include <Arduino.h>
#define RELAY_PCF8574 1
#define RELAY_XL9535 2
#define RELAY_GPIO 3
#define RELAY_MCP23017 4
#define HWP_MAX_RELAY_BOARDS 8   // (the rbAmp relays.cpp sizes its status array with it)
namespace hw {
struct RelayBoard { uint8_t driver, address, relays, activeHigh; };
inline RelayBoard RELAY_BOARD[3];                   // the table of the test: 3 boards (the PZEM relays.cpp needs a constant count)
static constexpr uint8_t RELAY_BOARD_COUNT = 3;
static constexpr int8_t GPIO_PIN[] = {-1};
}
