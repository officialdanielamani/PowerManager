// Sensor check (docs/protocol.md §13.1): collects readings of one channel for the Zero & Tare wizard.
// Pure logic (no Arduino / FreeRTOS), shared by both sketches and run by the PC test firmware/tools/sensorcheck_test.
//   - at most ONE reading is counted per second; a check ends after at least n readings and at least n seconds (10 s)
//   - the statistics are of the RAW sensor values (before zero and tare), so repeating a check never compounds
//   - a check that cannot collect n readings within CHECK_TIMEOUT_MS ends as failed ("sensor")
#pragma once
#include <stdint.h>

namespace sc {

static const uint8_t MIN_READINGS = 10;
static const uint8_t MAX_READINGS = 60;
static const uint32_t READING_GAP_MS = 1000;      // one counted reading per second
static const uint32_t CHECK_TIMEOUT_MS = 60000;   // enough valid readings must arrive within this

struct Stat {
  float mean = 0, mn = 0, mx = 0;
};

struct Result {
  bool ok = false;
  const char *why = nullptr;  // "sensor" when it timed out
  uint8_t ch = 0;
  uint8_t n = 0;              // readings counted
  uint32_t secs = 0;          // seconds from the start to the last counted reading
  Stat a, w, v;
};

class Check {
 public:
  bool active() const { return active_; }
  uint8_t channel() const { return ch_; }

  // Starts a check of channel ch for `readings` readings (clamped to 10..60).
  void start(uint8_t ch, uint8_t readings, uint32_t nowMs) {
    active_ = true;
    ch_ = ch;
    want_ = readings < MIN_READINGS ? MIN_READINGS : readings > MAX_READINGS ? MAX_READINGS : readings;
    n_ = 0;
    startMs_ = nowMs;
    lastCountMs_ = 0;
    a_ = Acc();
    w_ = Acc();
    v_ = Acc();
  }

  void cancel() { active_ = false; }

  // One valid reading of the channel (raw values). Returns true when the check is complete; the result is then in
  // `out` and the check is no longer active.
  bool feed(float a, float w, float v, uint32_t nowMs, Result &out) {
    if (!active_) return false;
    if (n_ > 0 && (uint32_t)(nowMs - lastCountMs_) < READING_GAP_MS) return false;  // at most one per second
    lastCountMs_ = nowMs;
    add(n_ == 0, a, w, v);
    n_++;
    // At least n readings AND at least n seconds since the start (10 readings = 10 s at least): the first reading counts at
    // the start, so the check completes with the reading counted n seconds later (n + 1 readings).
    if (n_ >= want_ && (uint32_t)(nowMs - startMs_) >= (uint32_t)want_ * 1000UL) {
      finish(true, nullptr, nowMs, out);
      return true;
    }
    return false;
  }

  // Call regularly: a check without enough valid readings ends as failed after CHECK_TIMEOUT_MS.
  bool tick(uint32_t nowMs, Result &out) {
    if (!active_ || (uint32_t)(nowMs - startMs_) < CHECK_TIMEOUT_MS) return false;
    finish(false, "sensor", nowMs, out);
    return true;
  }

 private:
  struct Acc {
    double sum = 0;
    float mn = 0, mx = 0;
  };
  Acc a_, w_, v_;
  bool active_ = false;
  uint8_t ch_ = 0, want_ = MIN_READINGS, n_ = 0;
  uint32_t startMs_ = 0, lastCountMs_ = 0;

  static void upd(Acc &x, bool first, float val) {
    x.sum += val;
    if (first || val < x.mn) x.mn = val;
    if (first || val > x.mx) x.mx = val;
  }
  void add(bool first, float a, float w, float v) {
    upd(a_, first, a);
    upd(w_, first, w);
    upd(v_, first, v);
  }
  static Stat stat(const Acc &x, uint8_t n) {
    Stat s;
    s.mean = n ? (float)(x.sum / n) : 0;
    s.mn = x.mn;
    s.mx = x.mx;
    return s;
  }
  void finish(bool ok, const char *why, uint32_t nowMs, Result &out) {
    out.ok = ok;
    out.why = why;
    out.ch = ch_;
    out.n = n_;
    out.secs = (nowMs - startMs_ + 500) / 1000;
    out.a = stat(a_, n_);
    out.w = stat(w_, n_);
    out.v = stat(v_, n_);
    active_ = false;
  }
};

// Zero and tare of one channel (docs/protocol.md §13.4 / 13.5). All values are >= 0.
struct Offsets {
  float zeroA = 0, zeroW = 0, tareA = 0, tareW = 0;
};

// The zero is subtracted right after every sensor read; nothing below 0; without current there is no power.
inline void applyZero(float &a, float &w, const Offsets &o) {
  a = a > o.zeroA ? a - o.zeroA : 0;
  w = w > o.zeroW ? w - o.zeroW : 0;
  if (a <= 0) w = 0;
}
// The tared current (limits): the zero-corrected current minus the tare, never below 0.
inline float taredCurrent(float a, const Offsets &o) { return a > o.tareA ? a - o.tareA : 0; }
// Energy of the tare during one reading period: the tare can never take away more than was measured.
inline double tareEnergyKwh(float w, const Offsets &o, uint32_t ms) {
  float t = w < o.tareW ? w : o.tareW;
  return t > 0 ? (double)t * ms / 3.6e9 : 0.0;
}

}  // namespace sc
