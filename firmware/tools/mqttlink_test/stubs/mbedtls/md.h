// mbedtls HMAC-SHA256 calls mapped onto OpenSSL (PC test only).
#pragma once
#include <openssl/evp.h>
#include <openssl/hmac.h>
#include <string>
enum { MBEDTLS_MD_SHA256 = 1 };
struct mbedtls_md_info_t {};
struct mbedtls_md_context_t { std::string key, data; };
inline const mbedtls_md_info_t *mbedtls_md_info_from_type(int) { static mbedtls_md_info_t i; return &i; }
inline void mbedtls_md_init(mbedtls_md_context_t *) {}
inline int mbedtls_md_setup(mbedtls_md_context_t *, const mbedtls_md_info_t *, int) { return 0; }
inline int mbedtls_md_hmac_starts(mbedtls_md_context_t *c, const unsigned char *k, size_t n) { c->key.assign((const char *)k, n); return 0; }
inline int mbedtls_md_hmac_update(mbedtls_md_context_t *c, const unsigned char *d, size_t n) { c->data.append((const char *)d, n); return 0; }
inline int mbedtls_md_hmac_finish(mbedtls_md_context_t *c, unsigned char *out) {
  unsigned int len = 32;
  HMAC(EVP_sha256(), c->key.data(), (int)c->key.size(), (const unsigned char *)c->data.data(), c->data.size(), out, &len);
  return 0;
}
inline void mbedtls_md_free(mbedtls_md_context_t *) {}
