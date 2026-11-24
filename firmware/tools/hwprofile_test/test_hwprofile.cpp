// Host test of src/hwprofile.cpp: rules, JSON, live clamp change, delayed restart, confirm, rollback, crash loop.
#include <ArduinoJson.h>
#include <cstdio>
#include <cstdlib>
#include <map>
#include <set>
#include <string>
#include <vector>

#include "Arduino.h"
#include "Preferences.h"
#include "hwprofile.h"

uint32_t g_ms = 0;
SerialT Serial;
std::map<std::string, std::string> g_nvs;
bool g_nvsFull = false;

static int failures = 0;
#define CHECK(c) do { if (!(c)) { printf("FAIL line %d: %s\n", __LINE__, #c); failures++; } } while (0)

// ---- fake hardware
static std::set<int> bus;                 // addresses that answer
static std::vector<std::string> events;   // "name ver inS reason addr"
static int restarts = 0, clampApplies = 0;
static bool probe(uint8_t a, bool) { return bus.count(a) > 0; }
static void event(const char *n, uint32_t ver, int inS, const char *reason, int addr) {
  char b[96];
  snprintf(b, sizeof b, "%s v%u in%d %s %d", n, (unsigned)ver, inS, reason ? reason : "-", addr);
  events.push_back(b);
}
static void restart() { restarts++; }
static void applyClamps() { clampApplies++; }
static uint32_t ms() { return g_ms; }
static hwp::Hooks hooks = {probe, event, restart, applyClamps, ms};

static uint8_t g_clamp = 3;  // clamp model of the compiled default (the test "flashes" another config.h by changing it)
static hwp::Profile defaultProfile() {  // config.h: one rbAmp 0x50, XL9535 0x20 with 2 relays, 2 channels
  hwp::Profile p;
  p.nBoards = 1;
  p.addr[0] = 0x50;
  p.nRelayBoards = 1;
  p.relay[0] = {1, HWP_DRV_XL9535, 0x20, 2, 1};
  p.nCh = 2;
  p.ch[0] = {1, 1, g_clamp, 1, 1};  // board, input, SCT_30A = 3, relay board 1, output 1
  p.ch[1] = {1, 2, 3, 1, 2};
  return p;
}

static JsonDocument newProfileDoc(int ver, const char *mode, int delay = 0) {
  JsonDocument d;
  d["ver"] = ver;
  d["mode"] = mode;
  if (delay) d["delay_s"] = delay;
  JsonObject p = d["profile"].to<JsonObject>();
  p["boards"].to<JsonArray>().add(0x50);
  p["boards"].add(0x51);
  JsonObject r = p["relays"].to<JsonArray>().add<JsonObject>();
  r["id"] = 1; r["drv"] = "mcp23017"; r["addr"] = 0x21; r["n"] = 8; r["hi"] = 1;
  const int amps[] = {10, 10, 30, 30, 30, 100, 100};
  for (int i = 0; i < 7; i++) {
    JsonObject c = p["ch"].add<JsonObject>();
    c["b"] = i < 3 ? 1 : 2; c["i"] = i < 3 ? i + 1 : i - 2; c["a"] = amps[i]; c["rb"] = 1; c["ro"] = i + 1;
  }
  return d;
}

static void boot() {  // a restart: everything in RAM is gone, NVS stays
  hwp::begin(defaultProfile(), 2, hooks);
  g_ms += 100;
}

static bool call(JsonDocument &d, const char *&err, char *detail, const char *&res, int &inS) {
  return hwp::onProfile(d.as<JsonObjectConst>(), err, detail, 64, res, inS);
}

static bool has(const std::string &needle) {
  for (auto &e : events)
    if (e.find(needle) != std::string::npos) return true;
  return false;
}

int main() {
  char err[64];
  // ---------------- rules
  hwp::Profile p = defaultProfile();
  CHECK(hwp::validate(p, 0, err, sizeof err));
  hwp::Profile q = p;
  q.addr[0] = 0x60; CHECK(!hwp::validate(q, 0, err, sizeof err));
  q = p; q.nBoards = 2; q.addr[1] = 0x50; CHECK(!hwp::validate(q, 0, err, sizeof err));
  q = p; q.ch[1].ro = 1; CHECK(!hwp::validate(q, 0, err, sizeof err));          // relay used twice
  q = p; q.ch[1].input = 1; CHECK(!hwp::validate(q, 0, err, sizeof err));       // input used twice
  q = p; q.ch[0].ro = 3; CHECK(!hwp::validate(q, 0, err, sizeof err));          // the board has 2 outputs
  q = p; q.ch[0].rb = 2; CHECK(!hwp::validate(q, 0, err, sizeof err));          // no second relay board
  q = p; q.ch[0].rb = 0; CHECK(!hwp::validate(q, 0, err, sizeof err));          // no board but an output
  q = p; q.ch[0].rb = 0; q.ch[0].ro = 0; CHECK(hwp::validate(q, 0, err, sizeof err));  // a channel without relay
  q = p; q.ch[0].clamp = 9; CHECK(!hwp::validate(q, 0, err, sizeof err));
  q = p; q.ch[0].input = 8; CHECK(!hwp::validate(q, 0, err, sizeof err));
  q = p; q.relay[0].address = 0x30; CHECK(!hwp::validate(q, 0, err, sizeof err));
  q = p; q.relay[0] = {1, HWP_DRV_PCF8574, 0x38, 9, 0}; CHECK(!hwp::validate(q, 0, err, sizeof err));
  q = p; q.relay[0] = {1, HWP_DRV_GPIO, 0, 3, 1}; CHECK(!hwp::validate(q, 2, err, sizeof err));  // 3 GPIO relays, 2 pins
  CHECK(hwp::validate(q, 3, err, sizeof err));
  q = p; q.nRelayBoards = 2; q.relay[1] = {2, HWP_DRV_MCP23017, 0x20, 4, 1}; CHECK(!hwp::validate(q, 0, err, sizeof err));  // 0x20 twice
  q = p; q.relay[0].address = 0x50; CHECK(!hwp::validate(q, 0, err, sizeof err));  // address of an rbAmp (also not valid for the driver)
  // relay board ids are stable: Board A removed, Board B stays Board B
  hwp::Profile g = defaultProfile();
  g.nRelayBoards = 2;
  g.relay[0] = {2, HWP_DRV_XL9535, 0x20, 2, 1};  // Board B (2 outputs)
  g.relay[1] = {3, HWP_DRV_MCP23017, 0x21, 8, 1};  // Board C (8 outputs)
  g.ch[0].rb = 2; g.ch[0].ro = 2;
  g.ch[1].rb = 3; g.ch[1].ro = 8;
  CHECK(hwp::validate(g, 0, err, sizeof err));
  CHECK(hwp::relayIndex(g, 1) == -1 && hwp::relayIndex(g, 2) == 0 && hwp::relayIndex(g, 3) == 1);
  CHECK(hwp::relayNumber(g, 2, 2) == 2 && hwp::relayNumber(g, 3, 1) == 3 && hwp::relayNumber(g, 3, 8) == 10 && hwp::relayNumber(g, 0, 0) == 0);
  q = g; q.ch[0].rb = 1; CHECK(!hwp::validate(q, 0, err, sizeof err) && strstr(err, "no such relay board"));  // Board A is gone
  q = g; q.relay[0].id = 3; CHECK(!hwp::validate(q, 0, err, sizeof err));                                    // ids must ascend
  q = g; q.relay[1].id = 9; CHECK(!hwp::validate(q, 0, err, sizeof err));                                    // 1..8 only
  q = g; q.relay[0].id = 0; CHECK(!hwp::validate(q, 0, err, sizeof err));
  hwp::Profile h2 = defaultProfile();
  CHECK(!hwp::sameStructure(g, h2));
  JsonDocument gd;
  hwp::toJson(g, gd.to<JsonObject>());
  hwp::Profile g2;
  CHECK(hwp::fromJson(gd, g2, err, sizeof err) && g2.relay[0].id == 2 && g2.relay[1].id == 3 && g2.ch[1].rb == 3 && hwp::sameStructure(g, g2));
  printf("rules ok\n");

  // ---------------- JSON round trip
  JsonDocument d = newProfileDoc(5, "reboot", 30);
  hwp::Profile np;
  CHECK(hwp::fromJson(d["profile"], np, err, sizeof err) && hwp::validate(np, 0, err, sizeof err));
  CHECK(np.nBoards == 2 && np.nRelayBoards == 1 && np.nCh == 7 && np.ch[5].clamp == 5 && np.ch[2].clamp == 3);
  JsonDocument back;
  hwp::toJson(np, back.to<JsonObject>());
  CHECK(back["ch"][5]["a"] == 100 && back["relays"][0]["drv"] == "mcp23017" && back["boards"][1] == 0x51);
  JsonDocument bad;
  bad["boards"].to<JsonArray>().add(0x50);
  CHECK(!hwp::fromJson(bad, np, err, sizeof err));  // relays / ch missing
  printf("json ok\n");

  // ---------------- default, live clamp change
  g_nvs.clear(); bus = {0x50, 0x20};
  boot();
  CHECK(hwp::state() == hwp::ST_DEFAULT && hwp::active().ver == 0 && hwp::active().nCh == 2);
  JsonDocument live;
  live["ver"] = 1; live["mode"] = "live";
  JsonObject lp = live["profile"].to<JsonObject>();
  lp["boards"].to<JsonArray>().add(0x50);
  JsonObject lr = lp["relays"].to<JsonArray>().add<JsonObject>();
  lr["id"] = 1; lr["drv"] = "xl9535"; lr["addr"] = 0x20; lr["n"] = 2; lr["hi"] = 1;
  JsonObject c0 = lp["ch"].to<JsonArray>().add<JsonObject>(); c0["b"] = 1; c0["i"] = 1; c0["a"] = 100; c0["rb"] = 1; c0["ro"] = 1;
  JsonObject c1 = lp["ch"].add<JsonObject>(); c1["b"] = 1; c1["i"] = 2; c1["a"] = 30; c1["rb"] = 1; c1["ro"] = 2;
  const char *e = nullptr, *res = ""; char det[64]; int inS = -1;
  CHECK(call(live, e, det, res, inS) && !strcmp(res, "live") && clampApplies == 1);
  CHECK(hwp::active().ch[0].clamp == 5 && hwp::active().ver == 1 && hwp::state() == hwp::ST_ACTIVE);
  boot();  // survives a restart
  CHECK(hwp::state() == hwp::ST_ACTIVE && hwp::active().ch[0].clamp == 5 && hwp::active().ver == 1);
  // live with a structural difference is refused
  JsonDocument wrong = newProfileDoc(2, "live");
  CHECK(!call(wrong, e, det, res, inS) && !strcmp(e, "mode"));
  printf("live ok\n");

  // ---------------- reboot flow with confirm
  events.clear(); restarts = 0;
  bus = {0x50, 0x51, 0x20, 0x21};
  JsonDocument rb = newProfileDoc(2, "reboot", 5);  // delay clamped to 10 s
  CHECK(call(rb, e, det, res, inS) && !strcmp(res, "pending") && inS == 10);
  CHECK(hwp::state() == hwp::ST_PENDING && hwp::rebootPending() && has("hw_reboot_pending v2 in10"));
  JsonDocument rb2 = newProfileDoc(3, "reboot", 20);  // a newer one replaces the pending one
  CHECK(call(rb2, e, det, res, inS) && inS == 20 && hwp::state() == hwp::ST_PENDING);
  g_ms += 19000; hwp::tick(); CHECK(restarts == 0);
  g_ms += 2000; hwp::tick(); CHECK(restarts == 1);
  boot();  // restarted
  CHECK(hwp::state() == hwp::ST_TRIAL && hwp::active().ver == 3 && hwp::active().nCh == 7 && !hwp::rebootPending());
  events.clear();
  g_ms += 1500; hwp::tick();  // all parts answer: confirmed
  CHECK(hwp::state() == hwp::ST_ACTIVE && has("hw_active v3"));
  boot();
  CHECK(hwp::state() == hwp::ST_ACTIVE && hwp::active().ver == 3 && hwp::active().nCh == 7);
  printf("reboot + confirm ok\n");

  // ---------------- rollback: a part does not answer
  events.clear(); restarts = 0;
  bus = {0x50, 0x20};  // 0x51 and 0x21 missing for the next profile
  JsonDocument rb3 = newProfileDoc(4, "reboot", 10);
  rb3["profile"]["boards"][1] = 0x52;  // a board that does not exist
  CHECK(call(rb3, e, det, res, inS));
  g_ms += 11000; hwp::tick(); CHECK(restarts == 1);
  boot();
  CHECK(hwp::state() == hwp::ST_TRIAL && hwp::active().ver == 4);
  g_ms += 5000; hwp::tick(); CHECK(restarts == 1 && hwp::state() == hwp::ST_TRIAL);  // still waiting
  CHECK(!call(rb3, e, det, res, inS) && !strcmp(e, "busy"));                          // no new profile while in trial
  g_ms += 60000; hwp::tick(); CHECK(restarts == 2);                                    // time is up: rollback + restart
  boot();
  CHECK(hwp::state() == hwp::ST_ACTIVE && hwp::active().ver == 3);  // the confirmed profile v3 again
  events.clear();
  hwp::tick();
  CHECK(has("hw_rollback v4 in0 no_reply 82"));  // 0x52
  events.clear(); hwp::tick(); CHECK(events.empty());  // reported once
  printf("rollback ok\n");

  // ---------------- crash loop: never confirmed, the meter keeps resetting
  events.clear();
  bus = {0x50, 0x51, 0x20, 0x21};
  JsonDocument rb4 = newProfileDoc(5, "reboot", 10);
  CHECK(call(rb4, e, det, res, inS));
  g_ms += 11000; hwp::tick();
  boot(); CHECK(hwp::state() == hwp::ST_TRIAL);       // start 1
  boot(); CHECK(hwp::state() == hwp::ST_TRIAL);       // start 2 (crashed before the confirmation)
  boot(); CHECK(hwp::state() == hwp::ST_ACTIVE && hwp::active().ver == 3);  // start 3: rolled back
  hwp::tick(); CHECK(has("hw_rollback v5 in0 reset"));
  printf("crash loop ok\n");

  // ---------------- cancel, invalid profile
  events.clear(); restarts = 0;
  JsonDocument rb5 = newProfileDoc(6, "reboot", 30);
  CHECK(call(rb5, e, det, res, inS));
  CHECK(hwp::onCancel() && hwp::state() == hwp::ST_ACTIVE && !hwp::rebootPending());
  g_ms += 40000; hwp::tick(); CHECK(restarts == 0);
  boot(); CHECK(hwp::state() == hwp::ST_ACTIVE && hwp::active().ver == 3);  // nothing was stored for the restart
  CHECK(!hwp::onCancel());
  JsonDocument inv = newProfileDoc(7, "reboot");
  inv["profile"]["boards"][1] = 0x50;
  CHECK(!call(inv, e, det, res, inS) && !strcmp(e, "invalid") && strstr(det, "twice"));
  printf("cancel + invalid ok\n");

  // ---------------- NVS full: nothing is claimed that cannot be kept
  events.clear(); restarts = 0;
  g_nvsFull = true;
  JsonDocument full1 = newProfileDoc(8, "reboot", 10);
  CHECK(!call(full1, e, det, res, inS) && !strcmp(e, "storage") && hwp::state() == hwp::ST_ACTIVE && !hwp::rebootPending());
  hwp::Profile before = hwp::active();
  JsonDocument lf;
  lf["ver"] = 9; lf["mode"] = "live";
  hwp::toJson(hwp::active(), lf["profile"].to<JsonObject>());
  lf["profile"]["ch"][0]["a"] = 5;
  CHECK(!call(lf, e, det, res, inS) && !strcmp(e, "storage") && hwp::active().ch[0].clamp == before.ch[0].clamp && hwp::active().ver == 3);
  g_nvsFull = false;
  // a trial profile that cannot be stored at the confirmation is not claimed: rollback
  JsonDocument tr = newProfileDoc(10, "reboot", 10);
  CHECK(call(tr, e, det, res, inS));
  g_ms += 11000; hwp::tick();
  boot(); CHECK(hwp::state() == hwp::ST_TRIAL);
  events.clear(); restarts = 0;
  g_nvsFull = true;
  g_ms += 1500; hwp::tick();
  CHECK(restarts == 1 && hwp::state() == hwp::ST_TRIAL);   // asked for a restart, not "active"
  g_nvsFull = false;
  boot();
  CHECK(hwp::state() == hwp::ST_ACTIVE && hwp::active().ver == 3);  // the trial profile is gone (rolled back)
  printf("NVS full ok\n");

  // ---------------- hello.hwp
  JsonDocument h;
  hwp::stateJson(h.to<JsonObject>());
  CHECK(h["state"] == "active" && h["ver"] == 3 && h["profile"]["ch"].size() == 7);

  // ---------------- config.h changed and flashed: it wins over the profile the server stored (30 A -> 20 A in config.h)
  boot();
  CHECK(hwp::state() == hwp::ST_ACTIVE && hwp::active().ver == 3);   // same config.h: the profile stays
  { JsonDocument j; hwp::stateJson(j.to<JsonObject>()); CHECK(j["reset"].isNull()); }
  g_clamp = 6;                                                       // SCT_20A
  boot();
  CHECK(hwp::state() == hwp::ST_DEFAULT && hwp::active().ver == 0 && hwp::active().ch[0].clamp == 6 && hwp::active().nCh == 2);
  { JsonDocument j; hwp::stateJson(j.to<JsonObject>()); CHECK(j["reset"] == "config" && j["profile"]["ch"][0]["a"] == 20 && j["state"] == "default"); }
  boot();                                                            // the next restart: nothing is stored any more, no reset flag
  CHECK(hwp::state() == hwp::ST_DEFAULT && hwp::active().ch[0].clamp == 6);
  { JsonDocument j; hwp::stateJson(j.to<JsonObject>()); CHECK(j["reset"].isNull()); }
  printf("config.h change ok\n");

  if (failures) {
    printf("%d check(s) FAILED\n", failures);
    return 1;
  }
  printf("all hardware profile checks passed\n");
  return 0;
}
