#ifndef LZSS_H
#define LZSS_H

#include <stdint.h>

/*
 * LZSS decompressor.
 *
 * Format: tokens in groups of 8, each group preceded by a flag byte (LSB first).
 *   Flag bit 0 = literal byte
 *   Flag bit 1 = match: [offset_lo] [(offset_hi<<4) | (length-3)]
 *     offset = 12 bits (1..4096), length = 4 bits (3..18)
 *
 * Returns number of bytes written to dst.
 */
uint24_t lzss_decompress(const uint8_t *src, uint24_t src_len,
                         uint8_t *dst, uint24_t dst_size);

#endif
