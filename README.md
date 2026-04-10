# Optimum Benchmark

3-way protocol performance comparison: **libp2p Direct** (transport baseline) / **GossipSub** / **mump2p (RLNC)**

```
Sender ──── libp2p stream ────→ Receiver     (Direct: pure TCP, ~1ms)

Sender → gRPC → Node-1 sidecar → gossip mesh → Node-2..8 → gRPC → Subscriber
                                  ↑ GossipSub: store-and-forward (~42ms spread)
                                  ↑ mump2p:    shard → recode → decode (~94ms)
```

## Prerequisites

> **Linux only** — This benchmark relies on `tc netem` for network latency simulation, which requires the Linux kernel. macOS Docker Desktop and Windows WSL2 do not reliably support `tc` inside containers. Always run on a native Linux host for accurate results.

- **OS**: Linux (Ubuntu, CentOS, Amazon Linux, etc.)
- Docker & Docker Compose
- Go 1.22+
- Python 3.10+
- `pip install -r requirements.txt`
- `tc` and `nsenter` on the host (for network shaping):

```bash
# RHEL / CentOS / Amazon Linux
sudo yum install -y iproute iproute-tc util-linux

# Debian / Ubuntu
sudo apt install -y iproute2 util-linux
```

## Quick Start

```bash
cd scripts

# 3-way comparison (auto setup → start → run → stop → clean)
python3 benchmark.py --mode compare

# With WAN latency simulation (Linux only — requires tc + nsenter, see Known Issues #9)
python3 benchmark.py --mode compare --latency wan

# 20-node, custom latency + bandwidth
python3 benchmark.py --mode compare --nodes 20 --latency 20,50,100 --bandwidth 1000

# Shard factor sweep
python3 benchmark.py --mode sweep --latency wan

# Raw libp2p transport baseline (standalone)
cd ../tools/libp2p-direct
bash run_direct_bench.sh 8 102400 10 500 /tmp/direct.tsv
```

## Default Test Parameters

| Parameter | Default | Description |
|-----------|---------|-------------|
| `--nodes` | **8** | Total P2P nodes (1 publisher + 7 receivers) |
| `--msg-size` | **100kb** | Message payload size (**max ~1MB**, see Known Issues) |
| `--bandwidth` | **unlimited** | Per-node bandwidth cap in mbit |
| `--count` | **10** | Number of messages per test |
| `--latency` | **off** | Network latency preset or custom values |

## Test Architecture

**Test Flow** (GossipSub / mump2p Phase):
1. `generate_compose.py` dynamically generates an N-node docker-compose and starts the cluster
2. `/api/v1/health` verifies all nodes are healthy
3. `p2p-multi-subscribe` listens on all receiving nodes, collecting trace TSV
4. `p2p-multi-publish` sends all messages in a single process (500ms interval)
5. `parse_results.py` parses trace TSV, filtering by topic to prevent cross-phase leakage

**Timing Model** (node-internal clocks, no process-spawn overhead):

| Protocol | Baseline Time | Latency Meaning |
|----------|--------------|-----------------|
| **mump2p** | First `NEW_SHARD` timestamp | Encoding + mesh propagation + decoding |
| **GossipSub** | Earliest `DELIVER_MESSAGE` | Inter-node propagation spread (first → last receiver) |
| **libp2p Direct** | In-process `time.Now()` | Pure TCP stream write → read |

## Benchmark Results (100KB, 8 nodes)

**Without latency (Local Docker bridge, ~0ms RTT):**

| Protocol | Avg Latency | Min | Max | Measurement |
|----------|------------|-----|-----|-------------|
| **libp2p Direct** | **~1ms** | <0.1ms | ~12ms | Pure TCP transport |
| **GossipSub** | **~42ms** | 0ms | ~142ms | Inter-node spread (first→last deliver) |
| **mump2p (RLNC)** | **~94ms** | ~43ms | ~181ms | Shard encode → propagate → decode |

> Without latency, GossipSub is faster than mump2p because store-and-forward has no encoding overhead.

**With WAN latency (`--latency wan`, Linux host):**

| Protocol | Avg Latency | Min | Max | Measurement |
|----------|------------|-----|-----|-------------|
| **libp2p Direct** | **~1ms** | ~0.4ms | ~4ms | Local TCP (unaffected by tc netem) |
| **GossipSub** | **~261ms** | ~261ms | ~261ms | Propagation spread across mesh |
| **mump2p (RLNC)** | **~234ms** | **~201ms** | ~261ms | RLNC coded fragment spread |

> With WAN latency, **mump2p (RLNC) is ~10% faster on average and ~23% faster in best case** than GossipSub. RLNC can reconstruct from any k-of-n coded fragments, exploiting faster paths instead of waiting for the slowest hop. GossipSub must forward the complete message through the mesh, bottlenecked by the highest-latency node on the path.

**Latency Breakdown (100KB message):**

```
libp2p Direct:   [~1ms TCP stream]
                 ━━━

GossipSub:       [~0ms] first receiver → [~42ms spread] → last receiver
                 ━━━━━━━━━━━━━━━━━━━━━━

mump2p (RLNC):  [encode ~20ms] → [shard propagation ~30ms] → [decode + spread ~44ms]
                 ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
```

## Latency Presets

| Preset | Delays (ms per node) | Scenario |
|--------|---------------------|----------|
| `off` | 0 everywhere | Local Docker bridge (~0ms) |
| `wan` | 20, 50, 100, 50, 80, 30, 60, 40 | Moderate WAN |
| `high` | 50, 100, 200, 100, 150, 80, 120, 90 | High latency WAN |
| `extreme` | 100, 200, 500, 200, 300, 150, 250, 180 | Intercontinental |

## Key Findings

| Dimension | libp2p Direct | GossipSub | mump2p (RLNC) |
|-----------|--------------|-----------|----------------|
| Propagation model | Point-to-point stream | Store-and-forward (full message) | Shard-and-recode (coded fragments) |
| Local Docker (no latency) | ~1ms | ~42ms | ~94ms |
| WAN latency simulation | ~1ms (local) | ~261ms | **~234ms (10% faster)** |
| Hoodi testnet (30 nodes) | — | ~1000ms | ~150ms (6.7x faster) |
| Intermediate node | N/A | Forward complete msg only | Recode partial shards |
| Packet loss resilience | None | Full retransmission | Any k independent shards suffice |
| Best scenario | Single hop, no loss | Low latency, no loss | High latency, multi-hop, packet loss |

## Project Structure

```
optimum-benchmark/
├── scripts/
│   ├── benchmark.py          # Main orchestrator (compare / sweep modes)
│   ├── generate_compose.py   # Dynamic N-node docker-compose generator
│   ├── run_test.sh           # Bash test runner (pub/sub + trace collection)
│   └── parse_results.py      # Trace TSV parser + latency statistics
├── tools/
│   └── libp2p-direct/        # Raw libp2p transport baseline
│       ├── main.go
│       ├── go.mod
│       └── run_direct_bench.sh
├── optimum-dev-setup-guide/  # Git submodule (P2P node + client binaries)
├── results/                  # Auto-generated benchmark outputs
├── requirements.txt
└── README.md
```

## Known Issues & Pitfalls

### 1. Message size limit ~1MB
p2pnode v0.0.1-rc16 has a hard limit of **1,048,576 bytes**. The `OPTIMUM_MAX_MSG_SIZE` environment variable has no effect beyond this. Oversized messages are silently dropped. Use `--msg-size 900kb` or smaller.

### 2. Python subprocess deadlock with Go binaries
`p2p-multi-publish` / `p2p-multi-subscribe` randomly hang when invoked via `subprocess.run()`, but work fine when executed directly from shell. All actual pub/sub calls are made through `run_test.sh` (bash); Python only handles environment setup and result parsing.

### 3. gRPC backpressure (fixed)
`p2p-multi-publish` originally blocked after 7-8 consecutive large messages because it used a bidi gRPC stream (`ListenCommands`) with `Send()`-only — never calling `Recv()`. Server responses filled the HTTP/2 receive window, causing flow-control backpressure. Fixed by: (a) adding a background goroutine to drain `Recv()`, (b) setting 1GB `InitialWindowSize` / `InitialConnWindowSize` on the gRPC client (matching the proxy client's configuration).

### 4. Topic cross-phase leakage
P2P nodes broadcast all topic messages to gRPC subscribers. Filtering by the `topic` field in trace TSV prevents contamination between benchmark phases.

### 5. Docker network/port residuals
After `docker-compose down` between phases, a 5s+ wait is needed for TIME_WAIT sockets. The benchmark proactively runs `docker network prune`.

### 6. GossipSub has no NEW_SHARD events
GossipSub traces only contain `DELIVER_MESSAGE`. Latency baseline is the spread of the same `msg_id`'s earliest to latest DELIVER (measuring inter-node propagation, not end-to-end). This is not directly comparable to mump2p's `NEW_SHARD` baseline, which includes encoding time.

### 7. First message GRAFT delay
The first message in each phase can show inflated spread while the mesh finishes GRAFT. **`parse_results.py` drops that sample only when there are at least 8 messages with valid spreads** so sparse WAN runs are not penalized twice (few deliveries + mandatory drop).

### 8. Large message + WAN latency combination failure
With emulated WAN (`--latency` not `off`), p2pnode rc16 often delivers **nothing** for GossipSub/mump2p if the payload is too large (often above ~32KB). **`benchmark.py` auto-caps MsgSize to 32KB when tc latency is active** so compare/sweep get non-empty traces. For full-size payloads without that limit, run with `--latency off` (or pass an explicit `--msg-size` at or below 32kb under WAN).

Under WAN, **`tc netem` uses `limit 50000`** (instead of the kernel default 1000 packets) to reduce tail drops on bursty pubsub, and **`run_test.sh` gets longer mesh / GRAFT / per-round waits** plus a **longer final drain** (`BENCH_FINAL_DRAIN_SECS`, default 45s under WAN) so the last publishes are not cut off while still on the wire. Override with `BENCH_MESH_STABILIZE_SECS`, `BENCH_TOPIC_GRAFT_WAIT_SECS`, `BENCH_PER_ROUND_WAIT_SECS`, or `BENCH_FINAL_DRAIN_SECS` if needed.

### 9. Network latency simulation requires a native Linux host
`--latency` relies on the Linux kernel's `tc netem` (traffic control) to inject network delay into container network namespaces via `nsenter`. This does **not** work on:
- **macOS Docker Desktop** — amd64 images run via Rosetta emulation; `rtnetlink` is unsupported (`Cannot talk to rtnetlink: Not supported`)
- **Windows Docker Desktop / WSL2** — the WSL2 kernel may lack `sch_netem` module and `nsenter` cannot access container network namespaces through the VM boundary

**Symptom**: Results with `--latency wan` are identical to `--latency off` — delay is silently not applied.

**Solution**: Always run on a **native Linux host**. Ensure `tc` and `nsenter` are installed:
```bash
# RHEL / CentOS / Amazon Linux
sudo yum install -y iproute iproute-tc util-linux

# Debian / Ubuntu
sudo apt install -y iproute2 util-linux
```
The benchmark uses the host's `tc` binary via `nsenter` (searching `/sbin/tc`, `/usr/sbin/tc`) to avoid version mismatches between container iproute2 and the host kernel. If `tc` or `nsenter` is missing, it falls back to the container's `tc` which may silently fail.
