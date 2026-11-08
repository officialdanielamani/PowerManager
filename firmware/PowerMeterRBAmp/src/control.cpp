#include "control.h"

#include <atomic>
#include <sys/time.h>

#include "../config.h"
#include "outbox.h"
#include "rbamp.h"
#include "relays.h"
#include "storage.h"
#include "system.h"
#include "auxrelay.h"
#include "temp.h"
#include "timekeep.h"

namespace control {

static std::atomic<uint32_t> stateVer_{1};
static std::atomic<bool> reportNow_{false};
static std::atomic<bool> sendReq_{false};  // time to send (report_s passed, event, Get Data Now)
static uint32_t lastSendReqMs_ = 0, lastReportMs_ = 0;
static bool offlineEventSent_ = false;
static SemaphoreHandle_t drdySem_ = nullptr;
static uint32_t relaySwitches_ = 0;

static void bump() { stateVer_++; }
uint32_t stateVersion() { return stateVer_; }
uint32_t relaySwitches() { return relaySwitches_; }
static const char *SENS_NAME[] = {"ok", "no_reply", "bad"};
static const char *ACTION_NAME[] = {"run", "hold", "end"};
static bool indep() { return g.cfg.mode == MODE_INDEPENDENT; }

// ------------------------------------------------------------------ outbox helpers

static void stampTime(JsonDocument &doc) {
  if (timekeep::valid()) {
    struct timeval tv;
    gettimeofday(&tv, nullptr);
    doc["ts"] = (uint32_t)tv.tv_sec;
    if (g.cfg.read_ms % 1000) doc["ms"] = (uint16_t)(tv.tv_usec / 1000);  // sub-second readings (DRDY)
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

// ------------------------------------------------------------------ energy / sessions

static double slotEnergy(uint8_t s) {
  // caller holds StateLock
  if (indep()) return g.ch[s].e;
  double t = 0;
  for (uint8_t i = 0; i < g.nch; i++) t += g.ch[i].e;
  return t;
}

double slotKwh(uint8_t s) {
  StateLock l;
  double k = slotEnergy(s) - g.sess[s].e0;
  return k > 0 ? k : 0;
}

static uint16_t wantMask() {
  uint16_t m = 0;
  for (uint8_t i = 0; i < MC_MAX_CH && i < 16; i++)
    if (g.ch[i].wantOn) m |= 1u << i;
  return m;
}
static uint16_t tripMask() {
  uint16_t m = 0;
  for (uint8_t i = 0; i < MC_MAX_CH && i < 16; i++)
    if (g.ch[i].tripped) m |= 1u << i;
  return m;
}
static void saveFlags() { store::saveFlags(wantMask(), tripMask()); }
static void saveEnergy() {
  double e[MC_MAX_CH];
  for (int i = 0; i < MC_MAX_CH; i++) e[i] = g.ch[i].e;
  store::saveEnergy(e);
}

uint32_t nextReset(uint8_t slot) {
  StateLock l;
  const SessConfig &c = slotSessCfg(slot);
  const SessionState &s = g.sess[slot];
  if (!s.sid) return 0;
  if (c.mode == SESS_DURATION) {
    if (s.start) return s.start + c.dur_min * 60;
    if (timekeep::valid()) return timekeep::now() - (millis() - s.startMs) / 1000 + c.dur_min * 60;
    return 0;
  }
  if (c.mode == SESS_DAILY && timekeep::valid()) {
    uint32_t start = s.start ? s.start : timekeep::now() - (millis() - s.startMs) / 1000;
    uint32_t day = start - start % 86400;
    uint32_t t = day + c.daily_utc_min * 60;
    if (t <= start) t += 86400;
    return t;
  }
  return 0;
}

static bool durationElapsedNoClock(uint8_t slot) {
  const SessConfig &c = slotSessCfg(slot);
  const SessionState &s = g.sess[slot];
  return c.mode == SESS_DURATION && !s.start && !timekeep::valid() && (millis() - s.startMs) / 60000UL >= c.dur_min;
}

struct SessCtx {
  uint32_t sid;
  int8_t ch;  // -1 = shared session
  bool autoRenew;
  float max;
  const char *reason;
  double kwh;
};

static void startSession(uint8_t slot, bool autoRenew) {
  // caller holds StateLock
  SessionState &s = g.sess[slot];
  g.sidCounter++;
  store::saveSidCounter(g.sidCounter);
  s.sid = g.sidCounter;
  s.start = timekeep::valid() ? timekeep::now() : 0;
  s.startMs = millis();
  s.e0 = slotEnergy(slot);
  s.status = SESS_ACTIVE;
  s.over = false;
  s.ended = false;
  s.stopReason = STOP_NONE;
  store::saveSession(slot, s);
  saveEnergy();
  SessCtx ctx = {s.sid, (int8_t)(indep() ? slot : -1), autoRenew, slotKwhMax(slot), nullptr, 0};
  emitEvent("session_start", [](JsonObject o, void *p) {
    SessCtx *c = (SessCtx *)p;
    o["sid"] = c->sid;
    if (c->ch >= 0) o["ch"] = c->ch;
    o["auto"] = c->autoRenew;
    o["max"] = num4(c->max);
  }, &ctx);
  bump();
}

static void endSession(uint8_t slot, const char *reason) {
  // caller holds StateLock. Sends session_end once per session.
  SessionState &s = g.sess[slot];
  if (s.ended || !s.sid) return;
  s.ended = true;
  double kwh = slotEnergy(slot) - s.e0;
  SessCtx ctx = {s.sid, (int8_t)(indep() ? slot : -1), false, 0, reason, kwh > 0 ? kwh : 0};
  emitEvent("session_end", [](JsonObject o, void *p) {
    SessCtx *c = (SessCtx *)p;
    o["sid"] = c->sid;
    if (c->ch >= 0) o["ch"] = c->ch;
    o["reason"] = c->reason;
    o["kwh"] = serialized(String(c->kwh, 4));
  }, &ctx);
  saveEnergy();
}

static const char *stopReasonName(uint8_t why) {
  switch (why) {
    case STOP_VOLTAGE: return "voltage";
    case STOP_TAMPER: return "tamper";
    case STOP_SENSOR: return "sensor";
    case STOP_MANUAL: return "stopped";
    case STOP_MODE: return "mode";
    case STOP_TEMP: return "temp";
    default: return "disabled";
  }
}

// End a slot's session now (protection "end", End command, mode change, disable): relay off until renewed.
static void stopSlot(uint8_t slot, StopReason why) {
  // caller holds StateLock
  SessionState &s = g.sess[slot];
  if (!s.sid || s.status == SESS_ENDED || s.status == SESS_EXPIRED) return;
  endSession(slot, stopReasonName(why));
  s.status = SESS_ENDED;
  s.stopReason = why;
  store::saveSession(slot, s);
  bump();
}
static void stopAll(StopReason why) {
  for (uint8_t i = 0; i < sessSlots(); i++) stopSlot(i, why);
}

static void renewSlot(uint8_t slot, const char *reason, bool autoRenew) {
  // caller holds StateLock
  if (g.disabled) return;  // a disabled meter has no sessions
  if (g.sess[slot].sid) endSession(slot, reason);  // no-op if a limit already ended it
  startSession(slot, autoRenew);
}

void renew(int8_t ch, const char *reason) {
  StateLock l;
  if (!indep()) renewSlot(0, reason, false);
  else if (ch >= 0 && ch < g.nch) renewSlot(ch, reason, false);
  else
    for (uint8_t i = 0; i < sessSlots(); i++) renewSlot(i, reason, false);
}

void endSessions(int8_t ch) {
  StateLock l;
  if (indep() && ch >= 0 && ch < g.nch) stopSlot(ch, STOP_MANUAL);
  else stopAll(STOP_MANUAL);
}

void setDisabled(bool disabled) {
  StateLock l;
  if (g.disabled == disabled) return;
  g.disabled = disabled;
  store::saveDisabled(disabled);
  if (disabled) stopAll(STOP_DISABLED);  // sessions end, relays off ("disabled" block)
  // Enabled again: no new session. The ended sessions keep the relays off until Renew.
  bump();
}

// ------------------------------------------------------------------ commands

bool setRelay(uint8_t ch, bool on, const char *src, const char *&err) {
  StateLock l;
  err = nullptr;
  if (ch >= MC_MAX_CH || (g.nch && ch >= g.nch)) {
    err = "bad_channel";
    return false;
  }
  if (on) {
    SessStatus st = g.sess[slotOf(ch)].status;
    if (g.disabled) err = "disabled";
    else if (g.ch[ch].tripped) err = "tripped";
    else if (!g.sess[slotOf(ch)].sid) err = "no_session";
    else if (st == SESS_QUOTA) err = "quota_exceeded";
    else if (st == SESS_EXPIRED || st == SESS_ENDED) err = "session_expired";
    if (err) return false;
  }
  if (g.ch[ch].wantOn != on) {
    g.ch[ch].wantOn = on;
    saveFlags();
    struct Ctx {
      uint8_t ch;
      bool on;
      const char *src;
    } ctx = {ch, on, src};
    emitEvent("relay", [](JsonObject o, void *p) {
      Ctx *c = (Ctx *)p;
      o["ch"] = c->ch;
      o["on"] = c->on;
      o["src"] = c->src;
    }, &ctx);
    bump();
  }
  return true;
}

void bumpState() { bump(); }

bool resetTrip(int8_t ch, const char *src, const char *by) {
  StateLock l;
  bool any = false;
  for (uint8_t i = 0; i < g.nch; i++) {
    if ((ch >= 0 && i != ch) || !g.ch[i].tripped) continue;
    g.ch[i].tripped = false;
    g.ch[i].overReads = 0;
    any = true;
    struct Ctx {
      uint8_t ch;
      const char *src, *by;
    } ctx = {i, src, by};
    emitEvent("trip_reset", [](JsonObject o, void *p) {
      Ctx *c = (Ctx *)p;
      o["ch"] = c->ch;
      o["src"] = c->src;
      if (c->by && *c->by) o["by"] = c->by;
    }, &ctx);
  }
  if (any) {
    saveFlags();
    bump();
  }
  return any;
}

void applyConfig(const MeterConfig &c) {
  auxrelay::commit();  // AUX names + automation (staged by net parseConfig)
  StateLock l;
  if (c.mode != g.cfg.mode) {
    // Shared <-> Independent: the running session(s) end; press Renew to start in the new mode.
    stopAll(STOP_MODE);
    g.cfg = c;
    for (uint8_t i = 0; i < sessSlots(); i++) {
      SessionState &s = g.sess[i];
      if (s.status == SESS_ACTIVE) {  // slot not used before (or never ended): no session yet
        s.status = SESS_ENDED;
        s.stopReason = STOP_MODE;
        s.ended = true;
        store::saveSession(i, s);
      }
    }
  } else {
    g.cfg = c;
  }
  store::saveConfig(c);
  // A session that already ended (kWh or time limit) stays ended: use Renew to start a new one.
  bump();
}

void reportNow() { reportNow_ = true; }
bool takeSendRequest() { return sendReq_.exchange(false); }

// ------------------------------------------------------------------ state

static void sessJson(JsonObject s, uint8_t slot) {
  // caller holds StateLock
  const SessionState &ss = g.sess[slot];
  s["sid"] = ss.sid;
  if (indep()) s["ch"] = slot;
  if (ss.start) s["start"] = ss.start;
  double k = slotEnergy(slot) - ss.e0;
  s["kwh"] = serialized(String(k > 0 ? k : 0, 4));
  s["max"] = num4(slotKwhMax(slot));
  s["status"] = sessStatusName(ss.status);
  if (ss.over) s["over"] = true;
  s["ended"] = ss.ended;
  if (ss.status == SESS_ENDED) s["end_reason"] = stopReasonName(ss.stopReason);
  uint32_t nr = nextReset(slot);
  if (nr) s["next_reset"] = nr;
}

void stateJson(JsonObject o) {
  StateLock l;
  o["mode"] = indep() ? "independent" : "shared";
  JsonArray relay = o["relay"].to<JsonArray>();
  bool anyTrip = false;
  for (uint8_t i = 0; i < g.nch; i++) {
    JsonObject r = relay.add<JsonObject>();
    r["i"] = i;
    r["on"] = g.ch[i].relayOn;
    r["want"] = g.ch[i].wantOn;
    if (g.ch[i].block) r["block"] = g.ch[i].block;
    else r["block"] = nullptr;
    r["tripped"] = g.ch[i].tripped;
    anyTrip |= g.ch[i].tripped;
  }
  o["tripped"] = anyTrip;
  o["tamper"] = g.tamper;
  o["ac"] = g.ac;
  if (g.disabled) o["disabled"] = true;
  if (g.voltOut) o["volt_out"] = true;
  if (g.tempHigh) o["temp_high"] = true;
  if (g.sensorErr) o["sensor_err"] = SENS_NAME[g.sensorErr % 3];
  if (!g.nch) o["no_sensor"] = true;
  if (auxrelay::count()) auxrelay::stateJson(o["aux"].to<JsonArray>());
  if (indep()) {
    JsonArray a = o["sessions"].to<JsonArray>();
    for (uint8_t i = 0; i < sessSlots(); i++) sessJson(a.add<JsonObject>(), i);
  } else {
    sessJson(o["sess"].to<JsonObject>(), 0);
  }
}

// ------------------------------------------------------------------ telemetry

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
    if (g.cfg.temp_action == ACT_END) stopAll(STOP_TEMP);
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

// Session progress with every reading (the "state" message is only sent on changes).
static void addSessionProgress(JsonDocument &doc) {
  // caller holds StateLock
  if (indep()) {
    JsonArray ss = doc["ss"].to<JsonArray>();
    for (uint8_t i = 0; i < sessSlots(); i++) {
      JsonObject o = ss.add<JsonObject>();
      o["ch"] = i;
      o["sid"] = g.sess[i].sid;
      double k = slotEnergy(i) - g.sess[i].e0;
      o["kwh"] = serialized(String(k > 0 ? k : 0, 4));
    }
  } else {
    JsonObject se = doc["s"].to<JsonObject>();
    se["sid"] = g.sess[0].sid;
    double k = slotEnergy(0) - g.sess[0].e0;
    se["kwh"] = serialized(String(k > 0 ? k : 0, 4));
  }
}

static void pushTelemetry() {
  JsonDocument doc;
  uint32_t seq = store::nextSeq();
  doc["seq"] = seq;
  stampTime(doc);
  {
    StateLock l;
    JsonArray ch = doc["ch"].to<JsonArray>();
    if (!g.readOk) doc["se"] = SENS_NAME[(g.lastReadErr ? g.lastReadErr : (uint8_t)SENS_NO_REPLY) % 3];
    if (g.readOk && g.ac) {
      for (uint8_t i = 0; i < g.nch; i++) {
        JsonObject c = ch.add<JsonObject>();
        c["i"] = i;
        if (g.hasVoltage) c["v"] = serialized(String(g.v, 1));
        c["a"] = serialized(String(g.ch[i].r.a, 3));
        if (g.hasVoltage) {
          c["w"] = serialized(String(g.ch[i].r.w, 1));
          c["pf"] = serialized(String(g.ch[i].r.pf, 2));
          if (g.hz) c["hz"] = g.hz;
        }
        c["e"] = serialized(String(g.ch[i].e, 4));
      }
    }
    addTemps(doc);
    addSessionProgress(doc);
  }
  String s;
  serializeJson(doc, s);
  outbox::push(outbox::TEL, seq, s);
}

// ------------------------------------------------------------------ DRDY frames
// DRDY data mode: every measurement window (~100 ms) is recorded raw. Windows are packed into one
// outbox item per second ("fr": [[ms, V, A1, W1, PF1, A2, ...], ...]) to keep RAM and traffic small.

struct Frame {
  uint64_t atMs;  // epoch ms (uptime ms while the clock is not set)
  float v;
  float a[MC_MAX_CH], w[MC_MAX_CH], pf[MC_MAX_CH];
};
static const uint8_t FRAMES_MAX = 16;
static Frame frames_[FRAMES_MAX];
static uint8_t nFrames_ = 0;
static uint32_t firstFrameMs_ = 0;

static uint64_t nowMs64() {
  if (timekeep::valid()) {
    struct timeval tv;
    gettimeofday(&tv, nullptr);
    return (uint64_t)tv.tv_sec * 1000ULL + tv.tv_usec / 1000;
  }
  return millis();
}

static void addFrame(const rbamp::Sample &smp) {
  // caller holds StateLock
  if (nFrames_ >= FRAMES_MAX) return;  // pushFrames() runs every second, so this does not happen
  Frame &f = frames_[nFrames_++];
  if (nFrames_ == 1) firstFrameMs_ = millis();
  f.atMs = nowMs64();
  f.v = smp.v;
  for (uint8_t i = 0; i < g.nch; i++) {
    f.a[i] = smp.a[i];
    f.w[i] = smp.w[i];
    f.pf[i] = smp.pf[i];
  }
}

static void addSessionProgress(JsonDocument &doc);

static void pushFrames() {
  JsonDocument doc;
  {
    StateLock l;
    if (!nFrames_) return;
    uint32_t seq = store::nextSeq();
    doc["seq"] = seq;
    uint32_t ts = (uint32_t)(frames_[0].atMs / 1000);
    doc["ts"] = ts;
    doc["tv"] = timekeep::valid() ? 1 : 0;
    if (!timekeep::valid()) doc["boot"] = store::bootCount();
    if (g.hz) doc["hz"] = g.hz;
    JsonArray e = doc["e"].to<JsonArray>();
    for (uint8_t i = 0; i < g.nch; i++) e.add(serialized(String(g.ch[i].e, 4)));
    JsonArray fr = doc["fr"].to<JsonArray>();
    for (uint8_t k = 0; k < nFrames_; k++) {
      const Frame &f = frames_[k];
      JsonArray x = fr.add<JsonArray>();
      x.add((uint32_t)(f.atMs - (uint64_t)ts * 1000ULL));  // ms after ts
      x.add(serialized(String(f.v, 1)));
      for (uint8_t i = 0; i < g.nch; i++) {
        x.add(serialized(String(f.a[i], 3)));
        x.add(serialized(String(f.w[i], 1)));
        x.add(serialized(String(f.pf[i], 2)));
      }
    }
    nFrames_ = 0;
    addTemps(doc);
    addSessionProgress(doc);
  }
  String s;
  serializeJson(doc, s);
  outbox::push(outbox::TEL, doc["seq"].as<uint32_t>(), s);
}

// ------------------------------------------------------------------ sensor

static void IRAM_ATTR onDrdy() {
  BaseType_t woken = pdFALSE;
  if (drdySem_) xSemaphoreGiveFromISR(drdySem_, &woken);
  if (woken) portYIELD_FROM_ISR();
}

static void sensorFound() {
  StateLock l;
  g.nch = rbamp::channels();
  g.hasVoltage = rbamp::hasVoltage();
  strncpy(g.variant, rbamp::variantName(), sizeof(g.variant) - 1);
  store::saveChannels(g.nch);
  bump();
}

void begin() {
#if TAMPER_ENABLE
  pinMode(PIN_TAMPER, INPUT_PULLUP);
#endif
  relays::begin();  // all relays OFF first
  drdySem_ = xSemaphoreCreateBinary();
  pinMode(PIN_DRDY, INPUT_PULLUP);
  attachInterrupt(digitalPinToInterrupt(PIN_DRDY), onDrdy, FALLING);
  {
    StateLock l;
#if TAMPER_ENABLE
    g.tamper = digitalRead(PIN_TAMPER) == TAMPER_OPEN_LEVEL;
#endif
    g.nch = hw::NCH;  // from the CHANNELS table (relays / sessions work before the modules answer)
    store::saveChannels(g.nch);
  }
  temp::begin();
  if (rbamp::begin()) sensorFound();
  else Serial.printf("[rbamp] not ready at start-up (%s); retrying every 5 s\n", rbamp::boardSummary().c_str());
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
  if (g.cfg.sensor_action == ACT_END) stopAll(STOP_SENSOR);
  bump();
}

// Operating voltage range: out for VOLT_OUT_MS triggers, back in range for VOLT_OK_MS clears.
// "No AC" (below SUPPLY_LOST_VOLTAGE) is not a range problem: it keeps the state.
static void checkVoltage(bool ok, float v) {
  // caller holds StateLock
  static uint32_t badSince = 0, goodSince = 0;
  bool enabled = g.hasVoltage && (g.cfg.volt_min > 0 || g.cfg.volt_max > 0);
  if (!enabled) {
    badSince = goodSince = 0;
    if (g.voltOut) {
      g.voltOut = false;
      emitEvent("voltage_ok", [](JsonObject o, void *) { o["disabled"] = true; });
      bump();
    }
    return;
  }
  if (!ok || v < SUPPLY_LOST_VOLTAGE) return;
  uint32_t now = millis();
  bool low = g.cfg.volt_min > 0 && v < g.cfg.volt_min;
  bool out = low || (g.cfg.volt_max > 0 && v > g.cfg.volt_max);
  struct Ctx {
    float v, min, max;
    const char *dir, *action;
  } ctx = {v, g.cfg.volt_min, g.cfg.volt_max, low ? "low" : "high", ACTION_NAME[g.cfg.volt_action % 3]};
  if (out) {
    goodSince = 0;
    if (!badSince) badSince = now;
    if (!g.voltOut && now - badSince >= VOLT_OUT_MS) {
      g.voltOut = true;
      emitEvent("voltage_out", [](JsonObject o, void *p) {
        Ctx *c = (Ctx *)p;
        o["v"] = serialized(String(c->v, 1));
        o["min"] = num4(c->min);
        o["max"] = num4(c->max);
        o["dir"] = c->dir;
        o["action"] = c->action;
      }, &ctx);
      if (g.cfg.volt_action == ACT_END) stopAll(STOP_VOLTAGE);
      bump();
    }
  } else {
    badSince = 0;
    if (!goodSince) goodSince = now;
    if (g.voltOut && now - goodSince >= VOLT_OK_MS) {
      g.voltOut = false;
      emitEvent("voltage_ok", [](JsonObject o, void *p) { o["v"] = serialized(String(((Ctx *)p)->v, 1)); }, &ctx);
      bump();
    }
  }
}

// One read of the module: every 100 ms window with DRDY, every second without. Over-current is
// checked on every read, so with DRDY a trip happens within ~200 ms.
static void readSensor() {
  rbamp::Sample smp;
  bool ok = rbamp::read(smp);
  uint8_t err = rbamp::lastError();
  StateLock l;
  g.lastReadErr = ok ? (uint8_t)SENS_OK : (err ? err : (uint8_t)SENS_NO_REPLY);
  checkSensor(ok, g.lastReadErr);
  g.readOk = ok;
  if (ok) {
    g.v = smp.v;
    g.hz = smp.hz;
    g.readAtMs = millis();
    g.sensorEverOk = true;
    for (uint8_t i = 0; i < g.nch; i++) {
      g.ch[i].r.a = smp.a[i];
      g.ch[i].r.w = smp.w[i];
      g.ch[i].r.pf = smp.pf[i];
    }
  }
  // The module has its own 5 V supply and keeps answering in a power cut: U ~ 0 = no AC.
  bool acNow = g.hasVoltage ? (ok ? smp.v >= SUPPLY_LOST_VOLTAGE : g.ac) : true;
  if (acNow != g.ac) {
    g.ac = acNow;
    emitEvent(acNow ? "ac_restored" : "ac_lost");
    bump();
  }
  checkVoltage(ok, smp.v);
  if (!ok) return;
  if (g.cfg.drdy) addFrame(smp);  // DRDY data mode: every window is recorded
  for (uint8_t i = 0; i < g.nch; i++) {
    ChState &c = g.ch[i];
    float lim = g.cfg.ch[i].amp_max;
    if (!c.tripped && lim > 0 && c.r.a > lim) {
      if (++c.overReads >= TRIP_CONFIRM_READS) {
        c.tripped = true;
        saveFlags();
        struct Ctx {
          uint8_t ch;
          float a, limit;
        } ctx = {i, c.r.a, lim};
        emitEvent("overcurrent", [](JsonObject o, void *p) {
          Ctx *x = (Ctx *)p;
          o["ch"] = x->ch;
          o["val"] = serialized(String(x->a, 2));
          o["limit"] = num4(x->limit);
        }, &ctx);
        bump();
      }
    } else if (!c.tripped) {
      c.overReads = 0;
    }
  }
}

// Energy: close a module period every ENERGY_PERIOD_MS; ~60 ms later read each channel's average
// power over it and add avg W x dt (master clock) to the channel's kWh counter.
static void energyTick() {
  static uint32_t lastStartMs = 0, finishAt = 0, pendingDt = 0, lastSaveMs = 0;
  static bool pending = false;
  uint32_t now = millis();
  if (!rbamp::present()) return;
  if (!pending && now - lastStartMs >= ENERGY_PERIOD_MS) {
    uint32_t prev = lastStartMs;
    if (rbamp::periodStart()) {
      pending = true;
      pendingDt = prev ? now - prev : 0;
      lastStartMs = now;
      finishAt = now + 60;
    }
  }
  if (pending && (int32_t)(now - finishAt) >= 0) {
    pending = false;
    float avg[MC_MAX_CH] = {0};
    bool valid = false;
    if (rbamp::periodFinish(avg, valid) && valid && pendingDt > 0 && pendingDt < 10000) {
      StateLock l;
      for (uint8_t i = 0; i < g.nch; i++) g.ch[i].e += (double)avg[i] * pendingDt / 3.6e9;  // W x ms -> kWh
    }
  }
  if (now - lastSaveMs >= ENERGY_SAVE_MS) {
    lastSaveMs = now;
    StateLock l;
    saveEnergy();
  }
}

// ------------------------------------------------------------------ rules + relays

static void noteRelaySwitch(uint8_t ch, const char *block) {
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
    Serial.printf("[relay] WARNING: %lu changes within a minute (last: CH%u %s)\n", (unsigned long)inWindow, ch + 1,
                  block ? block : "user");
    struct Ctx {
      uint32_t n;
      uint8_t ch;
      const char *why;
    } ctx = {inWindow, ch, block ? block : "user"};
    emitEvent("relay_chatter", [](JsonObject o, void *p) {
      o["n"] = ((Ctx *)p)->n;
      o["ch"] = ((Ctx *)p)->ch;
      o["reason"] = ((Ctx *)p)->why;
    }, &ctx);
  }
}

static void checkRules(bool firstPass) {
  StateLock l;
  uint8_t slots = sessSlots();
  // No session starts on its own after pairing: the admin starts the first one(s) with Renew.

  for (uint8_t s = 0; s < slots; s++) {
    SessionState &ss = g.sess[s];
    // kWh quota
    double kwh = slotEnergy(s) - ss.e0;
    float max = slotKwhMax(s);
    uint8_t act = slotKwhAction(s);
    if (ss.sid && ss.status == SESS_ACTIVE && max > 0 && kwh >= max) {
      static const char *ACT[] = {"end", "hold", "warn"};
      struct QCtx {
        double kwh;
        const char *action;
        int8_t ch;
      } q = {kwh, ACT[act % 3], (int8_t)(indep() ? s : -1)};
      auto fillQuota = [](JsonObject o, void *p) {
        QCtx *c = (QCtx *)p;
        if (c->ch >= 0) o["ch"] = c->ch;
        o["kwh"] = serialized(String(c->kwh, 4));
        o["action"] = c->action;
        o["end"] = strcmp(c->action, "warn") != 0;
      };
      if (act == KWH_WARN) {
        if (!ss.over) {  // warn once, keep the power on
          ss.over = true;
          store::saveSession(s, ss);
          emitEvent("quota", fillQuota, &q);
          bump();
        }
      } else {
        emitEvent("quota", fillQuota, &q);
        if (act == KWH_END) endSession(s, "quota");  // end now, even with time left
        ss.status = SESS_QUOTA;                       // relay off either way
        store::saveSession(s, ss);
        bump();
      }
    }
    // Session time limit (a disabled meter never renews)
    const SessConfig &sc = slotSessCfg(s);
    if (g.disabled) {
      // nothing: no sessions while disabled
    } else if (ss.sid && ss.status != SESS_EXPIRED && ss.status != SESS_ENDED) {
      uint32_t nr = nextReset(s);
      bool due = (nr && timekeep::valid() && timekeep::now() >= nr) || durationElapsedNoClock(s);
      if (due) {
        endSession(s, "expired");
        if (sc.auto_renew) {
          startSession(s, true);
        } else {
          ss.status = SESS_EXPIRED;
          store::saveSession(s, ss);
          bump();
        }
      }
    } else if (ss.sid && ss.status == SESS_EXPIRED && sc.auto_renew) {
      startSession(s, true);  // auto renew was switched on while the session had ended
    }
    // Remember the real start time once the clock becomes valid.
    if (ss.sid && !ss.start && timekeep::valid()) {
      ss.start = timekeep::now() - (millis() - ss.startMs) / 1000;
      store::saveSession(s, ss);
    }
  }

  // Relay decision per channel. Meter-wide blocks first, then the channel's own.
  const char *meterBlock = nullptr;
  if (g.disabled) meterBlock = "disabled";
  else if (g.cfg.tamper_protect && g.cfg.tamper_action == ACT_HOLD && g.tamper) meterBlock = "tamper";
  else if (g.cfg.volt_action == ACT_HOLD && g.voltOut) meterBlock = "voltage";
  else if (g.cfg.sensor_action == ACT_HOLD && g.sensorErr) meterBlock = "sensor";
  else if (g.cfg.temp_action == ACT_HOLD && g.tempHigh) meterBlock = "temp";
  else if (g.paired && g.cfg.offline_off_min && g.offlineSinceMs &&
           millis() - g.offlineSinceMs >= g.cfg.offline_off_min * 60000UL)
    meterBlock = "offline";
  bool anyWant = false;
  for (uint8_t i = 0; i < g.nch; i++) anyWant |= g.ch[i].wantOn;
  if (meterBlock && !strcmp(meterBlock, "offline") && !offlineEventSent_ && anyWant) {
    emitEvent("offline_relay_off");
    offlineEventSent_ = true;
  }
  if (!meterBlock || strcmp(meterBlock, "offline") != 0) offlineEventSent_ = false;

  static uint32_t offSince[MC_MAX_CH] = {0};
  static uint32_t lastWriteMs = 0;
  static bool relayErrSent = false;
  bool changed = firstPass;
  for (uint8_t i = 0; i < g.nch; i++) {
    ChState &c = g.ch[i];
    SessStatus st = g.sess[slotOf(i)].status;
    const char *block = meterBlock;
    if (!block) {
      if (c.tripped) block = "tripped";
      else if (!g.sess[slotOf(i)].sid) block = "no_session";  // first session not started yet (Renew)
      else if (st == SESS_QUOTA) block = "quota";
      else if (st == SESS_EXPIRED) block = "expired";
      else if (st == SESS_ENDED) block = "ended";
    }
    bool on = c.wantOn && block == nullptr;
    // Anti-chatter: OFF is immediate; ON again only after RELAY_MIN_OFF_MS off.
    if (on && !c.relayOn && !firstPass && offSince[i] && millis() - offSince[i] < RELAY_MIN_OFF_MS) {
      on = false;
      if (!block) block = c.block;
    }
    if (on != c.relayOn || block != c.block) {
      if (on != c.relayOn) {
        Serial.printf("[relay] CH%u %s%s%s\n", i + 1, on ? "ON" : "OFF", block ? " - " : "", block ? block : "");
        if (!on) offSince[i] = millis();
        noteRelaySwitch(i, block);
      }
      c.relayOn = on;
      c.block = block;
      changed = true;
      bump();
    }
  }
  // Write the board on a change, and every 2 s anyway (recovers from a glitch on the relay board).
  // AUX relays: only their manual ON / OFF (and OFF while the meter is disabled).
  static uint32_t lastAux = 0xFFFFFFFF;
  uint32_t aux = auxrelay::outputMask();  // AUX relays: manual / schedule / event (auxrelay.cpp)
  if (aux != lastAux) {
    lastAux = aux;
    changed = true;
  }
  if (changed || millis() - lastWriteMs >= 2000) {
    lastWriteMs = millis();
    uint32_t mask = 0;  // bit k = relay k+1 (CHANNELS in config.h says which relay a channel uses)
    for (uint8_t i = 0; i < g.nch && i < hw::NCH; i++)
      if (g.ch[i].relayOn && hw::CHANNEL[i].relay) mask |= 1UL << (hw::CHANNEL[i].relay - 1);
    mask |= aux;
    bool ok = relays::write(mask);
    if (!ok && !relayErrSent) {
      relayErrSent = true;
      Serial.println("[relay] ERROR: relay board not answering on I2C");
      emitEvent("relay_error");
    } else if (ok && relayErrSent) {
      relayErrSent = false;
      emitEvent("relay_ok");
    }
  }
}

// AUX relay automation: live values for the event rules (per channel; kWh balance and session of the
// session that governs the channel: the shared one, or the channel's own in independent mode).
static void auxTick() {
  if (!auxrelay::count()) return;
  auxrelay::Inputs in;
  {
    StateLock l;
    in.nch = g.nch < auxrelay::MAX_CH ? g.nch : auxrelay::MAX_CH;
    in.readOk = g.readOk && g.ac;
    in.disabled = g.disabled;
    for (uint8_t i = 0; i < in.nch; i++) {
      in.a[i] = g.ch[i].r.a;
      in.w[i] = g.ch[i].r.w;
      uint8_t s = slotOf(i);
      const SessionState &ss = g.sess[s];
      in.running[i] = ss.sid && ss.status == SESS_ACTIVE && !g.disabled;
      float max = slotKwhMax(s);
      if (ss.sid && max > 0) {
        double left = max - (slotEnergy(s) - ss.e0);
        in.balance[i] = left > 0 ? (float)left : 0.0f;
      }
    }
  }
  auxrelay::tick(in);
}

static void checkTamper() {
#if TAMPER_ENABLE
  static uint32_t changeSince = 0;
  bool open = digitalRead(PIN_TAMPER) == TAMPER_OPEN_LEVEL;
  StateLock l;
  if (open == g.tamper) {
    changeSince = 0;
    return;
  }
  if (!changeSince) changeSince = millis();
  if (millis() - changeSince < TAMPER_DEBOUNCE_MS) return;  // a long tamper wire picks up noise
  changeSince = 0;
  g.tamper = open;
  if (open && g.cfg.tamper_protect) {
    const char *act = ACTION_NAME[g.cfg.tamper_action % 3];
    emitEvent("tamper_open", [](JsonObject o, void *p) { o["action"] = (const char *)p; }, (void *)act);
    if (g.cfg.tamper_action == ACT_END) stopAll(STOP_TAMPER);
  } else {
    emitEvent(open ? "tamper_open" : "tamper_close");
  }
  bump();
#endif  // TAMPER_ENABLE (no switch fitted: never open)
}

void task(void *) {
  bool first = true;
  {
    StateLock l;  // conditions already present at boot (docs/protocol.md §2.5)
    if (g.tamper) emitEvent("tamper_open", [](JsonObject o, void *) { o["boot"] = 1; });
    for (uint8_t i = 0; i < g.nch; i++)
      if (g.ch[i].tripped) {
        uint8_t ch = i;
        emitEvent("overcurrent", [](JsonObject o, void *p) {
          o["ch"] = *(uint8_t *)p;
          o["boot"] = 1;
        }, &ch);
      }
  }
  uint32_t lastReadMs = 0, lastProbeMs = 0;
  TickType_t last = xTaskGetTickCount();
  for (;;) {
    bool drdy;
    {
      StateLock l;
      drdy = g.cfg.drdy;
    }
    bool fresh = false;
    if (drdy) {
      fresh = xSemaphoreTake(drdySem_, pdMS_TO_TICKS(CONTROL_TICK_MS)) == pdTRUE;  // every 100 ms window
      last = xTaskGetTickCount();
    } else {
      vTaskDelayUntil(&last, pdMS_TO_TICKS(CONTROL_TICK_MS));
    }
    uint32_t now = millis();
    if (!rbamp::present() && now - lastProbeMs >= 5000) {
      lastProbeMs = now;
      sys::crumb(sys::CR_CONTROL, "sensor probe");
      if (rbamp::begin()) sensorFound();
    }
    if (fresh || now - lastReadMs >= SENSOR_READ_MS) {  // without DRDY wired: once a second
      lastReadMs = now;
      sys::crumb(sys::CR_CONTROL, "sensor");
      readSensor();
    }
    sys::crumb(sys::CR_CONTROL, "energy");
    energyTick();
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

    // Normal mode: record a reading every read_s. DRDY mode: every window, packed once a second.
    // Either way the buffered readings are sent every report_s.
    uint32_t readMs, sendMs;
    bool drdyMode;
    {
      StateLock l;
      drdyMode = g.cfg.drdy;
      sendMs = (uint32_t)g.cfg.report_s * 1000UL;
      readMs = g.cfg.read_ms ? g.cfg.read_ms : (uint32_t)(g.cfg.read_s ? g.cfg.read_s : g.cfg.report_s) * 1000UL;
    }
    bool nowReq = reportNow_.exchange(false);
    if (drdyMode) {
      bool due;
      {
        StateLock l;
        due = nFrames_ && (millis() - firstFrameMs_ >= 1000 || nFrames_ >= FRAMES_MAX);
      }
      if (due || nowReq) {
        sys::crumb(sys::CR_CONTROL, "telemetry");
        pushFrames();
      }
    } else if (nowReq || millis() - lastReportMs_ >= readMs) {
      lastReportMs_ = millis();
      sys::crumb(sys::CR_CONTROL, "telemetry");
      pushTelemetry();
    }
    if (nowReq || millis() - lastSendReqMs_ >= sendMs) {
      lastSendReqMs_ = millis();
      sendReq_ = true;
    }
  }
}

}  // namespace control
