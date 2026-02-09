"""VDP byte stream builder — encodes high-level drawing ops into raw VDU bytes.

The Agon VDP processes a stream of VDU (Video Display Unit) command bytes.
This module provides a builder that accumulates VDU commands and emits
the raw byte stream that would normally come from an eZ80 over UART.

VDU reference: https://agonconsole8.github.io/agon-docs/VDP.html
"""

import struct


# -- Agon 64-colour palette helpers --
#
# Mode 8 has 64 colours. The palette uses the BBC Micro interleaved bit scheme:
#   bit 0 = R high, bit 1 = G high, bit 2 = B high   (same as BBC 8-colour)
#   bit 3 = R low,  bit 4 = G low,  bit 5 = B low
#
# Each channel is 2 bits (0-3), with the high bit giving coarse intensity
# and the low bit giving fine intensity.

def agon_rgb(r: int, g: int, b: int) -> int:
    """Convert 2-bit RGB components (0-3 each) to Agon 64-colour palette index."""
    r_hi, r_lo = (r >> 1) & 1, r & 1
    g_hi, g_lo = (g >> 1) & 1, g & 1
    b_hi, b_lo = (b >> 1) & 1, b & 1
    return (b_lo << 5) | (g_lo << 4) | (r_lo << 3) | (b_hi << 2) | (g_hi << 1) | r_hi


def agon_index_to_rgb(idx: int) -> tuple[int, int, int]:
    """Convert Agon 64-colour palette index to 2-bit RGB components (0-3 each)."""
    r_hi = (idx >> 0) & 1
    g_hi = (idx >> 1) & 1
    b_hi = (idx >> 2) & 1
    r_lo = (idx >> 3) & 1
    g_lo = (idx >> 4) & 1
    b_lo = (idx >> 5) & 1
    return (r_hi << 1) | r_lo, (g_hi << 1) | g_lo, (b_hi << 1) | b_lo


def agon_index_to_hex(idx: int) -> str:
    """Convert Agon 64-colour palette index to '#rrggbb' hex string."""
    r, g, b = agon_index_to_rgb(idx)
    return f"#{r * 85:02x}{g * 85:02x}{b * 85:02x}"


class VDPStream:
    """Accumulates VDU commands and produces a raw byte stream."""

    def __init__(self):
        self._buf = bytearray()

    # -- Raw byte helpers --

    def raw(self, *args: int) -> "VDPStream":
        """Append raw bytes."""
        self._buf.extend(args)
        return self

    def raw_bytes(self, data: bytes | bytearray) -> "VDPStream":
        """Append raw byte sequence."""
        self._buf.extend(data)
        return self

    # -- System commands --

    def general_poll(self, echo: int = 1) -> "VDPStream":
        """VDU 23, 0, &80, echo — General Poll handshake.

        The VDP firmware blocks all VDU processing until it receives this.
        Must be sent before any other commands.
        """
        self._buf.extend((23, 0, 0x80, echo))
        return self

    # -- Screen commands --

    def mode(self, n: int) -> "VDPStream":
        """VDU 22, n — set screen mode."""
        self._buf.extend((22, n))
        return self

    def cursor(self, on: bool = True) -> "VDPStream":
        """VDU 23, 1, n — show (1) or hide (0) text cursor."""
        self._buf.extend((23, 1, 1 if on else 0))
        return self

    def cls(self) -> "VDPStream":
        """VDU 12 — clear text area."""
        self._buf.append(12)
        return self

    def clg(self) -> "VDPStream":
        """VDU 16 — clear graphics area."""
        self._buf.append(16)
        return self

    def swap_buffers(self) -> "VDPStream":
        """VDU 23, 0, &C3 — swap double-buffer at next vsync.

        Only works in double-buffered modes (mode + 128, e.g. MODE 136).
        Atomically swaps the back buffer to screen — no tearing.
        """
        self._buf.extend((23, 0, 0xC3))
        return self

    def set_logical_coords(self, on: bool = True) -> "VDPStream":
        """VDU 23, 0, &C0, n — enable (1) or disable (0) logical coordinate scaling.

        When off: pixel coordinates, origin top-left, Y down.
        When on (default): 1280x1024 logical coords, origin bottom-left, Y up.
        """
        self._buf.extend((23, 0, 0xC0, 1 if on else 0))
        return self

    # -- Colour commands --

    def gcol(self, mode: int, col: int) -> "VDPStream":
        """VDU 18, mode, col — set graphics colour."""
        self._buf.extend((18, mode, col))
        return self

    def colour(self, c: int) -> "VDPStream":
        """VDU 17, c — set text colour."""
        self._buf.extend((17, c))
        return self

    def set_palette(self, logical: int, physical: int, r: int, g: int, b: int) -> "VDPStream":
        """VDU 19, logical, physical, r, g, b — define palette entry."""
        self._buf.extend((19, logical, physical, r, g, b))
        return self

    # -- Plot / drawing commands --

    def _plot_xy(self, code: int, x: int, y: int):
        """Encode PLOT command: VDU 25, code, x_lo, x_hi, y_lo, y_hi."""
        # x, y are signed 16-bit
        x = x & 0xFFFF
        y = y & 0xFFFF
        self._buf.append(25)
        self._buf.append(code)
        self._buf.extend(struct.pack("<HH", x, y))

    def plot(self, code: int, x: int, y: int) -> "VDPStream":
        """VDU 25, code, x; y; — general PLOT command."""
        self._plot_xy(code, x, y)
        return self

    def move(self, x: int, y: int) -> "VDPStream":
        """VDU 25, 4, x; y; — move graphics cursor."""
        self._plot_xy(4, x, y)
        return self

    def draw(self, x: int, y: int) -> "VDPStream":
        """VDU 25, 5, x; y; — draw line to absolute position."""
        self._plot_xy(5, x, y)
        return self

    def triangle(self, x: int, y: int) -> "VDPStream":
        """VDU 25, 85, x; y; — draw filled triangle (third vertex)."""
        self._plot_xy(85, x, y)
        return self

    def filled_rect(self, x1: int, y1: int, x2: int, y2: int) -> "VDPStream":
        """MOVE(corner1), PLOT 101(corner2) — filled rectangle, 12 bytes."""
        self.move(x1, y1)
        self._plot_xy(101, x2, y2)
        return self

    def point(self, x: int, y: int) -> "VDPStream":
        """PLOT 69 — plot single pixel, 6 bytes."""
        self._plot_xy(69, x, y)
        return self

    def line(self, x1: int, y1: int, x2: int, y2: int) -> "VDPStream":
        """MOVE(start), DRAW(end) — line, 12 bytes."""
        self.move(x1, y1)
        self.draw(x2, y2)
        return self

    def filled_triangle(self, x1: int, y1: int,
                        x2: int, y2: int,
                        x3: int, y3: int) -> "VDPStream":
        """Draw a filled triangle given three vertices.

        Uses the VDP triangle fill: MOVE to v1, MOVE to v2, PLOT 85 to v3.
        """
        self.move(x1, y1)
        self.move(x2, y2)
        self.triangle(x3, y3)
        return self

    # -- Output --

    def get_bytes(self) -> bytes:
        """Return accumulated VDU byte stream."""
        return bytes(self._buf)

    def reset(self) -> "VDPStream":
        """Clear the buffer."""
        self._buf.clear()
        return self

    def __len__(self) -> int:
        return len(self._buf)
