// otatag.h on the PC: the firmware tag scanner (any chunking, no false hits) and the partition layout hash
// (the same value the server computes in Python: services/firmware.py, tests compare against these numbers).
#include <cstdio>
#include <cstdlib>
#include <string>
#include <vector>
#include "otatag.h"

static int fails = 0;
#define CHECK(c, what) do { if (!(c)) { printf("FAIL  %s\n", what); fails++; } else printf("ok    %s\n", what); } while (0)

static std::string marker() {  // built from the encoded marker: this file / binary does not contain the literal
  std::string m;
  for (uint8_t b : otatag::MARK) m += (char)(b ^ otatag::KEY);
  return m;
}

static std::vector<uint8_t> image(const std::string &tag, size_t before, size_t after, unsigned seed) {
  std::vector<uint8_t> v;
  srand(seed);
  for (size_t i = 0; i < before; i++) v.push_back((uint8_t)(rand() & 0xff));
  for (char c : tag) v.push_back((uint8_t)c);
  for (size_t i = 0; i < after; i++) v.push_back((uint8_t)(rand() & 0xff));
  return v;
}

static bool scanChunks(const std::vector<uint8_t> &v, size_t chunk, otatag::Scan &s) {
  s.reset();
  for (size_t i = 0; i < v.size(); i += chunk) s.feed(v.data() + i, std::min(chunk, v.size() - i));
  return s.found();
}

int main(int argc, char **argv) {
  std::string tag = marker() + "rbamp-mc|1.3.0|esp32s3-n16r8|";
  auto img = image(tag, 5000, 3000, 1);
  bool all = true;
  for (size_t chunk : {1, 2, 3, 7, 100, 1024, 4096, 100000}) {
    otatag::Scan s;
    bool f = scanChunks(img, chunk, s);
    if (!f || strcmp(s.type, "rbamp-mc") || strcmp(s.ver, "1.3.0") || strcmp(s.hw, "esp32s3-n16r8")) all = false;
  }
  CHECK(all, "the tag is found with every chunk size (1 byte ... whole image)");

  otatag::Scan s;
  CHECK(!scanChunks(image("", 8000, 0, 2), 1024, s), "an image without a tag: not found");
  CHECK(!scanChunks(image(marker() + "x", 100, 100, 3), 64, s), "marker followed by junk: not a tag");
  CHECK(!scanChunks(image(marker() + "|a|b|", 100, 100, 4), 64, s), "empty type field: not a tag");
  std::string longf = marker() + std::string(60, 'a') + "|1|2|";
  CHECK(!scanChunks(image(longf, 100, 100, 5), 64, s), "field longer than 39 characters: not a tag");
  // a damaged marker start inside the data does not hide the real tag
  auto tricky = image("PMFWPMFW" + tag, 100, 100, 6);
  CHECK(scanChunks(tricky, 5, s) && !strcmp(s.type, "rbamp-mc"), "partial marker before the tag");
  // two tags: the first valid one wins
  auto two = image(tag + marker() + "pzem-ac|9|x|", 10, 10, 7);
  CHECK(scanChunks(two, 33, s) && !strcmp(s.type, "rbamp-mc"), "first valid tag wins");

  // this very program must not contain a tag (the scanner's own marker is encoded)
  if (argc > 0) {
    FILE *f = fopen(argv[0], "rb");
    std::vector<uint8_t> self;
    if (f) { uint8_t b[4096]; size_t n; while ((n = fread(b, 1, sizeof b, f)) > 0) self.insert(self.end(), b, b + n); fclose(f); }
    CHECK(!self.empty() && !scanChunks(self, 4096, s), "the scanner binary itself holds no tag");
  }

  CHECK(otatag::crc32(0, (const uint8_t *)"123456789", 9) == 0xCBF43926u, "crc32 = zlib crc32 (check value)");
  // the layout of firmware/PowerMeterRBAmp/partitions.csv in table order (the server test uses the same entries)
  otatag::Layout l;
  l.add(1, 2, 0x9000, 0x5000);      // nvs
  l.add(1, 0, 0xe000, 0x2000);      // otadata
  l.add(0, 0x10, 0x10000, 0x600000);  // app0 (ota_0)
  l.add(0, 0x11, 0x610000, 0x600000); // app1 (ota_1)
  l.add(1, 0x82, 0xC10000, 0x3E0000); // spiffs
  l.add(1, 3, 0xFF0000, 0x10000);   // coredump
  printf("layout %08x\n", (unsigned)l.crc);
  return fails ? 1 : 0;
}
