#include "ota.h"

#include <HTTPClient.h>
#include <Preferences.h>
#include <Update.h>
#include <WiFi.h>
#include <WiFiClient.h>
#include <WiFiClientSecure.h>
#include <esp_ota_ops.h>
#include <esp_partition.h>
#include <mbedtls/sha256.h>

#include "../config.h"
#include "certs.h"
#include "otatag.h"

#ifndef OTA_CONFIRM_MS
#define OTA_CONFIRM_MS 120000UL  // the new firmware must reach the server within this time, else rollback
#endif
#define OTA_MAX_BOOTS 3          // unconfirmed boots of a new firmware (crash loops count) before the rollback

namespace ota {

// The tag is a constant of the image the server and the meter can read (otatag.h). tag() is used by the hello, so the
// linker keeps it.
static const char TAG[] = "PMFW" "TAG|" FW_TYPE "|" FW_VERSION "|" HW_NAME "|";
const char *tag() { return TAG; }

static Hooks H = {nullptr, nullptr, nullptr, nullptr, nullptr};
enum Phase : uint8_t { IDLE = 0, DOWNLOADING, VERIFYING, REBOOTING, FAILED };
static volatile Phase phase_ = IDLE;
static volatile uint8_t pct_ = 0;
static char why_[24] = "";
static char ver_[24] = "";          // version being installed
static volatile bool newState_ = false;
static uint8_t lastPct_ = 255;
static Phase lastPhase_ = IDLE;

static bool trial_ = false;         // running a freshly installed firmware that the server has not confirmed yet
static uint32_t trialSince_ = 0;
static char tried_[24] = "";        // after a rollback: the version that did not work
static char rbWhy_[24] = "";

// ---- request of the current update (copied from the command)
static String path_;
static char sha_[65];
static uint32_t size_ = 0;

static const esp_partition_t *nextSlot() { return esp_ota_get_next_update_partition(nullptr); }

static void setFailed(const char *why) {
  strlcpy(why_, why, sizeof(why_));
  phase_ = FAILED;
  newState_ = true;
}

static void rollback(const char *why) {
  Preferences p;
  p.begin("ota", false);
  String prev = p.getString("prev", "");
  String tv = p.getString("try", "");
  p.remove("try");
  p.remove("boots");
  p.putString("rb", why);
  p.putString("rbver", tv);
  p.end();
  const esp_partition_t *part = prev.length() ? esp_partition_find_first(ESP_PARTITION_TYPE_APP, ESP_PARTITION_SUBTYPE_ANY, prev.c_str()) : nullptr;
  Serial.printf("[ota] ROLLBACK (%s) to %s\n", why, part ? part->label : "?");
  if (part) esp_ota_set_boot_partition(part);
  delay(300);
  ESP.restart();
}

void begin(const Hooks &hooks) {
  H = hooks;
  Preferences p;
  p.begin("ota", false);
  String tv = p.getString("try", "");
  if (tv.length()) {
    uint8_t boots = p.getUChar("boots", 0) + 1;
    p.putUChar("boots", boots);
    p.end();
    if (boots > OTA_MAX_BOOTS) rollback("boots");  // does not return
    trial_ = true;
    trialSince_ = millis();
    strlcpy(ver_, tv.c_str(), sizeof(ver_));
    Serial.printf("[ota] new firmware %s on trial (boot %u)\n", ver_, boots);
    return;
  }
  String rb = p.getString("rb", "");
  if (rb.length()) {
    strlcpy(rbWhy_, rb.c_str(), sizeof(rbWhy_));
    strlcpy(tried_, p.getString("rbver", "").c_str(), sizeof(tried_));
  }
  p.end();
}

void capsJson(JsonObject o) {
  const esp_partition_t *s = nextSlot();
  o["slot"] = s ? s->size : 0;
  otatag::Layout l;
  for (esp_partition_iterator_t it = esp_partition_find(ESP_PARTITION_TYPE_ANY, ESP_PARTITION_SUBTYPE_ANY, nullptr); it; it = esp_partition_next(it)) {
    const esp_partition_t *x = esp_partition_get(it);
    l.add((uint8_t)x->type, (uint8_t)x->subtype, x->address, x->size);
  }
  char b[9];
  snprintf(b, sizeof(b), "%08x", (unsigned)l.crc);
  o["layout"] = b;
  o["tag"] = tag();
}

void stateJson(JsonObject o) {
  o["state"] = trial_ ? "trial" : rbWhy_[0] ? "rolled_back" : "ok";
  o["ver"] = FW_VERSION;
  if (rbWhy_[0]) {
    o["why"] = rbWhy_;
    o["tried"] = tried_;
  }
}

bool busy() { return phase_ == DOWNLOADING || phase_ == VERIFYING || phase_ == REBOOTING; }

static bool hexEq(const uint8_t *d, const char *hex) {
  for (int i = 0; i < 32; i++) {
    char b[3];
    snprintf(b, sizeof(b), "%02x", d[i]);
    if (strncasecmp(b, hex + 2 * i, 2)) return false;
  }
  return true;
}

static void task(void *) {
  String host;
  uint16_t port = 0;
  bool tls = false, insecure = false;
  if (!H.server || !H.server(host, port, tls, insecure)) {
    setFailed("no_server");
    vTaskDelete(nullptr);
    return;
  }
  String url = String(tls ? "https://" : "http://") + host + ":" + port + path_;
  HTTPClient http;
  WiFiClientSecure secure;
  WiFiClient plain;
  http.setTimeout(20000);
  http.setConnectTimeout(10000);
  bool begun;
  if (tls) {
    if (insecure) secure.setInsecure();
    else secure.setCACert(ROOT_CA_PEM);
    begun = http.begin(secure, url);
  } else {
    begun = http.begin(plain, url);
  }
  if (!begun) {
    setFailed("bad_url");
    vTaskDelete(nullptr);
    return;
  }
  Serial.printf("[ota] downloading %s\n", path_.c_str());
  int code = http.GET();
  if (code != 200) {
    char w[24];
    snprintf(w, sizeof(w), code < 0 ? "net_%d" : "http_%d", code < 0 ? -code : code);
    http.end();
    setFailed(w);
    vTaskDelete(nullptr);
    return;
  }
  if ((uint32_t)http.getSize() != size_) {
    http.end();
    setFailed("size");
    vTaskDelete(nullptr);
    return;
  }
  if (!Update.begin(size_, U_FLASH)) {
    http.end();
    setFailed("flash_begin");
    vTaskDelete(nullptr);
    return;
  }
  mbedtls_sha256_context sha;
  mbedtls_sha256_init(&sha);
  mbedtls_sha256_starts(&sha, 0);
  otatag::Scan scan;
  WiFiClient *s = http.getStreamPtr();
  uint8_t buf[1024];
  uint32_t got = 0, lastData = millis();
  const char *fail = nullptr;
  while (got < size_) {
    size_t avail = s->available();
    if (!avail) {
      if (!http.connected() || millis() - lastData > 30000) {
        fail = "timeout";
        break;
      }
      vTaskDelay(pdMS_TO_TICKS(5));
      continue;
    }
    size_t n = s->readBytes(buf, min(avail, min(sizeof(buf), (size_t)(size_ - got))));
    if (!n) continue;
    lastData = millis();
    mbedtls_sha256_update(&sha, buf, n);
    scan.feed(buf, n);
    if (Update.write(buf, n) != n) {
      fail = "flash_write";
      break;
    }
    got += n;
    pct_ = (uint8_t)((uint64_t)got * 100 / size_);
    newState_ = true;  // tick() reports every 10 %
    vTaskDelay(1);
  }
  http.end();
  uint8_t digest[32];
  mbedtls_sha256_finish(&sha, digest);
  mbedtls_sha256_free(&sha);
  if (!fail) {
    phase_ = VERIFYING;
    newState_ = true;
    if (!hexEq(digest, sha_)) fail = "sha256";
    else if (!scan.found() || strcmp(scan.type, FW_TYPE)) fail = "wrong_type";
    else if (!Update.end(true)) fail = "flash_end";
  }
  if (fail) {
    Update.abort();
    setFailed(fail);
    Serial.printf("[ota] failed: %s\n", fail);
    vTaskDelete(nullptr);
    return;
  }
  const esp_partition_t *run = esp_ota_get_running_partition();
  Preferences p;
  p.begin("ota", false);
  p.putString("prev", run ? run->label : "");
  p.putString("try", ver_);
  p.putUChar("boots", 0);
  p.remove("rb");
  p.remove("rbver");
  p.end();
  Serial.printf("[ota] installed %s, restarting\n", ver_);
  phase_ = REBOOTING;
  pct_ = 100;
  newState_ = true;
  vTaskDelay(pdMS_TO_TICKS(4000));  // the event goes out first
  if (H.restart) H.restart();
  ESP.restart();
}

bool onCommand(JsonObjectConst m, const char *&err) {
  err = nullptr;
  if (busy()) {
    err = "busy";
    return false;
  }
  const char *path = m["path"] | "";
  const char *sha = m["sha256"] | "";
  const char *ver = m["ver"] | "";
  const char *type = m["type"] | "";
  uint32_t size = m["size"] | 0;
  if (path[0] != '/' || strlen(sha) != 64 || !ver[0] || !size) {
    err = "bad_request";
    return false;
  }
  if (strcmp(type, FW_TYPE)) {
    err = "wrong_type";
    return false;
  }
  const esp_partition_t *slot = nextSlot();
  if (!slot || size > slot->size) {
    err = "too_big";
    return false;
  }
  String h;
  uint16_t pt;
  bool tls, ins;
  if (!H.server || !H.server(h, pt, tls, ins)) {
    err = "no_server";
    return false;
  }
  if (H.wifiUp && !H.wifiUp()) {
    err = "no_wifi";
    return false;
  }
  path_ = path;
  strlcpy(sha_, sha, sizeof(sha_));
  strlcpy(ver_, ver, sizeof(ver_));
  size_ = size;
  pct_ = 0;
  why_[0] = 0;
  lastPct_ = 255;
  lastPhase_ = IDLE;
  if (H.prepare) H.prepare();  // sessions end, relays off: from here on the meter does no switching
  phase_ = DOWNLOADING;
  newState_ = true;
  if (xTaskCreate(task, "ota", 12288, nullptr, 1, nullptr) != pdPASS) {
    setFailed("task");
    err = "task";
    return false;
  }
  return true;
}

void webJson(JsonObject o) {
  stateJson(o);
  const esp_partition_t *s = nextSlot();
  o["slot"] = s ? s->size : 0;
  Phase ph = phase_;
  if (ph != IDLE) {
    o["busy"] = ph == DOWNLOADING ? "downloading" : ph == VERIFYING ? "verifying" : ph == REBOOTING ? "restarting" : "failed";
    o["pct"] = (uint8_t)pct_;
    if (ph == FAILED) o["why"] = why_;
  }
}

void confirm() {
  if (!trial_) {
    if (rbWhy_[0]) {  // the report of a rollback went out with the hello
      Preferences p;
      p.begin("ota", false);
      p.remove("rb");
      p.remove("rbver");
      p.end();
    }
    return;
  }
  Preferences p;
  p.begin("ota", false);
  p.remove("try");
  p.remove("boots");
  p.end();
  trial_ = false;
  Serial.printf("[ota] %s confirmed by the server\n", ver_);
  if (H.event) H.event("ota_ok", "ok", 100, nullptr, ver_);
}

void tick() {
  if (trial_ && millis() - trialSince_ > OTA_CONFIRM_MS) rollback("no_server");  // does not return
  if (!newState_) return;
  Phase ph = phase_;
  uint8_t pc = pct_;
  if (ph == DOWNLOADING && ph == lastPhase_ && lastPct_ != 255 && pc / 10 == lastPct_ / 10) return;  // every 10 %
  newState_ = false;
  lastPhase_ = ph;
  lastPct_ = pc;
  if (!H.event) return;
  if (ph == FAILED) H.event("ota_failed", "failed", pc, why_, ver_);
  else H.event("ota_progress", ph == DOWNLOADING ? "downloading" : ph == VERIFYING ? "verifying" : "rebooting", pc, nullptr, ver_);
}

}  // namespace ota
