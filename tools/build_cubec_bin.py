#!/usr/bin/env python3
"""Build the C cube player binary with LZSS-compressed transposed frame data.

Pipeline:
  1. Generate cube frames (reuses gen_cube.py)
  2. Parse VDU frames into structured triangle data
  3. Transpose by field (colors, coordinates grouped by column)
  4. LZSS compress the transposed blob
  5. Export compressed blob + setup blob
  6. Build with agondev (make)

Usage:
  python build_cubec_bin.py [--frames 360]
"""

import argparse
import math
import os
import struct
import subprocess
import sys
import time

import numpy as np

from gen_cube import make_cube_mesh, rotation_matrix, generate_frame
from compress_test import parse_vdu_frames
from lzss import lzss_compress

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
PLAYER_DIR = os.path.join(os.path.dirname(SCRIPT_DIR), "player")


def generate_all_frames(num_frames):
    """Generate all cube VDU frame byte blobs."""
    vertices, triangles, face_ids = make_cube_mesh()
    frames = []
    for i in range(num_frames):
        angle_y = 2 * math.pi * i / num_frames
        angle_x = 0.3 * math.sin(2 * math.pi * i / num_frames * 2)
        rot = rotation_matrix(angle_x, angle_y)
        vdp_stream, _canvas = generate_frame(vertices, triangles, face_ids, rot)
        frames.append(vdp_stream.get_bytes())
    return frames


def frames_to_raw_blob(frames):
    """Pack frames into raw blob: [num_frames:u16-LE][len:u16-LE][data...]..."""
    parts = [struct.pack("<H", len(frames))]
    for frame_bytes in frames:
        parts.append(struct.pack("<H", len(frame_bytes)))
        parts.append(frame_bytes)
    return b"".join(parts)


def transpose_frames(frames_structured):
    """Transpose structured triangle data into slot-major columnar layout
    with delta encoding.

    Slot-major means: for each triangle slot (0..max_tris-1), store that
    slot's value across ALL frames consecutively. This groups the same
    logical triangle over time, making delta encoding very effective
    (slowly changing coordinates → lots of zeros/small deltas).

    Blob format:
      [num_frames:u16-LE]
      [max_tris:u8]           — max triangles per frame (padded slots)
      [tri_counts: num_frames bytes]   — actual triangle count per frame
      Then 10 delta-encoded columns, each of length max_tris * num_frames:
        [colors] [x1_lo] [x1_hi] [y1] [x2_lo] [x2_hi] [y2]
        [x3_lo] [x3_hi] [y3]
    """
    num_frames = len(frames_structured)
    max_tris = max(len(f) for f in frames_structured)

    # Pad each frame to max_tris with zeros
    def pad(tris):
        padded = list(tris)
        while len(padded) < max_tris:
            padded.append({"color": 0, "x1": 0, "y1": 0,
                           "x2": 0, "y2": 0, "x3": 0, "y3": 0})
        return padded

    padded_frames = [pad(f) for f in frames_structured]

    # Build columns in slot-major order:
    #   slot0_frame0, slot0_frame1, ..., slot1_frame0, slot1_frame1, ...
    tri_counts = bytearray(len(f) for f in frames_structured)
    colors = bytearray()
    x1_lo, x1_hi, y1 = bytearray(), bytearray(), bytearray()
    x2_lo, x2_hi, y2 = bytearray(), bytearray(), bytearray()
    x3_lo, x3_hi, y3 = bytearray(), bytearray(), bytearray()

    for slot in range(max_tris):
        for f in padded_frames:
            t = f[slot]
            colors.append(t["color"])
            x1_lo.append(t["x1"] & 0xFF); x1_hi.append(t["x1"] >> 8)
            y1.append(t["y1"] & 0xFF)
            x2_lo.append(t["x2"] & 0xFF); x2_hi.append(t["x2"] >> 8)
            y2.append(t["y2"] & 0xFF)
            x3_lo.append(t["x3"] & 0xFF); x3_hi.append(t["x3"] >> 8)
            y3.append(t["y3"] & 0xFF)

    def delta_encode(data):
        if not data:
            return bytes()
        out = bytearray([data[0]])
        for i in range(1, len(data)):
            out.append((data[i] - data[i - 1]) & 0xFF)
        return bytes(out)

    header = struct.pack("<HB", num_frames, max_tris)
    columns = [colors, x1_lo, x1_hi, y1, x2_lo, x2_hi, y2, x3_lo, x3_hi, y3]
    delta_columns = [delta_encode(col) for col in columns]

    blob = header + bytes(tri_counts) + b"".join(delta_columns)
    return blob


def export_setup_blob(setup_path):
    """Export VDU setup sequence as a binary blob."""
    setup = bytearray()
    setup.extend([22, 8])           # VDU 22, 8 — mode 8 (320x240, 64 colours)
    setup.extend([23, 0, 0xC0, 0])  # pixel coordinates
    setup.extend([23, 1, 0])        # cursor off
    setup.append(12)                # CLS
    setup.append(16)                # CLG
    with open(setup_path, "wb") as f:
        f.write(setup)
    return len(setup)


def main():
    parser = argparse.ArgumentParser(
        description="Build compressed C cube player binary")
    parser.add_argument("--frames", type=int, default=360)
    args = parser.parse_args()

    # Step 1: Generate frames
    print(f"[build] Generating {args.frames} frames...", file=sys.stderr)
    t0 = time.time()
    vdu_frames = generate_all_frames(args.frames)
    print(f"[build] Generated in {time.time()-t0:.1f}s", file=sys.stderr)

    # Step 2: Parse into structured data
    raw_blob = frames_to_raw_blob(vdu_frames)
    frames_structured = parse_vdu_frames(raw_blob)
    total_tris = sum(len(f) for f in frames_structured)
    print(f"[build] {len(frames_structured)} frames, {total_tris} triangles",
          file=sys.stderr)

    # Step 3: Transpose
    transposed = transpose_frames(frames_structured)
    print(f"[build] Transposed: {len(transposed):,}B", file=sys.stderr)

    # Step 4: LZSS compress
    compressed = lzss_compress(transposed)
    ratio = len(compressed) / len(raw_blob)
    print(f"[build] Compressed: {len(compressed):,}B "
          f"({ratio:.0%} of raw {len(raw_blob):,}B)", file=sys.stderr)

    # Step 5: Export blobs to player directory
    comp_path = os.path.join(PLAYER_DIR, "cube_compressed.bin")
    with open(comp_path, "wb") as f:
        # Header: [decompressed_size:u24-LE] then compressed data
        decompressed_size = len(transposed)
        f.write(struct.pack("<I", decompressed_size)[:3])  # u24-LE
        f.write(compressed)
    print(f"[build] Wrote {os.path.getsize(comp_path):,}B → {comp_path}",
          file=sys.stderr)

    setup_path = os.path.join(PLAYER_DIR, "cube_setup.bin")
    export_setup_blob(setup_path)

    # Step 6: Build
    print("[build] Running make...", file=sys.stderr)
    env = os.environ.copy()
    env["PATH"] = "/Users/alice/dev/agondev/agondev/bin:" + env["PATH"]
    result = subprocess.run(["make", "clean"], cwd=PLAYER_DIR, env=env,
                          capture_output=True, text=True)
    result = subprocess.run(["make"], cwd=PLAYER_DIR, env=env,
                          capture_output=True, text=True)
    print(result.stdout, end="", file=sys.stderr)
    if result.stderr:
        print(result.stderr, end="", file=sys.stderr)
    if result.returncode != 0:
        print("[build] ERROR: make failed", file=sys.stderr)
        sys.exit(1)

    bin_path = os.path.join(PLAYER_DIR, "bin", "cubec.bin")
    print(f"[build] Success! {os.path.getsize(bin_path):,}B → {bin_path}",
          file=sys.stderr)

    # Copy to sdcard
    sdcard = "/Users/alice/dev/fab-agon-emulator/sdcard_local"
    if os.path.isdir(sdcard):
        import shutil
        dest = os.path.join(sdcard, "cubec.bin")
        shutil.copy2(bin_path, dest)
        print(f"[build] Copied to {dest}", file=sys.stderr)


if __name__ == "__main__":
    main()
