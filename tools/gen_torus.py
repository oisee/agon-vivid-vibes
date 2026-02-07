#!/usr/bin/env python3
"""Generate a spinning torus (donut) rendered as filled triangles.

Outputs a VDP byte stream that can be:
  --preview    streamed live to agon-vdp-sdl via fake_ez80 TCP server
  --output F   saved to a .vdp file for later playback on real hardware

The torus is a parametric surface tessellated into triangles, with
painter's algorithm depth sorting and simple diffuse shading mapped
to the Agon 64-colour palette.
"""

import argparse
import json
import math
import struct
import sys
import time

import numpy as np

from vdp_stream import VDPStream

# -- Screen geometry (mode 8: 320x240, 64 colours) --
SCREEN_W = 320
SCREEN_H = 240
SCREEN_MODE = 8

# -- Torus parameters --
MAJOR_R = 1.0    # distance from centre of tube to centre of torus
MINOR_R = 0.4    # radius of tube

# -- Camera / projection --
CAMERA_Z = -3.0  # camera position (looking toward +Z)
FOV_FACTOR = 300  # perspective scale factor

# -- Lighting --
LIGHT_DIR = np.array([0.3, 0.5, -0.8])  # direction TO the light (near camera)
LIGHT_DIR /= np.linalg.norm(LIGHT_DIR)
AMBIENT = 0.15


def make_torus_mesh(segments_u: int, segments_v: int) -> tuple[np.ndarray, list[tuple[int, int, int]]]:
    """Generate torus vertices and triangle indices.

    Returns:
        vertices: (N, 3) array of 3D positions
        triangles: list of (i, j, k) index triples
    """
    verts = []
    for i in range(segments_u):
        theta = 2 * math.pi * i / segments_u
        cos_t, sin_t = math.cos(theta), math.sin(theta)
        for j in range(segments_v):
            phi = 2 * math.pi * j / segments_v
            cos_p, sin_p = math.cos(phi), math.sin(phi)
            # Parametric torus
            x = (MAJOR_R + MINOR_R * cos_p) * cos_t
            y = MINOR_R * sin_p
            z = (MAJOR_R + MINOR_R * cos_p) * sin_t
            verts.append((x, y, z))

    vertices = np.array(verts, dtype=np.float64)

    # Build triangle indices (two triangles per quad)
    triangles = []
    for i in range(segments_u):
        ni = (i + 1) % segments_u
        for j in range(segments_v):
            nj = (j + 1) % segments_v
            a = i * segments_v + j
            b = ni * segments_v + j
            c = ni * segments_v + nj
            d = i * segments_v + nj
            triangles.append((a, c, b))
            triangles.append((a, d, c))

    return vertices, triangles


def compute_normals(vertices: np.ndarray, triangles: list[tuple[int, int, int]]) -> np.ndarray:
    """Compute per-triangle face normals."""
    normals = np.zeros((len(triangles), 3))
    for idx, (a, b, c) in enumerate(triangles):
        v0, v1, v2 = vertices[a], vertices[b], vertices[c]
        edge1 = v1 - v0
        edge2 = v2 - v0
        n = np.cross(edge1, edge2)
        length = np.linalg.norm(n)
        if length > 1e-10:
            n /= length
        normals[idx] = n
    return normals


def rotation_matrix(angle_x: float, angle_y: float) -> np.ndarray:
    """Combined X then Y rotation matrix."""
    cx, sx = math.cos(angle_x), math.sin(angle_x)
    cy, sy = math.cos(angle_y), math.sin(angle_y)
    # Ry @ Rx
    return np.array([
        [cy,      sx * sy,  cx * sy],
        [0,       cx,       -sx],
        [-sy,     sx * cy,  cx * cy],
    ])


def project(point: np.ndarray) -> tuple[int, int]:
    """Perspective project a 3D point to screen coordinates (clamped)."""
    z = point[2] - CAMERA_Z
    if z < 0.1:
        z = 0.1
    x = int(point[0] * FOV_FACTOR / z + SCREEN_W / 2)
    y = int(-point[1] * FOV_FACTOR / z + SCREEN_H / 2)
    # Clamp to screen bounds to prevent VDP fill overflow
    x = max(-32, min(351, x))
    y = max(-32, min(271, y))
    return x, y


def luminance_to_colour(lum: float) -> int:
    """Map a 0..1 luminance value to an Agon 64-colour palette index.

    The Agon palette in mode 8 (64 colours) maps indices to a 2-2-2 RGB space:
      index = (R_2bit << 4) | (G_2bit << 2) | B_2bit
    where each component is 0-3.

    We map luminance to a warm-toned ramp (donut-coloured: orange-ish).
    """
    lum = max(0.0, min(1.0, lum))

    # Warm tint: boost red, moderate green, low blue
    r = min(3, int(lum * 4.5))      # 0-3, slightly boosted
    g = min(3, int(lum * 3.0))      # 0-3
    b = min(3, int(lum * 1.5))      # 0-2 mostly

    return (r << 4) | (g << 2) | b


def agon_colour_to_rgb(col: int) -> str:
    """Convert Agon 64-colour index to '#rrggbb' hex string."""
    r = (col >> 4) & 3
    g = (col >> 2) & 3
    b = col & 3
    return f"#{r * 85:02x}{g * 85:02x}{b * 85:02x}"


def generate_frame(vertices: np.ndarray,
                   triangles: list[tuple[int, int, int]],
                   rot: np.ndarray) -> tuple[VDPStream, list[dict]]:
    """Generate one frame as both VDP commands and canvas-friendly data."""
    transformed = (rot @ vertices.T).T
    normals = compute_normals(transformed, triangles)

    draw_list = []
    for idx, (a, b, c) in enumerate(triangles):
        v0, v1, v2 = transformed[a], transformed[b], transformed[c]
        avg_z = (v0[2] + v1[2] + v2[2]) / 3.0

        # Back-face culling: camera at CAMERA_Z (-4) looking toward +Z
        # Visible faces point TOWARD camera, i.e. normal.z < 0
        if normals[idx][2] >= 0:
            continue

        draw_list.append((avg_z, idx))

    draw_list.sort(key=lambda t: -t[0])

    s = VDPStream()
    s.cls()   # clear text area (prevents stray text)
    s.clg()   # clear graphics area
    canvas_tris = []

    for avg_z, idx in draw_list:
        a, b, c = triangles[idx]
        x1, y1 = project(transformed[a])
        x2, y2 = project(transformed[b])
        x3, y3 = project(transformed[c])

        dot = float(np.dot(normals[idx], LIGHT_DIR))
        lum = max(0.0, dot) * (1.0 - AMBIENT) + AMBIENT
        col = luminance_to_colour(lum)

        s.gcol(0, col)
        s.filled_triangle(x1, y1, x2, y2, x3, y3)

        canvas_tris.append({
            "color": agon_colour_to_rgb(col),
            "verts": [[x1, y1], [x2, y2], [x3, y3]],
        })

    return s, canvas_tris


def generate_html(frames_data: list[list[dict]], filename: str) -> None:
    """Write a self-contained HTML canvas animation file."""
    html = f"""\
<!DOCTYPE html>
<html>
<head>
<meta charset="utf-8">
<title>Agon Torus Preview</title>
<style>
  body {{ background: #111; color: #ccc; font-family: monospace; margin: 20px; }}
  canvas {{ border: 1px solid #444; image-rendering: pixelated; }}
  .controls {{ margin: 10px 0; }}
  button {{ font-family: monospace; font-size: 14px; padding: 4px 12px; margin-right: 8px; }}
  #info {{ margin-top: 8px; }}
</style>
</head>
<body>
<h3>Agon Torus Preview (320x240, 64-colour palette)</h3>
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
  const tris = FRAMES[f];
  for (const tri of tris) {{
    ctx.fillStyle = tri.color;
    ctx.beginPath();
    ctx.moveTo(tri.verts[0][0] * SCALE, tri.verts[0][1] * SCALE);
    ctx.lineTo(tri.verts[1][0] * SCALE, tri.verts[1][1] * SCALE);
    ctx.lineTo(tri.verts[2][0] * SCALE, tri.verts[2][1] * SCALE);
    ctx.closePath();
    ctx.fill();
    ctx.strokeStyle = tri.color;
    ctx.lineWidth = 1;
    ctx.stroke();
  }}
  info.textContent = `Frame ${{f + 1}} / ${{FRAMES.length}} (${{tris.length}} triangles)`;
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
    print(f"[gen_torus] Wrote HTML preview to {filename}", file=sys.stderr)


def write_vdp_file(filename: str, frames: list[bytes], mode: int, fps: int) -> None:
    """Write frames to a .vdp stream file.

    Format:
        Header:  "VDP1" (4B) | mode (u16) | frame_count (u16) | fps (u16)
        Per frame: byte_count (u16) | raw VDU bytes
    """
    with open(filename, "wb") as f:
        f.write(b"VDP1")
        f.write(struct.pack("<HHH", mode, len(frames), fps))
        for frame_data in frames:
            f.write(struct.pack("<H", len(frame_data)))
            f.write(frame_data)
    print(f"[gen_torus] Wrote {len(frames)} frames to {filename}", file=sys.stderr)


def run_preview(frames_iter, total_frames: int, port: int, verbose: bool = False) -> None:
    """Stream frames live to agon-vdp-sdl via fake_ez80 server."""
    from fake_ez80 import FakeEz80Server

    server = FakeEz80Server(port=port, verbose=verbose)
    server.start()

    # Send General Poll to unlock VDP firmware (it blocks until it gets this)
    init = VDPStream()
    init.general_poll()
    server.send_vdu(init.get_bytes())
    print("[gen_torus] Sent General Poll (VDP init handshake)", file=sys.stderr)

    # Wait for VDP to process the GP and become ready
    for _ in range(5):
        server.wait_vsync()

    # Set screen mode, pixel coordinates, hide cursor
    init = VDPStream()
    init.mode(SCREEN_MODE)
    init.set_logical_coords(False)  # pixel coords: origin top-left, Y down
    init.cursor(False)               # hide blinking text cursor
    server.send_vdu(init.get_bytes())
    print(f"[gen_torus] Sent mode({SCREEN_MODE}) + pixel coords + cursor off", file=sys.stderr)

    # Wait for mode switch to take effect (a few real vsyncs)
    for _ in range(5):
        server.wait_vsync()

    frame_num = 0
    try:
        for frame_stream, _canvas_tris in frames_iter:
            if not server.connected:
                break
            frame_bytes = frame_stream.get_bytes()
            server.send_vdu(frame_bytes)
            # Wait for VDP to finish drawing
            server.wait_vsync()
            server.wait_vsync()
            frame_num += 1
            if frame_num % 60 == 0:
                print(f"[gen_torus] Frame {frame_num}/{total_frames} ({len(frame_bytes)}B)", file=sys.stderr)
    except KeyboardInterrupt:
        print(f"\n[gen_torus] Stopped at frame {frame_num}", file=sys.stderr)
    finally:
        server.shutdown()


def main():
    parser = argparse.ArgumentParser(description="Generate spinning torus VDP stream")
    parser.add_argument("--html", type=str, default=None,
                        help="Write self-contained HTML canvas preview")
    parser.add_argument("--preview", action="store_true",
                        help="Stream live to agon-vdp-sdl via TCP")
    parser.add_argument("--output", "-o", type=str, default=None,
                        help="Save to .vdp file")
    parser.add_argument("--port", type=int, default=5001,
                        help="TCP port for preview (default: 5001)")
    parser.add_argument("--segments", type=int, default=8,
                        help="Torus tessellation segments (default: 8)")
    parser.add_argument("--frames", type=int, default=360,
                        help="Number of frames to generate (default: 360)")
    parser.add_argument("--fps", type=int, default=30,
                        help="Target FPS (default: 30)")
    parser.add_argument("-v", "--verbose", action="store_true",
                        help="Verbose protocol logging")
    args = parser.parse_args()

    if not args.html and not args.preview and not args.output:
        parser.error("Specify --html FILE, --preview, and/or --output FILE")

    seg = args.segments
    num_frames = args.frames
    print(f"[gen_torus] Torus: {seg}x{seg} grid = {2 * seg * seg} triangles", file=sys.stderr)
    print(f"[gen_torus] Generating {num_frames} frames...", file=sys.stderr)

    vertices, triangles = make_torus_mesh(seg, seg)

    def frame_generator():
        for i in range(num_frames):
            angle_y = 2 * math.pi * i / num_frames
            angle_x = 0.8 + 0.3 * math.sin(2 * math.pi * i / num_frames * 2)
            rot = rotation_matrix(angle_x, angle_y)
            yield generate_frame(vertices, triangles, rot)

    if args.html:
        t0 = time.time()
        all_canvas_frames = []
        all_vdp_frames = []
        for i, (vdp_stream, canvas_tris) in enumerate(frame_generator()):
            all_canvas_frames.append(canvas_tris)
            all_vdp_frames.append(vdp_stream.get_bytes())
            if (i + 1) % 60 == 0:
                print(f"[gen_torus] Generated {i + 1}/{num_frames} frames", file=sys.stderr)
        elapsed = time.time() - t0
        print(f"[gen_torus] Generation took {elapsed:.1f}s", file=sys.stderr)
        generate_html(all_canvas_frames, args.html)

        if args.output:
            write_vdp_file(args.output, all_vdp_frames, SCREEN_MODE, args.fps)

    elif args.output:
        t0 = time.time()
        frame_bytes = []
        for i, (vdp_stream, _canvas_tris) in enumerate(frame_generator()):
            frame_bytes.append(vdp_stream.get_bytes())
            if (i + 1) % 60 == 0:
                print(f"[gen_torus] Generated {i + 1}/{num_frames} frames", file=sys.stderr)
        elapsed = time.time() - t0
        print(f"[gen_torus] Generation took {elapsed:.1f}s", file=sys.stderr)
        write_vdp_file(args.output, frame_bytes, SCREEN_MODE, args.fps)

    if args.preview:
        run_preview(frame_generator(), num_frames, args.port, args.verbose)


if __name__ == "__main__":
    main()
