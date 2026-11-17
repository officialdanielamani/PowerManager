#include "hwprofile.h"

#include <Preferences.h>

#ifndef HW_CONFIRM_MS
#define HW_CONFIRM_MS 60000UL
#endif
#define HWP_DELAY_MIN 10
#define HWP_DELAY_MAX 600
#define HWP_DELAY_DEFAULT 60
#define HWP_UNCONFIRMED_BOOTS 2  // a trial profile may start this many times without being confirmed

namespace hwp {

// ---------------------------------------------------------------- tables: names / codes

// SCT-013 model codes of the rbAmp module (fixed by its register map) <-> amps in the protocol.
static const struct { uint8_t code; uint8_t amps; } CLAMP[] = {{0, 0}, {1, 5}, {2, 10}, {6, 20}, {3, 30}, {4, 50}, {7, 60}, {5, 100}};
static const struct { uint8_t drv; const char *name; uint8_t maxOut; } DRV[] = {
    {HWP_DRV_MCP23017, "mcp23017", 16}, {HWP_DRV_XL9535, "xl9535", 16}, {HWP_DRV_PCF8574, "pcf8574", 8}, {HWP_DRV_GPIO, "gpio", 16}};

static int ampsOf(uint8_t code) {
  for (auto &c : CLAMP)
    if (c.code == code) return c.amps;
  return -1;
}
static int codeOf(int amps) {
  for (auto &c : CLAMP)
    if (c.amps == amps) return c.code;
  return -1;
}
static const char *driverName(uint8_t d) {
  for (auto &x : DRV)
    if (x.drv == d) return x.name;
  return "";
}
static int driverOf(const char *name) {
  for (auto &x : DRV)
    if (!strcmp(x.name, name)) return x.drv;
  return -1;
}
static int maxOutputs(uint8_t d) {
  for (auto &x : DRV)
    if (x.drv == d) return x.maxOut;
  return 0;
}

static void fail(char *err, size_t n, const char *fmt, int a = 0, int b = 0) {
  if (err && n) snprintf(err, n, fmt, a, b);
}

// ---------------------------------------------------------------- rules

int totalRelays(const Profile &p) {
  int t = 0;
  for (uint8_t i = 0; i < p.nRelayBoards; i++) t += p.relay[i].relays;
  return t;
}

static bool addrOk(uint8_t drv, uint8_t a) {
  if (drv == HWP_DRV_GPIO) return a == 0;
  if (drv == HWP_DRV_PCF8574) return (a >= 0x20 && a <= 0x27) || (a >= 0x38 && a <= 0x3F);
  return a >= 0x20 && a <= 0x27;
}

bool validate(const Profile &p, uint8_t gpioPins, char *err, size_t n) {
  if (p.nBoards < 1 || p.nBoards > HWP_MAX_BOARDS) return fail(err, n, "rbAmp boards: 1 to %d", HWP_MAX_BOARDS), false;
  for (uint8_t i = 0; i < p.nBoards; i++) {
    if (p.addr[i] < 0x50 || p.addr[i] > 0x57) return fail(err, n, "rbAmp board %d: address must be 0x50 to 0x57", i + 1), false;
    for (uint8_t j = 0; j < i; j++)
      if (p.addr[j] == p.addr[i]) return fail(err, n, "rbAmp board %d: address used twice", i + 1), false;
  }
  if (p.nRelayBoards > HWP_MAX_RELAY_BOARDS) return fail(err, n, "relay boards: at most %d", HWP_MAX_RELAY_BOARDS), false;
  int total = 0, gpio = 0;
  for (uint8_t i = 0; i < p.nRelayBoards; i++) {
    const RelayBoard &r = p.relay[i];
    if (!maxOutputs(r.driver)) return fail(err, n, "relay board %d: unknown driver", i + 1), false;
    if (!addrOk(r.driver, r.address)) return fail(err, n, "relay board %d: invalid I2C address", i + 1), false;
    if (r.relays < 1 || r.relays > maxOutputs(r.driver)) return fail(err, n, "relay board %d: 1 to %d outputs", i + 1, maxOutputs(r.driver)), false;
    if (r.driver != HWP_DRV_GPIO) {
      for (uint8_t j = 0; j < p.nBoards; j++)
        if (p.addr[j] == r.address) return fail(err, n, "relay board %d: address used twice", i + 1), false;
      for (uint8_t j = 0; j < i; j++)
        if (p.relay[j].driver != HWP_DRV_GPIO && p.relay[j].address == r.address)
          return fail(err, n, "relay board %d: address used twice", i + 1), false;
    }
    total += r.relays;
    if (r.driver == HWP_DRV_GPIO) gpio += r.relays;
  }
  if (total > HWP_MAX_RELAYS) return fail(err, n, "at most %d relays in total", HWP_MAX_RELAYS), false;
  if (gpio > gpioPins) return fail(err, n, "this build has %d GPIO relay pin(s)", gpioPins), false;
  if (p.nCh < 1 || p.nCh > HWP_MAX_CH) return fail(err, n, "channels: 1 to %d", HWP_MAX_CH), false;
  for (uint8_t i = 0; i < p.nCh; i++) {
    const Channel &c = p.ch[i];
    if (c.board < 1 || c.board > p.nBoards) return fail(err, n, "CH%d: invalid board", i + 1), false;
    if (c.input < 1 || c.input > 7) return fail(err, n, "CH%d: input must be 1 to 7", i + 1), false;
    if (ampsOf(c.clamp) < 0) return fail(err, n, "CH%d: invalid clamp model", i + 1), false;
    if (c.relay > total) return fail(err, n, "CH%d: relay must be 0 to %d", i + 1, total), false;
    for (uint8_t j = 0; j < i; j++) {
      if (p.ch[j].board == c.board && p.ch[j].input == c.input) return fail(err, n, "CH%d: input used twice", i + 1), false;
      if (c.relay && p.ch[j].relay == c.relay) return fail(err, n, "CH%d: relay used twice", i + 1), false;
    }
  }
  return true;
}

bool sameStructure(const Profile &a, const Profile &b) {
  if (a.nBoards != b.nBoards || a.nRelayBoards != b.nRelayBoards || a.nCh != b.nCh) return false;
  for (uint8_t i = 0; i < a.nBoards; i++)
    if (a.addr[i] != b.addr[i]) return false;
  for (uint8_t i = 0; i < a.nRelayBoards; i++)
    if (a.relay[i].driver != b.relay[i].driver || a.relay[i].address != b.relay[i].address ||
        a.relay[i].relays != b.relay[i].relays || a.relay[i].activeHigh != b.relay[i].activeHigh)
      return false;
  for (uint8_t i = 0; i < a.nCh; i++)
    if (a.ch[i].board != b.ch[i].board || a.ch[i].input != b.ch[i].input || a.ch[i].relay != b.ch[i].relay) return false;
  return true;
}

// ---------------------------------------------------------------- JSON

static bool intIn(JsonVariantConst v, int lo, int hi, int &out) {
  if (!v.is<int>()) return false;
  int x = v.as<int>();
  if (x < lo || x > hi) return false;
  out = x;
  return true;
}

bool fromJson(JsonVariantConst j, Profile &p, char *err, size_t n) {
  p = Profile();
  JsonArrayConst boards = j["boards"], relays = j["relays"], chans = j["ch"];
  if (boards.isNull() || relays.isNull() || chans.isNull()) return fail(err, n, "profile: boards, relays and ch are required"), false;
  if (boards.size() > HWP_MAX_BOARDS || relays.size() > HWP_MAX_RELAY_BOARDS || chans.size() > HWP_MAX_CH)
    return fail(err, n, "profile: too many entries"), false;
  for (JsonVariantConst b : boards) {
    int a;
    if (!intIn(b, 0, 255, a)) return fail(err, n, "boards: addresses are numbers"), false;
    p.addr[p.nBoards++] = a;
  }
  for (JsonObjectConst r : relays) {
    int drv = driverOf(r["drv"] | ""), addr, cnt;
    if (drv < 0 || !intIn(r["addr"], 0, 255, addr) || !intIn(r["n"], 0, 255, cnt))
      return fail(err, n, "relays: drv, addr and n are required"), false;
    p.relay[p.nRelayBoards++] = {(uint8_t)drv, (uint8_t)addr, (uint8_t)cnt, (uint8_t)((r["hi"] | 1) ? 1 : 0)};
  }
  for (JsonObjectConst c : chans) {
    int b, i, a, rl;
    if (!intIn(c["b"], 0, 255, b) || !intIn(c["i"], 0, 255, i) || !intIn(c["r"], 0, 255, rl))
      return fail(err, n, "ch: b, i and r are required"), false;
    int amps = c["a"] | 0;
    a = codeOf(amps);
    if (a < 0) return fail(err, n, "ch: invalid clamp model"), false;
    p.ch[p.nCh++] = {(uint8_t)b, (uint8_t)i, (uint8_t)a, (uint8_t)rl};
  }
  return true;
}

void toJson(const Profile &p, JsonObject o) {
  JsonArray b = o["boards"].to<JsonArray>();
  for (uint8_t i = 0; i < p.nBoards; i++) b.add(p.addr[i]);
  JsonArray r = o["relays"].to<JsonArray>();
  for (uint8_t i = 0; i < p.nRelayBoards; i++) {
    JsonObject x = r.add<JsonObject>();
    x["drv"] = driverName(p.relay[i].driver);
    x["addr"] = p.relay[i].address;
    x["n"] = p.relay[i].relays;
    x["hi"] = p.relay[i].activeHigh ? 1 : 0;
  }
  JsonArray c = o["ch"].to<JsonArray>();
  for (uint8_t i = 0; i < p.nCh; i++) {
    JsonObject x = c.add<JsonObject>();
    x["b"] = p.ch[i].board;
    x["i"] = p.ch[i].input;
    x["a"] = ampsOf(p.ch[i].clamp);
    x["r"] = p.ch[i].relay;
  }
}

// ---------------------------------------------------------------- storage (NVS namespace "pm": a factory reset clears it)

static const char *K_ACT = "hw_act", *K_TRY = "hw_try", *K_BOOTS = "hw_boots", *K_NOTE = "hw_note";
static const size_t BUF = 1400;

static bool load(const char *key, Profile &p) {
  Preferences pr;
  pr.begin("pm", true);
  size_t len = pr.getBytesLength(key);
  bool ok = false;
  if (len > 0 && len < BUF) {
    char buf[BUF];
    pr.getBytes(key, buf, len);
    JsonDocument doc;
    char err[48];
    if (!deserializeJson(doc, buf, len)) {
      ok = fromJson(doc.as<JsonVariantConst>(), p, err, sizeof(err));
      p.ver = doc["ver"] | 0;
    }
  }
  pr.end();
  return ok;
}

static void save(const char *key, const Profile &p) {
  JsonDocument doc;
  doc["ver"] = p.ver;
  toJson(p, doc.as<JsonObject>());
  char buf[BUF];
  size_t len = serializeJson(doc, buf, sizeof(buf));
  Preferences pr;
  pr.begin("pm", false);
  pr.putBytes(key, buf, len);
  pr.end();
}

static void drop(const char *key) {
  Preferences pr;
  pr.begin("pm", false);
  pr.remove(key);
  pr.end();
}

static void putNote(uint32_t ver, const char *reason, int addr) {
  JsonDocument doc;
  doc["ver"] = ver;
  doc["reason"] = reason;
  doc["addr"] = addr;
  char buf[96];
  size_t len = serializeJson(doc, buf, sizeof(buf));
  Preferences pr;
  pr.begin("pm", false);
  pr.putBytes(K_NOTE, buf, len);
  pr.end();
}

static uint8_t getBoots() {
  Preferences pr;
  pr.begin("pm", true);
  uint8_t b = pr.getUChar(K_BOOTS, 0);
  pr.end();
  return b;
}
static void setBoots(uint8_t b) {
  Preferences pr;
  pr.begin("pm", false);
  if (b) pr.putUChar(K_BOOTS, b);
  else pr.remove(K_BOOTS);
  pr.end();
}

// ---------------------------------------------------------------- state

static Profile cur;            // the profile in use
static Profile trial;          // trial profile (state TRIAL: in use, not confirmed; PENDING: stored for the restart)
static Profile dflt;
static Hooks hk = {};
static uint8_t gpioPins = 0;
static State st = ST_DEFAULT;
static uint32_t dueMs = 0, bootMs = 0, lastProbeMs = 0;
static bool noteDone = false;

const Profile &active() { return cur; }
State state() { return st; }
bool rebootPending() { return st == ST_PENDING; }

static uint32_t nowMs() { return hk.millis ? hk.millis() : millis(); }
static void ev(const char *name, uint32_t ver, int inS = 0, const char *reason = nullptr, int addr = -1) {
  if (hk.event) hk.event(name, ver, inS, reason, addr);
}

void begin(const Profile &compiledDefault, uint8_t gpio, const Hooks &hooks) {
  dflt = compiledDefault;
  dflt.ver = 0;
  gpioPins = gpio;
  hk = hooks;
  st = ST_DEFAULT;
  cur = dflt;
  noteDone = false;
  bootMs = nowMs();
  char err[64];

  Profile act;
  bool haveAct = load(K_ACT, act) && validate(act, gpioPins, err, sizeof(err));
  if (haveAct) {
    cur = act;
    st = ST_ACTIVE;
  }
  Profile t;
  Preferences pr;
  pr.begin("pm", true);
  bool hasTry = pr.getBytesLength(K_TRY) > 0;
  pr.end();
  if (hasTry) {
    bool ok = load(K_TRY, t) && validate(t, gpioPins, err, sizeof(err));
    uint8_t boots = getBoots() + 1;
    if (!ok) {
      putNote(0, "invalid", -1);
      drop(K_TRY);
      setBoots(0);
    } else if (boots > HWP_UNCONFIRMED_BOOTS) {
      // Started with it more often than allowed without being confirmed: crashes / resets. Roll back.
      putNote(t.ver, "reset", -1);
      drop(K_TRY);
      setBoots(0);
    } else {
      setBoots(boots);
      trial = t;
      cur = t;
      st = ST_TRIAL;
    }
  }
  Serial.printf("[hwp] profile in use: %s (v%u)\n", st == ST_TRIAL ? "TRIAL" : st == ST_ACTIVE ? "from the server" : "compiled default",
                (unsigned)cur.ver);
}

void stateJson(JsonObject o) {
  o["ver"] = cur.ver;
  o["state"] = st == ST_DEFAULT ? "default" : st == ST_ACTIVE ? "active" : st == ST_PENDING ? "pending" : "trial";
  toJson(cur, o["profile"].to<JsonObject>());
}

// ---------------------------------------------------------------- commands

bool onProfile(JsonObjectConst msg, const char *&err, char *detail, size_t dn, const char *&resState, int &inS) {
  err = nullptr;
  if (detail && dn) detail[0] = 0;
  if (st == ST_TRIAL) {
    err = "busy";  // still being confirmed: wait for the result
    snprintf(detail, dn, "trial profile not confirmed yet");
    return false;
  }
  Profile p;
  char e[64] = "";
  if (!fromJson(msg["profile"], p, e, sizeof(e)) || !validate(p, gpioPins, e, sizeof(e))) {
    err = "invalid";
    if (detail && dn) snprintf(detail, dn, "%s", e);
    return false;
  }
  p.ver = msg["ver"] | 0;
  if (!p.ver) {
    err = "invalid";
    snprintf(detail, dn, "ver is required");
    return false;
  }
  const char *mode = msg["mode"] | "reboot";
  if (!strcmp(mode, "live")) {
    if (!sameStructure(cur, p)) {
      err = "mode";
      snprintf(detail, dn, "more than the clamp models changed");
      return false;
    }
    for (uint8_t i = 0; i < p.nCh; i++) cur.ch[i].clamp = p.ch[i].clamp;
    cur.ver = p.ver;
    save(K_ACT, cur);
    if (st == ST_DEFAULT) st = ST_ACTIVE;
    if (hk.applyClamps) hk.applyClamps();
    resState = "live";
    inS = 0;
    return true;
  }
  int delay = msg["delay_s"] | HWP_DELAY_DEFAULT;
  if (delay < HWP_DELAY_MIN) delay = HWP_DELAY_MIN;
  if (delay > HWP_DELAY_MAX) delay = HWP_DELAY_MAX;
  save(K_TRY, p);  // a newer profile simply replaces a pending one
  trial = p;
  st = ST_PENDING;
  dueMs = nowMs() + (uint32_t)delay * 1000UL;
  resState = "pending";
  inS = delay;
  ev("hw_reboot_pending", p.ver, delay);
  return true;
}

bool onCancel() {
  if (st != ST_PENDING) return false;
  drop(K_TRY);
  setBoots(0);
  st = ST_ACTIVE;
  if (cur.ver == 0) st = ST_DEFAULT;
  return true;
}

// ---------------------------------------------------------------- tick

static bool allPartsAnswer(int &missing) {
  missing = -1;
  if (!hk.probe) return true;
  for (uint8_t i = 0; i < cur.nBoards; i++)
    if (!hk.probe(cur.addr[i], true)) {
      missing = cur.addr[i];
      return false;
    }
  for (uint8_t i = 0; i < cur.nRelayBoards; i++)
    if (cur.relay[i].driver != HWP_DRV_GPIO && !hk.probe(cur.relay[i].address, false)) {
      missing = cur.relay[i].address;
      return false;
    }
  return true;
}

void tick() {
  uint32_t now = nowMs();
  if (!noteDone) {  // a rollback that happened before this start: tell the server (once)
    noteDone = true;
    Preferences pr;
    pr.begin("pm", true);
    size_t len = pr.getBytesLength(K_NOTE);
    char buf[96];
    bool have = len > 0 && len < sizeof(buf);
    if (have) pr.getBytes(K_NOTE, buf, len);
    pr.end();
    if (have) {
      JsonDocument doc;
      if (!deserializeJson(doc, buf, len)) ev("hw_rollback", doc["ver"] | 0, 0, doc["reason"] | "unknown", doc["addr"] | -1);
      drop(K_NOTE);
    }
  }
  if (st == ST_PENDING && (int32_t)(now - dueMs) >= 0) {
    Serial.printf("[hwp] restarting with profile v%u\n", (unsigned)trial.ver);
    if (hk.restart) hk.restart();
    return;
  }
  if (st != ST_TRIAL) return;
  if (now - lastProbeMs < 1000) return;
  lastProbeMs = now;
  int missing;
  if (allPartsAnswer(missing)) {  // confirmed: the trial profile becomes the active one
    save(K_ACT, cur);
    drop(K_TRY);
    setBoots(0);
    st = ST_ACTIVE;
    Serial.printf("[hwp] profile v%u confirmed\n", (unsigned)cur.ver);
    ev("hw_active", cur.ver);
  } else if (now - bootMs >= HW_CONFIRM_MS) {  // something did not answer in time: previous profile again
    Serial.printf("[hwp] 0x%02X does not answer: rolling back profile v%u\n", missing, (unsigned)cur.ver);
    putNote(cur.ver, "no_reply", missing);
    drop(K_TRY);
    setBoots(0);
    if (hk.restart) hk.restart();
  }
}

}  // namespace hwp
