#!/usr/bin/env python3
"""Combined probe — runs all Tier 1 effects sequentially (60 frames each).

Useful for quick visual verification and frame dumping.

Outputs:
  --html FILE    self-contained HTML canvas animation
  --preview      stream live to agon-vdp-sdl via fake_ez80 TCP server
  --output FILE  save to a .vdp file
"""

import argparse
import json
import struct
import sys
import time

from vdp_stream import VDPStream

from gen_cell16 import generate_frame as cell16_frame
from gen_tesseract import generate_frame as tesseract_frame
from gen_glitch import generate_frame as glitch_frame
from gen_ignition import generate_frame as ignition_frame
from gen_sales_dance import generate_frame as sales_dance_frame

SCREEN_W = 320
SCREEN_H = 240
SCREEN_MODE = 8
FRAMES_PER_EFFECT = 60

EFFECTS = [
    ("cell16",      cell16_frame),
    ("tesseract",   tesseract_frame),
    ("glitch",      glitch_frame),
    ("ignition",    ignition_frame),
    ("sales_dance", sales_dance_frame),
]


def generate_all_frames():
    """Yield (vdp_stream, canvas_data, effect_name) for all effects."""
    for name, gen_fn in EFFECTS:
        total = FRAMES_PER_EFFECT
        # sales_dance needs more total_frames context for its phase logic
        effective_total = 360 if name == "sales_dance" else total
        for i in range(total):
            # For sales_dance, spread frames across all 3 phases
            if name == "sales_dance":
                # Map 60 frames → sample phases: 0-19=phase1, 20-39=phase2, 40-59=phase3
                if i < 20:
                    frame_idx = int(i * 89 / 19)       # phase 1: frames 0-89
                elif i < 40:
                    frame_idx = 90 + int((i - 20) * 89 / 19)  # phase 2: 90-179
                else:
                    frame_idx = 180 + int((i - 40) * 179 / 19)  # phase 3: 180-359
                vdp, canvas = gen_fn(frame_idx, effective_total)
            else:
                vdp, canvas = gen_fn(i, total)
            yield vdp, canvas, name


def generate_html(frames_data, effect_names, filename):
    """Write a self-contained HTML canvas animation file."""
    # Determine draw function based on what types each frame contains
    html = f"""\
<!DOCTYPE html>
<html>
<head>
<meta charset="utf-8">
<title>Agon Probe — All Effects</title>
<style>
  body {{ background: #111; color: #ccc; font-family: monospace; margin: 20px; }}
  canvas {{ border: 1px solid #444; image-rendering: pixelated; }}
  .controls {{ margin: 10px 0; }}
  button {{ font-family: monospace; font-size: 14px; padding: 4px 12px; margin-right: 8px; }}
  #info {{ margin-top: 8px; }}
</style>
</head>
<body>
<h3>Agon Probe — All Effects (320x240, 64-colour palette)</h3>
<canvas id="c" width="640" height="480"></canvas>
<div class="controls">
  <button id="playBtn">Pause</button>
  <button id="prevBtn">&larr; Prev</button>
  <button id="nextBtn">Next &rarr;</button>
  <span id="info">Frame 0 / 0</span>
</div>
<script>
const FRAMES = {json.dumps(frames_data, separators=(',', ':'))};
const NAMES = {json.dumps(effect_names, separators=(',', ':'))};
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
    if (op.x1 !== undefined && op.y1 !== undefined && op.x2 !== undefined && op.y2 !== undefined && op.type === undefined) {{
      // line (cell16/tesseract format)
      ctx.strokeStyle = op.color;
      ctx.lineWidth = 2;
      ctx.beginPath();
      ctx.moveTo(op.x1 * SCALE, op.y1 * SCALE);
      ctx.lineTo(op.x2 * SCALE, op.y2 * SCALE);
      ctx.stroke();
    }} else if (op.verts) {{
      // triangle
      ctx.fillStyle = op.color;
      ctx.beginPath();
      ctx.moveTo(op.verts[0][0] * SCALE, op.verts[0][1] * SCALE);
      ctx.lineTo(op.verts[1][0] * SCALE, op.verts[1][1] * SCALE);
      ctx.lineTo(op.verts[2][0] * SCALE, op.verts[2][1] * SCALE);
      ctx.closePath();
      ctx.fill();
    }} else if (op.type === 'rect') {{
      ctx.fillStyle = op.color;
      ctx.fillRect(op.x * SCALE, op.y * SCALE, op.w * SCALE, op.h * SCALE);
    }} else if (op.type === 'point') {{
      ctx.fillStyle = op.color;
      ctx.fillRect(op.x * SCALE, op.y * SCALE, SCALE, SCALE);
    }} else if (op.type === 'line') {{
      ctx.strokeStyle = op.color;
      ctx.lineWidth = 2;
      ctx.beginPath();
      ctx.moveTo(op.x1 * SCALE, op.y1 * SCALE);
      ctx.lineTo(op.x2 * SCALE, op.y2 * SCALE);
      ctx.stroke();
    }} else if (op.type === 'text') {{
      ctx.fillStyle = op.color;
      ctx.font = (16 * SCALE) + 'px monospace';
      ctx.fillText(op.text, op.x * SCALE * 8, op.y * SCALE * 8);
    }}
  }}
  info.textContent = `Frame ${{f + 1}} / ${{FRAMES.length}} — ${{NAMES[f]}} (${{ops.length}} ops)`;
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
    print(f"[gen_probe] Wrote HTML preview to {filename}", file=sys.stderr)


def write_vdp_file(filename, frames, mode, fps):
    with open(filename, "wb") as f:
        f.write(b"VDP1")
        f.write(struct.pack("<HHH", mode, len(frames), fps))
        for frame_data in frames:
            f.write(struct.pack("<H", len(frame_data)))
            f.write(frame_data)
    print(f"[gen_probe] Wrote {len(frames)} frames to {filename}", file=sys.stderr)


def run_preview(port, verbose=False):
    from fake_ez80 import FakeEz80Server

    server = FakeEz80Server(port=port, verbose=verbose)
    server.start()

    init = VDPStream()
    init.general_poll()
    server.send_vdu(init.get_bytes())
    print("[gen_probe] Sent General Poll", file=sys.stderr)

    for _ in range(5):
        server.wait_vsync()

    # Mode switch first — must complete before setting pixel coords
    init = VDPStream()
    init.mode(SCREEN_MODE)
    server.send_vdu(init.get_bytes())
    for _ in range(5):
        server.wait_vsync()

    # Now set pixel coords + hide cursor (mode is active)
    init = VDPStream()
    init.set_logical_coords(False)
    init.cursor(False)
    init.cls()
    init.clg()
    server.send_vdu(init.get_bytes())
    print(f"[gen_probe] Sent mode({SCREEN_MODE}) + pixel coords", file=sys.stderr)

    for _ in range(3):
        server.wait_vsync()

    frame_num = 0
    total = len(EFFECTS) * FRAMES_PER_EFFECT
    prev_name = None
    try:
        for vdp_stream, _canvas, name in generate_all_frames():
            if not server.connected:
                break
            if name != prev_name:
                if prev_name is not None:
                    print(f"[gen_probe] {prev_name} done — frame {frame_num}/{total}", file=sys.stderr)
                prev_name = name
            frame_bytes = vdp_stream.get_bytes()
            server.send_vdu(frame_bytes)
            server.wait_vsync()
            frame_num += 1
            if frame_num % FRAMES_PER_EFFECT == 0:
                print(f"[gen_probe] {name} done — frame {frame_num}/{total} ({len(frame_bytes)}B)", file=sys.stderr)
    except KeyboardInterrupt:
        print(f"\n[gen_probe] Stopped at frame {frame_num}", file=sys.stderr)
    finally:
        server.shutdown()


def main():
    parser = argparse.ArgumentParser(description="Combined probe — all Tier 1 effects")
    parser.add_argument("--html", type=str, default=None)
    parser.add_argument("--preview", action="store_true")
    parser.add_argument("--output", "-o", type=str, default=None)
    parser.add_argument("--port", type=int, default=5001)
    parser.add_argument("--fps", type=int, default=30)
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args()

    if not args.html and not args.preview and not args.output:
        parser.error("Specify --html FILE, --preview, and/or --output FILE")

    total = len(EFFECTS) * FRAMES_PER_EFFECT
    print(f"[gen_probe] {len(EFFECTS)} effects x {FRAMES_PER_EFFECT} frames = {total} total", file=sys.stderr)

    if args.html:
        t0 = time.time()
        all_canvas = []
        all_vdp = []
        all_names = []
        for vdp_stream, canvas_data, name in generate_all_frames():
            all_canvas.append(canvas_data)
            all_vdp.append(vdp_stream.get_bytes())
            all_names.append(name)
        elapsed = time.time() - t0
        print(f"[gen_probe] Generation took {elapsed:.1f}s", file=sys.stderr)
        generate_html(all_canvas, all_names, args.html)
        if args.output:
            write_vdp_file(args.output, all_vdp, SCREEN_MODE, args.fps)
    elif args.output:
        t0 = time.time()
        frame_bytes = []
        for vdp_stream, _, name in generate_all_frames():
            frame_bytes.append(vdp_stream.get_bytes())
        elapsed = time.time() - t0
        print(f"[gen_probe] Generation took {elapsed:.1f}s", file=sys.stderr)
        write_vdp_file(args.output, frame_bytes, SCREEN_MODE, args.fps)

    if args.preview:
        run_preview(args.port, args.verbose)


if __name__ == "__main__":
    main()
