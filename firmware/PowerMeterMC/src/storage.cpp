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

// Flashing a different firmware type on this ESP32 (PZEM AC / PZEM DC / rbAmp multi-channel): the
// sessions, kWh counters, relay / trip state and config of the old firmware mean nothing to the new
// one. Clear them (Wi-Fi, admin login, pairing, "disabled" and the sequence numbers stay). The server
// sees the new type and keeps the old history as an archived meter.
static void checkFirmwareType() {
  String prev = pm.getString("fwtype", "");
  if (prev == FW_TYPE) return;
  if (prev.length()) {
    static const char *KEYS[] = {"cfg", "s_sid", "s_start", "s_e0", "s_stat", "s_over", "s_end", "s_stop", "s_tare",
                                 "want", "tripped", "wantm", "tripm", "energy", "nch", "sidc", "started",
                                 "ss0", "ss1", "ss2", "ss3", "ss4", "ss5", "ss6", "auxm"};
    for (const char *k : KEYS) pm.remove(k);
    Serial.printf("[store] firmware type changed (%s -> %s): old sessions / kWh / relay state cleared\n",
                  prev.c_str(), FW_TYPE);
  }
  pm.putString("fwtype", FW_TYPE);
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
  checkFirmwareType();
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
  if (b.version != 102) return false;  // older layout: use defaults, the server re-sends the config
  c = b.c;
  return true;
}
void saveConfig(const MeterConfig &c) {
  CfgBlob b;
  b.version = 102;
  b.c = c;
  pm.putBytes("cfg", &b, sizeof(b));
}

struct SessBlob {
  uint8_t version;
  SessionState s;
};

void loadSession(uint8_t i, SessionState &s) {
  char key[6];
  snprintf(key, sizeof(key), "ss%u", i);
  SessBlob b;
  s = SessionState();
  if (pm.getBytesLength(key) == sizeof(b)) {
    pm.getBytes(key, &b, sizeof(b));
    if (b.version == 1) s = b.s;
  }
  s.startMs = millis();
}
void saveSession(uint8_t i, const SessionState &s) {
  char key[6];
  snprintf(key, sizeof(key), "ss%u", i);
  SessBlob b;
  b.version = 1;
  b.s = s;
  pm.putBytes(key, &b, sizeof(b));
}
uint32_t loadSidCounter() { return pm.getUInt("sidc", 0); }
void saveSidCounter(uint32_t n) { pm.putUInt("sidc", n); }
bool loadEverStarted() { return pm.getBool("started", false); }
void saveEverStarted(bool b) {
  if (pm.getBool("started", false) != b) pm.putBool("started", b);
}

void loadFlags(uint8_t &wantMask, uint8_t &trippedMask) {
  wantMask = pm.getUChar("wantm", 0xFF);
  trippedMask = pm.getUChar("tripm", 0);
}
void saveFlags(uint8_t wantMask, uint8_t trippedMask) {
  if (pm.getUChar("wantm", 0xFF) != wantMask) pm.putUChar("wantm", wantMask);
  if (pm.getUChar("tripm", 0) != trippedMask) pm.putUChar("tripm", trippedMask);
}

void loadEnergy(double e[MC_MAX_CH]) {
  for (int i = 0; i < MC_MAX_CH; i++) e[i] = 0;
  if (pm.getBytesLength("energy") == sizeof(double) * MC_MAX_CH) pm.getBytes("energy", e, sizeof(double) * MC_MAX_CH);
}
void saveEnergy(const double e[MC_MAX_CH]) { pm.putBytes("energy", e, sizeof(double) * MC_MAX_CH); }
uint8_t loadChannels() { return pm.getUChar("nch", 0); }
uint8_t loadAux() { return pm.getUChar("auxm", 0); }
void saveAux(uint8_t mask) {
  if (pm.getUChar("auxm", 0) != mask) pm.putUChar("auxm", mask);
}
void saveChannels(uint8_t n) {
  if (pm.getUChar("nch", 0) != n) pm.putUChar("nch", n);
}

bool loadDisabled() { return pm.getBool("disabled", false); }
void saveDisabled(bool d) {
  if (pm.getBool("disabled", false) != d) pm.putBool("disabled", d);
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

String linkKey() { return pm.getString("lkey", ""); }
void setLinkKey(const String &k) {
  if (pm.getString("lkey", "") != k) pm.putString("lkey", k);
}

static uint32_t sNonce = 0, sNonceReserved = 0;
uint32_t nextLinkNonce() {
  xSemaphoreTake(sSeqLock, portMAX_DELAY);
  if (!sNonceReserved) sNonce = sNonceReserved = pm.getUInt("lnres", 0);
  sNonce++;
  if (sNonce >= sNonceReserved) {
    sNonceReserved = sNonce + 100;
    pm.putUInt("lnres", sNonceReserved);
  }
  uint32_t v = sNonce;
  xSemaphoreGive(sSeqLock);
  return v;
}

uint64_t linkDownLast() { return pm.getULong64("ldn", 0); }
void setLinkDownLast(uint64_t n) { pm.putULong64("ldn", n); }
uint8_t meshRole() { return pm.getUChar("mrole", 0); }
void setMeshRole(uint8_t r) { pm.putUChar("mrole", r); }
bool meshOnly() { return pm.getBool("monly", false); }
void setMeshOnly(bool on) { pm.putBool("monly", on); }

void factoryReset() {
  pm.clear();  // identity + AP password stay in "ident"
}

}  // namespace store
