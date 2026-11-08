#pragma once
#include <ArduinoJson.h>
namespace control { void emitEvent(const char *e, void (*fill)(JsonObject, void *) = nullptr, void *ctx = nullptr); void bumpState(); }
