// Firmware tag and partition-table hash for OTA (docs/protocol.md §14). Pure C++, no Arduino dependencies:
// shared by both sketches and checked on the PC (firmware/tools/ota_test). The server does the same in Python
// (services/firmware.py): it reads the tag from the uploaded image and hashes the zip's partitions.bin.
#pragma once
#include <stddef.h>
#include <stdint.h>
#include <string.h>

namespace otatag {

// "PMFWTAG|<type>|<version>|<hw>|" is a constant in every firmware image (ota.cpp). Scanning for it must not find
// the scanner's own copy of the marker, so the marker is kept XOR-ed here and decoded at run time.
static const uint8_t KEY = 0x5A;
static const uint8_t MARK[8] = {'P' ^ KEY, 'M' ^ KEY, 'F' ^ KEY, 'W' ^ KEY, 'T' ^ KEY, 'A' ^ KEY, 'G' ^ KEY, '|' ^ KEY};
static const size_t FIELD_MAX = 40;

// Streaming scanner: feed() the image chunk by chunk (any chunk size); found() when a valid tag has passed.
struct Scan {
  char type[FIELD_MAX], ver[FIELD_MAX], hw[FIELD_MAX];
  bool done = false;
  // state: 0..7 matching the marker, then field text; a tag is valid when 3 non-empty printable fields end with '|'
  uint8_t match = 0;
  uint8_t field = 0;      // 0 = not in a tag, 1..3 = type / ver / hw
  size_t len = 0;
  char buf[FIELD_MAX];

  void reset() { done = false; match = 0; field = 0; len = 0; type[0] = ver[0] = hw[0] = 0; }
  Scan() { reset(); }

  void feed(const uint8_t *p, size_t n) {
    for (size_t i = 0; i < n && !done; i++) {
      uint8_t c = p[i];
      if (field == 0) {
        if (c == (uint8_t)(MARK[match] ^ KEY)) {
          if (++match == sizeof(MARK)) { match = 0; field = 1; len = 0; }
        } else {
          match = (c == (uint8_t)(MARK[0] ^ KEY)) ? 1 : 0;  // the marker has no repeated letters: restart is enough
        }
        continue;
      }
      if (c == '|') {
        if (len == 0) { field = 0; continue; }  // empty field: not a tag
        buf[len] = 0;
        char *dst = field == 1 ? type : field == 2 ? ver : hw;
        memcpy(dst, buf, len + 1);
        len = 0;
        if (++field == 4) { done = true; field = 0; }
      } else if (c < 0x21 || c > 0x7e || len >= FIELD_MAX - 1) {
        field = 0; match = 0; len = 0;  // not printable / too long: not a tag
      } else {
        buf[len++] = (char)c;
      }
    }
  }
  bool found() const { return done; }
};

// zlib-compatible CRC-32 (the server uses zlib.crc32).
inline uint32_t crc32(uint32_t crc, const uint8_t *p, size_t n) {
  crc = ~crc;
  while (n--) {
    crc ^= *p++;
    for (int k = 0; k < 8; k++) crc = (crc >> 1) ^ (0xEDB88320u & (0u - (crc & 1u)));
  }
  return ~crc;
}

// Hash of a partition table: for every entry in table order: type, subtype, offset (u32 LE), size (u32 LE).
struct Layout {
  uint32_t crc = 0;
  void add(uint8_t type, uint8_t subtype, uint32_t offset, uint32_t size) {
    uint8_t b[10] = {type, subtype, (uint8_t)offset, (uint8_t)(offset >> 8), (uint8_t)(offset >> 16), (uint8_t)(offset >> 24),
                     (uint8_t)size, (uint8_t)(size >> 8), (uint8_t)(size >> 16), (uint8_t)(size >> 24)};
    crc = crc32(crc, b, sizeof(b));
  }
};

}  // namespace otatag
