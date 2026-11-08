#include "temp.h"

#if TEMP_ENABLE
#include <driver/gpio.h>

#include "i2cbus.h"

namespace temp {

#if TEMP_SHT
struct ShtCfg {
  uint8_t type, address;
};
static constexpr ShtCfg SHT[] = SHT_SENSORS;
static constexpr uint8_t SHT_N = sizeof(SHT) / sizeof(SHT[0]);
#else
static constexpr uint8_t SHT_N = 0;
#endif
#if TEMP_DS18B20
static constexpr uint8_t DS_MAX = TEMP_MAX_SENSORS;
#else
static constexpr uint8_t DS_MAX = 0;
#endif
static constexpr uint8_t ALL = SHT_N + DS_MAX > 0 ? SHT_N + DS_MAX : 1;

static float vals[ALL], hums[ALL];
static uint8_t fails[ALL];
static uint8_t nDs = 0;
static bool fresh = false;

static void clearValues() {
  for (uint8_t i = 0; i < ALL; i++) {
    vals[i] = hums[i] = NAN;
    fails[i] = 0;
  }
}

static uint8_t crc8(const uint8_t *p, uint8_t len, uint8_t poly, uint8_t init) {  // Sensirion (MSB first)
  uint8_t crc = init;
  while (len--) {
    crc ^= *p++;
    for (uint8_t i = 0; i < 8; i++) crc = crc & 0x80 ? (uint8_t)((crc << 1) ^ poly) : (uint8_t)(crc << 1);
  }
  return crc;
}

// ------------------------------------------------------------------ SHT20 / SHT3x / SHT4x (I2C)
#if TEMP_SHT
static const char *shtName(uint8_t t) { return t == SHT_20 ? "SHT20" : t == SHT_3X ? "SHT3x" : "SHT4x"; }

static void shtTrigger(uint8_t j) {
  const ShtCfg &c = SHT[j];
  if (c.type == SHT_3X) {
    uint8_t cmd[2] = {0x24, 0x00};  // single shot, high repeatability, no clock stretching (~15 ms)
    i2cbus::writeRaw(c.address, cmd, 2);
  } else {
    uint8_t cmd = c.type == SHT_4X ? 0xFD : 0xF3;  // SHT4x high precision (~9 ms) / SHT20 T no hold (~85 ms)
    i2cbus::writeRaw(c.address, &cmd, 1);
  }
}

static bool shtRead(uint8_t j, float &t, float &rh) {
  const ShtCfg &c = SHT[j];
  uint8_t b[6];
  if (c.type == SHT_20) {
    if (!i2cbus::readRaw(c.address, b, 3) || crc8(b, 2, 0x31, 0x00) != b[2]) return false;
    t = -46.85f + 175.72f * (((b[0] << 8) | b[1]) & 0xFFFC) / 65536.0f;
    uint8_t cmd = 0xF5;  // RH, no hold (~29 ms)
    if (!i2cbus::writeRaw(c.address, &cmd, 1)) return false;
    delay(30);
    if (!i2cbus::readRaw(c.address, b, 3) || crc8(b, 2, 0x31, 0x00) != b[2]) return false;
    rh = -6.0f + 125.0f * (((b[0] << 8) | b[1]) & 0xFFFC) / 65536.0f;
  } else {
    if (!i2cbus::readRaw(c.address, b, 6) || crc8(b, 2, 0x31, 0xFF) != b[2] || crc8(b + 3, 2, 0x31, 0xFF) != b[5])
      return false;
    t = -45.0f + 175.0f * ((b[0] << 8) | b[1]) / 65535.0f;
    uint16_t r = (b[3] << 8) | b[4];
    rh = c.type == SHT_3X ? 100.0f * r / 65535.0f : -6.0f + 125.0f * r / 65535.0f;
  }
  rh = constrain(rh, 0.0f, 100.0f);
  return t > -45 && t < 130;
}
#endif

// ------------------------------------------------------------------ DS18B20 (1-Wire)
#if TEMP_DS18B20
static const gpio_num_t PIN = (gpio_num_t)PIN_TEMP;
static portMUX_TYPE mux = portMUX_INITIALIZER_UNLOCKED;
static uint8_t roms[DS_MAX][8];

// 1-Wire: open drain, timing per bit inside a short critical section (the 480 us reset pulse itself is
// not timing critical).
static inline void low() { gpio_set_level(PIN, 0); }
static inline void release() { gpio_set_level(PIN, 1); }

static bool owReset() {
  low();
  delayMicroseconds(480);
  portENTER_CRITICAL(&mux);
  release();
  delayMicroseconds(70);
  int presence = gpio_get_level(PIN);
  portEXIT_CRITICAL(&mux);
  delayMicroseconds(410);
  return presence == 0;
}

static void owWriteBit(bool b) {
  portENTER_CRITICAL(&mux);
  low();
  if (b) {
    delayMicroseconds(6);
    release();
    delayMicroseconds(64);
  } else {
    delayMicroseconds(60);
    release();
    delayMicroseconds(10);
  }
  portEXIT_CRITICAL(&mux);
}

static bool owReadBit() {
  portENTER_CRITICAL(&mux);
  low();
  delayMicroseconds(3);
  release();
  delayMicroseconds(10);
  int r = gpio_get_level(PIN);
  portEXIT_CRITICAL(&mux);
  delayMicroseconds(53);
  return r;
}

static void owWrite(uint8_t v) {
  for (uint8_t i = 0; i < 8; i++) owWriteBit(v & (1 << i));
}

static uint8_t owRead() {
  uint8_t v = 0;
  for (uint8_t i = 0; i < 8; i++)
    if (owReadBit()) v |= 1 << i;
  return v;
}

static uint8_t dsCrc(const uint8_t *p, uint8_t len) {  // Dallas / Maxim CRC-8 (LSB first)
  uint8_t crc = 0;
  while (len--) {
    uint8_t b = *p++;
    for (uint8_t i = 0; i < 8; i++) {
      uint8_t mix = (crc ^ b) & 1;
      crc >>= 1;
      if (mix) crc ^= 0x8C;
      b >>= 1;
    }
  }
  return crc;
}

// ROM search (Maxim application note 187): finds every device on the bus.
static uint8_t search(uint8_t out[][8], uint8_t max) {
  uint8_t rom[8] = {0}, found = 0;
  int lastDisc = -1;
  bool done = false;
  while (!done && found < max) {
    if (!owReset()) return found;
    owWrite(0xF0);  // SEARCH ROM
    int disc = -1;
    for (int bit = 0; bit < 64; bit++) {
      bool a = owReadBit(), b = owReadBit();
      bool dir;
      if (a && b) return found;  // nobody answered
      if (a != b) {
        dir = a;
      } else {  // conflict: both values present
        dir = bit < lastDisc ? (rom[bit / 8] >> (bit % 8)) & 1 : bit == lastDisc;
        if (!dir) disc = bit;
      }
      if (dir) rom[bit / 8] |= 1 << (bit % 8);
      else rom[bit / 8] &= ~(1 << (bit % 8));
      owWriteBit(dir);
    }
    lastDisc = disc;
    done = disc < 0;
    if (dsCrc(rom, 7) == rom[7] && (rom[0] == 0x28 || rom[0] == 0x22 || rom[0] == 0x3B)) memcpy(out[found++], rom, 8);
  }
  return found;
}

static String hex(const uint8_t *rom) {
  char b[24];
  snprintf(b, sizeof(b), "%02x-%02x%02x%02x%02x%02x%02x", rom[0], rom[6], rom[5], rom[4], rom[3], rom[2], rom[1]);
  return b;
}


static void dsScan() {
  nDs = search(roms, DS_MAX);
  // Sort by serial number: the same sensor keeps its number over a restart.
  for (uint8_t i = 0; i < nDs; i++)
    for (uint8_t j = i + 1; j < nDs; j++)
      if (memcmp(roms[j], roms[i], 8) < 0) {
        uint8_t t[8];
        memcpy(t, roms[i], 8);
        memcpy(roms[i], roms[j], 8);
        memcpy(roms[j], t, 8);
      }
  Serial.printf("[temp] %u DS18B20 sensor(s) on GPIO %d\n", nDs, PIN_TEMP);
  for (uint8_t i = 0; i < nDs; i++) Serial.printf("[temp]   T%u = %s\n", SHT_N + i + 1, hex(roms[i]).c_str());
}

static bool dsRead(uint8_t k, float &c) {
  if (!owReset()) return false;
  owWrite(0x55);  // MATCH ROM
  for (uint8_t b = 0; b < 8; b++) owWrite(roms[k][b]);
  owWrite(0xBE);  // READ SCRATCHPAD
  uint8_t sp[9];
  for (uint8_t b = 0; b < 9; b++) sp[b] = owRead();
  if (dsCrc(sp, 8) != sp[8]) return false;
  c = (int16_t)((sp[1] << 8) | sp[0]) / 16.0f;
  return c >= -55 && c <= 125;
}
#endif

// ------------------------------------------------------------------ common

void begin() {
  clearValues();
#if TEMP_SHT
  i2cbus::begin();
  for (uint8_t j = 0; j < SHT_N; j++)
    Serial.printf("[temp] T%u = %s @0x%02X: %s\n", j + 1, shtName(SHT[j].type), SHT[j].address,
                  i2cbus::probe(SHT[j].address) ? "OK" : "NOT ANSWERING (check wiring / address)");
#endif
#if TEMP_DS18B20
  gpio_reset_pin(PIN);
  gpio_set_direction(PIN, GPIO_MODE_INPUT_OUTPUT_OD);
  gpio_set_pull_mode(PIN, GPIO_PULLUP_ONLY);  // weak; fit 4.7 k to 3.3 V for real wiring
  release();
  delay(2);
  dsScan();
#endif
}

uint8_t count() { return SHT_N + nDs; }

void poll() {
  static uint32_t startMs = 0, lastScanMs = 0;
  static bool measuring = false, first = true;
  static uint8_t next = 0;
  uint32_t now = millis();
#if TEMP_DS18B20
  if (!nDs && now - lastScanMs >= 60000) {  // no DS18B20 found: look again every minute (plugged in later)
    lastScanMs = now;
    dsScan();
  }
#endif
  if (!count()) return;
  if (!measuring) {
    if (!first && now - startMs < TEMP_READ_MS) return;
#if TEMP_SHT
    for (uint8_t j = 0; j < SHT_N; j++) shtTrigger(j);
#endif
#if TEMP_DS18B20
    if (nDs && owReset()) {
      owWrite(0xCC);  // SKIP ROM: every DS18B20 converts at once
      owWrite(0x44);  // CONVERT T (12 bit: up to 750 ms)
    }
#endif
    measuring = true;
    startMs = now;
    next = 0;
    return;
  }
  if (now - startMs < 800) return;
  // One sensor per call keeps each call short (~12 ms), so the control loop is not held up.
  bool ok = false;
  float t = NAN, rh = NAN;
#if TEMP_SHT
  if (next < SHT_N) ok = shtRead(next, t, rh);
#endif
#if TEMP_DS18B20
  if (next >= SHT_N) ok = dsRead(next - SHT_N, t);
#endif
  if (ok) {
    vals[next] = t;
    hums[next] = rh;
    fails[next] = 0;
  } else if (++fails[next] >= 3) {  // keep the last value over a single bad read
    vals[next] = hums[next] = NAN;
  }
  if (++next >= count()) {
    measuring = false;
    if (!first) fresh = true;  // the very first round may still be a DS18B20's 85 C power-on value
    first = false;
  }
}

float value(uint8_t i) { return i < count() ? vals[i] : NAN; }
float humidity(uint8_t i) { return i < count() ? hums[i] : NAN; }
bool hasHumidity() { return SHT_N > 0; }
bool takeFresh() {
  bool f = fresh;
  fresh = false;
  return f;
}

String summary() {
  if (!count()) return "no sensor found";
  String s;
  for (uint8_t i = 0; i < count(); i++) {
    if (i) s += ", ";
    s += String("T") + (i + 1) + " ";
#if TEMP_SHT
    if (i < SHT_N) {
      char b[16];
      snprintf(b, sizeof(b), "%s @0x%02X", shtName(SHT[i].type), SHT[i].address);
      s += b;
    }
#endif
#if TEMP_DS18B20
    if (i >= SHT_N) s += String("DS18B20 ") + hex(roms[i - SHT_N]);
#endif
    s += " " + (isnan(vals[i]) ? String("--") : String(vals[i], 1)) + " C";
    if (!isnan(hums[i])) s += " " + String(hums[i], 0) + " %RH";
  }
  return s;
}

}  // namespace temp
#endif  // TEMP_ENABLE
