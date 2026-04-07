#!/usr/bin/env bash
#
# run_direct_bench.sh — Run libp2p-direct benchmark
#
# Usage:
#   bash run_direct_bench.sh <nodes> <msg_size_bytes> <count> <sleep_ms> <output_tsv>
#
# Example:
#   bash run_direct_bench.sh 8 102400 10 500 /tmp/direct.tsv

set -euo pipefail

NODES="${1:?nodes required}"
SIZE="${2:?msg_size_bytes required}"
COUNT="${3:?count required}"
SLEEP="${4:-500}"
OUTPUT="${5:-/tmp/direct.tsv}"

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"

echo "=== libp2p Direct Benchmark ==="
echo "  Nodes:    $NODES"
echo "  Size:     $SIZE bytes"
echo "  Count:    $COUNT"
echo "  Sleep:    ${SLEEP}ms"
echo "  Output:   $OUTPUT"
echo ""

cd "$SCRIPT_DIR"

if [ ! -f "go.sum" ]; then
    echo "Running go mod tidy..."
    go mod tidy
fi

echo "Building benchmark binary..."
go build -o bench-direct .

echo "Running benchmark..."
./bench-direct \
    -nodes="$NODES" \
    -size="$SIZE" \
    -count="$COUNT" \
    -sleep="$SLEEP" \
    -out="$OUTPUT"

echo ""
echo "=== Done ==="
