// Host test of src/relays.cpp against fake I2C expanders: a board that is unplugged, loses power or is plugged in after the
// start must work again by itself (the pin directions are sent with every write).
#include <cstdio>
#include <map>

#include "Arduino.h"
#include "i2cbus.h"
#include "relays.h"
#include "relaymap.h"

uint32_t g_ms = 0;
SerialT Serial;
void pinMode(int, int) {}
void digitalWrite(int, int) {}
int digitalRead(int) { return 0; }

static int failures = 0;
#define CHECK(c) do { if (!(c)) { printf("FAIL line %d: %s\n", __LINE__, #c); failures++; } } while (0)

// ---- fake devices
struct Dev {
  int kind = 0;               // RELAY_XL9535 / RELAY_MCP23017 / RELAY_PCF8574
  bool present = true;        // plugged in and powered
  uint8_t reg[0x20] = {0};
  void reset() {              // power-up state: every pin is an input, outputs off
    memset(reg, 0, sizeof(reg));
    if (kind == RELAY_XL9535) { reg[0x02] = reg[0x03] = 0xFF; reg[0x06] = reg[0x07] = 0xFF; }
    if (kind == RELAY_MCP23017) { reg[0x00] = reg[0x01] = 0xFF; }
    if (kind == RELAY_PCF8574) reg[0] = 0xFF;
  }
  // 16 bit mask of the pins that are driven high / low by the chip (direction = output), PCF8574: the byte itself
  uint16_t outputs() const {
    if (kind == RELAY_XL9535) return (uint16_t)(~(reg[0x06] | reg[0x07] << 8)) & (reg[0x02] | reg[0x03] << 8);
    if (kind == RELAY_MCP23017) return (uint16_t)(~(reg[0x00] | reg[0x01] << 8)) & (reg[0x14] | reg[0x15] << 8);
    return reg[0];
  }
  uint16_t driven() const {   // pins the chip drives at all
    if (kind == RELAY_XL9535) return (uint16_t)~(reg[0x06] | reg[0x07] << 8);
    if (kind == RELAY_MCP23017) return (uint16_t)~(reg[0x00] | reg[0x01] << 8);
    return 0xFFFF;
  }
};
static std::map<uint8_t, Dev> bus;
static int regWrites = 0;

namespace i2cbus {
void begin() {}
bool writeReg(uint8_t addr, uint8_t reg, uint8_t val) {
  auto it = bus.find(addr);
  if (it == bus.end() || !it->second.present) return false;
  it->second.reg[reg & 0x1F] = val;
  regWrites++;
  return true;
}
bool writeRaw(uint8_t addr, const uint8_t *d, size_t) {
  auto it = bus.find(addr);
  if (it == bus.end() || !it->second.present) return false;
  it->second.reg[0] = d[0];
  return true;
}
}  // namespace i2cbus

static void add(uint8_t addr, int kind) { bus[addr].kind = kind; bus[addr].present = true; bus[addr].reset(); }

int main() {
  // Board A: XL9535 0x20, 2 relays (active high); Board B: MCP23017 0x21, 8 relays (active high);
  // Board C: PCF8574 0x22, 4 relays (active low: a set bit = relay OFF)
  hw::RELAY_BOARD[0] = {RELAY_XL9535, 0x20, 2, 1};
  hw::RELAY_BOARD[1] = {RELAY_MCP23017, 0x21, 8, 1};
  hw::RELAY_BOARD[2] = {RELAY_PCF8574, 0x22, 4, 0};
  add(0x20, RELAY_XL9535); add(0x21, RELAY_MCP23017); add(0x22, RELAY_PCF8574);

  // relays 1..2 = A, 3..10 = B, 11..14 = C. ON: relay 2 (A), relay 3 and 10 (B), relay 11 (C)
  uint32_t on = (1u << 1) | (1u << 2) | (1u << 9) | (1u << 10);
  CHECK(relays::begin());
  CHECK(relays::write(on));
  CHECK(bus[0x20].outputs() == 0x0002 && bus[0x20].driven() == 0xFFFF);
  CHECK(bus[0x21].outputs() == ((1 << 0) | (1 << 7)) && bus[0x21].driven() == 0xFFFF);
  CHECK((bus[0x22].reg[0] & 0x0F) == 0x0E);  // active low: relay 11 ON = bit 0 low, the others high
  printf("normal switching ok\n");

  // 1. brief brownout: the boards lose power and come back between two writes (no write failed in between)
  bus[0x20].reset(); bus[0x21].reset(); bus[0x22].reset();
  CHECK(bus[0x20].driven() == 0 && bus[0x21].driven() == 0);  // all pins are inputs now: no relay can be switched
  CHECK(relays::write(on));                                    // the next periodic write brings everything back
  CHECK(bus[0x20].outputs() == 0x0002 && bus[0x21].outputs() == ((1 << 0) | (1 << 7)) && (bus[0x22].reg[0] & 0x0F) == 0x0E);
  printf("power loss between two writes ok\n");

  // 2. a board is unplugged: the write reports it, the other boards keep working; plugged in again it works at once
  bus[0x21].present = false;
  CHECK(!relays::write(on) && !relays::ok());
  CHECK(bus[0x20].outputs() == 0x0002);                        // Board A is still written
  CHECK(relays::summary().length() > 0);
  bus[0x21].present = true; bus[0x21].reset();                 // plugged in: comes up as inputs
  CHECK(relays::write(on) && relays::ok());
  CHECK(bus[0x21].driven() == 0xFFFF && bus[0x21].outputs() == ((1 << 0) | (1 << 7)));
  printf("unplug / plug in ok\n");

  // 3. the board is missing at start-up and plugged in later
  bus.clear();
  add(0x20, RELAY_XL9535); add(0x22, RELAY_PCF8574);
  CHECK(!relays::begin());
  add(0x21, RELAY_MCP23017);                                   // plugged in after the start
  CHECK(relays::write(on) && relays::ok());
  CHECK(bus[0x21].driven() == 0xFFFF && bus[0x21].outputs() == ((1 << 0) | (1 << 7)));
  printf("plugged in after start-up ok\n");

  // 4. relays that are switched OFF stay OFF when a board comes back (nothing is switched on by the re-init)
  bus[0x21].reset();
  CHECK(relays::write(0));
  CHECK(bus[0x20].outputs() == 0 && bus[0x21].outputs() == 0 && (bus[0x22].reg[0] & 0x0F) == 0x0F);
  printf("OFF stays OFF ok\n");

  if (failures) { printf("%d check(s) FAILED\n", failures); return 1; }
  printf("all relay recovery checks passed\n");
  return 0;
}
