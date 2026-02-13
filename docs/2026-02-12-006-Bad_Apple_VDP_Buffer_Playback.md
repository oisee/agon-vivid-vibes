# Bad Apple — VDP Buffer Autonomous Playback Analysis

**Date:** 2026-02-12
**Tool:** `tools/gen_badapple_vdp.py`
**Parameters:** 320x240 (Mode 8/136), 8x8 tiles, 256-tile codebook, 30fps, 1000 test frames

## Overview

Autonomous VDP playback using buffered commands and VSYNC callback chaining. All frame data is uploaded to VDP PSRAM during initialization — playback requires **zero eZ80 bandwidth**.

## Architecture

### Buffer Layout

| Buffer IDs | Content | Count | Size |
|---|---|---|---|
| 1–256 | Tile bitmaps (8x8, RGBA2222) | 256 | 20.8 KB |
| 1000–1999 | Frame draw commands | 1000 | 1,057 KB |
| **Total** | | **1256** | **1,078 KB (1.1 MB)** |

### Tile Bitmaps

Each 8x8 monochrome tile is stored as a 64-byte RGBA2222 bitmap:
- White pixel: `0xFF` (R=3, G=3, B=3, A=3)
- Black pixel: `0xC0` (R=0, G=0, B=0, A=3)
- Upload: `VDU 23,27,0,n` (select) + `VDU 23,27,1,w;h;data` (upload)
- 256 tiles × (8B header + 64B data) = 18,432B upload payload

### Frame Buffers

Each frame buffer contains VDU commands to draw the frame delta:
1. Select bitmap: `VDU 23,27,0,bitmap_id` (4 bytes)
2. Plot at position: `VDU 23,27,3,x;y;` (7 bytes)
3. Swap double buffer: `VDU 23,0,0xC3` (3 bytes)
4. Register next frame VSYNC: `VDU 23,0,0xA0,id_lo,id_hi,80` (6 bytes)

Tiles are grouped by bitmap ID — select once, plot many positions = fewer select commands.

### Double-Buffer Delta Encoding

Because the VDP uses double buffering (Mode 136), the visible frame alternates between two buffers:
- Frame N draws on the **back buffer**, which shows frame N-2 (not N-1)
- Therefore, delta is computed from frame **N-2** to frame N
- Frames 0 and 1 are full keyframes (CLG + draw all non-black tiles)

## Results (1000 frames, 320x240)

### Frame Buffer Sizes

| Metric | Value |
|---|---|
| Min | 12 B |
| Max | 5,289 B |
| Mean | 1,074 B |
| Median | 982 B |
| P80 | 1,402 B |
| Total | 1,057 KB |

### Delta Changes (from N-2)

| Metric | Tiles Changed | % of 1200 |
|---|---|---|
| Mean | 121 | 10.1% |
| Median | 111 | 9.3% |
| P80 | 165 | 13.8% |

### Memory Budget

| Component | Size |
|---|---|
| Tile bitmaps (256) | 20.8 KB |
| Frame buffers (1000) | 1,057 KB |
| **Total PSRAM** | **1,078 KB (1.1 MB)** |
| VDP PSRAM available | ~4 MB |
| **Utilization** | **26%** |

### Estimated Full Video Capacity

| Metric | Value |
|---|---|
| Bytes/frame (mean) | 1,074 B |
| PSRAM budget (4 MB) | 4,194,304 B |
| Max frames at this rate | **~3,875** |
| Duration at 30fps | **~129 seconds** |
| Full video (6572 frames) | 219 seconds |
| Coverage | **~59%** |

## Playback Mechanism

### Initialization (eZ80 side)

1. Set Mode 136 (320x240, double-buffered)
2. Upload 256 tile bitmaps (buffers 1–256)
3. Upload N frame buffers (buffers 1000–1000+N)
4. Register buffer 1000 as VSYNC callback
5. **Done** — VDP plays autonomously

### Per-Frame Execution (VDP side, autonomous)

Each frame buffer executes on VSYNC:
1. Draw delta tiles (select bitmap + plot at changed positions)
2. Swap double buffer (`VDU 23,0,0xC3`)
3. Register **next** frame buffer as VSYNC callback
4. Last frame: deregister callback (stop playback)

### Bandwidth

| Phase | eZ80→VDP | Direction |
|---|---|---|
| Upload | ~1.1 MB (one-time) | eZ80 → VDP |
| Playback | **0 bytes** | Autonomous |

## Comparison with Streaming Approach

| | VDP Buffered | Streaming |
|---|---|---|
| Upload time | ~1.1 MB one-time | None |
| Per-frame bandwidth | 0 B | ~1 KB/frame |
| eZ80 CPU during playback | Free | Busy |
| Max resolution | Limited by PSRAM | Limited by UART |
| Max duration | ~129s (at current rate) | Unlimited |
| Complexity | Simple (upload + trigger) | Frame loop + timing |

## Limitations

1. **Duration cap**: ~3875 frames (~129s) at 320x240 with current encoding. The full Bad Apple video is 6572 frames (219s). Options:
   - Segment into 2 uploads with brief pause
   - More aggressive delta threshold (skip small changes)
   - Reduce tile count (fewer unique tiles = smaller deltas)

2. **Upload time**: 1.1 MB over UART at ~115200 baud would take ~96 seconds. Over TCP (emulator) it's instant. On real hardware, could use SPI or pre-load from SD card.

3. **No audio sync**: Playback is locked to VSYNC (60fps display, 30fps content). Audio synchronization would need separate handling.

## Next Steps

1. **Build C player** that uploads buffers from SD card and triggers autonomous playback
2. **Test on emulator** with full 1000-frame segment
3. **Add audio** — bytebeat or PCM playback synchronized to frame count
4. **Optimize for full video** — segment into 2–3 parts or reduce per-frame cost
