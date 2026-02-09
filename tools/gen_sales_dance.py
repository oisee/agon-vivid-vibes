#!/usr/bin/env python3
"""Generate an animated bar chart that transitions from corporate to dance party.

Three-phase state machine:
  Phase 1 (frames 0-89):   4 bars grow with staggered easing — corporate gray
  Phase 2 (frames 90-179): 4→32 bars subdivide, brightening
  Phase 3 (frames 180+):   32 bars dance with dual sine waves, rainbow HSL colours

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

# -- Bar chart layout --
MARGIN_X = 10
MARGIN_BOTTOM = 20
BAR_AREA_W = SCREEN_W - 2 * MARGIN_X
BAR_AREA_H = SCREEN_H - MARGIN_BOTTOM - 10  # top margin of 10
BASE_Y = SCREEN_H - MARGIN_BOTTOM

# -- Phase boundaries --
PHASE1_END = 90
PHASE2_END = 180

# -- Phase 1 target heights (4 bars) --
TARGET_HEIGHTS = [0.6, 0.85, 0.4, 0.95]


def hsl_to_agon(hue, lightness=0.5):
    """Map HSL to Agon 64-colour (2-2-2 RGB). Saturation ~90%."""
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


def ease_out_cubic(t):
    """Cubic ease-out: fast start, slow finish."""
    return 1 - (1 - t) ** 3


def gray_colour(brightness):
    """Map 0-1 brightness to Agon gray (equal R=G=B)."""
    v = min(3, int(brightness * 4))
    return agon_rgb(v, v, v)


def generate_frame(frame_idx, total_frames):
    """Generate one frame of the sales dance animation."""
    s = VDPStream()
    s.clg()
    canvas_ops = []

    if frame_idx < PHASE1_END:
        # -- Phase 1: 4 bars growing with staggered easing --
        num_bars = 4
        bar_w = BAR_AREA_W // num_bars - 2
        gap = 2

        for i in range(num_bars):
            # Stagger: each bar starts 10 frames after the previous
            local_t = (frame_idx - i * 10) / (PHASE1_END - i * 10)
            local_t = max(0.0, min(1.0, local_t))
            eased = ease_out_cubic(local_t)

            h = int(TARGET_HEIGHTS[i] * BAR_AREA_H * eased)
            if h < 1:
                continue

            x1 = MARGIN_X + i * (bar_w + gap)
            x2 = x1 + bar_w
            y1 = BASE_Y - h
            y2 = BASE_Y

            # Corporate gray: brightness proportional to height
            brightness = 0.3 + 0.4 * TARGET_HEIGHTS[i]
            col = gray_colour(brightness)

            s.gcol(0, col)
            s.filled_rect(x1, y1, x2, y2)
            canvas_ops.append({
                "type": "rect",
                "color": agon_index_to_hex(col),
                "x": x1, "y": y1, "w": x2 - x1, "h": y2 - y1,
            })

    elif frame_idx < PHASE2_END:
        # -- Phase 2: subdivide 4→32 bars, brightening --
        phase_t = (frame_idx - PHASE1_END) / (PHASE2_END - PHASE1_END)
        # Interpolate bar count: 4 → 32
        num_bars = 4 + int(phase_t * 28)
        bar_w = max(2, BAR_AREA_W // num_bars - 1)
        gap = 1

        for i in range(num_bars):
            # Height: interpolate from parent bar heights
            parent_idx = min(3, int(i * 4 / num_bars))
            h_ratio = TARGET_HEIGHTS[parent_idx]
            # Add some variation
            variation = 0.1 * math.sin(i * 0.8 + phase_t * 3)
            h = int((h_ratio + variation) * BAR_AREA_H)
            h = max(2, min(BAR_AREA_H, h))

            x1 = MARGIN_X + i * (bar_w + gap)
            x2 = min(SCREEN_W - MARGIN_X, x1 + bar_w)
            y1 = BASE_Y - h
            y2 = BASE_Y

            if x1 >= SCREEN_W - MARGIN_X:
                break

            # Brightening gray
            brightness = 0.3 + 0.5 * phase_t + 0.1 * (i % 4) / 4
            col = gray_colour(min(1.0, brightness))

            s.gcol(0, col)
            s.filled_rect(x1, y1, x2, y2)
            canvas_ops.append({
                "type": "rect",
                "color": agon_index_to_hex(col),
                "x": x1, "y": y1, "w": x2 - x1, "h": y2 - y1,
            })

    else:
        # -- Phase 3: 32 bars dancing with dual sine waves, rainbow colours --
        num_bars = 32
        bar_w = max(2, BAR_AREA_W // num_bars - 1)
        gap = 1
        dance_t = (frame_idx - PHASE2_END) / 30.0  # time in seconds

        for i in range(num_bars):
            # Dual sine wave for height
            wave1 = math.sin(dance_t * 3.0 + i * 0.4) * 0.3
            wave2 = math.sin(dance_t * 5.0 + i * 0.7) * 0.2
            base_h = 0.5 + wave1 + wave2
            base_h = max(0.05, min(1.0, base_h))

            h = int(base_h * BAR_AREA_H)
            x1 = MARGIN_X + i * (bar_w + gap)
            x2 = min(SCREEN_W - MARGIN_X, x1 + bar_w)
            y1 = BASE_Y - h
            y2 = BASE_Y

            if x1 >= SCREEN_W - MARGIN_X:
                break

            # Rainbow HSL: hue cycles across bars and over time
            hue = (i * 360 / num_bars + dance_t * 60) % 360
            lightness = 0.4 + 0.2 * base_h
            col = hsl_to_agon(hue, lightness)

            s.gcol(0, col)
            s.filled_rect(x1, y1, x2, y2)
            canvas_ops.append({
                "type": "rect",
                "color": agon_index_to_hex(col),
                "x": x1, "y": y1, "w": x2 - x1, "h": y2 - y1,
            })

    return s, canvas_ops


def generate_html(frames_data, filename):
    """Write a self-contained HTML canvas animation file."""
    html = f"""\
<!DOCTYPE html>
<html>
<head>
<meta charset="utf-8">
<title>Agon Sales Dance Preview</title>
<style>
  body {{ background: #111; color: #ccc; font-family: monospace; margin: 20px; }}
  canvas {{ border: 1px solid #444; image-rendering: pixelated; }}
  .controls {{ margin: 10px 0; }}
  button {{ font-family: monospace; font-size: 14px; padding: 4px 12px; margin-right: 8px; }}
  #info {{ margin-top: 8px; }}
</style>
</head>
<body>
<h3>Agon Sales Dance Preview (320x240, 64-colour palette)</h3>
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
    ctx.fillStyle = op.color;
    ctx.fillRect(op.x * SCALE, op.y * SCALE, op.w * SCALE, op.h * SCALE);
  }}
  info.textContent = `Frame ${{f + 1}} / ${{FRAMES.length}} (${{ops.length}} bars)`;
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
    print(f"[gen_sales_dance] Wrote HTML preview to {filename}", file=sys.stderr)


def write_vdp_file(filename, frames, mode, fps):
    """Write frames to a .vdp stream file."""
    with open(filename, "wb") as f:
        f.write(b"VDP1")
        f.write(struct.pack("<HHH", mode, len(frames), fps))
        for frame_data in frames:
            f.write(struct.pack("<H", len(frame_data)))
            f.write(frame_data)
    print(f"[gen_sales_dance] Wrote {len(frames)} frames to {filename}", file=sys.stderr)


def run_preview(frames_iter, total_frames, port, verbose=False):
    """Stream frames live to agon-vdp-sdl via fake_ez80 server."""
    from fake_ez80 import FakeEz80Server

    server = FakeEz80Server(port=port, verbose=verbose)
    server.start()

    init = VDPStream()
    init.general_poll()
    server.send_vdu(init.get_bytes())
    print("[gen_sales_dance] Sent General Poll", file=sys.stderr)

    for _ in range(5):
        server.wait_vsync()

    init = VDPStream()
    init.mode(SCREEN_MODE)
    init.set_logical_coords(False)
    init.cursor(False)
    server.send_vdu(init.get_bytes())
    print(f"[gen_sales_dance] Sent mode({SCREEN_MODE}) + pixel coords", file=sys.stderr)

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
                print(f"[gen_sales_dance] Frame {frame_num}/{total_frames} ({len(frame_bytes)}B)", file=sys.stderr)
    except KeyboardInterrupt:
        print(f"\n[gen_sales_dance] Stopped at frame {frame_num}", file=sys.stderr)
    finally:
        server.shutdown()


def main():
    parser = argparse.ArgumentParser(description="Generate animated sales dance bar chart")
    parser.add_argument("--html", type=str, default=None,
                        help="Write self-contained HTML canvas preview")
    parser.add_argument("--preview", action="store_true",
                        help="Stream live to agon-vdp-sdl via TCP")
    parser.add_argument("--output", "-o", type=str, default=None,
                        help="Save to .vdp file")
    parser.add_argument("--port", type=int, default=5001)
    parser.add_argument("--frames", type=int, default=360)
    parser.add_argument("--fps", type=int, default=30)
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args()

    if not args.html and not args.preview and not args.output:
        parser.error("Specify --html FILE, --preview, and/or --output FILE")

    num_frames = args.frames
    print(f"[gen_sales_dance] 3-phase bar chart: grow → subdivide → dance", file=sys.stderr)
    print(f"[gen_sales_dance] Generating {num_frames} frames...", file=sys.stderr)

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
        print(f"[gen_sales_dance] Generation took {elapsed:.1f}s", file=sys.stderr)
        generate_html(all_canvas_frames, args.html)
        if args.output:
            write_vdp_file(args.output, all_vdp_frames, SCREEN_MODE, args.fps)
    elif args.output:
        t0 = time.time()
        frame_bytes = []
        for i, (vdp_stream, _) in enumerate(frame_generator()):
            frame_bytes.append(vdp_stream.get_bytes())
        elapsed = time.time() - t0
        print(f"[gen_sales_dance] Generation took {elapsed:.1f}s", file=sys.stderr)
        write_vdp_file(args.output, frame_bytes, SCREEN_MODE, args.fps)

    if args.preview:
        run_preview(frame_generator(), num_frames, args.port, args.verbose)


if __name__ == "__main__":
    main()
