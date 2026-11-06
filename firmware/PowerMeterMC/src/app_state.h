// Shared device state of the multi-channel meter, protected by one recursive mutex (use StateLock).
#pragma once
#include <Arduino.h>
#include "freertos/FreeRTOS.h"
#include "freertos/semphr.h"
#include "../config.h"

enum KwhAction : uint8_t { KWH_END = 0, KWH_HOLD = 1, KWH_WARN = 2 };
enum SessMode : uint8_t { SESS_NONE = 0, SESS_DURATION = 1, SESS_DAILY = 2 };
// ENDED = ended early (protection, End command, mode change, disabled): relay off until renewed.
enum SessStatus : uint8_t { SESS_ACTIVE = 0, SESS_QUOTA = 1, SESS_EXPIRED = 2, SESS_ENDED = 3 };
// Voltage / tamper / sensor protection: run as usual, relay off while the problem lasts, or end the session.
enum ProtectAction : uint8_t { ACT_RUN = 0, ACT_HOLD = 1, ACT_END = 2 };
enum StopReason : uint8_t {
  STOP_NONE = 0, STOP_VOLTAGE = 1, STOP_TAMPER = 2, STOP_DISABLED = 3, STOP_SENSOR = 4,
  STOP_MANUAL = 5,  // End / End all
  STOP_MODE = 6     // the configuration mode changed (Shared <-> Independent)
};
// Sensor problem: no reply on I2C, or a reply that is not valid (module error / no valid result).
enum SensorErr : uint8_t { SENS_OK = 0, SENS_NO_REPLY = 1, SENS_BAD = 2 };
enum MeterMode : uint8_t { MODE_SHARED = 0, MODE_INDEPENDENT = 1 };
enum NetStatus : uint8_t {
  NET_SETUP_AP = 0,      // no Wi-Fi configured
  NET_WIFI_CONNECTING,   // Wi-Fi configured, not connected
  NET_NOT_PAIRED,        // Wi-Fi OK, no server credentials (or rejected)
  NET_CONNECTING,        // connecting to server
  NET_ONLINE             // WebSocket up and hello done
};

struct SessConfig {
  SessMode mode = SESS_NONE;
  uint16_t daily_utc_min = 0;
  uint32_t dur_min = 1440;
  bool auto_renew = false;
};

struct ChConfig {
  char name[21] = "";
  float amp_max = 0;      // 0 = disabled (always per channel)
  float kwh_max = 0;      // independent mode; 0 = unlimited
  uint8_t kwh_action = KWH_HOLD;
  SessConfig sess;        // independent mode
};

struct MeterConfig {
  uint32_t ver = 0;
  uint16_t report_s = 60;  // send to the server every ... s
  uint16_t read_s = 60;    // record a reading every ... s (<= report_s)
  bool rdy = false;        // read the module on every DRDY pulse (100 ms windows)
  uint16_t offline_off_min = 0;  // 0 = disabled
  uint8_t mode = MODE_SHARED;
  SessConfig sess;         // shared mode
  float kwh_max = 0;       // shared mode: total of all channels; 0 = unlimited
  uint8_t kwh_action = KWH_HOLD;
  float volt_min = 0;      // operating voltage range; 0 = no limit on that side
  float volt_max = 0;
  uint8_t volt_action = ACT_HOLD;
  bool tamper_protect = false;
  uint8_t tamper_action = ACT_HOLD;
  uint16_t sensor_to_s = 10;  // no valid reading for this long = sensor error; 0 = off
  uint8_t sensor_action = ACT_HOLD;
  ChConfig ch[MC_MAX_CH];
};

struct Reading {  // one channel
  float a = 0, w = 0, pf = 0;
};

struct SessionState {  // one session slot: [0] in shared mode, [ch] in independent mode
  uint32_t sid = 0;       // 0 = no session in this slot yet
  uint32_t start = 0;     // epoch; 0 = started while time was unknown
  uint32_t startMs = 0;   // millis() at start (used while start == 0)
  double e0 = 0;          // energy counter(s) at session start
  SessStatus status = SESS_ACTIVE;
  bool over = false;      // kWh limit passed with KWH_WARN (warned once, power stays on)
  bool ended = false;     // session_end already sent
  uint8_t stopReason = STOP_NONE;  // why status is SESS_ENDED
};

struct ChState {
  Reading r;
  double e = 0;                // this channel's kWh counter (kept by the meter, saved in NVS)
  bool wantOn = true;          // user's wanted relay state
  bool relayOn = false;        // actual output
  bool tripped = false;        // over-current latch
  const char *block = nullptr; // why the relay is off
  uint8_t overReads = 0;
};

struct AppState {
  MeterConfig cfg;
  // sensor (filled by control from the rbAmp module)
  uint8_t nch = 0;             // channels of the module (0 until it is found)
  bool hasVoltage = true;      // UI* variants; I* variants have no voltage input
  char variant[6] = "?";
  float v = 0;                 // voltage (shared by all channels)
  uint8_t hz = 0;
  bool readOk = false;         // the latest read was valid
  uint32_t readAtMs = 0;
  ChState ch[MC_MAX_CH];
  SessionState sess[MC_MAX_CH];
  uint32_t sidCounter = 0;     // session numbers, unique across channels
  bool everStarted = false;    // first session(s) after pairing start on their own
  bool disabled = false;       // disabled by the server: relays off, no session (persisted)
  bool tamper = false;
  bool ac = true;
  bool sensorEverOk = false;
  bool voltOut = false;        // voltage outside the operating range (debounced)
  uint8_t sensorErr = SENS_OK; // set after cfg.sensor_to_s without a valid reading
  uint8_t lastReadErr = SENS_OK;
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

// Session slots in use: 1 in shared mode, one per channel in independent mode.
inline uint8_t sessSlots() { return g.cfg.mode == MODE_INDEPENDENT ? (g.nch ? g.nch : 1) : 1; }
// The session slot that governs channel `ch`.
inline uint8_t slotOf(uint8_t ch) { return g.cfg.mode == MODE_INDEPENDENT ? ch : 0; }
inline const SessConfig &slotSessCfg(uint8_t s) { return g.cfg.mode == MODE_INDEPENDENT ? g.cfg.ch[s].sess : g.cfg.sess; }
inline float slotKwhMax(uint8_t s) { return g.cfg.mode == MODE_INDEPENDENT ? g.cfg.ch[s].kwh_max : g.cfg.kwh_max; }
inline uint8_t slotKwhAction(uint8_t s) { return g.cfg.mode == MODE_INDEPENDENT ? g.cfg.ch[s].kwh_action : g.cfg.kwh_action; }
