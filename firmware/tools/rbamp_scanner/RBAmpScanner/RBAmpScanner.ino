// rbAmp debug tool for the ESP32-S3: scan the I2C bus, identify a module, read its sensor data, change its address.
// Open the Serial Monitor at 115200 baud, line ending "Newline", and type a command (type  help ).
// Not part of the meter firmware: it only helps to check wiring and modules. Not tested on hardware by the author.
// Register map: the same as firmware/PowerMeterRBAmp/src/rbamp.cpp.
#include <Wire.h>

// ---- wiring (change here, or at run time with  pins <sda> <scl> <drdy>)
int PIN_SDA = 7, PIN_SCL = 9, PIN_DRDY = 4;   // PIN_DRDY -1 = not wired
uint32_t I2C_HZ = 50000;                      // rbAmp: 50 kHz on ESP32
uint8_t cur = 0x50;                           // the module the commands use (change with  use 0x51 )

// ---- rbAmp registers
const uint8_t REG_STATUS = 0x00, REG_COMMAND = 0x01, REG_ERROR = 0x02, REG_VERSION = 0x03, REG_CT_MODEL = 0x05;
const uint8_t REG_PERIOD_VALID = 0x07, REG_AC_FREQ = 0x20, REG_SENSOR_CLASS = 0x25, REG_I2C_ADDRESS = 0x30, REG_ADDR_MAGIC = 0x31;
const uint8_t REG_CH_SELECT = 0x32, REG_CH_DATA = 0x3C, REG_UPTIME = 0x46, REG_APPLIED_CT0 = 0x51, REG_PRODUCT_ID = 0x54;
const uint8_t REG_HW_VARIANT = 0x55, REG_CAPABILITY = 0x57, REG_RT = 0x86, REG_RT_STATUS = 0xCE;
const uint8_t CMD_RESET = 0x01, CMD_COMMIT_ADDR = 0x30;
const uint8_t F_I_RMS = 0, F_P_REAL = 2, F_PF = 3;       // channel window fields (inputs 4..7)
struct Variant { const char *name; uint8_t inputs; };
const Variant VARIANTS[] = {{"?", 0}, {"UI1", 1}, {"UI2", 2}, {"UI3", 3}, {"I1", 1}, {"I2", 2}, {"I3", 3}, {"UI5", 5}, {"UI7", 7}};
const char *CT_NAMES[] = {"none", "SCT-013-005 (5 A)", "SCT-013-010 (10 A)", "SCT-013-030 (30 A)", "SCT-013-050 (50 A)",
                          "SCT-013-100 (100 A)", "SCT-013-020 (20 A)", "SCT-013-060 (60 A)"};

volatile uint32_t drdyCount = 0;
void IRAM_ATTR onDrdy() { drdyCount++; }

// ------------------------------------------------------------------ I2C helpers
static bool writeReg(uint8_t a, uint8_t reg, uint8_t val) {
  Wire.beginTransmission(a);
  Wire.write(reg);
  Wire.write(val);
  return Wire.endTransmission() == 0;
}

static bool readRegs(uint8_t a, uint8_t reg, uint8_t *buf, size_t n) {   // register pointer, STOP, then the read
  Wire.beginTransmission(a);
  Wire.write(reg);
  if (Wire.endTransmission(true) != 0) return false;
  size_t got = 0;
  while (got < n) {                                                       // at most 32 bytes per request
    size_t chunk = min((size_t)32, n - got);
    if (Wire.requestFrom(a, (uint8_t)chunk) != chunk) return false;
    for (size_t i = 0; i < chunk; i++) buf[got++] = Wire.read();
    if (got < n) {                                                        // next part: set the pointer again
      Wire.beginTransmission(a);
      Wire.write((uint8_t)(reg + got));
      if (Wire.endTransmission(true) != 0) return false;
    }
  }
  return true;
}

static bool ack(uint8_t a) {
  Wire.beginTransmission(a);
  return Wire.endTransmission() == 0;
}

// A real device at this address: an ACK AND a byte back when read (an rbAmp module ACKs every address, a missing part only ACKs the write).
static bool real(uint8_t a) {
  if (!ack(a)) return false;
  uint8_t b = 0;
  if (readRegs(a, REG_VERSION, &b, 1)) return true;
  Wire.beginTransmission(a);                                  // a part without registers (PCF8574 ...): plain read
  return Wire.endTransmission() == 0 && Wire.requestFrom(a, (uint8_t)1) == 1;
}

static float f32(const uint8_t *p) { float f; memcpy(&f, p, 4); return isfinite(f) ? f : NAN; }

static void startWire() {
  Wire.begin(PIN_SDA, PIN_SCL, I2C_HZ);
  Wire.setTimeOut(50);
}

static void recoverBus() {              // 9 clocks + STOP: frees a device that holds SDA low
  Wire.end();
  pinMode(PIN_SDA, INPUT_PULLUP);
  pinMode(PIN_SCL, INPUT_PULLUP);
  delayMicroseconds(20);
  int n = 0;
  while (digitalRead(PIN_SDA) == LOW && n < 9) {
    pinMode(PIN_SCL, OUTPUT); digitalWrite(PIN_SCL, LOW); delayMicroseconds(20);
    pinMode(PIN_SCL, INPUT_PULLUP); delayMicroseconds(20);
    n++;
  }
  pinMode(PIN_SDA, OUTPUT); digitalWrite(PIN_SDA, LOW); delayMicroseconds(20);
  pinMode(PIN_SCL, INPUT_PULLUP); delayMicroseconds(20);
  pinMode(PIN_SDA, INPUT_PULLUP); delayMicroseconds(20);
  Serial.printf("recovery: %d clock pulse(s), SDA now %d\n", n, digitalRead(PIN_SDA));
  startWire();
}

static bool isRbamp(uint8_t a) {
  uint8_t pid = 0;
  return readRegs(a, REG_PRODUCT_ID, &pid, 1) && pid == 0x01;
}

// ------------------------------------------------------------------ commands
static void cmdPins() {
  Serial.printf("SDA=GPIO%d SCL=GPIO%d DRDY=%d  clock=%lu Hz  module=0x%02X\n", PIN_SDA, PIN_SCL, PIN_DRDY, (unsigned long)I2C_HZ, cur);
}

static void cmdBus() {                  // idle levels, an address nobody has, recovery
  Wire.end();
  pinMode(PIN_SDA, INPUT); pinMode(PIN_SCL, INPUT);
  delay(2);
  int sda = digitalRead(PIN_SDA), scl = digitalRead(PIN_SCL);
  Serial.printf("idle levels: SDA=%d SCL=%d -> %s\n", sda, scl, (sda && scl) ? "ok" : "a line is held LOW (wiring / power / short)");
  startWire();
  const uint8_t canary[] = {0x03, 0x1F, 0x7E};
  bool flood = false;
  for (uint8_t a : canary) flood = flood || ack(a);
  Serial.printf("addresses nobody should have (0x03 0x1F 0x7E): %s\n", flood ? "they ACK = the bus answers EVERYWHERE (an rbAmp module does this)" : "silent = normal");
  if (sda == 0 || scl == 0) recoverBus();
}

static void cmdScan() {
  uint8_t acks[112];
  int n = 0;
  for (uint8_t a = 0x08; a <= 0x77; a++)
    if (ack(a)) acks[n++] = a;
  Serial.printf("%d address(es) ACK%s\n", n, n > 16 ? "  -> FLOOD: an ACK alone proves nothing, only the ones that return data are real" : "");
  int real = 0;
  for (int i = 0; i < n; i++) {
    uint8_t b = 0;
    bool rd = readRegs(acks[i], REG_VERSION, &b, 1);
    if (n > 16 && !rd) continue;                       // flooded: list only what returns a byte
    real++;
    Serial.printf("  0x%02X  %s", acks[i], rd ? "" : "(ACK only)");
    if (isRbamp(acks[i])) Serial.print(" rbAmp (PRODUCT_ID 0x01)");
    else if (acks[i] == 0x68) Serial.print(" DS3231 clock?");
    else if (acks[i] >= 0x50 && acks[i] <= 0x57) Serial.print(" rbAmp range / clock EEPROM");
    else if (acks[i] == 0x44 || acks[i] == 0x45) Serial.print(" SHT3x?");
    else if (acks[i] == 0x40) Serial.print(" SHT20 / SHT4x?");
    else if (acks[i] >= 0x20 && acks[i] <= 0x27) Serial.print(" relay / I/O expander?");
    Serial.print("\n");
  }
  Serial.printf("%d listed\n", real);
}

static void cmdId() {
  uint8_t pid = 0, var = 0, cap[2] = {0, 0}, cls = 0, ver = 0, adr = 0, st = 0, er = 0, hz = 0, up[4] = {0};
  if (!readRegs(cur, REG_PRODUCT_ID, &pid, 1)) { Serial.printf("0x%02X does not answer (check wiring, address, 'use', 'bus')\n", cur); return; }
  readRegs(cur, REG_HW_VARIANT, &var, 1);
  readRegs(cur, REG_CAPABILITY, cap, 2);
  readRegs(cur, REG_SENSOR_CLASS, &cls, 1);
  readRegs(cur, REG_VERSION, &ver, 1);
  readRegs(cur, REG_I2C_ADDRESS, &adr, 1);
  readRegs(cur, REG_STATUS, &st, 1);
  readRegs(cur, REG_ERROR, &er, 1);
  readRegs(cur, REG_AC_FREQ, &hz, 1);
  readRegs(cur, REG_UPTIME, up, 4);
  uint32_t upt = up[0] | (up[1] << 8) | (up[2] << 16) | ((uint32_t)up[3] << 24);
  Serial.printf("module 0x%02X: PRODUCT_ID=0x%02X %s\n", cur, pid, pid == 0x01 ? "(rbAmp)" : "(NOT an rbAmp)");
  Serial.printf("  variant 0x%02X = %s (%u current inputs)\n", var, var <= 8 ? VARIANTS[var].name : "unknown", var <= 8 ? VARIANTS[var].inputs : 0);
  Serial.printf("  capability 0x%02X%02X: voltage input %s, keeps CT model over restart %s\n", cap[1], cap[0], (cap[1] & 1) ? "yes" : "no", (cap[1] & 0x10) ? "yes" : "no");
  Serial.printf("  VERSION reg 0x03 = 0x%02X, sensor class = %u (1 = SCT-013), I2C address register = 0x%02X %s\n", ver, cls, adr, adr == cur ? "(matches)" : "(DIFFERENT)");
  Serial.printf("  STATUS=0x%02X ERROR=0x%02X AC freq=%u Hz  uptime=%lu\n", st, er, hz, (unsigned long)upt);
  uint8_t n = var <= 8 ? VARIANTS[var].inputs : 0;
  for (uint8_t i = 0; i < n; i++) {
    uint8_t m = 0;
    if (i < 3) {
      readRegs(cur, REG_APPLIED_CT0 + i, &m, 1);
    } else {  // inputs 4..7: channel window field 15 (0x51 + i would be PRODUCT_ID, HW_VARIANT ...)
      writeReg(cur, REG_CH_SELECT, (uint8_t)((15 << 4) | i));
      readRegs(cur, REG_CH_DATA, &m, 1);
    }
    Serial.printf("  input %u clamp model: %u = %s\n", i + 1, m, m < 8 ? CT_NAMES[m] : "?");
  }
}

static bool windowRead(uint8_t field, uint8_t in, float &out) {
  if (!writeReg(cur, REG_CH_SELECT, (uint8_t)((field << 4) | in))) return false;
  uint8_t b[4];
  if (!readRegs(cur, REG_CH_DATA, b, 4)) return false;
  out = f32(b);
  return true;
}

static bool snapshot(bool header) {      // one reading of every input of the module
  uint8_t var = 0, cap[2] = {0, 0}, st = 0, valid = 0, hz = 0;
  if (!readRegs(cur, REG_HW_VARIANT, &var, 1) || var < 1 || var > 8) { Serial.println("no rbAmp answers (variant unreadable)"); return false; }
  readRegs(cur, REG_CAPABILITY, cap, 2);
  uint8_t rt[56];
  if (!readRegs(cur, REG_RT, rt, sizeof(rt))) { Serial.println("real-time block (0x86) unreadable"); return false; }
  readRegs(cur, REG_STATUS, &st, 1);
  readRegs(cur, REG_RT_STATUS, &valid, 1);
  readRegs(cur, REG_AC_FREQ, &hz, 1);
  bool volt = cap[1] & 1;
  uint8_t n = VARIANTS[var].inputs;
  if (header) Serial.printf("%s 0x%02X: status 0x%02X, result %s%s\n", VARIANTS[var].name, cur, st, (valid & 1) ? "valid" : "NOT valid yet", (st & 2) ? ", MODULE ERROR" : "");
  if (volt) Serial.printf("  voltage %.1f V  %u Hz\n", f32(rt), hz);
  for (uint8_t i = 0; i < n; i++) {
    float a = NAN, w = NAN, pf = NAN;
    if (i < 3) { a = f32(rt + 8 + i * 4); w = f32(rt + 32 + i * 4); pf = f32(rt + 44 + i * 4); }
    else { windowRead(F_I_RMS, i, a); windowRead(F_P_REAL, i, w); windowRead(F_PF, i, pf); }
    Serial.printf("  input %u: %8.3f A  %9.2f W  PF %5.3f\n", i + 1, a, w, pf);
  }
  return true;
}

static void cmdWatch(int seconds) {
  Serial.printf("reading every second for %d s (send any character to stop)\n", seconds);
  while (Serial.available()) Serial.read();
  for (int s = 0; s < seconds && !Serial.available(); s++) {
    Serial.printf("[%d] ", s + 1);
    if (!snapshot(true)) break;
    delay(1000);
  }
  while (Serial.available()) Serial.read();
}

static void cmdDrdy() {
  if (PIN_DRDY < 0) { Serial.println("DRDY pin not set (pins <sda> <scl> <drdy>)"); return; }
  pinMode(PIN_DRDY, INPUT_PULLUP);
  drdyCount = 0;
  attachInterrupt(digitalPinToInterrupt(PIN_DRDY), onDrdy, FALLING);
  delay(2000);
  detachInterrupt(digitalPinToInterrupt(PIN_DRDY));
  Serial.printf("DRDY falling edges in 2 s: %lu (%.1f/s)\n", (unsigned long)drdyCount, drdyCount / 2.0);
}

static void cmdReg(uint8_t reg, int n) {
  uint8_t b[64];
  n = constrain(n, 1, 64);
  if (!readRegs(cur, reg, b, n)) { Serial.println("read failed"); return; }
  Serial.printf("0x%02X:", reg);
  for (int i = 0; i < n; i++) Serial.printf(" %02X", b[i]);
  if (n == 4) Serial.printf("   (as float %.4f)", f32(b));
  Serial.print("\n");
}

static void cmdAddr(uint8_t from, uint8_t to, bool force) {   // the same steps as the meter firmware
  if (to < 0x08 || to > 0x77 || from == to) { Serial.println("new address must be 0x08..0x77 and different"); return; }
  if (!isRbamp(from)) { Serial.printf("no rbAmp at 0x%02X\n", from); return; }
  if (!force && real(to)) { Serial.printf("a device already answers at 0x%02X (it returns data): connect ONLY this module, or use a free address (addr %02X %02X force = skip this check)\n", to, from, to); return; }
  Serial.printf("changing 0x%02X -> 0x%02X (only ONE rbAmp may be on the bus) ...\n", from, to);
  bool ok = writeReg(from, REG_I2C_ADDRESS, to);        // stage
  ok = ok && writeReg(from, REG_ADDR_MAGIC, 0xA5);      // arm (expires after 5 s)
  ok = ok && writeReg(from, REG_COMMAND, CMD_COMMIT_ADDR);
  delay(700);                                           // flash write
  writeReg(from, REG_COMMAND, CMD_RESET);               // the new address applies after a reset
  delay(500);
  bool alive = isRbamp(to);
  Serial.printf("writes %s, new address %s\n", ok ? "ok" : "FAILED", alive ? "ANSWERS: label the module! (use it with: use 0x.. )" : "does NOT answer (power-cycle the module and run: scan)");
  if (alive) cur = to;
}

static void help() {
  Serial.println(
      "commands (numbers: 0x50 or 80):\n"
      "  help                 this list\n"
      "  pins [sda scl drdy]  show / set the pins (drdy -1 = none), restarts the bus\n"
      "  speed <hz>           I2C clock, e.g. speed 50000 (rbAmp: 50 kHz)\n"
      "  use <addr>           the module the next commands talk to (now 0x50 by default)\n"
      "  bus                  idle levels, flood check, bus recovery\n"
      "  scan                 all addresses; with a flood only the ones that return data\n"
      "  id                   identify the module: product, variant, voltage input, clamp models, uptime\n"
      "  data                 one reading: voltage, Hz, A / W / PF of every input\n"
      "  watch [seconds]      reading every second (default 30)\n"
      "  drdy                 count DRDY pulses for 2 s\n"
      "  reg <reg> [n]        raw read of n registers (1..64), e.g. reg 0x54  or  reg 0x86 4\n"
      "  wreg <reg> <val>     raw write of one register (careful)\n"
      "  ct <input 1-7> <code 0-7> [win|cmd|both]  set the clamp model of one input (code: 1=5A 2=10A 3=30A 4=50A 5=100A 6=20A 7=60A), then show the models read back; win = channel window (default for inputs 4-7), cmd = stage + command 0x28+input (inputs 1-3), both\n"
      "  addr <from> <to>     change the I2C address (ONE module on the bus!), e.g. addr 0x50 0x51  (add  force  to skip the free-address check)\n"
      "  reset                send the reset command to the module");
}

// Clamp model of one input, the two ways the meter firmware knows: the channel window (field 15) or stage + command.
static uint8_t appliedModel(uint8_t i) {
  uint8_t m = 0;
  if (i < 3) {
    readRegs(cur, REG_APPLIED_CT0 + i, &m, 1);
  } else {
    writeReg(cur, REG_CH_SELECT, (uint8_t)((15 << 4) | i));
    readRegs(cur, REG_CH_DATA, &m, 1);
  }
  return m;
}
static void cmdCt(long input, long code, const char *how) {
  if (input < 1 || input > 7 || code < 0 || code > 7) { Serial.println("usage: ct <input 1-7> <code 0-7> [win|cmd|both]"); return; }
  uint8_t i = input - 1;
  bool win = how ? (!strcmp(how, "win") || !strcmp(how, "both")) : i >= 3;
  bool cm = how ? (!strcmp(how, "cmd") || !strcmp(how, "both")) : i < 3;
  Serial.printf("input %ld: model was %u\n", input, appliedModel(i));
  if (win) {
    writeReg(cur, REG_CH_SELECT, (uint8_t)((15 << 4) | i));
    writeReg(cur, REG_CH_DATA, (uint8_t)code);
    delay(5);
    Serial.printf("  window write done: reads back %u\n", appliedModel(i));
  }
  if (cm) {
    writeReg(cur, REG_CT_MODEL, (uint8_t)code);
    writeReg(cur, 0x01, (uint8_t)(0x28 + i));
    delay(10);
    uint8_t e = 0;
    readRegs(cur, 0x02, &e, 1);
    Serial.printf("  stage + command 0x%02X done (ERROR reg 0x%02X): reads back %u\n", 0x28 + i, e, appliedModel(i));
  }
  Serial.println("  (not saved to the module's flash: use  wreg 0x01 0x32  to save, or let the meter firmware do it)");
}

// ------------------------------------------------------------------ command line
static long num(const char *s) { return strtol(s, nullptr, 0); }

static void handle(char *line) {
  char *tok[5] = {nullptr};
  int n = 0;
  for (char *p = strtok(line, " \t"); p && n < 5; p = strtok(nullptr, " \t")) tok[n++] = p;
  if (!n) return;
  const char *c = tok[0];
  if (!strcmp(c, "help") || !strcmp(c, "?")) help();
  else if (!strcmp(c, "pins")) {
    if (n >= 4) {
      Wire.end();
      PIN_SDA = num(tok[1]); PIN_SCL = num(tok[2]); PIN_DRDY = num(tok[3]);
      startWire();
    }
    cmdPins();
  } else if (!strcmp(c, "speed") && n >= 2) { I2C_HZ = num(tok[1]); Wire.setClock(I2C_HZ); cmdPins(); }
  else if (!strcmp(c, "use") && n >= 2) { cur = num(tok[1]); cmdPins(); }
  else if (!strcmp(c, "bus")) cmdBus();
  else if (!strcmp(c, "scan")) cmdScan();
  else if (!strcmp(c, "id")) cmdId();
  else if (!strcmp(c, "data")) snapshot(true);
  else if (!strcmp(c, "watch")) cmdWatch(n >= 2 ? num(tok[1]) : 30);
  else if (!strcmp(c, "drdy")) cmdDrdy();
  else if (!strcmp(c, "reg") && n >= 2) cmdReg(num(tok[1]), n >= 3 ? num(tok[2]) : 1);
  else if (!strcmp(c, "wreg") && n >= 3) Serial.printf("write %s\n", writeReg(cur, num(tok[1]), num(tok[2])) ? "ok" : "FAILED");
  else if (!strcmp(c, "ct") && n >= 3) cmdCt(num(tok[1]), num(tok[2]), n >= 4 ? tok[3] : nullptr);
  else if (!strcmp(c, "addr") && n >= 3) cmdAddr(num(tok[1]), num(tok[2]), n >= 4 && !strcmp(tok[3], "force"));
  else if (!strcmp(c, "reset")) Serial.printf("reset %s\n", writeReg(cur, REG_COMMAND, CMD_RESET) ? "sent" : "FAILED");
  else Serial.println("unknown command: type  help");
}

void setup() {
  Serial.begin(115200);
  delay(1500);
  Serial.println("\n=== rbAmp debug tool ===");
  pinMode(PIN_SDA, INPUT); pinMode(PIN_SCL, INPUT);
  delay(2);
  Serial.printf("idle levels: SDA=%d SCL=%d\n", digitalRead(PIN_SDA), digitalRead(PIN_SCL));
  startWire();
  delay(300);
  cmdPins();
  help();
  Serial.print("> ");
}

void loop() {
  static char line[96];
  static size_t len = 0;
  while (Serial.available()) {
    char ch = Serial.read();
    if (ch == '\r' || ch == '\n') {
      if (len) {
        line[len] = 0;
        len = 0;
        Serial.println();
        handle(line);
        Serial.print("> ");
      }
    } else if (len < sizeof(line) - 1) {
      line[len++] = ch;
    }
  }
}
