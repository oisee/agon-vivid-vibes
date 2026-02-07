/*
 * Vivid Vibes for Agon Light
 * A demoscene production inspired by github.com/oisee/vivid-vibes
 *
 * Compile with AgDev: make
 *
 * Effects:
 *   1. Starfield - hyperspace warp
 *   2. Copper Bars - Amiga style oscillating bars
 *   3. Plasma - text mode color plasma
 */

#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <mos_api.h>
#include <agon/vdp_vdu.h>
#include <agon/vdp_key.h>

/* Screen dimensions */
#define SCREEN_W 320
#define SCREEN_H 240
#define TEXT_COLS 40
#define TEXT_ROWS 30

/* Sine table (256 entries, 0-255 range) */
static uint8_t sintab[256];

/* System variables pointer */
static volatile SYSVAR *sv;

/* Initialize sine table using parabolic approximation */
static void init_sintab(void) {
    int i;
    for (i = 0; i < 256; i++) {
        int s;
        /* Better sine approximation */
        if (i < 64) {
            s = 128 + i * 2;
        } else if (i < 128) {
            s = 128 + (128 - i) * 2;
        } else if (i < 192) {
            s = 128 - (i - 128) * 2;
        } else {
            s = 128 - (256 - i) * 2;
        }
        sintab[i] = (uint8_t)(s > 255 ? 255 : (s < 0 ? 0 : s));
    }
}

/* Check for keypress (non-blocking) */
static int key_pressed(void) {
    return (sv->vkeycount != 0 && sv->vkeydown);
}

/* Wait for key release */
static void wait_key_release(void) {
    while (sv->vkeydown) {
        /* wait */
    }
}

/* Short delay */
static void delay(int count) {
    volatile int i, j;
    for (i = 0; i < count; i++) {
        for (j = 0; j < 100; j++);
    }
}

/* ============================================================
 * STARFIELD EFFECT
 * ============================================================ */

#define STAR_COUNT 80
#define STAR_SPEED 6

typedef struct {
    int16_t x;      /* X offset from center */
    int16_t y;      /* Y offset from center */
    uint8_t z;      /* Depth (1-255, lower = closer) */
} Star;

static Star stars[STAR_COUNT];

static void starfield_init(void) {
    int i;
    srand(12345);  /* Fixed seed for reproducibility */
    for (i = 0; i < STAR_COUNT; i++) {
        stars[i].x = (rand() % SCREEN_W) - SCREEN_W/2;
        stars[i].y = (rand() % SCREEN_H) - SCREEN_H/2;
        stars[i].z = (rand() % 255) + 1;
    }
}

static void starfield_frame(void) {
    int i;
    int16_t cx = SCREEN_W/2, cy = SCREEN_H/2;
    int16_t sx, sy, scale;
    uint8_t bright;

    for (i = 0; i < STAR_COUNT; i++) {
        /* Erase old position */
        if (stars[i].z < 255) {
            scale = 256 / (stars[i].z + 1);
            sx = cx + (stars[i].x * scale) / 32;
            sy = cy + (stars[i].y * scale) / 32;
            if (sx >= 0 && sx < SCREEN_W && sy >= 0 && sy < SCREEN_H) {
                vdp_gcol(0, 0);  /* Black */
                vdp_point(sx, sy);
            }
        }

        /* Move star closer */
        if (stars[i].z <= STAR_SPEED) {
            /* Reset star */
            stars[i].x = (rand() % SCREEN_W) - SCREEN_W/2;
            stars[i].y = (rand() % SCREEN_H) - SCREEN_H/2;
            stars[i].z = 255;
        } else {
            stars[i].z -= STAR_SPEED;
        }

        /* Draw new position */
        scale = 256 / (stars[i].z + 1);
        sx = cx + (stars[i].x * scale) / 32;
        sy = cy + (stars[i].y * scale) / 32;

        if (sx >= 0 && sx < SCREEN_W && sy >= 0 && sy < SCREEN_H) {
            /* Brightness based on depth */
            bright = 63 - (stars[i].z >> 2);
            if (bright > 63) bright = 63;
            vdp_gcol(0, bright);
            vdp_point(sx, sy);
        }
    }
}

/* ============================================================
 * COPPER BARS EFFECT
 * ============================================================ */

#define BAR_COUNT   6
#define BAR_HEIGHT  4

static uint8_t copper_time = 0;

static void copper_frame(void) {
    int bar, row;
    uint8_t phase, ybase, y, hue, bright, col;
    int seg, r, g, b;

    /* Clear screen with black */
    vdp_set_text_colour(128);  /* Black background */
    vdp_cls();

    for (bar = 0; bar < BAR_COUNT; bar++) {
        /* Calculate bar Y position (oscillating) */
        phase = (bar * 256 / BAR_COUNT + copper_time * 4) & 0xFF;
        ybase = 15 + ((sintab[phase] - 128) * 10) / 128;

        /* Bar hue based on bar number + time */
        hue = (bar * 12 + copper_time) & 63;

        /* Draw bar rows */
        for (row = 0; row < BAR_HEIGHT; row++) {
            y = ybase + row;
            if (y >= 0 && y < TEXT_ROWS) {
                /* Brightness gradient (brightest in middle) */
                bright = BAR_HEIGHT / 2;
                if (row < BAR_HEIGHT / 2) {
                    bright = row + 1;
                } else {
                    bright = BAR_HEIGHT - row;
                }

                /* Convert hue to RRGGBB (simplified HSV) */
                seg = hue / 11;
                if (seg > 5) seg = 5;

                switch (seg) {
                    case 0: r = 3; g = (hue % 11) * 3 / 11; b = 0; break;
                    case 1: r = 3 - (hue % 11) * 3 / 11; g = 3; b = 0; break;
                    case 2: r = 0; g = 3; b = (hue % 11) * 3 / 11; break;
                    case 3: r = 0; g = 3 - (hue % 11) * 3 / 11; b = 3; break;
                    case 4: r = (hue % 11) * 3 / 11; g = 0; b = 3; break;
                    default: r = 3; g = 0; b = 3 - (hue % 11) * 3 / 11; break;
                }

                /* Apply brightness */
                r = (r * bright * 2) / BAR_HEIGHT;
                g = (g * bright * 2) / BAR_HEIGHT;
                b = (b * bright * 2) / BAR_HEIGHT;
                if (r > 3) r = 3;
                if (g > 3) g = 3;
                if (b > 3) b = 3;

                col = (r << 4) | (g << 2) | b;

                /* Draw full row */
                vdp_cursor_tab(0, y);
                vdp_set_text_colour(128 + col);
                printf("%80s", " ");  /* 80 spaces = full row in mode 3 */
            }
        }
    }

    copper_time++;
}

/* ============================================================
 * PLASMA EFFECT (Text Mode)
 * ============================================================ */

#define PLASMA_W TEXT_COLS
#define PLASMA_H TEXT_ROWS

static uint8_t plasma_dist[PLASMA_W * PLASMA_H];
static uint8_t plasma_time = 0;

static void plasma_init(void) {
    int x, y, dx, dy;
    int cx = PLASMA_W / 2;
    int cy = PLASMA_H / 2;

    /* Precompute distance table for radial wave */
    for (y = 0; y < PLASMA_H; y++) {
        for (x = 0; x < PLASMA_W; x++) {
            dx = x - cx;
            dy = y - cy;
            /* Fast integer sqrt approximation */
            int dist = dx * dx + dy * dy;
            int r = 0;
            while (r * r < dist) r++;
            plasma_dist[y * PLASMA_W + x] = (r * 6) & 0xFF;
        }
    }
}

static void plasma_frame(void) {
    int x, y, p;
    uint8_t v1, v2, v3, v4, col;

    p = 0;
    for (y = 0; y < PLASMA_H; y++) {
        vdp_cursor_tab(0, y);
        for (x = 0; x < PLASMA_W; x++) {
            /* Four overlapping waves */
            v1 = sintab[(x * 6 + plasma_time) & 0xFF] >> 2;
            v2 = sintab[(y * 8 + plasma_time * 2) & 0xFF] >> 2;
            v3 = sintab[((x + y) * 4 + plasma_time) & 0xFF] >> 2;
            v4 = sintab[(plasma_dist[p] + plasma_time) & 0xFF] >> 2;

            /* Average waves to get color */
            col = ((v1 + v2 + v3 + v4) >> 2) & 63;

            /* Set background color and print space */
            vdp_set_text_colour(128 + col);
            putch(' ');
            p++;
        }
    }

    plasma_time += 2;
}

/* ============================================================
 * TITLE DISPLAY
 * ============================================================ */

static void show_title(const char *text) {
    int len = strlen(text);
    vdp_set_text_colour(63);  /* White text */
    vdp_cursor_tab((TEXT_COLS - len) / 2, 1);
    printf("%s", text);
}

static void show_centered(int y, int col, const char *text) {
    int len = strlen(text);
    vdp_set_text_colour(col);
    vdp_cursor_tab((TEXT_COLS - len) / 2, y);
    printf("%s", text);
}

/* ============================================================
 * MAIN DEMO
 * ============================================================ */

int main(int argc, char *argv[]) {
    int frame;

    (void)argc;
    (void)argv;

    /* Initialize VDP and get system variables */
    sv = vdp_vdu_init();

    /* Initialize sine table */
    init_sintab();

    /* === INTRO === */
    vdp_mode(8);  /* 320x240, 64 colors */
    vdp_cursor_enable(false);
    vdp_cls();

    /* White background */
    vdp_set_text_colour(128 + 63);
    for (frame = 0; frame < TEXT_ROWS; frame++) {
        vdp_cursor_tab(0, frame);
        printf("%40s", " ");
    }

    /* Title text */
    vdp_set_text_colour(0);  /* Black text */
    show_centered(12, 0, "VIVID VIBES");
    show_centered(14, 0, "for Agon Light");
    show_centered(18, 0, "github.com/oisee");

    /* Wait a bit */
    for (frame = 0; frame < 150; frame++) {
        delay(100);
        if (key_pressed()) goto end;
    }

    /* === STARFIELD === */
    vdp_mode(8);
    vdp_cursor_enable(false);
    vdp_cls();
    show_title("HYPERSPACE");
    starfield_init();

    for (frame = 0; frame < 250; frame++) {
        starfield_frame();
        if (key_pressed()) goto end;
    }

    /* === COPPER BARS === */
    vdp_mode(3);  /* 640x240, 64 colors, 80x30 text */
    vdp_cursor_enable(false);
    vdp_cls();

    for (frame = 0; frame < 200; frame++) {
        copper_frame();
        vdp_set_text_colour(63);
        vdp_cursor_tab(32, 1);
        printf("COPPER BARS");
        if (key_pressed()) goto end;
    }

    /* === PLASMA === */
    vdp_mode(8);
    vdp_cursor_enable(false);
    vdp_cls();
    plasma_init();

    for (frame = 0; frame < 200; frame++) {
        plasma_frame();
        show_title("PLASMA");
        if (key_pressed()) goto end;
    }

    /* === OUTRO === */
    vdp_mode(8);
    vdp_cursor_enable(false);
    vdp_cls();

    show_centered(12, 63, "THE END");
    show_centered(16, 42, "Thanks for watching!");
    show_centered(20, 21, "Press any key...");

    /* Wait for keypress */
    wait_key_release();
    while (!key_pressed()) {
        delay(10);
    }

end:
    vdp_cursor_enable(true);
    vdp_mode(0);
    printf("Vivid Vibes - Agon Light Demo\n\r");
    printf("Inspired by github.com/oisee/vivid-vibes\n\r");

    return 0;
}
