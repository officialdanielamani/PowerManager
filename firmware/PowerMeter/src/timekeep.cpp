#include "timekeep.h"

#include <sys/time.h>
#include <time.h>
#include "esp_sntp.h"
#include "../config.h"
#include "system.h"
#if HAS_DS3231
#include <Wire.h>
#endif

namespace timekeep {

static const uint32_t MIN_VALID = 1700000000UL;  // 2023-11
static volatile TimeSource src_ = TS_NONE;
static bool ntpStarted = false;

#if HAS_DS3231
static bool rtcOk = false;  // false in safe mode: the DS3231 is not used at all
static SemaphoreHandle_t i2cLock = nullptr;  // the RTC is read from the net and web tasks
static uint8_t bcd2dec(uint8_t v) { return (v >> 4) * 10 + (v & 0x0F); }
static uint8_t dec2bcd(uint8_t v) { return ((v / 10) << 4) | (v % 10); }

static uint32_t rtcReadRaw();
static uint32_t rtcRead() {
  if (i2cLock) xSemaphoreTake(i2cLock, portMAX_DELAY);
  uint32_t e = rtcReadRaw();
  if (i2cLock) xSemaphoreGive(i2cLock);
  return e;
}
static uint32_t rtcReadRaw() {
  Wire.beginTransmission(0x68);
  Wire.write(0);
  if (Wire.endTransmission() != 0) return 0;
  if (Wire.requestFrom(0x68, 7) != 7) return 0;
  uint8_t r[7];
  for (int i = 0; i < 7; i++) r[i] = Wire.read();
  struct tm t = {};
  t.tm_sec = bcd2dec(r[0] & 0x7F);
  t.tm_min = bcd2dec(r[1]);
  t.tm_hour = bcd2dec(r[2] & 0x3F);
  t.tm_mday = bcd2dec(r[4]);
  t.tm_mon = bcd2dec(r[5] & 0x1F) - 1;
  t.tm_year = bcd2dec(r[6]) + 100;
  return (uint32_t)mktime(&t);  // TZ is UTC on this device
}

static void rtcWrite(uint32_t epoch) {
  if (i2cLock) xSemaphoreTake(i2cLock, portMAX_DELAY);
  time_t e = epoch;
  struct tm t;
  gmtime_r(&e, &t);
  Wire.beginTransmission(0x68);
  Wire.write(0);
  Wire.write(dec2bcd(t.tm_sec));
  Wire.write(dec2bcd(t.tm_min));
  Wire.write(dec2bcd(t.tm_hour));
  Wire.write(dec2bcd(t.tm_wday + 1));
  Wire.write(dec2bcd(t.tm_mday));
  Wire.write(dec2bcd(t.tm_mon + 1));
  Wire.write(dec2bcd(t.tm_year % 100));
  Wire.endTransmission();
  if (i2cLock) xSemaphoreGive(i2cLock);
}
#endif

void begin() {
  setenv("TZ", "UTC0", 1);
  tzset();
#if HAS_DS3231
  rtcOk = !sys::safeMode;
  if (!rtcOk) return;
  i2cLock = xSemaphoreCreateMutex();
  Wire.begin(PIN_I2C_SDA, PIN_I2C_SCL);
  uint32_t e = rtcRead();
  if (e > MIN_VALID) set(e, TS_RTC);
#endif
}

void set(uint32_t epoch, TimeSource src) {
  if (epoch < MIN_VALID) return;
  if (src < src_ && valid()) return;  // never let a weaker source override a stronger one
  uint32_t cur = (uint32_t)time(nullptr);
  bool big = !valid() || (cur > epoch ? cur - epoch : epoch - cur) > 2;
  if (big) {
    struct timeval tv = {(time_t)epoch, 0};
    settimeofday(&tv, nullptr);
  }
  src_ = src;
#if HAS_DS3231
  if (rtcOk && big && src >= TS_NTP) rtcWrite(epoch);
#endif
}

#if HAS_DS3231
static volatile bool rtcWritePending = false;
#endif

// Runs in the lwIP task, which has a very small stack: never do I2C here (it can overflow that
// stack). Only note it; loop() (net task) writes the RTC.
static void onNtpSync(struct timeval *) {
  // SNTP already set the system clock. Server time stays the stronger source.
  if (src_ != TS_SERVER) src_ = TS_NTP;
#if HAS_DS3231
  rtcWritePending = true;
#endif
}

void loop() {
  if (!ntpStarted) {
    sntp_set_time_sync_notification_cb(onNtpSync);
    configTime(0, 0, "pool.ntp.org", "time.google.com");
    ntpStarted = true;
  }
#if HAS_DS3231
  if (rtcWritePending) {
    rtcWritePending = false;
    if (rtcOk) rtcWrite((uint32_t)time(nullptr));
  }
#endif
}

int64_t rtcNow() {
#if HAS_DS3231
  if (!rtcOk) return -1;
  uint32_t e = rtcRead();
  return e > MIN_VALID ? (int64_t)e : 0;
#else
  return -1;
#endif
}

bool valid() { return src_ != TS_NONE && (uint32_t)time(nullptr) > MIN_VALID; }
uint32_t now() { return (uint32_t)time(nullptr); }
TimeSource source() { return src_; }

const char *sourceName() {
  switch (src_) {
    case TS_SERVER: return "server";
    case TS_NTP: return "NTP";
    case TS_RTC: return "RTC";
    case TS_BROWSER: return "browser";
    default: return "not set";
  }
}

String format(uint32_t epoch) {
  time_t e = epoch;
  struct tm t;
  gmtime_r(&e, &t);
  char buf[24];
  strftime(buf, sizeof(buf), "%Y-%m-%d %H:%M:%S", &t);
  return String(buf);
}

}  // namespace timekeep
