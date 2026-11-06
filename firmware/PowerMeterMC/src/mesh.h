// Mesh over ESP-NOW (docs/mesh.md). One firmware, two roles at run time:
//   gateway: online on Wi-Fi; sends beacons and relays leaf messages to/from the server
//   leaf:    no Wi-Fi; finds the best gateway and talks to the server through it
// Leaf messages are signed (HMAC-SHA256 with the server-issued link key), and so are the
// server's replies, so a gateway cannot fake, change or replay them.
#pragma once
#include <Arduino.h>

namespace mesh {

void begin();  // after Wi-Fi is started
void loop();   // call often from the net task (processes received frames, beacons, scanning)

// gateway role
void setGateway(bool on);
uint8_t leafCount();
// From the server: {"t":"via",...} or {"t":"via_pair_res",...} for leaf `leafId` -> send to it.
void toLeaf(const char *json, size_t len, const char *leafId, bool pairResult);

// leaf role
void startLeaf();
void stopLeaf();
bool leafLinked();          // a gateway is selected and its beacons are heard
void leafReset();           // drop the gateway (e.g. no pong) and search again
String gatewayId();
uint8_t channel();
bool leafSend(const String &innerJson);    // wrap + sign + send one protocol message
bool leafSendPair(const String &bodyJson); // pairing request (not signed: no key yet)

}  // namespace mesh
