"""Simple RLE compression/decompression for VDU frame data.

Format:
  Control byte:
    0x00..0x7F  →  (control + 1) literal bytes follow  (1..128 literals)
    0x80..0xFF  →  repeat next byte (control - 126) times  (2..129 repeats)

This is a classic PackBits-style RLE, simple and fast to decode on eZ80.
"""


def rle_compress(data: bytes) -> bytes:
    """Compress data using PackBits-style RLE."""
    out = bytearray()
    i = 0
    n = len(data)

    while i < n:
        # Look for a run of identical bytes
        run_start = i
        while i + 1 < n and data[i] == data[i + 1] and i - run_start < 128:
            i += 1

        run_len = i - run_start + 1

        if run_len >= 3:
            # Encode as a run: control = run_len + 126
            out.append(run_len + 126)
            out.append(data[run_start])
            i = run_start + run_len
        else:
            # Collect literals — bytes that don't form runs of 3+
            lit_start = run_start
            i = run_start

            while i < n:
                # Check if a run of 3+ starts here
                if (i + 2 < n and data[i] == data[i + 1] == data[i + 2]):
                    break
                i += 1
                if i - lit_start >= 128:
                    break

            lit_len = i - lit_start
            out.append(lit_len - 1)  # control = lit_len - 1
            out.extend(data[lit_start:lit_start + lit_len])

    return bytes(out)


def rle_decompress(data: bytes) -> bytes:
    """Decompress PackBits-style RLE data."""
    out = bytearray()
    i = 0
    n = len(data)

    while i < n:
        control = data[i]
        i += 1

        if control < 0x80:
            # Literal run: (control + 1) bytes
            count = control + 1
            out.extend(data[i:i + count])
            i += count
        else:
            # Repeat run: (control - 126) copies of next byte
            count = control - 126
            out.append(data[i] * count) if count == 1 else out.extend(
                bytes([data[i]]) * count
            )
            i += 1

    return bytes(out)


def compress_frames(frames: list[bytes]) -> tuple[bytes, dict]:
    """Compress a list of VDU frame byte blobs.

    Returns (compressed_blob, stats_dict).

    Blob format: [num_frames:u16-LE] then per frame [compressed_len:u16-LE][rle_data...]
    """
    import struct

    parts = [struct.pack("<H", len(frames))]
    total_raw = 0
    total_compressed = 0

    for frame in frames:
        compressed = rle_compress(frame)
        parts.append(struct.pack("<H", len(compressed)))
        parts.append(compressed)
        total_raw += len(frame)
        total_compressed += len(compressed)

    blob = b"".join(parts)
    stats = {
        "num_frames": len(frames),
        "raw_bytes": total_raw,
        "compressed_bytes": total_compressed,
        "blob_bytes": len(blob),
        "ratio": total_compressed / total_raw if total_raw else 0,
    }
    return blob, stats


# --- Self-test ---
if __name__ == "__main__":
    # Test roundtrip
    test_cases = [
        b"",
        b"A",
        b"AAAA",
        b"ABCDEF",
        b"AAABBBCCC",
        b"AABCDDDDDDDDDDEF",
        bytes(range(256)),
        bytes([0x25] * 50 + [4, 0, 100, 0, 50, 0] + [0x25] * 50),
    ]

    for i, original in enumerate(test_cases):
        compressed = rle_compress(original)
        decompressed = rle_decompress(compressed)
        assert decompressed == original, (
            f"Test {i} failed: {len(original)}B → {len(compressed)}B → "
            f"{len(decompressed)}B (mismatch)"
        )
        ratio = len(compressed) / len(original) if original else 0
        print(f"Test {i}: {len(original):4d}B → {len(compressed):4d}B "
              f"({ratio:.0%})")

    print("All tests passed!")
