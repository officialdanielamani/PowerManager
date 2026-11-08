#include "relays.h"

#include <driver/gpio.h>

#include "i2cbus.h"
#include "relaymap.h"

namespace relays {

static bool lastOk = false;
static bool boardOk[hw::RELAY_BOARD_COUNT] = {false};

// Output pattern of one board from its slice of the relay mask (16 bits: XL9535 port 0 + port 1).
static uint16_t pattern(const hw::RelayBoard &b, uint16_t on) {
  uint16_t used = b.relays >= 16 ? 0xFFFF : (uint16_t)((1u << b.relays) - 1);
  on &= used;
  // Active-high: a set bit switches the relay on. Active-low: inverted (unused outputs stay "off").
  return b.activeHigh ? on : (uint16_t)~on;
}

// First RELAY_GPIO_PINS index used by board i.
static uint8_t gpioFirst(uint8_t i) {
  uint8_t n = 0;
  for (uint8_t k = 0; k < i; k++)
    if (hw::RELAY_BOARD[k].driver == RELAY_GPIO) n += hw::RELAY_BOARD[k].relays;
  return n;
}

static bool writeBoard(uint8_t i, uint16_t p) {
  const hw::RelayBoard &b = hw::RELAY_BOARD[i];
  if (b.driver == RELAY_GPIO) {
    uint8_t first = gpioFirst(i);
    for (uint8_t k = 0; k < b.relays; k++) digitalWrite(hw::GPIO_PIN[first + k], (p >> k) & 1 ? HIGH : LOW);
    return true;
  }
  if (b.driver == RELAY_XL9535) {
    if (!i2cbus::writeReg(b.address, 0x02, (uint8_t)p)) return false;               // port 0 = relays 1..8
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
    bool ok = writeBoard(i, p);
    if (!ok) {  // one retry: a busy bus is the usual cause
      delay(2);
      ok = writeBoard(i, p);
    }
    boardOk[i] = ok;
    all &= ok;
    first += b.relays;
  }
  lastOk = all;
  return all;
}

bool begin() {
  bool all = true;
  for (uint8_t i = 0; i < hw::RELAY_BOARD_COUNT; i++) {
    const hw::RelayBoard &b = hw::RELAY_BOARD[i];
    uint16_t p = pattern(b, 0);
    bool ok;
    if (b.driver == RELAY_GPIO) {
      uint8_t first = gpioFirst(i);
      for (uint8_t k = 0; k < b.relays; k++) {
        int pin = hw::GPIO_PIN[first + k];
        digitalWrite(pin, (p >> k) & 1 ? HIGH : LOW);  // level first: no glitch when the pin becomes an output
        pinMode(pin, OUTPUT);
        // Strongest drive: an SSR input needs ~3 V and the 3.3 V rail dips when Wi-Fi transmits.
        gpio_set_drive_capability((gpio_num_t)pin, GPIO_DRIVE_CAP_3);
      }
      ok = true;
    } else if (b.driver == RELAY_XL9535) {
      i2cbus::begin();
      // Output values first, then make port 0 / port 1 outputs: no glitch when they switch from input.
      ok = i2cbus::writeReg(b.address, 0x02, (uint8_t)p) && i2cbus::writeReg(b.address, 0x03, (uint8_t)(p >> 8)) &&
           i2cbus::writeReg(b.address, 0x06, 0x00) && i2cbus::writeReg(b.address, 0x07, 0x00);
    } else {
      i2cbus::begin();
      ok = writeBoard(i, p);
    }
    boardOk[i] = ok;
    all &= ok;
    if (b.driver == RELAY_GPIO) Serial.printf("[relays] GPIO, %u relay(s)\n", b.relays);
    else
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
    if (b.driver == RELAY_GPIO) {
      String pins;
      uint8_t first = gpioFirst(i);
      for (uint8_t k = 0; k < b.relays; k++) pins += (k ? "," : "") + String(hw::GPIO_PIN[first + k]);
      snprintf(buf, sizeof(buf), "%sGPIO %s (%u)", i ? ", " : "", pins.c_str(), b.relays);
    } else {
      snprintf(buf, sizeof(buf), "%s%s @0x%02X (%u) %s", i ? ", " : "", b.driver == RELAY_XL9535 ? "XL9535" : "PCF8574",
               b.address, b.relays, boardOk[i] ? "OK" : "NOT ANSWERING");
    }
    s += buf;
  }
  return s.length() ? s : String("none");
}

}  // namespace relays
