#pragma once
#include "FreeRTOS.h"
inline SemaphoreHandle_t xSemaphoreCreateRecursiveMutex() { return nullptr; }
inline int xSemaphoreTakeRecursive(SemaphoreHandle_t, TickType_t) { return 1; }
inline int xSemaphoreGiveRecursive(SemaphoreHandle_t) { return 1; }
inline SemaphoreHandle_t xSemaphoreCreateMutex() { return nullptr; }
inline SemaphoreHandle_t xSemaphoreCreateBinary() { return nullptr; }
inline int xSemaphoreTake(SemaphoreHandle_t, TickType_t) { return 1; }
inline int xSemaphoreGive(SemaphoreHandle_t) { return 1; }
inline int xSemaphoreGiveFromISR(SemaphoreHandle_t, BaseType_t *) { return 1; }
