#include "relays.h"

#include <driver/gpio.h>

#include "i2cbus.h"
#include "relaymap.h"

namespace relays {

static bool lastOk = false;

static const char *driverName(uint8_t d) {
  return d == RELAY_MCP23017 ? "MCP23017" : d == RELAY_XL9535 ? "XL9535" : d == RELAY_PCF8574 ? "PCF8574" : "GPIO";
}
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

// The two direction registers (reg, reg + 1) read back as 0x00: a real expander answers like that, an empty address does not.
static bool readsZero(uint8_t addr, uint8_t reg) {
  uint8_t d[2] = {0xFF, 0xFF};
  return i2cbus::readRegs(addr, reg, d, 2) && d[0] == 0x00 && d[1] == 0x00;
}

static bool writeBoard(uint8_t i, uint16_t p) {
  const hw::RelayBoard &b = hw::RELAY_BOARD[i];
  if (b.driver == RELAY_GPIO) {
    uint8_t first = gpioFirst(i);
    for (uint8_t k = 0; k < b.relays; k++) digitalWrite(hw::GPIO_PIN[first + k], (p >> k) & 1 ? HIGH : LOW);
    return true;
  }
  // XL9535 / MCP23017: the output state AND the direction (output) are sent every time, state first (no glitch). A
  // board that lost power, or was plugged in after the start, comes back as inputs: without the direction
  // registers its relays would never switch again until the meter restarts.
  // An rbAmp on the bus ACKs EVERY address, so a write that was "acknowledged" does not prove the board is there: the direction
  // registers are read back (they must be 0x00 = all outputs) and that is what counts.
  if (b.driver == RELAY_XL9535) {
    return i2cbus::writeReg(b.address, 0x02, (uint8_t)p) && i2cbus::writeReg(b.address, 0x03, (uint8_t)(p >> 8)) &&  // output ports 0 / 1
           i2cbus::writeReg(b.address, 0x06, 0x00) && i2cbus::writeReg(b.address, 0x07, 0x00) &&                      // configuration: all outputs
           readsZero(b.address, 0x06);
  }
  if (b.driver == RELAY_MCP23017) {  // OLATA / OLATB (GPA = 1..8, GPB = 9..16), then IODIRA / IODIRB, IOCON.BANK = 0
    return i2cbus::writeReg(b.address, 0x14, (uint8_t)p) && i2cbus::writeReg(b.address, 0x15, (uint8_t)(p >> 8)) &&
           i2cbus::writeReg(b.address, 0x00, 0x00) && i2cbus::writeReg(b.address, 0x01, 0x00) && readsZero(b.address, 0x00);
  }
  uint8_t v = (uint8_t)p;
  return i2cbus::writeRaw(b.address, &v, 1);  // PCF8574: the byte is the output state
}

// Writes every board (state + direction), also while one does not answer: it is tried again on every call.
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
    } else if (b.driver == RELAY_MCP23017) {
      i2cbus::begin();
      // Latches first (OLATA / OLATB), then IODIRA / IODIRB = outputs: no glitch when the pins switch from input.
      ok = i2cbus::writeReg(b.address, 0x14, (uint8_t)p) && i2cbus::writeReg(b.address, 0x15, (uint8_t)(p >> 8)) &&
           i2cbus::writeReg(b.address, 0x00, 0x00) && i2cbus::writeReg(b.address, 0x01, 0x00) && readsZero(b.address, 0x00);
    } else if (b.driver == RELAY_XL9535) {
      i2cbus::begin();
      // Output values first, then make port 0 / port 1 outputs: no glitch when they switch from input.
      ok = i2cbus::writeReg(b.address, 0x02, (uint8_t)p) && i2cbus::writeReg(b.address, 0x03, (uint8_t)(p >> 8)) &&
           i2cbus::writeReg(b.address, 0x06, 0x00) && i2cbus::writeReg(b.address, 0x07, 0x00) && readsZero(b.address, 0x06);
    } else {
      i2cbus::begin();
      ok = writeBoard(i, p);
    }
    boardOk[i] = ok;
    all &= ok;
    if (b.driver == RELAY_GPIO) Serial.printf("[relays] GPIO, %u relay(s)\n", b.relays);
    else
      Serial.printf("[relays] %s at 0x%02X, %u relay(s): %s\n", driverName(b.driver),
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
      snprintf(buf, sizeof(buf), "%s%s @0x%02X (%u) %s", i ? ", " : "", driverName(b.driver),
               b.address, b.relays, boardOk[i] ? "OK" : "NOT ANSWERING");
    }
    s += buf;
  }
  return s.length() ? s : String("none");
}

}  // namespace relays
