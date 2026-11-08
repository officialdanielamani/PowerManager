#pragma once
#include <cstdint>
#include <cstddef>
struct Preferences { bool begin(const char*, bool){return true;} void end(){} size_t getBytesLength(const char*){return 0;}
 size_t getBytes(const char*,void*,size_t){return 0;} size_t putBytes(const char*,const void*,size_t n){return n;}
 uint16_t getUShort(const char*,uint16_t d){return d;} void putUShort(const char*,uint16_t){} bool isKey(const char*){return false;} void clear(){} };
