#include "relays.h"

#include "../config.h"
#include "i2cbus.h"

namespace relays {

static bool lastOk = false;

static uint8_t pattern(uint8_t onMask) {
  uint8_t used = RELAY_COUNT >= 8 ? 0xFF : (uint8_t)((1u << RELAY_COUNT) - 1);
  onMask &= used;
  // Active-high: a set bit switches the relay on. Active-low: inverted (unused outputs stay "off").
  return RELAY_ACTIVE_HIGH ? onMask : (uint8_t)~onMask;
}

bool write(uint8_t onMask) {
  uint8_t p = pattern(onMask);
  bool ok;
#if RELAY_DRIVER == RELAY_XL9535
  ok = i2cbus::writeReg(RELAY_ADDRESS, 0x02, p);  // output port 0 = relays 1..8
#else
  ok = i2cbus::writeRaw(RELAY_ADDRESS, &p, 1);    // PCF8574: the byte is the output state
#endif
  if (!ok) {  // one retry: a busy bus is the usual cause
    delay(2);
#if RELAY_DRIVER == RELAY_XL9535
    ok = i2cbus::writeReg(RELAY_ADDRESS, 0x02, p);
#else
    ok = i2cbus::writeRaw(RELAY_ADDRESS, &p, 1);
#endif
  }
  lastOk = ok;
  return ok;
}

bool begin() {
  i2cbus::begin();
#if RELAY_DRIVER == RELAY_XL9535
  // Output value first, then make port 0 / port 1 outputs: no glitch when they switch from input.
  bool ok = i2cbus::writeReg(RELAY_ADDRESS, 0x02, pattern(0)) && i2cbus::writeReg(RELAY_ADDRESS, 0x03, RELAY_ACTIVE_HIGH ? 0x00 : 0xFF) &&
            i2cbus::writeReg(RELAY_ADDRESS, 0x06, 0x00) && i2cbus::writeReg(RELAY_ADDRESS, 0x07, 0x00);
  lastOk = ok;
#else
  bool ok = write(0);
#endif
  Serial.printf("[relays] %s at 0x%02X, %d relay(s): %s\n", boardName(), RELAY_ADDRESS, RELAY_COUNT,
                ok ? "OK" : "NOT ANSWERING (check wiring / address)");
  return ok;
}

bool ok() { return lastOk; }
const char *boardName() { return RELAY_DRIVER == RELAY_XL9535 ? "XL9535" : "PCF8574"; }

}  // namespace relays
