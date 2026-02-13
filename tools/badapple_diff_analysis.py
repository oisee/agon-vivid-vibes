#!/usr/bin/env python3
"""Bad Apple frame diff analysis — measure tile changes between frames.

Computes per-frame diff stats and encoding cost estimates.
"""

import argparse
import os
import subprocess
import sys
import tempfile
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


def frame_to_tilemap(path, tile_size=8, threshold=128):
    """Convert frame to array of tile keys (one per grid position)."""
    img = Image.open(path).convert("L")
    arr = np.array(img)
    binary = (arr >= threshold).astype(np.uint8)
    h, w = binary.shape
    rows = h // tile_size
    cols = w // tile_size
    tilemap = []
    for ty in range(rows):
        for tx in range(cols):
            tile = binary[ty*tile_size:(ty+1)*tile_size,
                          tx*tile_size:(tx+1)*tile_size]
            # Pack to 8 bytes
            key = bytes(
                sum(tile[r, c] << (7 - c) for c in range(8))
                for r in range(8)
            )
            tilemap.append(key)
    return tilemap


def build_codebook(all_tilemaps, codebook_size=256):
    """Build frequency-based codebook from all frames."""
    counter = Counter()
    for tm in all_tilemaps:
        counter.update(tm)
    top = counter.most_common(codebook_size)
    key_to_id = {k: i for i, (k, _) in enumerate(top)}
    return key_to_id, counter


def hamming_distance(a, b):
    return sum(bin(x ^ y).count('1') for x, y in zip(a, b))


def nearest_codebook_entry(key, codebook_keys):
    best_dist = 64
    best_key = codebook_keys[0]
    for cb_key in codebook_keys:
        d = hamming_distance(key, cb_key)
        if d < best_dist:
            best_dist = d
            best_key = cb_key
        if d == 0:
            break
    return best_key, best_dist


def encode_tilemap(tilemap, key_to_id, codebook_keys):
    """Map each tile to codebook ID (nearest match if not exact)."""
    ids = []
    for key in tilemap:
        if key in key_to_id:
            ids.append(key_to_id[key])
        else:
            nearest, _ = nearest_codebook_entry(key, codebook_keys)
            ids.append(key_to_id[nearest])
    return ids


def main():
    parser = argparse.ArgumentParser(description="Bad Apple diff analysis")
    parser.add_argument("--video", type=str, default=str(VIDEO_PATH))
    parser.add_argument("--width", type=int, default=256)
    parser.add_argument("--height", type=int, default=192)
    parser.add_argument("--tile", type=int, default=8)
    parser.add_argument("--codebook", type=int, default=256)
    args = parser.parse_args()

    tiles_per_frame = (args.width // args.tile) * (args.height // args.tile)

    with tempfile.TemporaryDirectory(prefix="badapple_diff_") as tmpdir:
        print("Extracting frames...", file=sys.stderr)
        frames = extract_frames(args.video, args.width, args.height, tmpdir)
        num_frames = len(frames)
        print(f"{num_frames} frames, {tiles_per_frame} tiles/frame", file=sys.stderr)

        # Pass 1: build all tilemaps and codebook
        print("Building tilemaps...", file=sys.stderr)
        all_tilemaps = []
        for i, fpath in enumerate(frames):
            if i % 1000 == 0:
                print(f"  Tilemap {i}/{num_frames}...", file=sys.stderr)
            all_tilemaps.append(frame_to_tilemap(fpath, args.tile))

        print("Building codebook...", file=sys.stderr)
        key_to_id, counter = build_codebook(all_tilemaps, args.codebook)
        codebook_keys = list(key_to_id.keys())

        # Pass 2: encode and compute diffs
        print("Encoding and diffing...", file=sys.stderr)
        encoded_frames = []
        for i, tm in enumerate(all_tilemaps):
            if i % 1000 == 0:
                print(f"  Encoding {i}/{num_frames}...", file=sys.stderr)
            encoded_frames.append(encode_tilemap(tm, key_to_id, codebook_keys))

    # Compute diffs
    diff_counts = []  # number of changed tiles per frame
    for i in range(1, num_frames):
        changes = sum(1 for a, b in zip(encoded_frames[i-1], encoded_frames[i]) if a != b)
        diff_counts.append(changes)

    diff_arr = np.array(diff_counts)

    print(f"\n{'='*60}")
    print(f"FRAME DIFF ANALYSIS (codebook={args.codebook})")
    print(f"{'='*60}")
    print(f"Frames:           {num_frames}")
    print(f"Tiles per frame:  {tiles_per_frame}")
    print(f"Diff frames:      {len(diff_counts)} (frame 0 is always a keyframe)")

    print(f"\n--- Tiles Changed Per Frame ---")
    print(f"Min:              {diff_arr.min()}")
    print(f"Max:              {diff_arr.max()}")
    print(f"Mean:             {diff_arr.mean():.1f}")
    print(f"Median:           {np.median(diff_arr):.0f}")
    print(f"P20:              {np.percentile(diff_arr, 20):.0f}")
    print(f"P80:              {np.percentile(diff_arr, 80):.0f}")
    print(f"P95:              {np.percentile(diff_arr, 95):.0f}")
    print(f"P99:              {np.percentile(diff_arr, 99):.0f}")

    # Frames with zero changes (duplicate frames)
    zero_frames = np.sum(diff_arr == 0)
    print(f"\nZero-change frames: {zero_frames} ({zero_frames/len(diff_arr):.1%})")

    # Distribution buckets
    print(f"\n--- Change Distribution ---")
    buckets = [(0, 0), (1, 10), (11, 30), (31, 60), (61, 100),
               (101, 200), (201, 400), (401, 768)]
    for lo, hi in buckets:
        count = np.sum((diff_arr >= lo) & (diff_arr <= hi))
        print(f"  {lo:3d}-{hi:3d} tiles changed: {count:5d} frames ({count/len(diff_arr):5.1%})")

    # Encoding cost analysis
    print(f"\n--- Encoding Cost Estimates ---")

    # Method A: full tilemap (768 bytes/frame), rely on LZSS for zeros
    print(f"\nMethod A: Full tilemap (delta XOR, LZSS compression)")
    print(f"  Keyframe: {tiles_per_frame} bytes")
    print(f"  Delta frame: {tiles_per_frame} bytes raw (mostly zeros → LZSS friendly)")

    # Method B: sparse — (position, tile_id) pairs
    # Position: 10 bits (0-767) + tile_id: 8 bits = 18 bits → 3 bytes per change
    # Or: 2 bytes position + 1 byte tile = 3 bytes per change
    mean_changes = diff_arr.mean()
    median_changes = np.median(diff_arr)
    p80_changes = np.percentile(diff_arr, 80)
    print(f"\nMethod B: Sparse changes — 3 bytes per change (pos16 + tile8)")
    print(f"  Mean frame:   {mean_changes:.0f} changes × 3B = {mean_changes*3:.0f}B")
    print(f"  Median frame: {median_changes:.0f} changes × 3B = {median_changes*3:.0f}B")
    print(f"  P80 frame:    {p80_changes:.0f} changes × 3B = {p80_changes*3:.0f}B")
    print(f"  + 1 byte header per frame")

    # Method C: change bitmap (96 bytes = 768 bits) + tile IDs for changed tiles
    print(f"\nMethod C: Change bitmap (96B) + tile IDs for changes")
    print(f"  Mean frame:   96 + {mean_changes:.0f} = {96+mean_changes:.0f}B")
    print(f"  Median frame: 96 + {median_changes:.0f} = {96+median_changes:.0f}B")
    print(f"  P80 frame:    96 + {p80_changes:.0f} = {96+p80_changes:.0f}B")
    print(f"  Bitmap break-even vs Method B: {96/2:.0f} = 48 changes")

    # Crossover: when is bitmap cheaper than sparse?
    crossover = 96 // 2  # 3B per change vs 96B + 1B per change → crossover at 48
    above_crossover = np.sum(diff_arr > crossover)
    print(f"  Frames above crossover ({crossover} changes): "
          f"{above_crossover} ({above_crossover/len(diff_arr):.1%})")

    # Method D: hybrid — sparse for small diffs, bitmap for large diffs
    method_d_sizes = []
    for changes in diff_counts:
        sparse = changes * 3 + 1  # 3B per change + 1B header
        bitmap = 96 + changes + 1  # bitmap + tile IDs + 1B header
        method_d_sizes.append(min(sparse, bitmap))
    d_arr = np.array(method_d_sizes)
    print(f"\nMethod D: Hybrid (min of sparse, bitmap) per frame")
    print(f"  Mean frame:   {d_arr.mean():.0f}B")
    print(f"  Median frame: {np.median(d_arr):.0f}B")
    print(f"  P80 frame:    {np.percentile(d_arr, 80):.0f}B")
    print(f"  Total raw:    {d_arr.sum()/1024:.0f}KB + 1 keyframe ({tiles_per_frame}B)")

    # Keyframe analysis: how much does a keyframe cost vs accumulated diff error?
    print(f"\n--- Keyframe Analysis ---")
    # Scene changes: frames with >50% tiles changed
    big_changes = np.sum(diff_arr > tiles_per_frame * 0.3)
    print(f"Frames with >30% change: {big_changes} ({big_changes/len(diff_arr):.1%})")
    big_changes = np.sum(diff_arr > tiles_per_frame * 0.5)
    print(f"Frames with >50% change: {big_changes} ({big_changes/len(diff_arr):.1%})")

    # Find natural "scene change" positions
    threshold_big = tiles_per_frame * 0.3
    scene_changes = [i+1 for i, d in enumerate(diff_counts) if d > threshold_big]
    print(f"\nScene changes (>30% tiles changed): {len(scene_changes)} positions")
    if scene_changes:
        # Gaps between scene changes
        gaps = [scene_changes[i+1] - scene_changes[i]
                for i in range(len(scene_changes)-1)]
        if gaps:
            print(f"  Gap between scene changes: min={min(gaps)}, max={max(gaps)}, "
                  f"mean={np.mean(gaps):.0f}")
        print(f"  First 20: {scene_changes[:20]}")

    # Cost of periodic keyframes
    print(f"\n--- Periodic Keyframe Cost ---")
    for interval in [30, 60, 120, 300, 600]:
        n_keyframes = (num_frames + interval - 1) // interval
        key_cost = n_keyframes * tiles_per_frame
        diff_cost = d_arr.sum()
        total = key_cost + diff_cost
        print(f"  Every {interval:3d} frames: {n_keyframes:3d} keyframes × {tiles_per_frame}B "
              f"= {key_cost/1024:.0f}KB keys + {diff_cost/1024:.0f}KB diffs "
              f"= {total/1024:.0f}KB total")


if __name__ == "__main__":
    main()
