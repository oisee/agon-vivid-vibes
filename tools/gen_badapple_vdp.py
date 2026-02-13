#!/usr/bin/env python3
"""Bad Apple VDP buffer generator — tile bitmaps + delta frame buffers.

Generates VDP buffered commands for autonomous VSYNC-driven playback.
Each frame buffer chains to the next via VSYNC callback registration.

Architecture:
  - Buffers 1-256: tile bitmaps (8x8, RGBA2222 format)
  - Buffers 1000+: frame draw commands (select bitmap + plot at position)
  - Each frame buffer ends with: swap + register next frame as VSYNC callback
  - eZ80 uploads all buffers, registers frame 1000 → VDP plays autonomously

Delta encoding with double-buffer:
  - Frame N draws delta from frame N-2 (back buffer state after swap)
  - Frames 0,1 are full keyframes (CLG + all non-black tiles)

Usage:
  python gen_badapple_vdp.py --frames 1000 [--html preview.html]
"""

import argparse
import base64
import math
import os
import struct
import subprocess
import sys
import tempfile
import zlib
from collections import Counter
from pathlib import Path

import numpy as np
from PIL import Image

SCRIPT_DIR = Path(__file__).parent
VIDEO_PATH = SCRIPT_DIR.parent / "assets" / "badapple-frames" / "badapple.mp4"

TILE_SIZE = 8
BITMAP_BASE_ID = 1       # tile bitmaps: 1..256
FRAME_BASE_ID = 1000     # frame buffers: 1000..1000+N


def extract_frames(video_path, width, height, tmpdir, max_frames=None):
    pattern = os.path.join(tmpdir, "frame_%05d.png")
    cmd = [
        "ffmpeg", "-i", str(video_path),
        "-vf", f"scale={width}:{height}:flags=area,format=gray",
        "-y", pattern
    ]
    if max_frames:
        cmd = cmd[:3] + ["-frames:v", str(max_frames)] + cmd[3:]
    subprocess.run(cmd, capture_output=True, check=True)
    frames = sorted(Path(tmpdir).glob("frame_*.png"))
    return frames


def frame_to_tilemap(path, threshold=128):
    img = Image.open(path).convert("L")
    arr = np.array(img)
    binary = (arr >= threshold).astype(np.uint8)
    h, w = binary.shape
    tilemap = []
    for ty in range(0, h - TILE_SIZE + 1, TILE_SIZE):
        for tx in range(0, w - TILE_SIZE + 1, TILE_SIZE):
            tile = binary[ty:ty+TILE_SIZE, tx:tx+TILE_SIZE]
            key = bytes(
                sum(tile[r, c] << (7 - c) for c in range(TILE_SIZE))
                for r in range(TILE_SIZE)
            )
            tilemap.append(key)
    return tilemap


def build_codebook(all_tilemaps, size=256):
    counter = Counter()
    for tm in all_tilemaps:
        counter.update(tm)
    top = counter.most_common(size)
    key_to_id = {k: i for i, (k, _) in enumerate(top)}
    codebook = [k for k, _ in top]
    return codebook, key_to_id


def hamming_distance(a, b):
    return sum(bin(x ^ y).count('1') for x, y in zip(a, b))


def encode_tilemap(tilemap, key_to_id, codebook):
    ids = []
    for key in tilemap:
        if key in key_to_id:
            ids.append(key_to_id[key])
        else:
            best_dist = 64
            best_id = 0
            for i, cb_key in enumerate(codebook):
                d = hamming_distance(key, cb_key)
                if d < best_dist:
                    best_dist = d
                    best_id = i
                if d == 0:
                    break
            ids.append(best_id)
    return ids


def tile_to_rgba2222(tile_key):
    """Convert 8-byte tile key to 64-byte RGBA2222 bitmap data.
    White pixel = 0xFF (R=3,G=3,B=3,A=3), Black = 0xC0 (A=3, RGB=0)."""
    data = bytearray(64)
    for r in range(8):
        for c in range(8):
            bit = (tile_key[r] >> (7 - c)) & 1
            # RGBA2222: bits 7-6=A, 5-4=B, 3-2=G, 1-0=R
            data[r * 8 + c] = 0xFF if bit else 0xC0  # white or black, full alpha
    return bytes(data)


def build_bitmap_upload_commands(codebook):
    """Generate VDU commands to upload all tile bitmaps.
    Returns list of (buffer_id, vdu_bytes) tuples."""
    uploads = []
    for i, tile_key in enumerate(codebook):
        bitmap_id = BITMAP_BASE_ID + i
        rgba_data = tile_to_rgba2222(tile_key)

        # VDU 23, 27, 0, bitmap_id — select bitmap
        # VDU 23, 27, 1, w; h; <data> — upload RGBA2222
        vdu = bytearray()
        vdu.extend([23, 27, 0, bitmap_id & 0xFF])
        vdu.extend([23, 27, 1])
        vdu.extend(struct.pack("<HH", 8, 8))
        vdu.extend(rgba_data)
        uploads.append((bitmap_id, bytes(vdu)))
    return uploads


def build_frame_buffer(frame_idx, encoded_frame, prev_encoded, tiles_w, tiles_h,
                       num_total_frames, is_keyframe=False):
    """Build VDU commands for a single frame buffer.

    For keyframes: CLG + draw all non-black tiles
    For delta: draw only tiles that changed from prev_encoded
    """
    tiles_per_frame = tiles_w * tiles_h
    vdu = bytearray()

    if is_keyframe:
        # Find which tile ID is all-black (should be tile 0 in our codebook)
        # CLG clears to background color (black) — skip black tiles
        vdu.append(16)  # CLG

        # Group tiles by bitmap ID for efficiency (select once, plot many)
        from collections import defaultdict
        by_tile = defaultdict(list)
        for pos in range(tiles_per_frame):
            tid = encoded_frame[pos]
            # Skip all-black tile (assumed to be tile ID 0)
            if tid == 0:
                continue
            by_tile[tid].append(pos)

        for tid, positions in sorted(by_tile.items()):
            bitmap_id = BITMAP_BASE_ID + tid
            # Select bitmap
            vdu.extend([23, 27, 0, bitmap_id & 0xFF])
            for pos in positions:
                tx = pos % tiles_w
                ty = pos // tiles_w
                x = tx * TILE_SIZE
                y = ty * TILE_SIZE
                # Plot bitmap at position
                vdu.extend([23, 27, 3])
                vdu.extend(struct.pack("<HH", x, y))
    else:
        # Delta frame: draw only changed tiles
        # Group by tile ID for efficiency
        from collections import defaultdict
        by_tile = defaultdict(list)
        for pos in range(tiles_per_frame):
            if encoded_frame[pos] != prev_encoded[pos]:
                by_tile[encoded_frame[pos]].append(pos)

        for tid, positions in sorted(by_tile.items()):
            bitmap_id = BITMAP_BASE_ID + tid
            vdu.extend([23, 27, 0, bitmap_id & 0xFF])
            for pos in positions:
                tx = pos % tiles_w
                ty = pos // tiles_w
                x = tx * TILE_SIZE
                y = ty * TILE_SIZE
                vdu.extend([23, 27, 3])
                vdu.extend(struct.pack("<HH", x, y))

    # Swap double buffer
    vdu.extend([23, 0, 0xC3])

    # Register next frame as VSYNC callback (chain playback)
    next_frame = frame_idx + 1
    if next_frame >= num_total_frames:
        # Last frame: deregister VSYNC callback (stop playback)
        # Command 80 with buffer 0xFFFF = clear callback
        vdu.extend([23, 0, 0xA0, 0xFF, 0xFF, 80])
    else:
        next_id = FRAME_BASE_ID + next_frame
        vdu.extend([23, 0, 0xA0, next_id & 0xFF, (next_id >> 8) & 0xFF, 80])

    return bytes(vdu)


def wrap_vdp_buffer(buffer_id, payload):
    """Wrap payload in VDP buffer write command.
    VDU 23, 0, &A0, id_lo, id_hi, 0, len_lo, len_hi, <payload>"""
    header = bytearray([
        23, 0, 0xA0,
        buffer_id & 0xFF, (buffer_id >> 8) & 0xFF,
        0,  # command 0 = write
        len(payload) & 0xFF, (len(payload) >> 8) & 0xFF
    ])
    return bytes(header) + payload


def generate_html(codebook, encoded_frames, frame_vdu_sizes, tiles_w, tiles_h, fps):
    """Generate HTML preview showing playback + VDP buffer size stats."""
    # Pack tile data for JS player (reuse gen_badapple format)
    tiles_per_frame = tiles_w * tiles_h
    num_frames = len(encoded_frames)

    # Encode codebook as JS array
    cb_js = "["
    for i, key in enumerate(codebook):
        cb_js += "[" + ",".join(str(b) for b in key) + "]"
        if i < len(codebook) - 1:
            cb_js += ","
    cb_js += "]"

    # Encode frames as delta stream for JS
    # Frame 0: full, then deltas from N-2
    frames_js_parts = []
    frames_js_parts.append("[" + ",".join(str(t) for t in encoded_frames[0]) + "]")
    for i in range(1, num_frames):
        prev_idx = max(0, i - 2) if i >= 2 else -1  # -1 means keyframe
        if prev_idx == -1:
            frames_js_parts.append("[" + ",".join(str(t) for t in encoded_frames[i]) + "]")
        else:
            # Delta: list of [pos, new_tid] pairs
            changes = []
            for pos in range(tiles_per_frame):
                if encoded_frames[i][pos] != encoded_frames[prev_idx][pos]:
                    changes.append(f"[{pos},{encoded_frames[i][pos]}]")
            frames_js_parts.append("[" + ",".join(changes) + "]")

    # VDU sizes for stats display
    sizes_js = "[" + ",".join(str(s) for s in frame_vdu_sizes) + "]"

    html = f"""<!DOCTYPE html>
<html>
<head>
<meta charset="utf-8">
<title>Bad Apple VDP — 320x240 Tile Preview</title>
<style>
  body {{ margin: 0; background: #000; display: flex; justify-content: center;
         align-items: center; min-height: 100vh; flex-direction: column; }}
  canvas {{ image-rendering: pixelated; image-rendering: crisp-edges; }}
  #info {{ color: #888; font: 12px monospace; margin-top: 8px; }}
  #stats {{ color: #6a6; font: 12px monospace; margin-top: 4px; }}
  #controls {{ color: #aaa; font: 13px monospace; margin-top: 6px; }}
</style>
</head>
<body>
<canvas id="c"></canvas>
<div id="info">Loading...</div>
<div id="stats"></div>
<div id="controls">Space: pause/play &nbsp; R: restart</div>
<script>
const tilesW = {tiles_w}, tilesH = {tiles_h}, tileSize = {TILE_SIZE};
const fps = {fps};
const W = tilesW * tileSize, H = tilesH * tileSize;
const tilesPerFrame = tilesW * tilesH;
const codebook = {cb_js};
const vduSizes = {sizes_js};

// Decode frames: frame 0,1 = full keyframes, rest = delta from N-2
const allFrames = [];  // will hold full tilemaps

// Frame data (mix of full frames and deltas)
const frameData = [{",".join(frames_js_parts)}];

// Reconstruct full frames from deltas
for (let i = 0; i < frameData.length; i++) {{
  if (i < 2) {{
    // Keyframe: frameData[i] is full tilemap array
    allFrames.push(new Uint8Array(frameData[i]));
  }} else {{
    // Delta from frame i-2
    const prev = allFrames[i - 2];
    const curr = new Uint8Array(prev);
    const changes = frameData[i];
    for (const ch of changes) {{
      curr[ch[0]] = ch[1];
    }}
    allFrames.push(curr);
  }}
}}

const numFrames = allFrames.length;

// Canvas setup
const canvas = document.getElementById('c');
const scale = Math.min(Math.floor(window.innerHeight * 0.85 / H),
                       Math.floor(window.innerWidth * 0.95 / W), 4);
canvas.width = W * scale;
canvas.height = H * scale;
const ctx = canvas.getContext('2d');
ctx.imageSmoothingEnabled = false;

const offscreen = new OffscreenCanvas(W, H);
const offCtx = offscreen.getContext('2d');
const imgData = offCtx.createImageData(W, H);

const info = document.getElementById('info');
const stats = document.getElementById('stats');
let frameIdx = 0, playing = true, lastTime = 0;
const frameMs = 1000 / fps;

// Compute total PSRAM usage
const bitmapBytes = codebook.length * (8 + 64);  // header + 64B RGBA2222 each
const totalFrameBytes = vduSizes.reduce((a, b) => a + b, 0);
const totalPSRAM = bitmapBytes + totalFrameBytes;

function renderFrame(idx) {{
  const tilemap = allFrames[idx];
  const pixels = imgData.data;
  for (let ty = 0; ty < tilesH; ty++) {{
    for (let tx = 0; tx < tilesW; tx++) {{
      const tid = tilemap[ty * tilesW + tx];
      const tile = codebook[tid];
      for (let r = 0; r < tileSize; r++) {{
        const rowByte = tile[r];
        for (let c = 0; c < tileSize; c++) {{
          const px = tx * tileSize + c;
          const py = ty * tileSize + r;
          const pidx = (py * W + px) * 4;
          const val = (rowByte >> (7 - c)) & 1 ? 255 : 0;
          pixels[pidx] = val;
          pixels[pidx + 1] = val;
          pixels[pidx + 2] = val;
          pixels[pidx + 3] = 255;
        }}
      }}
    }}
  }}
  offCtx.putImageData(imgData, 0, 0);
  ctx.drawImage(offscreen, 0, 0, W, H, 0, 0, canvas.width, canvas.height);

  const pct = ((idx / numFrames) * 100).toFixed(1);
  const sec = (idx / fps).toFixed(1);
  info.textContent = `Frame ${{idx}}/${{numFrames}} (${{sec}}s) ${{pct}}%`;
  const vduB = vduSizes[idx];
  stats.textContent = `VDP buffer: ${{vduB}}B | ` +
    `Total PSRAM: ${{(totalPSRAM/1024).toFixed(0)}}KB ` +
    `(bitmaps: ${{(bitmapBytes/1024).toFixed(1)}}KB + ` +
    `frames: ${{(totalFrameBytes/1024).toFixed(0)}}KB)`;
}}

function animate(ts) {{
  if (!lastTime) lastTime = ts;
  if (playing) {{
    const elapsed = ts - lastTime;
    if (elapsed >= frameMs) {{
      lastTime = ts - (elapsed % frameMs);
      renderFrame(frameIdx);
      frameIdx = (frameIdx + 1) % numFrames;
    }}
  }}
  requestAnimationFrame(animate);
}}

document.addEventListener('keydown', e => {{
  if (e.code === 'Space') {{ playing = !playing; e.preventDefault(); }}
  if (e.code === 'KeyR') {{ frameIdx = 0; lastTime = 0; playing = true; }}
}});

renderFrame(0);
requestAnimationFrame(animate);
</script>
</body>
</html>"""
    return html


def main():
    parser = argparse.ArgumentParser(description="Bad Apple VDP buffer generator")
    parser.add_argument("--video", type=str, default=str(VIDEO_PATH))
    parser.add_argument("--html", type=str, help="Output HTML preview")
    parser.add_argument("--width", type=int, default=320)
    parser.add_argument("--height", type=int, default=240)
    parser.add_argument("--codebook", type=int, default=256)
    parser.add_argument("--frames", type=int, default=1000)
    parser.add_argument("--fps", type=int, default=30)
    args = parser.parse_args()

    tiles_w = args.width // TILE_SIZE
    tiles_h = args.height // TILE_SIZE
    tiles_per_frame = tiles_w * tiles_h

    print(f"Target: {args.width}x{args.height}, {tiles_w}x{tiles_h} grid, "
          f"{tiles_per_frame} tiles/frame, {args.frames} frames", file=sys.stderr)

    with tempfile.TemporaryDirectory(prefix="badapple_vdp_") as tmpdir:
        print("Extracting frames...", file=sys.stderr)
        frames = extract_frames(args.video, args.width, args.height, tmpdir, args.frames)
        num_frames = len(frames)
        print(f"{num_frames} frames extracted", file=sys.stderr)

        print("Building tilemaps...", file=sys.stderr)
        all_tilemaps = []
        for i, fpath in enumerate(frames):
            if i % 500 == 0:
                print(f"  Frame {i}/{num_frames}...", file=sys.stderr)
            all_tilemaps.append(frame_to_tilemap(fpath))

    print("Building codebook...", file=sys.stderr)
    codebook, key_to_id = build_codebook(all_tilemaps, args.codebook)

    print("Encoding frames...", file=sys.stderr)
    encoded_frames = []
    for i, tm in enumerate(all_tilemaps):
        if i % 500 == 0:
            print(f"  Encoding {i}/{num_frames}...", file=sys.stderr)
        encoded_frames.append(encode_tilemap(tm, key_to_id, codebook))

    # Build VDP bitmap upload commands
    print("Building VDP commands...", file=sys.stderr)
    bitmap_uploads = build_bitmap_upload_commands(codebook)
    bitmap_total = sum(len(wrap_vdp_buffer(bid, vdu)) for bid, vdu in bitmap_uploads)

    # Build frame buffers with delta from N-2 (double-buffer aware)
    frame_vdu_sizes = []
    frame_total = 0
    keyframe_count = 0
    delta_changes = []

    for f in range(num_frames):
        if f < 2:
            # Keyframes (first two frames)
            is_keyframe = True
            prev = None
            keyframe_count += 1
        else:
            # Delta from frame f-2 (back buffer state)
            is_keyframe = False
            prev = encoded_frames[f - 2]

        vdu = build_frame_buffer(f, encoded_frames[f], prev,
                                 tiles_w, tiles_h, num_frames, is_keyframe)
        wrapped = wrap_vdp_buffer(FRAME_BASE_ID + f, vdu)
        frame_vdu_sizes.append(len(vdu))
        frame_total += len(wrapped)

        if not is_keyframe:
            changes = sum(1 for a, b in zip(encoded_frames[f], encoded_frames[f-2]) if a != b)
            delta_changes.append(changes)

    total_psram = bitmap_total + frame_total

    print(f"\n{'='*60}", file=sys.stderr)
    print(f"VDP BUFFER ANALYSIS", file=sys.stderr)
    print(f"{'='*60}", file=sys.stderr)
    print(f"Frames:           {num_frames}", file=sys.stderr)
    print(f"Tile bitmaps:     {len(codebook)} × {8+64}B = "
          f"{bitmap_total:,}B ({bitmap_total/1024:.1f}KB)", file=sys.stderr)
    print(f"Keyframes:        {keyframe_count}", file=sys.stderr)

    vdu_arr = np.array(frame_vdu_sizes)
    print(f"\nFrame buffer sizes:", file=sys.stderr)
    print(f"  Min:    {vdu_arr.min():,}B", file=sys.stderr)
    print(f"  Max:    {vdu_arr.max():,}B", file=sys.stderr)
    print(f"  Mean:   {vdu_arr.mean():,.0f}B", file=sys.stderr)
    print(f"  Median: {np.median(vdu_arr):,.0f}B", file=sys.stderr)
    print(f"  P80:    {np.percentile(vdu_arr, 80):,.0f}B", file=sys.stderr)
    print(f"  Total:  {frame_total:,}B ({frame_total/1024:.0f}KB)", file=sys.stderr)

    if delta_changes:
        dc = np.array(delta_changes)
        print(f"\nDelta changes (from N-2):", file=sys.stderr)
        print(f"  Mean:   {dc.mean():.0f} tiles ({dc.mean()/tiles_per_frame:.1%})", file=sys.stderr)
        print(f"  Median: {np.median(dc):.0f} tiles", file=sys.stderr)
        print(f"  P80:    {np.percentile(dc, 80):.0f} tiles", file=sys.stderr)

    print(f"\nTotal PSRAM: {total_psram:,}B ({total_psram/1024:.0f}KB / "
          f"{total_psram/1024/1024:.1f}MB)", file=sys.stderr)
    print(f"VDP PSRAM available: ~4MB", file=sys.stderr)
    fits = "YES" if total_psram < 4 * 1024 * 1024 else "NO"
    print(f"Fits in PSRAM: {fits}", file=sys.stderr)

    max_frames_est = int(4 * 1024 * 1024 / (frame_total / num_frames)) if frame_total > 0 else 0
    print(f"Estimated max frames at this rate: ~{max_frames_est}", file=sys.stderr)

    # Playback stats
    duration = num_frames / args.fps
    print(f"\nPlayback: {duration:.1f}s at {args.fps}fps", file=sys.stderr)
    print(f"eZ80 bandwidth during playback: 0 bytes (fully autonomous!)", file=sys.stderr)

    # Generate HTML preview
    if args.html:
        print(f"\nGenerating HTML preview...", file=sys.stderr)
        html = generate_html(codebook, encoded_frames, frame_vdu_sizes,
                             tiles_w, tiles_h, args.fps)
        with open(args.html, "w") as f:
            f.write(html)
        html_size = os.path.getsize(args.html)
        print(f"Written: {args.html} ({html_size:,}B / {html_size/1024:.0f}KB)",
              file=sys.stderr)


if __name__ == "__main__":
    main()
