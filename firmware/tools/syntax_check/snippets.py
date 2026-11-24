#!/usr/bin/env python3
"""Cuts the hardware-profile code out of net.cpp (command handlers) and PowerMeterRBAmp.ino (glue) and wraps it with
small stubs, so syntax_check/run.sh can compile it without the ESP32 toolchain.   snippets.py <output dir>"""
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
SK = os.path.join(ROOT, "firmware", "PowerMeterRBAmp")
out = sys.argv[1]

net = open(os.path.join(SK, "src", "net.cpp"), encoding="utf-8").read()
helpers = net[net.index("// Answer to a hardware profile command"):net.index("static void goOnline() {")]
helpers = net[net.index("// Answer to a hardware profile command"):net.index("static void goOnline() {")]
block = net[net.index('  } else if (!strcmp(c, "hw_profile")) {'):net.index('  } else if (!strcmp(c, "remove")) {')]
open(os.path.join(out, "net_snippet.cpp"), "w", encoding="utf-8").write('''#include "Arduino.h"
#include <ArduinoJson.h>
#include "hwprofile.h"
#include "ota.h"
SerialT Serial;
static void sendJson(JsonDocument &) {}
namespace rbamp { uint8_t scan(uint8_t *, uint8_t) { return 0; } bool scanFlooded() { return false; } bool isRbamp(uint8_t) { return true; }
                  bool changeAddress(uint8_t, uint8_t, String &) { return true; } }
namespace control { bool anySessionRunning() { return false; }
                    bool startSensorCheck(uint8_t, uint8_t, const char *&) { return true; } }
namespace i2cbus { bool probe(uint8_t) { return false; } bool present(uint8_t) { return false; } }
''' + helpers + '''
static void handle(JsonObject m) {
  const char *c = m["c"] | "";
  uint32_t ref = m["ref"] | 0;
  const char *err = nullptr;
  bool ok = true;
  int8_t ch = m["ch"].is<int>() ? (int8_t)m["ch"].as<int>() : (int8_t)-1;
  if (false) {
''' + block + '''  }
  (void)ok; (void)err; (void)ref;
}
''')

ino = open(os.path.join(SK, "PowerMeterRBAmp.ino"), encoding="utf-8").read()
glue = ino[ino.index("// ---- hardware profile"):ino.index("static void loadState() {")]
open(os.path.join(out, "glue_snippet.cpp"), "w", encoding="utf-8").write('''#include "Arduino.h"
#include <ArduinoJson.h>
#include "hwmap.h"
#include "hwprofile.h"
#include "ota.h"
SerialT Serial;
namespace net { bool serverParams(String &, uint16_t &, bool &, bool &) { return false; } }
struct WifiT { int status() { return 0; } };
static WifiT WiFi;
#define WL_CONNECTED 3
namespace rbamp { bool isRbamp(uint8_t) { return true; } void applyClamps() {} }
namespace i2cbus { bool probe(uint8_t) { return true; } bool present(uint8_t) { return true; } }
namespace control { void emitEvent(const char *, void (*)(JsonObject, void *) = nullptr, void * = nullptr) {}
                    void prepareRestart(bool = false) {} void bumpState() {} }
namespace sys { void reboot(uint32_t) {} }
''' + glue)

# PZEM sketch: the sensor_check command branch of its net.cpp (it has no hardware-profile code)
pz = open(os.path.join(ROOT, "firmware", "PowerMeterPZEM", "src", "net.cpp"), encoding="utf-8").read()
a = pz.index('  } else if (!strcmp(c, "sensor_check")) {')
b = pz.index('  } else if (!strcmp(c, "send_now")) {')
open(os.path.join(out, "pzem_net_snippet.cpp"), "w", encoding="utf-8").write('''#include "Arduino.h"
#include <ArduinoJson.h>
namespace control { bool startSensorCheck(uint8_t, uint8_t, const char *&) { return true; } }
static void handle(JsonObject m) {
  const char *c = m["c"] | "";
  const char *err = nullptr;
  bool ok = true;
  if (false) {
''' + pz[a:b] + '''  }
  (void)ok; (void)err; (void)c;
}
''')

# PZEM parseConfig: the zero / tare lines
a = pz.index('  // Zero and tare of the measured line')
b = pz.index('  m.tare_limits = c["tare_limits"] | true;') + len('  m.tare_limits = c["tare_limits"] | true;')
open(os.path.join(out, "pzem_cfg_snippet.cpp"), "w", encoding="utf-8").write('''#include "Arduino.h"
#include <ArduinoJson.h>
struct M { float zero_a = 0, zero_w = 0, tare_a = 0, tare_w = 0; bool tare_limits = true; };
static bool parse(JsonObject c, M &m, String &err) {
''' + pz[a:b] + '''
  return true;
}
''')
