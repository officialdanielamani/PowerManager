#include "control.h"

#include <atomic>
#include <driver/gpio.h>

#include "../config.h"
#include "outbox.h"
#include "storage.h"
#include "system.h"
#include "auxrelay.h"
#include "hwmap.h"
#include "relays.h"
#include "temp.h"
#include "timekeep.h"

namespace control {

static Sensor *sensor_ = nullptr;
static std::atomic<uint32_t> stateVer_{1};
static std::atomic<bool> reportNow_{false};
static std::atomic<bool> sendReq_{false};  // time to send (report_s passed, event, Get Data Now)
static uint32_t lastSendReqMs_ = 0;
static uint8_t failReads_ = 0;
static uint8_t overReads_ = 0;
static uint32_t lastReportMs_ = 0;
static bool offlineEventSent_ = false;

static void bump() { stateVer_++; }
static const char *SENS_NAME[] = {"ok", "no_reply", "bad"};
static bool hasTare() { return g.cfg.tare_a > 0 || g.cfg.tare_w > 0; }
uint32_t stateVersion() { return stateVer_; }

// ------------------------------------------------------------------ outbox helpers

static void stampTime(JsonDocument &doc) {
  if (timekeep::valid()) {
    doc["ts"] = timekeep::now();
    doc["tv"] = 1;
  } else {
    doc["ts"] = millis() / 1000;  // uptime; server converts using hello.up
    doc["tv"] = 0;
    doc["boot"] = store::bootCount();
  }
}

void emitEvent(const char *e, void (*fill)(JsonObject, void *), void *ctx) {
  JsonDocument doc;
  uint32_t seq = store::nextSeq();
  doc["t"] = "evt";
  doc["seq"] = seq;
  stampTime(doc);
  doc["e"] = e;
  if (fill) fill(doc.as<JsonObject>(), ctx);
  String s;
  serializeJson(doc, s);
  outbox::push(outbox::EVT, seq, s);
  sendReq_ = true;  // events go out right away (with any readings queued before them)
  Serial.printf("[event] %s\n", s.c_str());
}

// Temperatures ("t": [T1, T2 ...] degC, null = not readable) with the first reading after each new round.
static bool tempsPending_ = false;
static void addTemps(JsonDocument &doc) {
  // caller holds StateLock
  if (!tempsPending_ || !temp::count()) return;
  tempsPending_ = false;
  JsonArray t = doc["t"].to<JsonArray>();
  for (uint8_t i = 0; i < temp::count(); i++) {
    float c = temp::value(i);
    if (isnan(c)) t.add(nullptr);
    else t.add(serialized(String(c, 1)));
  }
  if (!temp::hasHumidity()) return;
  JsonArray rh = doc["rh"].to<JsonArray>();  // humidity %RH of the same sensors (SHT only; null = none)
  for (uint8_t i = 0; i < temp::count(); i++) {
    float h = temp::humidity(i);
    if (isnan(h)) rh.add(nullptr);
    else rh.add(serialized(String(h, 0)));
  }
}

static void pushTelemetry() {
  JsonDocument doc;
  uint32_t seq = store::nextSeq();
  doc["seq"] = seq;
  stampTime(doc);
  JsonArray ch = doc["ch"].to<JsonArray>();
  double skwh = sessionKwh();
  {
    StateLock l;
    // Session progress with every reading, so the server's "session used" stays current
    // (the "state" message is only sent when something changes).
    JsonObject se = doc["s"].to<JsonObject>();
    se["sid"] = g.sess.sid;
    se["kwh"] = serialized(String(skwh, 4));
    if (hasTare()) se["kwh_t"] = serialized(String(sessionKwhTared(), 4));
    if (!g.reading.ok) doc["se"] = SENS_NAME[(g.lastReadErr ? g.lastReadErr : (uint8_t)SENS_NO_REPLY) % 3];
    if (g.reading.ok && g.ac) {
      JsonObject c = ch.add<JsonObject>();
      c["i"] = 0;
#if METER_DC
      c["v"] = serialized(String(g.reading.v, 2));  // PZEM-017: 0.01 V / 0.01 A, no PF / Hz
      c["a"] = serialized(String(g.reading.a, 2));
      c["w"] = serialized(String(g.reading.w, 1));
#else
      c["v"] = serialized(String(g.reading.v, 1));
      c["a"] = serialized(String(g.reading.a, 3));
      c["w"] = serialized(String(g.reading.w, 1));
      c["pf"] = serialized(String(g.reading.pf, 2));
      c["hz"] = serialized(String(g.reading.hz, 1));
#endif
      c["e"] = serialized(String(g.reading.e, 3));
    }
    addTemps(doc);
  }
  String s;
  serializeJson(doc, s);
  outbox::push(outbox::TEL, seq, s);
}

// ------------------------------------------------------------------ session

double sessionKwh() {
  StateLock l;
  double kwh = g.lastE - g.sess.e0;  // raw, as measured (never call the tared/limit helpers here)
  return kwh > 0 ? kwh : 0;
}

double sessionKwhTared() {
  StateLock l;
  double kwh = sessionKwh() - g.sess.tareKwh;
  return kwh > 0 ? kwh : 0;
}

double sessionKwhLimit() {
  StateLock l;
  return g.cfg.tare_limits ? sessionKwhTared() : sessionKwh();
}

uint32_t nextReset() {
  StateLock l;
  const MeterConfig &c = g.cfg;
  if (c.sess_mode == SESS_DURATION) {
    if (g.sess.start) return g.sess.start + c.dur_min * 60;
    if (timekeep::valid())  // started while time was unknown: estimate from millis
      return timekeep::now() - (millis() - g.sess.startMs) / 1000 + c.dur_min * 60;
    return 0;
  }
  if (c.sess_mode == SESS_DAILY && timekeep::valid()) {
    uint32_t start = g.sess.start ? g.sess.start : timekeep::now() - (millis() - g.sess.startMs) / 1000;
    uint32_t day = start - start % 86400;
    uint32_t t = day + c.daily_utc_min * 60;
    if (t <= start) t += 86400;
    return t;
  }
  return 0;
}

static bool durationElapsedNoClock() {
  return g.cfg.sess_mode == SESS_DURATION && !g.sess.start && !timekeep::valid() &&
         (millis() - g.sess.startMs) / 60000UL >= g.cfg.dur_min;
}

struct EndCtx {
  uint32_t sid;
  const char *reason;
  double kwh;
  double kwhT;
  bool tared;
};

static void startSession(bool autoRenew) {
  // caller holds StateLock
  g.sess.sid += 1;
  g.sess.start = timekeep::valid() ? timekeep::now() : 0;
  g.sess.startMs = millis();
  g.sess.tareKwh = 0;
  g.sess.e0 = g.lastE;
  g.sess.status = SESS_ACTIVE;
  g.sess.over = false;
  g.sess.ended = false;
  g.sess.stopReason = STOP_NONE;
  store::saveSession(g.sess);
  struct Ctx {
    uint32_t sid;
    bool autoRenew;
    float max;
  } ctx = {g.sess.sid, autoRenew, g.cfg.kwh_max};
  emitEvent("session_start", [](JsonObject o, void *p) {
    Ctx *c = (Ctx *)p;
    o["sid"] = c->sid;
    o["auto"] = c->autoRenew;
    o["max"] = num4(c->max);
  }, &ctx);
  bump();
}

static void endSession(const char *reason) {
  // caller holds StateLock. Sends session_end once per session.
  if (g.sess.ended) return;
  g.sess.ended = true;
  EndCtx ctx = {g.sess.sid, reason, g.lastE - g.sess.e0, sessionKwhTared(), hasTare()};
  emitEvent("session_end", [](JsonObject o, void *p) {
    EndCtx *c = (EndCtx *)p;
    o["sid"] = c->sid;
    o["reason"] = c->reason;
    o["kwh"] = serialized(String(c->kwh > 0 ? c->kwh : 0, 4));
    if (c->tared) o["kwh_t"] = serialized(String(c->kwhT, 4));
  }, &ctx);
}

void renew(const char *reason, bool autoRenew) {
  StateLock l;
  if (g.disabled) return;  // a disabled meter has no sessions
  if (g.sess.sid) endSession(reason);  // no-op if a limit already ended it
  startSession(autoRenew);
}

static const char *stopReasonName(uint8_t why) {
  return why == STOP_VOLTAGE ? "voltage" : why == STOP_TAMPER ? "tamper" : why == STOP_SENSOR ? "sensor"
       : why == STOP_MANUAL ? "stopped" : why == STOP_TEMP ? "temp" : "disabled";
}

// Voltage / tamper / sensor protection with action "end": end the session now (relay off until renewed).
static void stopSession(StopReason why) {
  // caller holds StateLock
  if (!g.sess.sid || g.sess.status == SESS_ENDED || g.sess.status == SESS_EXPIRED) return;
  endSession(stopReasonName(why));
  g.sess.status = SESS_ENDED;
  g.sess.stopReason = why;
  store::saveSession(g.sess);
  bump();
}

static const char *ACTION_NAME[] = {"run", "hold", "end"};

// End session (server): ends now, relay off until Renew.
void endSession() {
  StateLock l;
  stopSession(STOP_MANUAL);
}

void setDisabled(bool disabled) {
  StateLock l;
  if (g.disabled == disabled) return;
  g.disabled = disabled;
  store::saveDisabled(disabled);
  if (disabled) stopSession(STOP_DISABLED);  // session ends, relay off below ("disabled" block)
  // Enabled again: no new session. The ended session keeps the relay off until Renew.
  bump();
}

// ------------------------------------------------------------------ commands

bool setRelay(bool on, const char *src, const char *&err) {
  StateLock l;
  if (on) {
    if (g.disabled) err = "disabled";
    else if (g.tripped) err = "tripped";
    else if (!g.sess.sid) err = "no_session";
    else if (g.sess.status == SESS_QUOTA) err = "quota_exceeded";
    else if (g.sess.status == SESS_EXPIRED || g.sess.status == SESS_ENDED) err = "session_expired";
    else err = nullptr;
    if (err) return false;
  }
  err = nullptr;
  if (g.wantOn != on) {
    g.wantOn = on;
    store::saveFlags(g.wantOn, g.tripped);
    struct Ctx {
      bool on;
      const char *src;
    } ctx = {on, src};
    emitEvent("relay", [](JsonObject o, void *p) {
      Ctx *c = (Ctx *)p;
      o["on"] = c->on;
      o["src"] = c->src;
    }, &ctx);
    bump();
  }
  return true;
}

bool resetTrip(const char *src, const char *by) {
  StateLock l;
  if (!g.tripped) return false;
  g.tripped = false;
  overReads_ = 0;
  store::saveFlags(g.wantOn, g.tripped);
  struct Ctx {
    const char *src, *by;
  } ctx = {src, by};
  emitEvent("trip_reset", [](JsonObject o, void *p) {
    Ctx *c = (Ctx *)p;
    o["src"] = c->src;
    if (c->by && *c->by) o["by"] = c->by;
  }, &ctx);
  bump();
  return true;
}

void applyConfig(const MeterConfig &c) {
  auxrelay::commit();  // AUX names + automation (staged by net parseConfig)
  StateLock l;
  g.cfg = c;
  store::saveConfig(c);
  // A session that already ended (kWh or time limit) stays ended: use Renew to start a new one.
  bump();
}

void reportNow() { reportNow_ = true; }
bool takeSendRequest() { return sendReq_.exchange(false); }

// ------------------------------------------------------------------ state

void stateJson(JsonObject o) {
  uint32_t nr = nextReset();
  double kwh = sessionKwh();
  StateLock l;
  JsonArray relay = o["relay"].to<JsonArray>();
  JsonObject r = relay.add<JsonObject>();
  r["i"] = 0;
  r["on"] = g.relayOn;
  r["want"] = g.wantOn;
  if (g.block) r["block"] = g.block;
  else r["block"] = nullptr;
  o["tripped"] = g.tripped;
  if (auxrelay::count()) auxrelay::stateJson(o["aux"].to<JsonArray>());
  o["tamper"] = g.tamper;
  o["ac"] = g.ac;
  JsonObject s = o["sess"].to<JsonObject>();
  s["sid"] = g.sess.sid;
  if (g.sess.start) s["start"] = g.sess.start;
  s["kwh"] = serialized(String(kwh, 4));
  if (hasTare()) s["kwh_t"] = serialized(String(sessionKwhTared(), 4));
  s["max"] = num4(g.cfg.kwh_max);
  s["status"] = sessStatusName(g.sess.status);
  if (g.sess.over) s["over"] = true;
  s["ended"] = g.sess.ended;
  if (g.sess.status == SESS_ENDED)
    s["end_reason"] = stopReasonName(g.sess.stopReason);
  if (g.disabled) o["disabled"] = true;
  if (g.voltOut) o["volt_out"] = true;
  if (g.tempHigh) o["temp_high"] = true;
  if (g.sensorErr) o["sensor_err"] = SENS_NAME[g.sensorErr % 3];
  if (nr) s["next_reset"] = nr;
}

// ------------------------------------------------------------------ main loop

void begin(Sensor *sensor) {
  sensor_ = sensor;
  sensor_->begin();
  relays::begin();  // every relay OFF (GPIO pins at full drive strength for SSR inputs)
  temp::begin();
#if TAMPER_ENABLE
  pinMode(PIN_TAMPER, INPUT_PULLUP);
#endif
  StateLock l;
#if TAMPER_ENABLE
  g.tamper = digitalRead(PIN_TAMPER) == TAMPER_OPEN_LEVEL;
#endif
  g.lastE = g.sess.e0;  // until the first reading arrives
}

// Operating voltage range. Out = VOLT_OUT_READS bad readings in a row; back = VOLT_OK_READS good
// ones. "No AC" (below SUPPLY_LOST_VOLTAGE / no reply) is not a range problem: it keeps the state.
static void checkVoltage(bool ok, float v) {
  // caller holds StateLock
  static uint8_t bad = 0, good = 0;
  bool enabled = g.cfg.volt_min > 0 || g.cfg.volt_max > 0;
  if (!enabled) {
    bad = good = 0;
    if (g.voltOut) {
      g.voltOut = false;
      emitEvent("voltage_ok", [](JsonObject o, void *) { o["disabled"] = true; });
      bump();
    }
    return;
  }
  if (!ok || v < SUPPLY_LOST_VOLTAGE) return;
  bool out = (g.cfg.volt_min > 0 && v < g.cfg.volt_min) || (g.cfg.volt_max > 0 && v > g.cfg.volt_max);
  struct Ctx {
    float v, min, max;
    const char *dir, *action;
  } ctx = {v, g.cfg.volt_min, g.cfg.volt_max, (g.cfg.volt_min > 0 && v < g.cfg.volt_min) ? "low" : "high",
           ACTION_NAME[g.cfg.volt_action % 3]};
  if (out) {
    good = 0;
    if (!g.voltOut && ++bad >= VOLT_OUT_READS) {
      g.voltOut = true;
      emitEvent("voltage_out", [](JsonObject o, void *p) {
        Ctx *c = (Ctx *)p;
        o["v"] = serialized(String(c->v, 1));
        o["min"] = num4(c->min);
        o["max"] = num4(c->max);
        o["dir"] = c->dir;
        o["action"] = c->action;
      }, &ctx);
      if (g.cfg.volt_action == ACT_END) stopSession(STOP_VOLTAGE);
      bump();
    }
  } else {
    bad = 0;
    if (g.voltOut && ++good >= VOLT_OK_READS) {
      g.voltOut = false;
      emitEvent("voltage_ok", [](JsonObject o, void *p) { o["v"] = serialized(String(((Ctx *)p)->v, 1)); }, &ctx);
      bump();
    }
  }
}

// Sensor timeout: no valid reading for cfg.sensor_to_s seconds = sensor error (once), with the
// configured action; the first valid reading clears it.
static void checkSensor(bool ok, uint8_t err) {
  // caller holds StateLock
  static uint32_t lastGoodMs = 0;
  uint32_t now = millis();
  if (!lastGoodMs) lastGoodMs = now;  // boot: the timeout counts from start-up
  if (ok) {
    lastGoodMs = now;
    if (g.sensorErr) {
      g.sensorErr = SENS_OK;
      emitEvent("sensor_ok");
      bump();
    }
    return;
  }
  if (g.cfg.sensor_to_s == 0 || g.sensorErr || now - lastGoodMs < g.cfg.sensor_to_s * 1000UL) return;
  g.sensorErr = err ? err : (uint8_t)SENS_NO_REPLY;
  struct Ctx {
    const char *kind, *action;
    uint16_t after;
  } ctx = {SENS_NAME[g.sensorErr % 3], ACTION_NAME[g.cfg.sensor_action % 3], g.cfg.sensor_to_s};
  emitEvent("sensor_error", [](JsonObject o, void *p) {
    Ctx *c = (Ctx *)p;
    o["kind"] = c->kind;
    o["after_s"] = c->after;
    o["action"] = c->action;
  }, &ctx);
  if (g.cfg.sensor_action == ACT_END) stopSession(STOP_SENSOR);
  bump();
}

static void readSensor() {
  Reading r;
  bool ok = sensor_->read(0, r);
  uint8_t err = sensor_->lastError();
  StateLock l;
  g.lastReadErr = ok ? (uint8_t)SENS_OK : (err ? err : (uint8_t)SENS_NO_REPLY);
  checkSensor(ok, g.lastReadErr);
  if (ok) {
    g.reading = r;
    g.lastE = r.e;
    // Own consumption: tare W over the time with readings (no AC = PZEM measures nothing).
    static uint32_t lastOkMs = 0;
    static uint32_t lastTareSaveMs = 0;
    uint32_t nowMs = millis();
    if (lastOkMs && g.sess.sid && g.cfg.tare_w > 0) {
      uint32_t dt = nowMs - lastOkMs;
      if (dt > 5000) dt = 5000;  // gap in readings: do not count it
      g.sess.tareKwh += (double)g.cfg.tare_w * dt / 3.6e9;
      if (nowMs - lastTareSaveMs > 600000UL) {  // keep it across reboots (every 10 min)
        lastTareSaveMs = nowMs;
        store::saveSession(g.sess);
      }
    }
    lastOkMs = nowMs;
    g.sensorEverOk = true;
    failReads_ = 0;
    // PZEM energy counter was reset or replaced: re-base the session.
    if (g.sess.sid && r.e + 0.001 < g.sess.e0) {
      Serial.println("[control] energy register went backwards, re-basing session");
      g.sess.e0 = r.e;
      store::saveSession(g.sess);
    }
  } else {
    g.reading.ok = false;
    if (failReads_ < 255) failReads_++;
  }
  bool acNow = ok ? r.v >= SUPPLY_LOST_VOLTAGE : (failReads_ < 3 && g.ac);
  if (acNow != g.ac) {
    g.ac = acNow;
    emitEvent(acNow ? "ac_restored" : "ac_lost");
    bump();
  }
  checkVoltage(ok, r.v);
}

// Counts relay changes; warns once (serial + "relay_chatter" event) when it switches too often.
static uint32_t relaySwitches_ = 0;
uint32_t relaySwitches() { return relaySwitches_; }
static void noteRelaySwitch(const char *block) {
  // caller holds StateLock
  static uint32_t windowStart = 0, inWindow = 0, lastWarnMs = 0;
  relaySwitches_++;
  uint32_t now = millis();
  if (now - windowStart > 60000) {
    windowStart = now;
    inWindow = 0;
  }
  if (++inWindow > RELAY_CHATTER_SWITCHES && (!lastWarnMs || now - lastWarnMs > 600000)) {
    lastWarnMs = now;
    Serial.printf("[relay] WARNING: %lu changes within a minute (last reason: %s)\n", (unsigned long)inWindow,
                  block ? block : "user");
    struct Ctx {
      uint32_t n;
      const char *why;
    } ctx = {inWindow, block ? block : "user"};
    emitEvent("relay_chatter", [](JsonObject o, void *p) {
      o["n"] = ((Ctx *)p)->n;
      o["reason"] = ((Ctx *)p)->why;
    }, &ctx);
  }
}

static std::atomic<bool> relayTest_{false}, relayForce_{false};
void relayTest(bool on) {
  relayTest_ = on;
  if (!on) relayForce_ = true;  // put back what the rules want right away
}

static void writeRelays(bool force);

static void checkRules(bool firstPass) {
  StateLock l;
  // No session starts on its own after pairing: the admin starts the first one with Renew.

  // Over-current (latched until reset).
  float ampNow = g.reading.a - (g.cfg.tare_limits ? g.cfg.tare_a : 0.0f);
  if (!g.tripped && g.cfg.amp_max > 0 && g.reading.ok && ampNow > g.cfg.amp_max) {
    if (++overReads_ >= TRIP_CONFIRM_READS) {
      g.tripped = true;
      store::saveFlags(g.wantOn, g.tripped);
      struct Ctx {
        float a, limit;
      } ctx = {ampNow, g.cfg.amp_max};  // tared current when tare_limits is on
      emitEvent("overcurrent", [](JsonObject o, void *p) {
        Ctx *c = (Ctx *)p;
        o["ch"] = 0;
        o["val"] = serialized(String(c->a, 2));
        o["limit"] = num4(c->limit);
      }, &ctx);
      bump();
    }
  } else if (!g.tripped) {
    overReads_ = 0;
  }

  // kWh quota.
  double kwh = sessionKwhLimit();  // tared when "tare_limits" is on
  if (g.sess.sid && g.sess.status == SESS_ACTIVE && g.cfg.kwh_max > 0 && kwh >= g.cfg.kwh_max) {
    static const char *ACT[] = {"end", "hold", "warn"};
    struct QCtx {
      double kwh;
      const char *action;
    } q = {kwh, ACT[g.cfg.kwh_action % 3]};
    auto fillQuota = [](JsonObject o, void *p) {
      QCtx *c = (QCtx *)p;
      o["ch"] = 0;
      o["kwh"] = serialized(String(c->kwh, 4));
      o["action"] = c->action;
      o["end"] = strcmp(c->action, "warn") != 0;  // for server v0.2.x
    };
    if (g.cfg.kwh_action == KWH_WARN) {
      if (!g.sess.over) {  // warn once, keep the power on
        g.sess.over = true;
        store::saveSession(g.sess);
        emitEvent("quota", fillQuota, &q);
        bump();
      }
    } else {
      emitEvent("quota", fillQuota, &q);
      if (g.cfg.kwh_action == KWH_END) endSession("quota");  // end now, even with time left
      g.sess.status = SESS_QUOTA;                            // relay off either way
      store::saveSession(g.sess);
      bump();
    }
  }

  // Session time limit (a disabled meter never renews).
  if (g.disabled) {
    // nothing: no sessions while disabled
  } else if (g.sess.sid && g.sess.status != SESS_EXPIRED) {
    uint32_t nr = nextReset();
    bool due = (nr && timekeep::valid() && timekeep::now() >= nr) || durationElapsedNoClock();
    if (due) {
      endSession("expired");  // no-op if the kWh limit already ended it
      if (g.cfg.auto_renew) {
        startSession(true);
      } else {
        g.sess.status = SESS_EXPIRED;
        store::saveSession(g.sess);
        bump();
      }
    }
  } else if (g.sess.status == SESS_EXPIRED && g.cfg.auto_renew) {
    startSession(true);  // auto renew was switched on while the session had ended
  }

  // Remember the real start time once the clock becomes valid.
  if (g.sess.sid && !g.sess.start && timekeep::valid()) {
    g.sess.start = timekeep::now() - (millis() - g.sess.startMs) / 1000;
    store::saveSession(g.sess);
  }

  // Relay decision.
  const char *block = nullptr;
  if (g.disabled) block = "disabled";
  else if (g.tripped) block = "tripped";
  else if (!g.sess.sid) block = "no_session";  // paired, first session not started yet (Renew)
  else if (g.sess.status == SESS_QUOTA) block = "quota";
  else if (g.sess.status == SESS_EXPIRED) block = "expired";
  else if (g.sess.status == SESS_ENDED) block = "ended";
  else if (g.cfg.tamper_protect && g.cfg.tamper_action == ACT_HOLD && g.tamper) block = "tamper";
  else if (g.cfg.volt_action == ACT_HOLD && g.voltOut) block = "voltage";
  else if (g.cfg.sensor_action == ACT_HOLD && g.sensorErr) block = "sensor";
  else if (g.cfg.temp_action == ACT_HOLD && g.tempHigh) block = "temp";
  else if (g.paired && g.cfg.offline_off_min && g.offlineSinceMs &&
           millis() - g.offlineSinceMs >= g.cfg.offline_off_min * 60000UL)
    block = "offline";
  if (block && strcmp(block, "offline") == 0 && !offlineEventSent_ && g.wantOn) {
    emitEvent("offline_relay_off");
    offlineEventSent_ = true;
  }
  if (!block || strcmp(block, "offline") != 0) offlineEventSent_ = false;

  bool on = g.wantOn && block == nullptr;
  // Anti-chatter: OFF is immediate, but ON again only after RELAY_MIN_OFF_MS off. A condition that
  // flips quickly (noisy tamper wire, value on the edge of a limit) then cannot pulse the SSR
  // (a lamp would flicker / glow brighter and dimmer).
  static uint32_t offSinceMs = 0;
  if (on && !g.relayOn && !firstPass && offSinceMs && millis() - offSinceMs < RELAY_MIN_OFF_MS) {
    on = false;
    if (!block) block = g.block;  // keep showing why it was off meanwhile
  }
  if (on != g.relayOn || block != g.block || firstPass) {
    if (on != g.relayOn || firstPass) {
      Serial.printf("[relay] %s%s%s\n", on ? "ON" : "OFF", block ? " - " : "", block ? block : "");
      if (!on) offSinceMs = millis();
      noteRelaySwitch(block);
    }
    g.relayOn = on;
    g.block = block;
    writeRelays(true);
    bump();
  }
}

// Relay outputs: the measured line (CHANNEL_RELAY) + AUX relays. Written on a change, and every 2 s
// anyway when I2C relay boards are used (recovers from a glitch on the board).
static void writeRelays(bool force) {
  // caller holds StateLock
  static uint32_t lastMask = 0xFFFFFFFF, lastWriteMs = 0;
  static bool errSent = false;
  uint32_t mask = auxrelay::outputMask();
  if (CHANNEL_RELAY && g.relayOn) mask |= 1UL << (CHANNEL_RELAY - 1);
  bool i2c = false;
  for (uint8_t i = 0; i < hw::RELAY_BOARD_COUNT; i++) i2c |= hw::RELAY_BOARD[i].driver != RELAY_GPIO;
  if (relayTest_) return;  // serial "relay test" owns the outputs
  if (relayForce_.exchange(false)) force = true;
  if (!force && mask == lastMask && !(i2c && millis() - lastWriteMs >= 2000)) return;
  lastMask = mask;
  lastWriteMs = millis();
  bool ok = relays::write(mask);
  if (!ok && !errSent) {
    errSent = true;
    Serial.println("[relay] ERROR: relay board not answering on I2C");
    emitEvent("relay_error");
  } else if (ok && errSent) {
    errSent = false;
    emitEvent("relay_ok");
  }
}

// AUX relay automation: live values of the measured line for the event rules.
static void auxTick() {
  if (!auxrelay::count()) return;
  auxrelay::Inputs in;
  {
    StateLock l;
    in.nch = 1;
    in.readOk = g.reading.ok && g.ac;
    in.disabled = g.disabled;
    in.a[0] = g.reading.a;
    in.w[0] = g.reading.w;
    in.running[0] = g.sess.sid && g.sess.status == SESS_ACTIVE && !g.disabled;
    if (g.sess.sid && g.cfg.kwh_max > 0) {
      double left = g.cfg.kwh_max - sessionKwhLimit();
      in.balance[0] = left > 0 ? (float)left : 0.0f;
    }
  }
  auxrelay::tick(in);
  StateLock l;
  writeRelays(false);
}

void bumpState() { bump(); }

#if TEMP_ENABLE
// Over-temperature: any sensor above cfg.temp_max triggers (event + action); every sensor back below
// temp_max - TEMP_HYST clears. A sensor that cannot be read does not count either way.
static void checkTemp() {
  // caller holds StateLock
  float hottest = NAN;
  uint8_t which = 0;
  for (uint8_t i = 0; i < temp::count(); i++) {
    float c = temp::value(i);
    if (!isnan(c) && (isnan(hottest) || c > hottest)) {
      hottest = c;
      which = i;
    }
  }
  if (g.cfg.temp_max <= 0 || isnan(hottest)) {
    if (g.tempHigh && g.cfg.temp_max <= 0) {
      g.tempHigh = false;
      emitEvent("temp_ok", [](JsonObject o, void *) { o["disabled"] = true; });
      bump();
    }
    return;
  }
  struct Ctx {
    uint8_t i;
    float c, max;
    const char *action;
  } ctx = {which, hottest, g.cfg.temp_max, ACTION_NAME[g.cfg.temp_action % 3]};
  if (!g.tempHigh && hottest > g.cfg.temp_max) {
    g.tempHigh = true;
    emitEvent("temp_high", [](JsonObject o, void *p) {
      Ctx *c = (Ctx *)p;
      o["i"] = c->i;
      o["c"] = serialized(String(c->c, 1));
      o["max"] = num4(c->max);
      o["action"] = c->action;
    }, &ctx);
    if (g.cfg.temp_action == ACT_END) stopSession(STOP_TEMP);
    bump();
  } else if (g.tempHigh && hottest < g.cfg.temp_max - TEMP_HYST) {
    g.tempHigh = false;
    emitEvent("temp_ok", [](JsonObject o, void *p) {
      o["i"] = ((Ctx *)p)->i;
      o["c"] = serialized(String(((Ctx *)p)->c, 1));
    }, &ctx);
    bump();
  }
}
#endif  // TEMP_ENABLE

static void checkTamper() {
#if TAMPER_ENABLE
  static uint8_t same = 0;
  bool open = digitalRead(PIN_TAMPER) == TAMPER_OPEN_LEVEL;
  StateLock l;
  if (open == g.tamper) {
    same = 0;
    return;
  }
  if (++same < TAMPER_DEBOUNCE_TICKS) return;  // debounce (~1 s): a long tamper wire picks up noise
  same = 0;
  g.tamper = open;
  if (open && g.cfg.tamper_protect) {
    const char *act = ACTION_NAME[g.cfg.tamper_action % 3];
    emitEvent("tamper_open", [](JsonObject o, void *p) { o["action"] = (const char *)p; }, (void *)act);
    if (g.cfg.tamper_action == ACT_END) stopSession(STOP_TAMPER);
  } else {
    emitEvent(open ? "tamper_open" : "tamper_close");
  }
  bump();
#endif  // TAMPER_ENABLE (no switch fitted: never open)
}

void task(void *) {
  uint32_t tick = 0;
  bool first = true;
  // Report conditions that are already present at boot (docs/protocol.md §2.5).
  {
    StateLock l;
    if (g.tamper) emitEvent("tamper_open", [](JsonObject o, void *) { o["boot"] = 1; });
    if (g.tripped) emitEvent("overcurrent", [](JsonObject o, void *) { o["boot"] = 1; });
  }
  TickType_t last = xTaskGetTickCount();
  for (;;) {
    sys::crumb(sys::CR_CONTROL, "sensor");
    if (tick % (1000 / CONTROL_TICK_MS) == 0) readSensor();
#if TEMP_ENABLE
    sys::crumb(sys::CR_CONTROL, "temperature");
    temp::poll();
    if (temp::takeFresh()) {
      StateLock l;
      tempsPending_ = true;
      checkTemp();
    }
#endif
    sys::crumb(sys::CR_CONTROL, "rules");
    checkTamper();
    checkRules(first);
    sys::crumb(sys::CR_CONTROL, "aux");
    auxTick();
    first = false;

    // Record a reading every read_s; send the buffered readings every report_s.
    uint32_t readMs, sendMs;
    {
      StateLock l;
      sendMs = (uint32_t)g.cfg.report_s * 1000UL;
      readMs = (uint32_t)(g.cfg.read_s ? g.cfg.read_s : g.cfg.report_s) * 1000UL;
    }
    bool now = reportNow_.exchange(false);
    if (now || millis() - lastReportMs_ >= readMs) {
      lastReportMs_ = millis();
      sys::crumb(sys::CR_CONTROL, "telemetry");
      pushTelemetry();
    }
    if (now || millis() - lastSendReqMs_ >= sendMs) {
      lastSendReqMs_ = millis();
      sendReq_ = true;
    }
    tick++;
    vTaskDelayUntil(&last, pdMS_TO_TICKS(CONTROL_TICK_MS));
  }
}

}  // namespace control
