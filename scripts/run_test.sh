#!/usr/bin/env bash
#
# run_test.sh — Execute a single benchmark phase (GossipSub or mump2p)
#
# Invoked by benchmark.py.  Does NOT start/stop Docker — that is the caller's job.
# Steps:
#   1. Wait for all nodes healthy AND peers connected (/api/v1/node-state)
#   2. Subscribe all nodes (background p2p-multi-subscribe)
#   3. Wait for topic GRAFT propagation
#   4. Publish all messages via p2p-multi-publish (single process, 500ms interval)
#   5. Wait for subscriber to collect traces, then kill
#
# Usage:
#   run_test.sh <protocol> <n_nodes> <msg_size_bytes> <count> <topic> \
#               <client_bin_dir> <trace_out> <ips_file>

set -euo pipefail

PROTOCOL="${1:?protocol required (optimum|gossipsub)}"
N_NODES="${2:?n_nodes required}"
MSG_SIZE="${3:?msg_size_bytes required}"
COUNT="${4:?message count required}"
TOPIC="${5:?topic required}"
CLIENT_DIR="${6:?client binary directory required}"
TRACE_OUT="${7:?trace output path required}"
IPS_FILE="${8:?ips file path required}"

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m'

log()  { echo -e "${BLUE}[run_test]${NC} $*"; }
ok()   { echo -e "${GREEN}[run_test]${NC} $*"; }
warn() { echo -e "${YELLOW}[run_test]${NC} $*"; }
err()  { echo -e "${RED}[run_test]${NC} $*" >&2; }

PUB_CLIENT="${CLIENT_DIR}/p2p-client"
MULTI_SUB="${CLIENT_DIR}/p2p-multi-subscribe"
MULTI_PUB="${CLIENT_DIR}/p2p-multi-publish"

for bin in "$PUB_CLIENT" "$MULTI_SUB"; do
    if [ ! -x "$bin" ]; then
        err "Binary not found or not executable: $bin"
        exit 1
    fi
done

if [ ! -f "$IPS_FILE" ]; then
    err "IPs file not found: $IPS_FILE"
    exit 1
fi

PUB_ADDR=$(head -1 "$IPS_FILE" | tr -d '[:space:]')
log "Protocol=$PROTOCOL  Nodes=$N_NODES  MsgSize=$MSG_SIZE  Count=$COUNT  Topic=$TOPIC"
log "Publisher → $PUB_ADDR"

# ─── Step 1: Health check + peer connectivity ───────────────────────────────
log "Waiting for nodes to be healthy..."
MAX_RETRIES=90
EXPECTED_PEERS=$((N_NODES - 1))
for i in $(seq 1 "$MAX_RETRIES"); do
    ALL_READY=true
    for idx in $(seq 0 $((N_NODES - 1))); do
        API_PORT=$((9091 + idx))
        HTTP_CODE=$(curl -s -o /dev/null -w '%{http_code}' "http://127.0.0.1:${API_PORT}/api/v1/health" 2>/dev/null || echo "000")
        if [ "$HTTP_CODE" != "200" ]; then
            ALL_READY=false
            break
        fi
    done
    if $ALL_READY; then
        # Verify that node-1 has peers connected via /api/v1/node-state
        STATE=$(curl -s "http://127.0.0.1:9091/api/v1/node-state" 2>/dev/null || echo "{}")
        PEER_COUNT=$(echo "$STATE" | python3 -c "
import sys, json
try:
    d = json.load(sys.stdin)
    peers = d.get('peers', [])
    print(len(peers))
except:
    print(0)
" 2>/dev/null || echo "0")

        if [ "$PEER_COUNT" -ge "$EXPECTED_PEERS" ]; then
            ok "All $N_NODES nodes healthy, node-1 sees $PEER_COUNT peers (after ${i}s)"
            break
        else
            if [ "$((i % 10))" -eq 0 ]; then
                log "  Nodes healthy but node-1 only sees $PEER_COUNT/$EXPECTED_PEERS peers (${i}s)..."
            fi
        fi
    fi
    if [ "$i" -eq "$MAX_RETRIES" ]; then
        warn "Timeout after ${MAX_RETRIES}s — proceeding anyway (peers may be incomplete)"
    fi
    sleep 1
done

# Extra settle time for mesh formation
log "Waiting 15s for mesh stabilization..."
sleep 15

# ─── Step 2: Subscribe all nodes ────────────────────────────────────────────
log "Starting multi-subscribe on all $N_NODES nodes, topic=$TOPIC ..."

"$MULTI_SUB" \
    -topic="$TOPIC" \
    -ipfile="$IPS_FILE" \
    -start-index=0 \
    -end-index="$N_NODES" \
    -output-trace="$TRACE_OUT" &
SUB_PID=$!
log "Subscriber PID=$SUB_PID"

# Wait for gRPC streams to establish + topic GRAFT propagation
log "Waiting 15s for topic GRAFT propagation..."
sleep 15

# ─── Step 3: Publish messages ─────────────────────────────────────────────────
log "Publishing $COUNT messages of ${MSG_SIZE} bytes..."

DATASIZE=$((MSG_SIZE / 2))
if [ "$DATASIZE" -lt 1 ]; then
    DATASIZE=1
fi

PUB_IPS_FILE=$(mktemp /tmp/bench_pub_ips.XXXXXX)
head -1 "$IPS_FILE" > "$PUB_IPS_FILE"

"$MULTI_PUB" \
    -topic="$TOPIC" \
    -ipfile="$PUB_IPS_FILE" \
    -start-index=0 \
    -end-index=1 \
    -count="$COUNT" \
    -datasize="$DATASIZE" \
    -sleep=500ms \
    2>&1 || warn "Publish may have partially failed"

rm -f "$PUB_IPS_FILE"
ok "All $COUNT messages published"

# ─── Step 4: Wait for propagation ───────────────────────────────────────────
DRAIN_SECS=30
log "Waiting ${DRAIN_SECS}s for message propagation..."
sleep "$DRAIN_SECS"

# ─── Step 5: Kill subscriber, clean up ──────────────────────────────────────
log "Stopping subscriber (PID=$SUB_PID)..."
kill "$SUB_PID" 2>/dev/null || true
wait "$SUB_PID" 2>/dev/null || true

if [ -f "$TRACE_OUT" ]; then
    LINE_COUNT=$(wc -l < "$TRACE_OUT" | tr -d '[:space:]')
    ok "Trace collected: $TRACE_OUT ($LINE_COUNT lines)"
else
    warn "No trace file produced at $TRACE_OUT"
fi

ok "Phase $PROTOCOL complete."
