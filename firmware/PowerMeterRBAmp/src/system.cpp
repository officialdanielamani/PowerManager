#include "system.h"

#include "auxrelay.h"

#include <esp_attr.h>
#include <esp_idf_version.h>
#include <esp_system.h>

#include "app_state.h"
#include "outbox.h"
#include "storage.h"

namespace sys {

static volatile uint32_t beats[HB_COUNT];
static const uint32_t HB_LIMIT_MS[HB_COUNT] = {180000, 60000};  // net may block on TLS/HTTP

void factoryReset(const char *why) {
  Serial.printf("[system] FACTORY RESET (%s)\n", why);
  {
    StateLock l;
    g.factoryResetPending = true;
  }
  outbox::clear();
  store::factoryReset();
  auxrelay::wipe();
  delay(1500);  // let the LED show white flashing
  ESP.restart();
}

void reboot(uint32_t delayMs) {
  delay(delayMs);
  ESP.restart();
}

void heartbeat(uint8_t task) { beats[task] = millis(); }

TaskHandle_t taskHandles[4] = {nullptr, nullptr, nullptr, nullptr};
static const char *TASK_NAMES[4] = {"control", "led", "net", "web"};

String stackReport() {
  String s = "Stack free:";
  for (int i = 0; i < 4; i++) {
    if (!taskHandles[i]) continue;
    // ESP-IDF reports the high-water mark in bytes
    s += String(" ") + TASK_NAMES[i] + " " + String((unsigned)uxTaskGetStackHighWaterMark(taskHandles[i])) + " B";
  }
  return s;
}

void checkStacks() {
  static uint32_t lastMs = 0;
  if (millis() - lastMs < 60000) return;
  lastMs = millis();
  for (int i = 0; i < 4; i++) {
    if (taskHandles[i] && uxTaskGetStackHighWaterMark(taskHandles[i]) < 1024)
      Serial.printf("[system] WARNING: task %s has only %u B of stack left\n", TASK_NAMES[i],
                    (unsigned)uxTaskGetStackHighWaterMark(taskHandles[i]));
  }
}

void checkHeartbeats() {
  uint32_t now = millis();
  for (int i = 0; i < HB_COUNT; i++) {
    if (beats[i] && now - beats[i] > HB_LIMIT_MS[i]) {
      Serial.printf("[system] task %d stuck for %lu ms, restarting\n", i, (unsigned long)(now - beats[i]));
      delay(100);
      ESP.restart();
    }
  }
}

// ------------------------------------------------------------------ crash breadcrumbs

static const uint32_t CRASH_MAGIC = 0x504D4352;  // "PMCR"
struct CrashInfo {
  uint32_t magic;
  uint32_t upS;      // uptime of the run, updated every second
  uint32_t minHeap;  // lowest free heap of the run
  uint8_t streak;    // crashes in a row soon after boot
  char where[CR_COUNT][28];
};
RTC_NOINIT_ATTR static CrashInfo crash;
bool safeMode = false;
static esp_reset_reason_t reason = ESP_RST_UNKNOWN;
static const char *SLOT_NAMES[CR_COUNT] = {"boot step", "loop", "net", "web", "control", "led"};

void crumb(uint8_t slot, const char *where) {
  if (slot >= CR_COUNT) return;
  strncpy(crash.where[slot], where, sizeof(crash.where[slot]) - 1);
  crash.where[slot][sizeof(crash.where[slot]) - 1] = 0;
}

const char *resetReason() {
  switch (reason) {
    case ESP_RST_POWERON: return "power on";
    case ESP_RST_EXT: return "reset pin";
    case ESP_RST_SW: return "restart";
    case ESP_RST_PANIC: return "CRASH (panic)";
    case ESP_RST_INT_WDT: return "CRASH (interrupt watchdog)";
    case ESP_RST_TASK_WDT: return "CRASH (task watchdog)";
    case ESP_RST_WDT: return "CRASH (watchdog)";
    case ESP_RST_BROWNOUT: return "brownout (supply voltage dropped)";
    case ESP_RST_DEEPSLEEP: return "deep sleep";
#if ESP_IDF_VERSION_MAJOR >= 5
    case ESP_RST_USB: return "USB reset";
#endif
    default: return "unknown";
  }
}

void bootCheck() {
  reason = esp_reset_reason();
  bool crashed = reason == ESP_RST_PANIC || reason == ESP_RST_INT_WDT || reason == ESP_RST_TASK_WDT ||
                 reason == ESP_RST_WDT || reason == ESP_RST_BROWNOUT;
  bool valid = crash.magic == CRASH_MAGIC && reason != ESP_RST_POWERON;
  Serial.printf("[boot] reset reason: %s\n", resetReason());
  if (valid && crashed) {
    Serial.println("[crash] ===== the previous run crashed =====");
    Serial.printf("[crash] after %lu s of uptime, lowest free heap %lu B\n", (unsigned long)crash.upS,
                  (unsigned long)crash.minHeap);
    for (int i = 0; i < CR_COUNT; i++) {
      crash.where[i][sizeof(crash.where[i]) - 1] = 0;
      Serial.printf("[crash]   %-9s : %s\n", SLOT_NAMES[i], crash.where[i][0] ? crash.where[i] : "-");
    }
    Serial.println("[crash] (send these lines with the crash log)");
    crash.streak = crash.upS < 120 ? crash.streak + 1 : 1;
  } else {
    crash.streak = valid ? crash.streak : 0;
    if (reason == ESP_RST_POWERON || reason == ESP_RST_EXT) crash.streak = 0;
  }
  safeMode = crash.streak >= 3;
  if (safeMode)
    Serial.printf("[boot] SAFE MODE: %u crashes in a row, starting without mesh and DS3231\n",
                  (unsigned)crash.streak);
  crash.magic = CRASH_MAGIC;
  crash.upS = 0;
  crash.minHeap = ESP.getFreeHeap();
  memset(crash.where, 0, sizeof(crash.where));
}

void bootDone() { crumb(CR_BOOT, "done"); }

void tick() {
  static uint32_t last = 0;
  uint32_t now = millis();
  if (now - last < 1000) return;
  last = now;
  crash.upS = now / 1000;
  crash.minHeap = ESP.getMinFreeHeap();
  if (crash.upS >= 120 && crash.streak) crash.streak = 0;  // stable run: next boot is normal
}

}  // namespace sys
