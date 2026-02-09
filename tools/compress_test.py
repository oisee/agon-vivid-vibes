#!/usr/bin/env python3
"""Test different compression strategies on cube frame data.

Parses the raw VDU frame blob, extracts structured triangle data,
then tries various encoding strategies to find the best compression.
"""

import os
import struct
import sys

from lzss import lzss_compress, lzss_decompress

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))


def parse_vdu_frames(blob: bytes) -> list[list[dict]]:
    """Parse raw VDU frame blob into structured triangle data.

    Each frame = CLG + N * (GCOL + MOVE + MOVE + PLOT85)
    Returns list of frames, each frame is list of triangles:
      {color, x1, y1, x2, y2, x3, y3}
    """
    num_frames = struct.unpack_from("<H", blob, 0)[0]
    frames = []
    pos = 2

    for _ in range(num_frames):
        flen = struct.unpack_from("<H", blob, pos)[0]
        pos += 2
        fdata = blob[pos:pos + flen]
        pos += flen

        tris = []
        i = 0
        assert fdata[i] == 16, f"Expected CLG (16), got {fdata[i]}"  # CLG
        i += 1

        while i < len(fdata):
            assert fdata[i] == 18 and fdata[i+1] == 0  # GCOL 0
            color = fdata[i+2]
            i += 3

            # MOVE: 25, 4, x_lo, x_hi, y_lo, y_hi
            assert fdata[i] == 25 and fdata[i+1] == 4
            x1 = fdata[i+2] | (fdata[i+3] << 8)
            y1 = fdata[i+4] | (fdata[i+5] << 8)
            i += 6

            # MOVE: 25, 4, x_lo, x_hi, y_lo, y_hi
            assert fdata[i] == 25 and fdata[i+1] == 4
            x2 = fdata[i+2] | (fdata[i+3] << 8)
            y2 = fdata[i+4] | (fdata[i+5] << 8)
            i += 6

            # PLOT 85: 25, 85, x_lo, x_hi, y_lo, y_hi
            assert fdata[i] == 25 and fdata[i+1] == 85
            x3 = fdata[i+2] | (fdata[i+3] << 8)
            y3 = fdata[i+4] | (fdata[i+5] << 8)
            i += 6

            tris.append({
                "color": color,
                "x1": x1, "y1": y1,
                "x2": x2, "y2": y2,
                "x3": x3, "y3": y3,
            })

        frames.append(tris)

    return frames


def strategy_raw(blob: bytes) -> tuple[int, str]:
    """Baseline: raw VDU bytes, no compression."""
    return len(blob), "raw VDU bytes"


def strategy_lzss_raw(blob: bytes) -> tuple[int, str]:
    """LZSS on raw VDU bytes."""
    c = lzss_compress(blob)
    return len(c), "LZSS(raw VDU)"


def strategy_display_list(frames: list) -> tuple[int, bytes, str]:
    """Strip VDU commands, store structured triangle data.

    Format: [num_frames:u16] per frame [num_tris:u8]
    then per tri: [color:u8][x1:u16-LE][y1:u16-LE][x2:u16-LE][y2:u16-LE][x3:u16-LE][y3:u16-LE]
    = 13 bytes per triangle
    """
    out = bytearray(struct.pack("<H", len(frames)))
    for tris in frames:
        out.append(len(tris))
        for t in tris:
            out.append(t["color"])
            out.extend(struct.pack("<HHHHHH",
                                   t["x1"], t["y1"],
                                   t["x2"], t["y2"],
                                   t["x3"], t["y3"]))
    return len(out), bytes(out), "display list (no VDU overhead)"


def strategy_display_list_lzss(frames: list) -> tuple[int, str]:
    """Display list + LZSS."""
    _, dl, _ = strategy_display_list(frames)
    c = lzss_compress(dl)
    return len(c), "LZSS(display list)"


def strategy_display_list_u8coords(frames: list) -> tuple[int, bytes, str]:
    """Display list with 8-bit coordinates (x clamped to 0-255, y as-is).

    For 320x240 mode: x needs 9 bits (0-319), y needs 8 bits (0-239).
    Store x as u8 + 1 bit packed into color byte? Too complex.
    Instead: x_lo + y as u8 each, plus packed x_hi bits.
    Per tri: [color:u8][x1_lo:u8][y1:u8][x2_lo:u8][y2:u8][x3_lo:u8][y3:u8] = 7 bytes
    Plus packed x_hi bits: 3 bits per tri (x1_h, x2_h, x3_h) → 1 byte per tri
    Total: 8 bytes per tri (vs 13 in display list, 21 in raw VDU)
    """
    out = bytearray(struct.pack("<H", len(frames)))
    for tris in frames:
        out.append(len(tris))
        for t in tris:
            out.append(t["color"])
            # Pack 3 x_hi bits into one byte
            x_hi = ((t["x1"] >> 8) & 1) | (((t["x2"] >> 8) & 1) << 1) | (((t["x3"] >> 8) & 1) << 2)
            out.append(x_hi)
            out.extend([
                t["x1"] & 0xFF, t["y1"] & 0xFF,
                t["x2"] & 0xFF, t["y2"] & 0xFF,
                t["x3"] & 0xFF, t["y3"] & 0xFF,
            ])
    return len(out), bytes(out), "display list (packed 8-bit coords)"


def strategy_transposed(frames: list) -> tuple[int, bytes, str]:
    """Transpose triangle data by field, then concatenate columns.

    Columns: [all tri_counts] [all colors] [all x1_lo] [all x1_hi]
             [all y1_lo] [all x2_lo] [all x2_hi] [all y2_lo]
             [all x3_lo] [all x3_hi] [all y3_lo]
    Each column has similar values → compresses much better.
    """
    tri_counts = bytearray()
    colors = bytearray()
    x1_lo, x1_hi, y1 = bytearray(), bytearray(), bytearray()
    x2_lo, x2_hi, y2 = bytearray(), bytearray(), bytearray()
    x3_lo, x3_hi, y3 = bytearray(), bytearray(), bytearray()

    for tris in frames:
        tri_counts.append(len(tris))
        for t in tris:
            colors.append(t["color"])
            x1_lo.append(t["x1"] & 0xFF); x1_hi.append(t["x1"] >> 8)
            y1.append(t["y1"] & 0xFF)
            x2_lo.append(t["x2"] & 0xFF); x2_hi.append(t["x2"] >> 8)
            y2.append(t["y2"] & 0xFF)
            x3_lo.append(t["x3"] & 0xFF); x3_hi.append(t["x3"] >> 8)
            y3.append(t["y3"] & 0xFF)

    # Header: num_frames, then column lengths, then column data
    columns = [tri_counts, colors, x1_lo, x1_hi, y1, x2_lo, x2_hi, y2, x3_lo, x3_hi, y3]
    header = struct.pack("<HB", len(frames), len(columns))
    for col in columns:
        header += struct.pack("<H", len(col))

    blob = header + b"".join(columns)
    return len(blob), bytes(blob), "transposed columns"


def strategy_transposed_lzss(frames: list) -> tuple[int, str]:
    """Transpose by field, then LZSS each column separately."""
    tri_counts = bytearray()
    colors = bytearray()
    x1_lo, x1_hi, y1 = bytearray(), bytearray(), bytearray()
    x2_lo, x2_hi, y2 = bytearray(), bytearray(), bytearray()
    x3_lo, x3_hi, y3 = bytearray(), bytearray(), bytearray()

    for tris in frames:
        tri_counts.append(len(tris))
        for t in tris:
            colors.append(t["color"])
            x1_lo.append(t["x1"] & 0xFF); x1_hi.append(t["x1"] >> 8)
            y1.append(t["y1"] & 0xFF)
            x2_lo.append(t["x2"] & 0xFF); x2_hi.append(t["x2"] >> 8)
            y2.append(t["y2"] & 0xFF)
            x3_lo.append(t["x3"] & 0xFF); x3_hi.append(t["x3"] >> 8)
            y3.append(t["y3"] & 0xFF)

    columns = [tri_counts, colors, x1_lo, x1_hi, y1, x2_lo, x2_hi, y2, x3_lo, x3_hi, y3]
    col_names = ["tri_counts", "colors", "x1_lo", "x1_hi", "y1",
                 "x2_lo", "x2_hi", "y2", "x3_lo", "x3_hi", "y3"]

    total = 2 + 1  # num_frames + num_columns header
    details = []
    for name, col in zip(col_names, columns):
        compressed = lzss_compress(bytes(col))
        details.append(f"  {name:12s}: {len(col):5d}B → {len(compressed):5d}B ({len(compressed)/len(col):.0%})")
        total += 2 + len(compressed)  # column length + data

    return total, "transposed + per-column LZSS\n" + "\n".join(details)


def strategy_transposed_delta_lzss(frames: list) -> tuple[int, str]:
    """Transpose, delta-encode each column, then LZSS."""
    tri_counts = bytearray()
    colors = bytearray()
    x1_lo, x1_hi, y1 = bytearray(), bytearray(), bytearray()
    x2_lo, x2_hi, y2 = bytearray(), bytearray(), bytearray()
    x3_lo, x3_hi, y3 = bytearray(), bytearray(), bytearray()

    for tris in frames:
        tri_counts.append(len(tris))
        for t in tris:
            colors.append(t["color"])
            x1_lo.append(t["x1"] & 0xFF); x1_hi.append(t["x1"] >> 8)
            y1.append(t["y1"] & 0xFF)
            x2_lo.append(t["x2"] & 0xFF); x2_hi.append(t["x2"] >> 8)
            y2.append(t["y2"] & 0xFF)
            x3_lo.append(t["x3"] & 0xFF); x3_hi.append(t["x3"] >> 8)
            y3.append(t["y3"] & 0xFF)

    def delta_encode(data: bytearray) -> bytes:
        if not data:
            return bytes(data)
        out = bytearray([data[0]])
        for i in range(1, len(data)):
            out.append((data[i] - data[i-1]) & 0xFF)
        return bytes(out)

    columns = [tri_counts, colors, x1_lo, x1_hi, y1, x2_lo, x2_hi, y2, x3_lo, x3_hi, y3]
    col_names = ["tri_counts", "colors", "x1_lo", "x1_hi", "y1",
                 "x2_lo", "x2_hi", "y2", "x3_lo", "x3_hi", "y3"]

    total = 2 + 1
    details = []
    for name, col in zip(col_names, columns):
        delta = delta_encode(col)
        compressed = lzss_compress(delta)
        details.append(f"  {name:12s}: {len(col):5d}B → delta → {len(compressed):5d}B ({len(compressed)/len(col):.0%})")
        total += 2 + len(compressed)

    return total, "transposed + delta + per-column LZSS\n" + "\n".join(details)


def strategy_transposed_whole_lzss(frames: list) -> tuple[int, str]:
    """Transpose all columns concatenated, then LZSS the whole thing."""
    _, blob, _ = strategy_transposed(frames)
    c = lzss_compress(blob)
    return len(c), "LZSS(transposed blob)"


def main():
    blob_path = os.path.join(SCRIPT_DIR, "cube_frames.bin")
    with open(blob_path, "rb") as f:
        raw = f.read()

    frames = parse_vdu_frames(raw)
    total_tris = sum(len(f) for f in frames)
    print(f"Parsed {len(frames)} frames, {total_tris} total triangles")
    print(f"Triangles per frame: {min(len(f) for f in frames)}-{max(len(f) for f in frames)}")
    print()

    print(f"{'Strategy':50s}  {'Size':>8s}  {'Ratio':>6s}")
    print("-" * 70)

    raw_size = len(raw)
    results = []

    # 1. Raw baseline
    sz, desc = strategy_raw(raw)
    results.append((sz, desc))

    # 2. LZSS on raw
    sz, desc = strategy_lzss_raw(raw)
    results.append((sz, desc))

    # 3. Display list (no compression)
    sz, _, desc = strategy_display_list(frames)
    results.append((sz, desc))

    # 4. Display list + LZSS
    sz, desc = strategy_display_list_lzss(frames)
    results.append((sz, desc))

    # 5. Packed 8-bit coords
    sz, _, desc = strategy_display_list_u8coords(frames)
    results.append((sz, desc))

    # 6. Packed 8-bit coords + LZSS
    _, u8blob, _ = strategy_display_list_u8coords(frames)
    c = lzss_compress(u8blob)
    results.append((len(c), "LZSS(packed 8-bit coords)"))

    # 7. Transposed columns (no compression)
    sz, _, desc = strategy_transposed(frames)
    results.append((sz, desc))

    # 8. Transposed + whole LZSS
    sz, desc = strategy_transposed_whole_lzss(frames)
    results.append((sz, desc))

    # 9. Transposed + per-column LZSS
    sz, desc = strategy_transposed_lzss(frames)
    results.append((sz, desc))

    # 10. Transposed + delta + per-column LZSS
    sz, desc = strategy_transposed_delta_lzss(frames)
    results.append((sz, desc))

    for sz, desc in results:
        # First line of description
        first_line = desc.split("\n")[0]
        ratio = sz / raw_size
        print(f"{first_line:50s}  {sz:7,}B  {ratio:5.0%}")
        # Extra detail lines
        for line in desc.split("\n")[1:]:
            print(line)

    print()
    best_sz, best_desc = min(results, key=lambda x: x[0])
    print(f"Best: {best_desc.split(chr(10))[0]} — {best_sz:,}B ({best_sz/raw_size:.0%} of raw)")


if __name__ == "__main__":
    main()
