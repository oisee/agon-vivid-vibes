# Bad Apple Implementations — Research Report

**Date:** 2026-02-12
**Context:** Evaluating existing Bad Apple ports for techniques applicable to Agon Light 2

## 1. TurBoss/agon-badapple — Agon Light port (z88dk)

**Repo:** https://github.com/TurBoss/agon-badapple

### Resolution: 44x30 pixels (extremely low-res)

Each "pixel" is a single native pixel in Mode 2 (640x480 monochrome), rendered into the upper-left corner. A `PIXEL_SIZE=12` constant exists but is never used — the image is literally 44x30 dots on a 640x480 screen.

### Compression: None

- Frames stored as **plain ASCII** text file `badapple.txt` — **4.18 MB** on SD card
- Each pixel is `'0'` or `'1'` character, 30 lines per frame, `*` delimiter between frames
- 3,245 frames at ~30fps = ~108 seconds
- Read line-by-line with `fgets()`

### Display: z88dk plot()/unplot() per pixel

- Uses `plot(x,y)` / `unplot(x,y)` from z88dk — each emits a VDU 25 (PLOT) command over UART
- **One good optimization:** delta rendering — maintains `displayBuffer` and `prevDisplayBuffer`, only calls plot/unplot when a pixel changes
- No frame pacing (no vsync/delay), no double buffering (Mode 2 not Mode 130)
- Speed entirely limited by SD I/O + per-pixel VDU overhead

### Verdict

Minimal proof-of-concept. 4 MB uncompressed ASCII, per-pixel plotting, tiny image. Everything can be improved upon.

---

## 2. cnlohr/badderapple — 65KB Extreme Compression

**Repo:** https://github.com/cnlohr/badderapple

### Target: CH32V006 RISC-V MCU ($0.10, 62KB flash, 8KB RAM)

**64x48 pixels, 3 grey levels, 30fps, 6570 frames** — entire video + audio + code in **62,976 bytes**.

### Data Budget

| Component | Size |
|---|---|
| Video stream | 54,492 B (**76 bits/frame!**) |
| Glyph codebook (256 8x8 tiles) | 2,509 B |
| Probability tables | 5,107 B |
| Song data | 673 B |
| Code | ~1,200 B |

### Tile-Based Architecture

- Screen divided into **8x8 tiles** → 8x6 = 48 tile positions per frame
- **256 unique tiles** selected via K-means clustering + PyTorch ML optimization (LPIPS perceptual loss)
- Each frame is 48 tile IDs, delta-encoded ("did this tile change?")

### Compression Techniques

1. **VPX Range Coding** (from VP8/VP9) — arithmetic coding where each bit has a known probability. Good predictions cost nearly zero bits. Decoder is ~364 bytes + 256-byte LUT. Approaches theoretical entropy limit.

2. **Delta encoding** — each cell: 1 VPX-coded bit "same as before?" ~85% of cells don't change → nearly free.

3. **Per-tile-class probability trees** — 13 tile classes from K-means clustering. Probability of change depends on class + how many frames since last change. Stored as `ba_vpx_probs_by_tile_run_continuous[13][128]`.

4. **Glyph ID encoding** — when a tile changes, new ID encoded MSB-first using class-conditioned probability tree `ba_chancetable_glyph_dual[13][255]`.

5. **Reverse LZSS** for song — backtracks in input stream (not output) to save RAM.

6. **H.264-style deblocking filter** — blurs tile boundaries. Implemented as bit-parallel SIMD on 32-bit RISC-V via Karnaugh map minimization.

### Glyph Codebook Generation

Two approaches combined:
- **K-means:** Start with 2048 random tiles, iteratively assign 8x8 blocks to nearest tile, compute centroids, cull least-used, repeat until 256 remain.
- **ML (PyTorch):** Differentiable rendering with Gumbel-Softmax for discrete tile selection, LPIPS perceptual loss, optical flow regularization for temporal stability.

### Key Insight

At 76 bits/frame, this approaches the theoretical entropy limit. H.265 (HEVC) — the result of millions in R&D — was tested and could not beat ~64 KB at comparable quality on this content.

---

## 3. Timendus/chip-8-bad-apple — Multi-Strategy Competition

**Repo:** https://github.com/Timendus/chip-8-bad-apple

### Resolution

| Mode | Resolution | FPS | Coverage |
|------|-----------|-----|----------|
| Lores | 48x32 | 10 | Full 3:39 video |
| Hires | 96x64 | 30 | ~16 seconds only |

1-bit black/white. Full video fits in ~60KB.

### Key Architecture: Compete and Pick Smallest

The encoder tries **25 combinations** of encoding techniques per frame and picks whichever produces the smallest output:

**Building blocks:**
- **XOR Delta (`diff`)** — frame XOR'd against current display buffer. The single most important technique — since consecutive frames mostly overlap, the diff is mostly zeros.
- **Bounding Box (`bbox`)** — only encode the changed rectangular region. 2 header bytes for min/max coordinates.
- **Global Huffman (`globalHuffman`)** — single codebook built from all frames. Max 16-bit codewords. Wins ~95% of frames.
- **RLE** — repeat/literal byte runs (MSB flag, 127 max run).
- **Interlacing** — even/odd rows alternate per frame, interpolate missing lines. Halves data.
- **Lossy diff reduction (`reduce-diff`)** — randomly zero out diff bytes with ≤2 set bits (~33% of diffs). Artifacts cleaned up by subsequent frames within 1-3 frames. The critical final hack that made it fit.
- **Frame skipping** — identical frames encoded as repeat count (1 byte).

### Per-Frame Header Byte

| Bit | Meaning |
|-----|---------|
| 7 | Diff mode (1=XOR against display, 0=clear first) |
| 6 | RLE compressed |
| 5 | Huffman compressed |
| 4 | Bounding box (2 extra header bytes follow) |
| 3 | Interlaced |
| 2 | Odd row (interlacing) |
| 1 | Repeat frame (1 byte count follows) |
| 0 | Scroll instruction follows |

### Memory Layout (XO-CHIP, ~64KB total)

| Region | Size | Contents |
|--------|------|----------|
| Code | 179 B | Main loop |
| Music player | 503 B | XO-Tracker |
| Video decoder | 100 B | All decoders |
| Music data | 1,980 B | Song |
| **Video data** | **61,754 B** | Frames + Huffman codebook |

### CHIP-8 XOR Advantage

CHIP-8's native `sprite` instruction XORs pixels onto the screen — delta frames work without explicit XOR logic in the decoder.

---

## Comparison Table

| | agon-badapple | badderapple | chip-8-bad-apple |
|---|---|---|---|
| Resolution | 44x30 | 64x48 | 48x32 / 96x64 |
| Colors | 1-bit | 3 grey | 1-bit |
| FPS | uncontrolled | 30 | 10 / 30 |
| Video data | 4.18 MB (!) | 54 KB | 60 KB |
| Compression | none | VPX range + tiles + delta | XOR delta + Huffman + bbox |
| Delta encoding | pixel-level redraw | tile-level | frame-level XOR |
| Tile-based | no | yes (256 8x8) | no |
| Audio | none | triangle wave + LFSR | XO-Tracker |
| Platform | Agon (z88dk) | CH32V006 RISC-V | XO-CHIP |
| Total binary | ~4.19 MB | 63 KB | 61-65 KB |

## Applicable Techniques for Agon Vivid Vibes

1. **Tile codebook** (cnlohr) — build 256+ 8x8 binary tiles from actual video content. Encode each screen position as 1-byte tile index.
2. **XOR delta** (CHIP-8) — encode frame-to-frame differences, not absolute frames. Most tiles don't change.
3. **Bounding box** (CHIP-8) — skip encoding unchanged regions entirely.
4. **Compete encoders** (CHIP-8) — try multiple strategies per frame, pick smallest.
5. **Our existing LZSS** works well for post-delta data — no need for VPX range coding complexity.
6. **VDP buffered commands** — already in our pipeline. Upload tile codebook as VDP buffers, reference by ID.
