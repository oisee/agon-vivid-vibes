#!/usr/bin/env python3
"""Build compressed C torus player binary.

Pipeline: generate frames → parse → slot-major transpose → LZSS → build
"""

import math
import os
import struct
import subprocess
import sys
import time

import numpy as np

from gen_torus import make_torus_mesh, rotation_matrix, generate_frame
from compress_test import parse_vdu_frames
from lzss import lzss_compress

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
PLAYER_DIR = os.path.join(os.path.dirname(SCRIPT_DIR), "player")

NUM_FRAMES = 90
SEGMENTS = 8


def main():
    # Step 1: Generate frames
    print(f"[build] Generating {NUM_FRAMES} torus frames...", file=sys.stderr)
    t0 = time.time()
    vertices, triangles = make_torus_mesh(SEGMENTS, SEGMENTS)
    vdu_frames = []
    for i in range(NUM_FRAMES):
        angle_y = 2 * math.pi * i / NUM_FRAMES
        angle_x = 0.8 + 0.3 * math.sin(2 * math.pi * i / NUM_FRAMES * 2)
        rot = rotation_matrix(angle_x, angle_y)
        s, _ = generate_frame(vertices, triangles, rot)
        data = s.get_bytes()
        # Strip CLS+CLG, prepend just CLG
        stripped = data[2:] if data[0] == 12 and data[1] == 16 else data
        vdu_frames.append(bytes([16]) + stripped)
    print(f"[build] Generated in {time.time()-t0:.1f}s", file=sys.stderr)

    # Step 2: Parse into structured triangle data
    raw_blob = struct.pack("<H", NUM_FRAMES)
    for frame in vdu_frames:
        raw_blob += struct.pack("<H", len(frame)) + frame
    frames_structured = parse_vdu_frames(raw_blob)
    total_tris = sum(len(f) for f in frames_structured)
    max_tris = max(len(f) for f in frames_structured)
    print(f"[build] {NUM_FRAMES} frames, {total_tris} tris, max {max_tris}/frame",
          file=sys.stderr)

    # Step 3: Slot-major transpose (no delta — doesn't help for torus)
    def pad(tris):
        padded = list(tris)
        while len(padded) < max_tris:
            padded.append({"color": 0, "x1": 0, "y1": 0,
                           "x2": 0, "y2": 0, "x3": 0, "y3": 0})
        return padded

    padded = [pad(f) for f in frames_structured]
    tri_counts = bytearray(len(f) for f in frames_structured)
    colors = bytearray()
    x1_lo, x1_hi, y1 = bytearray(), bytearray(), bytearray()
    x2_lo, x2_hi, y2 = bytearray(), bytearray(), bytearray()
    x3_lo, x3_hi, y3 = bytearray(), bytearray(), bytearray()

    for slot in range(max_tris):
        for f in padded:
            t = f[slot]
            colors.append(t["color"])
            x1_lo.append(t["x1"] & 0xFF); x1_hi.append(t["x1"] >> 8)
            y1.append(t["y1"] & 0xFF)
            x2_lo.append(t["x2"] & 0xFF); x2_hi.append(t["x2"] >> 8)
            y2.append(t["y2"] & 0xFF)
            x3_lo.append(t["x3"] & 0xFF); x3_hi.append(t["x3"] >> 8)
            y3.append(t["y3"] & 0xFF)

    header = struct.pack("<HB", NUM_FRAMES, max_tris)
    transposed = header + bytes(tri_counts) + bytes(colors) + \
                 bytes(x1_lo) + bytes(x1_hi) + bytes(y1) + \
                 bytes(x2_lo) + bytes(x2_hi) + bytes(y2) + \
                 bytes(x3_lo) + bytes(x3_hi) + bytes(y3)
    print(f"[build] Transposed: {len(transposed):,}B", file=sys.stderr)

    # Step 4: LZSS compress
    compressed = lzss_compress(transposed)
    ratio = len(compressed) / len(raw_blob)
    print(f"[build] Compressed: {len(compressed):,}B "
          f"({ratio:.0%} of raw {len(raw_blob):,}B)", file=sys.stderr)

    # Step 5: Export blobs
    comp_path = os.path.join(PLAYER_DIR, "torus_compressed.bin")
    with open(comp_path, "wb") as f:
        decomp_size = len(transposed)
        f.write(struct.pack("<I", decomp_size)[:3])  # u24-LE
        f.write(compressed)
    print(f"[build] Wrote {os.path.getsize(comp_path):,}B → {comp_path}",
          file=sys.stderr)

    setup_path = os.path.join(PLAYER_DIR, "torus_setup.bin")
    setup = bytearray([22, 8, 23, 0, 0xC0, 0, 23, 1, 0, 12, 16])
    with open(setup_path, "wb") as f:
        f.write(setup)

    # Step 6: Build
    print("[build] Running make...", file=sys.stderr)
    env = os.environ.copy()
    env["PATH"] = "/Users/alice/dev/agondev/agondev/bin:" + env["PATH"]
    subprocess.run(["make", "clean"], cwd=PLAYER_DIR, env=env,
                   capture_output=True, text=True)
    result = subprocess.run(["make"], cwd=PLAYER_DIR, env=env,
                           capture_output=True, text=True)
    print(result.stdout, end="", file=sys.stderr)
    if result.stderr:
        print(result.stderr, end="", file=sys.stderr)
    if result.returncode != 0:
        print("[build] ERROR: make failed", file=sys.stderr)
        sys.exit(1)

    bin_path = os.path.join(PLAYER_DIR, "bin", "torusc.bin")
    print(f"[build] Success! {os.path.getsize(bin_path):,}B → {bin_path}",
          file=sys.stderr)

    # Copy to sdcard
    sdcard = "/Users/alice/dev/fab-agon-emulator/sdcard_local"
    if os.path.isdir(sdcard):
        import shutil
        shutil.copy2(bin_path, os.path.join(sdcard, "torusc.bin"))
        print(f"[build] Copied to {sdcard}/torusc.bin", file=sys.stderr)


if __name__ == "__main__":
    main()
