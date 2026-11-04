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

// Mesh
String linkKey();                 // HMAC key for mesh messages (from the server)
void setLinkKey(const String &k);
uint32_t nextLinkNonce();         // uplink counter, strictly increasing, survives reboot
uint64_t linkDownLast();          // highest downlink counter accepted (replay protection)
void setLinkDownLast(uint64_t n);
uint8_t meshRole();               // 0 = Auto (Wi-Fi first, mesh fallback, relay for others), 1 = Wi-Fi only
void setMeshRole(uint8_t r);
bool meshOnly();                  // set up without Wi-Fi: always a mesh leaf
void setMeshOnly(bool on);

void factoryReset();  // wipe everything except identity + AP password

}  // namespace store
