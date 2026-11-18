// Firmware update over the air (docs/protocol.md §14): the server sends the command "ota" (path with a one-time token,
// size, SHA-256, version, type); the meter ends its sessions, switches the relays off, downloads the image over HTTP(S)
// into the spare app slot (own task), checks size / SHA-256 / firmware tag, switches slot and restarts.
// The new app must reach the server within OTA_CONFIRM_MS (welcome from the server), otherwise it goes back to the
// previous slot. (The Arduino bootloader has no rollback support, so this is done by the app: an NVS note "try" is
// set before the restart, counted at every boot and cleared by confirm().) Same file in both sketches.
#pragma once
#include <Arduino.h>
#include <ArduinoJson.h>

namespace ota {

struct Hooks {
  void (*prepare)();                                    // end the sessions (reason "restart"), save energy / state, relays off
  void (*restart)();                                    // restart now (does not return)
  void (*event)(const char *name, const char *state, int pct, const char *why, const char *ver);  // ota_* events
  bool (*server)(String &host, uint16_t &port, bool &tls, bool &insecure);  // false: no server (mesh leaf, not paired)
  bool (*wifiUp)();
};

// First thing in setup(): handles a pending update (counts unconfirmed boots, rolls back when needed).
void begin(const Hooks &hooks);
const char *tag();                 // "PMFWTAG|<type>|<version>|<hw>|" (also keeps the tag in the image)
void capsJson(JsonObject o);       // hello.caps.ota: {slot, layout, tag}
void stateJson(JsonObject o);      // hello.ota: {state: ok | trial | rolled_back, ver, tried?, why?}
bool onCommand(JsonObjectConst m, const char *&err);  // "ota": starts the update (the download runs in its own task)
bool busy();                       // an update is running (config / profile changes are refused meanwhile)
void confirm();                    // the server answered the hello of this boot: the new firmware is good
void tick();                       // loop: events for the progress, confirm timeout (rollback)

}  // namespace ota
