#!/bin/bash
# Preview Bad Apple via fake_ez80 → agon-vdp
#
# Usage:
#   ./preview_badapple.sh [--frames 100] [--verbose] [--dump /tmp/ba_frames]
#
# Launches Python gen + VDP emulator, kills both on exit.

set -e

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PORT=5001
FRAMES=100
VDP_FLAGS=""
DUMP_DIR=""
EXTRA_ARGS=""

while [[ $# -gt 0 ]]; do
    case "$1" in
        --frames)   FRAMES="$2"; shift 2 ;;
        --verbose)  VDP_FLAGS="-v"; shift ;;
        --trace)    VDP_FLAGS="-vv"; shift ;;
        --dump)     DUMP_DIR="$2"; shift 2 ;;
        *)          EXTRA_ARGS="$EXTRA_ARGS $1"; shift ;;
    esac
done

if [[ -n "$DUMP_DIR" ]]; then
    mkdir -p "$DUMP_DIR"
    VDP_FLAGS="$VDP_FLAGS --dump-keyframes $DUMP_DIR"
fi

cleanup() {
    echo ""
    echo "[preview] Cleaning up..."
    [[ -n "$PY_PID" ]] && kill "$PY_PID" 2>/dev/null
    [[ -n "$VDP_PID" ]] && kill "$VDP_PID" 2>/dev/null
    wait 2>/dev/null
}
trap cleanup EXIT

# Start Python (fake_ez80 server) in background
echo "[preview] Starting gen_badapple_vdp.py --preview --frames $FRAMES ..."
python "$SCRIPT_DIR/gen_badapple_vdp.py" \
    --preview --port "$PORT" --frames "$FRAMES" $EXTRA_ARGS 2>&1 &
PY_PID=$!

# Give Python time to start listening
sleep 1

# Start VDP emulator, connect to fake_ez80
echo "[preview] Starting agon-vdp --tcp localhost:$PORT $VDP_FLAGS"
agon-vdp --tcp "localhost:$PORT" $VDP_FLAGS &
VDP_PID=$!

# Wait for Python to finish (it drives playback)
wait "$PY_PID" 2>/dev/null
PY_PID=""

echo "[preview] Playback finished. Press Enter or close VDP window."
read -r

# cleanup runs via trap
