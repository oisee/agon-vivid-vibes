# Bad Apple Frame Diff Analysis

**Date:** 2026-02-12
**Tool:** `tools/badapple_diff_analysis.py`
**Parameters:** 256x192, 8x8 tiles, 256-tile codebook, 6572 frames

## Frame-to-Frame Tile Changes

After encoding all frames with the 256-tile codebook (nearest-match for non-codebook tiles), measuring how many of the 768 tile positions change between consecutive frames:

| Stat | Tiles Changed | % of Frame |
|---|---|---|
| Min | 0 | 0% |
| P20 | 41 | 5.3% |
| **Median** | **81** | **10.5%** |
| Mean | 90.4 | 11.8% |
| P80 | 137 | 17.8% |
| P95 | 194 | 25.3% |
| P99 | 252 | 32.8% |
| Max | 768 | 100% |

**~88% of tiles remain unchanged per frame on average.** Delta encoding is extremely effective.

### Distribution

```
  0 tiles changed:    190 frames ( 2.9%)  ← duplicate frames
  1- 10 changed:      116 frames ( 1.8%)  ← near-static
 11- 30 changed:      558 frames ( 8.5%)  ← slow movement
 31- 60 changed:    1,363 frames (20.7%)  ← moderate animation
 61-100 changed:    1,897 frames (28.9%)  ← typical animation  ← BULK
101-200 changed:    2,186 frames (33.3%)  ← fast animation     ← BULK
201-400 changed:      258 frames ( 3.9%)  ← transitions
401-768 changed:        3 frames ( 0.0%)  ← scene wipes
```

The distribution is concentrated in 31-200 tiles changed (83% of frames). Very few frames are near-static or full-wipe.

## Encoding Cost Comparison

### Method A: Full Tilemap + LZSS
- 768 bytes per frame (raw), rely on LZSS to compress unchanged tiles (zeros after XOR)
- Simple but wasteful — sends 768 bytes even when only 81 changed

### Method B: Sparse Changes — 3 bytes per change
- `[position_lo, position_hi, tile_id]` per changed tile
- Mean: 90 × 3 = **271 B/frame**
- Best for frames with few changes (<48)

### Method C: Change Bitmap + Tile IDs
- 96-byte bitmap (768 bits, one per tile position) + 1 byte tile ID per changed tile
- Mean: 96 + 90 = **186 B/frame**
- Better than sparse when >48 tiles change (**75% of frames**)

### Method D: Hybrid (best per frame)
Pick whichever of sparse/bitmap is smaller for each frame:

| Stat | Bytes/Frame |
|---|---|
| Mean | **177 B** |
| Median | **178 B** |
| P80 | **234 B** |
| Total (all diffs) | **1,134 KB** |

## Keyframe Analysis

### Natural Scene Changes

| Threshold | Frames | % |
|---|---|---|
| >30% tiles changed | 113 | 1.7% |
| >50% tiles changed | 5 | 0.1% |
| 100% (full wipe) | 3 | 0.05% |

Scene changes are rare and clustered in bursts. The 113 ">30%" frames occur in groups (fast animation sequences like frames 357-361, 478-482, 1509-1514). Gap between scene changes: min=1, max=722, mean=45 frames.

### Periodic Keyframe Cost

A keyframe is simply a full tilemap: 768 bytes.

| Keyframe Interval | # Keyframes | Key Cost | Diff Cost | Total |
|---|---|---|---|---|
| Every 30 frames | 220 | 165 KB | 1,134 KB | 1,299 KB |
| Every 60 frames | 110 | 82 KB | 1,134 KB | 1,217 KB |
| Every 120 frames | 55 | 41 KB | 1,134 KB | 1,175 KB |
| Every 300 frames | 22 | 16 KB | 1,134 KB | 1,151 KB |
| Every 600 frames | 11 | 8 KB | 1,134 KB | 1,142 KB |

**Keyframes are cheap** — even every 30 frames (1 second) adds only 165 KB. The diff stream dominates.

**Recommendation:** Keyframe every 120-300 frames (4-10 seconds), plus forced keyframes at the 113 natural scene change positions. Total overhead: <50 KB.

## Total Size Estimates

| Component | Size |
|---|---|
| Tile codebook (256 × 8B) | 2 KB |
| Diff stream (hybrid encoding) | 1,134 KB |
| Keyframes (every 120 frames) | 41 KB |
| **Pre-LZSS total** | **~1,177 KB** |
| **After LZSS (est. 30% ratio)** | **~350-400 KB** |

### Comparison

| Approach | Total Size |
|---|---|
| TurBoss/agon-badapple (uncompressed ASCII) | 4,180 KB |
| Our tile approach (raw) | 1,177 KB |
| Our tile approach (LZSS compressed) | ~350-400 KB |
| cnlohr/badderapple (VPX range coding, 64x48) | 55 KB |

We achieve ~10x compression over the naive Agon port at **6x the resolution** (256x192 vs 44x30).

## Conclusions

1. **Delta encoding is essential.** 88% of tiles unchanged per frame — only ~90 tiles change on average out of 768.

2. **Change bitmap encoding wins.** For 75% of frames, a 96-byte bitmap + tile IDs beats sparse coordinate encoding. The hybrid (pick smaller per frame) averages 177 B/frame.

3. **Keyframes are almost free.** At 768 B each, even frequent keyframes (every 2-4 seconds) add minimal overhead. They enable seeking and error recovery.

4. **The estimated ~350-400 KB compressed total fits easily** in Agon's memory:
   - VDP PSRAM: 4 MB (could hold all decoded frames as buffers)
   - eZ80 RAM: 512 KB (could stream from SD)
   - SD card: unlimited

5. **For quality-critical regions:** The 7.8% of tiles not in the codebook could use a per-scene supplementary codebook or be stored as raw 8-byte tile data with an escape code. This adds ~3% overhead for pixel-perfect edges.

## Next Steps

- Build actual encoder pipeline and measure real LZSS compression ratio
- Implement VDP tile rendering (bitmap mode or per-tile buffer calls)
- Test on emulator at 30fps with VDP buffered commands
- Consider adaptive codebook: scene-specific supplementary tiles
