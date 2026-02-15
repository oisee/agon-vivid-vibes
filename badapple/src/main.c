/*
 * Bad Apple BA2S streaming player
 *
 * Reads badapple.ba2s from SD card, uploads tile bitmaps to VDP PSRAM,
 * then decodes LZSS+Huffman compressed tile data per GOP and streams
 * VDU draw commands to the VDP each frame.
 *
 * Press ESC to exit.
 */

#include <stdint.h>
#include <string.h>
#include <agon/mos.h>
#include <agon/vdp.h>

/* Fast UART write — bypasses mos_puts, fills 16-byte FIFO in batches */
extern void fast_vdu(char *data, int len);

static uint8_t use_fast_vdu = 0;
static uint8_t use_charprint = 0;

static void vdp_write(const uint8_t *data, uint16_t len) {
    if (use_fast_vdu)
        fast_vdu((char *)data, len);
    else
        mos_puts((char *)data, len, 0);
}
#define VDP_WRITE(buf, len) vdp_write((const uint8_t *)(buf), (len))

/* ── BA2S header ─────────────────────────────────────────────── */

typedef struct {
    uint8_t  grid_w;
    uint8_t  grid_h;
    uint16_t num_tiles;
    uint16_t num_frames;
    uint8_t  fps;
    uint16_t gop_size;
    uint8_t  num_gops;
} BA2S_Header;

/* ── Huffman types ───────────────────────────────────────────── */

typedef struct {
    int16_t children[2];   /* child indices, negative = none */
    int16_t symbol;        /* -1 = internal, 0-255 = leaf */
} HuffNode;

typedef struct {
    const uint8_t *data;
    uint32_t data_len;
    uint32_t byte_pos;
    uint8_t  bit_pos;      /* 7..0 within current byte, MSB-first */
} BitReader;

/* ── Static buffers ──────────────────────────────────────────── */

#define MASK_BUF_SIZE  45000   /* decompressed masks for one GOP */
#define COMP_BUF_SIZE  65536   /* compressed data read buffer */
#define VDU_BUF_SIZE   16384   /* VDU output for one frame (keyframe: ~13KB) */
#define HUFF_MAX_NODES 512
#define READ_CHUNK     4096    /* file I/O chunk size */
#define BAR_WIDTH      20
#define DEBUG_FRAMES   1       /* show frame counter in corner */
#define FRAME_BASE_ID  1000    /* VDP buffer IDs for frame data */

static uint8_t  mask_buf[MASK_BUF_SIZE];
static uint8_t  comp_buf[COMP_BUF_SIZE];
static uint8_t  tiles[2][1200];
static uint8_t  vdu_buf[VDU_BUF_SIZE];
static HuffNode huff_tree[HUFF_MAX_NODES];
static uint16_t huff_num_nodes;
static uint16_t huff_root;

/* Small buffer for setup VDU */
static uint8_t  setup_buf[32];
static uint16_t setup_len;

/* ── Helpers ─────────────────────────────────────────────────── */

static uint16_t read_u16(const uint8_t *p) {
    return p[0] | ((uint16_t)p[1] << 8);
}

static uint32_t read_u32(const uint8_t *p) {
    return p[0] | ((uint32_t)p[1] << 8) |
           ((uint32_t)p[2] << 16) | ((uint32_t)p[3] << 24);
}

static void put_num(uint16_t n) {
    char digits[6];
    int d = 0;
    do { digits[d++] = '0' + (n % 10); n /= 10; } while (n);
    while (d--) putch(digits[d]);
}

static void print_progress(const char *label, uint16_t current, uint16_t total) {
    putch('\r');
    while (*label) putch(*label++);
    putch(' ');
    putch('[');
    uint16_t filled = (uint16_t)((uint32_t)current * BAR_WIDTH / total);
    for (uint8_t i = 0; i < BAR_WIDTH; i++)
        putch(i < filled ? '#' : '.');
    putch(']');
    putch(' ');
    put_num(current);
    putch('/');
    put_num(total);
    putch(' ');
}

/* ── LZSS decompressor ───────────────────────────────────────── */

static uint32_t lzss_decompress(const uint8_t *src, uint32_t src_len,
                                 uint8_t *dst, uint32_t dst_max)
{
    uint32_t sp = 0, dp = 0;
    while (dp < dst_max && sp < src_len) {
        uint8_t flag = src[sp++];
        for (uint8_t bit = 0; bit < 8 && dp < dst_max && sp < src_len; bit++) {
            if (flag & (1 << bit)) {
                /* literal */
                dst[dp++] = src[sp++];
            } else {
                /* match: offset-1 stored, length-3 stored */
                uint16_t offset = (uint16_t)src[sp++] + 1;
                uint16_t length = (uint16_t)src[sp++] + 3;
                uint32_t start = dp - offset;
                for (uint16_t j = 0; j < length && dp < dst_max; j++)
                    dst[dp++] = dst[start + j];
            }
        }
    }
    return dp;
}

/* ── Huffman tree builder ────────────────────────────────────── */

static uint16_t huff_alloc_node(void) {
    uint16_t idx = huff_num_nodes++;
    huff_tree[idx].children[0] = -1;
    huff_tree[idx].children[1] = -1;
    huff_tree[idx].symbol = -1;
    return idx;
}

static void huffman_build_tree(const uint8_t *table, uint16_t table_len) {
    huff_num_nodes = 0;
    huff_root = huff_alloc_node();

    if (table_len == 0 || table[0] == 0)
        return;

    uint8_t max_len = table[0];
    uint16_t pos = 1;

    /* Read counts per length */
    uint8_t counts[33];
    for (uint8_t l = 1; l <= max_len; l++)
        counts[l] = table[pos++];

    /* Reconstruct canonical codes and insert into tree */
    uint32_t code = 0;
    uint8_t prev_len = 0;
    uint8_t first = 1;

    for (uint8_t l = 1; l <= max_len; l++) {
        for (uint8_t c = 0; c < counts[l]; c++) {
            uint8_t sym = table[pos++];

            if (!first) {
                code++;
                code <<= (l - prev_len);
            }
            first = 0;
            prev_len = l;

            /* Insert into tree by walking bits MSB-first */
            uint16_t node = huff_root;
            for (int8_t i = l - 1; i >= 0; i--) {
                uint8_t bit = (code >> i) & 1;
                if (huff_tree[node].children[bit] < 0)
                    huff_tree[node].children[bit] = (int16_t)huff_alloc_node();
                node = (uint16_t)huff_tree[node].children[bit];
            }
            huff_tree[node].symbol = (int16_t)sym;
        }
    }
}

/* ── Huffman bit reader & decoder ────────────────────────────── */

static void bitreader_init(BitReader *br, const uint8_t *data, uint32_t len) {
    br->data = data;
    br->data_len = len;
    br->byte_pos = 0;
    br->bit_pos = 7;  /* MSB first */
}

static uint8_t huffman_decode_one(BitReader *br) {
    uint16_t node = huff_root;
    while (huff_tree[node].symbol < 0) {
        uint8_t bit = (br->data[br->byte_pos] >> br->bit_pos) & 1;
        if (br->bit_pos == 0) {
            br->bit_pos = 7;
            br->byte_pos++;
        } else {
            br->bit_pos--;
        }
        node = (uint16_t)huff_tree[node].children[bit];
    }
    return (uint8_t)huff_tree[node].symbol;
}

/* ── File reading helpers ────────────────────────────────────── */

/* Read exactly n bytes from file, using comp_buf as intermediate if needed.
 * For large reads, reads in chunks into the target buffer directly. */
static void file_read(uint8_t fh, uint8_t *dst, uint32_t n) {
    while (n > 0) {
        uint24_t chunk = (n > READ_CHUNK) ? READ_CHUNK : (uint24_t)n;
        mos_fread(fh, (char *)dst, chunk);
        dst += chunk;
        n -= chunk;
    }
}

/* Read large compressed data that may exceed comp_buf into comp_buf
 * (up to COMP_BUF_SIZE). Returns actual bytes read. */
static uint32_t file_read_to_comp(uint8_t fh, uint32_t n) {
    if (n > COMP_BUF_SIZE) n = COMP_BUF_SIZE;
    file_read(fh, comp_buf, n);
    return n;
}

/* ── Main ────────────────────────────────────────────────────── */

static uint8_t streq(const char *a, const char *b) {
    while (*a && *b) { if (*a++ != *b++) return 0; }
    return *a == *b;
}

static void put_centiseconds(uint32_t cs) {
    /* Print as seconds with 2 decimal places: "12.34s" */
    uint32_t sec = cs / 100;
    uint8_t  frac = (uint8_t)(cs % 100);
    put_num((uint16_t)sec);
    putch('.');
    if (frac < 10) putch('0');
    put_num(frac);
    putch('s');
}

int main(int argc, char *argv[]) {
    uint8_t fh;
    BA2S_Header hdr;
    uint8_t header_buf[16];
    uint16_t tiles_per_frame, mask_bytes_per_frame;
    uint16_t block_len;

    /* Parse args */
    uint16_t max_frames = 0;  /* 0 = no limit */
    uint8_t  speed_mode = 0;  /* 0=normal, 1=max(60fps), 2=nosync */
    const char *filename = "badapple.ba2s";
    for (int i = 1; i < argc; i++) {
        if (streq(argv[i], "-f") || streq(argv[i], "--fast-vdu")) {
            use_fast_vdu = 1;
        } else if (streq(argv[i], "-m") || streq(argv[i], "--max")) {
            speed_mode = 1;
        } else if (streq(argv[i], "-n") || streq(argv[i], "--nosync")) {
            speed_mode = 2;
        } else if (argv[i][0] >= '1' && argv[i][0] <= '9') {
            uint16_t n = 0;
            for (const char *p = argv[i]; *p >= '0' && *p <= '9'; p++)
                n = n * 10 + (*p - '0');
            max_frames = n;
        } else {
            filename = argv[i];
        }
    }

    /* 1. Open data file */
    fh = mos_fopen(filename, FA_READ);
    if (!fh) {
        mos_puts("Error: ", 7, 0);
        mos_puts((char *)filename, strlen(filename), 0);
        mos_puts(" not found\r\n", 12, 0);
        return 1;
    }

    /* 2. Read 16-byte header */
    mos_fread(fh, (char *)header_buf, 16);
    if (header_buf[0] != 'B' || header_buf[1] != 'A' ||
        header_buf[2] != '2' || header_buf[3] != 'S') {
        mos_puts("Error: bad BA2S magic\r\n", 22, 0);
        mos_fclose(fh);
        return 1;
    }

    hdr.grid_w     = header_buf[5];
    hdr.grid_h     = header_buf[6];
    hdr.num_tiles  = header_buf[7] ? header_buf[7] : 256;
    hdr.num_frames = read_u16(header_buf + 8);
    hdr.fps        = header_buf[10];
    hdr.gop_size   = read_u16(header_buf + 11);
    hdr.num_gops   = header_buf[13];
    use_charprint  = header_buf[14] & 0x01;

    tiles_per_frame = (uint16_t)hdr.grid_w * hdr.grid_h;
    mask_bytes_per_frame = (tiles_per_frame + 7) / 8;

    /* Apply frame limit */
    uint16_t play_frames = hdr.num_frames;
    if (max_frames > 0 && max_frames < play_frames)
        play_frames = max_frames;

    /* Print info */
    mos_puts("BA2S: ", 6, 0);
    put_num(play_frames);
    putch('/');
    put_num(hdr.num_frames);
    mos_puts(" frames, ", 9, 0);
    put_num(hdr.num_gops);
    mos_puts(" GOPs", 5, 0);
    if (use_charprint)
        mos_puts(" [charprint]", 12, 0);
    mos_puts("\r\n", 2, 0);

    /* 3. Read setup VDU block (sent later before playback) */
    mos_fread(fh, (char *)header_buf, 2);
    setup_len = read_u16(header_buf);
    mos_fread(fh, (char *)setup_buf, setup_len);

    /* 4. Upload tile bitmaps to VDP PSRAM */
    uint32_t t_start = getsysvar_time();
    for (uint16_t i = 0; i < hdr.num_tiles; i++) {
        mos_fread(fh, (char *)header_buf, 2);
        block_len = read_u16(header_buf);

        /* Bitmap blocks always fit in comp_buf */
        mos_fread(fh, (char *)comp_buf, block_len);
        VDP_WRITE(comp_buf, block_len);

        if ((i & 0x0F) == 0 || i == hdr.num_tiles - 1)
            print_progress("Bitmaps", i + 1, hdr.num_tiles);
    }
    {
        uint32_t t_bitmaps = getsysvar_time() - t_start;
        putch('\n');
        mos_puts("  Bitmaps: ", 11, 0);
        put_centiseconds(t_bitmaps);
        mos_puts("\r\n", 2, 0);
    }

    /* 5. Read Huffman table, build decode tree */
    mos_fread(fh, (char *)header_buf, 2);
    uint16_t huff_table_len = read_u16(header_buf);
    mos_fread(fh, (char *)comp_buf, huff_table_len);
    huffman_build_tree(comp_buf, huff_table_len);

    mos_puts("Huffman tree: ", 14, 0);
    put_num(huff_num_nodes);
    mos_puts(" nodes\r\n", 8, 0);

    /* ── Phase 1: Decode BA2S and upload frame buffers ─────────── */
    t_start = getsysvar_time();

    uint16_t total_frames_played = 0;

    for (uint8_t gop = 0; gop < hdr.num_gops && total_frames_played < play_frames; gop++) {
        uint8_t gop_hdr[10];
        uint16_t gop_frames;
        uint32_t mask_comp_len, id_comp_len, id_count;

        /* Read GOP header: gop_frames + mask_comp_size */
        mos_fread(fh, (char *)gop_hdr, 6);
        gop_frames    = read_u16(gop_hdr);
        mask_comp_len = read_u32(gop_hdr + 2);

        /* Read compressed masks, decompress into mask_buf */
        file_read_to_comp(fh, mask_comp_len);
        uint32_t mask_total = (uint32_t)gop_frames * mask_bytes_per_frame;
        lzss_decompress(comp_buf, mask_comp_len, mask_buf, mask_total);

        /* Read GOP ID header: id_comp_size + id_count */
        mos_fread(fh, (char *)gop_hdr, 8);
        id_comp_len = read_u32(gop_hdr);
        id_count    = read_u32(gop_hdr + 4);

        /* Read compressed IDs into comp_buf */
        file_read_to_comp(fh, id_comp_len);

        /* Init Huffman bit reader */
        BitReader br;
        bitreader_init(&br, comp_buf, id_comp_len);

        /* Decode each frame, build VDU commands, upload as buffer */
        for (uint16_t fi = 0; fi < gop_frames; fi++) {
            uint16_t frame_idx = total_frames_played + fi;

            /* Stop if we've hit the frame limit */
            if (frame_idx >= play_frames)
                break;
            uint8_t  buf_idx = fi & 1;
            uint8_t *cur_tiles = tiles[buf_idx];
            uint32_t mask_off = (uint32_t)fi * mask_bytes_per_frame;
            uint16_t vdu_pos = 0;

            /* First two global frames: keyframes from zeros + CLG */
            if (frame_idx < 2) {
                memset(cur_tiles, 0, tiles_per_frame);
                vdu_buf[vdu_pos++] = 16;  /* VDU 16 = CLG */
            }

            /* Apply mask + decode IDs, build VDU tile commands */
            if (use_charprint) {
                /* Charprint: scan row by row, find runs, emit MOVE + chars */
                for (uint8_t row = 0; row < hdr.grid_h; row++) {
                    uint16_t row_off = (uint16_t)row * hdr.grid_w;
                    int16_t run_start = -1;
                    for (uint8_t col = 0; col <= hdr.grid_w; col++) {
                        uint16_t pos = row_off + col;
                        uint8_t changed = 0;
                        if (col < hdr.grid_w) {
                            uint16_t byte_idx = (uint16_t)(mask_off + (pos >> 3));
                            changed = mask_buf[byte_idx] & (1 << (pos & 7));
                        }
                        if (changed) {
                            uint8_t tile_id = huffman_decode_one(&br);
                            cur_tiles[pos] = tile_id;
                            if (run_start < 0) {
                                /* Start new run: MOVE to (col*8, row*8) */
                                uint16_t x = (uint16_t)col * 8;
                                uint16_t y = (uint16_t)row * 8;
                                vdu_buf[vdu_pos++] = 25;  /* VDU 25 = PLOT */
                                vdu_buf[vdu_pos++] = 4;   /* MOVE absolute */
                                vdu_buf[vdu_pos++] = (uint8_t)(x & 0xFF);
                                vdu_buf[vdu_pos++] = (uint8_t)(x >> 8);
                                vdu_buf[vdu_pos++] = (uint8_t)(y & 0xFF);
                                vdu_buf[vdu_pos++] = (uint8_t)(y >> 8);
                                run_start = col;
                            }
                            /* Emit character byte (tile_id is already 32-255) */
                            vdu_buf[vdu_pos++] = tile_id;
                        } else {
                            run_start = -1;
                        }
                    }
                }
            } else {
                /* Legacy bitmap mode: select + draw per tile */
                uint8_t last_tile_id = 0xFF;
                for (uint16_t pos = 0; pos < tiles_per_frame; pos++) {
                    uint16_t byte_idx = (uint16_t)(mask_off + (pos >> 3));
                    if (!(mask_buf[byte_idx] & (1 << (pos & 7))))
                        continue;

                    uint8_t tile_id = huffman_decode_one(&br);
                    cur_tiles[pos] = tile_id;

                    uint16_t tx = pos % hdr.grid_w;
                    uint16_t ty = pos / hdr.grid_w;
                    uint16_t x = tx * 8;
                    uint16_t y = ty * 8;

                    /* Select bitmap if different from last */
                    if (tile_id != last_tile_id) {
                        vdu_buf[vdu_pos++] = 23;
                        vdu_buf[vdu_pos++] = 27;
                        vdu_buf[vdu_pos++] = 0;
                        vdu_buf[vdu_pos++] = tile_id;
                        last_tile_id = tile_id;
                    }

                    /* Draw bitmap at (x, y) */
                    vdu_buf[vdu_pos++] = 23;
                    vdu_buf[vdu_pos++] = 27;
                    vdu_buf[vdu_pos++] = 3;
                    vdu_buf[vdu_pos++] = (uint8_t)(x & 0xFF);
                    vdu_buf[vdu_pos++] = (uint8_t)(x >> 8);
                    vdu_buf[vdu_pos++] = (uint8_t)(y & 0xFF);
                    vdu_buf[vdu_pos++] = (uint8_t)(y >> 8);
                }
            }

            /* Append double-buffer swap inside the buffer */
            vdu_buf[vdu_pos++] = 23;
            vdu_buf[vdu_pos++] = 0;
            vdu_buf[vdu_pos++] = 0xC3;

            /* Upload as VDP buffer: cmd 0 = write/create */
            {
                uint16_t buf_id = FRAME_BASE_ID + frame_idx;
                uint8_t upload_hdr[8] = {
                    23, 0, 0xA0,
                    (uint8_t)(buf_id & 0xFF), (uint8_t)(buf_id >> 8),
                    0,  /* command 0 = write */
                    (uint8_t)(vdu_pos & 0xFF), (uint8_t)(vdu_pos >> 8)
                };
                VDP_WRITE(upload_hdr, 8);
                VDP_WRITE(vdu_buf, vdu_pos);
            }

            if ((frame_idx & 0x0F) == 0 || frame_idx == play_frames - 1)
                print_progress("Frames", frame_idx + 1, play_frames);

            if (getsysvar_keyascii() == 0x1B) {
                play_frames = frame_idx + 1;  /* play only what we uploaded */
                break;
            }
        }

        if (getsysvar_keyascii() == 0x1B)
            break;
        total_frames_played += gop_frames;
    }
    {
        uint32_t t_frames = getsysvar_time() - t_start;
        putch('\n');
        mos_puts("  Frames:  ", 11, 0);
        put_centiseconds(t_frames);
        putch(' ');
        put_num(play_frames);
        mos_puts(" uploaded\r\n", 11, 0);
    }

    mos_fclose(fh);

    if (play_frames == 0) goto cleanup;

    /* ── Phase 2: Switch to double-buffered mode, start playback ── */
    VDP_WRITE(setup_buf, setup_len);
    for (uint8_t i = 0; i < 60; i++)  /* ~1s pause for mode to settle */
        waitvblank();

    /* Loop animation until ESC */
    uint8_t vblanks_per_frame;
    if (speed_mode == 2)
        vblanks_per_frame = 0;  /* nosync */
    else if (speed_mode == 1)
        vblanks_per_frame = 1;  /* max 60fps */
    else
        vblanks_per_frame = (hdr.fps > 0 && hdr.fps <= 60) ? (60 / hdr.fps) : 1;

    uint8_t esc = 0;
    while (!esc) {
        uint32_t t_loop = getsysvar_time();
        for (uint16_t f = 0; f < play_frames; f++) {
            uint16_t buf_id = FRAME_BASE_ID + f;
            uint8_t call_cmd[6] = {
                23, 0, 0xA0,
                (uint8_t)(buf_id & 0xFF), (uint8_t)(buf_id >> 8),
                1   /* command 1 = call/execute buffer */
            };
            VDP_WRITE(call_cmd, 6);
            for (uint8_t v = 0; v < vblanks_per_frame; v++)
                waitvblank();

            if (getsysvar_keyascii() == 0x1B) {
                esc = 1;
                break;
            }
        }
        if (!esc && speed_mode) {
            /* Show loop timing */
            uint32_t t_elapsed = getsysvar_time() - t_loop;
            mos_puts("\r\nLoop: ", 8, 0);
            put_centiseconds(t_elapsed);
            mos_puts(" (", 2, 0);
            put_num((uint16_t)((uint32_t)play_frames * 100 / t_elapsed));
            mos_puts("fps)\r\n", 6, 0);
        }
    }

cleanup:
    /* Clear all VDP buffers */
    {
        uint8_t clear[] = { 23, 0, 0xA0, 0xFF, 0xFF, 2 };
        VDP_WRITE(clear, 6);
    }
    vdp_mode(0);
    vdp_cursor_enable(1);

    return 0;
}
