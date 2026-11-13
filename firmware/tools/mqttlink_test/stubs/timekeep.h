#pragma once
#include <ctime>
namespace timekeep { inline bool valid() { return true; } inline uint32_t now() { return (uint32_t)time(nullptr); } }
