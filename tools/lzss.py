"""LZSS compression/decompression for VDU frame data.

Encoding:
  Tokens are grouped in sets of 8, preceded by a flag byte (LSB first).
  Flag bit = 0: literal byte follows (1 byte)
  Flag bit = 1: match follows (2 bytes)
    byte0 = offset_lo (low 8 bits of offset-1)
    byte1 = (offset_hi << 4) | (length - 3)
    offset: 12 bits → 1..4096 bytes back
    length: 4 bits  → 3..18 bytes

Window size: 4096 bytes.  Min match: 3.  Max match: 18.
"""

WINDOW_SIZE = 4096
MIN_MATCH = 3
MAX_MATCH = 18


def _find_match(data: bytes, pos: int, window_start: int) -> tuple[int, int]:
    """Find longest match in the sliding window. Returns (offset, length)."""
    best_offset = 0
    best_length = 0
    end = min(pos + MAX_MATCH, len(data))

    for i in range(max(0, window_start), pos):
        length = 0
        while (pos + length < end and
               data[i + length] == data[pos + length]):
            length += 1
        if length > best_length:
            best_length = length
            best_offset = pos - i
            if length == MAX_MATCH:
                break

    return best_offset, best_length


def lzss_compress(data: bytes) -> bytes:
    """Compress data using LZSS."""
    out = bytearray()
    pos = 0
    n = len(data)

    while pos < n:
        flag_byte = 0
        flag_pos = len(out)
        out.append(0)  # placeholder for flag byte
        tokens_in_group = 0

        for bit in range(8):
            if pos >= n:
                break

            window_start = max(0, pos - WINDOW_SIZE)
            offset, length = _find_match(data, pos, window_start)

            if length >= MIN_MATCH:
                # Match
                flag_byte |= (1 << bit)
                off_minus1 = offset - 1
                out.append(off_minus1 & 0xFF)
                out.append(((off_minus1 >> 4) & 0xF0) | (length - MIN_MATCH))
                pos += length
            else:
                # Literal
                out.append(data[pos])
                pos += 1

            tokens_in_group += 1

        out[flag_pos] = flag_byte

    return bytes(out)


def lzss_decompress(data: bytes, output_size: int) -> bytes:
    """Decompress LZSS data. output_size is the expected decompressed size."""
    out = bytearray()
    i = 0

    while i < len(data) and len(out) < output_size:
        flag_byte = data[i]
        i += 1

        for bit in range(8):
            if i >= len(data) or len(out) >= output_size:
                break

            if flag_byte & (1 << bit):
                # Match
                b0 = data[i]
                b1 = data[i + 1]
                i += 2
                offset = ((b1 & 0xF0) << 4) | b0
                offset += 1
                length = (b1 & 0x0F) + MIN_MATCH

                start = len(out) - offset
                for j in range(length):
                    out.append(out[start + j])
            else:
                # Literal
                out.append(data[i])
                i += 1

    return bytes(out)


def compress_frame_blob(raw_blob: bytes) -> tuple[bytes, bytes, dict]:
    """Compress an entire frame blob.

    Args:
        raw_blob: Original frame blob [num_frames:u16-LE][len:u16-LE][data...]...

    Returns:
        (compressed_data, header, stats)
        Header format: [num_frames:u16-LE][decompressed_size:u24-LE]
        The player decompresses the whole blob at startup, then plays as before.
    """
    import struct

    num_frames = struct.unpack_from("<H", raw_blob, 0)[0]
    compressed = lzss_compress(raw_blob)

    header = struct.pack("<HI", num_frames, len(raw_blob))[:6]  # u16 + u24 (take 5 bytes)
    # Actually let's use a cleaner header
    header = struct.pack("<HHH", num_frames, len(raw_blob) & 0xFFFF, len(raw_blob) >> 16)

    stats = {
        "num_frames": num_frames,
        "raw_bytes": len(raw_blob),
        "compressed_bytes": len(compressed),
        "ratio": len(compressed) / len(raw_blob) if raw_blob else 0,
    }
    return compressed, header, stats


# --- Self-test ---
if __name__ == "__main__":
    import os, struct

    # Basic roundtrip tests
    test_cases = [
        b"",
        b"A",
        b"AAAA",
        b"ABCDEF",
        b"AAABBBCCC",
        b"Hello Hello Hello World World World",
        bytes(range(256)) * 4,
        bytes([0x25, 4, 0, 0, 100, 0] * 50),  # simulated VDU MOVE commands
        bytes([0x25, 85, 50, 0, 80, 0] * 50),  # simulated VDU PLOT85 commands
    ]

    for i, original in enumerate(test_cases):
        compressed = lzss_compress(original)
        decompressed = lzss_decompress(compressed, len(original))
        assert decompressed == original, (
            f"Test {i} failed: {len(original)}B → {len(compressed)}B → "
            f"{len(decompressed)}B"
        )
        ratio = len(compressed) / len(original) if original else 0
        print(f"Test {i}: {len(original):4d}B → {len(compressed):4d}B "
              f"({ratio:.0%})")

    # Test on real cube frames if available
    blob_path = os.path.join(os.path.dirname(__file__), "cube_frames.bin")
    if os.path.exists(blob_path):
        with open(blob_path, "rb") as f:
            raw = f.read()
        compressed = lzss_compress(raw)
        decompressed = lzss_decompress(compressed, len(raw))
        assert decompressed == raw, "Cube frame roundtrip failed!"
        ratio = len(compressed) / len(raw)
        print(f"\nCube frames: {len(raw):,}B → {len(compressed):,}B ({ratio:.0%})")

    print("\nAll tests passed!")
