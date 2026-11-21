#pragma once
#include <cstdint>
#include <cstddef>
typedef enum { ESP_PARTITION_TYPE_APP = 0, ESP_PARTITION_TYPE_DATA = 1, ESP_PARTITION_TYPE_ANY = 0xff } esp_partition_type_t;
typedef enum { ESP_PARTITION_SUBTYPE_ANY = 0xff } esp_partition_subtype_t;
struct esp_partition_t { esp_partition_type_t type; uint8_t subtype; uint32_t address; uint32_t size; char label[17]; };
typedef const void *esp_partition_iterator_t;
inline esp_partition_iterator_t esp_partition_find(esp_partition_type_t, esp_partition_subtype_t, const char *) { return nullptr; }
inline const esp_partition_t *esp_partition_find_first(esp_partition_type_t, esp_partition_subtype_t, const char *) { return nullptr; }
inline const esp_partition_t *esp_partition_get(esp_partition_iterator_t) { return nullptr; }
inline esp_partition_iterator_t esp_partition_next(esp_partition_iterator_t) { return nullptr; }
