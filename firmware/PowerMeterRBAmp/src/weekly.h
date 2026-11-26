#pragma once
#include <stdint.h>

// Session reset on chosen days of the week, each with its own time (docs/protocol.md §3, sess.mode "weekly").
// wk[] = up to 7 times as UTC minutes of the week, Monday 00:00 UTC = 0 (the server converts from its time zone).
// Returns the first reset time (epoch) after `after`, 0 = none.
inline uint32_t weeklyNext(uint32_t after, const uint16_t *wk, uint8_t n) {
  if (!n) return 0;
  uint32_t days = after / 86400UL;
  uint32_t weekStart = (days - (days + 3) % 7) * 86400UL;  // Monday 00:00 UTC (1 Jan 1970 was a Thursday)
  uint32_t best = 0;
  for (uint8_t i = 0; i < n; i++) {
    uint32_t t = weekStart + (uint32_t)wk[i] * 60UL;
    if (t <= after) t += 7UL * 86400UL;
    if (!best || t < best) best = t;
  }
  return best;
}
