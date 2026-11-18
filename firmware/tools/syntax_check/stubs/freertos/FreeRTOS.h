#pragma once
#include <cstdint>
typedef void *SemaphoreHandle_t;
typedef uint32_t TickType_t;
#define portMAX_DELAY 0xFFFFFFFF
typedef void *TaskHandle_t;
typedef int BaseType_t;
typedef unsigned int UBaseType_t;
#define pdTRUE 1
#define pdFALSE 0
#define pdPASS 1
#define pdMS_TO_TICKS(ms) ((TickType_t)(ms))
#define portYIELD_FROM_ISR()
inline TickType_t xTaskGetTickCount() { return 0; }
inline void vTaskDelay(TickType_t) {}
inline void vTaskDelayUntil(TickType_t *, TickType_t) {}
