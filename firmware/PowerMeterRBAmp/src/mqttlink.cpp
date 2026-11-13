#include "mqttlink.h"

#include <Preferences.h>
#include <mbedtls/md.h>
#include <vector>

#include "certs.h"
#include "storage.h"
#include "timekeep.h"

namespace mqttlink {

static Cfg cfg_;
static Preferences prefs;

void begin() {
  prefs.begin("mqtt", true);
  cfg_.enabled = prefs.getBool("en", false);
  cfg_.host = prefs.getString("host", "");
  cfg_.port = prefs.getUShort("port", 443);
  cfg_.tls = prefs.getBool("tls", true);
  cfg_.insecure = prefs.getBool("insec", false);
  cfg_.path = prefs.getString("path", "/");
  cfg_.user = prefs.getString("user", "");
  cfg_.pass = prefs.getString("pass", "");
  cfg_.prefix = prefs.getString("prefix", "powermanager");
  prefs.end();
}

const Cfg &cfg() { return cfg_; }

void save(const Cfg &c) {
  cfg_ = c;
  prefs.begin("mqtt", false);
  prefs.putBool("en", c.enabled);
  prefs.putString("host", c.host);
  prefs.putUShort("port", c.port);
  prefs.putBool("tls", c.tls);
  prefs.putBool("insec", c.insecure);
  prefs.putString("path", c.path);
  prefs.putString("user", c.user);
  prefs.putString("pass", c.pass);
  prefs.putString("prefix", c.prefix);
  prefs.end();
}

void wipe() {
  prefs.begin("mqtt", false);
  prefs.clear();
  prefs.end();
  cfg_ = Cfg();
}

bool fromJson(JsonObjectConst o, Cfg &out, const char *&err) {
  Cfg c;
  c.enabled = o["enabled"] | true;
  c.host = String(o["host"] | "");
  int port = o["port"] | 443;
  c.tls = o["tls"] | true;
  c.insecure = o["insecure"] | false;
  c.path = String(o["path"] | "/");
  c.user = String(o["user"] | "");
  c.pass = String(o["pass"] | "");
  c.prefix = String(o["prefix"] | "powermanager");
  if (c.path.length() == 0 || c.path[0] != '/') c.path = "/" + c.path;
  if (c.enabled && (c.host.length() < 1 || c.host.length() > 100)) err = "mqtt.host";
  else if (port < 1 || port > 65535) err = "mqtt.port";
  else if (c.path.length() > 64 || c.user.length() > 64 || c.pass.length() > 128) err = "mqtt.login";
  else if (c.prefix.length() < 1 || c.prefix.length() > 48 || strpbrk(c.prefix.c_str(), "#+ "))
    err = "mqtt.prefix";
  if (err) return false;
  c.port = port;
  out = c;
  return true;
}

bool active() { return NET_MQTT && cfg_.enabled && cfg_.host.length() > 0; }

#if NET_MQTT
// ------------------------------------------------------------------ signing

static void hmac(const String &key, const char *devId, uint64_t n, const char *m, size_t mlen, char out[33]) {
  char head[40];
  snprintf(head, sizeof(head), "%s|%llu|", devId, (unsigned long long)n);
  uint8_t mac[32];
  const mbedtls_md_info_t *info = mbedtls_md_info_from_type(MBEDTLS_MD_SHA256);
  mbedtls_md_context_t ctx;
  mbedtls_md_init(&ctx);
  mbedtls_md_setup(&ctx, info, 1);
  mbedtls_md_hmac_starts(&ctx, (const uint8_t *)key.c_str(), key.length());
  mbedtls_md_hmac_update(&ctx, (const uint8_t *)head, strlen(head));
  mbedtls_md_hmac_update(&ctx, (const uint8_t *)m, mlen);
  mbedtls_md_hmac_finish(&ctx, mac);
  mbedtls_md_free(&ctx);
  for (int i = 0; i < 16; i++) snprintf(out + i * 2, 3, "%02x", mac[i]);  // first 16 bytes, hex
}

// ------------------------------------------------------------------ MQTT 3.1.1 packets (QoS 0)

static std::vector<uint8_t> rx;
static bool subscribed = false;
static uint32_t lastTxMs = 0;
static uint64_t lastDown = 0;

static String topic(const char *leaf) { return cfg_.prefix + "/" + store::deviceId() + "/" + leaf; }

static void putStr(std::vector<uint8_t> &b, const String &s) {
  b.push_back(s.length() >> 8);
  b.push_back(s.length() & 0xFF);
  b.insert(b.end(), s.c_str(), s.c_str() + s.length());
}

static bool sendPacket(WebSocketsClient &ws, uint8_t type, const std::vector<uint8_t> &body) {
  std::vector<uint8_t> p;
  p.reserve(body.size() + 5);
  p.push_back(type);
  size_t len = body.size();
  do {  // remaining length (variable length encoding)
    uint8_t d = len % 128;
    len /= 128;
    p.push_back(len ? d | 0x80 : d);
  } while (len);
  p.insert(p.end(), body.begin(), body.end());
  lastTxMs = millis();
  return ws.sendBIN(p.data(), p.size());
}

static bool publishRaw(WebSocketsClient &ws, const String &t, const String &payload, bool retain) {
  std::vector<uint8_t> b;
  b.reserve(t.length() + payload.length() + 2);
  putStr(b, t);
  b.insert(b.end(), payload.c_str(), payload.c_str() + payload.length());
  return sendPacket(ws, retain ? 0x31 : 0x30, b);
}

void reset() {
  rx.clear();
  subscribed = false;
}

void start(WebSocketsClient &ws) {
  reset();
  ws.setExtraHeaders("");
  if (cfg_.tls) {
    if (cfg_.insecure) ws.beginSSL(cfg_.host.c_str(), cfg_.port, cfg_.path.c_str(), "", "mqtt");
    else ws.beginSslWithCA(cfg_.host.c_str(), cfg_.port, cfg_.path.c_str(), ROOT_CA_PEM, "mqtt");
  } else {
    ws.begin(cfg_.host.c_str(), cfg_.port, cfg_.path.c_str(), "mqtt");
  }
  Serial.printf("[mqtt] connecting to %s://%s:%u%s (topics %s/%s/...)\n", cfg_.tls ? "wss" : "ws", cfg_.host.c_str(),
                cfg_.port, cfg_.path.c_str(), cfg_.prefix.c_str(), store::deviceId().c_str());
}

void onWsConnected(WebSocketsClient &ws) {
  reset();
  std::vector<uint8_t> b = {0x00, 0x04, 'M', 'Q', 'T', 'T', 0x04};
  uint8_t flags = 0x02 | 0x04 | 0x20;  // clean session, last will, will retained
  if (cfg_.user.length()) flags |= 0x80;
  if (cfg_.user.length() && cfg_.pass.length()) flags |= 0x40;
  b.push_back(flags);
  b.push_back(0);
  b.push_back(60);  // keep-alive 60 s (the protocol pings every 30 s anyway)
  putStr(b, "pm-" + store::deviceId());
  putStr(b, topic("status"));
  putStr(b, "offline");
  if (flags & 0x80) putStr(b, cfg_.user);
  if (flags & 0x40) putStr(b, cfg_.pass);
  sendPacket(ws, 0x10, b);
}

// One verified protocol message out of a PUBLISH on our "down" topic.
static bool openEnvelope(const uint8_t *payload, size_t len, String &inner) {
  JsonDocument doc;
  if (deserializeJson(doc, (const char *)payload, len)) return false;
  const char *m = doc["m"] | (const char *)nullptr;
  const char *h = doc["h"] | "";
  uint64_t n = doc["n"] | (uint64_t)0;
  if (!m || !n) return false;
  String key = store::linkKey();
  char want[33];
  hmac(key, store::deviceId().c_str(), n, m, strlen(m), want);
  bool fresh = n > lastDown;
  // The server's counter is its clock in ms: with a valid clock, refuse anything older than 10 minutes
  // (a recording replayed after a restart of the meter).
  if (fresh && timekeep::valid() && n / 1000 + 600 < (uint64_t)timekeep::now()) fresh = false;
  if (!key.length() || strcmp(want, h) != 0 || !fresh) {
    Serial.println("[mqtt] dropped a message (bad signature or replay)");
    return false;
  }
  lastDown = n;
  inner = m;
  return true;
}

Result onBinary(WebSocketsClient &ws, const uint8_t *data, size_t len, String &inner) {
  if (len) {
    if (rx.size() + len > 40000) rx.clear();  // never happens with our message sizes
    rx.insert(rx.end(), data, data + len);
  }
  while (rx.size() >= 2) {
    size_t rem = 0, mult = 1, i = 1;
    bool complete = false;
    for (; i < rx.size() && i <= 4; i++) {
      rem += (rx[i] & 0x7F) * mult;
      mult *= 128;
      if (!(rx[i] & 0x80)) {
        complete = true;
        i++;
        break;
      }
    }
    if (!complete || rx.size() < i + rem) return NOTHING;  // wait for the rest of the packet
    uint8_t type = rx[0] & 0xF0;
    const uint8_t *body = rx.data() + i;
    Result res = NOTHING;
    if (type == 0x20) {  // CONNACK
      if (rem >= 2 && body[1] == 0) {
        publishRaw(ws, topic("status"), "online", true);
        std::vector<uint8_t> b = {0x00, 0x01};  // packet id 1
        putStr(b, topic("down"));
        b.push_back(0);  // QoS 0
        sendPacket(ws, 0x82, b);
      } else {
        Serial.printf("[mqtt] broker refused the login (code %u)\n", rem >= 2 ? body[1] : 255);
        res = REFUSED;
      }
    } else if (type == 0x90) {  // SUBACK
      subscribed = true;
      Serial.println("[mqtt] connected to the broker");
      res = READY;
    } else if (type == 0x30 && rem >= 2) {  // PUBLISH
      size_t tl = (body[0] << 8) | body[1];
      size_t off = 2 + tl + (((rx[0] >> 1) & 3) ? 2 : 0);  // + packet id when QoS > 0
      if (off <= rem && openEnvelope(body + off, rem - off, inner)) res = MESSAGE;
    }
    rx.erase(rx.begin(), rx.begin() + i + rem);
    if (res != NOTHING) return res;
  }
  return NOTHING;
}

bool publish(WebSocketsClient &ws, const String &inner) {
  if (!subscribed) return false;
  uint32_t n = store::nextLinkNonce();
  char h[33];
  hmac(store::linkKey(), store::deviceId().c_str(), n, inner.c_str(), inner.length(), h);
  JsonDocument doc;
  doc["n"] = n;
  doc["h"] = h;
  doc["m"] = inner;
  String payload;
  serializeJson(doc, payload);
  return publishRaw(ws, topic("up"), payload, false);
}

void loop(WebSocketsClient &ws) {
  if (subscribed && millis() - lastTxMs > 45000) sendPacket(ws, 0xC0, {});  // PINGREQ
}
#endif  // NET_MQTT

}  // namespace mqttlink
