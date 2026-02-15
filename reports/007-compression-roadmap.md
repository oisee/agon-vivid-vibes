# 007: Bad Apple Compression Roadmap

## Context

Current architecture: all 1000 frames pre-built as VDP buffer commands,
uploaded to 8MB PSRAM, VDP plays autonomously via VSYNC callbacks.
Total PSRAM: ~1.1MB. Works but leaves no room for more frames or higher res.

## Current Frame Format (VDP Buffers)

Each frame buffer contains full VDU commands:
```
For each changed tile:
  VDU 23, 27, 0, bitmap_id    — 4 bytes (select bitmap)
  VDU 23, 27, 3, x_lo, x_hi, y_lo, y_hi  — 7 bytes (plot at position)
= 11 bytes per changed tile
```
Plus: CLG (1 byte for keyframes), swap (3 bytes), chain (6 bytes).
Actual mean at 1000 frames: **1,345 B/frame** (122 changed tiles avg).

---

## Experimental Results (2026-02-14)

All numbers: 1000 frames, 320×240, merge codebook, 40×30 tile grid.

### Mask Stream Compression

| Strategy | Size | vs Raw |
|----------|------|--------|
| Raw masks (150B/frame) | 150,000 B | baseline |
| RLE per frame | 146,302 B | -2% |
| LZSS per GOP(300) | 67,755 B | -55% |
| XOR + LZSS per GOP | 62,648 B | -58% |
| Z-transposed + LZSS GOP | 57,977 B | **-61%** |
| Temporal bit-pack + LZSS | 58,444 B | **-61%** |
| Z-transposed + zlib | 30,081 B | -80% (theoretical) |
| Temporal bit-pack + zlib | 33,990 B | -77% (theoretical) |

Zero-byte density: 78% (temporal), 68% (raw masks).

### ID Stream Compression

| Strategy | Size | vs Raw |
|----------|------|--------|
| Raw IDs (1 byte each) | 124,018 B | baseline |
| Huffman (6.02 bits/ID entropy) | 94,648 B | **-24%** |
| LZSS per GOP | 111,616 B | -10% |
| RLE per frame | 187,016 B | +51% (worse!) |
| zlib | 87,161 B | -30% (theoretical) |

Top-2 updater tiles: ID 0 (black)=20.4%, ID 1 (white)=20.4% = 40% of all updates.
Shannon entropy: 6.02 bits/ID (vs 8 bits raw).

### Combined Formats (masks + IDs)

| Format | Total | vs VDU | eZ80? |
|--------|-------|--------|-------|
| VDU buffers (current) | 1,315 KB | baseline | yes (dumb pipe) |
| Raw masks + raw IDs | 268 KB | -80% | yes (trivial) |
| **LZSS masks + Huffman IDs** | **159 KB** | **-88%** | **yes** |
| XOR+LZSS masks + Huffman IDs | 154 KB | -88% | yes |
| Z-trans+LZSS masks + Huffman IDs | 149 KB | -89% | yes (complex) |
| Temporal+LZSS masks + Huffman IDs | 150 KB | -89% | yes |
| Dense Z-trans + zlib (no masks) | 119 KB | -91% | no (zlib) |

### Key Findings

1. **Two-stream (LZSS masks + Huffman IDs) = 159 KB = 88% smaller**
2. Huffman beats LZSS for IDs (94KB vs 112KB) — skewed distribution
3. LZSS is best practical compressor for masks (55-61% savings)
4. Temporal bit-packing ≈ Z-transpose for LZSS (both -61%)
5. Codebook sorting: marginal (6%), not worth complexity
6. RLE alone is useless for IDs and barely helps masks

---

## Target Architecture: Two-Stream with GOP

```
badapple.dat file layout:
┌──────────────────────────────────────┐
│ Header (16B)                         │
│   magic "BA2S"                       │
│   num_tiles, num_frames, fps         │
│   gop_size, tile_grid_w, tile_grid_h │
├──────────────────────────────────────┤
│ Huffman Table (≤512B)                │
│   256 entries: symbol → code_len     │
│   (fixed for whole video)            │
├──────────────────────────────────────┤
│ Tile Bitmaps (256 × 264B = ~66KB)   │
│   VDU 23,27,0,N + VDU 23,27,1,8,8,  │
│   RGBA8888 data per tile             │
├──────────────────────────────────────┤
│ GOP 0                                │
│   Mask stream (LZSS compressed)      │
│   ID stream (Huffman coded)          │
├──────────────────────────────────────┤
│ GOP 1                                │
│   ...                                │
├──────────────────────────────────────┤
│ ...                                  │
└──────────────────────────────────────┘
```

### eZ80 Decoder Pipeline (per GOP)

```
1. Read mask stream → LZSS decompress → mask_buf[300][150]
2. Read ID stream → Huffman decode → id_buf[~37K IDs]
3. For each frame in GOP:
     Read 150-byte mask from mask_buf
     For each set bit in mask:
       id = next from id_buf
       Send VDU: select bitmap(id) + plot at (x,y)
     Send VDU: swap double buffer
     Wait VSYNC
```

### Memory Budget (eZ80 = 512KB)

```
Tile state buffer:     1,200 B  (current tilemap, for delta tracking)
Mask buffer (1 GOP):  45,000 B  (300 frames × 150 bytes, pre-decompressed)
ID buffer (1 GOP):    37,000 B  (avg ~124 IDs/frame × 300, pre-decoded)
Read buffer:           8,192 B  (SD card read)
Code + stack:         ~4,000 B
Total:               ~95 KB     ← fits easily in 512KB
```

### Bandwidth Budget (per frame at 30fps)

```
SD card read:     ~159 B/frame (from compressed data)
VDP commands:     ~122 tiles × 11 bytes = ~1,342 B/frame
UART throughput:  115200 baud = ~11,520 B/s = ~384 B/frame at 30fps

PROBLEM: 1,342 B/frame > 384 B/frame UART capacity!
Need to either:
  a) Reduce fps to ~8-10 fps (384×30/1342 ≈ 8.6 fps)
  b) Use VDP buffer pre-upload + VSYNC callbacks (current approach)
  c) Use higher baud rate if available
```

This bandwidth constraint means the **two-stream format is best for SD storage**,
but playback still needs VDP buffer pre-upload for real-time speed.

### Hybrid Architecture

```
Phase 1 (upload):  Read two-stream from SD → decode → build VDP buffers → upload
Phase 2 (play):    Register VSYNC callback → autonomous VDP playback

Benefits:
  - SD file: 159 KB (vs 1.1MB for raw VDP buffers)
  - VDP PSRAM: same as now (~1.1MB) — decoded at upload time
  - Upload time: faster (less SD read)
  - No real-time decoding pressure
```

---

## Implementation Plan

### Phase 1: Two-Stream Encoder (Python) ✅ DONE
- Added `--output-2s` flag to gen_badapple_vdp.py
- Output: BA2S header + setup VDU + tile bitmaps + huffman table + GOP blocks
- Full round-trip verification: LZSS decompress + Huffman decode matches original
- Actual results (1000 frames): masks 150→57KB (62%), IDs 123→91KB (26%)
- Total file: 212KB (84% smaller than 1.3MB VDU buffers)
- Payload only (masks+IDs): 148KB — better than 159KB estimate

### Phase 2: Two-Stream Decoder (eZ80 C)
- LZSS decompressor (~200B code)
- Huffman decoder with lookup table (~300B code)
- GOP loader: decompress → build VDP buffer commands → upload
- Progress bar during upload

### Phase 3: Temporal Bit-Packing (optional)
- Instead of per-frame masks, store 8 frames per byte per tile
- eZ80 reads one byte, gets 8 frames of mask data
- Slightly better compression, simpler random access within GOP

---

## Decision Log

| Date | Decision | Rationale |
|------|----------|-----------|
| 2026-02-13 | Start with 1A + 1B | Zero-risk, measurable, no VDP changes |
| 2026-02-13 | Defer eZ80 decoder | Fix pixel coords issue first |
| 2026-02-13 | Keep VDP buffer arch | Works for 1000 frames, fits in PSRAM |
| 2026-02-14 | Skip codebook sort | Tested: marginal gain (6%), not worth complexity |
| 2026-02-14 | Compact format = 5x win | bitmask+abs IDs: 274B/fr vs 1345B/fr |
| 2026-02-14 | Delta IDs < abs IDs | Large deltas need escapes, 1-byte abs ID optimal |
| 2026-02-14 | Two-stream = 88% saving | LZSS masks + Huffman IDs: 159KB vs 1315KB |
| 2026-02-14 | Temporal ≈ Z-trans for LZSS | Both -61% on masks; temporal simpler to decode |
| 2026-02-14 | Hybrid upload+playback | Decode from SD at upload, play from VDP PSRAM |
| 2026-02-14 | Phase 1 complete | BA2S encoder: 212KB file, 84% smaller, round-trip verified |
