#include <cassert>
#include "auxrelay.h"
#include "hwmap.h"
uint32_t g_ms = 100000, g_now = 0; float g_temp[4] = {NAN, NAN, NAN, NAN}; SerialT Serial;
static int events = 0;
namespace control { void emitEvent(const char *, void (*)(JsonObject, void *), void *) { events++; } void bumpState() {} }
// 2026-11-07 is a Saturday. Local = UTC+8 (Malaysia).
static bool on(int i) { return auxrelay::outputMask() & (1u << (hw::auxRelay(i - 1) - 1)); }
int main() {
  // 2026-11-07 00:00 UTC = 1762473600. Local midnight (UTC+8) of Sat 2026-11-07 = 1762473600 - 8*3600.
  const uint32_t SAT0 = 1794009600u - 8 * 3600;
  auto T = [&](int dayOffset, int h, int m) { return SAT0 + dayOffset * 86400 + h * 3600 + m * 60; };
  JsonDocument d;
  deserializeJson(d, R"([
    {"i":0,"name":"Desk Lamp","mode":"schedule","override":true,"else":"off",
     "sched":[{"on":true,"start":1080,"end":1439,"days":127},{"on":true,"start":60,"end":120,"days":32}]},
    {"i":1,"name":"Exhaust Fan","mode":"event","override":false,
     "event":{"src":"temp","idx":1,"cmp":"over","value":50,"on":true}},
    {"i":2,"name":"Night","mode":"schedule","else":"off","sched":[{"on":true,"start":1320,"end":360,"days":1}]}])");
  const char *err = nullptr;
  assert(auxrelay::parse(d.as<JsonArrayConst>(), 480, err));
  auxrelay::commit();
  auxrelay::Inputs in; in.nch = 1; in.readOk = true;
  auto step = [&](uint32_t now) { g_now = now; g_ms += 6000; auxrelay::tick(in); };
  // Desk lamp: Saturday 01:30 local -> ON (Saturday window); 03:00 -> OFF; 18:30 -> ON; 23:59 -> still ON till midnight
  step(T(0, 1, 30)); assert(on(1));
  step(T(0, 3, 0)); assert(!on(1));
  step(T(0, 18, 30)); assert(on(1));
  step(T(0, 23, 59)); assert(on(1));
  step(T(1, 0, 0)); assert(!on(1));       // Sunday 00:00: off
  step(T(1, 1, 30)); assert(!on(1));      // Sunday 01:30: the 01:00-02:00 window is Saturday only
  // Override: Saturday next week 01:05 user turns it OFF -> stays OFF until the automation changes (02:00 end)
  step(T(7, 1, 0)); assert(on(1));
  assert(auxrelay::set(0, false, "server", err)); step(T(7, 1, 5)); assert(!on(1));
  step(T(7, 1, 50)); assert(!on(1));      // still overridden
  step(T(7, 2, 0)); assert(!on(1));       // automation -> OFF, override ends
  step(T(7, 18, 0)); assert(on(1));       // next window ON again
  // Fan: override not allowed
  assert(!auxrelay::set(1, true, "server", err) && !strcmp(err, "automation"));
  g_temp[1] = 49.5; step(T(8, 12, 0)); assert(!on(2));
  g_temp[1] = 50.0; step(T(8, 12, 1)); assert(on(2));    // >= 50 -> ON
  g_temp[1] = 49.5; step(T(8, 12, 2)); assert(on(2));    // hysteresis 1 C: stays ON
  g_temp[1] = 48.9; step(T(8, 12, 3)); assert(!on(2));   // below 49 -> OFF
  g_temp[1] = NAN;  step(T(8, 12, 4)); assert(!on(2));   // sensor lost: keep last state
  // Overnight window Monday 22:00 -> Tuesday 06:00 (days = Monday only)
  step(T(2, 23, 0)); assert(on(3));       // Monday 23:00
  step(T(3, 5, 59)); assert(on(3));       // Tuesday 05:59 (started Monday)
  step(T(3, 6, 0)); assert(!on(3));
  step(T(3, 23, 0)); assert(!on(3));      // Tuesday 23:00: not a Monday start
  // Disabled meter: everything off
  in.disabled = true; step(T(7, 19, 0)); assert(!on(1));
  printf("all AUX automation checks passed (%d events)\n", events);
}
