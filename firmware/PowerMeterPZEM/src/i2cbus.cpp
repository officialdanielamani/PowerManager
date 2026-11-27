#include "i2cbus.h"

#include "../config.h"

namespace i2cbus {

static SemaphoreHandle_t mtx = nullptr;
static bool started = false;
static uint8_t busFaults = 0;       // consecutive transactions that failed in a way a missing device does not cause
static uint32_t recovered = 0, lastRecoverMs = 0, lastCheckMs = 0;
static const uint8_t FAULTS_BEFORE_RECOVERY = 3;
static const uint32_t RECOVERY_MIN_GAP_MS = 2000;  // do not hammer a bus that stays broken

static void startWire() {
  Wire.begin(PIN_I2C_SDA, PIN_I2C_SCL, I2C_FREQ_HZ);
  Wire.setTimeOut(50);
}

void begin() {
  if (started) return;
  if (!mtx) mtx = xSemaphoreCreateRecursiveMutex();
  startWire();
  started = true;
}

// A device that browned out in the middle of a byte can keep SDA low and block the whole bus (rbAmp, relays, clock,
// sensors). Standard cure: stop the Wire driver, clock SCL up to 9 times until the device lets go of SDA, send a STOP,
// start the driver again. Takes the bus lock, so no transaction is cut off.
static void recoverBus(const char *why) {
  Lock l;
  Wire.end();
  pinMode(PIN_I2C_SDA, INPUT_PULLUP);
  pinMode(PIN_I2C_SCL, INPUT_PULLUP);
  delayMicroseconds(10);
  int pulses = 0;
  while (digitalRead(PIN_I2C_SDA) == LOW && pulses < 9) {  // open-drain style: SCL low = output LOW, SCL high = input (pulled up)
    pinMode(PIN_I2C_SCL, OUTPUT);
    digitalWrite(PIN_I2C_SCL, LOW);
    delayMicroseconds(10);
    pinMode(PIN_I2C_SCL, INPUT_PULLUP);
    delayMicroseconds(10);
    pulses++;
  }
  pinMode(PIN_I2C_SDA, OUTPUT);  // STOP: SDA low -> SCL high -> SDA high
  digitalWrite(PIN_I2C_SDA, LOW);
  delayMicroseconds(10);
  pinMode(PIN_I2C_SCL, INPUT_PULLUP);
  delayMicroseconds(10);
  pinMode(PIN_I2C_SDA, INPUT_PULLUP);
  delayMicroseconds(10);
  bool free = digitalRead(PIN_I2C_SDA) == HIGH;
  startWire();
  busFaults = 0;
  recovered++;
  lastRecoverMs = millis();
  Serial.printf("[i2c] bus recovery (%s): %d clock pulse(s), SDA %s\n", why, pulses, free ? "high again" : "STILL LOW (a device holds it: check wiring / power)");
}

bool ready() { return started; }
uint32_t recoveries() { return recovered; }

// Result of one Wire transaction. 0 = ok, 2 / 3 = NACK (nothing answers at that address: normal for a device that is
// unplugged, not a bus fault), 1 / 4 / 5 = too long / other error / timeout: the bus itself is in trouble.
static void note(uint8_t rc) {
  if (rc == 0) {
    busFaults = 0;
  } else if (rc != 2 && rc != 3) {
    if (busFaults < 255) busFaults++;
    if (busFaults >= FAULTS_BEFORE_RECOVERY && millis() - lastRecoverMs >= RECOVERY_MIN_GAP_MS) recoverBus("transactions time out");
  }
}

void tick() {
  if (!started || millis() - lastCheckMs < 1000) return;
  lastCheckMs = millis();
  Lock l;  // no transaction is running while we hold the lock: SDA must be high on an idle bus
  if (digitalRead(PIN_I2C_SDA) == LOW && millis() - lastRecoverMs >= RECOVERY_MIN_GAP_MS) recoverBus("SDA held low");
}

SemaphoreHandle_t mutex() {
  if (!mtx) mtx = xSemaphoreCreateRecursiveMutex();
  return mtx;
}

bool readRegs(uint8_t addr, uint8_t reg, uint8_t *buf, size_t n) {
  Lock l;
  Wire.beginTransmission(addr);
  Wire.write(reg);
  uint8_t rc = Wire.endTransmission(false);  // repeated start
  if (rc != 0) {
    note(rc);
    return false;
  }
  if (Wire.requestFrom((uint16_t)addr, (uint8_t)n) != n) {
    note(5);  // the address was acknowledged but the data did not come: a timeout, not a missing device
    return false;
  }
  for (size_t i = 0; i < n; i++) buf[i] = Wire.read();
  note(0);
  return true;
}

bool readRaw(uint8_t addr, uint8_t *buf, size_t n) {
  Lock l;
  if (Wire.requestFrom((uint16_t)addr, (uint8_t)n) != n) return false;
  for (size_t i = 0; i < n; i++) buf[i] = Wire.read();
  return true;
}

bool probe(uint8_t addr) {
  Lock l;
  Wire.beginTransmission(addr);
  uint8_t rc = Wire.endTransmission();
  note(rc);
  return rc == 0;
}

// An rbAmp module ACKs EVERY address (SCT-UI7 alone on the bus: 112 of 112), so an ACK does not prove that a part is there.
// flooded(): an address nobody has ACKs (looked at most every 10 s). Then present() also wants a byte back from a read,
// which a missing device never gives (it only ACKs the write).
static bool flooded() {
  static bool flood = false;
  static uint32_t checkedAt = 0;
  if (checkedAt && millis() - checkedAt < 10000) return flood;
  checkedAt = millis() | 1;
  flood = false;
  for (uint8_t a : {0x03, 0x1F, 0x7E}) flood = flood || probe(a);
  return flood;
}

bool present(uint8_t addr) {
  Lock l;
  if (!probe(addr)) return false;
  if (!flooded()) return true;
  uint8_t b = 0;
  return readRegs(addr, 0x03, &b, 1) || readRaw(addr, &b, 1);
}

bool writeReg(uint8_t addr, uint8_t reg, uint8_t val) {
  uint8_t d[2] = {reg, val};
  return writeRaw(addr, d, 2);
}

bool writeRaw(uint8_t addr, const uint8_t *data, size_t n) {
  Lock l;
  Wire.beginTransmission(addr);
  Wire.write(data, n);
  uint8_t rc = Wire.endTransmission();
  note(rc);
  return rc == 0;
}

}  // namespace i2cbus
