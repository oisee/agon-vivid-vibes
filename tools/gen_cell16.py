#!/usr/bin/env python3
"""Generate a spinning 16-cell (4D cross-polytope) wireframe.

The 16-cell has 8 vertices (±1 on each 4D axis) and 24 edges
(all non-antipodal pairs). 4D rotation in XW and YZ planes,
stereographic 4D→3D projection, then perspective 3D→2D.

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

# -- Camera / projection --
STEREO_DISTANCE = 2.5  # stereographic projection distance in 4D
FOV_FACTOR = 120        # perspective 3D→2D scale
CAMERA_Z = -4.0         # camera position

# -- 16-cell geometry --
# 8 vertices: ±1 on each of the 4 axes
VERTICES_4D = [
    ( 1,  0,  0,  0),
    (-1,  0,  0,  0),
    ( 0,  1,  0,  0),
    ( 0, -1,  0,  0),
    ( 0,  0,  1,  0),
    ( 0,  0, -1,  0),
    ( 0,  0,  0,  1),
    ( 0,  0,  0, -1),
]

# 24 edges: all pairs that are NOT antipodal (not opposite vertices)
EDGES = []
for i in range(8):
    for j in range(i + 1, 8):
        # Antipodal pairs are (0,1), (2,3), (4,5), (6,7)
        if not (i % 2 == 0 and j == i + 1):
            EDGES.append((i, j))


def hsl_to_agon(hue, lightness=0.6):
    """Map HSL to Agon 64-colour (2-2-2 RGB). Saturation assumed ~90%.

    hue: 0-360 degrees
    lightness: 0.0-1.0
    """
    hue = hue % 360
    s = 0.9
    c = (1 - abs(2 * lightness - 1)) * s
    x = c * (1 - abs((hue / 60) % 2 - 1))
    m = lightness - c / 2

    if hue < 60:
        r, g, b = c, x, 0
    elif hue < 120:
        r, g, b = x, c, 0
    elif hue < 180:
        r, g, b = 0, c, x
    elif hue < 240:
        r, g, b = 0, x, c
    elif hue < 300:
        r, g, b = x, 0, c
    else:
        r, g, b = c, 0, x

    r2 = min(3, int((r + m) * 4))
    g2 = min(3, int((g + m) * 4))
    b2 = min(3, int((b + m) * 4))
    return agon_rgb(r2, g2, b2)


def rotate_4d(verts, angle_xw, angle_yz):
    """Apply 4D rotation in XW and YZ planes."""
    cxw, sxw = math.cos(angle_xw), math.sin(angle_xw)
    cyz, syz = math.cos(angle_yz), math.sin(angle_yz)

    result = []
    for x, y, z, w in verts:
        # XW rotation
        x2 = x * cxw - w * sxw
        w2 = x * sxw + w * cxw
        # YZ rotation
        y2 = y * cyz - z * syz
        z2 = y * syz + z * cyz
        result.append((x2, y2, z2, w2))
    return result


def stereo_project(x, y, z, w):
    """Stereographic projection 4D→3D."""
    denom = STEREO_DISTANCE - w
    if abs(denom) < 0.01:
        denom = 0.01
    scale = STEREO_DISTANCE / denom
    return x * scale, y * scale, z * scale


def perspective_project(x3, y3, z3):
    """Perspective 3D→2D screen coordinates."""
    z = z3 - CAMERA_Z
    if z < 0.1:
        z = 0.1
    sx = int(x3 * FOV_FACTOR / z + SCREEN_W / 2)
    sy = int(-y3 * FOV_FACTOR / z + SCREEN_H / 2)
    sx = max(0, min(319, sx))
    sy = max(0, min(239, sy))
    return sx, sy


def generate_frame(frame_idx, total_frames):
    """Generate one frame of the 16-cell animation."""
    t = frame_idx / total_frames
    angle_xw = t * 2 * math.pi * 0.6 * (total_frames / 60)
    angle_yz = t * 2 * math.pi * 0.4 * (total_frames / 60)

    rotated = rotate_4d(VERTICES_4D, angle_xw, angle_yz)

    # Project to 3D then 2D
    projected_3d = [stereo_project(*v) for v in rotated]
    projected_2d = [perspective_project(*v) for v in projected_3d]
    w_values = [v[3] for v in rotated]

    s = VDPStream()
    s.clg()
    canvas_lines = []

    for i, j in EDGES:
        # Color based on average W depth of edge endpoints
        avg_w = (w_values[i] + w_values[j]) / 2
        hue = (avg_w + 1) * 180  # map -1..1 to 0..360
        lightness = 0.4 + 0.3 * (avg_w + 1) / 2  # 0.4..0.7
        col = hsl_to_agon(hue, lightness)

        x1, y1 = projected_2d[i]
        x2, y2 = projected_2d[j]

        s.gcol(0, col)
        s.line(x1, y1, x2, y2)

        canvas_lines.append({
            "color": agon_index_to_hex(col),
            "x1": x1, "y1": y1, "x2": x2, "y2": y2,
        })

    return s, canvas_lines


def generate_html(frames_data, filename):
    """Write a self-contained HTML canvas animation file."""
    html = f"""\
<!DOCTYPE html>
<html>
<head>
<meta charset="utf-8">
<title>Agon 16-Cell Preview</title>
<style>
  body {{ background: #111; color: #ccc; font-family: monospace; margin: 20px; }}
  canvas {{ border: 1px solid #444; image-rendering: pixelated; }}
  .controls {{ margin: 10px 0; }}
  button {{ font-family: monospace; font-size: 14px; padding: 4px 12px; margin-right: 8px; }}
  #info {{ margin-top: 8px; }}
</style>
</head>
<body>
<h3>Agon 16-Cell Preview (320x240, 64-colour palette)</h3>
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
  const lines = FRAMES[f];
  ctx.lineWidth = 2;
  for (const ln of lines) {{
    ctx.strokeStyle = ln.color;
    ctx.beginPath();
    ctx.moveTo(ln.x1 * SCALE, ln.y1 * SCALE);
    ctx.lineTo(ln.x2 * SCALE, ln.y2 * SCALE);
    ctx.stroke();
  }}
  info.textContent = `Frame ${{f + 1}} / ${{FRAMES.length}} (${{lines.length}} edges)`;
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
    print(f"[gen_cell16] Wrote HTML preview to {filename}", file=sys.stderr)


def write_vdp_file(filename, frames, mode, fps):
    """Write frames to a .vdp stream file."""
    with open(filename, "wb") as f:
        f.write(b"VDP1")
        f.write(struct.pack("<HHH", mode, len(frames), fps))
        for frame_data in frames:
            f.write(struct.pack("<H", len(frame_data)))
            f.write(frame_data)
    print(f"[gen_cell16] Wrote {len(frames)} frames to {filename}", file=sys.stderr)


def run_preview(frames_iter, total_frames, port, verbose=False):
    """Stream frames live to agon-vdp-sdl via fake_ez80 server."""
    from fake_ez80 import FakeEz80Server

    server = FakeEz80Server(port=port, verbose=verbose)
    server.start()

    init = VDPStream()
    init.general_poll()
    server.send_vdu(init.get_bytes())
    print("[gen_cell16] Sent General Poll", file=sys.stderr)

    for _ in range(5):
        server.wait_vsync()

    init = VDPStream()
    init.mode(SCREEN_MODE)
    init.set_logical_coords(False)
    init.cursor(False)
    server.send_vdu(init.get_bytes())
    print(f"[gen_cell16] Sent mode({SCREEN_MODE}) + pixel coords", file=sys.stderr)

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
                print(f"[gen_cell16] Frame {frame_num}/{total_frames} ({len(frame_bytes)}B)", file=sys.stderr)
    except KeyboardInterrupt:
        print(f"\n[gen_cell16] Stopped at frame {frame_num}", file=sys.stderr)
    finally:
        server.shutdown()


def main():
    parser = argparse.ArgumentParser(description="Generate 16-cell polytope wireframe")
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
    print(f"[gen_cell16] 16-cell: 8 vertices, {len(EDGES)} edges", file=sys.stderr)
    print(f"[gen_cell16] Generating {num_frames} frames...", file=sys.stderr)

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
        print(f"[gen_cell16] Generation took {elapsed:.1f}s", file=sys.stderr)
        generate_html(all_canvas_frames, args.html)
        if args.output:
            write_vdp_file(args.output, all_vdp_frames, SCREEN_MODE, args.fps)
    elif args.output:
        t0 = time.time()
        frame_bytes = []
        for i, (vdp_stream, _) in enumerate(frame_generator()):
            frame_bytes.append(vdp_stream.get_bytes())
        elapsed = time.time() - t0
        print(f"[gen_cell16] Generation took {elapsed:.1f}s", file=sys.stderr)
        write_vdp_file(args.output, frame_bytes, SCREEN_MODE, args.fps)

    if args.preview:
        run_preview(frame_generator(), num_frames, args.port, args.verbose)


if __name__ == "__main__":
    main()
