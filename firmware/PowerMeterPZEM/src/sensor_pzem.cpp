#include "sensor_pzem.h"
#include "../config.h"

uint16_t modbusCrc(const uint8_t *buf, size_t len) {
  uint16_t crc = 0xFFFF;
  for (size_t i = 0; i < len; i++) {
    crc ^= buf[i];
    for (int b = 0; b < 8; b++) crc = (crc & 1) ? (crc >> 1) ^ 0xA001 : crc >> 1;
  }
  return crc;
}

// PZEM sends 32-bit values as low word first, each word big-endian.
static uint32_t u32(const uint8_t *p) {
  return ((uint32_t)p[0] << 8 | p[1]) | ((uint32_t)p[2] << 24 | (uint32_t)p[3] << 16);
}
static uint16_t u16(const uint8_t *p) { return (uint16_t)p[0] << 8 | p[1]; }

bool ModbusSensor::transact(uint8_t *req, size_t len, uint8_t *resp, size_t respLen) {
  uint16_t crc = modbusCrc(req, len);
  req[len] = crc & 0xFF;
  req[len + 1] = crc >> 8;
  while (port_.available()) port_.read();  // drop stale bytes
  if (de_ >= 0) digitalWrite(de_, HIGH);   // RS485: transmit
  port_.write(req, len + 2);
  port_.flush();                           // wait until the last bit is out
  if (de_ >= 0) digitalWrite(de_, LOW);    // RS485: receive

  size_t got = 0;
  uint32_t deadline = millis() + PZEM_TIMEOUT_MS;
  while (got < respLen && (int32_t)(deadline - millis()) > 0) {
    if (port_.available()) {
      resp[got++] = port_.read();
    } else {
      vTaskDelay(pdMS_TO_TICKS(2));  // yield, never busy-wait
    }
  }
  if (got == 0) {
    err_ = SENS_NO_REPLY;
    return false;
  }
  err_ = SENS_BAD;
  if (got != respLen || resp[1] != req[1]) return false;
  uint16_t rc = modbusCrc(resp, respLen - 2);
  if (resp[respLen - 2] != (rc & 0xFF) || resp[respLen - 1] != (rc >> 8)) return false;
  err_ = SENS_OK;
  return true;
}

// ------------------------------------------------------------------ PZEM-004T (AC)

void PzemSensor::begin() { port_.begin(9600, SERIAL_8N1, rx_, tx_); }

bool PzemSensor::read(uint8_t ch, Reading &out) {
  err_ = SENS_BAD;
  if (ch != 0) return false;
  // Read 10 input registers starting at 0x0000: 3 + 20 + 2 bytes back.
  uint8_t req[8] = {addr_, 0x04, 0x00, 0x00, 0x00, 0x0A};
  uint8_t resp[25];
  if (!transact(req, 6, resp, sizeof(resp))) return false;
  if (resp[2] != 20) {
    err_ = SENS_BAD;
    return false;
  }
  const uint8_t *d = resp + 3;
  out.v = u16(d) * 0.1f;
  out.a = u32(d + 2) * 0.001f;
  out.w = u32(d + 6) * 0.1f;
  out.e = u32(d + 10) / 1000.0;  // Wh -> kWh
  out.hz = u16(d + 14) * 0.1f;
  out.pf = u16(d + 16) * 0.01f;
  out.ok = true;
  out.atMs = millis();
  return true;
}

// ------------------------------------------------------------------ PZEM-017 (DC)

void Pzem017Sensor::begin() {
  if (de_ >= 0) {
    pinMode(de_, OUTPUT);
    digitalWrite(de_, LOW);
  }
  port_.begin(9600, SERIAL_8N2, rx_, tx_);
  if (!shuntA_) return;
  // Holding register 0x0003 = current range: 0 = 100 A, 1 = 50 A, 2 = 200 A, 3 = 300 A.
  uint16_t code = shuntA_ == 50 ? 1 : shuntA_ == 200 ? 2 : shuntA_ == 300 ? 3 : 0;
  uint8_t req[8] = {addr_, 0x06, 0x00, 0x03, (uint8_t)(code >> 8), (uint8_t)code};
  uint8_t resp[8];  // echo of the request
  bool ok = transact(req, 6, resp, sizeof(resp));
  Serial.printf("[sensor] PZEM-017 shunt %u A: %s\n", shuntA_, ok ? "set" : "no reply (DC supply off?), using its current setting");
}

bool Pzem017Sensor::read(uint8_t ch, Reading &out) {
  err_ = SENS_BAD;
  if (ch != 0) return false;
  // Read 8 input registers starting at 0x0000: 3 + 16 + 2 bytes back.
  uint8_t req[8] = {addr_, 0x04, 0x00, 0x00, 0x00, 0x08};
  uint8_t resp[21];
  if (!transact(req, 6, resp, sizeof(resp))) return false;
  if (resp[2] != 16) {
    err_ = SENS_BAD;
    return false;
  }
  const uint8_t *d = resp + 3;
  out.v = u16(d) * 0.01f;
  out.a = u16(d + 2) * 0.01f;
  out.w = u32(d + 4) * 0.1f;
  out.e = u32(d + 8) / 1000.0;  // Wh -> kWh
  out.hz = 0;                   // DC: no frequency / power factor
  out.pf = 0;
  out.ok = true;
  out.atMs = millis();
  return true;
}
