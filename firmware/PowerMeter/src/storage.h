// Persistent settings in NVS (Preferences). "ident" survives factory reset, "pm" does not.
#pragma once
#include <Arduino.h>
#include "app_state.h"

namespace store {

struct Server {
  String host;
  uint16_t port = 443;
  bool tls = true;
  bool insecure = false;  // skip TLS certificate check (self-signed servers)
  String secret;          // empty = not paired
};

void begin();

String deviceId();     // 8 chars from the chip's MAC, e.g. "K7P29QXM"
String apPassword();   // random on first boot, kept forever
uint32_t bootCount();  // incremented every boot

bool hasWifi();
String wifiSsid();
String wifiPass();
void setWifi(const String &ssid, const String &pass);

bool hasAdmin();
String adminUser();
void setAdmin(const String &user, const String &pass);
bool checkAdmin(const String &user, const String &pass);

bool loadServer(Server &s);
void saveServer(const Server &s);
void clearSecret();

bool loadConfig(MeterConfig &c);
void saveConfig(const MeterConfig &c);

void loadSession(SessionState &s);
void saveSession(const SessionState &s);

void loadFlags(bool &wantOn, bool &tripped);
void saveFlags(bool wantOn, bool tripped);
bool loadDisabled();
void saveDisabled(bool disabled);

uint32_t nextSeq();  // strictly increasing, survives reboot

void factoryReset();  // wipe everything except identity + AP password

}  // namespace store
