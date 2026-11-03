// Wi-Fi, pairing and the server WebSocket (docs/protocol.md). Runs in its own task.
#pragma once
#include <Arduino.h>

namespace net {

struct PairRequest {
  String host;
  uint16_t port = 443;
  bool tls = true;
  bool insecure = false;
  String code;
};

void begin();
void task(void *);

void requestPair(const PairRequest &r);  // handled asynchronously by the net task
String pairStatus();                     // "idle", "working", "ok" or an error message
// First-time Wi-Fi setup: try the network while the setup AP stays on, so the page can show
// the new IP address. Status: "idle", "connecting", "ok", "failed".
void tryWifi(const String &ssid, const String &pass);
String wifiTestStatus();

String serverHost();
uint16_t outboxCount();
String apSsid();
String ipAddress();
int rssi();

}  // namespace net
