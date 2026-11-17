// Hardware profile of the rbAmp meter: which rbAmp modules, relay boards and channel wiring it uses.
// The compiled tables of config.h are the default profile; the server can send another one
// (docs/protocol.md §12). A new structure is stored as a *trial* profile and used after a delayed restart; it
// becomes the active one only when every listed part answers, otherwise the meter rolls back to the previous
// profile. Clamp models alone are applied at once (live). Pure logic + NVS: no I2C / control dependencies
// (those come in through Hooks), so it also runs in the PC test (firmware/tools/hwprofile_test).
#pragma once
#include <Arduino.h>
#include <ArduinoJson.h>

#define HWP_MAX_BOARDS 8        // rbAmp modules
#define HWP_MAX_RELAY_BOARDS 8  // entries of the relay table
#define HWP_MAX_CH 16           // measured channels (same as MC_MAX_CH)
#define HWP_MAX_RELAYS 32
// Relay drivers (same numbers as RELAY_* in relaymap.h)
#define HWP_DRV_PCF8574 1
#define HWP_DRV_XL9535 2
#define HWP_DRV_GPIO 3
#define HWP_DRV_MCP23017 4

namespace hwp {

struct RelayBoard {
  uint8_t driver, address, relays, activeHigh;
};
struct Channel {
  uint8_t board;  // 1 = first rbAmp of the list
  uint8_t input;  // 1..7
  uint8_t clamp;  // SCT_* model code written to the module (0 = keep)
  uint8_t rb;     // relay board: 1 = first entry of the relay table, 0 = no relay
  uint8_t ro;     // output on that board: 1..relays of the board (0 when rb is 0)
};
struct Profile {
  uint32_t ver = 0;  // 0 = the compiled default
  uint8_t nBoards = 0;
  uint8_t addr[HWP_MAX_BOARDS] = {0};
  uint8_t nRelayBoards = 0;
  RelayBoard relay[HWP_MAX_RELAY_BOARDS] = {};
  uint8_t nCh = 0;
  Channel ch[HWP_MAX_CH] = {};
};

enum State : uint8_t { ST_DEFAULT = 0, ST_ACTIVE = 1, ST_PENDING = 2, ST_TRIAL = 3 };

// What the profile logic needs from the rest of the firmware.
struct Hooks {
  bool (*probe)(uint8_t addr, bool rbamp);   // does a part answer at this I2C address (rbamp: identifies as rbAmp)
  void (*event)(const char *name, uint32_t ver, int inS, const char *reason, int addr);  // hw_* events
  void (*restart)();                         // end sessions, save state, restart (does not return)
  void (*applyClamps)();                     // live clamp change: tables reloaded, models written to the modules
  uint32_t (*millis)();
};

// ---- rules (the same as the server's services/hwprofile.py)
int totalRelays(const Profile &p);
bool validate(const Profile &p, uint8_t gpioPins, char *err, size_t n);  // false + first error text
// ---- JSON (format of docs/protocol.md §12.1; clamp models as amps, drivers as names)
bool fromJson(JsonVariantConst j, Profile &p, char *err, size_t n);      // structure only; call validate() after
void toJson(const Profile &p, JsonObject o);
bool sameStructure(const Profile &a, const Profile &b);                  // everything except the clamp models

// ---- life cycle
// Chooses the profile to run: the trial one (boot of a pending change), else the stored active one, else the
// compiled default. Counts unconfirmed trial boots (two are tolerated, then the profile is rolled back).
void begin(const Profile &compiledDefault, uint8_t gpioPins, const Hooks &hooks);
const Profile &active();
State state();
bool rebootPending();         // countdown to the restart is running (AUX event source "reboot")
void stateJson(JsonObject o); // hello.hwp: {ver, state, profile}
// Commands. err / detail are filled on failure; resState = "live" | "pending", inS = countdown seconds.
bool onProfile(JsonObjectConst msg, const char *&err, char *detail, size_t dn, const char *&resState, int &inS);
bool onCancel();
void tick();                  // every 100..500 ms: announce a stored note, countdown, confirm check

}  // namespace hwp
