#include "net.h"

#include <ArduinoJson.h>
#include <ESPmDNS.h>
#include <HTTPClient.h>
#include <WebSocketsClient.h>
#include <WiFi.h>
#include <WiFiClientSecure.h>
#include <esp_wifi.h>

#include "../config.h"
#include "app_state.h"
#include "battery.h"
#include "certs.h"
#include "control.h"
#include "led.h"
#include "mesh.h"
#include "outbox.h"
#include "storage.h"
#include "system.h"
#include "auxrelay.h"
#include "hwmap.h"
#include "relays.h"
#include "temp.h"
#include "timekeep.h"

namespace net {

static WebSocketsClient ws;
static store::Server srv;
static bool wsStarted = false;
static bool wsConnected = false;  // TCP/WS up
static bool helloDone = false;    // got "welcome"
static uint32_t lastPingMs = 0, pingSentMs = 0;
static bool awaitingPong = false;
static uint32_t sentMaxSeq = 0, sentAtMs = 0;
static bool awaitingAck = false;
static uint32_t lastStateVer = 0, lastStateSentMs = 0;
static uint32_t reconnectMs = 2000;
static uint32_t lostEpoch = 0;
static bool everOnline = false;

static SemaphoreHandle_t pairLock;
static PairRequest pairReq;
static bool pairPending = false;
static String pairStatus_ = "idle";

static uint32_t wifiDownSinceMs = 0, wifiUpSinceMs = 0, lastWifiRetryMs = 0;
// mesh: leaf mode = no Wi-Fi, talk to the server through a gateway meter
static bool leafMode = false;
static uint32_t leafSinceMs = 0, helloSentMs = 0;
static uint32_t noLinkSinceMs = 0;  // leaf: no gateway since (0 = linked)
static bool wifiRetryFromLeaf = false; // short Wi-Fi attempt, then back to the mesh
static bool linkWasUp = false;
static PairRequest meshPairReq;
static bool meshPairPending = false;
static uint32_t meshPairDeadline = 0;
static String testSsid, testPass;
static volatile bool testPending = false, testing = false;
static volatile uint8_t testState = 0;  // 0 idle, 1 connecting, 2 ok, 3 failed
static uint32_t testStartMs = 0;
static bool mdnsStarted = false;

String apSsid() { return "Power_" + store::deviceId(); }
String serverHost() { return srv.host; }
String linkMode() {
  if (leafMode) return mesh::leafLinked() ? "mesh" : "searching";
  return WiFi.status() == WL_CONNECTED ? "wifi" : "none";
}
String meshGateway() { return leafMode ? mesh::gatewayId() : String(""); }
uint8_t meshLeaves() { return leafMode ? 0 : mesh::leafCount(); }
uint16_t outboxCount() { return outbox::count(); }
String ipAddress() { return (!leafMode && WiFi.status() == WL_CONNECTED) ? WiFi.localIP().toString() : String(""); }
int rssi() { return WiFi.status() == WL_CONNECTED ? WiFi.RSSI() : 0; }

static void setNet(NetStatus s) {
  StateLock l;
  g.net = s;
}

// ------------------------------------------------------------------ Wi-Fi

static void startAp() {
  if (g.apActive) return;
  WiFi.mode(WIFI_AP_STA);  // STA side is needed for Wi-Fi scanning and testing
  WiFi.softAP(apSsid().c_str(), store::apPassword().c_str());
  StateLock l;
  g.apActive = true;
  Serial.printf("[net] setup AP '%s' started at %s\n", apSsid().c_str(), WiFi.softAPIP().toString().c_str());
}

static void stopAp() {
  if (!g.apActive) return;
  WiFi.softAPdisconnect(true);
  WiFi.mode(WIFI_STA);
  StateLock l;
  g.apActive = false;
  Serial.println("[net] setup AP closed");
}

void tryWifi(const String &ssid, const String &pass) {
  xSemaphoreTake(pairLock, portMAX_DELAY);
  testSsid = ssid;
  testPass = pass;
  testPending = true;
  testState = 1;
  xSemaphoreGive(pairLock);
}

String wifiTestStatus() {
  static const char *names[] = {"idle", "connecting", "ok", "failed"};
  return names[testState];
}

static bool wifiTestLoop() {
  if (testPending) {
    xSemaphoreTake(pairLock, portMAX_DELAY);
    String ssid = testSsid, pass = testPass;
    testPending = false;
    xSemaphoreGive(pairLock);
    WiFi.mode(WIFI_AP_STA);  // keep the setup AP so the phone stays connected
    WiFi.disconnect();
    WiFi.begin(ssid.c_str(), pass.c_str());
    testStartMs = millis();
    testing = true;
    Serial.printf("[net] trying Wi-Fi '%s'\n", ssid.c_str());
  }
  if (!testing) return false;
  if (WiFi.status() == WL_CONNECTED) {
    xSemaphoreTake(pairLock, portMAX_DELAY);
    store::setWifi(testSsid, testPass);
    xSemaphoreGive(pairLock);
    testing = false;
    testState = 2;
    lastWifiRetryMs = millis();
    Serial.printf("[net] Wi-Fi OK, IP %s (setup AP closes in 5 min)\n", WiFi.localIP().toString().c_str());
    return false;
  }
  if (millis() - testStartMs > 20000) {
    WiFi.disconnect();
    testing = false;
    testState = 3;
    Serial.println("[net] Wi-Fi test failed");
    return false;
  }
  setNet(NET_WIFI_CONNECTING);
  return true;
}

static void wifiLoop() {
  if (wifiTestLoop()) return;
  if (!store::hasWifi()) {
    startAp();
    if (!store::meshOnly()) setNet(NET_SETUP_AP);
    return;
  }
  uint32_t now = millis();
  if (WiFi.status() != WL_CONNECTED) {
    wifiUpSinceMs = 0;
    if (!wifiDownSinceMs) wifiDownSinceMs = now;
    if (now - lastWifiRetryMs > 30000) {  // keep trying, the driver may give up on its own
      lastWifiRetryMs = now;
      WiFi.begin(store::wifiSsid().c_str(), store::wifiPass().c_str());
    }
    if (now - wifiDownSinceMs > WIFI_FALLBACK_AP_MS) startAp();  // let the user fix Wi-Fi
    setNet(NET_WIFI_CONNECTING);
    return;
  }
  wifiDownSinceMs = 0;
  if (!wifiUpSinceMs) {
    wifiUpSinceMs = now;
    Serial.printf("[net] Wi-Fi connected, IP %s\n", WiFi.localIP().toString().c_str());
  }
  if (g.apActive && store::hasAdmin() && now - wifiUpSinceMs > AP_CLOSE_AFTER_STA_MS) stopAp();
  if (!mdnsStarted) {
    String host = "power-" + store::deviceId();
    host.toLowerCase();
    sys::crumb(sys::CR_NET, "wifi: mDNS");
    mdnsStarted = MDNS.begin(host.c_str());
    if (mdnsStarted) MDNS.addService("http", "tcp", 80);
  }
  sys::crumb(sys::CR_NET, "wifi: time");
  timekeep::loop();
}

// ------------------------------------------------------------------ helpers

// Every message to the server goes through here: the WebSocket, or the mesh when a leaf.
static void txt(String s) {
  if (leafMode) mesh::leafSend(s);
  else ws.sendTXT(s);
}

static void sendJson(JsonDocument &doc) {
  String s;
  serializeJson(doc, s);
  txt(s);
}

static bool parseConfig(JsonObject c, MeterConfig &out, String &err) {
  MeterConfig m;
  int report = c["report_s"] | 60;
  if (report < 10 || report > 3600) { err = "report_s"; return false; }
  m.report_s = report;
  int readS = c["read_s"] | report;  // older servers: one reading per send
  if (readS < 1 || readS > report) { err = "read_s"; return false; }
  m.read_s = readS;
  int off = c["offline_off_min"] | 0;
  if (off < 0 || off > 1440) { err = "offline_off_min"; return false; }
  m.offline_off_min = off;
  JsonObject s = c["sess"];
  const char *mode = s["mode"] | "none";
  if (!strcmp(mode, "none")) m.sess_mode = SESS_NONE;
  else if (!strcmp(mode, "duration")) m.sess_mode = SESS_DURATION;
  else if (!strcmp(mode, "daily")) m.sess_mode = SESS_DAILY;
  else { err = "sess.mode"; return false; }
  int daily = s["daily_utc_min"] | 0;
  if (daily < 0 || daily > 1439) { err = "sess.daily_utc_min"; return false; }
  m.daily_utc_min = daily;
  long dur = s["dur_min"] | 1440;
  if (dur < 1 || dur > 525600) { err = "sess.dur_min"; return false; }
  m.dur_min = dur;
  m.auto_renew = s["auto"] | false;
  JsonObject ch = c["ch"][0];
  float kwh = ch["kwh_max"] | 0.0f, amp = ch["amp_max"] | 0.0f;
  if (kwh < 0 || kwh > 100000) { err = "ch[0].kwh_max"; return false; }
  if (amp < 0 || amp > SENSOR_AMP_RATING) { err = "ch[0].amp_max"; return false; }
  m.kwh_max = kwh;
  auto action = [](const char *a) -> uint8_t {
    return !strcmp(a, "run") ? ACT_RUN : !strcmp(a, "end") ? ACT_END : ACT_HOLD;
  };
  JsonObject v = c["volt"];
  float vmin = v["min"] | 0.0f, vmax = v["max"] | 0.0f;
  if (vmin < 0 || vmin > 400 || vmax < 0 || vmax > 400 || (vmin > 0 && vmax > 0 && vmin >= vmax)) {
    err = "volt";
    return false;
  }
  m.volt_min = vmin;
  m.volt_max = vmax;
  m.volt_action = action(v["action"] | "hold");
  JsonObject ta = c["tare"];
  float tA = ta["a"] | 0.0f, tW = ta["w"] | 0.0f;
  if (tA < 0 || tA > 5 || tW < 0 || tW > 500) {
    err = "tare";
    return false;
  }
  m.tare_a = tA;
  m.tare_w = tW;
  m.tare_limits = ta["limits"] | true;
  JsonObject t = c["tamper"];
  m.tamper_protect = t["protect"] | false;
  m.tamper_action = action(t["action"] | "hold");
  JsonObject se = c["sensor"];
  int sto = se["timeout_s"] | 10;
  if (sto < 0 || sto > 3600) {
    err = "sensor.timeout_s";
    return false;
  }
  m.sensor_to_s = sto;
  m.sensor_action = action(se["action"] | "hold");
  m.summary = c["summary"] | false;
  JsonObject tp = c["temp"];
  float tmax = tp["max"] | 0.0f;
  if (tmax < 0 || tmax > 125) {
    err = "temp.max";
    return false;
  }
  m.temp_max = tmax;
  m.temp_action = action(tp["action"] | "hold");
  const char *act = ch["kwh_action"] | "";
  if (!strcmp(act, "hold")) m.kwh_action = KWH_HOLD;
  else if (!strcmp(act, "warn")) m.kwh_action = KWH_WARN;
  else if (!*act && !(ch["kwh_end"] | true)) m.kwh_action = KWH_WARN;  // server v0.2.x
  else m.kwh_action = KWH_END;
  m.amp_max = amp;
  const char *aerr = nullptr;  // AUX relays: names + automation (applied with the rest in applyConfig)
  if (!auxrelay::parse(c["aux"].as<JsonArrayConst>(), c["tz_min"] | 0, aerr)) {
    err = aerr;
    return false;
  }
  out = m;
  return true;
}

// ------------------------------------------------------------------ pairing

void requestPair(const PairRequest &r) {
  xSemaphoreTake(pairLock, portMAX_DELAY);
  pairReq = r;
  pairPending = true;
  pairStatus_ = "working";
  xSemaphoreGive(pairLock);
}

String pairStatus() {
  xSemaphoreTake(pairLock, portMAX_DELAY);
  String s = pairStatus_;
  xSemaphoreGive(pairLock);
  return s;
}

static void setPairStatus(const String &s) {
  xSemaphoreTake(pairLock, portMAX_DELAY);
  pairStatus_ = s;
  xSemaphoreGive(pairLock);
}

static void stopWs() {
  if (wsStarted) ws.disconnect();
  wsStarted = wsConnected = helloDone = false;
}

static String pairBody(const PairRequest &r) {
  JsonDocument body;
  body["code"] = r.code;
  body["device_id"] = store::deviceId();
  body["fw"] = FW_VERSION;
  body["hw"] = HW_NAME;
  body["proto"] = PROTO_VERSION;
  JsonObject caps = body["caps"].to<JsonObject>();
  caps["kind"] = METER_KIND;  // "ac" / "dc": the server sets the meter type from this
  caps["sensor"] = METER_DC ? "pzem017" : "pzem004t";
  caps["channels"] = SENSOR_CHANNELS;
  caps["relays"] = CHANNEL_RELAY ? 1 : 0;
  caps["aux"] = auxrelay::count();  // AUX relays (every relay output except CHANNEL_RELAY)
  caps["inputs"].to<JsonArray>().add("tamper");
  caps["rtc"] = (bool)HAS_DS3231;
  caps["tamper"] = (bool)TAMPER_ENABLE;
  if (TEMP_ENABLE) caps["temp"] = temp::count();  // temperature sensors found
  caps["amp_rating"] = SENSOR_AMP_RATING;
  String payload;
  serializeJson(body, payload);
  return payload;
}

// Same handling for a direct (HTTPS) and a mesh pairing answer.
static void applyPairResult(int code, JsonObject doc, const PairRequest &r, const String &netError) {
  if (code == 200) {
    if (!doc["secret"].is<const char *>()) {
      setPairStatus("Unexpected reply from server");
      return;
    }
    stopWs();
    srv.host = r.host;
    srv.port = r.port;
    srv.tls = r.tls;
    srv.insecure = r.insecure;
    srv.secret = doc["secret"].as<String>();
    store::saveServer(srv);
    if (doc["link_key"].is<const char *>()) store::setLinkKey(doc["link_key"].as<String>());
    timekeep::set(doc["time"] | 0, TS_SERVER);
    MeterConfig c;
    String err;
    if (parseConfig(doc["config"]["cfg"], c, err)) {
      c.ver = doc["config"]["ver"] | 0;
      control::applyConfig(c);
    }
    outbox::clear();  // old items belong to a previous pairing
    {
      StateLock l;
      g.paired = true;
      g.unauthorized = false;
      g.offlineSinceMs = 0;
    }
    everOnline = false;
    linkWasUp = false;
    helloDone = false;
    led::flash(led::FLASH_PAIRED);
    setPairStatus("ok");
    Serial.println("[net] paired");
    return;
  }
  String msg;
  if (code == 404) msg = "Pairing code is wrong or expired. Make a new one on the server.";
  else if (code == 409) msg = "This meter is still paired on the server. Remove it there first.";
  else if (code == 429) msg = "Too many attempts. Wait a minute.";
  else if (code < 0) msg = "Cannot reach server: " + netError;
  else msg = "Server replied HTTP " + String(code);
  setPairStatus(msg);
}

void meshPairResult(int code, JsonObject resp) {
  if (!meshPairPending) return;
  meshPairPending = false;
  applyPairResult(code, resp, meshPairReq, "");
}

static void doPair(PairRequest r) {
  String payload = pairBody(r);
  if (leafMode) {  // no Wi-Fi: ask a gateway meter to pair us
    if (!mesh::leafLinked()) {
      setPairStatus("No Wi-Fi and no gateway meter found yet. Wait a moment and try again.");
      return;
    }
    meshPairReq = r;
    meshPairPending = true;
    meshPairDeadline = millis() + 20000;
    mesh::leafSendPair(payload);
    Serial.printf("[net] pairing through gateway %s\n", mesh::gatewayId().c_str());
    return;
  }
  if (WiFi.status() != WL_CONNECTED) {
    setPairStatus("Wi-Fi is not connected");
    return;
  }
  String url = String(r.tls ? "https://" : "http://") + r.host + ":" + r.port + "/api/v1/pair";
  HTTPClient http;
  WiFiClientSecure secure;
  WiFiClient plain;
  http.setTimeout(15000);
  http.setConnectTimeout(10000);
  bool begun;
  if (r.tls) {
    if (r.insecure) secure.setInsecure();
    else secure.setCACert(ROOT_CA_PEM);
    begun = http.begin(secure, url);
  } else {
    begun = http.begin(plain, url);
  }
  if (!begun) {
    setPairStatus("Bad server address");
    return;
  }
  http.addHeader("Content-Type", "application/json");
  Serial.printf("[net] pairing with %s\n", url.c_str());
  int code = http.POST(payload);
  String resp = http.getString();
  String netErr = code < 0 ? http.errorToString(code) : String("");
  http.end();
  JsonDocument doc;
  if (code == 200 && deserializeJson(doc, resp)) {
    setPairStatus("Unexpected reply from server");
    return;
  }
  applyPairResult(code, doc.as<JsonObject>(), r, netErr);
}

// ------------------------------------------------------------------ outbox flush

// Readings are buffered and sent in a batch every report_s (control asks with takeSendRequest).
// Once a round starts it continues, batch after batch, until the buffer is empty.
static bool sendRound = true;  // true at start-up / reconnect: send any backlog right away
static void flushOutbox() {
  if (control::takeSendRequest()) sendRound = true;
  if (!outbox::count()) {
    sendRound = false;
    return;
  }
  if (!sendRound) return;
  if (awaitingAck && millis() - sentAtMs < ACK_TIMEOUT_MS) return;
  std::vector<outbox::Item> items;
  outbox::nextBatch(items, leafMode ? MESH_BATCH_MAX : OUTBOX_BATCH_MAX);
  if (items.empty()) return;
  if (items[0].kind == outbox::EVT) {
    txt(items[0].json);
  } else {
    String msg = "{\"t\":\"tel\",\"items\":[";
    for (size_t i = 0; i < items.size(); i++) {
      if (i) msg += ',';
      msg += items[i].json;
    }
    msg += "]}";
    txt(msg);
  }
  sentMaxSeq = items.back().seq;
  awaitingAck = true;
  sentAtMs = millis();
}

// ------------------------------------------------------------------ messages

static void sendHello() {
  JsonDocument doc;
  doc["t"] = "hello";
  doc["fw"] = FW_VERSION;
  doc["proto"] = PROTO_VERSION;
  doc["boot"] = store::bootCount();
  doc["up"] = millis() / 1000;
  doc["tv"] = timekeep::valid() ? 1 : 0;
  doc["ts"] = timekeep::now();
  {
    StateLock l;
    doc["cfg_ver"] = g.cfg.ver;
  }
  JsonObject caps = doc["caps"].to<JsonObject>();
  caps["kind"] = METER_KIND;
  caps["sensor"] = METER_DC ? "pzem017" : "pzem004t";
  caps["mc"] = false;  // single-channel firmware (the server detects a change of hardware type)
  caps["aux"] = auxrelay::count();
  caps["channels"] = SENSOR_CHANNELS;
  caps["rtc"] = (bool)HAS_DS3231;
  caps["tamper"] = (bool)TAMPER_ENABLE;
  if (TEMP_ENABLE) caps["temp"] = temp::count();
  caps["amp_rating"] = SENSOR_AMP_RATING;
  control::stateJson(doc["state"].to<JsonObject>());
  sendJson(doc);
  lastStateVer = control::stateVersion();
}

static void sendState() {
  JsonDocument doc;
  doc["t"] = "state";
  control::stateJson(doc.as<JsonObject>());
  sendJson(doc);
  lastStateVer = control::stateVersion();
  lastStateSentMs = millis();
}

static void markOffline() {
  StateLock l;
  if (g.paired && !g.offlineSinceMs) {
    g.offlineSinceMs = millis();
    if (everOnline && !lostEpoch) {
      lostEpoch = timekeep::valid() ? timekeep::now() : 1;
      control::emitEvent("server_lost");
    }
  }
}

// Answer to the server's "Get meter info".
static void sendInfo() {
  JsonDocument doc;
  doc["t"] = "info";
  doc["ip"] = ipAddress();
  doc["link"] = linkMode();
  if (leafMode) {
    doc["gateway"] = mesh::gatewayId();
    doc["mesh_ch"] = mesh::channel();
  } else {
    doc["leaves"] = mesh::leafCount();
  }
  doc["ssid"] = WiFi.SSID();
  doc["rssi"] = WiFi.RSSI();
  doc["mac"] = WiFi.macAddress();
  doc["heap"] = ESP.getFreeHeap();
  doc["heap_min"] = ESP.getMinFreeHeap();
  doc["psram"] = ESP.getPsramSize();
  doc["psram_free"] = ESP.getFreePsram();
  doc["fw"] = FW_VERSION;
  doc["hw"] = HW_NAME;
  doc["hw_info"] = HW_INFO;
  doc["sensor"] = SENSOR_NAME;
  doc["relay_board"] = relays::summary();
  if (TEMP_ENABLE) doc["temp_sensors"] = temp::summary();
  doc["kind"] = METER_KIND;
  doc["chip"] = ESP.getChipModel();
  doc["flash"] = ESP.getFlashChipSize();
  doc["core"] = ESP.getSdkVersion();
  doc["uptime"] = millis() / 1000;
  doc["boot"] = store::bootCount();
  doc["outbox"] = outbox::count();
  doc["time_src"] = timekeep::sourceName();
  doc["clock"] = timekeep::valid() ? timekeep::now() : 0;  // the meter's clock
  int64_t rtc = timekeep::rtcNow();
  if (rtc >= 0) doc["rtc"] = (uint32_t)rtc;  // DS3231 fitted: its own time (0 = not readable)
  doc["stack"] = sys::stackReport();
  doc["relay_sw"] = control::relaySwitches();
  doc["reset"] = String(sys::resetReason()) + (sys::safeMode ? " - SAFE MODE (mesh + DS3231 off)" : "");
  if (battery::enabled()) {
    float v = battery::volts();
    doc["bat_v"] = serialized(String(v, 2));
    doc["bat_pct"] = battery::percent(v);
  }
  sendJson(doc);
}

static void cmdAck(uint32_t ref, bool ok, const char *err) {
  JsonDocument doc;
  doc["t"] = "cmd_ack";
  doc["ref"] = ref;
  doc["ok"] = ok;
  if (err) doc["err"] = err;
  sendJson(doc);
}

static void goOnline() {
  sendRound = true;  // backlog from the offline time goes out now
  {
    StateLock l;
    g.net = NET_ONLINE;
    g.offlineSinceMs = 0;
    g.lastAckMs = millis();
  }
  if (lostEpoch) {
    uint32_t since = lostEpoch > 1 ? lostEpoch : 0;  // 1 = lost while the clock was unknown
    control::emitEvent("server_back", [](JsonObject o, void *p) {
      if (*(uint32_t *)p) o["since"] = *(uint32_t *)p;
    }, &since);
    lostEpoch = 0;
  }
  everOnline = true;
  reconnectMs = 2000;
}

static void handleCommand(JsonObject m) {
  const char *c = m["c"] | "";
  uint32_t ref = m["ref"] | 0;
  const char *err = nullptr;
  bool ok = true;
  if (!strcmp(c, "relay")) {
    ok = control::setRelay(m["on"] | false, "server", err);
    if (!ok) led::flash(led::FLASH_REJECT);
  } else if (!strcmp(c, "aux")) {
    ok = auxrelay::set(m["i"] | 0, m["on"] | false, "server", err);
  } else if (!strcmp(c, "renew")) {
    control::renew("manual", false);
  } else if (!strcmp(c, "end")) {
    control::endSession();
  } else if (!strcmp(c, "send_now")) {
    control::reportNow();
  } else if (!strcmp(c, "reset_trip")) {
    control::resetTrip("server", m["by"] | "");
  } else if (!strcmp(c, "get_state")) {
    sendState();
  } else if (!strcmp(c, "disable") || !strcmp(c, "enable")) {
    control::setDisabled(!strcmp(c, "disable"));
  } else if (!strcmp(c, "get_info")) {
    sendInfo();
  } else if (!strcmp(c, "remove")) {
    cmdAck(ref, true, nullptr);
    if (!leafMode) ws.loop();
    delay(200);
    sys::factoryReset("removed by server");
    return;
  } else {
    ok = false;
    err = "unknown_cmd";
  }
  cmdAck(ref, ok, err);
}

static void handleMessage(const char *payload, size_t len) {
  JsonDocument doc;
  if (deserializeJson(doc, payload, len)) return;
  JsonObject m = doc.as<JsonObject>();
  const char *t = m["t"] | "";
  if (m["time"].is<uint32_t>()) timekeep::set(m["time"].as<uint32_t>(), TS_SERVER);

  if (!strcmp(t, "welcome")) {
    helloDone = true;
    goOnline();
    Serial.println("[net] online");
  } else if (!strcmp(t, "ack")) {
    uint32_t last = m["last_seq"] | 0;
    bool progress = awaitingAck && last >= sentMaxSeq;
    outbox::ackUpTo(last);
    if (last >= sentMaxSeq) awaitingAck = false;
    if (progress) led::flash(led::FLASH_ACK);
    StateLock l;
    g.lastAckMs = millis();
    g.lastSyncEpoch = timekeep::valid() ? timekeep::now() : 0;
  } else if (!strcmp(t, "cfg")) {
    MeterConfig c;
    String err;
    JsonDocument ack;
    ack["t"] = "cfg_ack";
    ack["ver"] = m["ver"] | 0;
    if (parseConfig(m["cfg"], c, err)) {
      c.ver = m["ver"] | 0;
      control::applyConfig(c);
      ack["ok"] = true;
      led::flash(led::FLASH_CONFIG);
      Serial.printf("[net] config v%u applied\n", (unsigned)c.ver);
    } else {
      ack["ok"] = false;
      ack["err"] = err;
      led::flash(led::FLASH_REJECT);
    }
    sendJson(ack);
  } else if (!strcmp(t, "cmd")) {
    handleCommand(m);
  } else if (!strcmp(t, "pong")) {
    awaitingPong = false;
  } else if (!strcmp(t, "link_key")) {
    store::setLinkKey(m["key"] | "");  // needed to talk through the mesh later
  } else if (!strcmp(t, "bye")) {
    int code = m["code"] | 0;
    if (code == 4403) {
      sys::factoryReset("removed by server (4403)");
    } else if (code == 4401) {
      Serial.println("[net] server rejected our credentials; re-pair needed");
      StateLock l;
      g.unauthorized = true;
    }
  }
}

void meshToServer(const char *json, size_t len) {
  if (!leafMode && wsConnected) ws.sendTXT((uint8_t *)json, len);
}

void meshDeliver(const char *json, size_t len) { handleMessage(json, len); }

void meshChannel(uint8_t ch) {
  // Our hotspot must use the same radio channel as the gateway (single radio).
  if (g.apActive) {
    uint8_t cur;
    wifi_second_chan_t second;
    esp_wifi_get_channel(&cur, &second);
    if (cur != ch) WiFi.softAP(apSsid().c_str(), store::apPassword().c_str(), ch);
  } else {
    esp_wifi_set_channel(ch, WIFI_SECOND_CHAN_NONE);
  }
}

static void onWsEvent(WStype_t type, uint8_t *payload, size_t len) {
  switch (type) {
    case WStype_CONNECTED:
      Serial.println("[net] WebSocket connected");
      wsConnected = true;
      helloDone = false;
      awaitingAck = awaitingPong = false;
      lastPingMs = millis();
      sendHello();
      break;
    case WStype_DISCONNECTED:
      if (wsConnected) Serial.println("[net] WebSocket disconnected");
      wsConnected = helloDone = false;
      reconnectMs = min<uint32_t>(reconnectMs * 2, 60000);
      ws.setReconnectInterval(reconnectMs + (esp_random() % 1000));
      break;
    case WStype_TEXT: {
      sys::crumb(sys::CR_NET, "ws: message");
      // Messages for our mesh leaves are forwarded as they are (they are signed by the server).
      JsonDocument filter, head;
      filter["t"] = true;
      filter["d"] = true;
      if (!deserializeJson(head, (const char *)payload, len, DeserializationOption::Filter(filter))) {
        const char *t = head["t"] | "";
        if (!strcmp(t, "via") || !strcmp(t, "via_pair_res")) {
          mesh::toLeaf((const char *)payload, len, head["d"] | "", t[3] == '_');
          break;
        }
      }
      handleMessage((const char *)payload, len);
      break;
    }
    default:
      break;
  }
}

static void startWs() {
  static String headers;
  headers = "Authorization: Bearer " + store::deviceId() + "." + srv.secret + "\r\nX-PM-Proto: 1";
  ws.setExtraHeaders(headers.c_str());
  ws.onEvent(onWsEvent);
  ws.setReconnectInterval(reconnectMs);
  if (srv.tls) {
    if (srv.insecure) ws.beginSSL(srv.host.c_str(), srv.port, "/ws/device", "", "");
    else ws.beginSslWithCA(srv.host.c_str(), srv.port, "/ws/device", ROOT_CA_PEM, "");
  } else {
    ws.begin(srv.host.c_str(), srv.port, "/ws/device", "");
  }
  wsStarted = true;
  Serial.printf("[net] connecting to %s://%s:%u/ws/device\n", srv.tls ? "wss" : "ws", srv.host.c_str(), srv.port);
}

static void onlineWork() {
  uint32_t now = millis();
  if (now - lastPingMs > WS_PING_INTERVAL_MS) {
    txt("{\"t\":\"ping\"}");
    lastPingMs = pingSentMs = now;
    awaitingPong = true;
  }
  if (awaitingPong && now - pingSentMs > WS_PONG_TIMEOUT_MS) {
    Serial.println("[net] no pong, reconnecting");
    awaitingPong = false;
    if (leafMode) {
      mesh::leafReset();  // find a (maybe other) gateway
      helloDone = linkWasUp = false;
    } else {
      ws.disconnect();
    }
    return;
  }
  sys::crumb(sys::CR_NET, "online: outbox");
  flushOutbox();
  sys::crumb(sys::CR_NET, "online");
  if (control::stateVersion() != lastStateVer && now - lastStateSentMs > 500) sendState();
}

static void serverLoop() {
  bool paired, unauthorized;
  {
    StateLock l;
    paired = g.paired;
    unauthorized = g.unauthorized;
  }
  if (!paired || unauthorized) {
    stopWs();
    setNet(NET_NOT_PAIRED);
    return;
  }
  if (!wsStarted) {
    sys::crumb(sys::CR_NET, "server: ws start");
    store::loadServer(srv);
    startWs();
  }
  sys::crumb(sys::CR_NET, "server: ws.loop");
  ws.loop();
  sys::crumb(sys::CR_NET, "server");

  if (!helloDone) {
    setNet(NET_CONNECTING);
    markOffline();
    return;
  }

  onlineWork();
}

// Leaf: same protocol, through a gateway meter.
static void leafLoop() {
  bool paired, unauthorized;
  {
    StateLock l;
    paired = g.paired;
    unauthorized = g.unauthorized;
  }
  if (!paired || unauthorized) {
    setNet(NET_NOT_PAIRED);
    return;
  }
  uint32_t now = millis();
  if (!mesh::leafLinked()) {
    linkWasUp = helloDone = false;
    setNet(NET_CONNECTING);
    markOffline();
    return;
  }
  if (!linkWasUp) {
    linkWasUp = true;
    awaitingAck = awaitingPong = false;
    lastPingMs = helloSentMs = now;
    sendHello();
  }
  if (!helloDone) {
    setNet(NET_CONNECTING);
    markOffline();
    if (now - helloSentMs > 10000) {  // no welcome yet: say hello again
      helloSentMs = now;
      sendHello();
    }
    return;
  }
  onlineWork();
}

// ------------------------------------------------------------------ task

// ------------------------------------------------------------------ mesh mode (Auto role)

static void enterLeaf() {
  Serial.println("[net] no Wi-Fi: switching to mesh (leaf)");
  leafMode = true;
  leafSinceMs = noLinkSinceMs = millis();
  stopWs();
  helloDone = linkWasUp = false;
  WiFi.setAutoReconnect(false);
  WiFi.disconnect();
  startAp();  // the meter's own page stays reachable on its hotspot
  mesh::setGateway(false);
  mesh::startLeaf();
}

static void exitLeaf() {
  Serial.println("[net] leaving mesh: trying Wi-Fi again");
  mesh::stopLeaf();
  leafMode = false;
  helloDone = linkWasUp = false;
  WiFi.setAutoReconnect(true);
  if (store::hasWifi()) WiFi.begin(store::wifiSsid().c_str(), store::wifiPass().c_str());
  wifiDownSinceMs = lastWifiRetryMs = millis();
  wifiRetryFromLeaf = true;
}

static void modeLoop() {
#if !MESH_ENABLE
  return;
#endif
  static uint32_t lastCheck = 0;
  uint32_t now = millis();
  if (now - lastCheck < 2000) return;  // settings live in NVS: no need to read them every loop
  lastCheck = now;
  bool meshAllowed = store::meshRole() == 0;  // Auto
  if (!leafMode) {
    if (WiFi.status() == WL_CONNECTED) wifiRetryFromLeaf = false;
    // First loss: give Wi-Fi 90 s. A retry coming from the mesh: only a short try, then back.
    uint32_t wait = wifiRetryFromLeaf ? MESH_WIFI_TRY_MS : MESH_LEAF_AFTER_MS;
    bool wifiGone = store::hasWifi() && WiFi.status() != WL_CONNECTED && wifiDownSinceMs &&
                    now - wifiDownSinceMs > wait;
    bool configured = store::hasAdmin() && (store::meshOnly() || store::hasWifi());
    if (meshAllowed && configured && !testing && (store::meshOnly() || wifiGone)) enterLeaf();
  } else {
    // Leaf: with a working gateway link, try Wi-Fi only every 10 min (the mesh is fine meanwhile);
    // without one for 2 min, try Wi-Fi sooner (the mesh is not helping).
    if (mesh::leafLinked()) noLinkSinceMs = 0;
    else if (!noLinkSinceMs) noLinkSinceMs = now;
    bool canWifi = store::hasWifi() && !store::meshOnly();
    bool retry = mesh::leafLinked() ? now - leafSinceMs > MESH_WIFI_RETRY_MS
                                    : now - noLinkSinceMs > MESH_NOLINK_RETRY_MS;
    if (!meshAllowed || (canWifi && retry)) exitLeaf();
  }
}

void begin() {
  pairLock = xSemaphoreCreateMutex();
  WiFi.persistent(false);
  WiFi.setAutoReconnect(true);
  WiFi.setHostname(("power-" + store::deviceId()).c_str());
  {
    StateLock l;
    g.paired = store::loadServer(srv);
  }
  if (store::hasWifi()) {
    WiFi.mode(WIFI_STA);
    WiFi.begin(store::wifiSsid().c_str(), store::wifiPass().c_str());
    lastWifiRetryMs = millis();
    setNet(NET_WIFI_CONNECTING);
  } else {
    startAp();
    setNet(NET_SETUP_AP);
  }
  mesh::begin();
}

void task(void *) {
  for (;;) {
    sys::heartbeat(sys::HB_NET);
    sys::crumb(sys::CR_NET, "mode");
    modeLoop();
    sys::crumb(sys::CR_NET, "wifi");
    if (!leafMode) wifiLoop();
    bool pending;
    PairRequest r;
    xSemaphoreTake(pairLock, portMAX_DELAY);
    pending = pairPending;
    if (pending) {
      r = pairReq;
      pairPending = false;
    }
    xSemaphoreGive(pairLock);
    if (pending) {
      sys::crumb(sys::CR_NET, "pair");
      doPair(r);
    }
    if (meshPairPending && (int32_t)(millis() - meshPairDeadline) > 0) {
      meshPairPending = false;
      setPairStatus("The gateway meter did not answer. Try again.");
    }

    sys::crumb(sys::CR_NET, "mesh");
    mesh::loop();
    sys::heartbeat(sys::HB_NET);  // a gateway search can take a few seconds
    if (leafMode) {
      sys::crumb(sys::CR_NET, "leaf");
      leafLoop();
    } else if (WiFi.status() == WL_CONNECTED) {
      sys::crumb(sys::CR_NET, "server");
      serverLoop();
    } else {
      if (wsStarted) stopWs();
      helloDone = false;
      markOffline();
    }
    // Auto role: an online meter also relays for nearby leaf meters.
    static uint8_t role = store::meshRole();  // changing the role restarts the meter
    sys::crumb(sys::CR_NET, "gateway role");
    mesh::setGateway(!leafMode && helloDone && WiFi.status() == WL_CONNECTED && role == 0);
    vTaskDelay(pdMS_TO_TICKS(10));
  }
}

}  // namespace net
