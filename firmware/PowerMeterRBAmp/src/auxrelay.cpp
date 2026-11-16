#include "auxrelay.h"

#include <Preferences.h>

#include "app_state.h"
#include "control.h"
#include "hwmap.h"
#include "temp.h"
#include "timekeep.h"

namespace auxrelay {

enum Mode : uint8_t { M_MANUAL = 0, M_SCHEDULE = 1, M_EVENT = 2 };
enum Src : uint8_t {
  S_TEMP = 0, S_HUM = 1, S_AMP = 2, S_WATT = 3, S_KWH = 4, S_SESSION = 5,
  S_TAMPER = 6, S_TRIP = 7, S_VOLT = 8, S_WIFI = 9, S_SERVER = 10, S_UPS = 11, S_BAT = 12, S_COUNT = 13
};
static const char *MODE_NAME[] = {"manual", "schedule", "event"};
static const char *SRC_NAME[] = {"temp", "hum", "amp", "watt", "kwh", "session", "tamper", "trip", "volt", "wifi", "server", "ups", "bat"};
static const float HYST[] = {1.0f, 2.0f, 0.1f, 10.0f, 0.01f, 0, 0, 0, 0, 0, 0, 0, 2.0f};  // a condition clears this far back
static const uint32_t MIN_SWITCH_MS = 5000;                      // event mode: at most one change per 5 s
static const uint8_t TOTAL = 255;                                 // amp / watt: all channels together
static const uint8_t ANY = 254;                                   // any channel (amp, watt, kWh, session, trip)

struct Sched {
  uint8_t used = 0, on = 1, days = 0x7F;  // days: bit 0 = Monday ... bit 6 = Sunday; 0 = by date (below)
  uint8_t mon = 0, day = 0;               // by date: month 1..12, day 1..31
  uint16_t year = 0;                      // by date: year, 0 = every year
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
  uint8_t version = 2;
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
    if (active.version != 2) active = Blob();  // other layout: defaults, the server re-sends the config
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
  for (uint8_t k = 0; k < S_COUNT; k++)
    if (!strcmp(s, SRC_NAME[k])) return k;
  return -1;
}

bool parse(JsonArrayConst list, int tzMin, const char *&err) {
  static Blob b;  // too big for the stack
  b = Blob();
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
      JsonArrayConst date = s["date"];  // [year (0 = every year), month, day]: this date instead of week days
      int y = date[0] | 0, mo = date[1] | 0, da = date[2] | 0;
      if (!date.isNull()) days = 0;
      if (st < 0 || st > 1439 || en < 0 || en > 1439 || days < 0 || days > 0x7F ||
          (date.isNull() ? days < 1 : (mo < 1 || mo > 12 || da < 1 || da > 31 || y < 0 || (y && (y < 2020 || y > 2199))))) {
        err = "aux.sched";
        return false;
      }
      c.s[k].year = y;
      c.s[k].mon = mo;
      c.s[k].day = da;
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
      if (src < 0 || idx < 0 || (idx >= MAX_CH && idx != TOTAL && idx != ANY)) {
        err = "aux.event";
        return false;
      }
      c.ev.src = src;
      c.ev.idx = idx;
      c.ev.over = strcmp(e["cmp"] | "over", "under") != 0;
      c.ev.value = e["value"] | 0.0f;
      c.ev.running = e["active"] | (e["running"] | true);  // session: running; tamper, trip, ...: active
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

// Year / month / day of a day number (days since 1970-01-01).
static void civil(int32_t z, int &y, int &m, int &d) {
  z += 719468;
  int32_t era = (z >= 0 ? z : z - 146096) / 146097;
  uint32_t doe = (uint32_t)(z - era * 146097);
  uint32_t yoe = (doe - doe / 1460 + doe / 36524 - doe / 146096) / 365;
  uint32_t doy = doe - (365 * yoe + yoe / 4 - yoe / 100);
  uint32_t mp = (5 * doy + 2) / 153;
  d = (int)(doy - (153 * mp + 2) / 5 + 1);
  m = (int)(mp < 10 ? mp + 3 : mp - 9);
  y = (int)(yoe + era * 400 + (m <= 2));
}

// Does the window start on this local day (day number since 1970)? By week day, or by date.
static bool dayOn(const Sched &s, int32_t dayNo) {
  if (s.days) return s.days & (1u << ((dayNo + 3) % 7));  // 1970-01-01 was a Thursday; bit 0 = Monday
  int y, m, d;
  civil(dayNo, y, m, d);
  return m == s.mon && d == s.day && (!s.year || y == s.year);
}

static bool evalSchedule(const Cfg &c, bool &st) {
  if (!timekeep::valid()) return false;  // no clock yet: keep the last state
  int64_t local = (int64_t)timekeep::now() + (int64_t)active.tz * 60;
  int m = (int)((local % 86400 + 86400) % 86400) / 60;
  int32_t d = (int32_t)(local / 86400);  // local day number
  for (uint8_t k = 0; k < SCHED_MAX; k++) {
    const Sched &s = c.s[k];
    if (!s.used) continue;
    int end = s.end == 1439 ? 1440 : s.end;  // "until 23:59" = to the end of the day
    bool hit;
    if (s.start == end) hit = dayOn(s, d);                                     // whole day
    else if (s.start < end) hit = dayOn(s, d) && m >= s.start && m < end;
    else hit = (m >= s.start && dayOn(s, d)) || (m < end && dayOn(s, d - 1));  // over midnight
    if (hit) {
      st = s.on;
      return true;
    }
  }
  st = c.elseOn;
  return true;
}

// One channel's value for a numeric rule (NAN = no value now).
static float channelValue(uint8_t src, uint8_t i, const Inputs &in) {
  if (i >= in.nch) return NAN;
  if (src == S_KWH) return in.balance[i];
  if (!in.readOk) return NAN;
  return src == S_AMP ? in.a[i] : in.w[i];
}

static bool compare(const Rule &r, bool latched, float v) {
  float h = HYST[r.src % S_COUNT];
  return r.over ? (latched ? v > r.value - h : v >= r.value) : (latched ? v < r.value + h : v <= r.value);
}

static bool evalEvent(const Cfg &c, Run &rn, const Inputs &in, bool &st) {
  const Rule &r = c.ev;
  bool cond = false;
  switch (r.src) {
    case S_TEMP:
    case S_HUM: {
      float v = r.src == S_TEMP ? temp::value(r.idx) : temp::humidity(r.idx);
      if (isnan(v)) return false;  // sensor missing: keep the last state
      cond = compare(r, rn.latched, v);
      break;
    }
    case S_AMP:
    case S_WATT:
    case S_KWH: {
      if (r.idx == ANY || r.idx == TOTAL) {  // any channel meets it / all channels together (amp, watt)
        bool some = false, any = false;
        float total = 0;
        for (uint8_t i = 0; i < in.nch; i++) {
          float v = channelValue(r.src, i, in);
          if (isnan(v)) continue;
          some = true;
          total += v;
          if (compare(r, rn.latched, v)) any = true;
        }
        if (!some) return false;
        cond = r.idx == ANY ? any : compare(r, rn.latched, total);
      } else {
        float v = channelValue(r.src, r.idx, in);
        if (isnan(v)) return false;  // no reading / no kWh limit: keep the last state
        cond = compare(r, rn.latched, v);
      }
      break;
    }
    case S_SESSION:
    case S_TRIP: {
      const bool *v = r.src == S_SESSION ? in.running : in.tripped;
      bool state = false;
      if (r.idx == ANY) {
        for (uint8_t i = 0; i < in.nch; i++) state = state || v[i];
      } else if (r.idx < in.nch) {
        state = v[r.idx];
      } else {
        return false;
      }
      cond = state == (bool)r.running;
      break;
    }
    case S_TAMPER: cond = in.tamper == (bool)r.running; break;
    case S_VOLT: cond = in.voltOut == (bool)r.running; break;
    case S_WIFI: cond = in.noWifi == (bool)r.running; break;
    case S_SERVER: cond = in.noServer == (bool)r.running; break;
    case S_UPS: cond = in.ups == (bool)r.running; break;
    case S_BAT:
      if (isnan(in.batPct)) return false;  // no battery measurement: keep the last state
      cond = compare(r, rn.latched, in.batPct);
      break;
    default: return false;
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
