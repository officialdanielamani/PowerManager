// Host test of src/i2cbus.cpp (the real file) against a fake Wire + GPIO model: a stuck bus is freed, a missing device is
// not mistaken for a bus fault.
#include <cstdio>

#include "Arduino.h"
#include "Wire.h"
#include "config.h"
#include "i2cbus.h"

uint32_t g_ms = 100000;
SerialT Serial;
WireT Wire;

static int failures = 0;
#define CHECK(c) do { if (!(c)) { printf("FAIL line %d: %s\n", __LINE__, #c); failures++; } } while (0)

// ---- fake GPIO: SDA is held low by a device until it has seen `sdaStuckPulses` clock pulses on SCL
static int sdaStuckPulses = 0, sclPulses = 0, wireEnds = 0, wireBegins = 0;
static int mode[64], level[64];
void pinMode(int pin, int m) {
  if (pin == PIN_I2C_SCL && m == INPUT_PULLUP && mode[pin] == OUTPUT && level[pin] == LOW) {  // SCL released after a low: one clock pulse
    sclPulses++;
    if (sdaStuckPulses > 0) sdaStuckPulses--;
  }
  mode[pin] = m;
}
void digitalWrite(int pin, int l) { level[pin] = l; }
int digitalRead(int pin) { return pin == PIN_I2C_SDA && sdaStuckPulses > 0 ? LOW : HIGH; }

// ---- fake Wire: absent addresses NACK (2); a stuck bus times out (5); `dataShort` = address ACKed but no data comes
static bool present[128];
static bool dataShort = false;
static uint8_t cur = 0;
void WireT::begin(int, int, uint32_t) { wireBegins++; }
void WireT::end() { wireEnds++; }
void WireT::beginTransmission(uint8_t a) { cur = a; }
uint8_t WireT::endTransmission(bool) { return sdaStuckPulses > 0 ? 5 : present[cur] ? 0 : 2; }
size_t WireT::requestFrom(uint16_t, uint8_t n) { return dataShort ? 0 : n; }

static void reset(int stuck) {
  sdaStuckPulses = stuck; sclPulses = 0; wireEnds = 0;
  dataShort = false;
  g_ms += 10000;           // clear the rate limit of an earlier scenario
}

int main() {
  present[0x50] = true;
  i2cbus::begin();
  uint8_t b[4];

  // 1. devices that are not there NACK: that is not a bus fault
  for (int i = 0; i < 100; i++) { i2cbus::probe(0x21); i2cbus::readRegs(0x33, 0, b, 1); i2cbus::writeReg(0x44, 0, 0); }
  CHECK(i2cbus::recoveries() == 0 && wireEnds == 0);
  CHECK(i2cbus::probe(0x50) && i2cbus::readRegs(0x50, 0, b, 4));
  printf("missing devices are not a bus fault ok\n");

  // 2. SDA held low (a device browned out mid-byte): the third failed transaction frees the bus
  reset(5);
  CHECK(!i2cbus::probe(0x50) && !i2cbus::probe(0x50));
  CHECK(i2cbus::recoveries() == 0);
  CHECK(!i2cbus::probe(0x50));
  CHECK(i2cbus::recoveries() == 1 && wireEnds == 1 && sclPulses == 5 && sdaStuckPulses == 0);
  CHECK(i2cbus::probe(0x50) && i2cbus::readRegs(0x50, 0, b, 4));   // works again
  printf("stuck bus freed after 3 timeouts ok\n");

  // 3. a success in between resets the count
  reset(1000);
  i2cbus::probe(0x50); i2cbus::probe(0x50);
  sdaStuckPulses = 0; CHECK(i2cbus::probe(0x50));   // bus is fine again by itself
  sdaStuckPulses = 1000; i2cbus::probe(0x50); i2cbus::probe(0x50);
  CHECK(wireEnds == 0);
  sdaStuckPulses = 0;
  printf("successes reset the fault count ok\n");

  // 4. a bus that stays broken is not hammered: at most one recovery every 2 s
  reset(100000);
  uint32_t before = i2cbus::recoveries();
  for (int i = 0; i < 100; i++) { g_ms += 100; i2cbus::probe(0x50); }   // 10 s of failures
  int tries = (int)(i2cbus::recoveries() - before);
  CHECK(tries >= 3 && tries <= 5);
  sdaStuckPulses = 0;
  printf("rate limit ok (%d recoveries in 10 s)\n", tries);

  // 5. SDA stuck low while nothing is being sent: tick() notices (once a second)
  reset(3);
  before = i2cbus::recoveries();
  i2cbus::tick();
  CHECK(i2cbus::recoveries() == before + 1 && sdaStuckPulses == 0);
  i2cbus::tick();
  g_ms += 1000; i2cbus::tick();
  CHECK(i2cbus::recoveries() == before + 1);   // free now: nothing more to do
  printf("idle check ok\n");

  // 6. the address is acknowledged but the data never comes: counts as a bus fault too
  reset(0);
  dataShort = true;
  before = i2cbus::recoveries();
  for (int i = 0; i < 3; i++) i2cbus::readRegs(0x50, 0, b, 4);
  CHECK(i2cbus::recoveries() == before + 1);
  dataShort = false;
  CHECK(i2cbus::readRegs(0x50, 0, b, 4));
  printf("data timeout ok\n");

  if (failures) { printf("%d check(s) FAILED\n", failures); return 1; }
  printf("all I2C bus recovery checks passed\n");
  return 0;
}
