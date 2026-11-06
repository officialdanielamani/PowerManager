// Wi-Fi, pairing and the server WebSocket (docs/protocol.md). Runs in its own task.
#pragma once
#include <Arduino.h>
#include <ArduinoJson.h>

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
String linkMode();      // "wifi", "mesh" (leaf via a gateway), "searching" (leaf, no gateway) or "none"
String meshGateway();   // gateway id while a mesh leaf
uint8_t meshLeaves();   // leaves relayed while a gateway

// Called by the mesh module (net task)
void meshToServer(const char *json, size_t len);        // gateway: forward a leaf message
void meshDeliver(const char *json, size_t len);         // leaf: verified message from the server
void meshPairResult(int code, ArduinoJson::JsonObject resp);  // leaf: pairing answer via a gateway
void meshChannel(uint8_t ch);                           // leaf: radio moved to the gateway's channel
uint16_t outboxCount();
String apSsid();
String ipAddress();
int rssi();

}  // namespace net
