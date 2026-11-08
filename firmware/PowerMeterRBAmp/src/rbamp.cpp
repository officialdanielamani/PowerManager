#include "rbamp.h"

#include "../config.h"
#include "hwmap.h"
#include "i2cbus.h"

namespace rbamp {

// Registers (BASIC map)
static const uint8_t REG_STATUS = 0x00, REG_COMMAND = 0x01, REG_ERROR = 0x02, REG_CT_MODEL = 0x05, REG_PERIOD_VALID = 0x07;
static const uint8_t REG_AC_FREQ = 0x20, REG_SENSOR_CLASS = 0x25, REG_I2C_ADDRESS = 0x30, REG_ADDR_MAGIC = 0x31;
static const uint8_t REG_CH_SELECT = 0x32, REG_CH_DATA = 0x3C;
static const uint8_t REG_UPTIME = 0x46, REG_APPLIED_CT0 = 0x51, REG_PRODUCT_ID = 0x54, REG_HW_VARIANT = 0x55;
static const uint8_t REG_CAPABILITY = 0x57, REG_RT = 0x86, REG_RT_STATUS = 0xCE;
static const uint8_t REG_AVG_P0 = 0xDC, REG_AVG_P1 = 0xC2, REG_AVG_P2 = 0xC6;
// Commands (written to REG_COMMAND)
static const uint8_t CMD_RESET = 0x01, CMD_LATCH_PERIOD = 0x27, CMD_SET_CT_MODEL_CH0 = 0x28, CMD_COMMIT_ADDR = 0x30,
                     CMD_SAVE_USER_CONFIG = 0x32;
// Channel window fields
static const uint8_t F_I_RMS = 0, F_P_REAL = 2, F_PF = 3, F_PERIOD_AVG_P = 13, F_CT_MODEL = 15;

struct Board {
  uint8_t addr = 0;
  bool found = false;
  uint8_t inputs = 0;        // current inputs of this variant
  bool volt = false;
  bool selfRating = false;   // CAPABILITY bit 12 (fw 1.4.20+): keeps the applied CT model over a restart
  uint8_t cls = 0;           // SENSOR_CLASS
  char vname[6] = "?";
  uint8_t want[7] = {0};     // clamp model per input (from CHANNELS; 0 = leave)
  uint8_t applied[7] = {0};  // read back from the module
  bool primed = false;       // first period after (re)start is a primer: discard
  uint32_t lastUptime = 0;
};

static Board boards[hw::BOARDS];
static bool ready = false;   // every board found and the CHANNELS table fits
static uint8_t err = SENS_NO_REPLY;
static bool mismatch = false;
static char variants[24] = "?";

static float f32(const uint8_t *p) {
  float f;
  memcpy(&f, p, 4);  // little-endian on both sides
  return isfinite(f) ? f : 0.0f;
}

static bool readFloat(uint8_t addr, uint8_t reg, float &out) {
  uint8_t b[4];
  if (!i2cbus::readRegs(addr, reg, b, 4)) return false;
  out = f32(b);
  return true;
}

// Channel access window: select (field << 4 | input), then read the 4-byte snapshot.
static bool windowRead(uint8_t addr, uint8_t field, uint8_t in, float &out) {
  i2cbus::Lock l;
  if (!i2cbus::writeReg(addr, REG_CH_SELECT, (uint8_t)((field << 4) | in))) return false;
  return readFloat(addr, REG_CH_DATA, out);
}

static float modelAmps(uint8_t code) {
  switch (code) {  // SCT-013 family codes (SENSOR_CLASS 1)
    case 1: return 5;
    case 2: return 10;
    case 3: return 30;
    case 4: return 50;
    case 5: return 100;
    case 6: return 20;
    case 7: return 60;
    default: return 0;
  }
}

static String clampName(uint8_t cls, uint8_t code) {
  if (cls == 2 || cls == 3) return String("factory-paired clamp (code ") + code + ")";
  if (!code) return "not set (code 0)";
  return String("SCT-013 ") + (int)modelAmps(code) + " A (code " + code + ")";
}

static uint8_t readApplied(Board &b, uint8_t in) {
  uint8_t m = 0;
  if (in < 3) {
    i2cbus::readRegs(b.addr, REG_APPLIED_CT0 + in, &m, 1);
  } else {
    i2cbus::Lock l;
    i2cbus::writeReg(b.addr, REG_CH_SELECT, (uint8_t)((F_CT_MODEL << 4) | in));
    i2cbus::readRegs(b.addr, REG_CH_DATA, &m, 1);
  }
  return m;
}

// Without CAPABILITY bit 12 the saved model CODE survives a restart (and reads back) but the SCALING
// falls back to the factory default at every power-up, so the model is committed again every time
// (rbAmp docs, init. §6.2).
static void setModels(Board &b, uint8_t bi) {
  uint8_t cls = 0;
  if (!i2cbus::readRegs(b.addr, REG_SENSOR_CLASS, &cls, 1)) return;
  if (cls == 2 || cls == 3) {  // CT-series / built-in sensor: factory set, never write
    Serial.printf("[rbamp] B%u: sensor class %u, factory-paired clamps, CT models not changed\n", bi + 1, cls);
    return;
  }
  bool any = false;
  for (uint8_t i = 0; i < b.inputs; i++) any |= b.want[i] != 0;
  if (!any) return;
  bool changed = false;
  if (cls != 1) {
    i2cbus::writeReg(b.addr, REG_SENSOR_CLASS, 1);  // SCT-013 (resets the models to 0)
    delay(5);
    changed = true;
  }
  for (uint8_t in = 0; in < b.inputs; in++) {
    uint8_t want = b.want[in];
    if (!want) continue;
    uint8_t applied = readApplied(b, in);
    if (applied == want && b.selfRating) continue;
    if (in < 3) {
      i2cbus::writeReg(b.addr, REG_CT_MODEL, want);                     // stage
      i2cbus::writeReg(b.addr, REG_COMMAND, CMD_SET_CT_MODEL_CH0 + in);  // bind (applies the scaling)
      delay(6);
      uint8_t e = 0;
      if (i2cbus::readRegs(b.addr, REG_ERROR, &e, 1) && e)
        Serial.printf("[rbamp] B%u input %u: CT model %u rejected (error 0x%02X)\n", bi + 1, in + 1, want, e);
    } else {
      i2cbus::Lock l;
      i2cbus::writeReg(b.addr, REG_CH_SELECT, (uint8_t)((F_CT_MODEL << 4) | in));
      i2cbus::writeReg(b.addr, REG_CH_DATA, want);  // RAM, applies at once
      delay(2);
    }
    if (applied == want) continue;  // only re-applied (fw < 1.4.20): already saved, no flash write
    changed = true;
    Serial.printf("[rbamp] B%u input %u: CT model %u (%g A)\n", bi + 1, in + 1, want, modelAmps(want));
  }
  if (changed) {
    i2cbus::writeReg(b.addr, REG_COMMAND, CMD_SAVE_USER_CONFIG);  // persist to the module's flash
    delay(750);
  }
}

static bool probeBoard(Board &b, uint8_t bi) {
  uint8_t pid = 0;
  uint32_t t0 = millis();
  while (!(i2cbus::readRegs(b.addr, REG_PRODUCT_ID, &pid, 1) && pid == 0x01)) {
    if (millis() - t0 > 400) return false;
    delay(20);
  }
  uint8_t var = 0, cap[2] = {0, 0};
  i2cbus::readRegs(b.addr, REG_HW_VARIANT, &var, 1);
  i2cbus::readRegs(b.addr, REG_CAPABILITY, cap, 2);
  static const struct { uint8_t n; bool u; const char *name; } V[] = {
      {0, false, "?"}, {1, true, "UI1"}, {2, true, "UI2"}, {3, true, "UI3"}, {1, false, "I1"},
      {2, false, "I2"}, {3, false, "I3"}, {5, true, "UI5"}, {7, true, "UI7"}};
  if (var < 1 || var > 8) {
    Serial.printf("[rbamp] B%u @0x%02X: unknown variant 0x%02X\n", bi + 1, b.addr, var);
    return false;
  }
  b.inputs = V[var].n;
  b.volt = cap[1] & 0x01;        // CAPABILITY bit 8 = voltage input
  b.selfRating = cap[1] & 0x10;  // CAPABILITY bit 12 = keeps the applied CT model over a restart
  strncpy(b.vname, V[var].name, sizeof(b.vname) - 1);
  Serial.printf("[rbamp] B%u @0x%02X: %s, %u input(s), voltage input %s, capability 0x%02X%02X%s\n", bi + 1, b.addr,
                b.vname, b.inputs, b.volt ? "yes" : "no", cap[1], cap[0],
                b.selfRating ? "" : " (fw < 1.4.20: CT model re-applied at every start)");
  setModels(b, bi);
  i2cbus::readRegs(b.addr, REG_SENSOR_CLASS, &b.cls, 1);
  for (uint8_t in = 0; in < b.inputs; in++) b.applied[in] = readApplied(b, in);
  b.primed = false;
  b.lastUptime = 0;
  return true;
}

bool begin() {
  i2cbus::begin();
  static bool init = false;
  if (!init) {
    init = true;
    for (uint8_t i = 0; i < hw::BOARDS; i++) boards[i].addr = hw::BOARD_ADDR[i];
    for (uint8_t c = 0; c < hw::NCH; c++) boards[hw::CHANNEL[c].board - 1].want[hw::CHANNEL[c].input - 1] = hw::CHANNEL[c].clamp;
  }
  bool all = true;
  for (uint8_t i = 0; i < hw::BOARDS; i++) {
    if (!boards[i].found) boards[i].found = probeBoard(boards[i], i);
    all &= boards[i].found;
  }
  variants[0] = 0;
  for (uint8_t i = 0; i < hw::BOARDS; i++) {
    if (i) strlcat(variants, "+", sizeof(variants));
    strlcat(variants, boards[i].found ? boards[i].vname : "?", sizeof(variants));
  }
  if (!all) {
    ready = false;
    err = SENS_NO_REPLY;
    return false;
  }
  // The CHANNELS table must fit the boards (an input the variant does not have = wiring / config error).
  bool fits = true;
  mismatch = false;
  for (uint8_t c = 0; c < hw::NCH; c++) {
    const hw::ChannelMap &m = hw::CHANNEL[c];
    Board &b = boards[m.board - 1];
    if (m.input > b.inputs) {
      Serial.printf("[rbamp] CH%u: board %u is an %s with %u input(s): it has no input %u (fix CHANNELS in config.h)\n",
                    c + 1, m.board, b.vname, b.inputs, m.input);
      fits = false;
      continue;
    }
    uint8_t applied = b.applied[m.input - 1];
    bool bad = b.cls == 1 && m.clamp && applied != m.clamp;
    mismatch |= bad;
    Serial.printf("[rbamp] CH%u = B%u input %u, relay %u: clamp in use %s%s\n", c + 1, m.board, m.input, m.relay,
                  clampName(b.cls, applied).c_str(),
                  bad ? "  <-- WARNING: config.h asks for another model, the module did not take it" : "");
  }
  if (!fits) {
    ready = false;
    err = SENS_BAD;
    return false;
  }
  ready = true;
  Serial.printf("[rbamp] ready: %u board(s) (%s), %u channel(s), voltage %s\n", hw::BOARDS, variants, hw::NCH,
                hasVoltage() ? "yes" : "no");
  periodStart();  // primer period
  return true;
}

String clampSummary() {
  String s;
  for (uint8_t c = 0; c < hw::NCH; c++) {
    const hw::ChannelMap &m = hw::CHANNEL[c];
    const Board &b = boards[m.board - 1];
    if (c) s += ", ";
    s += String("CH") + (c + 1) + " ";
    if (hw::BOARDS > 1) s += String("(B") + m.board + " in " + m.input + ") ";
    s += b.found && m.input <= b.inputs ? clampName(b.cls, b.applied[m.input - 1]) : String("board not found");
  }
  if (mismatch) s += "  (WARNING: not what config.h asks for)";
  return s;
}

String boardSummary() {
  String s;
  for (uint8_t i = 0; i < hw::BOARDS; i++) {
    char buf[64];
    snprintf(buf, sizeof(buf), "%sB%u %s @0x%02X %s", i ? ", " : "", i + 1, boards[i].found ? boards[i].vname : "?",
             boards[i].addr, boards[i].found ? "OK" : "NOT FOUND");
    s += buf;
  }
  return s;
}

bool present() { return ready; }
uint8_t channels() { return hw::NCH; }
bool hasVoltage() {
  for (uint8_t i = 0; i < hw::BOARDS; i++)
    if (boards[i].found && boards[i].volt) return true;
  return false;
}
const char *variantName() { return variants; }
float rating(uint8_t ch) {
  if (ch >= hw::NCH) return 0;
  const hw::ChannelMap &m = hw::CHANNEL[ch];
  const Board &b = boards[m.board - 1];
  uint8_t code = b.found && m.input <= b.inputs ? b.applied[m.input - 1] : 0;
  return modelAmps(code ? code : m.clamp);
}
uint8_t lastError() { return err; }

bool read(Sample &out) {
  if (!ready) {
    err = SENS_NO_REPLY;
    return false;
  }
  static uint8_t rt[hw::BOARDS][56];  // 0x86..0xBD of each board in one burst = one coherent window
  int8_t vBoard = -1;
  for (uint8_t i = 0; i < hw::BOARDS; i++) {
    Board &b = boards[i];
    uint8_t st = 0, valid = 0, hz = 0;
    {
      i2cbus::Lock l;
      if (!i2cbus::readRegs(b.addr, REG_RT, rt[i], sizeof(rt[i]))) {
        err = SENS_NO_REPLY;
        return false;
      }
      i2cbus::readRegs(b.addr, REG_STATUS, &st, 1);
      i2cbus::readRegs(b.addr, REG_RT_STATUS, &valid, 1);
      if (vBoard < 0 && b.volt) i2cbus::readRegs(b.addr, REG_AC_FREQ, &hz, 1);
    }
    if ((st & 0x02) || !(valid & 0x01)) {  // module error / no valid result yet
      err = SENS_BAD;
      return false;
    }
    if (vBoard < 0 && b.volt) {
      vBoard = i;
      out.v = f32(rt[i] + 0);
      out.hz = hz;
    }
  }
  if (vBoard < 0) {
    out.v = 0;
    out.hz = 0;
  }
  for (uint8_t c = 0; c < hw::NCH; c++) {
    const hw::ChannelMap &m = hw::CHANNEL[c];
    uint8_t bi = m.board - 1, in = m.input - 1;
    if (in < 3) {
      out.a[c] = f32(rt[bi] + 8 + in * 4);    // 0x8E + 4 x input
      out.w[c] = f32(rt[bi] + 32 + in * 4);   // 0xA6 + 4 x input
      out.pf[c] = f32(rt[bi] + 44 + in * 4);  // 0xB2 + 4 x input
    } else {
      uint8_t addr = boards[bi].addr;
      if (!windowRead(addr, F_I_RMS, in, out.a[c]) || !windowRead(addr, F_P_REAL, in, out.w[c]) ||
          !windowRead(addr, F_PF, in, out.pf[c])) {
        err = SENS_NO_REPLY;
        return false;
      }
    }
  }
  err = SENS_OK;
  return true;
}

bool periodStart() {
  if (!ready) return false;
  bool ok = true;
  for (uint8_t i = 0; i < hw::BOARDS; i++) ok &= i2cbus::writeReg(boards[i].addr, REG_COMMAND, CMD_LATCH_PERIOD);
  return ok;
}

bool periodFinish(float avgW[MC_MAX_CH], bool &valid) {
  valid = false;
  if (!ready) return false;
  bool allValid = true;
  for (uint8_t i = 0; i < hw::BOARDS; i++) {
    Board &b = boards[i];
    uint8_t pv = 0, up[4] = {0};
    if (!i2cbus::readRegs(b.addr, REG_PERIOD_VALID, &pv, 1) || !i2cbus::readRegs(b.addr, REG_UPTIME, up, 4)) return false;
    uint32_t uptime = (uint32_t)up[0] | (uint32_t)up[1] << 8 | (uint32_t)up[2] << 16 | (uint32_t)up[3] << 24;
    bool restarted = b.primed && uptime < b.lastUptime;  // the module restarted inside the period
    if (restarted) {
      Serial.printf("[rbamp] B%u restarted: period discarded, CT models re-applied\n", i + 1);
      setModels(b, i);
    }
    b.lastUptime = uptime;
    bool wasPrimed = b.primed;
    b.primed = true;
    if (!(pv & 0x01) || restarted || !wasPrimed) allValid = false;  // primer / stale: discard
  }
  if (!allValid) return true;
  for (uint8_t c = 0; c < hw::NCH; c++) {
    const hw::ChannelMap &m = hw::CHANNEL[c];
    uint8_t addr = boards[m.board - 1].addr, in = m.input - 1;
    float p = 0;
    bool ok = in == 0 ? readFloat(addr, REG_AVG_P0, p) : in == 1 ? readFloat(addr, REG_AVG_P1, p)
            : in == 2 ? readFloat(addr, REG_AVG_P2, p) : windowRead(addr, F_PERIOD_AVG_P, in, p);
    if (!ok) return false;
    avgW[c] = p > 0 ? p : 0;
  }
  valid = true;
  return true;
}

bool changeAddress(uint8_t from, uint8_t to, String &msg) {
  i2cbus::begin();
  if (to < 0x08 || to > 0x77) {
    msg = "new address must be 0x08..0x77";
    return false;
  }
  i2cbus::Lock l;  // the whole sequence: nothing else uses the bus meanwhile
  uint8_t pid = 0;
  if (!(i2cbus::readRegs(from, REG_PRODUCT_ID, &pid, 1) && pid == 0x01)) {
    msg = "no rbAmp answers at the old address";
    return false;
  }
  if (from == to) {
    msg = "already at that address";
    return true;
  }
  if (i2cbus::probe(to)) {
    msg = "something already answers at the new address";
    return false;
  }
  bool ok = i2cbus::writeReg(from, REG_I2C_ADDRESS, to) && i2cbus::writeReg(from, REG_ADDR_MAGIC, 0xA5) &&
            i2cbus::writeReg(from, REG_COMMAND, CMD_COMMIT_ADDR);
  if (!ok) {
    msg = "the module did not accept the address change";
    return false;
  }
  delay(750);                                      // flash write
  i2cbus::writeReg(from, REG_COMMAND, CMD_RESET);  // the new address applies after a reset
  uint32_t t0 = millis();
  delay(300);
  while (millis() - t0 < 2000) {
    if (i2cbus::readRegs(to, REG_PRODUCT_ID, &pid, 1) && pid == 0x01) {
      for (uint8_t i = 0; i < hw::BOARDS; i++)  // a configured board may have moved: probe it again
        if (boards[i].addr == from || boards[i].addr == to) boards[i].found = false;
      ready = false;
      msg = "done: the module now answers at the new address";
      return true;
    }
    delay(50);
  }
  msg = "no answer at the new address after the reset (power-cycle the module and run 'i2c scan')";
  return false;
}

}  // namespace rbamp
