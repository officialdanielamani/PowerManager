#pragma once
#include "esp_partition.h"
inline const esp_partition_t *esp_ota_get_next_update_partition(const esp_partition_t *) { return nullptr; }
inline const esp_partition_t *esp_ota_get_running_partition() { return nullptr; }
inline int esp_ota_set_boot_partition(const esp_partition_t *) { return 0; }
