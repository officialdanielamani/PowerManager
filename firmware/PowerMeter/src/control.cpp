#include "control.h"

#include <atomic>

#include "../config.h"
#include "outbox.h"
#include "storage.h"
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
  double kwh = g.lastE - g.sess.e0;
  return kwh > 0 ? kwh : 0;
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
};

static void startSession(bool autoRenew) {
  // caller holds StateLock
  g.sess.sid += 1;
  g.sess.start = timekeep::valid() ? timekeep::now() : 0;
  g.sess.startMs = millis();
  g.sess.e0 = g.lastE;
  g.sess.status = SESS_ACTIVE;
  g.sess.over = false;
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
    o["max"] = c->max;
  }, &ctx);
  bump();
}

static void endSession(const char *reason) {
  // caller holds StateLock
  EndCtx ctx = {g.sess.sid, reason, g.lastE - g.sess.e0};
  emitEvent("session_end", [](JsonObject o, void *p) {
    EndCtx *c = (EndCtx *)p;
    o["sid"] = c->sid;
    o["reason"] = c->reason;
    o["kwh"] = serialized(String(c->kwh > 0 ? c->kwh : 0, 4));
  }, &ctx);
}

void renew(const char *reason, bool autoRenew) {
  StateLock l;
  // A session stopped by its kWh limit or time limit has already been ended.
  if (g.sess.sid && g.sess.status == SESS_ACTIVE) endSession(reason);
  startSession(autoRenew);
}

// ------------------------------------------------------------------ commands

bool setRelay(bool on, const char *src, const char *&err) {
  StateLock l;
  if (on) {
    if (g.tripped) err = "tripped";
    else if (g.sess.status == SESS_QUOTA) err = "quota_exceeded";
    else if (g.sess.status == SESS_EXPIRED) err = "session_expired";
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
  s["max"] = g.cfg.kwh_max;
  s["status"] = sessStatusName(g.sess.status);
  if (g.sess.over) s["over"] = true;
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

static void readSensor() {
  Reading r;
  bool ok = sensor_->read(0, r);
  StateLock l;
  if (ok) {
    g.reading = r;
    g.lastE = r.e;
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
}

static void checkRules(bool firstPass) {
  StateLock l;
  // First session after pairing / factory reset starts with the first reading.
  if (g.sess.sid == 0 && (g.sensorEverOk || millis() > 15000)) startSession(false);

  // Over-current (latched until reset).
  if (!g.tripped && g.cfg.amp_max > 0 && g.reading.ok && g.reading.a > g.cfg.amp_max) {
    if (++overReads_ >= TRIP_CONFIRM_READS) {
      g.tripped = true;
      store::saveFlags(g.wantOn, g.tripped);
      struct Ctx {
        float a, limit;
      } ctx = {g.reading.a, g.cfg.amp_max};
      emitEvent("overcurrent", [](JsonObject o, void *p) {
        Ctx *c = (Ctx *)p;
        o["ch"] = 0;
        o["val"] = serialized(String(c->a, 2));
        o["limit"] = c->limit;
      }, &ctx);
      bump();
    }
  } else if (!g.tripped) {
    overReads_ = 0;
  }

  // kWh quota.
  double kwh = g.lastE - g.sess.e0;
  if (g.sess.sid && g.sess.status == SESS_ACTIVE && g.cfg.kwh_max > 0 && kwh >= g.cfg.kwh_max) {
    struct QCtx {
      double kwh;
      bool end;
    } q = {kwh, g.cfg.kwh_end};
    auto fillQuota = [](JsonObject o, void *p) {
      QCtx *c = (QCtx *)p;
      o["ch"] = 0;
      o["kwh"] = serialized(String(c->kwh, 4));
      o["end"] = c->end;
    };
    if (g.cfg.kwh_end) {  // hard limit: the session ends now, relay off until renewed
      emitEvent("quota", fillQuota, &q);
      endSession("quota");
      g.sess.status = SESS_QUOTA;
      store::saveSession(g.sess);
      bump();
    } else if (!g.sess.over) {  // soft limit: warn once, keep the power on
      g.sess.over = true;
      store::saveSession(g.sess);
      emitEvent("quota", fillQuota, &q);
      bump();
    }
  }

  // Session time limit.
  if (g.sess.sid && g.sess.status != SESS_EXPIRED) {
    uint32_t nr = nextReset();
    bool due = (nr && timekeep::valid() && timekeep::now() >= nr) || durationElapsedNoClock();
    if (due) {
      bool running = g.sess.status == SESS_ACTIVE;  // a kWh-ended session was already closed
      if (g.cfg.auto_renew) {
        if (running) endSession("expired");
        startSession(true);
      } else {
        if (running) endSession("expired");
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
  if (g.tripped) block = "tripped";
  else if (g.sess.status == SESS_QUOTA) block = "quota";
  else if (g.sess.status == SESS_EXPIRED) block = "expired";
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
  emitEvent(open ? "tamper_open" : "tamper_close");
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
    if (tick % (1000 / CONTROL_TICK_MS) == 0) readSensor();
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
      pushTelemetry();
    }
    tick++;
    vTaskDelayUntil(&last, pdMS_TO_TICKS(CONTROL_TICK_MS));
  }
}

}  // namespace control
