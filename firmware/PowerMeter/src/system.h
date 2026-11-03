#pragma once
#include <Arduino.h>

namespace sys {
void factoryReset(const char *why);  // wipe settings + buffer, reboot into first-boot AP mode
void reboot(uint32_t delayMs = 500);
void heartbeat(uint8_t task);        // soft watchdog for tasks that may block on the network
void checkHeartbeats();              // called from loop()
enum { HB_NET = 0, HB_WEB = 1, HB_COUNT = 2 };
}
