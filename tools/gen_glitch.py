#!/usr/bin/env python3
"""Generate a digital noise glitch effect.

Random colored blocks, displaced scanlines, and text overlay.
Each frame is deterministically generated from a time-based seed.

Outputs:
  --html FILE    self-contained HTML canvas animation
  --preview      stream live to agon-vdp-sdl via fake_ez80 TCP server
  --output FILE  save to a .vdp file
"""

import argparse
import json
import math
import struct
import sys
import time

from vdp_stream import VDPStream, agon_rgb, agon_index_to_hex

# -- Screen geometry (mode 8: 320x240, 64 colours) --
SCREEN_W = 320
SCREEN_H = 240
SCREEN_MODE = 8

# -- Glitch parameters --
NUM_BLOCKS = 20
NUM_SCANLINES = 20
SCANLINE_SHIFT_MAX = 15

OVERLAY_TEXTS = [
    "VIVID VIBES",
    "AGON LIGHT",
    ">>> VDP <<<",
]


def pseudo_random(seed):
    """Simple deterministic pseudo-random generator. Returns 0.0-1.0."""
    # LCG-style hash
    seed = ((seed * 1103515245 + 12345) & 0x7FFFFFFF)
    return (seed & 0xFFFF) / 65535.0, seed


def rand_int(seed, lo, hi):
    """Return random int in [lo, hi] and next seed."""
    val, seed = pseudo_random(seed)
    return lo + int(val * (hi - lo + 0.999)), seed


def rand_colour(seed):
    """Generate a random Agon 64-colour index."""
    r, seed = rand_int(seed, 0, 3)
    g, seed = rand_int(seed, 0, 3)
    b, seed = rand_int(seed, 0, 3)
    return agon_rgb(r, g, b), seed


def generate_frame(frame_idx, total_frames):
    """Generate one frame of the glitch effect."""
    seed = frame_idx * 7919 + 42

    s = VDPStream()
    s.clg()
    canvas_ops = []

    # -- Random colored blocks --
    for _ in range(NUM_BLOCKS):
        x, seed = rand_int(seed, 0, SCREEN_W - 20)
        y, seed = rand_int(seed, 0, SCREEN_H - 10)
        w, seed = rand_int(seed, 10, 80)
        h, seed = rand_int(seed, 5, 40)
        col, seed = rand_colour(seed)

        x2 = min(SCREEN_W - 1, x + w)
        y2 = min(SCREEN_H - 1, y + h)

        s.gcol(0, col)
        s.filled_rect(x, y, x2, y2)
        canvas_ops.append({
            "type": "rect",
            "color": agon_index_to_hex(col),
            "x": x, "y": y, "w": x2 - x, "h": y2 - y,
        })

    # -- Displaced scanlines (partial-width glitch strips) --
    for i in range(NUM_SCANLINES):
        scan_y, seed = rand_int(seed, 0, SCREEN_H - 2)
        shift, seed = rand_int(seed, -SCANLINE_SHIFT_MAX, SCANLINE_SHIFT_MAX)
        thickness, seed = rand_int(seed, 1, 2)
        strip_w, seed = rand_int(seed, 40, 200)
        strip_x, seed = rand_int(seed, 0, SCREEN_W - strip_w)
        col, seed = rand_colour(seed)

        sx = max(0, strip_x + shift)
        ex = min(SCREEN_W - 1, strip_x + strip_w + shift)
        ey = min(SCREEN_H - 1, scan_y + thickness)

        s.gcol(0, col)
        s.filled_rect(sx, scan_y, ex, ey)
        canvas_ops.append({
            "type": "rect",
            "color": agon_index_to_hex(col),
            "x": sx, "y": scan_y, "w": ex - sx, "h": ey - scan_y,
        })

    # -- Text overlay (VDU text printing) --
    text_idx = (frame_idx // 20) % len(OVERLAY_TEXTS)
    text = OVERLAY_TEXTS[text_idx]
    # Flicker: sometimes skip text
    show_text = (frame_idx % 3) != 0

    if show_text:
        # Text position: slightly jittered
        tx_jitter, seed = rand_int(seed, -3, 3)
        ty_jitter, seed = rand_int(seed, -2, 2)
        text_col, seed = rand_colour(seed)

        canvas_ops.append({
            "type": "text",
            "text": text,
            "color": agon_index_to_hex(text_col),
            "x": 10 + tx_jitter,
            "y": 12 + ty_jitter,
        })

        # VDP text: set cursor position and print
        # TAB(x,y) = VDU 31, x, y
        s.colour(text_col)
        tx_char = max(0, min(39, 5 + tx_jitter))
        ty_char = max(0, min(29, 14 + ty_jitter))
        s.raw(31, tx_char, ty_char)
        for ch in text:
            s.raw(ord(ch))

    return s, canvas_ops


def generate_html(frames_data, filename):
    """Write a self-contained HTML canvas animation file."""
    html = f"""\
<!DOCTYPE html>
<html>
<head>
<meta charset="utf-8">
<title>Agon Glitch Preview</title>
<style>
  body {{ background: #111; color: #ccc; font-family: monospace; margin: 20px; }}
  canvas {{ border: 1px solid #444; image-rendering: pixelated; }}
  .controls {{ margin: 10px 0; }}
  button {{ font-family: monospace; font-size: 14px; padding: 4px 12px; margin-right: 8px; }}
  #info {{ margin-top: 8px; }}
</style>
</head>
<body>
<h3>Agon Glitch Preview (320x240, 64-colour palette)</h3>
<canvas id="c" width="640" height="480"></canvas>
<div class="controls">
  <button id="playBtn">Pause</button>
  <button id="prevBtn">&larr; Prev</button>
  <button id="nextBtn">Next &rarr;</button>
  <span id="info">Frame 0 / 0</span>
</div>
<script>
const FRAMES = {json.dumps(frames_data, separators=(',', ':'))};
const W = 320, H = 240, SCALE = 2;
const canvas = document.getElementById('c');
const ctx = canvas.getContext('2d');
const info = document.getElementById('info');
const playBtn = document.getElementById('playBtn');

let frame = 0;
let playing = true;
let lastTime = 0;
const FPS = 30;
const FRAME_MS = 1000 / FPS;

function drawFrame(f) {{
  ctx.fillStyle = '#000';
  ctx.fillRect(0, 0, W * SCALE, H * SCALE);
  const ops = FRAMES[f];
  for (const op of ops) {{
    if (op.type === 'rect') {{
      ctx.fillStyle = op.color;
      ctx.fillRect(op.x * SCALE, op.y * SCALE, op.w * SCALE, op.h * SCALE);
    }} else if (op.type === 'text') {{
      ctx.fillStyle = op.color;
      ctx.font = (16 * SCALE) + 'px monospace';
      ctx.fillText(op.text, op.x * SCALE * 8, op.y * SCALE * 8);
    }}
  }}
  info.textContent = `Frame ${{f + 1}} / ${{FRAMES.length}} (${{ops.length}} ops)`;
}}

function animate(ts) {{
  if (playing) {{
    if (ts - lastTime >= FRAME_MS) {{
      lastTime = ts;
      drawFrame(frame);
      frame = (frame + 1) % FRAMES.length;
    }}
  }}
  requestAnimationFrame(animate);
}}

playBtn.onclick = () => {{
  playing = !playing;
  playBtn.textContent = playing ? 'Pause' : 'Play';
}};
document.getElementById('prevBtn').onclick = () => {{
  playing = false;
  playBtn.textContent = 'Play';
  frame = (frame - 1 + FRAMES.length) % FRAMES.length;
  drawFrame(frame);
}};
document.getElementById('nextBtn').onclick = () => {{
  playing = false;
  playBtn.textContent = 'Play';
  drawFrame(frame);
  frame = (frame + 1) % FRAMES.length;
}};
document.addEventListener('keydown', (e) => {{
  if (e.key === 'ArrowLeft') {{
    playing = false;
    playBtn.textContent = 'Play';
    frame = (frame - 1 + FRAMES.length) % FRAMES.length;
    drawFrame(frame);
  }} else if (e.key === 'ArrowRight') {{
    playing = false;
    playBtn.textContent = 'Play';
    drawFrame(frame);
    frame = (frame + 1) % FRAMES.length;
  }} else if (e.key === ' ') {{
    e.preventDefault();
    playing = !playing;
    playBtn.textContent = playing ? 'Pause' : 'Play';
  }}
}});

drawFrame(0);
requestAnimationFrame(animate);
</script>
</body>
</html>"""

    with open(filename, "w") as f:
        f.write(html)
    print(f"[gen_glitch] Wrote HTML preview to {filename}", file=sys.stderr)


def write_vdp_file(filename, frames, mode, fps):
    """Write frames to a .vdp stream file."""
    with open(filename, "wb") as f:
        f.write(b"VDP1")
        f.write(struct.pack("<HHH", mode, len(frames), fps))
        for frame_data in frames:
            f.write(struct.pack("<H", len(frame_data)))
            f.write(frame_data)
    print(f"[gen_glitch] Wrote {len(frames)} frames to {filename}", file=sys.stderr)


def run_preview(frames_iter, total_frames, port, verbose=False):
    """Stream frames live to agon-vdp-sdl via fake_ez80 server."""
    from fake_ez80 import FakeEz80Server

    server = FakeEz80Server(port=port, verbose=verbose)
    server.start()

    init = VDPStream()
    init.general_poll()
    server.send_vdu(init.get_bytes())
    print("[gen_glitch] Sent General Poll", file=sys.stderr)

    for _ in range(5):
        server.wait_vsync()

    init = VDPStream()
    init.mode(SCREEN_MODE)
    init.set_logical_coords(False)
    init.cursor(False)
    server.send_vdu(init.get_bytes())
    print(f"[gen_glitch] Sent mode({SCREEN_MODE}) + pixel coords", file=sys.stderr)

    for _ in range(5):
        server.wait_vsync()

    frame_num = 0
    try:
        for frame_stream, _canvas in frames_iter:
            if not server.connected:
                break
            frame_bytes = frame_stream.get_bytes()
            server.send_vdu(frame_bytes)
            server.wait_vsync()
            frame_num += 1
            if frame_num % 60 == 0:
                print(f"[gen_glitch] Frame {frame_num}/{total_frames} ({len(frame_bytes)}B)", file=sys.stderr)
    except KeyboardInterrupt:
        print(f"\n[gen_glitch] Stopped at frame {frame_num}", file=sys.stderr)
    finally:
        server.shutdown()


def main():
    parser = argparse.ArgumentParser(description="Generate digital glitch effect")
    parser.add_argument("--html", type=str, default=None,
                        help="Write self-contained HTML canvas preview")
    parser.add_argument("--preview", action="store_true",
                        help="Stream live to agon-vdp-sdl via TCP")
    parser.add_argument("--output", "-o", type=str, default=None,
                        help="Save to .vdp file")
    parser.add_argument("--port", type=int, default=5001)
    parser.add_argument("--frames", type=int, default=120)
    parser.add_argument("--fps", type=int, default=30)
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args()

    if not args.html and not args.preview and not args.output:
        parser.error("Specify --html FILE, --preview, and/or --output FILE")

    num_frames = args.frames
    print(f"[gen_glitch] Generating {num_frames} frames of digital glitch...", file=sys.stderr)

    def frame_generator():
        for i in range(num_frames):
            yield generate_frame(i, num_frames)

    if args.html:
        t0 = time.time()
        all_canvas_frames = []
        all_vdp_frames = []
        for i, (vdp_stream, canvas_data) in enumerate(frame_generator()):
            all_canvas_frames.append(canvas_data)
            all_vdp_frames.append(vdp_stream.get_bytes())
        elapsed = time.time() - t0
        print(f"[gen_glitch] Generation took {elapsed:.1f}s", file=sys.stderr)
        generate_html(all_canvas_frames, args.html)
        if args.output:
            write_vdp_file(args.output, all_vdp_frames, SCREEN_MODE, args.fps)
    elif args.output:
        t0 = time.time()
        frame_bytes = []
        for i, (vdp_stream, _) in enumerate(frame_generator()):
            frame_bytes.append(vdp_stream.get_bytes())
        elapsed = time.time() - t0
        print(f"[gen_glitch] Generation took {elapsed:.1f}s", file=sys.stderr)
        write_vdp_file(args.output, frame_bytes, SCREEN_MODE, args.fps)

    if args.preview:
        run_preview(frame_generator(), num_frames, args.port, args.verbose)


if __name__ == "__main__":
    main()
