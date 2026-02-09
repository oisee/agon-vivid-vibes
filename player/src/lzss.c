#include "lzss.h"

#define LZSS_MIN_MATCH 3

uint24_t lzss_decompress(const uint8_t *src, uint24_t src_len,
                         uint8_t *dst, uint24_t dst_size)
{
    uint24_t si = 0;  /* source index */
    uint24_t di = 0;  /* destination index */

    while (si < src_len && di < dst_size) {
        uint8_t flags = src[si++];

        for (uint8_t bit = 0; bit < 8; bit++) {
            if (si >= src_len || di >= dst_size)
                break;

            if (flags & (1 << bit)) {
                /* Match */
                uint8_t b0 = src[si++];
                uint8_t b1 = src[si++];
                uint16_t offset = ((uint16_t)(b1 & 0xF0) << 4) | b0;
                offset += 1;
                uint8_t length = (b1 & 0x0F) + LZSS_MIN_MATCH;

                uint24_t copy_from = di - offset;
                for (uint8_t j = 0; j < length && di < dst_size; j++) {
                    dst[di++] = dst[copy_from + j];
                }
            } else {
                /* Literal */
                dst[di++] = src[si++];
            }
        }
    }

    return di;
}
