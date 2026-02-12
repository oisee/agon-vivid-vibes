/*
 * demo player — sequential cube/torus switcher with 2 bytebeat songs
 *
 * Song 1: JS-256 engine — base-36 melody with echo (computed via LUT)
 * Song 2: BPM=160 arpeggiator with XOR rhythm gate
 * All computed on the eZ80 at runtime, uploaded to VDP as samples.
 * Frames pre-loaded into VDP buffers for 9 bytes/frame playback.
 *
 * Song cycle: 1-1-2-2 repeat. Space to exit.
 */

#include <stdint.h>
#include <agon/mos.h>
#include <agon/vdp.h>
#include "lzss.h"

extern uint8_t setup_data[];
extern uint8_t setup_data_end[];
extern uint8_t cube_compressed[];
extern uint8_t cube_compressed_end[];
extern uint8_t torus_compressed[];
extern uint8_t torus_compressed_end[];

#define BB_SAMPLES 65536
#define NUM_SONGS  2

static uint8_t decomp_buf[131072];
static uint8_t vdu_buf[1536];
static uint8_t swap_cmd[] = { 23, 0, 0xC3 };

/* ---- JS-256 engine tables ---- */

/* r=t/1.205, a=r=>(t*2^(parseInt(melody[(r>>13)%32],36)/12)/2.67%128
   +(r>>6&127)&128)*(1-r%8192/8192),
   a(r)+a(r-d)/2+a(r-2d)/4+a(r-3d)/8  where d=12288 */

static const char js_melody[] = "99C9E9GECCGCJCGC77B7C7EC5595C5CB";

/* 2^(n/12) / 2.67 * 128, for n=0..19 */
static const uint8_t js_freq[20] = {
    48, 51, 54, 57, 60, 64, 68, 72, 76, 81,
    85, 90, 96, 102, 108, 114, 121, 128, 136, 144
};

static uint8_t js_b36(char c)
{
    return (c <= '9') ? (uint8_t)(c - '0') : (uint8_t)(c - 'A' + 10);
}

static uint8_t js_voice(uint24_t t, int r)
{
    if (r < 0) return 0;

    uint8_t n = js_b36(js_melody[((unsigned)r >> 13) & 31]);
    uint8_t saw = (uint8_t)(((uint24_t)t * js_freq[n]) >> 7) & 127;
    uint8_t sub = ((unsigned)r >> 6) & 127;

    if (!((saw + sub) & 128))
        return 0;

    uint16_t env = 8192 - ((unsigned)r & 8191);
    return (uint8_t)(env >> 6);  /* 0..128 */
}

/* ---- end JS-256 ---- */

/* ---- BPM=160 arpeggiator tables (Song 4) ---- */

/* pitch * 256 (8.8 fixed): [1, 2, 3, 4, 4.5, 4.75, 6, 8] */
static const uint16_t bpm_pitch[8] = {
    256, 512, 768, 1024, 1152, 1216, 1536, 2048
};

/* (pitch2 / 8) * 256 (8.8 fixed): [0.5, 0.5625, 0.59375, 0.6667] */
static const uint8_t bpm_pitch2[4] = { 128, 144, 152, 171 };

static uint16_t read_u16(const uint8_t *p)
{
    return p[0] | ((uint16_t)p[1] << 8);
}

static uint24_t decompress(const uint8_t *src, uint24_t src_len, uint8_t *dst)
{
    uint24_t decomp_size = src[0] |
                           ((uint24_t)src[1] << 8) |
                           ((uint24_t)src[2] << 16);
    lzss_decompress(src + 3, src_len - 3, dst, decomp_size);
    return decomp_size;
}

static void init_bytebeat(void)
{
    /* Song 1: JS-256 — base-36 melody with echo */
    for (uint24_t t = 0; t < BB_SAMPLES; t++) {
        int r = (int)((t * 213) >> 8);   /* t / 1.205 */
        uint8_t val = js_voice(t, r) +
                      (js_voice(t, r - 12288) >> 1) +
                      (js_voice(t, r - 24576) >> 2) +
                      (js_voice(t, r - 36864) >> 3);
        decomp_buf[t] = val ^ 0x80;
    }
    vdp_audio_load_sample(-1, BB_SAMPLES, decomp_buf);

    /* Song 2: BPM=160 arpeggiator — 8-step pitch + XOR rhythm gate */
    for (uint24_t t = 0; t < BB_SAMPLES; t++) {
        uint24_t tf = t * 11;
        uint16_t pitch_fp = bpm_pitch[(tf >> 14) & 7];
        uint8_t pitch2_fp = bpm_pitch2[(tf >> 18) & 3];
        uint8_t saw = (uint8_t)((t * pitch_fp) >> 8);
        uint24_t half_tf = tf >> 1;
        uint16_t a = (uint16_t)(half_tf & 0xFFFF);
        uint16_t b = (uint16_t)(((half_tf >> 5) ^ (half_tf >> 6)) & 0xFFFF);
        uint16_t denom = a | b;
        if (denom == 0) denom = 1;
        uint24_t pulse = ((uint24_t)saw << 8) / denom;
        uint8_t main_val = (pulse & 1) ? 128 : 0;
        uint8_t bass = (uint8_t)((t * (uint24_t)pitch2_fp) >> 8);
        decomp_buf[t] = (uint8_t)(main_val + (bass >> 1)) ^ 0x80;
    }
    vdp_audio_load_sample(-2, BB_SAMPLES, decomp_buf);

    for (int i = -1; i >= -NUM_SONGS; i--) {
        vdp_audio_set_sample_repeat_start(i, 0);
        vdp_audio_set_sample_repeat_length(i, BB_SAMPLES);
    }
    vdp_audio_sample_rate(0, 8000);
}

static void play_song(int sample)
{
    vdp_audio_set_waveform(0, sample);
    vdp_audio_play_note(0, 127, 0, -1);
}

/* Upload all frames of an effect as VDP buffered commands.
 * Each frame becomes VDP buffer base_id+0 .. base_id+N-1.
 * Returns number of frames. */
static uint16_t upload_effect(uint8_t *data, uint16_t base_id)
{
    uint8_t *d = data;
    uint16_t num_frames = read_u16(d); d += 2;
    uint8_t  max_tris   = *d++;

    uint8_t *tri_counts = d;  d += num_frames;

    uint16_t col_len = (uint16_t)max_tris * num_frames;
    uint8_t *col_colors = d;   d += col_len;
    uint8_t *col_x1_lo  = d;   d += col_len;
    uint8_t *col_x1_hi  = d;   d += col_len;
    uint8_t *col_y1     = d;   d += col_len;
    uint8_t *col_x2_lo  = d;   d += col_len;
    uint8_t *col_x2_hi  = d;   d += col_len;
    uint8_t *col_y2     = d;   d += col_len;
    uint8_t *col_x3_lo  = d;   d += col_len;
    uint8_t *col_x3_hi  = d;   d += col_len;
    uint8_t *col_y3     = d;

    for (uint16_t f = 0; f < num_frames; f++) {
        uint8_t ntris = tri_counts[f];
        /* Build VDU payload at offset 8, leaving room for buffer header */
        uint8_t *vp = vdu_buf + 8;

        *vp++ = 16;  /* CLG */

        for (uint8_t s = 0; s < ntris; s++) {
            uint16_t idx = (uint16_t)s * num_frames + f;

            *vp++ = 18; *vp++ = 0; *vp++ = col_colors[idx];

            *vp++ = 25; *vp++ = 4;
            *vp++ = col_x1_lo[idx]; *vp++ = col_x1_hi[idx];
            *vp++ = col_y1[idx];    *vp++ = 0;

            *vp++ = 25; *vp++ = 4;
            *vp++ = col_x2_lo[idx]; *vp++ = col_x2_hi[idx];
            *vp++ = col_y2[idx];    *vp++ = 0;

            *vp++ = 25; *vp++ = 85;
            *vp++ = col_x3_lo[idx]; *vp++ = col_x3_hi[idx];
            *vp++ = col_y3[idx];    *vp++ = 0;
        }

        uint16_t payload_len = (uint16_t)(vp - (vdu_buf + 8));
        uint16_t id = base_id + f;

        /* VDU 23, 0, &A0, id_lo, id_hi, 0, len_lo, len_hi, <payload> */
        vdu_buf[0] = 23;
        vdu_buf[1] = 0;
        vdu_buf[2] = 0xA0;
        vdu_buf[3] = (uint8_t)(id & 0xFF);
        vdu_buf[4] = (uint8_t)(id >> 8);
        vdu_buf[5] = 0;   /* command 0: write to buffer */
        vdu_buf[6] = (uint8_t)(payload_len & 0xFF);
        vdu_buf[7] = (uint8_t)(payload_len >> 8);

        mos_puts((char *)vdu_buf, (uint24_t)(vp - vdu_buf), 0);
    }
    return num_frames;
}

/* Play an effect by calling pre-uploaded VDP buffers.
 * 9 bytes/frame (call + swap) instead of ~1.5KB streamed. */
static uint8_t play_effect(uint16_t base_id, uint16_t num_frames, uint8_t cycles)
{
    uint8_t call_cmd[6] = { 23, 0, 0xA0, 0, 0, 1 };

    for (uint8_t cycle = 0; cycle < cycles; cycle++) {
        for (uint16_t f = 0; f < num_frames; f++) {
            uint16_t id = base_id + f;
            call_cmd[3] = (uint8_t)(id & 0xFF);
            call_cmd[4] = (uint8_t)(id >> 8);

            mos_puts((char *)call_cmd, 6, 0);
            mos_puts((char *)swap_cmd, 3, 0);
            waitvblank();

            if (getsysvar_keyascii() == ' ')
                return 1;
        }
    }
    return 0;
}

static void clear_vdp_buffers(void)
{
    /* VDU 23, 0, &A0, &FF, &FF, 2 — clear ALL VDP buffers */
    uint8_t cmd[] = { 23, 0, 0xA0, 0xFF, 0xFF, 2 };
    mos_puts((char *)cmd, 6, 0);
}

int main(void)
{
    /* Clear any stale VDP buffers from a previous run */
    clear_vdp_buffers();

    /* Sound disabled for debugging */

    mos_puts("vivid: decompressing frames...\r\n", 31, 0);

    /* Decompress frame data into decomp_buf */
    uint24_t cube_size = decompress(cube_compressed,
                                    (uint24_t)(cube_compressed_end - cube_compressed),
                                    decomp_buf);
    uint8_t *torus_data = decomp_buf + cube_size;
    decompress(torus_compressed,
               (uint24_t)(torus_compressed_end - torus_compressed),
               torus_data);

    mos_puts("vivid: uploading to VDP...\r\n", 28, 0);

    /* Upload all frames as VDP buffers — one-time cost over UART.
     * After this, playback is just 9 bytes/frame (call + swap). */
    uint16_t cube_nframes  = upload_effect(decomp_buf, 1);
    uint16_t torus_nframes = upload_effect(torus_data, 1000);

    /* Now switch to graphics mode */
    uint24_t setup_len = (uint24_t)(setup_data_end - setup_data);
    mos_puts((char *)setup_data, setup_len, 0);
    for (uint8_t i = 0; i < 5; i++)
        waitvblank();

    for (;;) {
        for (uint8_t song = 0; song < NUM_SONGS; song++) {
            play_song(-(song + 1));
            if (play_effect(1, cube_nframes, 2))
                goto done;
            if (play_effect(1000, torus_nframes, 2))
                goto done;
        }
    }
done:
    vdp_audio_set_volume(0, 0);
    vdp_audio_reset_channel(0);
    clear_vdp_buffers();
    vdp_mode(0);
    vdp_cursor_enable(1);
    return 0;
}
