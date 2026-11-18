// In-memory NVS: shared by every instance, survives "restarts" of the test.
#pragma once
#include <cstdint>
#include <cstddef>
#include <cstring>
#include <map>
#include <string>
extern std::map<std::string, std::string> g_nvs;
extern bool g_nvsFull;   // test switch: putBytes stores nothing (NVS full)
struct Preferences {
  bool begin(const char *, bool) { return true; }
  void end() {}
  size_t getBytesLength(const char *k) { auto i = g_nvs.find(k); return i == g_nvs.end() ? 0 : i->second.size(); }
  size_t getBytes(const char *k, void *b, size_t n) { auto i = g_nvs.find(k); if (i == g_nvs.end()) return 0; n = n < i->second.size() ? n : i->second.size(); memcpy(b, i->second.data(), n); return n; }
  size_t putBytes(const char *k, const void *b, size_t n) { if (g_nvsFull) return 0; g_nvs[k] = std::string((const char *)b, n); return n; }
  uint8_t getUChar(const char *k, uint8_t d) { auto i = g_nvs.find(k); return i == g_nvs.end() ? d : (uint8_t)i->second[0]; }
  size_t putUChar(const char *k, uint8_t v) { g_nvs[k] = std::string(1, (char)v); return 1; }
  bool remove(const char *k) { return g_nvs.erase(k) > 0; }
};
