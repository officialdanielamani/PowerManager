#pragma once
#include <map>
#include <string>
struct Preferences {  // in memory
  static std::map<std::string, std::string> &m() { static std::map<std::string, std::string> x; return x; }
  bool begin(const char *, bool) { return true; }
  void end() {}
  void clear() { m().clear(); }
  bool getBool(const char *k, bool d) { return m().count(k) ? m()[k] == "1" : d; }
  void putBool(const char *k, bool v) { m()[k] = v ? "1" : "0"; }
  std::string getString(const char *k, const char *d) { return m().count(k) ? m()[k] : d; }
  void putString(const char *k, const std::string &v) { m()[k] = v; }
  uint16_t getUShort(const char *k, uint16_t d) { return m().count(k) ? (uint16_t)std::stoi(m()[k]) : d; }
  void putUShort(const char *k, uint16_t v) { m()[k] = std::to_string(v); }
};
