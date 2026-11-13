#pragma once
#include <Arduino.h>
extern String g_devId, g_key;
extern uint32_t g_nonce;
namespace store {
inline String deviceId() { return g_devId; }
inline String linkKey() { return g_key; }
inline uint32_t nextLinkNonce() { return ++g_nonce; }
}
