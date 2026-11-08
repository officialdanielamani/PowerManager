#include "auxrelay.h"

#include <Preferences.h>

#include "app_state.h"
#include "control.h"
#include "hwmap.h"
#include "temp.h"
#include "timekeep.h"

namespace auxrelay {

enum Mode : uint8_t { M_MANUAL = 0, M_SCHEDULE = 1, M_EVENT = 2 };
enum Src : uint8_t { S_TEMP = 0, S_HUM = 1, S_AMP = 2, S_WATT = 3, S_KWH = 4, S_SESSION = 5 };
static const char *MODE_NAME[] = {"manual", "schedule", "event"};
static const char *SRC_NAME[] = {"temp", "hum", "amp", "watt", "kwh", "session"};
static const float HYST[] = {1.0f, 2.0f, 0.1f, 10.0f, 0.01f, 0};  // a condition clears this far back
static const uint32_t MIN_SWITCH_MS = 5000;                      // event mode: at most one change per 5 s
static const uint8_t TOTAL = 255;                                 // amp / watt: all channels together

struct Sched {
  uint8_t used = 0, on = 1, days = 0x7F;  // days: bit 0 = Monday ... bit 6 = Sunday
  uint16_t start = 0, end = 0;            // minutes of the local day; end 1439 (23:59) = end of the day
};
struct Rule {
  uint8_t src = S_TEMP, idx = 0, over = 1, on = 1, running = 1;
  float value = 0;
};
struct Cfg {
  char name[21] = "";
  uint8_t mode = M_MANUAL, override = 0, elseOn = 0;
  Sched s[SCHED_MAX];
  Rule ev;
};
struct Blob {
  uint8_t version = 1;
  int16_t tz = 0;  // local time = UTC + tz minutes (sent by the server, which knows the time zone)
  Cfg c[MAX];
};

struct Run {
  bool want = false;      // manual state (persisted)
  bool autoState = false, autoKnown = false;
  bool ovr = false, ovrState = false;  // manual override of the automation
  bool out = false;       // what the relay does now
  bool latched = false;   // event condition currently true
  uint32_t lastAutoMs = 0;
  const char *src = nullptr;  // who caused the next change (manual commands)
};

static Blob active, staged;
static Run run[MAX];
static bool disabledNow = false;
static Preferences prefs;

uint8_t count() { return hw::AUX < MAX ? hw::AUX : MAX; }
const char *name(uint8_t i) { return i < MAX ? active.c[i].name : ""; }

static void save() {
  prefs.begin("aux", false);
  prefs.putBytes("cfg", &active, sizeof(active));
  prefs.end();
}

static void saveWant() {
  uint16_t m = 0;
  for (uint8_t i = 0; i < count(); i++)
    if (run[i].want) m |= 1u << i;
  prefs.begin("aux", false);
  if (prefs.getUShort("want", 0) != m || !prefs.isKey("want")) prefs.putUShort("want", m);
  prefs.end();
}

void begin() {
  prefs.begin("aux", true);
  if (prefs.getBytesLength("cfg") == sizeof(Blob)) {
    prefs.getBytes("cfg", &active, sizeof(active));
    if (active.version != 1) active = Blob();
  }
  uint16_t want = prefs.getUShort("want", 0);
  prefs.end();
  for (uint8_t i = 0; i < count(); i++) run[i].want = want & (1u << i);
  staged = active;
}

void wipe() {
  prefs.begin("aux", false);
  prefs.clear();
  prefs.end();
}

// ------------------------------------------------------------------ config from the server

static int srcOf(const char *s) {
  for (uint8_t k = 0; k < 6; k++)
    if (!strcmp(s, SRC_NAME[k])) return k;
  return -1;
}

bool parse(JsonArrayConst list, int tzMin, const char *&err) {
  Blob b;
  if (tzMin < -840 || tzMin > 840) {
    err = "tz_min";
    return false;
  }
  b.tz = tzMin;
  for (JsonObjectConst a : list) {
    int i = a["i"] | -1;
    if (i < 0 || i >= MAX) continue;
    Cfg &c = b.c[i];
    strlcpy(c.name, a["name"] | "", sizeof(c.name));
    const char *mode = a["mode"] | "manual";
    c.mode = !strcmp(mode, "schedule") ? M_SCHEDULE : !strcmp(mode, "event") ? M_EVENT : M_MANUAL;
    c.override = a["override"] | false;
    c.elseOn = !strcmp(a["else"] | "off", "on");
    uint8_t k = 0;
    for (JsonObjectConst s : a["sched"].as<JsonArrayConst>()) {
      if (k >= SCHED_MAX) break;
      int st = s["start"] | -1, en = s["end"] | -1, days = s["days"] | 0x7F;
      if (st < 0 || st > 1439 || en < 0 || en > 1439 || days < 1 || days > 0x7F) {
        err = "aux.sched";
        return false;
      }
      c.s[k].used = 1;
      c.s[k].start = st;
      c.s[k].end = en;
      c.s[k].days = days;
      c.s[k].on = s["on"] | true;
      k++;
    }
    JsonObjectConst e = a["event"];
    if (!e.isNull()) {
      int src = srcOf(e["src"] | "");
      int idx = e["idx"] | 0;
      if (src < 0 || idx < 0 || (idx >= MAX_CH && idx != TOTAL)) {
        err = "aux.event";
        return false;
      }
      c.ev.src = src;
      c.ev.idx = idx;
      c.ev.over = strcmp(e["cmp"] | "over", "under") != 0;
      c.ev.value = e["value"] | 0.0f;
      c.ev.running = e["running"] | true;
      c.ev.on = e["on"] | true;
    } else if (c.mode == M_EVENT) {
      err = "aux.event";
      return false;
    }
  }
  staged = b;
  return true;
}

void commit() {
  StateLock l;
  for (uint8_t i = 0; i < MAX; i++)
    if (memcmp(&staged.c[i].mode, &active.c[i].mode, sizeof(Cfg) - sizeof(Cfg::name))) {
      run[i].autoKnown = run[i].ovr = run[i].latched = false;  // automation changed: start fresh
      run[i].lastAutoMs = 0;
    }
  bool changed = memcmp(&staged, &active, sizeof(Blob)) != 0;
  active = staged;
  if (changed) save();
}

// ------------------------------------------------------------------ automation

static bool dayOn(uint8_t days, int d) { return days & (1u << ((d + 7) % 7)); }

static bool evalSchedule(const Cfg &c, bool &st) {
  if (!timekeep::valid()) return false;  // no clock yet: keep the last state
  int64_t local = (int64_t)timekeep::now() + (int64_t)active.tz * 60;
  int m = (int)((local % 86400 + 86400) % 86400) / 60;
  int d = (int)(((local / 86400) + 3) % 7);  // 1970-01-01 was a Thursday; 0 = Monday
  for (uint8_t k = 0; k < SCHED_MAX; k++) {
    const Sched &s = c.s[k];
    if (!s.used) continue;
    int end = s.end == 1439 ? 1440 : s.end;  // "until 23:59" = to the end of the day
    bool hit;
    if (s.start == end) hit = dayOn(s.days, d);                                          // whole day
    else if (s.start < end) hit = dayOn(s.days, d) && m >= s.start && m < end;
    else hit = (m >= s.start && dayOn(s.days, d)) || (m < end && dayOn(s.days, d - 1));  // over midnight
    if (hit) {
      st = s.on;
      return true;
    }
  }
  st = c.elseOn;
  return true;
}

static float valueOf(const Rule &r, const Inputs &in, bool &known) {
  known = true;
  switch (r.src) {
    case S_TEMP: return temp::value(r.idx);
    case S_HUM: return temp::humidity(r.idx);
    case S_AMP:
    case S_WATT: {
      if (!in.readOk) return NAN;
      const float *v = r.src == S_AMP ? in.a : in.w;
      if (r.idx != TOTAL) return r.idx < in.nch ? v[r.idx] : NAN;
      float t = 0;
      for (uint8_t i = 0; i < in.nch; i++) t += v[i];
      return t;
    }
    case S_KWH: return r.idx < in.nch ? in.balance[r.idx] : NAN;
    default: known = false; return NAN;
  }
}

static bool evalEvent(const Cfg &c, Run &rn, const Inputs &in, bool &st) {
  const Rule &r = c.ev;
  bool cond;
  if (r.src == S_SESSION) {
    if (r.idx >= in.nch) return false;
    cond = in.running[r.idx] == (bool)r.running;
  } else {
    bool known;
    float v = valueOf(r, in, known);
    if (!known || isnan(v)) return false;  // no value (sensor missing, no reading): keep the last state
    float h = HYST[r.src % 6];
    cond = r.over ? (rn.latched ? v > r.value - h : v >= r.value) : (rn.latched ? v < r.value + h : v <= r.value);
  }
  rn.latched = cond;
  st = cond ? r.on : !r.on;
  return true;
}

void tick(const Inputs &in) {
  StateLock l;
  disabledNow = in.disabled;
  uint32_t now = millis();
  for (uint8_t i = 0; i < count(); i++) {
    const Cfg &c = active.c[i];
    Run &rn = run[i];
    const char *why = rn.src;
    if (c.mode != M_MANUAL) {
      bool st = rn.autoState;
      bool ok = c.mode == M_SCHEDULE ? evalSchedule(c, st) : evalEvent(c, rn, in, st);
      if (ok && (!rn.autoKnown || st != rn.autoState)) {
        if (c.mode == M_EVENT && rn.autoKnown && rn.lastAutoMs && now - rn.lastAutoMs < MIN_SWITCH_MS) {
          // anti-chatter: wait
        } else {
          if (rn.autoKnown) rn.ovr = false;  // the automation changed state: a manual override ends here
          rn.autoState = st;
          rn.autoKnown = true;
          rn.lastAutoMs = now;
          if (!why) why = MODE_NAME[c.mode];
        }
      }
    }
    bool out = in.disabled ? false : c.mode == M_MANUAL ? rn.want : rn.ovr ? rn.ovrState : (rn.autoKnown && rn.autoState);
    if (in.disabled && rn.out && !why) why = "disabled";
    if (out != rn.out) {
      rn.out = out;
      struct Ctx {
        uint8_t i, relay;
        bool on;
        const char *src;
      } ctx = {i, hw::auxRelay(i), out, why ? why : MODE_NAME[c.mode]};
      control::emitEvent("aux_relay", [](JsonObject o, void *p) {
        Ctx *x = (Ctx *)p;
        o["i"] = x->i;
        o["relay"] = x->relay;
        o["on"] = x->on;
        o["src"] = x->src;
      }, &ctx);
      Serial.printf("[relay] AUX%u (relay %u) %s - %s\n", i + 1, ctx.relay, out ? "ON" : "OFF", ctx.src);
      control::bumpState();
    }
    rn.src = nullptr;
  }
}

uint32_t outputMask() {
  StateLock l;
  uint32_t m = 0;
  for (uint8_t i = 0; i < count(); i++)
    if (run[i].out) m |= 1UL << (hw::auxRelay(i) - 1);
  return m;
}

bool set(uint8_t i, bool on, const char *src, const char *&err) {
  StateLock l;
  err = nullptr;
  if (i >= count()) err = "bad_aux";
  else if (on && disabledNow) err = "disabled";
  else if (active.c[i].mode != M_MANUAL && !active.c[i].override) err = "automation";  // override not allowed
  if (err) return false;
  Run &rn = run[i];
  if (active.c[i].mode == M_MANUAL) {
    if (rn.want != on) {
      rn.want = on;
      saveWant();
    }
  } else {
    rn.ovr = true;  // until the automation changes state
    rn.ovrState = on;
  }
  rn.src = src;
  control::bumpState();
  return true;
}

void stateJson(JsonArray a) {
  StateLock l;
  for (uint8_t i = 0; i < count(); i++) {
    JsonObject o = a.add<JsonObject>();
    o["i"] = i;
    o["relay"] = hw::auxRelay(i);
    o["on"] = run[i].out;
    o["mode"] = MODE_NAME[active.c[i].mode % 3];
    if (active.c[i].mode != M_MANUAL) {
      o["auto"] = run[i].autoKnown && run[i].autoState;
      if (run[i].ovr) o["override"] = true;
      o["can_override"] = (bool)active.c[i].override;
    }
  }
}

void configJson(JsonArray a) {
  StateLock l;
  for (uint8_t i = 0; i < count(); i++) {
    const Cfg &c = active.c[i];
    JsonObject o = a.add<JsonObject>();
    o["name"] = c.name;
    o["mode"] = MODE_NAME[c.mode % 3];
  }
}

}  // namespace auxrelay
