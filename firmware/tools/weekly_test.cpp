// Host test: g++ -std=c++17 -I../PowerMeterRBAmp/src weekly_test.cpp && ./a.out
#include "weekly.h"
#include <cstdio>
#include <ctime>
static int bad = 0;
#define CHECK(c) do { if (!(c)) { printf("FAIL line %d: %s\n", __LINE__, #c); bad++; } } while (0)
static uint32_t at(int y, int mo, int d, int h, int mi) {  // UTC epoch
  struct tm t = {}; t.tm_year = y - 1900; t.tm_mon = mo - 1; t.tm_mday = d; t.tm_hour = h; t.tm_min = mi;
  return (uint32_t)timegm(&t);
}
int main() {
  // 2026-11-26 is a Thursday. Monday 08:00 = 480, Wednesday 18:30 = 2*1440+1110, Sunday 00:15 = 6*1440+15.
  uint16_t wk[3] = {480, 2 * 1440 + 1110, 6 * 1440 + 15};
  CHECK(weeklyNext(at(2026, 11, 26, 12, 0), wk, 3) == at(2026, 11, 29, 0, 15));   // Thu noon -> Sunday 00:15
  CHECK(weeklyNext(at(2026, 11, 29, 0, 15), wk, 3) == at(2026, 11, 30, 8, 0));    // exactly Sunday 00:15 -> next Monday 08:00
  CHECK(weeklyNext(at(2026, 11, 30, 7, 59), wk, 3) == at(2026, 11, 30, 8, 0));
  CHECK(weeklyNext(at(2026, 11, 30, 8, 0), wk, 3) == at(2026, 12, 2, 18, 30));    // -> Wednesday
  CHECK(weeklyNext(at(2026, 12, 2, 18, 30), wk, 3) == at(2026, 12, 6, 0, 15));
  uint16_t one[1] = {0};                                                           // Monday 00:00 only
  CHECK(weeklyNext(at(2026, 11, 29, 23, 59), one, 1) == at(2026, 11, 30, 0, 0));
  CHECK(weeklyNext(at(2026, 11, 30, 0, 0), one, 1) == at(2026, 12, 7, 0, 0));
  CHECK(weeklyNext(1000, wk, 0) == 0);
  printf(bad ? "FAILED\n" : "weekly ok\n");
  return bad;
}
