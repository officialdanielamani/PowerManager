#pragma once
#include <Arduino.h>

namespace sys {
void factoryReset(const char *why);  // wipe settings + buffer, reboot into first-boot AP mode
void reboot(uint32_t delayMs = 500);
void heartbeat(uint8_t task);        // soft watchdog for tasks that may block on the network
void checkHeartbeats();              // called from loop()
extern TaskHandle_t taskHandles[4];  // control, led, net, web
String stackReport();                // "Stack free: control 1234 B, ..."
void checkStacks();                  // warns on serial when a task gets close to its stack limit
enum { HB_NET = 0, HB_WEB = 1, HB_COUNT = 2 };

// Crash breadcrumbs: each task notes what it is doing. They live in RTC memory, which survives a
// crash reboot, so the next boot can print where every task was when the meter crashed.
enum { CR_BOOT = 0, CR_MAIN, CR_NET, CR_WEB, CR_CONTROL, CR_LED, CR_COUNT };
void crumb(uint8_t slot, const char *where);
void bootCheck();          // first thing in setup(): reset reason + report of the previous crash
void bootDone();           // end of setup()
void tick();               // from loop(): uptime / heap for the crash report, clears the crash streak
const char *resetReason(); // of this boot
// Safe mode: after 3 crashes in a row within 2 min of boot, the next boot skips the mesh and the
// DS3231 so the meter keeps working (and reporting) while the cause is found.
extern bool safeMode;
}
