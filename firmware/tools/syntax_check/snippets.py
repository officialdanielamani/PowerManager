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
SerialT Serial;
static void sendJson(JsonDocument &) {}
namespace rbamp { uint8_t scan(uint8_t *, uint8_t) { return 0; } bool isRbamp(uint8_t) { return true; }
                  bool changeAddress(uint8_t, uint8_t, String &) { return true; } }
namespace control { bool anySessionRunning() { return false; } }
namespace i2cbus { bool probe(uint8_t) { return false; } }
''' + helpers + '''
static void handle(JsonObject m) {
  const char *c = m["c"] | "";
  uint32_t ref = m["ref"] | 0;
  const char *err = nullptr;
  bool ok = true;
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
SerialT Serial;
namespace rbamp { bool isRbamp(uint8_t) { return true; } void applyClamps() {} }
namespace i2cbus { bool probe(uint8_t) { return true; } }
namespace control { void emitEvent(const char *, void (*)(JsonObject, void *) = nullptr, void * = nullptr) {}
                    void prepareRestart() {} void bumpState() {} }
namespace sys { void reboot(uint32_t) {} }
''' + glue)
