#include "storage.h"

#include "../config.h"

#include <Preferences.h>
#include <esp_random.h>
#include <mbedtls/sha256.h>

namespace store {

static Preferences ident;  // identity, never wiped
static Preferences pm;     // everything else
static String sDeviceId, sApPass;
static uint32_t sBoot = 0;
static uint32_t sSeq = 0, sSeqReserved = 0;
static SemaphoreHandle_t sSeqLock;
static const uint32_t SEQ_BLOCK = 200;

static const char *ID_ALPHABET = "23456789ABCDEFGHJKMNPQRSTUVWXYZ";
static const char *PW_ALPHABET = "abcdefghjkmnpqrstuvwxyz23456789";

static void sha256(const uint8_t *data, size_t len, uint8_t out[32]) {
  mbedtls_sha256(data, len, out, 0);
}

static String hashPassword(const String &salt, const String &pass) {
  uint8_t h[32];
  String s = salt + ":" + pass;
  sha256((const uint8_t *)s.c_str(), s.length(), h);
  for (int i = 0; i < 2000; i++) sha256(h, sizeof(h), h);  // slow down brute force a little
  char hex[65];
  for (int i = 0; i < 32; i++) sprintf(hex + i * 2, "%02x", h[i]);
  return String(hex);
}

static String randomString(const char *alphabet, int len) {
  String s;
  int n = strlen(alphabet);
  for (int i = 0; i < len; i++) s += alphabet[esp_random() % n];
  return s;
}

void begin() {
  sSeqLock = xSemaphoreCreateMutex();
  ident.begin("ident", false);
  pm.begin("pm", false);

  sDeviceId = ident.getString("devid", "");
  if (sDeviceId.length() != 8) {
    uint64_t mac = ESP.getEfuseMac();
    uint8_t h[32];
    sha256((const uint8_t *)&mac, sizeof(mac), h);
    sDeviceId = "";
    for (int i = 0; i < 8; i++) sDeviceId += ID_ALPHABET[h[i] % strlen(ID_ALPHABET)];
    ident.putString("devid", sDeviceId);
  }
  sApPass = ident.getString("appass", "");
  if (sApPass.length() < 8) {
    sApPass = randomString(PW_ALPHABET, 8);
    ident.putString("appass", sApPass);
  }
  sBoot = pm.getUInt("boot", 0) + 1;
  pm.putUInt("boot", sBoot);

  // Sequence numbers are reserved in blocks so NVS is not written for every item.
  sSeqReserved = pm.getUInt("seqres", 0);
  sSeq = sSeqReserved;
}

String deviceId() { return sDeviceId; }
String apPassword() {
#if AP_PASSWORD_IS_DEVICE_ID
  return sDeviceId;  // 8 characters, meets the WPA2 minimum
#else
  return sApPass;
#endif
}
uint32_t bootCount() { return sBoot; }

bool hasWifi() { return pm.getString("wssid", "").length() > 0; }
String wifiSsid() { return pm.getString("wssid", ""); }
String wifiPass() { return pm.getString("wpass", ""); }
void setWifi(const String &ssid, const String &pass) {
  pm.putString("wssid", ssid);
  pm.putString("wpass", pass);
}

bool hasAdmin() { return pm.getString("auser", "").length() > 0; }
String adminUser() { return pm.getString("auser", ""); }
void setAdmin(const String &user, const String &pass) {
  String salt = randomString(PW_ALPHABET, 16);
  String u = user;
  u.toLowerCase();
  pm.putString("auser", u);
  pm.putString("asalt", salt);
  pm.putString("ahash", hashPassword(salt, pass));
}
bool checkAdmin(const String &user, const String &pass) {
  String u = user;
  u.toLowerCase();
  if (!hasAdmin() || u != adminUser()) return false;
  return hashPassword(pm.getString("asalt", ""), pass) == pm.getString("ahash", "x");
}

bool loadServer(Server &s) {
  s.host = pm.getString("shost", "");
  s.port = pm.getUShort("sport", 443);
  s.tls = pm.getBool("stls", true);
  s.insecure = pm.getBool("sinsec", false);
  s.secret = pm.getString("ssecret", "");
  return s.host.length() > 0 && s.secret.length() > 0;
}
void saveServer(const Server &s) {
  pm.putString("shost", s.host);
  pm.putUShort("sport", s.port);
  pm.putBool("stls", s.tls);
  pm.putBool("sinsec", s.insecure);
  pm.putString("ssecret", s.secret);
}
void clearSecret() { pm.remove("ssecret"); }

struct CfgBlob {
  uint8_t version;
  MeterConfig c;
};

bool loadConfig(MeterConfig &c) {
  CfgBlob b;
  if (pm.getBytesLength("cfg") != sizeof(b)) return false;
  pm.getBytes("cfg", &b, sizeof(b));
  if (b.version != 2) return false;  // older layout: use defaults, the server re-sends the config
  c = b.c;
  return true;
}
void saveConfig(const MeterConfig &c) {
  CfgBlob b;
  b.version = 2;
  b.c = c;
  pm.putBytes("cfg", &b, sizeof(b));
}

void loadSession(SessionState &s) {
  s.sid = pm.getUInt("s_sid", 0);
  s.start = pm.getUInt("s_start", 0);
  s.e0 = pm.getDouble("s_e0", 0);
  s.status = (SessStatus)pm.getUChar("s_stat", SESS_ACTIVE);
  s.over = pm.getBool("s_over", false);
  s.startMs = millis();
}
void saveSession(const SessionState &s) {
  pm.putUInt("s_sid", s.sid);
  pm.putUInt("s_start", s.start);
  pm.putDouble("s_e0", s.e0);
  pm.putUChar("s_stat", (uint8_t)s.status);
  pm.putBool("s_over", s.over);
}

void loadFlags(bool &wantOn, bool &tripped) {
  wantOn = pm.getBool("want", true);
  tripped = pm.getBool("tripped", false);
}
void saveFlags(bool wantOn, bool tripped) {
  if (pm.getBool("want", true) != wantOn) pm.putBool("want", wantOn);
  if (pm.getBool("tripped", false) != tripped) pm.putBool("tripped", tripped);
}

uint32_t nextSeq() {
  xSemaphoreTake(sSeqLock, portMAX_DELAY);
  sSeq++;
  if (sSeq >= sSeqReserved) {
    sSeqReserved = sSeq + SEQ_BLOCK;
    pm.putUInt("seqres", sSeqReserved);
  }
  uint32_t v = sSeq;
  xSemaphoreGive(sSeqLock);
  return v;
}

void factoryReset() {
  pm.clear();  // identity + AP password stay in "ident"
}

}  // namespace store
