#include "temp.h"

#if TEMP_ENABLE
#include <driver/gpio.h>

namespace temp {

static const gpio_num_t PIN = (gpio_num_t)PIN_TEMP;
static portMUX_TYPE mux = portMUX_INITIALIZER_UNLOCKED;

static uint8_t roms[TEMP_MAX_SENSORS][8];
static float vals[TEMP_MAX_SENSORS];
static uint8_t fails[TEMP_MAX_SENSORS];
static uint8_t n = 0;
static bool fresh = false;

// ------------------------------------------------------------------ 1-Wire (open drain, timing per bit
// inside a short critical section; the 480 us reset pulse itself is not timing critical)

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

static uint8_t crc8(const uint8_t *p, uint8_t len) {
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
    if (crc8(rom, 7) == rom[7] && (rom[0] == 0x28 || rom[0] == 0x22 || rom[0] == 0x3B)) memcpy(out[found++], rom, 8);
  }
  return found;
}

static String hex(const uint8_t *rom) {
  char b[24];
  snprintf(b, sizeof(b), "%02x-%02x%02x%02x%02x%02x%02x", rom[0], rom[6], rom[5], rom[4], rom[3], rom[2], rom[1]);
  return b;
}

static void scan() {
  n = search(roms, TEMP_MAX_SENSORS);
  // Sort by serial number: the same sensor keeps its number (T1, T2 ...) over a restart.
  for (uint8_t i = 0; i < n; i++)
    for (uint8_t j = i + 1; j < n; j++)
      if (memcmp(roms[j], roms[i], 8) < 0) {
        uint8_t t[8];
        memcpy(t, roms[i], 8);
        memcpy(roms[i], roms[j], 8);
        memcpy(roms[j], t, 8);
      }
  for (uint8_t i = 0; i < TEMP_MAX_SENSORS; i++) {
    vals[i] = NAN;
    fails[i] = 0;
  }
  Serial.printf("[temp] %u DS18B20 sensor(s) on GPIO %d\n", n, PIN_TEMP);
  for (uint8_t i = 0; i < n; i++) Serial.printf("[temp]   T%u = %s\n", i + 1, hex(roms[i]).c_str());
}

void begin() {
  gpio_reset_pin(PIN);
  gpio_set_direction(PIN, GPIO_MODE_INPUT_OUTPUT_OD);
  gpio_set_pull_mode(PIN, GPIO_PULLUP_ONLY);  // weak; fit 4.7 k to 3.3 V for real wiring
  release();
  delay(2);
  scan();
}

static bool readOne(uint8_t i) {
  if (!owReset()) return false;
  owWrite(0x55);  // MATCH ROM
  for (uint8_t k = 0; k < 8; k++) owWrite(roms[i][k]);
  owWrite(0xBE);  // READ SCRATCHPAD
  uint8_t sp[9];
  for (uint8_t k = 0; k < 9; k++) sp[k] = owRead();
  if (crc8(sp, 8) != sp[8]) return false;
  int16_t raw = (int16_t)((sp[1] << 8) | sp[0]);
  float c = raw / 16.0f;
  if (c < -55 || c > 125) return false;
  vals[i] = c;
  return true;
}

void poll() {
  static uint32_t convStartMs = 0, lastScanMs = 0;
  static bool converting = false, first = true;
  static uint8_t next = 0;
  uint32_t now = millis();
  if (!n) {  // nothing found: look again every minute (sensor plugged in later)
    if (now - lastScanMs >= 60000) {
      lastScanMs = now;
      scan();
    }
    return;
  }
  if (!converting) {
    if (!first && now - convStartMs < TEMP_READ_MS) return;
    if (!owReset()) {
      for (uint8_t i = 0; i < n; i++) vals[i] = NAN;
      convStartMs = now;
      first = false;
      return;
    }
    owWrite(0xCC);  // SKIP ROM: every sensor converts at once
    owWrite(0x44);  // CONVERT T (12 bit: up to 750 ms)
    converting = true;
    convStartMs = now;
    next = 0;
    return;
  }
  if (now - convStartMs < 800) return;
  // One sensor per call keeps each call short (~12 ms), so the control loop is not held up.
  if (!readOne(next)) {
    if (++fails[next] >= 3) vals[next] = NAN;  // keep the last value over a single bad read
  } else {
    fails[next] = 0;
  }
  if (++next >= n) {
    converting = false;
    if (!first) fresh = true;  // the very first round may still be the 85 C power-on value: skip it
    first = false;
  }
}

uint8_t count() { return n; }
float value(uint8_t i) { return i < n ? vals[i] : NAN; }
bool takeFresh() {
  bool f = fresh;
  fresh = false;
  return f;
}
String summary() {
  if (!n) return "no sensor found";
  String s;
  for (uint8_t i = 0; i < n; i++) {
    if (i) s += ", ";
    s += String("T") + (i + 1) + " " + hex(roms[i]) + " " + (isnan(vals[i]) ? String("--") : String(vals[i], 1)) + " C";
  }
  return s;
}

}  // namespace temp
#endif  // TEMP_ENABLE
