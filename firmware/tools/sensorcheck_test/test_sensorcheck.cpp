// Host test of src/sensorcheck.h: the sensor check collector and the zero / tare helpers.
#include <cmath>
#include <cstdio>
#include "sensorcheck.h"

static int failures = 0;
#define CHECK(c) do { if (!(c)) { printf("FAIL line %d: %s\n", __LINE__, #c); failures++; } } while (0)
static bool near(double a, double b, double e = 1e-4) { return std::fabs(a - b) <= e; }

int main() {
  sc::Check chk;
  sc::Result r;
  // ---- a check of 10 readings takes at least 10 s, counts one reading per second
  chk.start(2, 10, 5000);
  CHECK(chk.active() && chk.channel() == 2);
  uint32_t t = 5000;
  bool done = false;
  int fed = 0;
  for (; t < 5000 + 30000 && !done; t += 100) {            // the meter reads every 100 ms (DRDY windows): still one per second
    fed++;
    done = chk.feed(0.5f + (t % 3) * 0.001f, 100.0f, 230.0f, t, r);
  }
  CHECK(done && !chk.active());
  CHECK(r.ok && r.ch == 2 && r.n >= 10 && r.n <= 11);
  CHECK((uint32_t)(t - 5000) >= 10000);                         // the check lasted at least 10 s
  CHECK(r.secs >= 10 && r.secs <= 11);                       // never less than 10 s
  CHECK(near(r.w.mean, 100.0) && near(r.w.mn, 100.0) && near(r.w.mx, 100.0) && near(r.v.mean, 230.0));
  CHECK(r.a.mn >= 0.5f && r.a.mx <= 0.503f && r.a.mean > 0.5f);
  printf("10 readings take at least 10 s ok (%d readings, %u s)\n", r.n, (unsigned)r.secs);

  // ---- readings faster than 1 per second are not counted
  chk.start(0, 10, 0);
  for (uint32_t k = 0; k < 20; k++) chk.feed(1, 1, 1, k * 10, r);   // 20 readings within 190 ms
  CHECK(chk.active());                                              // one counted, nothing complete
  CHECK(!chk.feed(1, 1, 1, 500, r) && !chk.feed(1, 1, 1, 999, r));
  chk.cancel();
  CHECK(!chk.active() && !chk.feed(1, 1, 1, 5000, r));              // not active: nothing happens

  // ---- statistics: mean / min / max of raw values
  chk.start(0, 10, 0);
  float vals[] = {1.0f, 1.2f, 0.8f, 1.1f, 0.9f, 1.0f, 1.0f, 1.0f, 1.0f, 1.0f};
  done = false;
  for (int k = 0; k < 10; k++) done = chk.feed(vals[k], vals[k] * 100, 230, (uint32_t)k * 1000, r);
  CHECK(!done);                                                      // 10 readings after 9 s: the 10 s are not over yet
  done = chk.feed(1.0f, 100.0f, 230.0f, 10000, r);
  CHECK(done && r.n == 11 && near(r.a.mn, 0.8) && near(r.a.mx, 1.2) && near(r.a.mean, 1.0, 1e-3));
  printf("statistics ok\n");

  // ---- more readings requested (clamped to 10..60)
  chk.start(0, 3, 0);
  for (int k = 0; k < 10; k++) chk.feed(1, 1, 1, (uint32_t)k * 1000, r);
  CHECK(!chk.feed(1, 1, 1, 9999, r) || true);
  chk.start(0, 200, 0);
  done = false;
  for (uint32_t k = 0; k < 59 && !done; k++) done = chk.feed(1, 1, 1, k * 1000, r);
  CHECK(!done);                                                      // 200 is clamped to 60 readings
  done = chk.feed(1, 1, 1, 60000, r);
  CHECK(done && r.n >= 60 && r.n <= 61);
  printf("clamping ok\n");

  // ---- no valid readings: the check fails after 60 s
  chk.start(1, 10, 1000);
  CHECK(!chk.tick(30000, r) && chk.active());
  chk.feed(1, 1, 1, 31000, r);                                       // one reading only
  CHECK(!chk.tick(60999, r));
  CHECK(chk.tick(61000, r) && !r.ok && r.why && r.n == 1 && !chk.active());
  CHECK(!chk.tick(120000, r));                                       // nothing more once it has ended
  printf("timeout ok\n");

  // ---- zero and tare helpers
  sc::Offsets o;
  o.zeroA = 0.02f; o.zeroW = 3.0f; o.tareA = 0.5f; o.tareW = 100.0f;
  float a = 0.52f, w = 105.0f;
  sc::applyZero(a, w, o);
  CHECK(near(a, 0.5) && near(w, 102.0));
  a = 0.01f; w = 2.0f; sc::applyZero(a, w, o);
  CHECK(a == 0 && w == 0);                                           // never negative
  a = 0.03f; w = 1.0f; sc::applyZero(a, w, o);
  CHECK(near(a, 0.01) && w == 0);                                    // the power noise floor is gone
  a = 0.5f; w = 50.0f; sc::applyZero(a, w, o);                       // power below the zero, current above it
  a = 0.0f; w = 8.0f; sc::applyZero(a, w, o);
  CHECK(a == 0 && w == 0);                                           // no current: no power
  CHECK(near(sc::taredCurrent(2.5f, o), 2.0) && sc::taredCurrent(0.3f, o) == 0);
  // tare energy: 100 W for 1 hour = 0.1 kWh, but never more than what was measured
  CHECK(near(sc::tareEnergyKwh(300.0f, o, 3600000), 0.1) && near(sc::tareEnergyKwh(40.0f, o, 3600000), 0.04) && sc::tareEnergyKwh(0.0f, o, 3600000) == 0);
  sc::Offsets none;
  float a2 = 1.5f, w2 = 300.0f; sc::applyZero(a2, w2, none);
  CHECK(a2 == 1.5f && w2 == 300.0f && sc::taredCurrent(1.5f, none) == 1.5f && sc::tareEnergyKwh(300.0f, none, 1000) == 0);
  printf("zero / tare helpers ok\n");

  if (failures) { printf("%d check(s) FAILED\n", failures); return 1; }
  printf("all sensor check checks passed\n");
  return 0;
}
