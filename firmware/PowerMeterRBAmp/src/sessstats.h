// Session summary: min / max / average of voltage, current and power over a session, sent with session_end
// ("sum"). With "Core data only" this is all the meter records. Shared by both firmwares.
#pragma once
#include <Arduino.h>
#include <ArduinoJson.h>

struct SessStats {
  uint32_t n = 0;
  float vmin = 0, vmax = 0, amin = 0, amax = 0, wmin = 0, wmax = 0;
  double vsum = 0, asum = 0, wsum = 0;

  void reset() { *this = SessStats(); }
  void add(float v, float a, float w) {
    if (!n) {
      vmin = vmax = v;
      amin = amax = a;
      wmin = wmax = w;
    } else {
      vmin = min(vmin, v), vmax = max(vmax, v);
      amin = min(amin, a), amax = max(amax, a);
      wmin = min(wmin, w), wmax = max(wmax, w);
    }
    vsum += v, asum += a, wsum += w;
    n++;
  }
  // "sum": {"n": readings, "v": [min, max, avg], "a": [...], "w": [...]}
  void toJson(JsonObject o) const {
    if (!n) return;
    JsonObject s = o["sum"].to<JsonObject>();
    s["n"] = n;
    auto put = [&](const char *k, float lo, float hi, double sum, uint8_t dec) {
      JsonArray x = s[k].to<JsonArray>();
      x.add(serialized(String(lo, (unsigned int)dec)));
      x.add(serialized(String(hi, (unsigned int)dec)));
      x.add(serialized(String(sum / n, (unsigned int)dec)));
    };
    put("v", vmin, vmax, vsum, 1);
    put("a", amin, amax, asum, 3);
    put("w", wmin, wmax, wsum, 1);
  }
};
