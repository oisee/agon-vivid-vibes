/*
 * bbplay — standalone bytebeat music player
 *
 * Usage: bbplay <1-4>
 *   1: JS-256 engine — base-36 melody with 4-tap echo
 *   2: t*((t&4096?6:16)+(1&t>>14))>>(3&t>>8)|t>>(t&4096?3:4)
 *   3: t*(t>>(t&4096?t*t>>12:t>>12))|t<<(t>>8)|t>>4
 *   4: BPM=160 arpeggiator — 8-step pitch sequence with XOR rhythm
 *
 * Space to stop. All computed on eZ80 at runtime, 8kHz playback.
 */

#include <stdint.h>
#include <stdlib.h>
#include <agon/mos.h>
#include <agon/vdp.h>

#define BB_SAMPLES 65536

static uint8_t buf[65536];

/* ---- JS-256 engine (Song 1) ---- */

static const char js_melody[] = "99C9E9GECCGCJCGC77B7C7EC5595C5CB";

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
    return (uint8_t)(env >> 6);
}

static void gen_song1(void)
{
    for (uint24_t t = 0; t < BB_SAMPLES; t++) {
        int r = (int)((t * 213) >> 8);
        uint8_t val = js_voice(t, r) +
                      (js_voice(t, r - 12288) >> 1) +
                      (js_voice(t, r - 24576) >> 2) +
                      (js_voice(t, r - 36864) >> 3);
        buf[t] = val ^ 0x80;
    }
}

/* ---- Song 2 ---- */

static void gen_song2(void)
{
    for (uint24_t t = 0; t < BB_SAMPLES; t++) {
        uint24_t mul = ((t & 4096) ? 6 : 16) + (1 & (t >> 14));
        uint24_t sh = 3 & (t >> 8);
        uint24_t rs = (t & 4096) ? 3 : 4;
        buf[t] = (uint8_t)(((t * mul) >> sh) | (t >> rs)) ^ 0x80;
    }
}

/* ---- Song 3 ---- */

static void gen_song3(void)
{
    for (uint24_t t = 0; t < BB_SAMPLES; t++) {
        uint24_t s = (t & 4096) ? ((t * t) >> 12) : (t >> 12);
        buf[t] = (uint8_t)((t * (t >> s)) | (t << (t >> 8)) | (t >> 4)) ^ 0x80;
    }
}

/* ---- Song 4: BPM=160 arpeggiator ---- */
/*
 * Bpm=160, Hz=44100 (adapted to 8kHz)
 * tf = t * 11                    (≈ t/Hz/180*3*32768*Bpm)
 * pitch  = [1,2,3,4,4.5,4.75,6,8][(tf>>14)%8]   — 8.8 fixed
 * pitch2 = [4,4.5,4.75,5.333][(tf>>18)%4] / 8    — 8.8 fixed
 * speed  = 2
 * denom  = (tf/2 & 0xFFFF) | ((tf/64 ^ tf/128) & 0xFFFF)
 * pulse  = (saw * 256 / denom) & 1
 * output = pulse*128 + bass/2
 */

/* pitch * 256 (8.8 fixed point) */
static const uint16_t bpm_pitch[8] = {
    256, 512, 768, 1024, 1152, 1216, 1536, 2048
};

/* pitch2 * 256, already /8 applied: [0.5, 0.5625, 0.59375, 0.6667]*256 */
static const uint8_t bpm_pitch2[4] = { 128, 144, 152, 171 };

static void gen_song4(void)
{
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
        buf[t] = (uint8_t)(main_val + (bass >> 1)) ^ 0x80;
    }
}

/* ---- playback ---- */

int main(int argc, char *argv[])
{
    if (argc < 2) {
        mos_puts("Usage: bbplay <1-4>\r\n", 21, 0);
        return 1;
    }

    int song = atoi(argv[1]);
    if (song < 1 || song > 4) {
        mos_puts("Song 1-4\r\n", 10, 0);
        return 1;
    }

    mos_puts("Computing...\r\n", 14, 0);

    switch (song) {
        case 1: gen_song1(); break;
        case 2: gen_song2(); break;
        case 3: gen_song3(); break;
        case 4: gen_song4(); break;
    }

    vdp_audio_load_sample(-1, BB_SAMPLES, buf);
    vdp_audio_set_sample_repeat_start(-1, 0);
    vdp_audio_set_sample_repeat_length(-1, BB_SAMPLES);
    vdp_audio_sample_rate(0, 8000);
    vdp_audio_set_waveform(0, -1);
    vdp_audio_play_note(0, 127, 0, -1);

    mos_puts("Playing... Space to stop.\r\n", 26, 0);

    while (getsysvar_keyascii() != ' ')
        waitvblank();

    vdp_audio_set_volume(0, 0);
    vdp_audio_reset_channel(0);

    return 0;
}
