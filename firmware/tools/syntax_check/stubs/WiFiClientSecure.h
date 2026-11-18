#pragma once
#include "WiFiClient.h"
struct WiFiClientSecure : WiFiClient {
  void setInsecure() {}
  void setCACert(const char *) {}
};
