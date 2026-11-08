// Local web UI (setup, login, dashboard, pairing). No external files or CDNs:
// the whole page is embedded in webui.h and works without internet.
#pragma once

namespace web {
void begin();
void task(void *);
}  // namespace web
