# Bad Apple Tile Analysis — 256x192 @ 8x8 Binary Tiles

**Date:** 2026-02-12
**Tool:** `tools/badapple_tile_analysis.py`
**Source:** `assets/badapple-frames/badapple.mp4` (480x360, 30fps, 219s)

## Parameters

| Parameter | Value |
|---|---|
| Target resolution | 256x192 |
| Tile size | 8x8 pixels |
| Tiles per frame | 32 x 24 = **768** |
| Total frames | **6,572** |
| Color depth | 1-bit (threshold at 128) |
| Total tile instances | **5,047,296** |

## Unique Tile Count

**106,754 unique 8x8 binary patterns** found across all frames.

In theory, 2^64 ≈ 1.8×10^19 patterns are possible for an 8x8 binary tile. Bad Apple uses only 106,754 — a tiny fraction, because the video is dominated by large solid regions with clean edges.

## Frequency Distribution

| Codebook Size | Coverage | Uncovered |
|---|---|---|
| Top 2 | **88.4%** | 11.6% |
| Top 10 | **89.0%** | 11.0% |
| Top 64 | **90.6%** | 9.4% |
| Top 128 | **91.4%** | 8.6% |
| Top 256 | **92.2%** | 7.8% |
| Top 512 | **93.2%** | 6.8% |
| Top 1024 | **94.5%** | 5.5% |
| Top 4096 | **96.9%** | 3.1% |

The distribution is **massively skewed**:
- Tile #1 (all-black `........`): **2,445,361 instances (48.5%)**
- Tile #2 (all-white `########`): **2,014,473 instances (39.9%)**
- Together: **88.4%** of all tile instances

The remaining ~12% are edge/transition tiles — various diagonal lines, partial fills, curves.

## Top 20 Tiles

```
#  1 (2,445,361x = 48.45%)  ........    all-black (empty)
#  2 (2,014,473x = 39.91%)  ########    all-white (solid)
#  3 (   4,862x =  0.10%)  .......#    1 pixel top-right corner
#  4 (   4,769x =  0.09%)  #.......    1 pixel bottom-left corner (inverted)
#  5 (   4,368x =  0.09%)  .......#    1 pixel bottom-right corner
#  6 (   4,281x =  0.08%)  .#######    1 pixel missing bottom-left
#  7 (   4,167x =  0.08%)  #######.    1 pixel missing top-right
#  8 (   4,150x =  0.08%)  #.......    1 pixel top-left corner
#  9 (   3,974x =  0.08%)  .#######    1 pixel missing top-left
# 10 (   3,834x =  0.08%)  #######.    1 pixel missing bottom-right
# 11 (   2,310x =  0.05%)  ######..    vertical edge at column 6
# 12 (   2,157x =  0.04%)  ####....    vertical edge at column 4
# 13 (   2,130x =  0.04%)  #######.    vertical edge at column 7
# 14 (   2,107x =  0.04%)  ##......    vertical edge at column 2
# 15-20: various corner and edge transitions
```

**Pattern:** After the two dominant solid tiles, the next most common are **single-pixel corner transitions** (anti-aliasing artifacts from downscaling), followed by **vertical edge tiles** at various column positions. This confirms Bad Apple's high-contrast silhouette nature.

## Codebook Error Analysis

When tiles not in the codebook are replaced by their closest match (by Hamming distance):

| Codebook | Coverage | Substitutions | Avg Error | Worst Error |
|---|---|---|---|---|
| 256 | 92.2% | 395,268 (7.8%) | **4.6 bits/tile** | 27 bits |
| 512 | 93.2% | 343,620 (6.8%) | **4.0 bits/tile** | 26 bits |
| 1024 | 94.5% | 278,341 (5.5%) | **3.5 bits/tile** | 24 bits |

At codebook-256: 7.8% of tiles are substituted with an average of 4.6 wrong pixels out of 64 — that's **7.2% pixel error within affected tiles**, or **0.56% total pixel error** across the whole frame. At 30fps this is imperceptible.

The worst case (27 bits = 42% of a tile wrong) occurs in rare complex tiles. These could be handled with a secondary "important region" encoding if needed.

## Size Estimates

### Raw (no compression)

| Codebook | Bytes/ID | Frame Size | Total Raw | Codebook Size |
|---|---|---|---|---|
| 256 | 1 | 768 B | 4,931 KB | 2 KB |
| 512 | 2 | 1,536 B | 9,862 KB | 4 KB |
| 1,024 | 2 | 1,536 B | 9,866 KB | 8 KB |
| 65,536 | 2 | 1,536 B | 10,370 KB | 512 KB |

### With Delta Encoding + LZSS (estimated)

Assuming ~80% of tiles unchanged per frame (conservative for Bad Apple):

| Codebook | Raw after delta | LZSS estimate |
|---|---|---|
| 256 (1B/id) | ~984 KB | **~295 KB** |
| 512 (2B/id) | ~1,974 KB | ~592 KB |
| 1,024 (2B/id) | ~1,978 KB | ~593 KB |

**The 256-tile / 1-byte codebook is the clear winner:**
- 2 KB codebook + ~295 KB compressed stream ≈ **~297 KB total**
- Fits comfortably in VDP PSRAM (4 MB usable) or eZ80 RAM (512 KB)
- 1 byte per tile position — simplest possible decoder

## Conclusions

1. **256 tiles is sufficient.** 92% exact coverage, with substitution errors averaging only 4.6 pixels per affected tile — visually imperceptible at 30fps.

2. **The distribution is perfect for delta encoding.** Bad Apple has long stretches of static content (silhouettes holding poses) where entire frames of tiles don't change. Delta + LZSS should compress extremely well.

3. **1-byte tile IDs are optimal.** Going to 2-byte IDs doubles frame size for only marginal quality improvement (92.2% → 93.2% at 512 tiles). Not worth it.

4. **Codebook is tiny.** 256 tiles × 8 bytes each = 2,048 bytes. Can be embedded in the binary or uploaded as a single VDP buffer.

5. **For quality-critical regions** (fine detail, small text), a secondary codebook or raw tile escape could handle the 7.8% of substituted tiles with minimal overhead.

## Next Steps

- Build encoder: frame → tile map → delta → LZSS compress
- Build decoder: LZSS decompress → delta reconstruct → tile map → VDP buffer upload
- Explore VDP tile/bitmap modes for more efficient rendering than per-tile buffer calls
- Test actual compression ratio (the 80% delta estimate may be conservative)
