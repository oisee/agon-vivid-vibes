"""Fake eZ80 server — speaks agon-protocol over TCP.

Implements the server (eZ80) side of the agon-protocol so that
agon-vdp-sdl can connect and render VDP command streams.

Protocol wire format: [len:u16-LE][type:u8][payload...]
  len includes the type byte but NOT the 2-byte length prefix.

Handshake:
  1. VDP connects to us
  2. VDP sends HELLO (type=0x10, payload=[version:u8, flags:u8])
  3. We send HELLO_ACK (type=0x11, payload=[version:u8, capabilities_json...])
  4. Normal streaming begins

Usage as library:
    server = FakeEz80Server("0.0.0.0", 5001)
    server.start()          # blocks until VDP connects + handshake
    server.send_vdu(vdu_bytes)
    server.wait_vsync()     # wait for VDP's vsync signal
    server.shutdown()

Usage standalone:
    python fake_ez80.py [--port 5001]
    # Then connect agon-vdp-sdl: --tcp localhost:5001
"""

import socket
import struct
import threading
import time
import sys

# -- Protocol constants (from agon-protocol/src/messages.rs) --

MSG_UART_DATA = 0x01
MSG_VSYNC = 0x02
MSG_CTS = 0x03
MSG_HELLO = 0x10
MSG_HELLO_ACK = 0x11
MSG_SHUTDOWN = 0x20

PROTOCOL_VERSION = 1
MAX_UART_DATA_SIZE = 1024

MSG_NAMES = {
    MSG_UART_DATA: "UART_DATA",
    MSG_VSYNC: "VSYNC",
    MSG_CTS: "CTS",
    MSG_HELLO: "HELLO",
    MSG_HELLO_ACK: "HELLO_ACK",
    MSG_SHUTDOWN: "SHUTDOWN",
}


def encode_message(msg_type: int, payload: bytes = b"") -> bytes:
    """Encode a protocol message to wire format."""
    length = 1 + len(payload)  # type byte + payload
    return struct.pack("<HB", length, msg_type) + payload


def decode_message(data: bytes) -> tuple[int, bytes, int]:
    """Decode one message from buffer. Returns (msg_type, payload, total_bytes_consumed)."""
    if len(data) < 3:
        raise ValueError("Incomplete message header")
    length = struct.unpack("<H", data[:2])[0]
    total = 2 + length
    if len(data) < total:
        raise ValueError(f"Incomplete message: have {len(data)}, need {total}")
    msg_type = data[2]
    payload = data[3:total]
    return msg_type, payload, total


def recv_message(sock: socket.socket) -> tuple[int, bytes]:
    """Read exactly one protocol message from socket."""
    # Read 2-byte length
    hdr = _recv_exact(sock, 2)
    length = struct.unpack("<H", hdr)[0]
    if length == 0:
        raise ConnectionError("Zero-length message")
    # Read type + payload
    body = _recv_exact(sock, length)
    msg_type = body[0]
    payload = body[1:]
    return msg_type, payload


def _recv_exact(sock: socket.socket, n: int) -> bytes:
    """Read exactly n bytes from socket."""
    buf = bytearray()
    while len(buf) < n:
        chunk = sock.recv(n - len(buf))
        if not chunk:
            raise ConnectionError("Connection closed")
        buf.extend(chunk)
    return bytes(buf)


class FakeEz80Server:
    """TCP server that pretends to be an eZ80, streaming VDU data to agon-vdp-sdl."""

    def __init__(self, host: str = "0.0.0.0", port: int = 5001, verbose: bool = False):
        self.host = host
        self.port = port
        self.verbose = verbose
        self._server_sock: socket.socket | None = None
        self._conn: socket.socket | None = None
        self._vsync_event = threading.Event()
        self._vsync_count = 0
        self._reader_thread: threading.Thread | None = None
        self._running = False

    def start(self, timeout: float = 30.0) -> None:
        """Listen for VDP connection and perform handshake. Blocks until ready."""
        self._server_sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._server_sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._server_sock.bind((self.host, self.port))
        self._server_sock.listen(1)
        print(f"[fake_ez80] Listening on {self.host}:{self.port}", file=sys.stderr)
        print(f"[fake_ez80] Connect VDP: agon-vdp-sdl --tcp localhost:{self.port}", file=sys.stderr)

        self._server_sock.settimeout(timeout)
        try:
            self._conn, addr = self._server_sock.accept()
        except socket.timeout:
            raise TimeoutError(f"No VDP connection within {timeout}s")
        self._conn.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        print(f"[fake_ez80] VDP connected from {addr}", file=sys.stderr)

        self._handshake()
        print("[fake_ez80] Handshake complete", file=sys.stderr)

        # Start background reader for VSYNC / CTS / etc
        self._running = True
        self._reader_thread = threading.Thread(target=self._reader_loop, daemon=True)
        self._reader_thread.start()

        # Wait for VDP firmware to settle — it needs a few vsync cycles
        # to finish initialization before it'll process our VDU commands
        print("[fake_ez80] Waiting for VDP to settle...", file=sys.stderr)
        time.sleep(0.5)
        # Drain any stale vsync events accumulated during sleep
        self._vsync_event.clear()
        self._vsync_count = 0
        print("[fake_ez80] Ready to stream", file=sys.stderr)

    def _handshake(self) -> None:
        """Perform agon-protocol handshake (VDP sends HELLO, we reply HELLO_ACK)."""
        msg_type, payload = recv_message(self._conn)
        if msg_type != MSG_HELLO:
            raise ConnectionError(f"Expected HELLO (0x10), got 0x{msg_type:02x}")
        version = payload[0] if payload else 0
        flags = payload[1] if len(payload) > 1 else 0
        print(f"[fake_ez80] <- HELLO version={version}, flags={flags}", file=sys.stderr)

        # Send HELLO_ACK
        caps = b'{"type":"ez80","version":"1.0"}'
        ack_payload = bytes([PROTOCOL_VERSION]) + caps
        self._conn.sendall(encode_message(MSG_HELLO_ACK, ack_payload))
        print(f"[fake_ez80] -> HELLO_ACK version={PROTOCOL_VERSION}", file=sys.stderr)

    def _reader_loop(self) -> None:
        """Background thread: read messages from VDP (VSYNC, CTS, keyboard, etc)."""
        while self._running:
            try:
                msg_type, payload = recv_message(self._conn)
                if msg_type == MSG_VSYNC:
                    self._vsync_count += 1
                    self._vsync_event.set()
                elif msg_type == MSG_UART_DATA:
                    if self.verbose:
                        print(f"[fake_ez80] <- UART ({len(payload)}B): {payload[:32].hex()}", file=sys.stderr)
                elif msg_type == MSG_CTS:
                    ready = payload[0] != 0 if payload else False
                    if self.verbose:
                        print(f"[fake_ez80] <- CTS ready={ready}", file=sys.stderr)
                elif msg_type == MSG_SHUTDOWN:
                    print("[fake_ez80] VDP sent SHUTDOWN", file=sys.stderr)
                    self._running = False
                    break
                else:
                    name = MSG_NAMES.get(msg_type, f"0x{msg_type:02x}")
                    if self.verbose:
                        print(f"[fake_ez80] <- {name} ({len(payload)}B)", file=sys.stderr)
            except ConnectionError:
                self._running = False
                break
            except Exception as e:
                print(f"[fake_ez80] Reader error: {e}", file=sys.stderr)
                self._running = False
                break

    def send_vdu(self, data: bytes) -> None:
        """Send raw VDU bytes as UartData message(s), splitting at MAX_UART_DATA_SIZE."""
        if not self._conn or not self._running:
            raise ConnectionError("Not connected")
        offset = 0
        while offset < len(data):
            chunk = data[offset:offset + MAX_UART_DATA_SIZE]
            self._conn.sendall(encode_message(MSG_UART_DATA, chunk))
            offset += MAX_UART_DATA_SIZE

    def wait_vsync(self, timeout: float = 1.0) -> bool:
        """Wait for a VSYNC signal from VDP. Returns True if received."""
        self._vsync_event.clear()
        got_it = self._vsync_event.wait(timeout)
        return got_it

    def shutdown(self) -> None:
        """Send shutdown and close connection."""
        self._running = False
        if self._conn:
            try:
                self._conn.sendall(encode_message(MSG_SHUTDOWN))
            except Exception:
                pass
            try:
                self._conn.close()
            except Exception:
                pass
        if self._server_sock:
            try:
                self._server_sock.close()
            except Exception:
                pass
        if self._reader_thread:
            self._reader_thread.join(timeout=2.0)

    @property
    def connected(self) -> bool:
        return self._running


def main():
    """Standalone test: start server, send a simple test pattern when VDP connects."""
    import argparse
    from vdp_stream import VDPStream

    parser = argparse.ArgumentParser(description="Fake eZ80 — agon-protocol server")
    parser.add_argument("--port", type=int, default=5001, help="TCP port (default: 5001)")
    parser.add_argument("-v", "--verbose", action="store_true", help="Verbose logging")
    args = parser.parse_args()

    server = FakeEz80Server(port=args.port, verbose=args.verbose)
    server.start()

    # Draw a simple test pattern: coloured triangles
    try:
        # Send General Poll to unlock VDP firmware
        s = VDPStream()
        s.general_poll()
        server.send_vdu(s.get_bytes())
        for _ in range(5):
            server.wait_vsync()

        s.reset()
        s.mode(8)  # 320x240, 64 colours
        server.send_vdu(s.get_bytes())

        # Wait for mode switch to take effect
        for _ in range(5):
            server.wait_vsync()

        for i in range(120):
            if not server.connected:
                break
            s.reset()
            s.clg()
            # Draw some moving triangles
            offset = i * 4
            for c in range(1, 16):
                s.gcol(0, c)
                x = (c * 20 + offset) % 320
                s.filled_triangle(x, 20, x + 30, 100, x - 10, 100)
            server.send_vdu(s.get_bytes())
            server.wait_vsync()
            server.wait_vsync()  # ~30fps

        print("[fake_ez80] Test pattern done", file=sys.stderr)
    except KeyboardInterrupt:
        print("\n[fake_ez80] Interrupted", file=sys.stderr)
    finally:
        server.shutdown()


if __name__ == "__main__":
    main()
