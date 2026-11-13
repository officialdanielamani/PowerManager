// The firmware sends MQTT packets as binary WebSocket frames; here they go straight onto a TCP socket to the
// broker's plain MQTT listener (the MQTT bytes are the same either way).
#pragma once
#include <cstdint>
#include <cstddef>
#include <unistd.h>
struct WebSocketsClient {
  int fd = -1;
  bool sendBIN(const uint8_t *p, size_t n) { return fd >= 0 && ::write(fd, p, n) == (ssize_t)n; }
  void setExtraHeaders(const char *) {}
  void begin(const char *, uint16_t, const char *, const char *) {}
  void beginSSL(const char *, uint16_t, const char *, const char *, const char *) {}
  void beginSslWithCA(const char *, uint16_t, const char *, const char *, const char *) {}
};
