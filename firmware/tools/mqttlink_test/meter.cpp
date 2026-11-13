// A "meter" on the PC that uses the firmware's real mqttlink.cpp: log in to the broker, say hello, send one
// reading, print what the server answers.   meter <broker port> <device id> <link key> <prefix> [tamper]
#include <arpa/inet.h>
#include <netinet/in.h>
#include <poll.h>
#include <sys/socket.h>

#include "src/mqttlink.h"
#include "src/timekeep.h"

SerialT Serial;
String g_devId, g_key;
uint32_t g_nonce = 0;

int main(int argc, char **argv) {
  if (argc < 5) return 2;
  g_devId = argv[2];
  g_key = argv[3];
  bool tamper = argc > 5;
  mqttlink::Cfg c;
  c.enabled = true;
  c.host = "127.0.0.1";
  c.prefix = argv[4];
  c.user = "meters";
  c.pass = "s3cret";
  mqttlink::save(c);
  WebSocketsClient ws;
  ws.fd = socket(AF_INET, SOCK_STREAM, 0);
  sockaddr_in a{};
  a.sin_family = AF_INET;
  a.sin_port = htons(atoi(argv[1]));
  inet_pton(AF_INET, "127.0.0.1", &a.sin_addr);
  if (connect(ws.fd, (sockaddr *)&a, sizeof(a))) return 3;
  mqttlink::start(ws);
  mqttlink::onWsConnected(ws);  // MQTT CONNECT (login + last will)
  if (tamper) g_key = "not-the-real-key";  // a meter (or attacker) with the wrong key
  uint32_t t0 = millis();
  int welcome = 0, ack = 0;
  while (millis() - t0 < 8000 && !(welcome && ack)) {
    pollfd p{ws.fd, POLLIN, 0};
    if (poll(&p, 1, 200) <= 0) continue;
    uint8_t buf[2048];
    ssize_t n = read(ws.fd, buf, sizeof(buf));
    if (n <= 0) break;
    String inner;
    for (size_t len = n;; len = 0) {
      mqttlink::Result r = mqttlink::onBinary(ws, buf, len, inner);
      if (r == mqttlink::NOTHING) break;
      if (r == mqttlink::REFUSED) return 4;
      if (r == mqttlink::READY) {
        char hello[256];
        snprintf(hello, sizeof(hello), "{\"t\":\"hello\",\"fw\":\"test\",\"proto\":1,\"boot\":1,\"up\":5,\"tv\":1,\"ts\":%u,"
                 "\"cfg_ver\":0,\"caps\":{\"kind\":\"ac\",\"channels\":1,\"relays\":1,\"mqtt\":true}}", timekeep::now());
        mqttlink::publish(ws, hello);
      } else if (r == mqttlink::MESSAGE) {
        printf("RX %s\n", inner.c_str());
        if (inner.find("\"t\":\"welcome\"") != String::npos && !welcome++) {
          char tel[256];
          snprintf(tel, sizeof(tel), "{\"t\":\"tel\",\"items\":[{\"seq\":7,\"ts\":%u,\"tv\":1,\"ch\":[{\"i\":0,\"v\":230.5,"
                   "\"a\":1.25,\"w\":288.1,\"pf\":1,\"hz\":50,\"e\":3.5}]}]}", timekeep::now());
          mqttlink::publish(ws, tel);
        }
        if (inner.find("\"t\":\"ack\"") != String::npos) ack++;
      }
    }
  }
  printf("RESULT welcome=%d ack=%d\n", welcome, ack);
  return welcome && ack ? 0 : 1;
}
