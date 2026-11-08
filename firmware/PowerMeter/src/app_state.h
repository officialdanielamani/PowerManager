// Shared device state, protected by one recursive mutex (use StateLock).
#pragma once
#include <Arduino.h>
#include "freertos/FreeRTOS.h"
#include "freertos/semphr.h"

enum KwhAction : uint8_t { KWH_END = 0, KWH_HOLD = 1, KWH_WARN = 2 };
enum SessMode : uint8_t { SESS_NONE = 0, SESS_DURATION = 1, SESS_DAILY = 2 };
// ENDED = ended early by the voltage or tamper protection (relay off until renewed).
enum SessStatus : uint8_t { SESS_ACTIVE = 0, SESS_QUOTA = 1, SESS_EXPIRED = 2, SESS_ENDED = 3 };
// Voltage / tamper protection: run as usual, relay off while the problem lasts, or end the session.
enum ProtectAction : uint8_t { ACT_RUN = 0, ACT_HOLD = 1, ACT_END = 2 };
enum StopReason : uint8_t { STOP_NONE = 0, STOP_VOLTAGE = 1, STOP_TAMPER = 2, STOP_DISABLED = 3, STOP_SENSOR = 4, STOP_MANUAL = 5,
                            STOP_TEMP = 6 };
// Sensor problem: no reply at all (sensor, wiring, or no AC: the PZEM is powered from the AC side) or
// replies that are garbled / fail the CRC (wiring, noise, wrong address).
enum SensorErr : uint8_t { SENS_OK = 0, SENS_NO_REPLY = 1, SENS_BAD = 2 };
enum NetStatus : uint8_t {
  NET_SETUP_AP = 0,      // no Wi-Fi configured
  NET_WIFI_CONNECTING,   // Wi-Fi configured, not connected
  NET_NOT_PAIRED,        // Wi-Fi OK, no server credentials (or rejected)
  NET_CONNECTING,        // connecting to server
  NET_ONLINE             // WebSocket up and hello done
};

struct MeterConfig {
  uint32_t ver = 0;
  uint16_t report_s = 60;  // send to the server every ... s
  uint16_t read_s = 60;    // record a reading every ... s (<= report_s; sent in batches)
  uint16_t offline_off_min = 0;  // 0 = disabled
  SessMode sess_mode = SESS_NONE;
  uint16_t daily_utc_min = 0;
  uint32_t dur_min = 1440;
  bool auto_renew = false;
  float kwh_max = 0;  // channel 0; 0 = unlimited
  // What happens when kwh_max is reached:
  //   KWH_END  relay off and the session ends now (even if time is left)       [default]
  //   KWH_HOLD relay off, the session ends when its time limit is reached (or on Renew)
  //   KWH_WARN warning only, relay stays on
  uint8_t kwh_action = 0;
  float amp_max = 0;  // channel 0; 0 = disabled
  float volt_min = 0;  // operating voltage range; 0 = no limit on that side
  float volt_max = 0;
  uint8_t volt_action = ACT_HOLD;
  float tare_a = 0;    // own consumption on the measured line (ESP32 + SSR)
  float tare_w = 0;
  bool tare_limits = true;  // use tared values for the kWh limit and max current
  bool tamper_protect = false;
  uint8_t tamper_action = ACT_HOLD;
  uint16_t sensor_to_s = 10;  // no valid reading for this long = sensor error; 0 = off
  uint8_t sensor_action = ACT_HOLD;
  float temp_max = 0;         // over-temperature: any sensor above this (degC); 0 = off
  uint8_t temp_action = ACT_HOLD;
};

struct Reading {
  bool ok = false;
  float v = 0, a = 0, w = 0, pf = 0, hz = 0;
  double e = 0;  // kWh, PZEM energy register
  uint32_t atMs = 0;
};

struct SessionState {
  uint32_t sid = 0;       // 0 = no session yet
  uint32_t start = 0;     // epoch; 0 = started while time was unknown
  uint32_t startMs = 0;   // millis() at start (used while start == 0)
  double e0 = 0;          // energy register at session start
  SessStatus status = SESS_ACTIVE;
  bool over = false;      // kWh limit passed with KWH_WARN (warned once, power stays on)
  bool ended = false;     // session_end already sent (kWh limit with KWH_END, or time limit)
  uint8_t stopReason = STOP_NONE;  // why status is SESS_ENDED
  double tareKwh = 0;     // own consumption counted this session (tare W x time with readings)
};

struct AppState {
  MeterConfig cfg;
  Reading reading;
  double lastE = 0;            // last known energy (kept while AC is lost)
  SessionState sess;
  bool wantOn = true;          // user's wanted relay state
  bool relayOn = false;        // actual output
  const char *block = nullptr; // why relay is off: tripped/quota/expired/offline or null
  bool tripped = false;
  bool disabled = false;       // disabled by the server: relay off, no session (persisted)
  bool tamper = false;
  bool ac = true;
  bool sensorEverOk = false;
  bool voltOut = false;        // voltage outside the operating range (debounced)
  bool tempHigh = false;       // a temperature sensor is above cfg.temp_max (until TEMP_HYST below)
  uint8_t sensorErr = SENS_OK; // set after cfg.sensor_to_s without a valid reading
  uint8_t lastReadErr = SENS_OK; // result of the latest read (for telemetry)
  NetStatus net = NET_SETUP_AP;
  bool paired = false;
  bool unauthorized = false;   // server rejected our credentials
  uint32_t offlineSinceMs = 0; // 0 = online (or never paired)
  uint32_t lastAckMs = 0;
  uint32_t lastSyncEpoch = 0;
  bool apActive = false;
  bool factoryResetPending = false;
};

extern AppState g;
extern SemaphoreHandle_t gLock;

void state_init();

struct StateLock {
  StateLock() { xSemaphoreTakeRecursive(gLock, portMAX_DELAY); }
  ~StateLock() { xSemaphoreGiveRecursive(gLock); }
};

const char *sessStatusName(SessStatus s);

// Config values are 32-bit floats: 0.05 would be sent as 0.050000001. Round to 4 decimals for JSON.
inline double num4(float f) { return round((double)f * 10000.0) / 10000.0; }
