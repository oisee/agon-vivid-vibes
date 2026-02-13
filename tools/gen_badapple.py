#!/usr/bin/env python3
"""Bad Apple tile-based animation — codebook encoder + HTML player.

Pipeline:
  1. Extract frames from video at target resolution
  2. Threshold to 1-bit, split into 8x8 tiles
  3. Build 256-tile codebook from frequency analysis
  4. Encode each frame as tile IDs (nearest match for non-codebook tiles)
  5. Delta-encode frame-to-frame (store only changes)
  6. Export as self-contained HTML player

Usage:
  python gen_badapple.py --html badapple.html [--width 256 --height 192 --fps 30]
"""

import argparse
import base64
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


def extract_frames(video_path, width, height, tmpdir):
    pattern = os.path.join(tmpdir, "frame_%05d.png")
    cmd = [
        "ffmpeg", "-i", str(video_path),
        "-vf", f"scale={width}:{height}:flags=area,format=gray",
        "-y", pattern
    ]
    subprocess.run(cmd, capture_output=True, check=True)
    return sorted(Path(tmpdir).glob("frame_*.png"))


def frame_to_tilemap(path, tile_size, threshold=128):
    img = Image.open(path).convert("L")
    arr = np.array(img)
    binary = (arr >= threshold).astype(np.uint8)
    h, w = binary.shape
    tilemap = []
    for ty in range(0, h - tile_size + 1, tile_size):
        for tx in range(0, w - tile_size + 1, tile_size):
            tile = binary[ty:ty+tile_size, tx:tx+tile_size]
            key = bytes(
                sum(tile[r, c] << (7 - c) for c in range(8))
                for r in range(8)
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


def pack_animation(codebook, encoded_frames, tiles_w, tiles_h, fps):
    """Pack animation into compact binary blob.

    Format:
      [magic: 4B "BA01"]
      [tiles_w: u8] [tiles_h: u8] [tile_size: u8] [fps: u8]
      [num_frames: u16-LE]
      [codebook_size: u16-LE]
      [codebook: codebook_size * 8 bytes]  — each tile is 8 bytes (8x8 bits)
      [frame 0: tiles_w*tiles_h bytes]     — full keyframe
      [frame 1..N: delta-encoded]
        For each delta frame:
          [num_changes: u16-LE]
          if num_changes == 0xFFFF: keyframe follows (tiles_w*tiles_h bytes)
          else: [changes: num_changes * 3 bytes each]
            [pos_lo, pos_hi, tile_id]
    """
    tiles_per_frame = tiles_w * tiles_h
    num_frames = len(encoded_frames)

    parts = []
    # Header
    parts.append(b"BA01")
    parts.append(struct.pack("<BBBB", tiles_w, tiles_h, 8, fps))
    parts.append(struct.pack("<HH", num_frames, len(codebook)))

    # Codebook
    for key in codebook:
        parts.append(key)  # 8 bytes each

    # Frame 0: keyframe
    parts.append(bytes(encoded_frames[0]))

    # Frames 1..N: delta
    for i in range(1, num_frames):
        prev = encoded_frames[i - 1]
        curr = encoded_frames[i]
        changes = []
        for pos in range(tiles_per_frame):
            if prev[pos] != curr[pos]:
                changes.append((pos, curr[pos]))

        # If >50% changed, send keyframe
        if len(changes) > tiles_per_frame // 2:
            parts.append(struct.pack("<H", 0xFFFF))
            parts.append(bytes(curr))
        else:
            parts.append(struct.pack("<H", len(changes)))
            for pos, tid in changes:
                parts.append(struct.pack("<HB", pos, tid))

    return b"".join(parts)


def generate_html(blob, width, height, tiles_w, tiles_h, fps):
    """Generate self-contained HTML player."""
    # Compress with zlib for smaller embedding
    compressed = zlib.compress(blob, 9)
    b64_data = base64.b64encode(compressed).decode('ascii')

    html = f"""<!DOCTYPE html>
<html>
<head>
<meta charset="utf-8">
<title>Bad Apple — Tile Codebook Player (256x192)</title>
<style>
  body {{ margin: 0; background: #000; display: flex; justify-content: center;
         align-items: center; min-height: 100vh; flex-direction: column; }}
  canvas {{ image-rendering: pixelated; image-rendering: crisp-edges; }}
  #info {{ color: #888; font: 12px monospace; margin-top: 8px; }}
  #controls {{ color: #aaa; font: 13px monospace; margin-top: 6px; }}
</style>
</head>
<body>
<canvas id="c"></canvas>
<div id="info">Loading...</div>
<div id="controls">Space: pause/play &nbsp; R: restart</div>
<script>
// Decode zlib-compressed base64 blob
const b64 = "{b64_data}";
const compressed = Uint8Array.from(atob(b64), c => c.charCodeAt(0));

// Inflate using DecompressionStream('deflate' = zlib RFC 1950 format)
async function inflate(data) {{
  const ds = new DecompressionStream('deflate');
  const writer = ds.writable.getWriter();
  writer.write(data);
  writer.close();
  const reader = ds.readable.getReader();
  const chunks = [];
  while (true) {{
    const {{done, value}} = await reader.read();
    if (done) break;
    chunks.push(value);
  }}
  const total = chunks.reduce((a, c) => a + c.length, 0);
  const result = new Uint8Array(total);
  let offset = 0;
  for (const chunk of chunks) {{
    result.set(chunk, offset);
    offset += chunk.length;
  }}
  return result;
}}

inflate(compressed).then(blob => {{
  const view = new DataView(blob.buffer);
  let off = 4; // skip magic
  const tilesW = blob[off++];
  const tilesH = blob[off++];
  const tileSize = blob[off++];
  const fps = blob[off++];
  const numFrames = view.getUint16(off, true); off += 2;
  const codebookSize = view.getUint16(off, true); off += 2;

  const W = tilesW * tileSize;
  const H = tilesH * tileSize;
  const tilesPerFrame = tilesW * tilesH;

  // Parse codebook: each tile is 8 bytes (8 rows, 8 bits per row)
  const codebook = [];
  for (let t = 0; t < codebookSize; t++) {{
    const tile = new Uint8Array(8);
    for (let r = 0; r < 8; r++) tile[r] = blob[off++];
    codebook.push(tile);
  }}

  // Parse frames
  const frames = [];

  // Frame 0: keyframe
  const frame0 = new Uint8Array(tilesPerFrame);
  for (let i = 0; i < tilesPerFrame; i++) frame0[i] = blob[off++];
  frames.push(frame0);

  // Remaining frames: delta
  for (let f = 1; f < numFrames; f++) {{
    const numChanges = view.getUint16(off, true); off += 2;
    const prev = frames[f - 1];
    const curr = new Uint8Array(prev);

    if (numChanges === 0xFFFF) {{
      // Keyframe
      for (let i = 0; i < tilesPerFrame; i++) curr[i] = blob[off++];
    }} else {{
      for (let c = 0; c < numChanges; c++) {{
        const pos = view.getUint16(off, true); off += 2;
        const tid = blob[off++];
        curr[pos] = tid;
      }}
    }}
    frames.push(curr);
  }}

  // Canvas setup
  const canvas = document.getElementById('c');
  const scale = Math.min(Math.floor(window.innerHeight * 0.85 / H),
                         Math.floor(window.innerWidth * 0.95 / W), 4);
  canvas.width = W * scale;
  canvas.height = H * scale;
  const ctx = canvas.getContext('2d');
  ctx.imageSmoothingEnabled = false;

  // Pre-render codebook tiles to ImageData
  const offscreen = new OffscreenCanvas(W, H);
  const offCtx = offscreen.getContext('2d');
  const imgData = offCtx.createImageData(W, H);

  const info = document.getElementById('info');
  let frameIdx = 0;
  let playing = true;
  let lastTime = 0;
  const frameMs = 1000 / fps;

  function renderFrame(idx) {{
    const tilemap = frames[idx];
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
    ctx.drawImage(offscreen, 0, 0, W, H, 0, 0, W * scale, H * scale);

    const pct = ((idx / numFrames) * 100).toFixed(1);
    const sec = (idx / fps).toFixed(1);
    info.textContent = `Frame ${{idx}}/${{numFrames}} (${{sec}}s) ${{pct}}%` +
      ` | ${{tilesW}}x${{tilesH}} tiles | codebook: ${{codebookSize}}`;
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
}});
</script>
</body>
</html>"""
    return html


def main():
    parser = argparse.ArgumentParser(description="Bad Apple tile encoder + HTML player")
    parser.add_argument("--video", type=str, default=str(VIDEO_PATH))
    parser.add_argument("--html", type=str, required=True, help="Output HTML file")
    parser.add_argument("--width", type=int, default=256)
    parser.add_argument("--height", type=int, default=192)
    parser.add_argument("--tile", type=int, default=8)
    parser.add_argument("--codebook", type=int, default=256)
    parser.add_argument("--fps", type=int, default=30)
    args = parser.parse_args()

    tiles_w = args.width // args.tile
    tiles_h = args.height // args.tile
    tiles_per_frame = tiles_w * tiles_h

    print(f"Target: {args.width}x{args.height}, {args.tile}x{args.tile} tiles, "
          f"{tiles_w}x{tiles_h} grid = {tiles_per_frame} tiles/frame", file=sys.stderr)

    with tempfile.TemporaryDirectory(prefix="badapple_") as tmpdir:
        print("Extracting frames...", file=sys.stderr)
        frames = extract_frames(args.video, args.width, args.height, tmpdir)
        num_frames = len(frames)
        print(f"{num_frames} frames extracted", file=sys.stderr)

        # Build tilemaps
        print("Building tilemaps...", file=sys.stderr)
        all_tilemaps = []
        for i, fpath in enumerate(frames):
            if i % 1000 == 0:
                print(f"  Frame {i}/{num_frames}...", file=sys.stderr)
            all_tilemaps.append(frame_to_tilemap(fpath, args.tile))

    # Build codebook
    print("Building codebook...", file=sys.stderr)
    codebook, key_to_id = build_codebook(all_tilemaps, args.codebook)
    print(f"Codebook: {len(codebook)} tiles", file=sys.stderr)

    # Encode frames
    print("Encoding frames...", file=sys.stderr)
    encoded_frames = []
    for i, tm in enumerate(all_tilemaps):
        if i % 1000 == 0:
            print(f"  Encoding {i}/{num_frames}...", file=sys.stderr)
        encoded_frames.append(encode_tilemap(tm, key_to_id, codebook))

    # Pack binary blob
    print("Packing animation...", file=sys.stderr)
    blob = pack_animation(codebook, encoded_frames, tiles_w, tiles_h, args.fps)
    print(f"Blob: {len(blob):,} bytes ({len(blob)/1024:.0f} KB)", file=sys.stderr)

    # Count stats
    total_changes = 0
    keyframe_count = 1
    for i in range(1, num_frames):
        changes = sum(1 for a, b in zip(encoded_frames[i-1], encoded_frames[i]) if a != b)
        total_changes += changes
        if changes > tiles_per_frame // 2:
            keyframe_count += 1
    avg_changes = total_changes / (num_frames - 1)
    print(f"Avg changes/frame: {avg_changes:.1f} ({avg_changes/tiles_per_frame:.1%})",
          file=sys.stderr)
    print(f"Keyframes: {keyframe_count}", file=sys.stderr)

    # Generate HTML
    print("Generating HTML...", file=sys.stderr)
    html = generate_html(blob, args.width, args.height, tiles_w, tiles_h, args.fps)

    with open(args.html, "w") as f:
        f.write(html)

    html_size = os.path.getsize(args.html)
    print(f"Written: {args.html} ({html_size:,} bytes / {html_size/1024:.0f} KB)",
          file=sys.stderr)


if __name__ == "__main__":
    main()
