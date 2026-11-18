#pragma once
#include "WiFiClient.h"
#include "WiFiClientSecure.h"
struct HTTPClient {
  void setTimeout(uint16_t) {}
  void setConnectTimeout(uint16_t) {}
  bool begin(WiFiClient &, const String &) { return true; }
  int GET() { return 200; }
  int getSize() { return 0; }
  WiFiClient *getStreamPtr() { return nullptr; }
  bool connected() { return false; }
  void end() {}
};
