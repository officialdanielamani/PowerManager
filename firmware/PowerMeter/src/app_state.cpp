#include "app_state.h"

AppState g;
SemaphoreHandle_t gLock = nullptr;

void state_init() { gLock = xSemaphoreCreateRecursiveMutex(); }

const char *sessStatusName(SessStatus s) {
  switch (s) {
    case SESS_QUOTA: return "quota";
    case SESS_EXPIRED: return "expired";
    case SESS_ENDED: return "ended";
    default: return "active";
  }
}
