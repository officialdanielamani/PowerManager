#include "mesh.h"

#include <ArduinoJson.h>
#include <WiFi.h>
#include <esp_idf_version.h>
#include <esp_now.h>
#include <esp_wifi.h>
#include <mbedtls/md.h>

#include "../config.h"
#include "net.h"
#include "storage.h"

namespace mesh {

// ------------------------------------------------------------------ frames
// [0]='P' [1]=type [2..3]=message id [4]=fragment index [5]=fragment count [6..]=payload
enum FrameType : uint8_t { F_BEACON = 1, F_UP = 2, F_DOWN = 3, F_PAIR_REQ = 4, F_PAIR_RES = 5 };
static const uint8_t MAGIC = 'P';
static const size_t HDR = 6;
static const size_t FRAME_MAX = 250;          // ESP-NOW v1 limit
static const size_t CHUNK = FRAME_MAX - HDR;  // 244
static const size_t MSG_MAX = 4096;           // biggest reassembled message
static const uint8_t BCAST[6] = {0xFF, 0xFF, 0xFF, 0xFF, 0xFF, 0xFF};

struct RxFrame {
  uint8_t mac[6];
  int8_t rssi;
  uint8_t len;
  uint8_t data[FRAME_MAX];
};
static QueueHandle_t rxq = nullptr;
static volatile uint16_t sendFailsInRow = 0;  // reset by every delivered frame
static uint16_t nextMsgId = 1;
static bool ready = false;

struct Slot {  // reassembly
  bool used;
  uint8_t mac[6];
  uint16_t id;
  uint8_t type, count;
  uint32_t got;  // bitmask of received fragments
  uint32_t startMs;
  size_t len;
  char buf[MSG_MAX + 1];
};
static Slot slots[3];

// ------------------------------------------------------------------ state
static bool gateway = false;
static uint32_t lastBeaconMs = 0;
struct Leaf {
  char id[17];
  uint8_t mac[6];
  uint32_t lastMs;
};
static Leaf leaves[MESH_MAX_LEAVES];

static bool leaf = false, linked = false;
static uint8_t gwMac[6];
static String gwId;
static uint8_t gwChannel = 0, lastGoodChannel = 0;
static uint32_t gwSeenMs = 0, lastScanMs = 0;
// scan results
static bool scanning = false;
static uint8_t candMac[6], candChannel = 0;
static String candId;
static int8_t candRssi = -127;

// ------------------------------------------------------------------ callbacks (Wi-Fi task)

// Runs inside the Wi-Fi driver task, which has a small stack: copy into a static buffer (not on
// the stack), queue it, and return. Everything else happens later in the net task.
static RxFrame rxBuf;
static void onRecv(const uint8_t *mac, const uint8_t *data, int len, int8_t rssi) {
  if (!rxq || !mac || !data || len < (int)HDR || len > (int)FRAME_MAX || data[0] != MAGIC) return;
  memcpy(rxBuf.mac, mac, 6);
  rxBuf.rssi = rssi;
  rxBuf.len = len;
  memcpy(rxBuf.data, data, len);
  xQueueSend(rxq, &rxBuf, 0);  // copies; drop if full
}

#if ESP_IDF_VERSION >= ESP_IDF_VERSION_VAL(5, 0, 0)
static void recvCb(const esp_now_recv_info_t *info, const uint8_t *data, int len) {
  onRecv(info->src_addr, data, len, info->rx_ctrl ? info->rx_ctrl->rssi : 0);
}
#else
static void recvCb(const uint8_t *mac, const uint8_t *data, int len) { onRecv(mac, data, len, 0); }
#endif

#if ESP_IDF_VERSION >= ESP_IDF_VERSION_VAL(5, 5, 0)
static void sendCb(const esp_now_send_info_t *, esp_now_send_status_t status) {
#else
static void sendCb(const uint8_t *, esp_now_send_status_t status) {
#endif
  if (status != ESP_NOW_SEND_SUCCESS) sendFailsInRow = sendFailsInRow + 1;
  else sendFailsInRow = 0;
}

// ------------------------------------------------------------------ helpers

static void hmac(const String &key, const char *devId, uint64_t n, const String &m, char out[33]) {
  char head[48];
  snprintf(head, sizeof(head), "%s|%llu|", devId, (unsigned long long)n);
  uint8_t mac[32];
  const mbedtls_md_info_t *info = mbedtls_md_info_from_type(MBEDTLS_MD_SHA256);
  mbedtls_md_context_t ctx;
  mbedtls_md_init(&ctx);
  mbedtls_md_setup(&ctx, info, 1);
  mbedtls_md_hmac_starts(&ctx, (const uint8_t *)key.c_str(), key.length());
  mbedtls_md_hmac_update(&ctx, (const uint8_t *)head, strlen(head));
  mbedtls_md_hmac_update(&ctx, (const uint8_t *)m.c_str(), m.length());
  mbedtls_md_hmac_finish(&ctx, mac);
  mbedtls_md_free(&ctx);
  for (int i = 0; i < 16; i++) sprintf(out + i * 2, "%02x", mac[i]);  // first 128 bits, like the server
  out[32] = 0;
}

static bool addPeer(const uint8_t *mac) {
  if (esp_now_is_peer_exist(mac)) return true;
  esp_now_peer_info_t p = {};
  memcpy(p.peer_addr, mac, 6);
  p.channel = 0;  // current channel
  p.ifidx = WIFI_IF_STA;
  p.encrypt = false;  // integrity comes from the HMAC on every message
  return esp_now_add_peer(&p) == ESP_OK;
}

static bool sendFrames(const uint8_t *mac, FrameType type, const char *data, size_t len) {
  if (!ready || len > MSG_MAX) return false;
  if (!addPeer(mac)) return false;
  uint8_t count = len ? (len + CHUNK - 1) / CHUNK : 1;
  uint16_t id = nextMsgId++;
  uint8_t buf[FRAME_MAX];
  for (uint8_t i = 0; i < count; i++) {
    size_t off = (size_t)i * CHUNK, n = min(CHUNK, len - off);
    buf[0] = MAGIC;
    buf[1] = type;
    buf[2] = id & 0xFF;
    buf[3] = id >> 8;
    buf[4] = i;
    buf[5] = count;
    memcpy(buf + HDR, data + off, n);
    if (esp_now_send(mac, buf, HDR + n) != ESP_OK) return false;
    if (count > 1) vTaskDelay(pdMS_TO_TICKS(4));  // let the radio keep up
  }
  return true;
}

// Reassemble; returns the slot when the message is complete.
static Slot *assemble(const RxFrame &f) {
  uint8_t type = f.data[1], idx = f.data[4], count = f.data[5];
  uint16_t id = f.data[2] | (f.data[3] << 8);
  size_t n = f.len - HDR;
  if (count == 0 || idx >= count || count > 32 || (size_t)count * CHUNK > MSG_MAX + CHUNK) return nullptr;
  Slot *s = nullptr, *free_ = nullptr, *oldest = &slots[0];
  for (auto &x : slots) {
    if (x.used && x.id == id && !memcmp(x.mac, f.mac, 6)) s = &x;
    if (x.used && millis() - x.startMs > 3000) x.used = false;  // stale
    if (!x.used && !free_) free_ = &x;
    if (x.startMs < oldest->startMs) oldest = &x;
  }
  if (!s) {
    s = free_ ? free_ : oldest;
    memset(s, 0, offsetof(Slot, buf));
    s->used = true;
    memcpy(s->mac, f.mac, 6);
    s->id = id;
    s->type = type;
    s->count = count;
    s->startMs = millis();
  }
  size_t off = (size_t)idx * CHUNK;
  if (off + n > MSG_MAX) {
    s->used = false;
    return nullptr;
  }
  memcpy(s->buf + off, f.data + HDR, n);
  if (idx == count - 1) s->len = off + n;
  s->got |= (1UL << idx);
  if (s->got == ((count == 32) ? 0xFFFFFFFFUL : ((1UL << count) - 1))) {
    s->buf[s->len] = 0;
    s->used = false;
    return s;
  }
  return nullptr;
}

static Leaf *findLeaf(const char *id) {
  for (auto &l : leaves)
    if (l.id[0] && !strcmp(l.id, id)) return &l;
  return nullptr;
}

static void rememberLeaf(const char *id, const uint8_t *mac) {
  if (!id || !*id || strlen(id) > 16) return;
  Leaf *l = findLeaf(id);
  if (!l) {  // new: free slot, or replace the longest silent one
    l = &leaves[0];
    for (auto &x : leaves) {
      if (!x.id[0]) { l = &x; break; }
      if (x.lastMs < l->lastMs) l = &x;
    }
    if (l->id[0]) esp_now_del_peer(l->mac);
    strncpy(l->id, id, 16);
    l->id[16] = 0;
  }
  memcpy(l->mac, mac, 6);
  l->lastMs = millis();
  addPeer(mac);
}

// ------------------------------------------------------------------ message handling

static void handleGateway(const Slot &s) {
  if (s.type == F_UP) {
    JsonDocument filter, doc;
    filter["d"] = true;
    if (deserializeJson(doc, s.buf, s.len, DeserializationOption::Filter(filter))) return;
    rememberLeaf(doc["d"] | "", s.mac);
    net::meshToServer(s.buf, s.len);  // already {"t":"via",...}; the server checks the signature
  } else if (s.type == F_PAIR_REQ) {
    JsonDocument filter, doc;
    filter["device_id"] = true;
    if (deserializeJson(doc, s.buf, s.len, DeserializationOption::Filter(filter))) return;
    rememberLeaf(doc["device_id"] | "", s.mac);
    String msg = "{\"t\":\"via_pair\",\"body\":";
    msg += s.buf;
    msg += "}";
    net::meshToServer(msg.c_str(), msg.length());
  }
}

static void handleLeaf(const Slot &s) {
  if (!linked || memcmp(s.mac, gwMac, 6)) return;  // only from our gateway
  JsonDocument doc;
  if (deserializeJson(doc, s.buf, s.len)) return;
  if (s.type == F_PAIR_RES) {
    net::meshPairResult(doc["code"] | 0, doc["resp"].as<JsonObject>());
    return;
  }
  if (s.type != F_DOWN) return;
  String me = store::deviceId();
  const char *d = doc["d"] | "";
  if (me != d) return;
  uint64_t n = doc["n"] | (uint64_t)0;
  String m = doc["m"] | "";
  const char *h = doc["h"] | "";
  String key = store::linkKey();
  if (!key.length()) return;
  char want[33];
  hmac(key, d, n, m, want);
  static uint64_t lastN = 0;
  static uint32_t lastSaveMs = 0;
  if (!lastN) lastN = store::linkDownLast();
  if (strcmp(want, h) != 0 || n <= lastN) {
    Serial.println("[mesh] dropped a downlink message (bad signature or replay)");
    return;
  }
  lastN = n;
  // Keep the replay counter across reboots: always for commands/config, else at most once a minute.
  bool important = m.indexOf("\"t\":\"cmd\"") >= 0 || m.indexOf("\"t\":\"cfg\"") >= 0 || m.indexOf("\"t\":\"bye\"") >= 0;
  if (important || millis() - lastSaveMs > 60000) {
    store::setLinkDownLast(n);
    lastSaveMs = millis();
  }
  net::meshDeliver(m.c_str(), m.length());
}

static void handleBeacon(const RxFrame &f) {
  // payload: "PMB1|<gateway id>|<leaf count>"
  char p[64];
  size_t n = min((size_t)f.len - HDR, sizeof(p) - 1);
  memcpy(p, f.data + HDR, n);
  p[n] = 0;
  if (strncmp(p, "PMB1|", 5)) return;
  char *id = p + 5, *bar = strchr(id, '|');
  if (!bar) return;
  *bar = 0;
  int load = atoi(bar + 1);
  if (linked && !memcmp(f.mac, gwMac, 6)) gwSeenMs = millis();
  if (scanning && load < MESH_MAX_LEAVES) {
    int8_t r = f.rssi ? f.rssi : -60;  // core 2.0.x reports no RSSI: take the first one heard
    if (!candId.length() || r > candRssi) {
      memcpy(candMac, f.mac, 6);
      candId = id;
      candRssi = r;
      uint8_t primary;
      wifi_second_chan_t second;
      esp_wifi_get_channel(&primary, &second);
      candChannel = primary;
    }
  }
}

static void processQueue() {
  RxFrame f;
  while (rxq && xQueueReceive(rxq, &f, 0) == pdTRUE) {
    if (f.data[1] == F_BEACON) {
      if (leaf) handleBeacon(f);
      continue;
    }
    Slot *s = assemble(f);
    if (!s) continue;
    if (gateway && (s->type == F_UP || s->type == F_PAIR_REQ)) handleGateway(*s);
    else if (leaf && (s->type == F_DOWN || s->type == F_PAIR_RES)) handleLeaf(*s);
  }
}

// ------------------------------------------------------------------ leaf: find a gateway

// The IDF refuses a channel change while a phone is connected to our hotspot; then the hotspot
// itself is moved (the phone drops and rejoins the same Power_... network).
static void setChannel(uint8_t ch) {
  if (WiFi.softAPgetStationNum() > 0 || esp_wifi_set_channel(ch, WIFI_SECOND_CHAN_NONE) != ESP_OK)
    net::meshChannel(ch);
}

static void listenOn(uint8_t ch) {
  setChannel(ch);
  uint32_t t0 = millis();
  while (millis() - t0 < MESH_SCAN_DWELL_MS) {
    processQueue();
    vTaskDelay(pdMS_TO_TICKS(10));
  }
}

static void scanForGateway() {
  scanning = true;
  candId = "";
  candRssi = -127;
  if (lastGoodChannel) listenOn(lastGoodChannel);  // fast path: same gateway channel as before
  if (!candId.length()) {
    for (uint8_t ch = 1; ch <= 13 && !candId.length(); ch++) {
      listenOn(ch);
      if (candId.length()) listenOn(ch);  // stay a little longer: maybe a stronger gateway
    }
  }
  scanning = false;
  lastScanMs = millis();
  if (!candId.length()) return;
  memcpy(gwMac, candMac, 6);
  gwId = candId;
  gwChannel = lastGoodChannel = candChannel;
  setChannel(gwChannel);
  addPeer(gwMac);
  linked = true;
  gwSeenMs = millis();
  sendFailsInRow = 0;
  net::meshChannel(gwChannel);
  Serial.printf("[mesh] leaf: using gateway %s on channel %u (rssi %d)\n", gwId.c_str(), gwChannel, candRssi);
}

// ------------------------------------------------------------------ public

void begin() {
#if !MESH_ENABLE
  Serial.println("[mesh] disabled in config.h (MESH_ENABLE 0)");
  return;
#endif
  if (ready) return;
  rxq = xQueueCreate(32, sizeof(RxFrame));
  if (esp_now_init() != ESP_OK) {
    Serial.println("[mesh] ESP-NOW init failed, mesh disabled");
    return;
  }
  esp_now_register_recv_cb(recvCb);
  esp_now_register_send_cb(sendCb);
  addPeer(BCAST);
  ready = true;
}

void setGateway(bool on) {
  if (on != gateway) Serial.printf("[mesh] gateway role %s\n", on ? "on" : "off");
  gateway = on && ready;
}

uint8_t leafCount() {
  uint8_t n = 0;
  for (auto &l : leaves)
    if (l.id[0] && millis() - l.lastMs < 120000) n++;
  return n;
}

void toLeaf(const char *json, size_t len, const char *leafId, bool pairResult) {
  Leaf *l = findLeaf(leafId);
  if (!gateway || !l) return;
  sendFrames(l->mac, pairResult ? F_PAIR_RES : F_DOWN, json, len);
}

void startLeaf() {
  leaf = ready;
  linked = false;
  lastScanMs = 0;
  if (leaf) Serial.println("[mesh] leaf mode: looking for a gateway");
}

void stopLeaf() {
  leaf = linked = false;
}

bool leafLinked() { return leaf && linked; }
void leafReset() { linked = false; }
String gatewayId() { return linked ? gwId : String(""); }
uint8_t channel() { return linked ? gwChannel : 0; }

bool leafSend(const String &inner) {
  if (!leafLinked()) return false;
  String key = store::linkKey();
  if (!key.length()) {
    Serial.println("[mesh] no link key yet: connect this meter over Wi-Fi once, or pair it again");
    return false;
  }
  String id = store::deviceId();
  uint32_t n = store::nextLinkNonce();
  char h[33];
  hmac(key, id.c_str(), n, inner, h);
  JsonDocument doc;
  doc["t"] = "via";
  doc["d"] = id;
  doc["n"] = n;
  doc["h"] = h;
  doc["m"] = inner;
  String out;
  serializeJson(doc, out);
  return sendFrames(gwMac, F_UP, out.c_str(), out.length());
}

bool leafSendPair(const String &body) {
  if (!leafLinked()) return false;
  return sendFrames(gwMac, F_PAIR_REQ, body.c_str(), body.length());
}

void loop() {
  if (!ready) return;
  processQueue();
  uint32_t now = millis();
  if (gateway && now - lastBeaconMs > MESH_BEACON_MS) {
    lastBeaconMs = now;
    char b[48];
    int n = snprintf(b, sizeof(b), "PMB1|%s|%u", store::deviceId().c_str(), leafCount());
    uint8_t buf[HDR + 48];
    buf[0] = MAGIC;
    buf[1] = F_BEACON;
    buf[2] = buf[3] = buf[4] = 0;
    buf[5] = 1;
    memcpy(buf + HDR, b, n);
    esp_now_send(BCAST, buf, HDR + n);
  }
  if (leaf) {
    if (linked && (now - gwSeenMs > MESH_GATEWAY_LOST_MS || sendFailsInRow > 8)) {
      Serial.printf("[mesh] leaf: lost gateway %s\n", gwId.c_str());
      linked = false;
    }
    if (!linked && (!lastScanMs || now - lastScanMs > 5000)) scanForGateway();
  }
}

}  // namespace mesh
