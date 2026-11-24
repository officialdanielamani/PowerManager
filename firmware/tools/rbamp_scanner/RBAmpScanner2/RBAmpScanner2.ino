// rbAmp + other I2C parts: finds out WHEN the bus starts to ACK everything ("flood") and what clears it.
// ESP32-S3, Serial Monitor 115200. Not tested on hardware by the author: send the whole output back.
// Wiring as in your first scanner: SDA = GPIO7, SCL = GPIO9, DRDY = GPIO4 (optional, -1 = not wired).
// Test it three times:  (A) only the rbAmp   (B) rbAmp + RTC   (C) everything.
#include <Wire.h>

const int PIN_SDA = 7, PIN_SCL = 9, PIN_DRDY = 4;
const uint8_t RBAMP = 0x50;
const uint32_t SPEEDS[] = {50000, 20000, 100000};       // tried in this order
const uint8_t CANARY[] = {0x03, 0x1F, 0x7E};            // addresses nothing should answer at: if one ACKs, the bus is flooded

volatile uint32_t drdy = 0;
void IRAM_ATTR onDrdy() { drdy++; }

static void level(const char *when) {   // SDA / SCL as the pins see them (call with the Wire driver stopped)
  Serial.printf("    levels %-28s SDA=%d SCL=%d\n", when, digitalRead(PIN_SDA), digitalRead(PIN_SCL));
}

static bool flooded() {                  // an ACK at an address nobody has
  for (uint8_t a : CANARY) {
    Wire.beginTransmission(a);
    if (Wire.endTransmission() == 0) return true;
  }
  return false;
}

static void recover() {                  // 9 clocks + STOP: frees a device that holds SDA low
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
  Serial.printf("    recovery: %d clock pulse(s), SDA now %d\n", n, digitalRead(PIN_SDA));
}

static bool readReg(uint8_t a, uint8_t reg, uint8_t &v) {
  Wire.beginTransmission(a);
  Wire.write(reg);
  if (Wire.endTransmission(true) != 0) return false;   // STOP, no repeated start
  if (Wire.requestFrom(a, (uint8_t)1) != 1) return false;
  v = Wire.read();
  return true;
}

static void step(const char *what) {     // after every step: is the bus still sane?
  bool f = flooded();
  Serial.printf("  [%s] canary: %s\n", what, f ? "FLOODED (an unused address ACKs)" : "clean");
  if (f) { recover(); Serial.printf("    after recovery: %s\n", flooded() ? "still flooded" : "clean"); }
}

static void probeList() {                // only the addresses that matter, ACK and a real read
  struct P { uint8_t a; const char *n; uint8_t reg; } list[] = {
    {0x50, "rbAmp", 0x03}, {0x68, "DS3231 RTC", 0x00}, {0x44, "SHT3x", 0xFF}, {0x45, "SHT3x (ADDR high)", 0xFF},
    {0x40, "SHT20 / SHT4x", 0xFF}, {0x20, "relay board", 0xFF}, {0x21, "relay board", 0xFF}};
  for (auto &p : list) {
    Wire.beginTransmission(p.a);
    uint8_t e = Wire.endTransmission();
    uint8_t v = 0;
    bool rd = (e == 0 && p.reg != 0xFF) ? readReg(p.a, p.reg, v) : false;
    Serial.printf("    0x%02X %-18s %s", p.a, p.n, e == 0 ? "ACK" : "-");
    if (rd) Serial.printf("  reg 0x%02X = 0x%02X", p.reg, v);
    Serial.print("\n");
  }
}

void setup() {
  Serial.begin(115200);
  delay(1500);
  Serial.println("\n=== rbAmp bus flood finder ===");
  pinMode(PIN_SDA, INPUT); pinMode(PIN_SCL, INPUT);
  level("before Wire.begin");
  if (PIN_DRDY >= 0) {
    pinMode(PIN_DRDY, INPUT_PULLUP);
    attachInterrupt(digitalPinToInterrupt(PIN_DRDY), onDrdy, FALLING);
  }
  for (uint32_t hz : SPEEDS) {
    Serial.printf("\n--- %lu Hz ---\n", (unsigned long)hz);
    Wire.begin(PIN_SDA, PIN_SCL, hz);
    Wire.setTimeOut(50);
    delay(300);
    step("after start");
    Wire.beginTransmission(RBAMP);
    Serial.printf("  ping 0x50: %s\n", Wire.endTransmission() == 0 ? "ACK" : "no answer");
    step("after ping");
    uint8_t v;
    if (readReg(RBAMP, 0x03, v)) Serial.printf("  VERSION: 0x%02X\n", v);
    else Serial.print("  VERSION: read failed\n");
    step("after reading the rbAmp");
    probeList();
    step("after the list");
    if (PIN_DRDY >= 0) {
      drdy = 0; delay(2000);
      Serial.printf("  DRDY edges in 2 s: %lu\n", (unsigned long)drdy);
      step("after DRDY wait");
    }
    Wire.end();
    level("after Wire.end");
  }
  Serial.println("\n=== done: copy everything above ===");
}

void loop() {}
