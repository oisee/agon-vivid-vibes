#!/usr/bin/env python3
"""Bad Apple tile analysis — extract 8x8 binary tiles, build frequency codebook.

Pipeline:
  1. ffmpeg extract frames at target resolution, threshold to 1-bit
  2. Split each frame into 8x8 tiles
  3. Count unique tiles across all frames
  4. Report frequency distribution + codebook sizes

Usage:
  python badapple_tile_analysis.py [--width 256 --height 192 --tile 8]
"""

import argparse
import os
import struct
import subprocess
import sys
import tempfile
from collections import Counter
from pathlib import Path

import numpy as np
from PIL import Image

SCRIPT_DIR = Path(__file__).parent
ASSETS_DIR = SCRIPT_DIR.parent / "assets"
VIDEO_PATH = ASSETS_DIR / "badapple-frames" / "badapple.mp4"


def extract_frames(video_path, width, height, tmpdir):
    """Extract frames from video as 1-bit PNGs at target resolution."""
    pattern = os.path.join(tmpdir, "frame_%05d.png")
    cmd = [
        "ffmpeg", "-i", str(video_path),
        "-vf", f"scale={width}:{height}:flags=area,format=gray",
        "-y", pattern
    ]
    subprocess.run(cmd, capture_output=True, check=True)
    frames = sorted(Path(tmpdir).glob("frame_*.png"))
    print(f"Extracted {len(frames)} frames at {width}x{height}", file=sys.stderr)
    return frames


def frame_to_binary(path, threshold=128):
    """Load frame as 1-bit numpy array (0 or 1)."""
    img = Image.open(path).convert("L")
    arr = np.array(img)
    return (arr >= threshold).astype(np.uint8)


def tile_to_key(tile):
    """Convert 8x8 binary tile to 8-byte key (1 bit per pixel, row-major)."""
    # Pack each row into a byte
    key = bytes(
        sum(tile[r, c] << (7 - c) for c in range(8))
        for r in range(8)
    )
    return key


def analyze_tiles(frames_paths, tile_size=8):
    """Split all frames into tiles, count unique patterns."""
    tile_counter = Counter()
    total_tiles = 0
    num_frames = len(frames_paths)

    for i, fpath in enumerate(frames_paths):
        if i % 500 == 0:
            print(f"  Processing frame {i}/{num_frames}...", file=sys.stderr)

        binary = frame_to_binary(fpath)
        h, w = binary.shape

        for ty in range(0, h, tile_size):
            for tx in range(0, w, tile_size):
                tile = binary[ty:ty+tile_size, tx:tx+tile_size]
                if tile.shape != (tile_size, tile_size):
                    continue
                key = tile_to_key(tile)
                tile_counter[key] += 1
                total_tiles += 1

    return tile_counter, total_tiles


def tile_from_key(key, tile_size=8):
    """Unpack 8-byte key back to 8x8 array for visualization."""
    tile = np.zeros((tile_size, tile_size), dtype=np.uint8)
    for r in range(tile_size):
        for c in range(tile_size):
            tile[r, c] = (key[r] >> (7 - c)) & 1
    return tile


def hamming_distance(a, b):
    """Count differing bits between two tile keys."""
    return sum(bin(x ^ y).count('1') for x, y in zip(a, b))


def compute_codebook_error(tile_counter, codebook_keys):
    """For tiles not in the codebook, find closest match and sum error."""
    codebook_set = set(codebook_keys)
    total_error = 0
    total_substitutions = 0
    worst_error = 0

    for key, count in tile_counter.items():
        if key in codebook_set:
            continue
        # Find closest codebook entry by hamming distance
        best_dist = 64  # max possible for 8x8 = 64 bits
        for cb_key in codebook_keys:
            d = hamming_distance(key, cb_key)
            if d < best_dist:
                best_dist = d
            if d <= 1:
                break
        total_error += best_dist * count
        total_substitutions += count
        worst_error = max(worst_error, best_dist)

    return total_error, total_substitutions, worst_error


def main():
    parser = argparse.ArgumentParser(description="Bad Apple tile analysis")
    parser.add_argument("--video", type=str, default=str(VIDEO_PATH))
    parser.add_argument("--width", type=int, default=256)
    parser.add_argument("--height", type=int, default=192)
    parser.add_argument("--tile", type=int, default=8)
    parser.add_argument("--top", type=int, default=256,
                        help="Codebook size to analyze (default 256)")
    args = parser.parse_args()

    tiles_per_frame = (args.width // args.tile) * (args.height // args.tile)
    print(f"Target: {args.width}x{args.height}, tile {args.tile}x{args.tile}, "
          f"{tiles_per_frame} tiles/frame", file=sys.stderr)

    with tempfile.TemporaryDirectory(prefix="badapple_") as tmpdir:
        print("Extracting frames...", file=sys.stderr)
        frames = extract_frames(args.video, args.width, args.height, tmpdir)

        print("Analyzing tiles...", file=sys.stderr)
        tile_counter, total_tiles = analyze_tiles(frames, args.tile)

    unique = len(tile_counter)
    print(f"\n{'='*60}")
    print(f"TILE ANALYSIS RESULTS")
    print(f"{'='*60}")
    print(f"Resolution:       {args.width}x{args.height}")
    print(f"Tile size:        {args.tile}x{args.tile}")
    print(f"Tiles per frame:  {tiles_per_frame}")
    print(f"Total frames:     {total_tiles // tiles_per_frame}")
    print(f"Total tile instances: {total_tiles:,}")
    print(f"Unique tiles:     {unique:,}")

    # Frequency distribution
    counts = tile_counter.most_common()
    print(f"\n--- Frequency Distribution ---")
    print(f"Top 10 tiles cover:   {sum(c for _,c in counts[:10]):,} / {total_tiles:,} "
          f"({sum(c for _,c in counts[:10])/total_tiles:.1%})")
    print(f"Top 64 tiles cover:   {sum(c for _,c in counts[:64]):,} / {total_tiles:,} "
          f"({sum(c for _,c in counts[:64])/total_tiles:.1%})")
    print(f"Top 128 tiles cover:  {sum(c for _,c in counts[:128]):,} / {total_tiles:,} "
          f"({sum(c for _,c in counts[:128])/total_tiles:.1%})")
    print(f"Top 256 tiles cover:  {sum(c for _,c in counts[:256]):,} / {total_tiles:,} "
          f"({sum(c for _,c in counts[:256])/total_tiles:.1%})")
    print(f"Top 512 tiles cover:  {sum(c for _,c in counts[:512]):,} / {total_tiles:,} "
          f"({sum(c for _,c in counts[:512])/total_tiles:.1%})")
    print(f"Top 1024 tiles cover: {sum(c for _,c in counts[:1024]):,} / {total_tiles:,} "
          f"({sum(c for _,c in counts[:1024])/total_tiles:.1%})")
    print(f"Top 4096 tiles cover: {sum(c for _,c in counts[:4096]):,} / {total_tiles:,} "
          f"({sum(c for _,c in counts[:4096])/total_tiles:.1%})")

    # Show top tiles as ASCII art
    print(f"\n--- Top 20 Tiles (ASCII) ---")
    for rank, (key, count) in enumerate(counts[:20]):
        tile = tile_from_key(key, args.tile)
        pct = count / total_tiles * 100
        rows = [''.join('#' if tile[r,c] else '.' for c in range(args.tile))
                for r in range(args.tile)]
        print(f"#{rank+1:3d} ({count:7,}x = {pct:5.2f}%)  {rows[0]}  {rows[4]}")
        print(f"                       {rows[1]}  {rows[5]}")
        print(f"                       {rows[2]}  {rows[6]}")
        print(f"                       {rows[3]}  {rows[7]}")

    # Codebook error analysis
    for cb_size in [256, 512, 1024]:
        if unique <= cb_size:
            print(f"\nCodebook {cb_size}: all {unique} tiles fit — zero error")
            continue
        codebook_keys = [k for k, _ in counts[:cb_size]]
        err, subs, worst = compute_codebook_error(tile_counter, codebook_keys)
        covered = sum(c for _, c in counts[:cb_size])
        print(f"\nCodebook {cb_size}: covers {covered/total_tiles:.1%} of instances, "
              f"{subs:,} substitutions ({subs/total_tiles:.1%}), "
              f"avg error {err/max(subs,1):.1f} bits/tile, worst {worst} bits")

    # Size estimates
    print(f"\n--- Size Estimates ---")
    for cb_size, bytes_per_id in [(256, 1), (512, 2), (1024, 2), (65536, 2)]:
        frame_bytes = tiles_per_frame * bytes_per_id
        codebook_bytes = cb_size * (args.tile * args.tile // 8)
        nframes = total_tiles // tiles_per_frame
        raw_bytes = frame_bytes * nframes + codebook_bytes
        print(f"Codebook {cb_size:5d} ({bytes_per_id}B/id): "
              f"{frame_bytes:,}B/frame raw, "
              f"{raw_bytes:,}B total raw ({raw_bytes/1024:.0f}KB), "
              f"codebook {codebook_bytes:,}B")
        # Estimate with delta: assume ~80% tiles unchanged per frame
        delta_bytes = int(frame_bytes * 0.2) * nframes + codebook_bytes
        print(f"  with ~80% delta skip: ~{delta_bytes/1024:.0f}KB raw → ~{delta_bytes/1024*0.3:.0f}KB LZSS est")


if __name__ == "__main__":
    main()
