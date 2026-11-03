// Shared device state, protected by one recursive mutex (use StateLock).
#pragma once
#include <Arduino.h>
#include "freertos/FreeRTOS.h"
#include "freertos/semphr.h"

enum SessMode : uint8_t { SESS_NONE = 0, SESS_DURATION = 1, SESS_DAILY = 2 };
enum SessStatus : uint8_t { SESS_ACTIVE = 0, SESS_QUOTA = 1, SESS_EXPIRED = 2 };
enum NetStatus : uint8_t {
  NET_SETUP_AP = 0,      // no Wi-Fi configured
  NET_WIFI_CONNECTING,   // Wi-Fi configured, not connected
  NET_NOT_PAIRED,        // Wi-Fi OK, no server credentials (or rejected)
  NET_CONNECTING,        // connecting to server
  NET_ONLINE             // WebSocket up and hello done
};

struct MeterConfig {
  uint32_t ver = 0;
  uint16_t report_s = 60;
  uint16_t offline_off_min = 0;  // 0 = disabled
  SessMode sess_mode = SESS_NONE;
  uint16_t daily_utc_min = 0;
  uint32_t dur_min = 1440;
  bool auto_renew = false;
  float kwh_max = 0;  // channel 0; 0 = unlimited
  float amp_max = 0;  // channel 0; 0 = disabled
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
  bool tamper = false;
  bool ac = true;
  bool sensorEverOk = false;
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
