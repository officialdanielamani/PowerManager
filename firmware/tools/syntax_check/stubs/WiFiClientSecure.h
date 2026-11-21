#pragma once
// In the real core WiFiClientSecure.h does not declare WiFiClient: the sketch must include WiFiClient.h itself.
#include "WiFiClient.h"
struct WiFiClientSecure : WiFiClient { void setInsecure() {} void setCACert(const char *) {} };
