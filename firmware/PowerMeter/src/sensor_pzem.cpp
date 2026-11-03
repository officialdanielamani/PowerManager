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

void PzemSensor::begin() { port_.begin(9600, SERIAL_8N1, rx_, tx_); }

static uint32_t u32(const uint8_t *p) {
  // PZEM sends 32-bit values as low word first, each word big-endian.
  return ((uint32_t)p[0] << 8 | p[1]) | ((uint32_t)p[2] << 24 | (uint32_t)p[3] << 16);
}

bool PzemSensor::read(uint8_t ch, Reading &out) {
  if (ch != 0) return false;
  // Read 10 input registers starting at 0x0000.
  uint8_t req[8] = {addr_, 0x04, 0x00, 0x00, 0x00, 0x0A, 0, 0};
  uint16_t crc = modbusCrc(req, 6);
  req[6] = crc & 0xFF;
  req[7] = crc >> 8;
  while (port_.available()) port_.read();  // drop stale bytes
  port_.write(req, sizeof(req));
  port_.flush();

  uint8_t resp[25];
  size_t got = 0;
  uint32_t deadline = millis() + PZEM_TIMEOUT_MS;
  while (got < sizeof(resp) && (int32_t)(deadline - millis()) > 0) {
    if (port_.available()) {
      resp[got++] = port_.read();
    } else {
      vTaskDelay(pdMS_TO_TICKS(2));  // yield, never busy-wait
    }
  }
  if (got != sizeof(resp) || resp[1] != 0x04 || resp[2] != 20) return false;
  uint16_t rc = modbusCrc(resp, 23);
  if (resp[23] != (rc & 0xFF) || resp[24] != (rc >> 8)) return false;

  const uint8_t *d = resp + 3;
  out.v = ((uint16_t)d[0] << 8 | d[1]) * 0.1f;
  out.a = u32(d + 2) * 0.001f;
  out.w = u32(d + 6) * 0.1f;
  out.e = u32(d + 10) / 1000.0;  // Wh -> kWh
  out.hz = ((uint16_t)d[14] << 8 | d[15]) * 0.1f;
  out.pf = ((uint16_t)d[16] << 8 | d[17]) * 0.01f;
  out.ok = true;
  out.atMs = millis();
  return true;
}
