#include "relays.h"

#include "../config.h"
#include "hwmap.h"
#include "i2cbus.h"

namespace relays {

static bool lastOk = false;
static bool boardOk[hw::RELAY_BOARD_COUNT ? hw::RELAY_BOARD_COUNT : 1] = {false};

// Output pattern of one board from its slice of the relay mask (16 bits: XL9535 port 0 + port 1).
static uint16_t pattern(const hw::RelayBoard &b, uint16_t on) {
  uint16_t used = b.relays >= 16 ? 0xFFFF : (uint16_t)((1u << b.relays) - 1);
  on &= used;
  // Active-high: a set bit switches the relay on. Active-low: inverted (unused outputs stay "off").
  return b.activeHigh ? on : (uint16_t)~on;
}

static bool writeBoard(const hw::RelayBoard &b, uint16_t p) {
  if (b.driver == RELAY_XL9535) {
    if (!i2cbus::writeReg(b.address, 0x02, (uint8_t)p)) return false;  // output port 0 = relays 1..8
    return b.relays <= 8 || i2cbus::writeReg(b.address, 0x03, (uint8_t)(p >> 8));  // port 1 = 9..16
  }
  uint8_t v = (uint8_t)p;
  return i2cbus::writeRaw(b.address, &v, 1);  // PCF8574: the byte is the output state
}

bool write(uint32_t onMask) {
  bool all = true;
  uint8_t first = 0;  // first relay (0-based) of the board
  for (uint8_t i = 0; i < hw::RELAY_BOARD_COUNT; i++) {
    const hw::RelayBoard &b = hw::RELAY_BOARD[i];
    uint16_t p = pattern(b, (uint16_t)(onMask >> first));
    bool ok = writeBoard(b, p);
    if (!ok) {  // one retry: a busy bus is the usual cause
      delay(2);
      ok = writeBoard(b, p);
    }
    boardOk[i] = ok;
    all &= ok;
    first += b.relays;
  }
  lastOk = all;
  return all;
}

bool begin() {
  i2cbus::begin();
  bool all = true;
  for (uint8_t i = 0; i < hw::RELAY_BOARD_COUNT; i++) {
    const hw::RelayBoard &b = hw::RELAY_BOARD[i];
    bool ok;
    if (b.driver == RELAY_XL9535) {
      // Output values first, then make port 0 / port 1 outputs: no glitch when they switch from input.
      uint16_t p = pattern(b, 0);
      ok = i2cbus::writeReg(b.address, 0x02, (uint8_t)p) && i2cbus::writeReg(b.address, 0x03, (uint8_t)(p >> 8)) &&
           i2cbus::writeReg(b.address, 0x06, 0x00) && i2cbus::writeReg(b.address, 0x07, 0x00);
    } else {
      ok = writeBoard(b, pattern(b, 0));
    }
    boardOk[i] = ok;
    all &= ok;
    Serial.printf("[relays] %s at 0x%02X, %u relay(s): %s\n", b.driver == RELAY_XL9535 ? "XL9535" : "PCF8574",
                  b.address, b.relays, ok ? "OK" : "NOT ANSWERING (check wiring / address)");
  }
  lastOk = all;
  return all;
}

bool ok() { return lastOk; }

String summary() {
  String s;
  for (uint8_t i = 0; i < hw::RELAY_BOARD_COUNT; i++) {
    const hw::RelayBoard &b = hw::RELAY_BOARD[i];
    char buf[48];
    snprintf(buf, sizeof(buf), "%s%s @0x%02X (%u) %s", i ? ", " : "", b.driver == RELAY_XL9535 ? "XL9535" : "PCF8574",
             b.address, b.relays, boardOk[i] ? "OK" : "NOT ANSWERING");
    s += buf;
  }
  return s.length() ? s : String("none");
}

}  // namespace relays
