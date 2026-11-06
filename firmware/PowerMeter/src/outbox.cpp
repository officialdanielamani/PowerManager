#include "outbox.h"

#include <LittleFS.h>
#include <Preferences.h>
#include <deque>
#include "../config.h"

// RAM first, flash only when needed: readings wait in RAM until the server acknowledges them, so a
// meter that is online (fast read intervals included) does not wear out its flash. They go to the
// LittleFS buffer only when RAM is full, when they are still unsent after OUTBOX_RAM_MAX_AGE_MS, or
// before an event (events always go to flash, behind everything older, so the order is kept).
#ifndef OUTBOX_RAM_ITEMS
#define OUTBOX_RAM_ITEMS 120
#endif
#ifndef OUTBOX_RAM_MAX_AGE_MS
#define OUTBOX_RAM_MAX_AGE_MS 30000
#endif

namespace outbox {

struct RamItem {
  Item it;
  uint32_t atMs;
};
static std::deque<RamItem> ram;  // newer than everything on flash, oldest first

static const char *DIR = "/ob";
static SemaphoreHandle_t lock;
static Preferences prefs;
static bool ok = false;
static uint32_t acked = 0;       // highest acknowledged seq
static uint32_t firstSeg = 1;    // oldest segment number present
static uint32_t lastSeg = 0;     // segment currently appended to (0 = none)
static uint16_t lastSegItems = 0;
static uint16_t pending = 0;     // unacknowledged items

static String segPath(uint32_t n) { return String(DIR) + "/" + String(n) + ".log"; }

// Each line: <seq> <kind> <json>\n
static bool parseLine(const String &line, Item &it) {
  int a = line.indexOf(' ');
  if (a < 1 || (int)line.length() < a + 3) return false;
  it.seq = strtoul(line.substring(0, a).c_str(), nullptr, 10);
  it.kind = (Kind)line[a + 1];
  it.json = line.substring(a + 3);
  return it.seq > 0;
}

static uint32_t segLastSeq(uint32_t n, uint16_t *items, uint16_t *unacked) {
  File f = LittleFS.open(segPath(n), "r");
  uint32_t last = 0;
  uint16_t cnt = 0, un = 0;
  while (f && f.available()) {
    String line = f.readStringUntil('\n');
    Item it;
    if (!parseLine(line, it)) continue;
    last = it.seq;
    cnt++;
    if (it.seq > acked) un++;
  }
  if (f) f.close();
  if (items) *items = cnt;
  if (unacked) *unacked = un;
  return last;
}

static void rescan() {
  firstSeg = 0;
  lastSeg = 0;
  pending = 0;
  File dir = LittleFS.open(DIR);
  while (dir) {
    File e = dir.openNextFile();
    if (!e) break;
    uint32_t n = strtoul(e.name(), nullptr, 10);
    e.close();
    if (!n) continue;
    if (!firstSeg || n < firstSeg) firstSeg = n;
    if (n > lastSeg) lastSeg = n;
  }
  if (!firstSeg) firstSeg = lastSeg = 0;
  for (uint32_t n = firstSeg; n && n <= lastSeg; n++) {
    if (!LittleFS.exists(segPath(n))) continue;
    uint16_t items = 0, un = 0;
    segLastSeq(n, &items, &un);
    pending += un;
    if (n == lastSeg) lastSegItems = items;
  }
}

bool begin() {
  lock = xSemaphoreCreateMutex();
  prefs.begin("ob", false);
  acked = prefs.getUInt("ack", 0);
  if (!LittleFS.begin(true)) {  // formats on first use
    Serial.println("[outbox] LittleFS mount failed, offline buffer disabled");
    return false;
  }
  if (!LittleFS.exists(DIR)) LittleFS.mkdir(DIR);
  rescan();
  ok = true;
  Serial.printf("[outbox] %u item(s) waiting from before reboot\n", pending);
  return true;
}

static void dropOldestSegment() {
  for (; firstSeg && firstSeg <= lastSeg; firstSeg++) {
    if (LittleFS.exists(segPath(firstSeg))) {
      uint16_t un = 0;
      segLastSeq(firstSeg, nullptr, &un);
      LittleFS.remove(segPath(firstSeg));
      pending -= min(pending, un);
      Serial.printf("[outbox] full: dropped %u oldest item(s)\n", un);
      firstSeg++;
      return;
    }
  }
}

// Appends one item to the flash segments. Caller holds the lock.
static void flashAppend(Kind kind, uint32_t seq, const String &json) {
  if (!lastSeg || lastSegItems >= OUTBOX_SEGMENT_ITEMS) {
    lastSeg++;
    if (!firstSeg) firstSeg = lastSeg;
    lastSegItems = 0;
    while (lastSeg - firstSeg >= OUTBOX_SEGMENTS) dropOldestSegment();
  }
  File f = LittleFS.open(segPath(lastSeg), "a");
  if (f) {
    f.printf("%lu %c ", (unsigned long)seq, (char)kind);
    f.print(json);
    f.print('\n');
    f.close();
    lastSegItems++;
    pending++;
  }
}

// Moves the oldest RAM items to flash (keeps the order). Caller holds the lock.
static void spill(size_t n) {
  while (n-- && !ram.empty()) {
    RamItem &r = ram.front();
    if (r.it.seq > acked) flashAppend(r.it.kind, r.it.seq, r.it.json);
    ram.pop_front();
  }
}

void push(Kind kind, uint32_t seq, const String &json) {
  if (!ok) return;
  xSemaphoreTake(lock, portMAX_DELAY);
  uint32_t now = millis();
  if (kind == EVT) {  // events are kept on flash right away, behind all older items
    spill(ram.size());
    flashAppend(kind, seq, json);
  } else {
    ram.push_back({{seq, kind, json}, now});
    if (ram.size() > OUTBOX_RAM_ITEMS) spill(OUTBOX_SEGMENT_ITEMS);  // offline for a while: to flash
    size_t old = 0;
    for (auto &r : ram) {
      if (now - r.atMs < OUTBOX_RAM_MAX_AGE_MS) break;
      old++;
    }
    spill(old);  // not acknowledged in time: keep them safe on flash
  }
  xSemaphoreGive(lock);
}

void nextBatch(std::vector<Item> &out, uint16_t max) {
  out.clear();
  if (!ok) return;
  xSemaphoreTake(lock, portMAX_DELAY);
  for (uint32_t n = firstSeg; n && n <= lastSeg && out.size() < max; n++) {
    File f = LittleFS.open(segPath(n), "r");
    while (f && f.available() && out.size() < max) {
      Item it;
      if (!parseLine(f.readStringUntil('\n'), it) || it.seq <= acked) continue;
      if (it.kind == EVT) {  // events are sent on their own
        if (out.empty()) out.push_back(it);
        f.close();
        xSemaphoreGive(lock);
        return;
      }
      out.push_back(it);
    }
    if (f) f.close();
  }
  for (auto &r : ram) {  // then the newer items still in RAM
    if (out.size() >= max) break;
    if (r.it.seq <= acked) continue;
    out.push_back(r.it);
  }
  xSemaphoreGive(lock);
}

uint16_t count() { return pending + ram.size(); }

void ackUpTo(uint32_t seq) {
  if (!ok || seq <= acked) return;
  xSemaphoreTake(lock, portMAX_DELAY);
  acked = seq;
  prefs.putUInt("ack", acked);
  while (!ram.empty() && ram.front().it.seq <= acked) ram.pop_front();
  // delete segments that are fully acknowledged (never the one being appended to)
  while (firstSeg && firstSeg < lastSeg) {
    if (LittleFS.exists(segPath(firstSeg)) && segLastSeq(firstSeg, nullptr, nullptr) > acked) break;
    LittleFS.remove(segPath(firstSeg));
    firstSeg++;
  }
  uint16_t un = 0;
  pending = 0;
  for (uint32_t n = firstSeg; n && n <= lastSeg; n++) {
    if (!LittleFS.exists(segPath(n))) continue;
    segLastSeq(n, nullptr, &un);
    pending += un;
  }
  xSemaphoreGive(lock);
}

void clear() {
  if (!ok) return;
  xSemaphoreTake(lock, portMAX_DELAY);
  for (uint32_t n = firstSeg; n && n <= lastSeg; n++) LittleFS.remove(segPath(n));
  ram.clear();
  firstSeg = lastSeg = 0;
  lastSegItems = pending = 0;
  acked = 0;
  prefs.putUInt("ack", 0);
  xSemaphoreGive(lock);
}

}  // namespace outbox
