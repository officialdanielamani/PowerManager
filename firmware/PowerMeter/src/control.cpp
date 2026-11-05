#include "control.h"

#include <atomic>

#include "../config.h"
#include "outbox.h"
#include "storage.h"
#include "system.h"
#include "timekeep.h"

namespace control {

static Sensor *sensor_ = nullptr;
static std::atomic<uint32_t> stateVer_{1};
static std::atomic<bool> reportNow_{false};
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
  Serial.printf("[event] %s\n", s.c_str());
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
    if (!g.reading.ok) doc["se"] = SENS_NAME[(g.lastReadErr ? g.lastReadErr : SENS_NO_REPLY) % 3];
    if (g.reading.ok && g.ac) {
      JsonObject c = ch.add<JsonObject>();
      c["i"] = 0;
      c["v"] = serialized(String(g.reading.v, 1));
      c["a"] = serialized(String(g.reading.a, 3));
      c["w"] = serialized(String(g.reading.w, 1));
      c["pf"] = serialized(String(g.reading.pf, 2));
      c["hz"] = serialized(String(g.reading.hz, 1));
      c["e"] = serialized(String(g.reading.e, 3));
    }
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
  return why == STOP_VOLTAGE ? "voltage" : why == STOP_TAMPER ? "tamper" : why == STOP_SENSOR ? "sensor" : "disabled";
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
  StateLock l;
  g.cfg = c;
  store::saveConfig(c);
  // A session that already ended (kWh or time limit) stays ended: use Renew to start a new one.
  bump();
}

void reportNow() { reportNow_ = true; }

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
  if (g.sensorErr) o["sensor_err"] = SENS_NAME[g.sensorErr % 3];
  if (nr) s["next_reset"] = nr;
}

// ------------------------------------------------------------------ main loop

void begin(Sensor *sensor) {
  sensor_ = sensor;
  sensor_->begin();
  pinMode(PIN_RELAY, OUTPUT);
  digitalWrite(PIN_RELAY, RELAY_ACTIVE_HIGH ? LOW : HIGH);  // start OFF
  pinMode(PIN_TAMPER, INPUT_PULLUP);
  StateLock l;
  g.tamper = digitalRead(PIN_TAMPER) == TAMPER_OPEN_LEVEL;
  g.lastE = g.sess.e0;  // until the first reading arrives
}

// Operating voltage range. Out = VOLT_OUT_READS bad readings in a row; back = VOLT_OK_READS good
// ones. "No AC" (below AC_LOST_VOLTAGE / no reply) is not a range problem: it keeps the state.
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
  if (!ok || v < AC_LOST_VOLTAGE) return;
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
  g.sensorErr = err ? err : SENS_NO_REPLY;
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
  g.lastReadErr = ok ? SENS_OK : (err ? err : SENS_NO_REPLY);
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
  bool acNow = ok ? r.v >= AC_LOST_VOLTAGE : (failReads_ < 3 && g.ac);
  if (acNow != g.ac) {
    g.ac = acNow;
    emitEvent(acNow ? "ac_restored" : "ac_lost");
    bump();
  }
  checkVoltage(ok, r.v);
}

static void checkRules(bool firstPass) {
  StateLock l;
  // First session after pairing / factory reset starts with the first reading.
  if (g.sess.sid == 0 && !g.disabled && (g.sensorEverOk || millis() > 15000)) startSession(false);

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
  else if (g.sess.status == SESS_QUOTA) block = "quota";
  else if (g.sess.status == SESS_EXPIRED) block = "expired";
  else if (g.sess.status == SESS_ENDED) block = "ended";
  else if (g.cfg.tamper_protect && g.cfg.tamper_action == ACT_HOLD && g.tamper) block = "tamper";
  else if (g.cfg.volt_action == ACT_HOLD && g.voltOut) block = "voltage";
  else if (g.cfg.sensor_action == ACT_HOLD && g.sensorErr) block = "sensor";
  else if (g.paired && g.cfg.offline_off_min && g.offlineSinceMs &&
           millis() - g.offlineSinceMs >= g.cfg.offline_off_min * 60000UL)
    block = "offline";
  if (block && strcmp(block, "offline") == 0 && !offlineEventSent_ && g.wantOn) {
    emitEvent("offline_relay_off");
    offlineEventSent_ = true;
  }
  if (!block || strcmp(block, "offline") != 0) offlineEventSent_ = false;

  bool on = g.wantOn && block == nullptr;
  if (on != g.relayOn || block != g.block || firstPass) {
    g.relayOn = on;
    g.block = block;
    digitalWrite(PIN_RELAY, (on == (bool)RELAY_ACTIVE_HIGH) ? HIGH : LOW);
    bump();
  }
}

static void checkTamper() {
  static uint8_t same = 0;
  bool open = digitalRead(PIN_TAMPER) == TAMPER_OPEN_LEVEL;
  StateLock l;
  if (open == g.tamper) {
    same = 0;
    return;
  }
  if (++same < 2) return;  // debounce: 2 ticks (~500 ms)
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
    sys::crumb(sys::CR_CONTROL, "rules");
    checkTamper();
    checkRules(first);
    first = false;

    uint32_t reportMs;
    {
      StateLock l;
      reportMs = (uint32_t)g.cfg.report_s * 1000UL;
    }
    if (reportNow_ || millis() - lastReportMs_ >= reportMs) {
      reportNow_ = false;
      lastReportMs_ = millis();
      sys::crumb(sys::CR_CONTROL, "telemetry");
      pushTelemetry();
    }
    tick++;
    vTaskDelayUntil(&last, pdMS_TO_TICKS(CONTROL_TICK_MS));
  }
}

}  // namespace control
