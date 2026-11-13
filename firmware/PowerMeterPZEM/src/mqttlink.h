// MQTT link to the server (NET_MQTT in config.h): the same JSON protocol as the WebSocket link, carried
// through an MQTT broker over WebSocket (ws:// or wss://, sub-protocol "mqtt"), e.g. behind Cloudflare.
//   <prefix>/<DEVICE_ID>/up      meter -> server      <prefix>/<DEVICE_ID>/down    server -> meter
//   <prefix>/<DEVICE_ID>/status  "online" / "offline" (retained, last will)
// Every message is wrapped as {"n": counter, "h": hmac, "m": "<protocol JSON>"} and signed with the meter's
// link key (given at pairing), so somebody who only has the broker login cannot fake a meter or command one.
// Own small MQTT 3.1.1 client (QoS 0; the protocol has its own acknowledgements): no extra library.
// Shared by both firmwares (firmware/tools/check_shared.py).
#pragma once
#include <Arduino.h>
#include <ArduinoJson.h>
#include <WebSocketsClient.h>
#include "../config.h"

#ifndef NET_MQTT
#define NET_MQTT 1
#endif
#ifndef NET_WEBSOCKET
#define NET_WEBSOCKET 1
#endif
#if !NET_MQTT && !NET_WEBSOCKET
#error "config.h: enable NET_WEBSOCKET or NET_MQTT (or both)"
#endif

namespace mqttlink {

struct Cfg {
  bool enabled = false;
  String host;
  uint16_t port = 443;
  bool tls = true;
  bool insecure = false;   // skip the TLS certificate check (self-signed broker)
  String path = "/";       // WebSocket path of the broker ("/" or "/mqtt")
  String user, pass;       // broker login (optional)
  String prefix = "powermanager";
};

void begin();                       // load the saved broker settings (NVS)
const Cfg &cfg();
void save(const Cfg &c);
bool fromJson(JsonObjectConst o, Cfg &out, const char *&err);  // pairing reply / set_mqtt command
void wipe();                        // factory reset
bool active();                      // compiled in, enabled and a broker host is set

#if NET_MQTT
enum Result : int8_t { NOTHING = 0, READY = 1, MESSAGE = 2, REFUSED = -1 };

void start(WebSocketsClient &ws);                  // open the WebSocket to the broker
void onWsConnected(WebSocketsClient &ws);          // WebSocket is up: send MQTT CONNECT
// Feed a binary WebSocket frame. READY = logged in and subscribed (send hello now); MESSAGE = `inner`
// holds one verified protocol message (call again with len 0 until NOTHING: frames can carry several).
Result onBinary(WebSocketsClient &ws, const uint8_t *data, size_t len, String &inner);
bool publish(WebSocketsClient &ws, const String &inner);  // sign + publish one protocol message
void loop(WebSocketsClient &ws);                   // keep-alive
void reset();                                      // link dropped
#endif

}  // namespace mqttlink
