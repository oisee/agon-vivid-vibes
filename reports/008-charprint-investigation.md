# 008 — Charprint Mode Investigation & VDP Replay Init Discovery

## Summary

Investigated using VDU 5 "text at graphics cursor" mode to dramatically reduce per-frame
bandwidth for Bad Apple tile animation. Found a critical VDP firmware init gate that blocks
all non-VDU-23 commands until a General Poll handshake is received. This blocked both the
charprint replay and ALL previous replay tests (they only appeared to work because they
never actually rendered).

## What is Charprint?

Instead of using bitmap select+draw (11 bytes/tile), redefine characters 32-255 as 8x8
tile patterns using `VDU 23, char_code, b0..b7`, then use `VDU 5` (text at graphics cursor)
to "print" tiles at the graphics cursor. Consecutive changed tiles in a row need only 1 byte
each (the character code), vs 11 bytes with the old bitmap approach.

### Bandwidth analysis

| Metric | Bitmap mode | Charprint mode |
|--------|-------------|----------------|
| Codebook size | 256 tiles | 224 tiles (skip 0-31 control codes) |
| Quality loss | baseline | 0.14% (negligible for Bad Apple) |
| Per-tile cost (isolated) | 11 bytes | 7 bytes (6B MOVE + 1B char) |
| Per-tile cost (in run) | 11 bytes | **1 byte** |
| Mean frame size | ~1780B | ~516B |
| Max frame size | ~4400B | ~1430B |
| 30fps UART budget (115,200 B/s) | 3,840B (tight) | 3,840B (all frames fit) |
| Full video in PSRAM | ~11MB (won't fit 4MB) | ~3.4MB (**fits!**) |

### Setup VDU sequence

```
VDU 5                          -- text at graphics cursor mode
GCOL 0, 63                     -- white foreground
GCOL 0, 128                    -- black background (128 = bg flag)
VDU 23, 32, b0..b7             -- define char 32 (tile 0)
VDU 23, 33, b0..b7             -- define char 33 (tile 1)
...
VDU 23, 255, b0..b7            -- define char 255 (tile 223)
```

224 chars x 10 bytes = 2,240 bytes of definitions.

### Per-frame VDU

```
MOVE x, y          -- 6 bytes, start of changed run
<char> <char> ...  -- 1 byte per tile in run (consecutive = no MOVE needed)
MOVE x, y          -- next run
<char> <char> ...
VDU 23, 0, 0xC3    -- swap double buffer (3 bytes)
```

## Implementation

### Python encoder (`gen_badapple_vdp.py`)

- `--charprint` flag: forces 224-tile codebook, offsets IDs by 32
- `build_chardef_commands()`: generates `VDU 23, char_code, b0..b7` for each tile
- `build_frame_charprint()`: row-scan, run detection, MOVE + char bytes + swap
- BA2S header byte 14 bit 0 = charprint flag
- `--replay-file` output: streams VDU per VSYNC (no VDP buffers)

### C player (`badapple/src/main.c`)

- Auto-detects charprint from BA2S header flag byte
- Builds MOVE + char VDU instead of bitmap select+draw
- Same decompression pipeline (LZSS masks + Huffman IDs)

## Critical Discovery: VDP `wait_eZ80()` Init Gate

### The problem

ALL VDP replay files were silently broken. The VDP appeared to process chunks (log showed
VSYNC events), but nothing rendered on screen — every frame was black with the startup text
"Agon Platform VDP Version 2.14.1" still visible.

### Root cause

**File:** `vdp-console8/video/vdu_sys.h` lines 46-64
**File:** `vdp-console8/video/video.ino` line 137

```cpp
void processLoop(void * parameter) {
    setupKeyboardAndMouse();
    processor->wait_eZ80();        // <-- BLOCKS HERE
    while (true) {
        processor->processNext();  // Normal VDU processing
    }
}

void VDUStreamProcessor::wait_eZ80() {
    while (!initialised) {
        if (byteAvailable()) {
            auto c = readByte();
            if (c == 23) {         // Only VDU 23 commands processed
                vdu_sys();
            }
            // ALL other bytes (VDU 22, VDU 5, VDU 18, etc.) are DISCARDED
        }
    }
    sendModeInformation();
}
```

The VDP firmware blocks in `wait_eZ80()` until `initialised` becomes true. During this
wait, it **only** processes VDU 23 commands. All other VDU commands are read and silently
discarded. This means:

- `VDU 22` (mode switch) — discarded
- `VDU 5` (text at graphics cursor) — discarded
- `VDU 18` (GCOL) — discarded
- `VDU 16` (CLG) — discarded
- `VDU 25` (PLOT/MOVE) — discarded
- Character codes 32-255 — discarded

VDU 23 with char >= 32 (character definitions) ARE processed during init. But that's it.

### What sets `initialised = true`?

**General Poll:** `VDU 23, 0, 0x80, n` — sets `initialised = true` at line 388.

This is the eZ80 → VDP handshake. In normal operation, MOS sends this during boot.
In replay mode, it must be explicitly included in the .vdu file.

### The fix

Prepend `VDU 23, 0, 0x80, 1` as the **first chunk** of any replay file:

```python
# Must be its own chunk — VDP sends GP response + mode info,
# and the replay tool needs to drain these before sending more data
# (otherwise: output-queue deadlock on CTS flow control).
write_chunk(f, bytes([23, 0, 0x80, 1]))
```

**Important:** The GP MUST be in its own chunk (not combined with other setup data).
After processing the GP, the VDP sends ~12 bytes of response (GP ack + mode info).
The replay tool drains VDP responses only between chunks. If GP is combined with other
data in the same chunk, the replay tool tries to send more bytes while the VDP's output
queue hasn't been drained, causing a CTS flow control deadlock.

### Verification

Simple draw test with GP init: **works perfectly** — mode switch renders, PLOT draws a
filled rectangle, all chunks processed to EOF.

## Current Status

| Component | Status | Notes |
|-----------|--------|-------|
| Charprint encoder | Done | `--charprint` flag, 224-tile codebook |
| BA2S charprint format | Done | Header flag byte, ID offset +32 |
| C player charprint decode | Done | Auto-detect from header |
| VDP replay GP init fix | Done | Separate first chunk |
| Charprint replay test | **TODO** | Need to regenerate & test with GP fix |
| Charprint C player test | **TODO** | Showed garbled output, needs debug |
| Full video charprint BA2S | Done | 6572 frames, 1.24MB, on sdcard |
| PSRAM buffer upload charprint | **TODO** | 3.4MB should fit in 4MB PSRAM |

## Next Steps

1. **Regenerate charprint replay** with the GP init fix and test with `agon-vdp --replay`
2. **Debug charprint rendering** — verify character definitions + VDU 5 printing actually
   produces correct tile graphics in the VDP
3. **Test C player** in full emulator (`fab-agon-emulator`) with charprint BA2S
4. **PSRAM buffer upload** — charprint frames are small enough (~516B avg) to fit all
   6572 frames in 4MB PSRAM for fully autonomous playback
5. **Consider filing an enhancement** for `agon-vdp --replay` to auto-send GP init

## Files Modified

- `tools/gen_badapple_vdp.py` — added GP init chunk to replay output
- `badapple/src/main.c` — charprint VDU building (from earlier session)

## Key Source References

- VDP init gate: `fab-agon-emulator/src/vdp/vdp-console8/video/vdu_sys.h:46-64`
- GP sets initialised: `fab-agon-emulator/src/vdp/vdp-console8/video/vdu_sys.h:388`
- processLoop entry: `fab-agon-emulator/src/vdp/vdp-console8/video/video.ino:131-155`
- Replay byte send: `fab-agon-emulator/agon-vdp-sdl/src/main.rs:386-401`
- CTS flow control: `fab-agon-emulator/src/vdp/userspace-vdp-gl/src/userspace-platform/HardwareSerial.h:41`
- Fake serial CTS threshold: set to 64 bytes via `setHwFlowCtrlMode(..., 64)`
