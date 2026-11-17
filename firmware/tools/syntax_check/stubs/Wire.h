#pragma once
#include <cstdint>
#include <cstddef>
struct WireT { void begin(int,int,uint32_t){} void setTimeOut(int){} void beginTransmission(uint8_t){} void write(uint8_t){} void write(const uint8_t*,size_t){} int endTransmission(bool=true){return 0;} size_t requestFrom(uint16_t,uint8_t){return 0;} int read(){return 0;} };
extern WireT Wire;
