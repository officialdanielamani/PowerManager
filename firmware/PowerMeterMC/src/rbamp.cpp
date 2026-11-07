#include "rbamp.h"

#include "../config.h"
#include "i2cbus.h"

namespace rbamp {

static const uint8_t ADDR = RBAMP_ADDRESS;
// Registers (BASIC map)
static const uint8_t REG_STATUS = 0x00, REG_COMMAND = 0x01, REG_CT_MODEL = 0x05, REG_PERIOD_VALID = 0x07;
static const uint8_t REG_AC_FREQ = 0x20, REG_SENSOR_CLASS = 0x25, REG_CH_SELECT = 0x32, REG_CH_DATA = 0x3C;
static const uint8_t REG_UPTIME = 0x46, REG_APPLIED_CT0 = 0x51, REG_PRODUCT_ID = 0x54, REG_HW_VARIANT = 0x55;
static const uint8_t REG_CAPABILITY = 0x57, REG_RT = 0x86, REG_RT_STATUS = 0xCE;
static const uint8_t REG_AVG_P0 = 0xDC, REG_AVG_P1 = 0xC2, REG_AVG_P2 = 0xC6;
// Commands (written to REG_COMMAND)
static const uint8_t CMD_LATCH_PERIOD = 0x27, CMD_SET_CT_MODEL_CH0 = 0x28, CMD_SAVE_USER_CONFIG = 0x32;
// Channel window fields
static const uint8_t F_I_RMS = 0, F_P_REAL = 2, F_PF = 3, F_PERIOD_AVG_P = 13, F_CT_MODEL = 15;

static bool found = false;
static uint8_t nch = 0, err = SENS_NO_REPLY;
static bool volt = true;
static char vname[6] = "?";
static uint8_t models[MC_MAX_CH] = {0};
static bool primed = false;       // first period after (re)start is a primer: discard
static uint32_t lastUptime = 0, periodStartMs = 0;

static const uint8_t CFG_MODELS[MC_MAX_CH] = RBAMP_CT_MODELS;

static float f32(const uint8_t *p) {
  float f;
  memcpy(&f, p, 4);  // little-endian on both sides
  return isfinite(f) ? f : 0.0f;
}

static bool readFloat(uint8_t reg, float &out) {
  uint8_t b[4];
  if (!i2cbus::readRegs(ADDR, reg, b, 4)) return false;
  out = f32(b);
  return true;
}

// Channel access window: select (field << 4 | ch), then read the 4-byte snapshot.
static bool windowRead(uint8_t field, uint8_t ch, float &out) {
  i2cbus::Lock l;
  if (!i2cbus::writeReg(ADDR, REG_CH_SELECT, (uint8_t)((field << 4) | ch))) return false;
  return readFloat(REG_CH_DATA, out);
}

static bool mismatch = false;
// CAPABILITY bit 12 (fw 1.4.20+): the module keeps the applied model over a restart. Without it the
// saved model CODE survives (and reads back) but the SCALING falls back to the factory default at
// every power-up, so the model must be committed again every time (rbAmp docs, init. §6.2).
static bool selfRating = false;
static String clampName(uint8_t cls, uint8_t code);

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

static void setModels() {
  uint8_t cls = 0;
  if (!i2cbus::readRegs(ADDR, REG_SENSOR_CLASS, &cls, 1)) return;
  if (cls == 2 || cls == 3) {  // CT-series / built-in sensor: factory set, never write
    Serial.printf("[rbamp] sensor class %u: factory-paired clamps, CT models not changed\n", cls);
    return;
  }
  bool any = false;
  for (uint8_t i = 0; i < nch; i++) any |= CFG_MODELS[i] != 0;
  if (!any) return;
  bool changed = false;
  if (cls != 1) {
    i2cbus::writeReg(ADDR, REG_SENSOR_CLASS, 1);  // SCT-013 (resets the models to 0)
    delay(5);
    changed = true;
  }
  for (uint8_t ch = 0; ch < nch; ch++) {
    uint8_t want = CFG_MODELS[ch];
    if (!want) continue;
    uint8_t applied = 0;
    if (ch < 3) {
      i2cbus::readRegs(ADDR, REG_APPLIED_CT0 + ch, &applied, 1);
      if (applied == want && selfRating) continue;
      i2cbus::writeReg(ADDR, REG_CT_MODEL, want);              // stage
      i2cbus::writeReg(ADDR, REG_COMMAND, CMD_SET_CT_MODEL_CH0 + ch);  // bind to the channel (applies the scaling)
      delay(6);
      uint8_t err = 0;
      if (i2cbus::readRegs(ADDR, 0x02, &err, 1) && err)
        Serial.printf("[rbamp] channel %u: CT model %u rejected (error 0x%02X)\n", ch + 1, want, err);
      if (applied == want) continue;  // only re-applied (fw < 1.4.20): already saved, no flash write
    } else {
      i2cbus::Lock l;
      if (i2cbus::writeReg(ADDR, REG_CH_SELECT, (uint8_t)((F_CT_MODEL << 4) | ch)) &&
          i2cbus::readRegs(ADDR, REG_CH_DATA, &applied, 1) && applied == want && selfRating)
        continue;
      i2cbus::writeReg(ADDR, REG_CH_SELECT, (uint8_t)((F_CT_MODEL << 4) | ch));
      i2cbus::writeReg(ADDR, REG_CH_DATA, want);
      delay(2);
    }
    changed = true;
    Serial.printf("[rbamp] channel %u: CT model %u (%g A)\n", ch + 1, want, modelAmps(want));
  }
  if (changed) {
    i2cbus::writeReg(ADDR, REG_COMMAND, CMD_SAVE_USER_CONFIG);  // persist to the module's flash
    delay(750);
  }
}

bool begin() {
  i2cbus::begin();
  uint8_t pid = 0;
  uint32_t t0 = millis();
  while (!(i2cbus::readRegs(ADDR, REG_PRODUCT_ID, &pid, 1) && pid == 0x01)) {
    if (millis() - t0 > 400) {
      found = false;
      err = SENS_NO_REPLY;
      return false;
    }
    delay(20);
  }
  uint8_t var = 0, cap[2] = {0, 0};
  i2cbus::readRegs(ADDR, REG_HW_VARIANT, &var, 1);
  i2cbus::readRegs(ADDR, REG_CAPABILITY, cap, 2);
  static const struct { uint8_t n; bool u; const char *name; } V[] = {
      {0, false, "?"}, {1, true, "UI1"}, {2, true, "UI2"}, {3, true, "UI3"}, {1, false, "I1"},
      {2, false, "I2"}, {3, false, "I3"}, {5, true, "UI5"}, {7, true, "UI7"}};
  if (var < 1 || var > 8) {
    Serial.printf("[rbamp] unknown variant 0x%02X\n", var);
    found = false;
    err = SENS_BAD;
    return false;
  }
  nch = min<uint8_t>(V[var].n, MC_MAX_CH);
  volt = cap[1] & 0x01;        // CAPABILITY bit 8 = voltage input
  selfRating = cap[1] & 0x10;  // CAPABILITY bit 12 = keeps the applied CT model over a restart
  Serial.printf("[rbamp] capability 0x%02X%02X%s\n", cap[1], cap[0],
                selfRating ? "" : " (fw < 1.4.20: CT model re-applied at every start)");
  strncpy(vname, V[var].name, sizeof(vname) - 1);
  setModels();
  for (uint8_t ch = 0; ch < nch; ch++) {
    uint8_t m = 0;
    if (ch < 3) i2cbus::readRegs(ADDR, REG_APPLIED_CT0 + ch, &m, 1);
    else {
      i2cbus::Lock l;
      i2cbus::writeReg(ADDR, REG_CH_SELECT, (uint8_t)((F_CT_MODEL << 4) | ch));
      i2cbus::readRegs(ADDR, REG_CH_DATA, &m, 1);
    }
    models[ch] = m;
  }
  // Show which clamp model the module really uses (wrong scaling = wrong current / power).
  uint8_t cls = 0;
  i2cbus::readRegs(ADDR, REG_SENSOR_CLASS, &cls, 1);
  mismatch = false;
  for (uint8_t ch = 0; ch < nch; ch++) {
    bool bad = cls == 1 && CFG_MODELS[ch] && models[ch] != CFG_MODELS[ch];
    mismatch |= bad;
    Serial.printf("[rbamp] CH%u clamp in use: %s%s\n", ch + 1, clampName(cls, models[ch]).c_str(),
                  bad ? "  <-- WARNING: config.h asks for another model, the module did not take it" : "");
  }
  found = true;
  primed = false;
  lastUptime = 0;
  Serial.printf("[rbamp] found %s: %u channel(s), voltage input %s\n", vname, nch, volt ? "yes" : "no");
  periodStart();  // primer period
  return true;
}

static String clampName(uint8_t cls, uint8_t code) {
  if (cls == 2 || cls == 3) return String("factory-paired clamp (code ") + code + ")";
  if (!code) return "not set (code 0)";
  return String("SCT-013 ") + (int)modelAmps(code) + " A (code " + code + ")";
}

String clampSummary() {
  String s;
  uint8_t cls = 0;
  if (found) i2cbus::readRegs(ADDR, REG_SENSOR_CLASS, &cls, 1);
  for (uint8_t ch = 0; ch < nch; ch++) {
    if (ch) s += ", ";
    s += String("CH") + (ch + 1) + " " + clampName(cls, models[ch]);
  }
  if (mismatch) s += "  (WARNING: not what config.h asks for)";
  return s;
}

bool present() { return found; }
uint8_t channels() { return nch; }
bool hasVoltage() { return volt; }
const char *variantName() { return vname; }
float rating(uint8_t ch) { return ch < MC_MAX_CH ? modelAmps(models[ch]) : 0; }
uint8_t lastError() { return err; }

bool read(Sample &out) {
  if (!found) {
    err = SENS_NO_REPLY;
    return false;
  }
  uint8_t rt[56];  // 0x86..0xBD in one burst = one coherent window
  uint8_t st = 0, valid = 0, hz = 0;
  {
    i2cbus::Lock l;
    if (!i2cbus::readRegs(ADDR, REG_RT, rt, sizeof(rt))) {
      err = SENS_NO_REPLY;
      return false;
    }
    i2cbus::readRegs(ADDR, REG_STATUS, &st, 1);
    i2cbus::readRegs(ADDR, REG_RT_STATUS, &valid, 1);
    i2cbus::readRegs(ADDR, REG_AC_FREQ, &hz, 1);
  }
  if ((st & 0x02) || !(valid & 0x01)) {  // module error / no valid result yet
    err = SENS_BAD;
    return false;
  }
  out.v = volt ? f32(rt + 0) : 0;
  out.hz = hz;
  for (uint8_t ch = 0; ch < nch; ch++) {
    if (ch < 3) {
      out.a[ch] = f32(rt + 8 + ch * 4);    // 0x8E + 4ch
      out.w[ch] = f32(rt + 32 + ch * 4);   // 0xA6 + 4ch
      out.pf[ch] = f32(rt + 44 + ch * 4);  // 0xB2 + 4ch
    } else if (!windowRead(F_I_RMS, ch, out.a[ch]) || !windowRead(F_P_REAL, ch, out.w[ch]) ||
               !windowRead(F_PF, ch, out.pf[ch])) {
      err = SENS_NO_REPLY;
      return false;
    }
  }
  err = SENS_OK;
  return true;
}

bool periodStart() {
  if (!found) return false;
  periodStartMs = millis();
  return i2cbus::writeReg(ADDR, REG_COMMAND, CMD_LATCH_PERIOD);
}

bool periodFinish(float avgW[MC_MAX_CH], bool &valid) {
  valid = false;
  if (!found) return false;
  uint8_t pv = 0, up[4] = {0};
  if (!i2cbus::readRegs(ADDR, REG_PERIOD_VALID, &pv, 1) || !i2cbus::readRegs(ADDR, REG_UPTIME, up, 4)) return false;
  uint32_t uptime = (uint32_t)up[0] | (uint32_t)up[1] << 8 | (uint32_t)up[2] << 16 | (uint32_t)up[3] << 24;
  bool restarted = primed && uptime < lastUptime;  // the module restarted inside the period
  if (restarted) {
    Serial.println("[rbamp] module restarted: period discarded, CT models re-applied");
    setModels();
  }
  lastUptime = uptime;
  bool wasPrimed = primed;
  primed = true;
  if (!(pv & 0x01) || restarted || !wasPrimed) return true;  // primer / stale: discard
  for (uint8_t ch = 0; ch < nch; ch++) {
    float p = 0;
    bool ok = ch == 0 ? readFloat(REG_AVG_P0, p) : ch == 1 ? readFloat(REG_AVG_P1, p)
            : ch == 2 ? readFloat(REG_AVG_P2, p) : windowRead(F_PERIOD_AVG_P, ch, p);
    if (!ok) return false;
    avgW[ch] = p > 0 ? p : 0;
  }
  valid = true;
  return true;
}

}  // namespace rbamp
