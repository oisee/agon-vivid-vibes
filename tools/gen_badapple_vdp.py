#!/usr/bin/env python3
"""Bad Apple VDP buffer generator — tile bitmaps + delta frame buffers.

Generates VDP buffered commands for autonomous VSYNC-driven playback.
Each frame buffer chains to the next via VSYNC callback registration.

Architecture:
  - Buffers 1-256: tile bitmaps (8x8, RGBA2222 format)
  - Buffers 1000+: frame draw commands (select bitmap + plot at position)
  - Each frame buffer ends with: swap + register next frame as VSYNC callback
  - eZ80 uploads all buffers, registers frame 1000 → VDP plays autonomously

Delta encoding with double-buffer:
  - Frame N draws delta from frame N-2 (back buffer state after swap)
  - Frames 0,1 are full keyframes (CLG + all non-black tiles)

Usage:
  python gen_badapple_vdp.py --frames 1000 [--html preview.html] [--output badapple.dat]
"""

import argparse
import base64
import math
import os
import struct
import subprocess
import sys
import tempfile
import zlib
from collections import Counter
from pathlib import Path

import numpy as np
from PIL import Image

SCRIPT_DIR = Path(__file__).parent
VIDEO_PATH = SCRIPT_DIR.parent / "assets" / "badapple-frames" / "badapple.mp4"

TILE_SIZE = 8
BITMAP_BASE_ID = 0       # tile bitmaps: 0..255
FRAME_BASE_ID = 1000     # frame buffers: 1000..1000+N


def extract_frames(video_path, width, height, tmpdir, max_frames=None):
    pattern = os.path.join(tmpdir, "frame_%05d.png")
    cmd = [
        "ffmpeg", "-i", str(video_path),
        "-vf", f"scale={width}:{height}:flags=area,format=gray",
        "-y", pattern
    ]
    if max_frames:
        cmd = cmd[:3] + ["-frames:v", str(max_frames)] + cmd[3:]
    subprocess.run(cmd, capture_output=True, check=True)
    frames = sorted(Path(tmpdir).glob("frame_*.png"))
    return frames


def frame_to_tilemap(path, threshold=128, target_size=None):
    img = Image.open(path).convert("L")
    if target_size and img.size != target_size:
        img = img.resize(target_size, Image.LANCZOS)
    arr = np.array(img)
    binary = (arr >= threshold).astype(np.uint8)
    h, w = binary.shape
    tilemap = []
    for ty in range(0, h - TILE_SIZE + 1, TILE_SIZE):
        for tx in range(0, w - TILE_SIZE + 1, TILE_SIZE):
            tile = binary[ty:ty+TILE_SIZE, tx:tx+TILE_SIZE]
            key = bytes(
                sum(tile[r, c] << (7 - c) for c in range(TILE_SIZE))
                for r in range(TILE_SIZE)
            )
            tilemap.append(key)
    return tilemap


def build_codebook(all_tilemaps, size=256):
    counter = Counter()
    for tm in all_tilemaps:
        counter.update(tm)
    top = counter.most_common(size)
    key_to_id = {k: i for i, (k, _) in enumerate(top)}
    codebook = [k for k, _ in top]
    return codebook, key_to_id


def hamming_distance(a, b):
    return sum(bin(x ^ y).count('1') for x, y in zip(a, b))


def build_codebook_merge(all_tilemaps, size=256, initial_size=512):
    """Greedy merge: start with top-N by freq, merge closest pairs down to size.

    Cost of merging pair (a,b) = freq(less_frequent) × hamming_distance.
    This frees slots for visually distinct tiles that frequency alone misses."""
    counter = Counter()
    for tm in all_tilemaps:
        counter.update(tm)

    actual_initial = min(initial_size, len(counter))
    if actual_initial <= size:
        return build_codebook(all_tilemaps, size)

    top = counter.most_common(actual_initial)
    keys = [k for k, _ in top]
    n = len(keys)
    freq = np.array([cnt for _, cnt in top], dtype=np.int64)

    # Pairwise hamming via vectorized XOR
    bits = np.zeros((n, 64), dtype=np.uint8)
    for i, k in enumerate(keys):
        for r in range(8):
            for c in range(8):
                bits[i, r * 8 + c] = (k[r] >> (7 - c)) & 1
    dist = np.sum(bits[:, None, :] ^ bits[None, :, :], axis=2).astype(np.int32)

    # Cost matrix (upper triangle only, rest = INF)
    INF = np.int64(10**18)
    cost = np.full((n, n), INF, dtype=np.int64)
    for i in range(n):
        for j in range(i + 1, n):
            cost[i, j] = min(freq[i], freq[j]) * dist[i, j]

    alive = np.ones(n, dtype=bool)
    num_merges = n - size

    for step in range(num_merges):
        flat = np.argmin(cost)
        i, j = divmod(int(flat), n)

        # Keep the more frequent tile
        if freq[i] < freq[j]:
            i, j = j, i

        freq[i] += freq[j]
        alive[j] = False
        cost[j, :] = INF
        cost[:, j] = INF

        # Update survivor's costs
        for k in range(n):
            if alive[k] and k != i:
                a, b = min(i, k), max(i, k)
                cost[a, b] = min(freq[a], freq[b]) * dist[a, b]

        if (step + 1) % 50 == 0:
            print(f"  Merged {step+1}/{num_merges}...", file=sys.stderr)

    codebook = [keys[i] for i in range(n) if alive[i]]
    key_to_id = {k: i for i, k in enumerate(codebook)}
    return codebook, key_to_id


def build_codebook_structured(all_tilemaps, size=256):
    """Structured codebook: 16 rotations × 16 shifts of a B/W half-plane.

    Each tile = a dividing line at one of 16 angles, shifted to one of 16
    positions. Covers edge tiles at all orientations. Deduplicated, with
    remaining slots filled by most-frequent data tiles."""
    raw_tiles = []
    for rot in range(16):
        angle = rot * math.pi / 16   # 0° to 168.75°
        cos_a = math.cos(angle)
        sin_a = math.sin(angle)
        for shift in range(16):
            threshold = (shift - 7.5) * 0.75  # range ≈ ±5.6
            key = bytearray(8)
            for row in range(8):
                byte_val = 0
                for col in range(8):
                    proj = (col - 3.5) * cos_a + (row - 3.5) * sin_a
                    if proj >= threshold:
                        byte_val |= (1 << (7 - col))
                key[row] = byte_val
            raw_tiles.append(bytes(key))

    # Deduplicate, preserving order
    seen = {}
    structured = []
    for t in raw_tiles:
        if t not in seen:
            seen[t] = len(structured)
            structured.append(t)

    num_structured = len(structured)
    remaining = size - num_structured
    print(f"  Structured: {num_structured} unique tiles "
          f"({256 - num_structured} duplicates removed)", file=sys.stderr)

    # Fill remaining slots with most-frequent data tiles
    if remaining > 0:
        counter = Counter()
        for tm in all_tilemaps:
            counter.update(tm)
        filled = 0
        for key, _ in counter.most_common():
            if key not in seen:
                seen[key] = len(structured)
                structured.append(key)
                filled += 1
                if filled >= remaining:
                    break
        print(f"  Filled {filled} extra slots from frequency data", file=sys.stderr)

    codebook = structured[:size]
    key_to_id = {k: i for i, k in enumerate(codebook)}
    return codebook, key_to_id


def measure_quality(all_tilemaps, codebook, key_to_id):
    """Compute codebook quality: exact match rate + mean distortion."""
    counter = Counter()
    for tm in all_tilemaps:
        counter.update(tm)
    total = sum(counter.values())
    exact = sum(cnt for k, cnt in counter.items() if k in key_to_id)
    total_dist = 0
    for k, cnt in counter.items():
        if k not in key_to_id:
            d = min(hamming_distance(k, cb) for cb in codebook)
            total_dist += cnt * d
    return exact / total, total_dist / total


def sort_codebook_hamming(codebook, key_to_id):
    """Reorder codebook so visually similar tiles have adjacent indices.

    Uses greedy nearest-neighbor: start with all-black tile (or first),
    always pick the closest unvisited tile by Hamming distance.

    Returns new (codebook, key_to_id) with reordered indices."""
    n = len(codebook)
    if n <= 1:
        return codebook, key_to_id

    # Precompute pairwise Hamming distances
    bits = np.zeros((n, 64), dtype=np.uint8)
    for i, k in enumerate(codebook):
        for r in range(8):
            for c in range(8):
                bits[i, r * 8 + c] = (k[r] >> (7 - c)) & 1
    dist = np.sum(bits[:, None, :] ^ bits[None, :, :], axis=2).astype(np.int32)

    # Find all-black tile (key = b'\x00'*8) as starting point
    black_key = b'\x00' * 8
    start = 0
    for i, k in enumerate(codebook):
        if k == black_key:
            start = i
            break

    # Greedy nearest-neighbor tour
    visited = np.zeros(n, dtype=bool)
    order = [start]
    visited[start] = True
    for _ in range(n - 1):
        curr = order[-1]
        # Find nearest unvisited
        d = dist[curr].copy()
        d[visited] = 999
        nxt = int(np.argmin(d))
        order.append(nxt)
        visited[nxt] = True

    # Build reordered codebook
    new_codebook = [codebook[i] for i in order]
    new_key_to_id = {k: new_i for new_i, k in enumerate(new_codebook)}

    # Report quality of ordering
    total_dist = sum(hamming_distance(new_codebook[i], new_codebook[i+1])
                     for i in range(n - 1))
    print(f"  Hamming sort: avg adjacent distance = {total_dist/(n-1):.1f} bits",
          file=sys.stderr)

    return new_codebook, new_key_to_id


def sort_codebook_transitions(codebook, key_to_id, encoded_frames):
    """Reorder codebook to minimize delta magnitudes in actual frame transitions.

    Builds a transition frequency matrix from encoded frames (delta from N-2),
    then uses greedy nearest-neighbor where "distance" = -transition_count.
    Tiles that frequently replace each other get adjacent indices.

    Returns new (codebook, key_to_id) with reordered indices."""
    n = len(codebook)
    if n <= 1:
        return codebook, key_to_id

    num_frames = len(encoded_frames)
    tiles_per_frame = len(encoded_frames[0])

    # Build transition frequency matrix
    trans = np.zeros((n, n), dtype=np.int64)
    for f in range(2, num_frames):
        prev = encoded_frames[f - 2]
        curr = encoded_frames[f]
        for pos in range(tiles_per_frame):
            if curr[pos] != prev[pos]:
                trans[prev[pos], curr[pos]] += 1
                trans[curr[pos], prev[pos]] += 1  # symmetric

    # Find most frequent tile (start point)
    tile_freq = np.zeros(n, dtype=np.int64)
    for enc in encoded_frames:
        for tid in enc:
            tile_freq[tid] += 1
    start = int(np.argmax(tile_freq))

    # Greedy: always pick unvisited tile with highest transition count to current
    visited = np.zeros(n, dtype=bool)
    order = [start]
    visited[start] = True
    for _ in range(n - 1):
        curr = order[-1]
        # Score = transition frequency (higher = closer, should be adjacent)
        score = trans[curr].copy().astype(np.float64)
        score[visited] = -1
        nxt = int(np.argmax(score))
        order.append(nxt)
        visited[nxt] = True

    # Build reordered codebook
    new_codebook = [codebook[i] for i in order]
    new_key_to_id = {k: new_i for new_i, k in enumerate(new_codebook)}

    # Compute quality: weighted average |delta| across all transitions
    # Build old_to_new mapping
    old_to_new = np.zeros(n, dtype=np.int32)
    for new_i, old_i in enumerate(order):
        old_to_new[old_i] = new_i

    total_weight = 0
    total_cost = 0
    for i in range(n):
        for j in range(i + 1, n):
            if trans[i, j] > 0:
                delta = abs(int(old_to_new[i]) - int(old_to_new[j]))
                total_cost += trans[i, j] * delta
                total_weight += trans[i, j]

    if total_weight > 0:
        avg_delta = total_cost / total_weight
        print(f"  Transition sort: weighted avg |delta| = {avg_delta:.1f}", file=sys.stderr)
    else:
        print(f"  Transition sort: no transitions found", file=sys.stderr)

    return new_codebook, new_key_to_id


def analyze_compact_delta(encoded_frames, tiles_w, tiles_h, sorted_cb=False):
    """Analyze compact delta format efficiency vs VDU format.

    Compact format per frame:
      Bitmask: ceil(tiles_per_frame / 8) bytes — 1 bit per tile
      Data: 1 byte per changed tile (absolute ID or delta ID)

    Returns dict with stats for logging."""
    tiles_per_frame = tiles_w * tiles_h
    mask_bytes = (tiles_per_frame + 7) // 8
    num_frames = len(encoded_frames)

    # Per-frame stats
    vdu_sizes = []         # current VDU format
    compact_abs_sizes = [] # compact with absolute IDs
    compact_delta_sizes = [] # compact with delta IDs (signed byte)
    delta_id_histogram = Counter()  # distribution of |delta_id|
    changes_per_frame = []

    for f in range(num_frames):
        if f < 2:
            # Keyframes — count non-black tiles
            non_black = sum(1 for t in encoded_frames[f] if t != 0)
            vdu_bytes = 1 + non_black * 11 + 3 + 6  # CLG + tiles + swap + chain
            compact_abs = mask_bytes + tiles_per_frame  # full frame
            compact_delta = compact_abs  # no delta for keyframes
            changes_per_frame.append(tiles_per_frame)
        else:
            # Delta from f-2
            prev = encoded_frames[f - 2]
            curr = encoded_frames[f]
            changed = [(pos, curr[pos], prev[pos])
                       for pos in range(tiles_per_frame)
                       if curr[pos] != prev[pos]]
            num_changed = len(changed)
            changes_per_frame.append(num_changed)

            # Current VDU format: 11 bytes per changed tile + 9 overhead
            vdu_bytes = num_changed * 11 + 3 + 6

            # Compact absolute: bitmask + 1 byte per changed tile
            compact_abs = mask_bytes + num_changed

            # Compact delta: bitmask + signed delta per changed tile
            # If all deltas fit in signed byte (-128..127), 1 byte each
            # Otherwise need escape + 2 bytes
            compact_d = mask_bytes
            for pos, new_id, old_id in changed:
                delta = new_id - old_id
                delta_id_histogram[abs(delta)] += 1
                if -128 <= delta <= 127:
                    compact_d += 1
                else:
                    compact_d += 3  # escape + 2 bytes
            compact_delta = compact_d

        vdu_sizes.append(vdu_bytes)
        compact_abs_sizes.append(compact_abs)
        compact_delta_sizes.append(compact_delta)

    vdu_total = sum(vdu_sizes)
    abs_total = sum(compact_abs_sizes)
    delta_total = sum(compact_delta_sizes)
    changes = np.array(changes_per_frame)

    stats = {
        'vdu_total': vdu_total,
        'compact_abs_total': abs_total,
        'compact_delta_total': delta_total,
        'vdu_mean': np.mean(vdu_sizes),
        'compact_abs_mean': np.mean(compact_abs_sizes),
        'compact_delta_mean': np.mean(compact_delta_sizes),
        'savings_abs_pct': (1 - abs_total / vdu_total) * 100 if vdu_total else 0,
        'savings_delta_pct': (1 - delta_total / vdu_total) * 100 if vdu_total else 0,
        'delta_id_histogram': delta_id_histogram,
        'changes_mean': changes.mean(),
        'changes_median': float(np.median(changes)),
    }
    return stats


def analyze_mask_compression(encoded_frames, tiles_w, tiles_h, gop_size=300):
    """Analyze different mask compression strategies.

    Strategies:
    1. Raw: 150 bytes/frame (baseline)
    2. RLE per frame: run-length encode each 150-byte mask
    3. Z-transposed GOP + zlib: transpose mask bytes across time, then compress
    4. Z-transposed GOP + RLE: same but simpler compression

    The Z-transpose idea: for a GOP of G frames, mask is 150 bytes/frame.
    Instead of [frame0: b0..b149][frame1: b0..b149]...
    store as [pos0: f0..fG-1][pos1: f0..fG-1]...
    Each column is one byte-position's history across time — mostly 0x00
    with occasional bursts of activity. Compresses extremely well.
    """
    tiles_per_frame = tiles_w * tiles_h
    mask_bytes_per_frame = (tiles_per_frame + 7) // 8
    num_frames = len(encoded_frames)

    # Build all masks
    masks = []
    for f in range(num_frames):
        mask = bytearray(mask_bytes_per_frame)
        if f < 2:
            # Keyframes: all tiles "changed"
            for i in range(mask_bytes_per_frame):
                mask[i] = 0xFF
            # Clear trailing bits
            trailing = tiles_per_frame % 8
            if trailing:
                mask[-1] = (1 << trailing) - 1
        else:
            prev = encoded_frames[f - 2]
            curr = encoded_frames[f]
            for pos in range(tiles_per_frame):
                if curr[pos] != prev[pos]:
                    mask[pos // 8] |= (1 << (pos % 8))
        masks.append(bytes(mask))

    # --- Strategy 1: Raw ---
    raw_total = mask_bytes_per_frame * num_frames

    # --- Strategy 2: RLE per frame ---
    def rle_encode(data):
        """Simple byte-level RLE: [count, value] pairs."""
        if not data:
            return b''
        out = bytearray()
        i = 0
        while i < len(data):
            val = data[i]
            count = 1
            while i + count < len(data) and data[i + count] == val and count < 255:
                count += 1
            out.append(count)
            out.append(val)
            i += count
        return bytes(out)

    rle_total = sum(len(rle_encode(m)) for m in masks)

    # --- Strategy 3: zlib per frame ---
    zlib_per_frame_total = sum(len(zlib.compress(m, 9)) for m in masks)

    # --- Strategy 4: Z-transposed GOPs + zlib ---
    z_zlib_total = 0
    z_rle_total = 0
    z_raw_total = 0  # transposed but uncompressed (same as raw)
    num_gops = 0
    gop_stats = []

    for gop_start in range(0, num_frames, gop_size):
        gop_end = min(gop_start + gop_size, num_frames)
        gop_len = gop_end - gop_start
        num_gops += 1

        # Transpose: [frame][byte_pos] → [byte_pos][frame]
        columns = []
        for byte_pos in range(mask_bytes_per_frame):
            col = bytes(masks[f][byte_pos] for f in range(gop_start, gop_end))
            columns.append(col)

        # Concatenate all columns
        transposed = b''.join(columns)
        z_raw_total += len(transposed)

        # Compress transposed block
        z_compressed = zlib.compress(transposed, 9)
        z_zlib_total += len(z_compressed)

        # RLE on transposed
        z_rle = b''.join(rle_encode(col) for col in columns)
        z_rle_total += len(z_rle)

        # Also try: zlib on non-transposed (row-major) for comparison
        row_major = b''.join(masks[gop_start:gop_end])
        row_compressed = zlib.compress(row_major, 9)

        gop_stats.append({
            'frames': gop_len,
            'raw': gop_len * mask_bytes_per_frame,
            'z_zlib': len(z_compressed),
            'z_rle': len(z_rle),
            'row_zlib': len(row_compressed),
        })

    # --- Also: zlib on ALL masks (row-major) ---
    all_masks_raw = b''.join(masks)
    all_zlib = len(zlib.compress(all_masks_raw, 9))

    # --- And: zlib on ALL masks (Z-transposed) ---
    all_columns = []
    for byte_pos in range(mask_bytes_per_frame):
        col = bytes(masks[f][byte_pos] for f in range(num_frames))
        all_columns.append(col)
    all_transposed = b''.join(all_columns)
    all_z_zlib = len(zlib.compress(all_transposed, 9))

    # --- XOR-delta masks (diff from previous frame) + Z-transposed + zlib ---
    xor_masks = [masks[0]]  # first frame stored as-is
    for f in range(1, num_frames):
        xor = bytes(a ^ b for a, b in zip(masks[f], masks[f-1]))
        xor_masks.append(xor)

    # Z-transpose XOR masks
    xor_columns = []
    for byte_pos in range(mask_bytes_per_frame):
        col = bytes(xor_masks[f][byte_pos] for f in range(num_frames))
        xor_columns.append(col)
    xor_transposed = b''.join(xor_columns)
    all_xor_z_zlib = len(zlib.compress(xor_transposed, 9))

    # XOR row-major for comparison
    xor_row = b''.join(xor_masks)
    all_xor_zlib = len(zlib.compress(xor_row, 9))

    # XOR zero-byte density
    xor_total_bytes = sum(len(m) for m in xor_masks)
    xor_zero_bytes = sum(sum(1 for b in m if b == 0) for m in xor_masks)
    xor_zero_pct = xor_zero_bytes / xor_total_bytes * 100 if xor_total_bytes else 0

    # Count zero-masks (completely unchanged frames)
    zero_masks = sum(1 for m in masks if all(b == 0 for b in m))

    # Byte-level stats: how many mask bytes are 0x00 across all frames?
    total_mask_bytes = sum(len(m) for m in masks)
    zero_bytes = sum(sum(1 for b in m if b == 0) for m in masks)

    stats = {
        'raw_total': raw_total,
        'rle_total': rle_total,
        'zlib_per_frame_total': zlib_per_frame_total,
        'z_zlib_total': z_zlib_total,
        'z_rle_total': z_rle_total,
        'all_zlib': all_zlib,
        'all_z_zlib': all_z_zlib,
        'all_xor_zlib': all_xor_zlib,
        'all_xor_z_zlib': all_xor_z_zlib,
        'xor_zero_pct': xor_zero_pct,
        'gop_stats': gop_stats,
        'num_gops': num_gops,
        'gop_size': gop_size,
        'zero_masks': zero_masks,
        'zero_byte_pct': zero_bytes / total_mask_bytes * 100 if total_mask_bytes else 0,
        'mask_bytes_per_frame': mask_bytes_per_frame,
    }
    return stats


def lzss_encode_size(data, window_size=256, min_match=3, max_match=18):
    """Estimate LZSS compressed size — realistic for eZ80 decoding.

    Format: groups of 8 tokens preceded by a flag byte.
    Literal = 1 byte, Match = 2 bytes (offset:8 + length:4+min_match).

    Uses hash-based matching for speed (O(n) instead of O(n*window))."""
    if not data:
        return 0
    data = bytes(data)
    n = len(data)

    # Hash table: 3-byte hash → list of positions
    htable = {}
    num_literals = 0
    num_matches = 0
    pos = 0

    while pos < n:
        best_len = 0
        best_off = 0

        if pos + min_match <= n:
            # Hash on first 3 bytes
            h = (data[pos] << 16) | (data[pos+1] << 8) | data[pos+2] if pos + 2 < n else -1

            if h >= 0 and h in htable:
                for match_pos in reversed(htable[h][-8:]):  # check last 8 candidates
                    off = pos - match_pos
                    if off > window_size:
                        continue
                    ml = 0
                    while (ml < max_match and pos + ml < n and
                           data[match_pos + ml] == data[pos + ml]):
                        ml += 1
                    if ml >= min_match and ml > best_len:
                        best_len = ml
                        best_off = off
                        if ml == max_match:
                            break

            # Update hash table
            if h >= 0:
                if h not in htable:
                    htable[h] = []
                htable[h].append(pos)
                # Keep window bounded
                if len(htable[h]) > 16:
                    htable[h] = htable[h][-16:]

        if best_len >= min_match:
            num_matches += 1
            pos += best_len
        else:
            num_literals += 1
            pos += 1

    total_tokens = num_literals + num_matches
    num_groups = (total_tokens + 7) // 8
    total_bytes = num_groups + num_literals + num_matches * 2
    return total_bytes


# ============================================================
# LZSS Compressor / Decompressor (actual byte output)
# ============================================================

LZSS_WINDOW = 256
LZSS_MIN_MATCH = 3
LZSS_MAX_MATCH = LZSS_MIN_MATCH + 255   # 258 — full byte for length

def lzss_compress(data):
    """LZSS compress data into bytes.

    Format: groups of 8 tokens, each group has 1 flag byte + token data.
    Flag bit=1 → literal (1 byte), bit=0 → match (2 bytes).
    Match: byte0 = offset-1, byte1 = length-LZSS_MIN_MATCH.
    Flag bits processed LSB-first."""
    data = bytes(data)
    n = len(data)
    if n == 0:
        return b''

    htable = {}
    tokens = []  # (value,) for literal, (offset, length) for match

    pos = 0
    while pos < n:
        best_len = 0
        best_off = 0

        if pos + LZSS_MIN_MATCH <= n:
            h = (data[pos] << 16) | (data[pos+1] << 8) | (data[pos+2] if pos+2 < n else 0)

            if pos + 2 < n and h in htable:
                for match_pos in reversed(htable[h][-16:]):
                    off = pos - match_pos
                    if off > LZSS_WINDOW or off <= 0:
                        continue
                    ml = 0
                    while (ml < LZSS_MAX_MATCH and pos + ml < n and
                           data[match_pos + ml] == data[pos + ml]):
                        ml += 1
                    if ml >= LZSS_MIN_MATCH and ml > best_len:
                        best_len = ml
                        best_off = off
                        if ml == LZSS_MAX_MATCH:
                            break

            # Update hash table for current position
            if pos + 2 < n:
                if h not in htable:
                    htable[h] = []
                htable[h].append(pos)
                if len(htable[h]) > 32:
                    htable[h] = htable[h][-32:]

        if best_len >= LZSS_MIN_MATCH:
            tokens.append((best_off, best_len))
            # Hash intermediate positions for better future matches
            for i in range(1, min(best_len, LZSS_MAX_MATCH)):
                if pos + i + 2 < n:
                    ih = (data[pos+i] << 16) | (data[pos+i+1] << 8) | data[pos+i+2]
                    if ih not in htable:
                        htable[ih] = []
                    htable[ih].append(pos + i)
                    if len(htable[ih]) > 32:
                        htable[ih] = htable[ih][-32:]
            pos += best_len
        else:
            tokens.append((data[pos],))
            pos += 1

    # Emit flag bytes + token data in groups of 8
    out = bytearray()
    i = 0
    while i < len(tokens):
        flag = 0
        group_data = bytearray()

        for bit in range(8):
            if i >= len(tokens):
                flag |= (1 << bit)  # unused slots marked as literal
                continue
            token = tokens[i]
            if len(token) == 1:
                flag |= (1 << bit)
                group_data.append(token[0])
            else:
                offset, length = token
                group_data.append(offset - 1)
                group_data.append(length - LZSS_MIN_MATCH)
            i += 1

        out.append(flag)
        out.extend(group_data)

    return bytes(out)


def lzss_decompress(compressed, original_len):
    """Decompress LZSS data. Stops after outputting original_len bytes."""
    out = bytearray()
    pos = 0
    comp = bytes(compressed)

    while len(out) < original_len and pos < len(comp):
        flag = comp[pos]
        pos += 1

        for bit in range(8):
            if len(out) >= original_len or pos >= len(comp):
                break

            if flag & (1 << bit):
                # Literal
                out.append(comp[pos])
                pos += 1
            else:
                # Match
                if pos + 1 >= len(comp):
                    break
                offset = comp[pos] + 1
                length = comp[pos + 1] + LZSS_MIN_MATCH
                pos += 2
                start = len(out) - offset
                for j in range(length):
                    if len(out) >= original_len:
                        break
                    out.append(out[start + j])

    return bytes(out[:original_len])


# ============================================================
# Canonical Huffman Encoder / Decoder
# ============================================================

import heapq

def build_huffman_codes(freq):
    """Build canonical Huffman codes from symbol frequencies.

    Args:
        freq: dict mapping symbol → count (only symbols with count > 0)
    Returns:
        dict mapping symbol → (code_bits, code_length)
    """
    # Filter zero-count symbols
    freq = {s: c for s, c in freq.items() if c > 0}
    if not freq:
        return {}
    if len(freq) == 1:
        sym = next(iter(freq))
        return {sym: (0, 1)}

    # Build Huffman tree via priority queue
    counter = 0
    heap = []
    for sym, count in freq.items():
        heapq.heappush(heap, (count, counter, sym))
        counter += 1

    while len(heap) > 1:
        left = heapq.heappop(heap)
        right = heapq.heappop(heap)
        heapq.heappush(heap, (left[0] + right[0], counter, (left, right)))
        counter += 1

    # Extract code lengths by tree walk
    lengths = {}
    def walk(node, depth):
        item = node[2]
        if isinstance(item, tuple):
            walk(item[0], depth + 1)
            walk(item[1], depth + 1)
        else:
            lengths[item] = max(depth, 1)
    walk(heap[0], 0)

    # Assign canonical codes: sort by (length, symbol)
    sorted_symbols = sorted(lengths.keys(), key=lambda s: (lengths[s], s))
    code = 0
    codes = {}
    for i, sym in enumerate(sorted_symbols):
        cl = lengths[sym]
        if i > 0:
            code += 1
            code <<= (cl - lengths[sorted_symbols[i - 1]])
        codes[sym] = (code, cl)
    return codes


def serialize_huffman_table(codes):
    """Serialize canonical Huffman table for eZ80 decoder.

    Format:
        max_code_len (u8)
        counts[1..max_code_len] (u8 each — symbols with this length)
        symbols (u8 each — in canonical order)
    """
    if not codes:
        return b'\x00'
    max_len = max(cl for _, cl in codes.values())
    by_length = [[] for _ in range(max_len + 1)]
    for sym, (code, cl) in codes.items():
        by_length[cl].append(sym)
    # Sort symbols within each length by value (canonical order)
    for bl in by_length:
        bl.sort()

    out = bytearray()
    out.append(max_len)
    for l in range(1, max_len + 1):
        out.append(len(by_length[l]))
    for l in range(1, max_len + 1):
        for s in by_length[l]:
            out.append(s)
    return bytes(out)


def deserialize_huffman_table(table_bytes):
    """Reconstruct canonical Huffman codes from serialized table.

    Returns dict: symbol → (code_bits, code_length)."""
    if not table_bytes or table_bytes[0] == 0:
        return {}
    max_len = table_bytes[0]
    pos = 1
    counts = []
    for l in range(1, max_len + 1):
        counts.append(table_bytes[pos])
        pos += 1

    sorted_symbols = []
    for l_idx, count in enumerate(counts):
        length = l_idx + 1
        for _ in range(count):
            sorted_symbols.append((table_bytes[pos], length))
            pos += 1

    code = 0
    codes = {}
    prev_len = 0
    for i, (sym, cl) in enumerate(sorted_symbols):
        if i > 0:
            code += 1
            code <<= (cl - prev_len)
        codes[sym] = (code, cl)
        prev_len = cl
    return codes


def huffman_encode_stream(data, codes):
    """Huffman encode a byte stream. Returns bit-packed bytes (MSB-first)."""
    bit_buf = 0
    num_bits = 0
    out = bytearray()

    for byte_val in data:
        code, code_len = codes[byte_val]
        bit_buf = (bit_buf << code_len) | code
        num_bits += code_len
        while num_bits >= 8:
            num_bits -= 8
            out.append((bit_buf >> num_bits) & 0xFF)

    # Flush remaining bits (zero-padded)
    if num_bits > 0:
        out.append((bit_buf << (8 - num_bits)) & 0xFF)

    return bytes(out)


def huffman_decode_stream(compressed, codes, num_symbols):
    """Decode num_symbols from Huffman-compressed data (MSB-first).

    Uses tree-walking decoder."""
    # Build decode tree
    class Node:
        __slots__ = ('children', 'symbol')
        def __init__(self):
            self.children = [None, None]
            self.symbol = None

    root = Node()
    for sym, (code, code_len) in codes.items():
        node = root
        for i in range(code_len - 1, -1, -1):
            bit = (code >> i) & 1
            if node.children[bit] is None:
                node.children[bit] = Node()
            node = node.children[bit]
        node.symbol = sym

    out = bytearray()
    bit_pos = 0
    total_bits = len(compressed) * 8
    node = root

    while len(out) < num_symbols and bit_pos < total_bits:
        byte_idx = bit_pos >> 3
        bit_idx = 7 - (bit_pos & 7)  # MSB first
        bit = (compressed[byte_idx] >> bit_idx) & 1
        bit_pos += 1
        node = node.children[bit]
        if node is None:
            raise ValueError(f"Invalid Huffman code at bit {bit_pos-1}")
        if node.symbol is not None:
            out.append(node.symbol)
            node = root

    return bytes(out)


def analyze_id_stream(encoded_frames, tiles_w, tiles_h, gop_size=300):
    """Analyze ID stream compression strategies.

    Strategies:
    1. Changed-IDs only (sparse): just the IDs for changed tiles, concatenated
    2. Full tilemaps Z-transposed: 1200 bytes/frame, transpose by position
    3. RLE / LZSS / zlib on each approach
    """
    tiles_per_frame = tiles_w * tiles_h
    num_frames = len(encoded_frames)

    # --- Build sparse ID streams (only changed tiles) ---
    sparse_streams = []  # per-frame list of changed IDs
    for f in range(num_frames):
        if f < 2:
            # Keyframe: all tile IDs
            sparse_streams.append(bytes(encoded_frames[f]))
        else:
            prev = encoded_frames[f - 2]
            curr = encoded_frames[f]
            changed_ids = bytes(curr[pos] for pos in range(tiles_per_frame)
                                if curr[pos] != prev[pos])
            sparse_streams.append(changed_ids)

    sparse_raw = b''.join(sparse_streams)
    sparse_total = len(sparse_raw)

    # --- Full tilemaps (dense): 1200 bytes/frame ---
    dense_raw = b''.join(bytes(enc) for enc in encoded_frames)
    dense_total = len(dense_raw)

    # --- Compression: sparse stream ---
    sparse_zlib = len(zlib.compress(sparse_raw, 9))
    sparse_rle_total = 0
    sparse_lzss_total = 0

    # Per-GOP sparse
    for gop_start in range(0, num_frames, gop_size):
        gop_end = min(gop_start + gop_size, num_frames)
        gop_data = b''.join(sparse_streams[gop_start:gop_end])
        sparse_rle_total += lzss_encode_size(gop_data, window_size=0, min_match=999,
                                         max_match=0) if False else 0
        sparse_lzss_total += lzss_encode_size(gop_data, window_size=256)

    # Simple RLE for sparse (byte-level)
    def rle_size(data):
        if not data:
            return 0
        out = 0
        i = 0
        while i < len(data):
            val = data[i]
            count = 1
            while i + count < len(data) and data[i + count] == val and count < 255:
                count += 1
            out += 2  # count + value
            i += count
        return out

    sparse_rle_total = sum(rle_size(s) for s in sparse_streams)

    # --- Z-transposed dense tilemaps ---
    # Transpose: [frame][pos] → [pos][frame]
    z_columns = []
    for pos in range(tiles_per_frame):
        col = bytes(encoded_frames[f][pos] for f in range(num_frames))
        z_columns.append(col)
    z_transposed = b''.join(z_columns)
    z_dense_zlib = len(zlib.compress(z_transposed, 9))

    # Z-transposed per-GOP with LZSS
    z_lzss_total = 0
    z_rle_total = 0
    for gop_start in range(0, num_frames, gop_size):
        gop_end = min(gop_start + gop_size, num_frames)
        gop_cols = []
        for pos in range(tiles_per_frame):
            col = bytes(encoded_frames[f][pos]
                        for f in range(gop_start, gop_end))
            gop_cols.append(col)
        gop_transposed = b''.join(gop_cols)
        z_lzss_total += lzss_encode_size(gop_transposed, window_size=256)
        z_rle_total += sum(rle_size(col) for col in gop_cols)

    # --- XOR-delta dense tilemaps (diff from prev frame) + Z-transpose ---
    xor_dense = [bytes(encoded_frames[0])]
    for f in range(1, num_frames):
        xor = bytes((encoded_frames[f][pos] - encoded_frames[f-1][pos]) & 0xFF
                     for pos in range(tiles_per_frame))
        xor_dense.append(xor)

    xor_z_columns = []
    for pos in range(tiles_per_frame):
        col = bytes(xor_dense[f][pos] for f in range(num_frames))
        xor_z_columns.append(col)
    xor_z_transposed = b''.join(xor_z_columns)
    xor_z_zlib = len(zlib.compress(xor_z_transposed, 9))

    # XOR dense zero density
    xor_zeros = sum(sum(1 for b in row if b == 0) for row in xor_dense)
    xor_zero_pct = xor_zeros / dense_total * 100

    # Per-GOP LZSS on XOR + Z-transposed
    xor_z_lzss_total = 0
    for gop_start in range(0, num_frames, gop_size):
        gop_end = min(gop_start + gop_size, num_frames)
        gop_cols = []
        for pos in range(tiles_per_frame):
            col = bytes(xor_dense[f][pos] for f in range(gop_start, gop_end))
            gop_cols.append(col)
        gop_data = b''.join(gop_cols)
        xor_z_lzss_total += lzss_encode_size(gop_data, window_size=256)

    stats = {
        'sparse_total': sparse_total,
        'sparse_zlib': sparse_zlib,
        'sparse_rle': sparse_rle_total,
        'sparse_lzss': sparse_lzss_total,
        'dense_total': dense_total,
        'z_dense_zlib': z_dense_zlib,
        'z_lzss': z_lzss_total,
        'z_rle': z_rle_total,
        'xor_z_zlib': xor_z_zlib,
        'xor_z_lzss': xor_z_lzss_total,
        'xor_zero_pct': xor_zero_pct,
    }
    return stats


def analyze_two_stream(encoded_frames, tiles_w, tiles_h, gop_size=300):
    """Analyze the two-stream architecture: mask stream + ID stream.

    Mask stream: LZSS-compressed delta bitmasks
    ID stream: Huffman-coded tile IDs (frequent updaters = fewer bits)

    This is the target eZ80 architecture."""
    tiles_per_frame = tiles_w * tiles_h
    mask_bytes_per_frame = (tiles_per_frame + 7) // 8
    num_frames = len(encoded_frames)

    # Build masks and sparse ID streams
    masks = []
    id_streams = []  # per-frame list of changed tile IDs
    all_update_ids = []  # flat list of all update IDs for frequency analysis

    for f in range(num_frames):
        mask = bytearray(mask_bytes_per_frame)
        ids = []
        if f < 2:
            # Keyframe: all tiles
            for pos in range(tiles_per_frame):
                mask[pos // 8] |= (1 << (pos % 8))
                ids.append(encoded_frames[f][pos])
            trailing = tiles_per_frame % 8
            if trailing:
                mask[-1] &= (1 << trailing) - 1
        else:
            prev = encoded_frames[f - 2]
            curr = encoded_frames[f]
            for pos in range(tiles_per_frame):
                if curr[pos] != prev[pos]:
                    mask[pos // 8] |= (1 << (pos % 8))
                    ids.append(curr[pos])
        masks.append(bytes(mask))
        id_streams.append(ids)
        all_update_ids.extend(ids)

    # === MASK STREAM ANALYSIS ===
    mask_raw = b''.join(masks)
    mask_raw_total = len(mask_raw)

    # XOR-delta masks (better for LZSS)
    xor_masks = [masks[0]]
    for f in range(1, num_frames):
        xor = bytes(a ^ b for a, b in zip(masks[f], masks[f-1]))
        xor_masks.append(xor)
    xor_mask_raw = b''.join(xor_masks)

    # LZSS per GOP on raw masks
    mask_lzss_total = 0
    for gop_start in range(0, num_frames, gop_size):
        gop_end = min(gop_start + gop_size, num_frames)
        gop_data = b''.join(masks[gop_start:gop_end])
        mask_lzss_total += lzss_encode_size(gop_data, window_size=256)

    # LZSS per GOP on XOR masks
    mask_xor_lzss_total = 0
    for gop_start in range(0, num_frames, gop_size):
        gop_end = min(gop_start + gop_size, num_frames)
        gop_data = b''.join(xor_masks[gop_start:gop_end])
        mask_xor_lzss_total += lzss_encode_size(gop_data, window_size=256)

    # Z-transposed masks (byte-level) + LZSS per GOP
    mask_z_lzss_total = 0
    for gop_start in range(0, num_frames, gop_size):
        gop_end = min(gop_start + gop_size, num_frames)
        gop_len = gop_end - gop_start
        cols = []
        for byte_pos in range(mask_bytes_per_frame):
            col = bytes(masks[f][byte_pos] for f in range(gop_start, gop_end))
            cols.append(col)
        gop_transposed = b''.join(cols)
        mask_z_lzss_total += lzss_encode_size(gop_transposed, window_size=256)

    # Bit-level temporal packing: each tile's change history packed into bytes
    # Tile (x,y) across 8 frames → 1 byte, across 64 frames → 8 bytes
    # For 1000 frames: 1200 tiles × ceil(1000/8) = 1200 × 125 = 150,000 bytes (same raw)
    # But each byte is ONE tile's temporal signal → compresses much better

    # Build per-tile change bit arrays
    tile_change_bits = []  # [tile_pos] → list of bools across frames
    for pos in range(tiles_per_frame):
        bits = []
        for f in range(num_frames):
            if f < 2:
                bits.append(True)  # keyframe: all changed
            else:
                bits.append(encoded_frames[f][pos] != encoded_frames[f-2][pos])
        tile_change_bits.append(bits)

    # Pack into temporal bytes: 8 frames per byte, per tile
    def pack_temporal(bits_list, num_frames):
        """Pack list of bools into bytes, 8 frames per byte."""
        out = bytearray()
        for i in range(0, num_frames, 8):
            byte_val = 0
            for bit in range(min(8, num_frames - i)):
                if bits_list[i + bit]:
                    byte_val |= (1 << bit)
            out.append(byte_val)
        return bytes(out)

    temporal_bytes_per_tile = (num_frames + 7) // 8  # e.g. 125 for 1000 frames

    # Full temporal packed data: all tiles concatenated
    temporal_all = b''.join(pack_temporal(tile_change_bits[pos], num_frames)
                            for pos in range(tiles_per_frame))
    temporal_raw_total = len(temporal_all)  # should equal mask_raw_total

    # Compression: whole thing
    temporal_zlib = len(zlib.compress(temporal_all, 9))
    temporal_lzss = lzss_encode_size(temporal_all, window_size=256)

    # Per-GOP temporal packing + compression
    temporal_gop_lzss = 0
    temporal_gop_zlib = 0
    for gop_start in range(0, num_frames, gop_size):
        gop_end = min(gop_start + gop_size, num_frames)
        gop_len = gop_end - gop_start
        gop_temporal = b''.join(
            pack_temporal(tile_change_bits[pos][gop_start:gop_end], gop_len)
            for pos in range(tiles_per_frame))
        temporal_gop_lzss += lzss_encode_size(gop_temporal, window_size=256)
        temporal_gop_zlib += len(zlib.compress(gop_temporal, 9))

    # Stats on temporal bytes: zero density
    temporal_zeros = sum(1 for b in temporal_all if b == 0)
    temporal_zero_pct = temporal_zeros / len(temporal_all) * 100

    # How many tiles have ALL-zero temporal (never change)?
    tiles_never_change = sum(1 for pos in range(tiles_per_frame)
                             if not any(tile_change_bits[pos][2:]))  # skip keyframes

    # === ID STREAM ANALYSIS ===
    id_raw = bytes(all_update_ids)
    id_raw_total = len(id_raw)

    # Frequency analysis of update IDs
    id_freq = Counter(all_update_ids)
    total_ids = len(all_update_ids)
    num_unique_ids = len(id_freq)

    # Shannon entropy (theoretical minimum bits/symbol)
    entropy = 0.0
    for count in id_freq.values():
        p = count / total_ids
        if p > 0:
            entropy -= p * math.log2(p)

    # Huffman estimate: entropy + ~0.05 overhead per symbol
    huffman_bits_per_id = entropy + 0.05  # realistic overhead
    huffman_total_bits = total_ids * huffman_bits_per_id
    huffman_total_bytes = int(math.ceil(huffman_total_bits / 8))
    # Add Huffman table overhead: 256 entries × (symbol + code_len) ≈ 512 bytes
    huffman_table_size = num_unique_ids * 2
    huffman_with_table = huffman_total_bytes + huffman_table_size

    # LZSS on raw ID stream per GOP
    id_lzss_total = 0
    for gop_start in range(0, num_frames, gop_size):
        gop_end = min(gop_start + gop_size, num_frames)
        gop_data = bytes(b for f in range(gop_start, gop_end)
                         for b in id_streams[f])
        id_lzss_total += lzss_encode_size(gop_data, window_size=256)

    # zlib on raw ID stream (theoretical best)
    id_zlib = len(zlib.compress(id_raw, 9))

    # Top-N most frequent update IDs
    top_ids = id_freq.most_common(20)

    stats = {
        # Masks
        'mask_raw': mask_raw_total,
        'mask_lzss': mask_lzss_total,
        'mask_xor_lzss': mask_xor_lzss_total,
        'mask_z_lzss': mask_z_lzss_total,
        # Temporal bit-packed
        'mask_temporal_raw': temporal_raw_total,
        'mask_temporal_zlib': temporal_zlib,
        'mask_temporal_lzss': temporal_lzss,
        'mask_temporal_gop_lzss': temporal_gop_lzss,
        'mask_temporal_gop_zlib': temporal_gop_zlib,
        'mask_temporal_zero_pct': temporal_zero_pct,
        'tiles_never_change': tiles_never_change,
        # IDs
        'id_raw': id_raw_total,
        'id_entropy': entropy,
        'id_huffman_bytes': huffman_total_bytes,
        'id_huffman_with_table': huffman_with_table,
        'id_lzss': id_lzss_total,
        'id_zlib': id_zlib,
        'id_unique': num_unique_ids,
        'id_top': top_ids,
        'total_updates': total_ids,
        # Combos
        'raw_masks_raw_ids': mask_raw_total + id_raw_total,
        'lzss_masks_huffman_ids': mask_lzss_total + huffman_with_table,
        'xor_lzss_masks_huffman_ids': mask_xor_lzss_total + huffman_with_table,
        'z_lzss_masks_huffman_ids': mask_z_lzss_total + huffman_with_table,
        'temporal_lzss_huffman_ids': temporal_gop_lzss + huffman_with_table,
        'lzss_masks_lzss_ids': mask_lzss_total + id_lzss_total,
    }
    return stats


def encode_tilemap(tilemap, key_to_id, codebook):
    ids = []
    for key in tilemap:
        if key in key_to_id:
            ids.append(key_to_id[key])
        else:
            best_dist = 64
            best_id = 0
            for i, cb_key in enumerate(codebook):
                d = hamming_distance(key, cb_key)
                if d < best_dist:
                    best_dist = d
                    best_id = i
                if d == 0:
                    break
            ids.append(best_id)
    return ids


def tile_to_rgba8888(tile_key):
    """Convert 8-byte tile key to 256-byte RGBA8888 bitmap data.
    White pixel = (255,255,255,255), Black = (0,0,0,255)."""
    data = bytearray(256)
    for r in range(8):
        for c in range(8):
            bit = (tile_key[r] >> (7 - c)) & 1
            idx = (r * 8 + c) * 4
            val = 255 if bit else 0
            data[idx] = val        # R
            data[idx + 1] = val    # G
            data[idx + 2] = val    # B
            data[idx + 3] = 255    # A (always opaque)
    return bytes(data)


def build_chardef_commands(codebook, id_offset=32):
    """Generate VDU commands to define characters for charprint mode.
    Each tile becomes a VDU 23 character definition.
    Returns list of (char_code, vdu_bytes) tuples."""
    defs = []
    for i, tile_key in enumerate(codebook):
        char_code = id_offset + i
        # VDU 23, char_code, b0, b1, ..., b7 — redefine character
        vdu = bytearray([23, char_code])
        vdu.extend(tile_key)  # 8 bytes, 1 per row, MSB=leftmost pixel
        defs.append((char_code, bytes(vdu)))
    return defs


def build_bitmap_upload_commands(codebook):
    """Generate VDU commands to upload all tile bitmaps.
    Returns list of (buffer_id, vdu_bytes) tuples.

    Uses VDU 23, 27, 1 (inline upload) which requires RGBA8888 format
    (4 bytes/pixel = 256 bytes per 8x8 tile)."""
    uploads = []
    for i, tile_key in enumerate(codebook):
        bitmap_id = BITMAP_BASE_ID + i
        rgba_data = tile_to_rgba8888(tile_key)

        # VDU 23, 27, 0, bitmap_id — select bitmap (8-bit ID, 0-255)
        # VDU 23, 27, 1, w; h; <RGBA8888 data> — create bitmap inline
        vdu = bytearray()
        vdu.extend([23, 27, 0, bitmap_id & 0xFF])
        vdu.extend([23, 27, 1])
        vdu.extend(struct.pack("<HH", 8, 8))
        vdu.extend(rgba_data)
        uploads.append((bitmap_id, bytes(vdu)))
    return uploads


def build_frame_buffer(frame_idx, encoded_frame, prev_encoded, tiles_w, tiles_h,
                       num_total_frames, is_keyframe=False):
    """Build VDU commands for a single frame buffer.

    For keyframes: CLG + draw all non-black tiles
    For delta: draw only tiles that changed from prev_encoded
    """
    tiles_per_frame = tiles_w * tiles_h
    vdu = bytearray()

    if is_keyframe:
        # Find which tile ID is all-black (should be tile 0 in our codebook)
        # CLG clears to background color (black) — skip black tiles
        vdu.append(16)  # CLG

        # Group tiles by bitmap ID for efficiency (select once, plot many)
        from collections import defaultdict
        by_tile = defaultdict(list)
        for pos in range(tiles_per_frame):
            tid = encoded_frame[pos]
            # Skip all-black tile (assumed to be tile ID 0)
            if tid == 0:
                continue
            by_tile[tid].append(pos)

        for tid, positions in sorted(by_tile.items()):
            bitmap_id = BITMAP_BASE_ID + tid
            # Select bitmap
            vdu.extend([23, 27, 0, bitmap_id & 0xFF])
            for pos in positions:
                tx = pos % tiles_w
                ty = pos // tiles_w
                x = tx * TILE_SIZE
                y = ty * TILE_SIZE
                # Plot bitmap at position
                vdu.extend([23, 27, 3])
                vdu.extend(struct.pack("<HH", x, y))
    else:
        # Delta frame: draw only changed tiles
        # Group by tile ID for efficiency
        from collections import defaultdict
        by_tile = defaultdict(list)
        for pos in range(tiles_per_frame):
            if encoded_frame[pos] != prev_encoded[pos]:
                by_tile[encoded_frame[pos]].append(pos)

        for tid, positions in sorted(by_tile.items()):
            bitmap_id = BITMAP_BASE_ID + tid
            vdu.extend([23, 27, 0, bitmap_id & 0xFF])
            for pos in positions:
                tx = pos % tiles_w
                ty = pos // tiles_w
                x = tx * TILE_SIZE
                y = ty * TILE_SIZE
                vdu.extend([23, 27, 3])
                vdu.extend(struct.pack("<HH", x, y))

    # Swap double buffer
    vdu.extend([23, 0, 0xC3])

    # Register next frame as VSYNC callback (chain playback)
    next_frame = frame_idx + 1
    if next_frame >= num_total_frames:
        # Last frame: deregister VSYNC callback (stop playback)
        # Command 80 with buffer 0xFFFF = clear callback
        vdu.extend([23, 0, 0xA0, 0xFF, 0xFF, 80])
    else:
        next_id = FRAME_BASE_ID + next_frame
        vdu.extend([23, 0, 0xA0, next_id & 0xFF, (next_id >> 8) & 0xFF, 80])

    return bytes(vdu)


def build_frame_charprint(encoded_frame, prev_encoded, tiles_w, tiles_h,
                          is_keyframe=False, id_offset=32):
    """Build charprint VDU commands: MOVE to run start + character bytes.
    Tile IDs are offset by id_offset (32) to map to printable char codes."""
    tiles_per_frame = tiles_w * tiles_h
    vdu = bytearray()

    if is_keyframe:
        vdu.append(16)  # CLG

    for row in range(tiles_h):
        row_off = row * tiles_w
        run_started = False
        for col in range(tiles_w):
            pos = row_off + col
            changed = False
            if is_keyframe:
                # Keyframe: draw all non-black tiles (tile 0 = all-black)
                changed = encoded_frame[pos] != 0
            else:
                changed = encoded_frame[pos] != prev_encoded[pos]

            if changed:
                if not run_started:
                    # MOVE to (col*8, row*8)
                    x, y = col * 8, row * 8
                    vdu.extend([25, 4])  # VDU 25, PLOT 4 = MOVE
                    vdu.extend(struct.pack("<HH", x, y))
                    run_started = True
                # Emit character byte
                vdu.append(encoded_frame[pos] + id_offset)
            else:
                run_started = False

    # Swap double buffer
    vdu.extend([23, 0, 0xC3])
    return bytes(vdu)


def wrap_vdp_buffer(buffer_id, payload):
    """Wrap payload in VDP buffer write command.
    VDU 23, 0, &A0, id_lo, id_hi, 0, len_lo, len_hi, <payload>"""
    header = bytearray([
        23, 0, 0xA0,
        buffer_id & 0xFF, (buffer_id >> 8) & 0xFF,
        0,  # command 0 = write
        len(payload) & 0xFF, (len(payload) >> 8) & 0xFF
    ])
    return bytes(header) + payload


def generate_html(codebook, encoded_frames, frame_vdu_sizes, tiles_w, tiles_h, fps,
                   compact_stats=None):
    """Generate HTML preview showing playback + VDP buffer size stats."""
    # Pack tile data for JS player (reuse gen_badapple format)
    tiles_per_frame = tiles_w * tiles_h
    num_frames = len(encoded_frames)

    # Encode codebook as JS array
    cb_js = "["
    for i, key in enumerate(codebook):
        cb_js += "[" + ",".join(str(b) for b in key) + "]"
        if i < len(codebook) - 1:
            cb_js += ","
    cb_js += "]"

    # Encode frames as delta stream for JS
    # Frame 0: full, then deltas from N-2
    frames_js_parts = []
    frames_js_parts.append("[" + ",".join(str(t) for t in encoded_frames[0]) + "]")
    for i in range(1, num_frames):
        prev_idx = max(0, i - 2) if i >= 2 else -1  # -1 means keyframe
        if prev_idx == -1:
            frames_js_parts.append("[" + ",".join(str(t) for t in encoded_frames[i]) + "]")
        else:
            # Delta: list of [pos, new_tid] pairs
            changes = []
            for pos in range(tiles_per_frame):
                if encoded_frames[i][pos] != encoded_frames[prev_idx][pos]:
                    changes.append(f"[{pos},{encoded_frames[i][pos]}]")
            frames_js_parts.append("[" + ",".join(changes) + "]")

    # VDU sizes for stats display
    sizes_js = "[" + ",".join(str(s) for s in frame_vdu_sizes) + "]"

    html = f"""<!DOCTYPE html>
<html>
<head>
<meta charset="utf-8">
<title>Bad Apple VDP — 320x240 Tile Preview</title>
<style>
  body {{ margin: 0; background: #000; display: flex; justify-content: center;
         align-items: center; min-height: 100vh; flex-direction: column; }}
  canvas {{ image-rendering: pixelated; image-rendering: crisp-edges; }}
  #info {{ color: #888; font: 12px monospace; margin-top: 8px; }}
  #stats {{ color: #6a6; font: 12px monospace; margin-top: 4px; }}
  #compact {{ color: #68c; font: 12px monospace; margin-top: 4px; }}
  #controls {{ color: #aaa; font: 13px monospace; margin-top: 6px; }}
</style>
</head>
<body>
<canvas id="c"></canvas>
<div id="info">Loading...</div>
<div id="stats"></div>
<div id="compact"></div>
<div id="controls">Space: pause/play &nbsp; R: restart</div>
<script>
const tilesW = {tiles_w}, tilesH = {tiles_h}, tileSize = {TILE_SIZE};
const fps = {fps};
const W = tilesW * tileSize, H = tilesH * tileSize;
const tilesPerFrame = tilesW * tilesH;
const codebook = {cb_js};
const vduSizes = {sizes_js};

// Decode frames: frame 0,1 = full keyframes, rest = delta from N-2
const allFrames = [];  // will hold full tilemaps

// Frame data (mix of full frames and deltas)
const frameData = [{",".join(frames_js_parts)}];

// Reconstruct full frames from deltas
for (let i = 0; i < frameData.length; i++) {{
  if (i < 2) {{
    // Keyframe: frameData[i] is full tilemap array
    allFrames.push(new Uint8Array(frameData[i]));
  }} else {{
    // Delta from frame i-2
    const prev = allFrames[i - 2];
    const curr = new Uint8Array(prev);
    const changes = frameData[i];
    for (const ch of changes) {{
      curr[ch[0]] = ch[1];
    }}
    allFrames.push(curr);
  }}
}}

const numFrames = allFrames.length;

// Canvas setup
const canvas = document.getElementById('c');
const scale = Math.min(Math.floor(window.innerHeight * 0.85 / H),
                       Math.floor(window.innerWidth * 0.95 / W), 4);
canvas.width = W * scale;
canvas.height = H * scale;
const ctx = canvas.getContext('2d');
ctx.imageSmoothingEnabled = false;

const offscreen = new OffscreenCanvas(W, H);
const offCtx = offscreen.getContext('2d');
const imgData = offCtx.createImageData(W, H);

const info = document.getElementById('info');
const stats = document.getElementById('stats');
let frameIdx = 0, playing = true, lastTime = 0;
const frameMs = 1000 / fps;

// Compute total PSRAM usage
const bitmapBytes = codebook.length * (8 + 64);  // header + 64B RGBA2222 each
const totalFrameBytes = vduSizes.reduce((a, b) => a + b, 0);
const totalPSRAM = bitmapBytes + totalFrameBytes;

// Compact format stats (injected by Python)
const compactAbsTotal = {compact_stats['compact_abs_total'] if compact_stats else 0};
const compactDeltaTotal = {compact_stats['compact_delta_total'] if compact_stats else 0};
const savingsAbsPct = {compact_stats['savings_abs_pct'] if compact_stats else 0:.1f};
const savingsDeltaPct = {compact_stats['savings_delta_pct'] if compact_stats else 0:.1f};

function renderFrame(idx) {{
  const tilemap = allFrames[idx];
  const pixels = imgData.data;
  for (let ty = 0; ty < tilesH; ty++) {{
    for (let tx = 0; tx < tilesW; tx++) {{
      const tid = tilemap[ty * tilesW + tx];
      const tile = codebook[tid];
      for (let r = 0; r < tileSize; r++) {{
        const rowByte = tile[r];
        for (let c = 0; c < tileSize; c++) {{
          const px = tx * tileSize + c;
          const py = ty * tileSize + r;
          const pidx = (py * W + px) * 4;
          const val = (rowByte >> (7 - c)) & 1 ? 255 : 0;
          pixels[pidx] = val;
          pixels[pidx + 1] = val;
          pixels[pidx + 2] = val;
          pixels[pidx + 3] = 255;
        }}
      }}
    }}
  }}
  offCtx.putImageData(imgData, 0, 0);
  ctx.drawImage(offscreen, 0, 0, W, H, 0, 0, canvas.width, canvas.height);

  const pct = ((idx / numFrames) * 100).toFixed(1);
  const sec = (idx / fps).toFixed(1);
  info.textContent = `Frame ${{idx}}/${{numFrames}} (${{sec}}s) ${{pct}}%`;
  const vduB = vduSizes[idx];
  stats.textContent = `VDP buffer: ${{vduB}}B | ` +
    `Total PSRAM: ${{(totalPSRAM/1024).toFixed(0)}}KB ` +
    `(bitmaps: ${{(bitmapBytes/1024).toFixed(1)}}KB + ` +
    `frames: ${{(totalFrameBytes/1024).toFixed(0)}}KB)`;
  if (compactAbsTotal > 0) {{
    document.getElementById('compact').textContent =
      `Compact: abs=${{(compactAbsTotal/1024).toFixed(0)}}KB (-${{savingsAbsPct.toFixed(0)}}%) | ` +
      `delta=${{(compactDeltaTotal/1024).toFixed(0)}}KB (-${{savingsDeltaPct.toFixed(0)}}%)`;
  }}
}}

function animate(ts) {{
  if (!lastTime) lastTime = ts;
  if (playing) {{
    const elapsed = ts - lastTime;
    if (elapsed >= frameMs) {{
      lastTime = ts - (elapsed % frameMs);
      renderFrame(frameIdx);
      frameIdx = (frameIdx + 1) % numFrames;
    }}
  }}
  requestAnimationFrame(animate);
}}

document.addEventListener('keydown', e => {{
  if (e.code === 'Space') {{ playing = !playing; e.preventDefault(); }}
  if (e.code === 'KeyR') {{ frameIdx = 0; lastTime = 0; playing = true; }}
}});

renderFrame(0);
requestAnimationFrame(animate);
</script>
</body>
</html>"""
    return html


def generate_html_2s(codebook, encoded_frames, tiles_w, tiles_h, fps, gop_size=300):
    """Generate HTML preview that decodes BA2S compressed data in-browser.

    Embeds LZSS-compressed masks and Huffman-coded IDs as base64.
    JavaScript implements both decoders — true end-to-end format test."""
    tiles_per_frame = tiles_w * tiles_h
    mask_bytes_per_frame = (tiles_per_frame + 7) // 8
    num_frames = len(encoded_frames)
    num_gops = (num_frames + gop_size - 1) // gop_size

    # Build masks and ID streams
    all_masks = []
    all_id_streams = []
    all_update_ids = []
    for f in range(num_frames):
        mask = bytearray(mask_bytes_per_frame)
        ids = []
        if f < 2:
            for pos in range(tiles_per_frame):
                mask[pos // 8] |= (1 << (pos % 8))
                ids.append(encoded_frames[f][pos])
        else:
            prev = encoded_frames[f - 2]
            curr = encoded_frames[f]
            for pos in range(tiles_per_frame):
                if curr[pos] != prev[pos]:
                    mask[pos // 8] |= (1 << (pos % 8))
                    ids.append(curr[pos])
        all_masks.append(bytes(mask))
        all_id_streams.append(ids)
        all_update_ids.extend(ids)

    # Build Huffman codes
    id_freq = Counter(all_update_ids)
    huffman_codes = build_huffman_codes(id_freq)
    huffman_table = serialize_huffman_table(huffman_codes)
    huffman_b64 = base64.b64encode(huffman_table).decode()

    # Compress GOPs and build JS data
    total_mask_raw = 0
    total_mask_comp = 0
    total_id_raw = 0
    total_id_comp = 0
    gop_js_parts = []
    for gop_idx in range(num_gops):
        gop_start = gop_idx * gop_size
        gop_end = min(gop_start + gop_size, num_frames)
        gop_frames = gop_end - gop_start

        gop_mask_data = b''.join(all_masks[gop_start:gop_end])
        mask_compressed = lzss_compress(gop_mask_data)
        total_mask_raw += len(gop_mask_data)
        total_mask_comp += len(mask_compressed)

        gop_ids = []
        for fi in range(gop_start, gop_end):
            gop_ids.extend(all_id_streams[fi])
        id_compressed = huffman_encode_stream(bytes(gop_ids), huffman_codes)
        total_id_raw += len(gop_ids)
        total_id_comp += len(id_compressed)

        masks_b64 = base64.b64encode(mask_compressed).decode()
        ids_b64 = base64.b64encode(id_compressed).decode()
        gop_js_parts.append(
            f'{{m:"{masks_b64}",i:"{ids_b64}",'
            f'f:{gop_frames},n:{len(gop_ids)},ml:{len(gop_mask_data)}}}'
        )

    gops_js = "[\n" + ",\n".join(gop_js_parts) + "\n]"

    # Codebook as JS array (tile bit patterns — 8 bytes per tile)
    cb_js = "["
    for i, key in enumerate(codebook):
        cb_js += "[" + ",".join(str(b) for b in key) + "]"
        if i < len(codebook) - 1:
            cb_js += ","
    cb_js += "]"

    total_compressed = total_mask_comp + total_id_comp + len(huffman_table)

    html = f"""<!DOCTYPE html>
<html>
<head>
<meta charset="utf-8">
<title>Bad Apple BA2S Decoder — Two-Stream Compressed</title>
<style>
  body {{ margin: 0; background: #000; display: flex; justify-content: center;
         align-items: center; min-height: 100vh; flex-direction: column; }}
  canvas {{ image-rendering: pixelated; image-rendering: crisp-edges; }}
  #info {{ color: #888; font: 12px monospace; margin-top: 8px; }}
  #stats {{ color: #6a6; font: 12px monospace; margin-top: 4px; }}
  #decode {{ color: #68c; font: 12px monospace; margin-top: 4px; }}
  #controls {{ color: #aaa; font: 13px monospace; margin-top: 6px; }}
</style>
</head>
<body>
<canvas id="c"></canvas>
<div id="info">Decoding BA2S data...</div>
<div id="stats"></div>
<div id="decode"></div>
<div id="controls">Space: pause/play &nbsp; R: restart</div>
<script>
const tilesW = {tiles_w}, tilesH = {tiles_h}, tileSize = {TILE_SIZE};
const fps = {fps};
const W = tilesW * tileSize, H = tilesH * tileSize;
const tilesPerFrame = tilesW * tilesH;
const maskBytesPerFrame = Math.ceil(tilesPerFrame / 8);
const codebook = {cb_js};
const huffTableB64 = "{huffman_b64}";
const gopData = {gops_js};

// ============================================================
// LZSS Decompressor (matches Python lzss_decompress)
// Flag bit=1 → literal, bit=0 → match(offset-1, length-3)
// LSB-first flag bits
// ============================================================
function lzssDecompress(data, originalLen) {{
  const out = new Uint8Array(originalLen);
  let op = 0, ip = 0;
  while (op < originalLen && ip < data.length) {{
    const flag = data[ip++];
    for (let bit = 0; bit < 8; bit++) {{
      if (op >= originalLen || ip >= data.length) break;
      if (flag & (1 << bit)) {{
        out[op++] = data[ip++];
      }} else {{
        if (ip + 1 >= data.length) break;
        const offset = data[ip++] + 1;
        const length = data[ip++] + 3;
        const start = op - offset;
        for (let j = 0; j < length && op < originalLen; j++)
          out[op++] = out[start + j];
      }}
    }}
  }}
  return out;
}}

// ============================================================
// Canonical Huffman Decoder
// Table format: max_len + counts[1..max_len] + symbols
// Bit packing: MSB-first
// ============================================================
function parseHuffmanTable(data) {{
  if (!data.length || data[0] === 0) return null;
  const maxLen = data[0];
  let pos = 1;
  const counts = [];
  for (let l = 1; l <= maxLen; l++) counts.push(data[pos++]);

  // Build canonical codes
  const entries = [];
  let code = 0, prevLen = 0;
  for (let l = 0; l < counts.length; l++) {{
    const codeLen = l + 1;
    for (let i = 0; i < counts[l]; i++) {{
      const sym = data[pos++];
      if (entries.length > 0) {{
        code++;
        code <<= (codeLen - prevLen);
      }}
      entries.push({{symbol: sym, code, codeLen}});
      prevLen = codeLen;
    }}
  }}

  // Build binary tree
  const root = {{c: [null, null], s: -1}};
  for (const {{symbol, code, codeLen}} of entries) {{
    let node = root;
    for (let i = codeLen - 1; i >= 0; i--) {{
      const bit = (code >> i) & 1;
      if (!node.c[bit]) node.c[bit] = {{c: [null, null], s: -1}};
      node = node.c[bit];
    }}
    node.s = symbol;
  }}
  return root;
}}

function huffmanDecode(data, tree, numSymbols) {{
  const out = new Uint8Array(numSymbols);
  let op = 0, bitPos = 0;
  const totalBits = data.length * 8;
  let node = tree;
  while (op < numSymbols && bitPos < totalBits) {{
    const byteIdx = bitPos >> 3;
    const bitIdx = 7 - (bitPos & 7);
    const bit = (data[byteIdx] >> bitIdx) & 1;
    bitPos++;
    node = node.c[bit];
    if (!node) throw new Error('Invalid Huffman code at bit ' + (bitPos-1));
    if (node.s >= 0) {{
      out[op++] = node.s;
      node = tree;
    }}
  }}
  return out;
}}

// ============================================================
// Decode BA2S compressed data into frames
// ============================================================
function b64ToU8(s) {{
  const bin = atob(s);
  const u8 = new Uint8Array(bin.length);
  for (let i = 0; i < bin.length; i++) u8[i] = bin.charCodeAt(i);
  return u8;
}}

const t0 = performance.now();
const huffTree = parseHuffmanTable(b64ToU8(huffTableB64));
const allFrames = [];

for (const gop of gopData) {{
  const maskComp = b64ToU8(gop.m);
  const idComp = b64ToU8(gop.i);

  const masks = lzssDecompress(maskComp, gop.ml);
  const ids = huffmanDecode(idComp, huffTree, gop.n);

  let idCursor = 0;
  for (let f = 0; f < gop.f; f++) {{
    const frameIdx = allFrames.length;
    const maskOff = f * maskBytesPerFrame;
    let frame;
    if (frameIdx < 2) {{
      frame = new Uint8Array(tilesPerFrame);
    }} else {{
      frame = new Uint8Array(allFrames[frameIdx - 2]);
    }}
    for (let pos = 0; pos < tilesPerFrame; pos++) {{
      if (masks[maskOff + (pos >> 3)] & (1 << (pos & 7))) {{
        frame[pos] = ids[idCursor++];
      }}
    }}
    allFrames.push(frame);
  }}
}}
const decodeMs = (performance.now() - t0).toFixed(1);
const numFrames = allFrames.length;

// ============================================================
// Renderer (same as regular HTML preview)
// ============================================================
const canvas = document.getElementById('c');
const scale = Math.min(Math.floor(window.innerHeight * 0.85 / H),
                       Math.floor(window.innerWidth * 0.95 / W), 4);
canvas.width = W * scale;
canvas.height = H * scale;
const ctx = canvas.getContext('2d');
ctx.imageSmoothingEnabled = false;

const offscreen = new OffscreenCanvas(W, H);
const offCtx = offscreen.getContext('2d');
const imgData = offCtx.createImageData(W, H);

const info = document.getElementById('info');
const statsEl = document.getElementById('stats');
const decodeEl = document.getElementById('decode');
let frameIdx = 0, playing = true, lastTime = 0;
const frameMs = 1000 / fps;

decodeEl.textContent = `BA2S decoded: ${{numFrames}} frames in ${{decodeMs}}ms | ` +
  `masks ${{({total_mask_raw}/1024).toFixed(0)}}K\\u2192${{({total_mask_comp}/1024).toFixed(0)}}K (LZSS) | ` +
  `IDs ${{({total_id_raw}/1024).toFixed(0)}}K\\u2192${{({total_id_comp}/1024).toFixed(0)}}K (Huffman) | ` +
  `total ${{({total_compressed}/1024).toFixed(0)}}KB compressed`;

function renderFrame(idx) {{
  const tilemap = allFrames[idx];
  const pixels = imgData.data;
  for (let ty = 0; ty < tilesH; ty++) {{
    for (let tx = 0; tx < tilesW; tx++) {{
      const tid = tilemap[ty * tilesW + tx];
      const tile = codebook[tid];
      for (let r = 0; r < tileSize; r++) {{
        const rowByte = tile[r];
        for (let c = 0; c < tileSize; c++) {{
          const px = tx * tileSize + c;
          const py = ty * tileSize + r;
          const pidx = (py * W + px) * 4;
          const val = (rowByte >> (7 - c)) & 1 ? 255 : 0;
          pixels[pidx] = val;
          pixels[pidx + 1] = val;
          pixels[pidx + 2] = val;
          pixels[pidx + 3] = 255;
        }}
      }}
    }}
  }}
  offCtx.putImageData(imgData, 0, 0);
  ctx.drawImage(offscreen, 0, 0, W, H, 0, 0, canvas.width, canvas.height);

  const pct = ((idx / numFrames) * 100).toFixed(1);
  const sec = (idx / fps).toFixed(1);
  info.textContent = `Frame ${{idx}}/${{numFrames}} (${{sec}}s) ${{pct}}%`;
  statsEl.textContent = `GOP ${{Math.floor(idx / {gop_size})}} | ` +
    `${{(numFrames / fps).toFixed(1)}}s at ${{fps}}fps`;
}}

function animate(ts) {{
  if (!lastTime) lastTime = ts;
  if (playing) {{
    const elapsed = ts - lastTime;
    if (elapsed >= frameMs) {{
      lastTime = ts - (elapsed % frameMs);
      renderFrame(frameIdx);
      frameIdx = (frameIdx + 1) % numFrames;
    }}
  }}
  requestAnimationFrame(animate);
}}

document.addEventListener('keydown', e => {{
  if (e.code === 'Space') {{ playing = !playing; e.preventDefault(); }}
  if (e.code === 'KeyR') {{ frameIdx = 0; lastTime = 0; playing = true; }}
}});

renderFrame(0);
requestAnimationFrame(animate);
</script>
</body>
</html>"""
    return html


def main():
    parser = argparse.ArgumentParser(description="Bad Apple VDP buffer generator")
    parser.add_argument("--video", type=str, default=str(VIDEO_PATH))
    parser.add_argument("--html", type=str, help="Output HTML preview")
    parser.add_argument("--html-2s", type=str, help="Output HTML with BA2S decoder (compressed)")
    parser.add_argument("--output", type=str, help="Output binary .dat for eZ80 player (uncompressed VDP buffers)")
    parser.add_argument("--output-2s", type=str, help="Output compressed two-stream .dat (LZSS masks + Huffman IDs)")
    parser.add_argument("--gop-size", type=int, default=300, help="GOP size for --output-2s (default 300)")
    parser.add_argument("--preview", action="store_true", help="Live VDP preview via fake_ez80")
    parser.add_argument("--port", type=int, default=5001, help="TCP port for --preview")
    parser.add_argument("--replay-file", type=str, help="Output VSYNC-chunked .vdu for agon-vdp --replay")
    parser.add_argument("--width", type=int, default=320)
    parser.add_argument("--height", type=int, default=240)
    parser.add_argument("--codebook", type=int, default=256)
    parser.add_argument("--charprint", action="store_true",
                        help="Use 224-tile charprint encoding (tile IDs 32-255, VDU 5 text mode)")
    parser.add_argument("--codebook-mode", choices=["freq", "merge", "structured"],
                        default="freq", help="Codebook strategy")
    parser.add_argument("--sort-codebook", choices=["hamming", "transitions"],
                        default=None,
                        help="Reorder codebook: hamming (visual similarity) or transitions (minimize deltas)")
    parser.add_argument("--frames", type=int, default=1000)
    parser.add_argument("--fps", type=int, default=30)
    parser.add_argument("--frames-dir", type=str, default=None,
                        help="Use pre-extracted frame images from directory instead of ffmpeg")
    parser.add_argument("--frame-step", type=int, default=1,
                        help="Take every Nth frame (2 = 15fps from 30fps source)")
    args = parser.parse_args()

    tiles_w = args.width // TILE_SIZE
    tiles_h = args.height // TILE_SIZE
    tiles_per_frame = tiles_w * tiles_h

    if args.charprint:
        args.codebook = 224
        print(f"Charprint mode: 224 tiles, IDs 32-255", file=sys.stderr)

    print(f"Target: {args.width}x{args.height}, {tiles_w}x{tiles_h} grid, "
          f"{tiles_per_frame} tiles/frame, {args.frames} frames", file=sys.stderr)

    with tempfile.TemporaryDirectory(prefix="badapple_vdp_") as tmpdir:
        if args.frames_dir:
            print(f"Loading frames from {args.frames_dir}...", file=sys.stderr)
            frames = sorted(Path(args.frames_dir).glob("*.jpg")) + \
                     sorted(Path(args.frames_dir).glob("*.png"))
            frames = frames[:args.frames]
        else:
            print("Extracting frames...", file=sys.stderr)
            frames = extract_frames(args.video, args.width, args.height, tmpdir, args.frames)
        if args.frame_step > 1:
            frames = frames[::args.frame_step]
        num_frames = len(frames)
        print(f"{num_frames} frames (step={args.frame_step})", file=sys.stderr)

        print("Building tilemaps...", file=sys.stderr)
        all_tilemaps = []
        for i, fpath in enumerate(frames):
            if i % 500 == 0:
                print(f"  Frame {i}/{num_frames}...", file=sys.stderr)
            all_tilemaps.append(frame_to_tilemap(fpath, target_size=(args.width, args.height)))

    print(f"Building codebook (mode={args.codebook_mode})...", file=sys.stderr)
    if args.codebook_mode == "merge":
        codebook, key_to_id = build_codebook_merge(all_tilemaps, args.codebook)
    elif args.codebook_mode == "structured":
        codebook, key_to_id = build_codebook_structured(all_tilemaps, args.codebook)
    else:
        codebook, key_to_id = build_codebook(all_tilemaps, args.codebook)

    exact_rate, mean_dist = measure_quality(all_tilemaps, codebook, key_to_id)
    print(f"  Exact match: {exact_rate:.1%}, mean distortion: {mean_dist:.2f} bits/tile",
          file=sys.stderr)

    if args.sort_codebook == "hamming":
        print("Sorting codebook by Hamming distance...", file=sys.stderr)
        codebook, key_to_id = sort_codebook_hamming(codebook, key_to_id)

    print("Encoding frames...", file=sys.stderr)
    encoded_frames = []
    for i, tm in enumerate(all_tilemaps):
        if i % 500 == 0:
            print(f"  Encoding {i}/{num_frames}...", file=sys.stderr)
        encoded_frames.append(encode_tilemap(tm, key_to_id, codebook))

    if args.sort_codebook == "transitions":
        print("Sorting codebook by transition frequency...", file=sys.stderr)
        codebook, key_to_id = sort_codebook_transitions(
            codebook, key_to_id, encoded_frames)
        # Re-encode with new IDs
        print("Re-encoding frames with sorted codebook...", file=sys.stderr)
        encoded_frames = []
        for i, tm in enumerate(all_tilemaps):
            encoded_frames.append(encode_tilemap(tm, key_to_id, codebook))

    # Compact delta format analysis
    print("\nCompact delta format analysis...", file=sys.stderr)
    compact_stats = analyze_compact_delta(encoded_frames, tiles_w, tiles_h,
                                          sorted_cb=args.sort_codebook is not None)
    print(f"  VDU format:          {compact_stats['vdu_total']:,}B "
          f"({compact_stats['vdu_mean']:.0f}B/frame)", file=sys.stderr)
    print(f"  Compact (abs IDs):   {compact_stats['compact_abs_total']:,}B "
          f"({compact_stats['compact_abs_mean']:.0f}B/frame) "
          f"— {compact_stats['savings_abs_pct']:.0f}% smaller", file=sys.stderr)
    print(f"  Compact (delta IDs): {compact_stats['compact_delta_total']:,}B "
          f"({compact_stats['compact_delta_mean']:.0f}B/frame) "
          f"— {compact_stats['savings_delta_pct']:.0f}% smaller", file=sys.stderr)

    # Delta ID distribution (top 10)
    hist = compact_stats['delta_id_histogram']
    if hist:
        total_deltas = sum(hist.values())
        print(f"  Delta ID distribution (|delta|, count, cumulative%):", file=sys.stderr)
        cumul = 0
        for val, cnt in sorted(hist.items())[:15]:
            cumul += cnt
            print(f"    |{val:3d}|: {cnt:6d} ({cumul/total_deltas:5.1%})", file=sys.stderr)

    # Mask compression analysis
    print(f"\nMask compression analysis (GOP={min(300, num_frames)})...", file=sys.stderr)
    mask_stats = analyze_mask_compression(encoded_frames, tiles_w, tiles_h,
                                          gop_size=min(300, num_frames))
    ms = mask_stats
    print(f"  Raw masks:           {ms['raw_total']:,}B "
          f"({ms['mask_bytes_per_frame']}B/frame × {num_frames})", file=sys.stderr)
    print(f"  Zero-byte density:   {ms['zero_byte_pct']:.0f}% "
          f"(frames with no changes: {ms['zero_masks']})", file=sys.stderr)
    print(f"  RLE per frame:       {ms['rle_total']:,}B "
          f"({ms['rle_total']/num_frames:.0f}B/frame, "
          f"{(1-ms['rle_total']/ms['raw_total'])*100:.0f}% smaller)", file=sys.stderr)
    print(f"  zlib per frame:      {ms['zlib_per_frame_total']:,}B "
          f"({ms['zlib_per_frame_total']/num_frames:.0f}B/frame, "
          f"{(1-ms['zlib_per_frame_total']/ms['raw_total'])*100:.0f}% smaller)", file=sys.stderr)
    print(f"  zlib row-major all:  {ms['all_zlib']:,}B "
          f"({(1-ms['all_zlib']/ms['raw_total'])*100:.0f}% smaller)", file=sys.stderr)
    print(f"  Z-transposed + zlib: {ms['all_z_zlib']:,}B "
          f"({(1-ms['all_z_zlib']/ms['raw_total'])*100:.0f}% smaller)", file=sys.stderr)
    print(f"  XOR-delta + zlib:    {ms['all_xor_zlib']:,}B "
          f"({(1-ms['all_xor_zlib']/ms['raw_total'])*100:.0f}% smaller) "
          f"[XOR zero-byte: {ms['xor_zero_pct']:.0f}%]", file=sys.stderr)
    print(f"  XOR + Z-trans + zlib:{ms['all_xor_z_zlib']:,}B "
          f"({(1-ms['all_xor_z_zlib']/ms['raw_total'])*100:.0f}% smaller)", file=sys.stderr)
    if ms['gop_stats']:
        print(f"  Per-GOP breakdown ({ms['num_gops']} GOPs of {ms['gop_size']}):",
              file=sys.stderr)
        for i, gs in enumerate(ms['gop_stats']):
            print(f"    GOP {i}: {gs['frames']}fr | "
                  f"raw={gs['raw']:,}B | "
                  f"Z+zlib={gs['z_zlib']:,}B ({(1-gs['z_zlib']/gs['raw'])*100:.0f}%) | "
                  f"Z+RLE={gs['z_rle']:,}B ({(1-gs['z_rle']/gs['raw'])*100:.0f}%) | "
                  f"row+zlib={gs['row_zlib']:,}B ({(1-gs['row_zlib']/gs['raw'])*100:.0f}%)",
                  file=sys.stderr)

    # Total compact format with mask compression
    id_stream_total = compact_stats['compact_abs_total'] - ms['raw_total']
    print(f"\n  Combined compact format:", file=sys.stderr)
    print(f"    Raw masks + abs IDs:         {compact_stats['compact_abs_total']:,}B "
          f"(masks {ms['raw_total']:,} + IDs {id_stream_total:,})", file=sys.stderr)
    best_mask = ms['all_z_zlib']
    print(f"    Z-transposed masks + abs IDs: {best_mask + id_stream_total:,}B "
          f"(masks {best_mask:,} + IDs {id_stream_total:,})", file=sys.stderr)
    print(f"    vs VDU format:               {compact_stats['vdu_total']:,}B "
          f"({(1-(best_mask+id_stream_total)/compact_stats['vdu_total'])*100:.0f}% smaller)",
          file=sys.stderr)

    # ID stream compression analysis
    print(f"\nID stream compression analysis...", file=sys.stderr)
    id_stats = analyze_id_stream(encoded_frames, tiles_w, tiles_h,
                                  gop_size=min(300, num_frames))
    ids = id_stats
    print(f"  Sparse IDs (changed only):  {ids['sparse_total']:,}B", file=sys.stderr)
    print(f"    + RLE:                    {ids['sparse_rle']:,}B "
          f"({(1-ids['sparse_rle']/ids['sparse_total'])*100:.0f}% smaller)",
          file=sys.stderr)
    print(f"    + LZSS (win=256):         {ids['sparse_lzss']:,}B "
          f"({(1-ids['sparse_lzss']/ids['sparse_total'])*100:.0f}% smaller)",
          file=sys.stderr)
    print(f"    + zlib:                   {ids['sparse_zlib']:,}B "
          f"({(1-ids['sparse_zlib']/ids['sparse_total'])*100:.0f}% smaller)",
          file=sys.stderr)
    print(f"  Dense tilemaps (full):      {ids['dense_total']:,}B "
          f"({tiles_per_frame}B/frame)", file=sys.stderr)
    print(f"    Z-transposed + zlib:      {ids['z_dense_zlib']:,}B "
          f"({(1-ids['z_dense_zlib']/ids['dense_total'])*100:.0f}% smaller)",
          file=sys.stderr)
    print(f"    Z-transposed + LZSS:      {ids['z_lzss']:,}B "
          f"({(1-ids['z_lzss']/ids['dense_total'])*100:.0f}% smaller)",
          file=sys.stderr)
    print(f"    Z-transposed + RLE:       {ids['z_rle']:,}B "
          f"({(1-ids['z_rle']/ids['dense_total'])*100:.0f}% smaller)",
          file=sys.stderr)
    print(f"    XOR + Z-trans + zlib:     {ids['xor_z_zlib']:,}B "
          f"({(1-ids['xor_z_zlib']/ids['dense_total'])*100:.0f}% smaller) "
          f"[XOR zeros: {ids['xor_zero_pct']:.0f}%]", file=sys.stderr)
    print(f"    XOR + Z-trans + LZSS:     {ids['xor_z_lzss']:,}B "
          f"({(1-ids['xor_z_lzss']/ids['dense_total'])*100:.0f}% smaller)",
          file=sys.stderr)

    # Grand total comparison
    print(f"\n{'='*60}", file=sys.stderr)
    print(f"GRAND TOTAL COMPARISON", file=sys.stderr)
    print(f"{'='*60}", file=sys.stderr)
    vdu_total = compact_stats['vdu_total']
    # Option 1: current VDU buffers
    print(f"  VDU buffers (current):      {vdu_total:,}B ({vdu_total/1024:.0f}KB)",
          file=sys.stderr)
    # Option 2: raw masks + sparse IDs
    opt2 = ms['raw_total'] + ids['sparse_total']
    print(f"  Raw masks + sparse IDs:     {opt2:,}B ({opt2/1024:.0f}KB) "
          f"[{(1-opt2/vdu_total)*100:.0f}% smaller]", file=sys.stderr)
    # Option 3: Z-masks(zlib) + sparse IDs
    opt3 = ms['all_z_zlib'] + ids['sparse_total']
    print(f"  Z-masks(zlib) + sparse:     {opt3:,}B ({opt3/1024:.0f}KB) "
          f"[{(1-opt3/vdu_total)*100:.0f}% smaller]", file=sys.stderr)
    # Option 4: no masks, dense Z-transposed + zlib
    opt4 = ids['z_dense_zlib']
    print(f"  Dense Z-trans + zlib:       {opt4:,}B ({opt4/1024:.0f}KB) "
          f"[{(1-opt4/vdu_total)*100:.0f}% smaller]", file=sys.stderr)
    # Option 5: no masks, XOR + Z-trans + zlib
    opt5 = ids['xor_z_zlib']
    print(f"  Dense XOR+Z-trans+zlib:     {opt5:,}B ({opt5/1024:.0f}KB) "
          f"[{(1-opt5/vdu_total)*100:.0f}% smaller]", file=sys.stderr)
    # Option 6: eZ80-friendly: RLE masks + LZSS IDs
    opt6_masks = ms['rle_total']  # RLE masks per frame
    opt6_ids = ids['sparse_lzss']
    opt6 = opt6_masks + opt6_ids
    print(f"  RLE masks + LZSS sparse:    {opt6:,}B ({opt6/1024:.0f}KB) "
          f"[{(1-opt6/vdu_total)*100:.0f}% smaller] ← eZ80-friendly", file=sys.stderr)
    # Option 7: XOR masks RLE + sparse LZSS
    # need XOR mask RLE... let me compute
    opt7_ids = ids['sparse_lzss']
    opt7 = ms['z_rle_total'] + opt7_ids if 'z_rle_total' in ms else opt6
    # Option 8: Z-trans dense LZSS (no masks needed)
    opt8 = ids['z_lzss']
    print(f"  Dense Z-trans + LZSS:       {opt8:,}B ({opt8/1024:.0f}KB) "
          f"[{(1-opt8/vdu_total)*100:.0f}% smaller] ← eZ80-friendly", file=sys.stderr)
    # Option 9: XOR + Z-trans + LZSS
    opt9 = ids['xor_z_lzss']
    print(f"  Dense XOR+Z-trans+LZSS:     {opt9:,}B ({opt9/1024:.0f}KB) "
          f"[{(1-opt9/vdu_total)*100:.0f}% smaller] ← eZ80-friendly", file=sys.stderr)

    # Two-stream architecture analysis
    print(f"\n{'='*60}", file=sys.stderr)
    print(f"TWO-STREAM ARCHITECTURE (eZ80 target)", file=sys.stderr)
    print(f"{'='*60}", file=sys.stderr)
    ts = analyze_two_stream(encoded_frames, tiles_w, tiles_h,
                             gop_size=min(300, num_frames))

    print(f"\nMASK STREAM (where changes are):", file=sys.stderr)
    print(f"  Raw:                {ts['mask_raw']:,}B", file=sys.stderr)
    print(f"  LZSS per GOP:       {ts['mask_lzss']:,}B "
          f"({(1-ts['mask_lzss']/ts['mask_raw'])*100:.0f}% smaller)", file=sys.stderr)
    print(f"  XOR + LZSS per GOP: {ts['mask_xor_lzss']:,}B "
          f"({(1-ts['mask_xor_lzss']/ts['mask_raw'])*100:.0f}% smaller)", file=sys.stderr)
    print(f"  Z-trans + LZSS GOP: {ts['mask_z_lzss']:,}B "
          f"({(1-ts['mask_z_lzss']/ts['mask_raw'])*100:.0f}% smaller)", file=sys.stderr)
    print(f"  --- Bit-level temporal packing ---", file=sys.stderr)
    print(f"  Temporal raw:       {ts['mask_temporal_raw']:,}B "
          f"(zero-byte density: {ts['mask_temporal_zero_pct']:.0f}%, "
          f"tiles never change: {ts['tiles_never_change']}/{tiles_per_frame})",
          file=sys.stderr)
    print(f"  Temporal + LZSS:    {ts['mask_temporal_lzss']:,}B "
          f"({(1-ts['mask_temporal_lzss']/ts['mask_raw'])*100:.0f}% smaller)",
          file=sys.stderr)
    print(f"  Temporal + zlib:    {ts['mask_temporal_zlib']:,}B "
          f"({(1-ts['mask_temporal_zlib']/ts['mask_raw'])*100:.0f}% smaller)",
          file=sys.stderr)
    print(f"  Temporal GOP LZSS:  {ts['mask_temporal_gop_lzss']:,}B "
          f"({(1-ts['mask_temporal_gop_lzss']/ts['mask_raw'])*100:.0f}% smaller)",
          file=sys.stderr)
    print(f"  Temporal GOP zlib:  {ts['mask_temporal_gop_zlib']:,}B "
          f"({(1-ts['mask_temporal_gop_zlib']/ts['mask_raw'])*100:.0f}% smaller)",
          file=sys.stderr)

    print(f"\nID STREAM (what tiles are):", file=sys.stderr)
    print(f"  Raw:                {ts['id_raw']:,}B "
          f"({ts['total_updates']:,} updates, {ts['id_unique']} unique IDs)", file=sys.stderr)
    print(f"  Shannon entropy:    {ts['id_entropy']:.2f} bits/ID "
          f"(vs 8 bits raw = {(1-ts['id_entropy']/8)*100:.0f}% theoretical saving)",
          file=sys.stderr)
    print(f"  Huffman estimate:   {ts['id_huffman_with_table']:,}B "
          f"({ts['id_huffman_bytes']:,}B data + table) "
          f"({(1-ts['id_huffman_with_table']/ts['id_raw'])*100:.0f}% smaller)",
          file=sys.stderr)
    print(f"  LZSS per GOP:       {ts['id_lzss']:,}B "
          f"({(1-ts['id_lzss']/ts['id_raw'])*100:.0f}% smaller)", file=sys.stderr)
    print(f"  zlib (theoretical): {ts['id_zlib']:,}B "
          f"({(1-ts['id_zlib']/ts['id_raw'])*100:.0f}% smaller)", file=sys.stderr)

    print(f"\n  Top-15 updater tiles (ID: count, %cumul):", file=sys.stderr)
    cumul = 0
    for tid, cnt in ts['id_top'][:15]:
        cumul += cnt
        print(f"    ID {tid:3d}: {cnt:6,}× ({cumul/ts['total_updates']:5.1%})",
              file=sys.stderr)

    print(f"\nCOMBINED FORMATS:", file=sys.stderr)
    vdu_total = compact_stats['vdu_total']
    fmt = [
        ("VDU buffers (current)", vdu_total),
        ("Raw masks + raw IDs", ts['raw_masks_raw_ids']),
        ("LZSS masks + Huffman IDs", ts['lzss_masks_huffman_ids']),
        ("XOR+LZSS masks + Huffman IDs", ts['xor_lzss_masks_huffman_ids']),
        ("Z-trans+LZSS masks + Huffman IDs", ts['z_lzss_masks_huffman_ids']),
        ("Temporal+LZSS masks + Huffman IDs", ts['temporal_lzss_huffman_ids']),
        ("LZSS masks + LZSS IDs", ts['lzss_masks_lzss_ids']),
    ]
    for name, size in fmt:
        pct = (1 - size / vdu_total) * 100 if size < vdu_total else 0
        marker = ""
        if "Huffman" in name:
            marker = " ← target"
        elif name == "VDU":
            marker = ""
        print(f"  {name:40s} {size:>10,}B ({size/1024:>6.0f}KB) "
              f"[{pct:+.0f}%]{marker}", file=sys.stderr)

    # Build VDP bitmap/char upload commands
    print("\nBuilding VDP commands...", file=sys.stderr)
    if args.charprint:
        chardef_commands = build_chardef_commands(codebook, id_offset=32)
        # Create bitmap_uploads-compatible list for downstream code
        bitmap_uploads = chardef_commands  # (char_code, vdu_bytes) tuples
    else:
        bitmap_uploads = build_bitmap_upload_commands(codebook)
    bitmap_total = sum(len(wrap_vdp_buffer(bid, vdu)) for bid, vdu in bitmap_uploads)

    # Build frame buffers with delta from N-2 (double-buffer aware)
    frame_vdu_sizes = []
    frame_total = 0
    keyframe_count = 0
    delta_changes = []

    for f in range(num_frames):
        if f < 2:
            # Keyframes (first two frames)
            is_keyframe = True
            prev = None
            keyframe_count += 1
        else:
            # Delta from frame f-2 (back buffer state)
            is_keyframe = False
            prev = encoded_frames[f - 2]

        vdu = build_frame_buffer(f, encoded_frames[f], prev,
                                 tiles_w, tiles_h, num_frames, is_keyframe)
        wrapped = wrap_vdp_buffer(FRAME_BASE_ID + f, vdu)
        frame_vdu_sizes.append(len(vdu))
        frame_total += len(wrapped)

        if not is_keyframe:
            changes = sum(1 for a, b in zip(encoded_frames[f], encoded_frames[f-2]) if a != b)
            delta_changes.append(changes)

    total_psram = bitmap_total + frame_total

    print(f"\n{'='*60}", file=sys.stderr)
    print(f"VDP BUFFER ANALYSIS", file=sys.stderr)
    print(f"{'='*60}", file=sys.stderr)
    print(f"Frames:           {num_frames}", file=sys.stderr)
    print(f"Tile bitmaps:     {len(codebook)} × {8+64}B = "
          f"{bitmap_total:,}B ({bitmap_total/1024:.1f}KB)", file=sys.stderr)
    print(f"Keyframes:        {keyframe_count}", file=sys.stderr)

    vdu_arr = np.array(frame_vdu_sizes)
    print(f"\nFrame buffer sizes:", file=sys.stderr)
    print(f"  Min:    {vdu_arr.min():,}B", file=sys.stderr)
    print(f"  Max:    {vdu_arr.max():,}B", file=sys.stderr)
    print(f"  Mean:   {vdu_arr.mean():,.0f}B", file=sys.stderr)
    print(f"  Median: {np.median(vdu_arr):,.0f}B", file=sys.stderr)
    print(f"  P80:    {np.percentile(vdu_arr, 80):,.0f}B", file=sys.stderr)
    print(f"  Total:  {frame_total:,}B ({frame_total/1024:.0f}KB)", file=sys.stderr)

    if delta_changes:
        dc = np.array(delta_changes)
        print(f"\nDelta changes (from N-2):", file=sys.stderr)
        print(f"  Mean:   {dc.mean():.0f} tiles ({dc.mean()/tiles_per_frame:.1%})", file=sys.stderr)
        print(f"  Median: {np.median(dc):.0f} tiles", file=sys.stderr)
        print(f"  P80:    {np.percentile(dc, 80):.0f} tiles", file=sys.stderr)

    print(f"\nTotal PSRAM: {total_psram:,}B ({total_psram/1024:.0f}KB / "
          f"{total_psram/1024/1024:.1f}MB)", file=sys.stderr)
    print(f"VDP PSRAM available: ~4MB", file=sys.stderr)
    fits = "YES" if total_psram < 4 * 1024 * 1024 else "NO"
    print(f"Fits in PSRAM: {fits}", file=sys.stderr)

    max_frames_est = int(4 * 1024 * 1024 / (frame_total / num_frames)) if frame_total > 0 else 0
    print(f"Estimated max frames at this rate: ~{max_frames_est}", file=sys.stderr)

    # Playback stats
    effective_fps = args.fps // args.frame_step if args.frame_step > 1 else args.fps
    duration = num_frames / effective_fps
    print(f"\nPlayback: {duration:.1f}s at {effective_fps}fps", file=sys.stderr)
    print(f"eZ80 bandwidth during playback: 0 bytes (fully autonomous!)", file=sys.stderr)

    # Generate HTML preview
    if args.html:
        print(f"\nGenerating HTML preview...", file=sys.stderr)
        html = generate_html(codebook, encoded_frames, frame_vdu_sizes,
                             tiles_w, tiles_h, effective_fps, compact_stats)
        with open(args.html, "w") as f:
            f.write(html)
        html_size = os.path.getsize(args.html)
        print(f"Written: {args.html} ({html_size:,}B / {html_size/1024:.0f}KB)",
              file=sys.stderr)

    # Generate HTML with BA2S decoder
    if args.html_2s:
        print(f"\nGenerating BA2S HTML decoder...", file=sys.stderr)
        html = generate_html_2s(codebook, encoded_frames,
                                tiles_w, tiles_h, effective_fps, args.gop_size)
        with open(args.html_2s, "w") as f:
            f.write(html)
        html_size = os.path.getsize(args.html_2s)
        print(f"Written: {args.html_2s} ({html_size:,}B / {html_size/1024:.0f}KB)",
              file=sys.stderr)

    # Generate binary .dat file for eZ80 player
    if args.output:
        print(f"\nGenerating binary data file...", file=sys.stderr)
        with open(args.output, "wb") as f:
            # Header (12 bytes)
            f.write(b"BAVD")                                    # magic
            f.write(struct.pack("<HH", len(codebook), num_frames))  # num_bitmaps, num_frames
            f.write(struct.pack("<BB", effective_fps, 0))            # fps, flags
            f.write(b"\x00\x00")                                # reserved

            # Setup block: mode 136 (320x240 double-buffered), pixel coords, cursor off, CLG
            setup_vdu = bytearray()
            setup_vdu.append(22)            # VDU 22, mode — set mode
            setup_vdu.append(136)           # mode 136 = mode 8 + double buffer
            setup_vdu.extend([23, 0, 0xC0, 0])  # pixel coordinates
            setup_vdu.extend([23, 1, 0])    # cursor off
            setup_vdu.append(16)            # CLG
            f.write(struct.pack("<H", len(setup_vdu)))
            f.write(setup_vdu)

            # Bitmap blocks: raw VDU commands (executed immediately by VDP
            # to create bitmap objects — NOT wrapped in buffer writes)
            for bitmap_id, vdu_payload in bitmap_uploads:
                f.write(struct.pack("<H", len(vdu_payload)))
                f.write(vdu_payload)

            # Frame blocks: rebuild and wrap each frame
            for fi in range(num_frames):
                if fi < 2:
                    is_kf = True
                    prev = None
                else:
                    is_kf = False
                    prev = encoded_frames[fi - 2]
                vdu = build_frame_buffer(fi, encoded_frames[fi], prev,
                                         tiles_w, tiles_h, num_frames, is_kf)
                wrapped = wrap_vdp_buffer(FRAME_BASE_ID + fi, vdu)
                f.write(struct.pack("<H", len(wrapped)))
                f.write(wrapped)

        dat_size = os.path.getsize(args.output)
        print(f"Written: {args.output} ({dat_size:,}B / {dat_size/1024:.0f}KB)",
              file=sys.stderr)

    # Generate compressed two-stream .dat file (BA2S format)
    if args.output_2s:
        print(f"\nGenerating two-stream compressed file...", file=sys.stderr)
        gop_size = args.gop_size
        tiles_per_frame = tiles_w * tiles_h
        mask_bytes_per_frame = (tiles_per_frame + 7) // 8
        num_gops = (num_frames + gop_size - 1) // gop_size

        # Build per-frame masks and ID streams
        all_masks = []
        all_id_streams = []
        all_update_ids = []

        for f in range(num_frames):
            mask = bytearray(mask_bytes_per_frame)
            ids = []
            if f < 2:
                for pos in range(tiles_per_frame):
                    mask[pos // 8] |= (1 << (pos % 8))
                    ids.append(encoded_frames[f][pos])
            else:
                prev = encoded_frames[f - 2]
                curr = encoded_frames[f]
                for pos in range(tiles_per_frame):
                    if curr[pos] != prev[pos]:
                        mask[pos // 8] |= (1 << (pos % 8))
                        ids.append(curr[pos])
            all_masks.append(bytes(mask))
            all_id_streams.append(ids)
            all_update_ids.extend(ids)

        # Charprint: offset IDs by 32 so they map to printable char codes
        if args.charprint:
            all_update_ids = [tid + 32 for tid in all_update_ids]
            for i in range(len(all_id_streams)):
                all_id_streams[i] = [tid + 32 for tid in all_id_streams[i]]

        # Build Huffman codes from all update IDs
        id_freq = Counter(all_update_ids)
        huffman_codes = build_huffman_codes(id_freq)
        huffman_table = serialize_huffman_table(huffman_codes)

        # Setup VDU block
        setup_vdu = bytearray()
        setup_vdu.append(22)
        setup_vdu.append(136)       # mode 136 = 320x240 double-buffered
        setup_vdu.extend([23, 0, 0xC0, 0])  # pixel coords
        setup_vdu.extend([23, 1, 0])         # cursor off
        setup_vdu.append(16)                 # CLG
        if args.charprint:
            setup_vdu.append(5)              # VDU 5: text at graphics cursor
            # GCOL 0, 63 — set fg to white (for tile "on" pixels)
            setup_vdu.extend([18, 0, 63])
            # Set bg to black (GCOL 0, 128+0 for bg)
            setup_vdu.extend([18, 0, 128])

        with open(args.output_2s, "wb") as f:
            # Header (16 bytes)
            f.write(b"BA2S")
            f.write(struct.pack("<B", 1))                        # version
            f.write(struct.pack("<BB", tiles_w, tiles_h))        # grid
            nt_byte = len(codebook) & 0xFF                       # 256 → 0
            f.write(struct.pack("<B", nt_byte))                  # num_tiles
            f.write(struct.pack("<H", num_frames))               # num_frames
            f.write(struct.pack("<B", effective_fps))                 # fps
            f.write(struct.pack("<H", gop_size))                 # gop_size
            f.write(struct.pack("<B", num_gops))                 # num_gops
            flags = 0x01 if args.charprint else 0x00
            f.write(struct.pack("<BB", flags, 0))                # flags, pad

            # Setup VDU block
            f.write(struct.pack("<H", len(setup_vdu)))
            f.write(setup_vdu)

            # Tile bitmap blocks (same VDU commands as BAVD format)
            for bitmap_id, vdu_payload in bitmap_uploads:
                f.write(struct.pack("<H", len(vdu_payload)))
                f.write(vdu_payload)

            # Huffman table
            f.write(struct.pack("<H", len(huffman_table)))
            f.write(huffman_table)

            # GOP blocks
            total_mask_raw = 0
            total_mask_compressed = 0
            total_id_raw = 0
            total_id_compressed = 0

            for gop_idx in range(num_gops):
                gop_start = gop_idx * gop_size
                gop_end = min(gop_start + gop_size, num_frames)
                gop_frames = gop_end - gop_start

                # Compress masks with LZSS
                gop_mask_data = b''.join(all_masks[gop_start:gop_end])
                mask_compressed = lzss_compress(gop_mask_data)
                total_mask_raw += len(gop_mask_data)
                total_mask_compressed += len(mask_compressed)

                # Huffman encode IDs
                gop_ids = []
                for fi in range(gop_start, gop_end):
                    gop_ids.extend(all_id_streams[fi])
                id_data = bytes(gop_ids)
                id_compressed = huffman_encode_stream(id_data, huffman_codes)
                total_id_raw += len(id_data)
                total_id_compressed += len(id_compressed)

                # Write GOP block
                f.write(struct.pack("<H", gop_frames))
                f.write(struct.pack("<I", len(mask_compressed)))
                f.write(mask_compressed)
                f.write(struct.pack("<I", len(id_compressed)))
                f.write(struct.pack("<I", len(gop_ids)))  # num IDs to decode
                f.write(id_compressed)

                # Verify this GOP immediately
                mask_decoded = lzss_decompress(mask_compressed, len(gop_mask_data))
                assert mask_decoded == gop_mask_data, \
                    f"GOP {gop_idx}: LZSS mask round-trip failed"
                id_decoded = huffman_decode_stream(id_compressed, huffman_codes,
                                                   len(gop_ids))
                assert id_decoded == id_data, \
                    f"GOP {gop_idx}: Huffman ID round-trip failed"

                print(f"  GOP {gop_idx}: {gop_frames}fr | "
                      f"masks {len(gop_mask_data):,}→{len(mask_compressed):,}B "
                      f"({(1-len(mask_compressed)/len(gop_mask_data))*100:.0f}%) | "
                      f"IDs {len(id_data):,}→{len(id_compressed):,}B "
                      f"({(1-len(id_compressed)/len(id_data))*100:.0f}%) ✓",
                      file=sys.stderr)

        dat_size = os.path.getsize(args.output_2s)
        bitmap_size = sum(len(vdu) + 2 for _, vdu in bitmap_uploads)
        overhead = 16 + 2 + len(setup_vdu) + 2 + len(huffman_table) + bitmap_size
        print(f"\nBA2S file: {args.output_2s}", file=sys.stderr)
        print(f"  Total:    {dat_size:,}B ({dat_size/1024:.0f}KB)", file=sys.stderr)
        print(f"  Overhead: {overhead:,}B (header+setup+bitmaps+hufftable)",
              file=sys.stderr)
        print(f"  Masks:    {total_mask_raw:,}→{total_mask_compressed:,}B "
              f"({(1-total_mask_compressed/total_mask_raw)*100:.0f}% LZSS)",
              file=sys.stderr)
        print(f"  IDs:      {total_id_raw:,}→{total_id_compressed:,}B "
              f"({(1-total_id_compressed/total_id_raw)*100:.0f}% Huffman)",
              file=sys.stderr)

        # Compare with uncompressed BAVD
        bavd_equiv = compact_stats['vdu_total']
        print(f"  vs VDU buffers: {bavd_equiv:,}B → {dat_size:,}B "
              f"({(1-dat_size/bavd_equiv)*100:.0f}% smaller)", file=sys.stderr)

        # Full verification: decode BA2S → reconstruct frames → compare byte-by-byte
        print(f"  Verifying: decode → reconstruct → compare vs original...",
              file=sys.stderr)
        with open(args.output_2s, "rb") as f:
            hdr = f.read(16)
            assert hdr[:4] == b"BA2S", "Bad magic"
            v_tw, v_th = hdr[5], hdr[6]
            v_nt = hdr[7] or 256
            v_nf = struct.unpack("<H", hdr[8:10])[0]
            v_gs = struct.unpack("<H", hdr[11:13])[0]
            v_ng = hdr[13]
            assert v_tw == tiles_w and v_th == tiles_h
            assert v_nt == len(codebook) and v_nf == num_frames
            assert v_gs == gop_size and v_ng == num_gops

            # Skip setup VDU
            sl = struct.unpack("<H", f.read(2))[0]
            f.read(sl)
            # Skip bitmaps
            for _ in range(v_nt):
                bl = struct.unpack("<H", f.read(2))[0]
                f.read(bl)
            # Read Huffman table
            tl = struct.unpack("<H", f.read(2))[0]
            tdata = f.read(tl)
            verify_codes = deserialize_huffman_table(tdata)
            for sym in huffman_codes:
                assert verify_codes[sym] == huffman_codes[sym], \
                    f"Huffman table mismatch for symbol {sym}"

            # Decode all GOPs → reconstruct frames → compare
            reconstructed = []
            mismatches = 0
            first_mismatch = None

            for gi in range(v_ng):
                gf = struct.unpack("<H", f.read(2))[0]
                ml = struct.unpack("<I", f.read(4))[0]
                mdata = f.read(ml)
                il = struct.unpack("<I", f.read(4))[0]
                ic = struct.unpack("<I", f.read(4))[0]
                idata = f.read(il)

                # LZSS decompress masks
                dec_masks = lzss_decompress(mdata, gf * mask_bytes_per_frame)
                # Huffman decode IDs
                dec_ids = huffman_decode_stream(idata, verify_codes, ic)

                # Reconstruct frames using mask + ID logic
                id_cursor = 0
                for fi in range(gf):
                    frame_idx = len(reconstructed)
                    mask_off = fi * mask_bytes_per_frame

                    if frame_idx < 2:
                        frame = [0] * tiles_per_frame
                    else:
                        frame = list(reconstructed[frame_idx - 2])

                    for pos in range(tiles_per_frame):
                        byte_idx = mask_off + (pos >> 3)
                        if dec_masks[byte_idx] & (1 << (pos & 7)):
                            frame[pos] = dec_ids[id_cursor]
                            id_cursor += 1

                    reconstructed.append(frame)

                    # Compare with original
                    original = list(encoded_frames[frame_idx])
                    if frame != original:
                        mismatches += 1
                        if first_mismatch is None:
                            diffs = [(p, frame[p], original[p])
                                     for p in range(tiles_per_frame)
                                     if frame[p] != original[p]]
                            first_mismatch = (frame_idx, len(diffs), diffs[:10])

                assert id_cursor == ic, \
                    f"GOP {gi}: used {id_cursor} IDs but expected {ic}"

            assert len(reconstructed) == num_frames

        if mismatches == 0:
            print(f"  ✓ PERFECT MATCH: all {num_frames} reconstructed frames "
                  f"identical to original", file=sys.stderr)
        else:
            fi, nd, samples = first_mismatch
            print(f"  ✗ MISMATCH: {mismatches}/{num_frames} frames differ!",
                  file=sys.stderr)
            print(f"    First bad frame: {fi} ({nd} tiles differ)", file=sys.stderr)
            for pos, got, exp in samples:
                tx, ty = pos % tiles_w, pos // tiles_w
                print(f"      tile ({tx},{ty}) pos={pos}: "
                      f"got={got} expected={exp}", file=sys.stderr)

    # Generate VSYNC-chunked replay file for agon-vdp --replay
    if args.replay_file:
        print(f"\nGenerating replay file...", file=sys.stderr)

        def write_chunk(f, data):
            """Write one VSYNC chunk: [u16-LE len][data]"""
            f.write(struct.pack("<H", len(data)))
            f.write(data)

        with open(args.replay_file, "wb") as f:
            # === Init chunk: General Poll unlocks VDP ===
            # VDP's wait_eZ80() loop discards non-VDU-23 bytes until it
            # receives VDU 23,0,&80,n (General Poll) which sets initialised=true.
            # Must be its own chunk so the replay tool drains the GP response
            # before sending more data (avoids output-queue deadlock).
            write_chunk(f, bytes([23, 0, 0x80, 1]))

            # === Setup chunk ===
            setup = bytearray()
            setup.append(22)                   # VDU 22 = mode switch
            setup.append(136)                  # Mode 136
            setup.extend([23, 0, 0xC0, 0])    # pixel coordinates
            setup.extend([23, 1, 0])           # cursor off
            setup.append(16)                   # CLG
            if args.charprint:
                setup.append(5)                # VDU 5: text at graphics cursor
                setup.extend([18, 0, 63])      # GCOL 0, 63 (white fg)
                setup.extend([18, 0, 128])     # GCOL 0, 128 (black bg)

            # Tile definitions (bitmaps or character defs)
            for _, vdu_payload in bitmap_uploads:
                setup.extend(vdu_payload)

            if args.charprint:
                # Charprint: stream VDU directly per frame (no VDP buffers)
                # Split setup into small chunks (~256B each) with empty VSYNC
                # frames between them to avoid CTS backpressure stalls
                SETUP_CHUNK = 256
                num_setup_chunks = (len(setup) + SETUP_CHUNK - 1) // SETUP_CHUNK
                print(f"  Setup: {len(setup):,}B ({len(bitmap_uploads)} chars) "
                      f"in {num_setup_chunks} chunks", file=sys.stderr)
                for i in range(0, len(setup), SETUP_CHUNK):
                    write_chunk(f, setup[i:i + SETUP_CHUNK])

                # Per-frame VSYNC chunks with charprint VDU
                vblanks_per_frame = 60 // effective_fps if effective_fps <= 60 else 1
                print(f"  Streaming {num_frames} frames ({effective_fps}fps, "
                      f"{vblanks_per_frame} vsyncs/frame)", file=sys.stderr)
                total_vdu = 0
                for fi in range(num_frames):
                    prev = encoded_frames[fi - 2] if fi >= 2 else None
                    draw_vdu = build_frame_charprint(
                        encoded_frames[fi], prev, tiles_w, tiles_h, fi < 2)
                    write_chunk(f, draw_vdu)
                    total_vdu += len(draw_vdu)
                    # Hold extra vsyncs for fps pacing
                    for _ in range(vblanks_per_frame - 1):
                        write_chunk(f, b"\x00")
                    if fi % 500 == 0:
                        print(f"  Frame {fi}/{num_frames} ({len(draw_vdu)}B)", file=sys.stderr)
                print(f"  Total VDU: {total_vdu:,}B ({total_vdu/1024:.0f}KB), "
                      f"avg {total_vdu/num_frames:.0f}B/frame", file=sys.stderr)
            else:
                # Legacy: upload VDP buffers then call them
                print(f"  Building upload blob...", file=sys.stderr)
                for fi in range(num_frames):
                    if fi < 2:
                        is_kf = True
                        prev = None
                    else:
                        is_kf = False
                        prev = encoded_frames[fi - 2]
                    vdu = build_frame_buffer(fi, encoded_frames[fi], prev,
                                             tiles_w, tiles_h, num_frames, is_kf)
                    draw_vdu = vdu[:-6]
                    draw_only = draw_vdu[:-3]
                    wrapped = wrap_vdp_buffer(FRAME_BASE_ID + fi, draw_only)
                    setup.extend(wrapped)
                    if fi % 100 == 0:
                        print(f"  Frames: {fi}/{num_frames}", file=sys.stderr)
                print(f"  Frames: {num_frames}/{num_frames}", file=sys.stderr)

                MAX_CHUNK = 0xFFFF
                num_chunks = (len(setup) + MAX_CHUNK - 1) // MAX_CHUNK
                print(f"  Upload blob: {len(setup):,}B ({len(setup)/1024:.0f}KB) "
                      f"in {num_chunks} chunks", file=sys.stderr)
                for i in range(0, len(setup), MAX_CHUNK):
                    write_chunk(f, setup[i:i + MAX_CHUNK])

                # Playback: call buffer + swap per frame
                vblanks_per_frame = 60 // effective_fps if effective_fps <= 60 else 1
                print(f"  Playback: {num_frames} frames ({effective_fps}fps)", file=sys.stderr)
                for fi in range(num_frames):
                    call_cmd = bytearray()
                    buf_id = FRAME_BASE_ID + fi
                    call_cmd.extend([23, 0, 0xA0,
                                     buf_id & 0xFF, (buf_id >> 8) & 0xFF, 1])
                    call_cmd.extend([23, 0, 0xC3])
                    write_chunk(f, call_cmd)
                    for _ in range(vblanks_per_frame - 1):
                        write_chunk(f, b"\x00")

            # EOF marker
            f.write(struct.pack("<H", 0))

        replay_size = os.path.getsize(args.replay_file)
        print(f"Written: {args.replay_file} ({replay_size:,}B / {replay_size/1024:.0f}KB)",
              file=sys.stderr)

    # Live VDP preview via fake_ez80
    if args.preview:
        from fake_ez80 import FakeEz80Server
        from vdp_stream import VDPStream

        server = FakeEz80Server(port=args.port)
        server.start()

        try:
            # General Poll + mode setup
            s = VDPStream()
            s.general_poll()
            server.send_vdu(s.get_bytes())
            for _ in range(5):
                server.wait_vsync()

            # Mode switch (async — resets VDP state)
            s.reset()
            s.mode(136)
            server.send_vdu(s.get_bytes())
            for _ in range(10):
                server.wait_vsync()

            # Pixel coords + cursor off + CLG (AFTER mode settled)
            s.reset()
            s.set_logical_coords(False)
            s.cursor(False)
            s.clg()
            if args.charprint:
                s.raw(bytes([5]))          # VDU 5: text at graphics cursor
                s.raw(bytes([18, 0, 63]))  # GCOL 0, 63 (white foreground)
                s.raw(bytes([18, 0, 128])) # GCOL 0, 128 (black background)
            server.send_vdu(s.get_bytes())
            for _ in range(2):
                server.wait_vsync()

            # Create bitmaps or character definitions
            if args.charprint:
                print("Defining characters...", file=sys.stderr)
            else:
                print("Creating bitmaps...", file=sys.stderr)
            for _, vdu_payload in bitmap_uploads:
                server.send_vdu(vdu_payload)
            for _ in range(3):
                server.wait_vsync()
            print(f"  {len(bitmap_uploads)} {'chars' if args.charprint else 'bitmaps'} created",
                  file=sys.stderr)

            # Stream frames: draw directly each vsync (no VDP buffers)
            print("Playing...", file=sys.stderr)
            for f in range(num_frames):
                if not server.connected:
                    break

                if args.charprint:
                    prev = encoded_frames[f - 2] if f >= 2 else None
                    is_kf = f < 2
                    draw_vdu = build_frame_charprint(
                        encoded_frames[f], prev, tiles_w, tiles_h, is_kf)
                else:
                    if f < 2:
                        prev = None
                        is_kf = True
                    else:
                        prev = encoded_frames[f - 2]
                        is_kf = False
                    vdu = build_frame_buffer(f, encoded_frames[f], prev,
                                             tiles_w, tiles_h, num_frames, is_kf)
                    draw_vdu = vdu[:-6]  # remove VSYNC chain, keep swap

                server.send_vdu(draw_vdu)
                server.wait_vsync()

                if f % 100 == 0:
                    print(f"  Frame {f}/{num_frames}", file=sys.stderr)

            print("Done!", file=sys.stderr)
        except KeyboardInterrupt:
            print("\nInterrupted", file=sys.stderr)
        finally:
            server.shutdown()


if __name__ == "__main__":
    main()
