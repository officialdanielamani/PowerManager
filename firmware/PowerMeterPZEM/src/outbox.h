// Offline buffer: an ordered queue of telemetry items and events on LittleFS.
// Everything sent to the server goes through here (docs/protocol.md §2.4 ordering rule),
// and items are removed only after the server acknowledges them.
//
// Storage: append-only segment files (/ob/<n>.log, OUTBOX_SEGMENT_ITEMS lines each) plus the
// highest acknowledged seq in NVS. Appends are cheap on LittleFS; fully acknowledged segments
// are deleted; when the buffer is full the oldest segment is dropped.
#pragma once
#include <Arduino.h>
#include <vector>

namespace outbox {

enum Kind : uint8_t { TEL = 'T', EVT = 'E' };

struct Item {
  uint32_t seq;
  Kind kind;
  String json;
};

bool begin();
void push(Kind kind, uint32_t seq, const String &json);
// Oldest unacknowledged items, in order: either one event, or up to `max` telemetry items.
void nextBatch(std::vector<Item> &out, uint16_t max);
uint16_t count();
void ackUpTo(uint32_t seq);
void clear();

}  // namespace outbox
