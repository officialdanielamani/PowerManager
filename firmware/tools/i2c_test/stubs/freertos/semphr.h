#pragma once
#include "FreeRTOS.h"
inline SemaphoreHandle_t xSemaphoreCreateRecursiveMutex() { return nullptr; }
inline int xSemaphoreTakeRecursive(SemaphoreHandle_t, TickType_t) { return 1; }
inline int xSemaphoreGiveRecursive(SemaphoreHandle_t) { return 1; }
