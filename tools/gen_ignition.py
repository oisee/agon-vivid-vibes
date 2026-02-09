#!/usr/bin/env python3
"""Generate an expanding particle burst / ignition effect.

Particles expand radially from center, with outer and inner rays.
Fire-coloured palette (reds, oranges, yellows).

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
CX = SCREEN_W // 2
CY = SCREEN_H // 2

# -- Particle parameters --
NUM_PARTICLES = 150
WRAP_RADIUS = 150
NUM_OUTER_RAYS = 8
NUM_INNER_RAYS = 6


def fire_colour(distance_ratio):
    """Map distance ratio (0=center, 1=far) to fire-coloured Agon palette.

    Near center: bright yellow/white. Far: dim red/dark.
    """
    # R is always high (2-3), G fades, B is near-zero
    r = 3
    g = max(0, min(3, int((1.0 - distance_ratio) * 3.5)))
    b = max(0, min(1, int((1.0 - distance_ratio) * 1.5)))
    return agon_rgb(r, g, b)


def generate_frame(frame_idx, total_frames):
    """Generate one frame of the ignition effect."""
    t = frame_idx / 30.0  # time in seconds at 30fps

    s = VDPStream()
    s.clg()
    canvas_ops = []

    # -- Pulsating glow radius --
    glow_r = 20 + math.sin(t * 10) * 5

    # -- Particles --
    for i in range(NUM_PARTICLES):
        # Each particle has a fixed angle and speed, determined by index
        angle = (i * 2.399) + i * 0.1  # golden-angle-ish spread
        speed = 20 + (i * 7919 % 100)  # pseudo-random speed 20-120
        base_r = (speed * t * 0.8) % WRAP_RADIUS

        px = CX + int(math.cos(angle) * base_r)
        py = CY + int(math.sin(angle) * base_r)

        # Skip if off screen
        if px < 0 or px >= SCREEN_W or py < 0 or py >= SCREEN_H:
            continue

        dist_ratio = base_r / WRAP_RADIUS
        col = fire_colour(dist_ratio)

        s.gcol(0, col)
        s.point(px, py)
        canvas_ops.append({
            "type": "point",
            "color": agon_index_to_hex(col),
            "x": px, "y": py,
        })

    # -- Outer rays (orange, fixed angles) --
    outer_col = agon_rgb(3, 1, 0)  # orange
    ray_len = glow_r + 30
    s.gcol(0, outer_col)
    for i in range(NUM_OUTER_RAYS):
        angle = i * (2 * math.pi / NUM_OUTER_RAYS)
        ex = CX + int(math.cos(angle) * ray_len)
        ey = CY + int(math.sin(angle) * ray_len)
        s.line(CX, CY, ex, ey)
        canvas_ops.append({
            "type": "line",
            "color": agon_index_to_hex(outer_col),
            "x1": CX, "y1": CY, "x2": ex, "y2": ey,
        })

    # -- Inner rotating rays (yellow) --
    inner_col = agon_rgb(3, 3, 0)  # yellow
    inner_len = glow_r
    rot_offset = t * 2.0  # rotate at 2 rad/s
    s.gcol(0, inner_col)
    for i in range(NUM_INNER_RAYS):
        angle = rot_offset + i * (2 * math.pi / NUM_INNER_RAYS)
        ex = CX + int(math.cos(angle) * inner_len)
        ey = CY + int(math.sin(angle) * inner_len)
        s.line(CX, CY, ex, ey)
        canvas_ops.append({
            "type": "line",
            "color": agon_index_to_hex(inner_col),
            "x1": CX, "y1": CY, "x2": ex, "y2": ey,
        })

    # -- Center glow (bright white/yellow filled rect) --
    glow_half = int(glow_r * 0.3)
    glow_col = agon_rgb(3, 3, 2)  # bright warm white
    s.gcol(0, glow_col)
    s.filled_rect(CX - glow_half, CY - glow_half,
                  CX + glow_half, CY + glow_half)
    canvas_ops.append({
        "type": "rect",
        "color": agon_index_to_hex(glow_col),
        "x": CX - glow_half, "y": CY - glow_half,
        "w": glow_half * 2, "h": glow_half * 2,
    })

    return s, canvas_ops


def generate_html(frames_data, filename):
    """Write a self-contained HTML canvas animation file."""
    html = f"""\
<!DOCTYPE html>
<html>
<head>
<meta charset="utf-8">
<title>Agon Ignition Preview</title>
<style>
  body {{ background: #111; color: #ccc; font-family: monospace; margin: 20px; }}
  canvas {{ border: 1px solid #444; image-rendering: pixelated; }}
  .controls {{ margin: 10px 0; }}
  button {{ font-family: monospace; font-size: 14px; padding: 4px 12px; margin-right: 8px; }}
  #info {{ margin-top: 8px; }}
</style>
</head>
<body>
<h3>Agon Ignition Preview (320x240, 64-colour palette)</h3>
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
    if (op.type === 'point') {{
      ctx.fillStyle = op.color;
      ctx.fillRect(op.x * SCALE, op.y * SCALE, SCALE, SCALE);
    }} else if (op.type === 'line') {{
      ctx.strokeStyle = op.color;
      ctx.lineWidth = 2;
      ctx.beginPath();
      ctx.moveTo(op.x1 * SCALE, op.y1 * SCALE);
      ctx.lineTo(op.x2 * SCALE, op.y2 * SCALE);
      ctx.stroke();
    }} else if (op.type === 'rect') {{
      ctx.fillStyle = op.color;
      ctx.fillRect(op.x * SCALE, op.y * SCALE, op.w * SCALE, op.h * SCALE);
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
    print(f"[gen_ignition] Wrote HTML preview to {filename}", file=sys.stderr)


def write_vdp_file(filename, frames, mode, fps):
    """Write frames to a .vdp stream file."""
    with open(filename, "wb") as f:
        f.write(b"VDP1")
        f.write(struct.pack("<HHH", mode, len(frames), fps))
        for frame_data in frames:
            f.write(struct.pack("<H", len(frame_data)))
            f.write(frame_data)
    print(f"[gen_ignition] Wrote {len(frames)} frames to {filename}", file=sys.stderr)


def run_preview(frames_iter, total_frames, port, verbose=False):
    """Stream frames live to agon-vdp-sdl via fake_ez80 server."""
    from fake_ez80 import FakeEz80Server

    server = FakeEz80Server(port=port, verbose=verbose)
    server.start()

    init = VDPStream()
    init.general_poll()
    server.send_vdu(init.get_bytes())
    print("[gen_ignition] Sent General Poll", file=sys.stderr)

    for _ in range(5):
        server.wait_vsync()

    init = VDPStream()
    init.mode(SCREEN_MODE)
    init.set_logical_coords(False)
    init.cursor(False)
    server.send_vdu(init.get_bytes())
    print(f"[gen_ignition] Sent mode({SCREEN_MODE}) + pixel coords", file=sys.stderr)

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
                print(f"[gen_ignition] Frame {frame_num}/{total_frames} ({len(frame_bytes)}B)", file=sys.stderr)
    except KeyboardInterrupt:
        print(f"\n[gen_ignition] Stopped at frame {frame_num}", file=sys.stderr)
    finally:
        server.shutdown()


def main():
    parser = argparse.ArgumentParser(description="Generate ignition particle burst")
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
    print(f"[gen_ignition] Ignition: {NUM_PARTICLES} particles, {NUM_OUTER_RAYS}+{NUM_INNER_RAYS} rays", file=sys.stderr)
    print(f"[gen_ignition] Generating {num_frames} frames...", file=sys.stderr)

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
        print(f"[gen_ignition] Generation took {elapsed:.1f}s", file=sys.stderr)
        generate_html(all_canvas_frames, args.html)
        if args.output:
            write_vdp_file(args.output, all_vdp_frames, SCREEN_MODE, args.fps)
    elif args.output:
        t0 = time.time()
        frame_bytes = []
        for i, (vdp_stream, _) in enumerate(frame_generator()):
            frame_bytes.append(vdp_stream.get_bytes())
        elapsed = time.time() - t0
        print(f"[gen_ignition] Generation took {elapsed:.1f}s", file=sys.stderr)
        write_vdp_file(args.output, frame_bytes, SCREEN_MODE, args.fps)

    if args.preview:
        run_preview(frame_generator(), num_frames, args.port, args.verbose)


if __name__ == "__main__":
    main()
