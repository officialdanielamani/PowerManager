#include "web.h"

#include <ArduinoJson.h>
#include <DNSServer.h>
#include <WebServer.h>
#include <WiFi.h>
#include <esp_random.h>

#include "../config.h"
#include "app_state.h"
#include "control.h"
#include "net.h"
#include "storage.h"
#include "system.h"
#include "timekeep.h"
#include "webui.h"

namespace web {

static WebServer server(80);
static DNSServer dns;
static bool dnsRunning = false;

// ------------------------------------------------------------------ sessions

struct WebSession {
  char token[33];
  uint32_t lastMs;
};
static WebSession sessions[4];
static uint8_t failedLogins = 0;
static uint32_t lockedUntilMs = 0;

static String newToken() {
  char buf[33];
  for (int i = 0; i < 16; i++) sprintf(buf + i * 2, "%02x", (uint8_t)(esp_random() & 0xFF));
  buf[32] = 0;
  // replace the oldest session
  int idx = 0;
  for (int i = 1; i < 4; i++)
    if (sessions[i].lastMs < sessions[idx].lastMs) idx = i;
  memcpy(sessions[idx].token, buf, 33);
  sessions[idx].lastMs = millis() | 1;
  return String(buf);
}

static String cookieToken() {
  String c = server.header("Cookie");
  int i = c.indexOf("pmsid=");
  if (i < 0) return "";
  int end = c.indexOf(';', i);
  return c.substring(i + 6, end < 0 ? c.length() : end);
}

static bool authed() {
  String t = cookieToken();
  if (t.length() != 32) return false;
  for (auto &s : sessions) {
    if (s.lastMs && t.equals(s.token)) {
      if (millis() - s.lastMs > WEB_SESSION_TTL_S * 1000UL) {
        s.lastMs = 0;
        return false;
      }
      s.lastMs = millis() | 1;
      return true;
    }
  }
  return false;
}

static void dropSessions() {
  for (auto &s : sessions) s.lastMs = 0;
}

// ------------------------------------------------------------------ helpers

static void sendJson(int code, JsonDocument &doc) {
  String s;
  serializeJson(doc, s);
  server.sendHeader("Cache-Control", "no-store");
  server.send(code, "application/json", s);
}

static void sendOk(const char *msg = nullptr) {
  JsonDocument d;
  d["ok"] = true;
  if (msg) d["message"] = msg;
  sendJson(200, d);
}

static void sendErr(int code, const String &msg) {
  JsonDocument d;
  d["ok"] = false;
  d["error"] = msg;
  sendJson(code, d);
}

static bool body(JsonDocument &doc) {
  if (deserializeJson(doc, server.arg("plain"))) {
    sendErr(400, "Bad request");
    return false;
  }
  return true;
}

// The browser sends its clock with every API call; used only while no better source exists.
static void browserTime() {
  String t = server.header("X-Time");
  if (t.length()) timekeep::set((uint32_t)t.toInt(), TS_BROWSER);
}

static bool requireAuth() {
  browserTime();
  if (!store::hasAdmin()) {
    sendErr(409, "setup");
    return false;
  }
  if (!authed()) {
    sendErr(401, "Login required");
    return false;
  }
  return true;
}

static String passwordProblem(const String &p) {
  if (p.length() < 10 || p.length() > 64) return "Password must be 10-64 characters";
  bool lo = false, up = false, dg = false, sy = false;
  for (char c : p) {
    if (islower(c)) lo = true;
    else if (isupper(c)) up = true;
    else if (isdigit(c)) dg = true;
    else sy = true;
  }
  if (!lo || !up) return "Password needs upper and lower case letters";
  if (!dg) return "Password needs a digit";
  if (!sy) return "Password needs a symbol";
  return "";
}

static String usernameProblem(const String &u) {
  if (u.length() < 3 || u.length() > 32) return "Username must be 3-32 characters";
  for (char c : u)
    if (!isalnum(c) && c != '_' && c != '.' && c != '-') return "Username: letters, digits, _ . - only";
  return "";
}

// ------------------------------------------------------------------ handlers

static void hIndex() {
  server.sendHeader("Cache-Control", "no-store");
  server.send_P(200, "text/html", INDEX_HTML);
}

static void hStatus() {  // public: tells the page which screen to show
  browserTime();
  JsonDocument d;
  d["device_id"] = store::deviceId();
  d["fw"] = FW_VERSION;
  d["admin"] = store::hasAdmin();
  d["wifi"] = store::hasWifi();
  d["auth"] = store::hasAdmin() && authed();
  StateLock l;
  d["paired"] = g.paired && !g.unauthorized;
  sendJson(200, d);
}

static void setCookie(const String &token) {
  server.sendHeader("Set-Cookie", "pmsid=" + token + "; Path=/; HttpOnly; SameSite=Strict");
}

static void hSetupAdmin() {
  if (store::hasAdmin()) return sendErr(409, "Admin already exists");
  JsonDocument b;
  if (!body(b)) return;
  String u = b["user"] | "", p = b["pass"] | "", p2 = b["confirm"] | "";
  u.trim();
  u.toLowerCase();
  String e = usernameProblem(u);
  if (!e.length()) e = p != p2 ? "Passwords do not match" : passwordProblem(p);
  if (e.length()) return sendErr(400, e);
  store::setAdmin(u, p);
  setCookie(newToken());
  sendOk();
}

static void hLogin() {
  browserTime();
  if (millis() < lockedUntilMs) return sendErr(429, "Too many attempts, wait a minute");
  JsonDocument b;
  if (!body(b)) return;
  if (!store::checkAdmin(b["user"] | "", b["pass"] | "")) {
    if (++failedLogins >= 5) {
      failedLogins = 0;
      lockedUntilMs = millis() + 60000;
    }
    return sendErr(401, "Wrong username or password");
  }
  failedLogins = 0;
  setCookie(newToken());
  sendOk();
}

static void hLogout() {
  String t = cookieToken();
  for (auto &s : sessions)
    if (t.equals(s.token)) s.lastMs = 0;
  sendOk();
}

static void hScan() {
  if (store::hasAdmin() && !requireAuth()) return;
  int n = WiFi.scanComplete();
  JsonDocument d;
  if (n == WIFI_SCAN_FAILED || (n >= 0 && server.hasArg("refresh"))) {
    WiFi.scanDelete();
    WiFi.scanNetworks(true);
    n = WIFI_SCAN_RUNNING;
  }
  d["running"] = n == WIFI_SCAN_RUNNING;
  JsonArray arr = d["networks"].to<JsonArray>();
  for (int i = 0; i < n; i++) {
    String ssid = WiFi.SSID(i);
    if (!ssid.length()) continue;
    bool dup = false;
    for (JsonObject o : arr)
      if (ssid == (const char *)o["ssid"]) dup = true;
    if (dup) continue;
    JsonObject o = arr.add<JsonObject>();
    o["ssid"] = ssid;
    o["rssi"] = WiFi.RSSI(i);
    o["open"] = WiFi.encryptionType(i) == WIFI_AUTH_OPEN;
  }
  sendJson(200, d);
}

static void hWifi() {
  if (!requireAuth()) return;
  JsonDocument b;
  if (!body(b)) return;
  String ssid = b["ssid"] | "", pass = b["pass"] | "";
  if (!ssid.length() || ssid.length() > 32) return sendErr(400, "SSID must be 1-32 characters");
  if (pass.length() && (pass.length() < 8 || pass.length() > 63)) return sendErr(400, "Wi-Fi password must be 8-63 characters");
  if (!store::hasWifi()) {
    net::tryWifi(ssid, pass);  // first setup: test while the setup AP stays on
    return sendOk("Connecting…");
  }
  store::setWifi(ssid, pass);  // changing networks later: save and restart
  sendOk("Saved. Restarting…");
  sys::reboot(1500);
}

static void hWifiStatus() {
  if (!requireAuth()) return;
  JsonDocument d;
  d["status"] = net::wifiTestStatus();
  d["ip"] = net::ipAddress();
  String host = "power-" + store::deviceId() + ".local";
  host.toLowerCase();
  d["mdns"] = host;
  sendJson(200, d);
}

static void hState() {
  if (!requireAuth()) return;
  JsonDocument d;
  d["device_id"] = store::deviceId();
  d["fw"] = FW_VERSION;
  d["time"] = timekeep::valid() ? timekeep::now() : 0;
  d["time_src"] = timekeep::sourceName();
  d["uptime"] = millis() / 1000;
  d["ip"] = net::ipAddress();
  d["rssi"] = net::rssi();
  d["ssid"] = store::wifiSsid();
  d["server"] = net::serverHost();
  d["outbox"] = net::outboxCount();
  d["next_reset"] = control::nextReset();
  d["kwh"] = serialized(String(control::sessionKwh(), 4));
  control::stateJson(d["state"].to<JsonObject>());
  StateLock l;
  static const char *NET[] = {"setup", "wifi_connecting", "not_paired", "connecting", "online"};
  d["net"] = NET[g.net];
  d["paired"] = g.paired && !g.unauthorized;
  d["unauthorized"] = g.unauthorized;
  d["last_sync"] = g.lastSyncEpoch;
  JsonObject r = d["reading"].to<JsonObject>();
  r["ok"] = g.reading.ok;
  if (g.reading.ok) {
    r["v"] = g.reading.v;
    r["a"] = g.reading.a;
    r["w"] = g.reading.w;
    r["pf"] = g.reading.pf;
    r["hz"] = g.reading.hz;
    r["e"] = g.reading.e;
  }
  JsonObject c = d["cfg"].to<JsonObject>();
  c["report_s"] = g.cfg.report_s;
  c["kwh_max"] = g.cfg.kwh_max;
  static const char *KA[] = {"end", "hold", "warn"};
  c["kwh_action"] = KA[g.cfg.kwh_action % 3];
  c["amp_max"] = g.cfg.amp_max;
  c["auto"] = g.cfg.auto_renew;
  c["mode"] = g.cfg.sess_mode == SESS_DAILY ? "daily" : g.cfg.sess_mode == SESS_DURATION ? "duration" : "none";
  c["offline_off_min"] = g.cfg.offline_off_min;
  sendJson(200, d);
}

static void hRelay() {
  if (!requireAuth()) return;
  JsonDocument b;
  if (!body(b)) return;
  const char *err = nullptr;
  if (!control::setRelay(b["on"] | false, "local", err)) {
    String msg = !strcmp(err, "tripped") ? "Blocked: over-current trip. Reset the trip first."
               : !strcmp(err, "quota_exceeded") ? "Blocked: kWh quota for this session is used up."
               : "Blocked: session ended. Renew it on the server.";
    return sendErr(409, msg);
  }
  sendOk();
}

static void hSendNow() {
  if (!requireAuth()) return;
  control::reportNow();
  sendOk("Queued");
}

static void hResetTrip() {
  if (!requireAuth()) return;
  control::resetTrip("local", store::adminUser().c_str());
  sendOk();
}

static void hPair() {
  if (!requireAuth()) return;
  JsonDocument b;
  if (!body(b)) return;
  net::PairRequest r;
  r.host = b["host"] | "";
  r.host.trim();
  r.port = b["port"] | 0;
  r.tls = b["tls"] | true;
  r.insecure = b["insecure"] | false;
  r.code = b["code"] | "";
  r.code.replace("-", "");
  r.code.trim();
  r.code.toUpperCase();
  if (!r.port) r.port = r.tls ? 443 : 80;
  if (!r.host.length() || r.host.indexOf('/') >= 0) return sendErr(400, "Server address is required (no http://)");
  if (r.code.length() != 8) return sendErr(400, "Pairing code must be 8 characters");
  if (WiFi.status() != WL_CONNECTED) return sendErr(409, "Wi-Fi is not connected yet");
  net::requestPair(r);
  sendOk();
}

static void hPairStatus() {
  if (!requireAuth()) return;
  JsonDocument d;
  d["status"] = net::pairStatus();
  sendJson(200, d);
}

static void hPassword() {
  if (!requireAuth()) return;
  JsonDocument b;
  if (!body(b)) return;
  String old = b["old"] | "", p = b["pass"] | "", p2 = b["confirm"] | "";
  if (!store::checkAdmin(store::adminUser(), old)) return sendErr(400, "Current password is wrong");
  String e = p != p2 ? "Passwords do not match" : passwordProblem(p);
  if (e.length()) return sendErr(400, e);
  store::setAdmin(store::adminUser(), p);
  dropSessions();
  setCookie(newToken());
  sendOk("Password changed");
}

static void hReboot() {
  if (!requireAuth()) return;
  sendOk("Restarting…");
  sys::reboot(1000);
}

static void hFactoryReset() {
  if (!requireAuth()) return;
  sendOk("Factory reset…");
  delay(300);
  sys::factoryReset("local web");
}

static void hNotFound() {
  // Captive portal: send any other host name to our page while the setup AP is on.
  if (g.apActive && server.hostHeader() != WiFi.softAPIP().toString()) {
    server.sendHeader("Location", "http://" + WiFi.softAPIP().toString() + "/", true);
    server.send(302, "text/plain", "");
    return;
  }
  server.send(404, "text/plain", "Not found");
}

// ------------------------------------------------------------------ setup / task

void begin() {
  const char *hdrs[] = {"Cookie", "X-Time"};
  server.collectHeaders(hdrs, 2);
  server.on("/", HTTP_GET, hIndex);
  server.on("/api/status", HTTP_GET, hStatus);
  server.on("/api/setup/admin", HTTP_POST, hSetupAdmin);
  server.on("/api/login", HTTP_POST, hLogin);
  server.on("/api/logout", HTTP_POST, hLogout);
  server.on("/api/wifi/scan", HTTP_GET, hScan);
  server.on("/api/wifi", HTTP_POST, hWifi);
  server.on("/api/wifi/status", HTTP_GET, hWifiStatus);
  server.on("/api/state", HTTP_GET, hState);
  server.on("/api/relay", HTTP_POST, hRelay);
  server.on("/api/send_now", HTTP_POST, hSendNow);
  server.on("/api/reset_trip", HTTP_POST, hResetTrip);
  server.on("/api/pair", HTTP_POST, hPair);
  server.on("/api/pair/status", HTTP_GET, hPairStatus);
  server.on("/api/password", HTTP_POST, hPassword);
  server.on("/api/reboot", HTTP_POST, hReboot);
  server.on("/api/factory_reset", HTTP_POST, hFactoryReset);
  server.onNotFound(hNotFound);
  server.begin();
}

void task(void *) {
  for (;;) {
    sys::heartbeat(sys::HB_WEB);
    bool ap;
    {
      StateLock l;
      ap = g.apActive;
    }
    if (ap && !dnsRunning) {
      dns.start(53, "*", WiFi.softAPIP());
      dnsRunning = true;
    } else if (!ap && dnsRunning) {
      dns.stop();
      dnsRunning = false;
    }
    if (dnsRunning) dns.processNextRequest();
    server.handleClient();
    vTaskDelay(pdMS_TO_TICKS(5));
  }
}

}  // namespace web
