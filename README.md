# Optimum Benchmark

3-way protocol performance comparison: **libp2p Direct** (transport baseline) / **GossipSub** / **mump2p (RLNC)**

```
Sender ──── libp2p stream ────→ Receiver     (Direct: pure TCP, ~1ms)

Sender → gRPC → Node-1 sidecar → gossip mesh → Node-2..8 → gRPC → Subscriber
                                  ↑ GossipSub: store-and-forward (~42ms spread)
                                  ↑ mump2p:    shard → recode → decode (~94ms)
```

## Prerequisites

- Docker & Docker Compose
- Go 1.22+
- Python 3.10+
- `pip install -r requirements.txt`

## Quick Start

```bash
cd scripts

# 3-way comparison (auto setup → start → run → stop → clean)
python3 benchmark.py --mode compare

# With WAN latency simulation (Linux only, see Known Issues #9)
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
4. Messages are sent one at a time (bash script, 3s interval to avoid gRPC backpressure)
5. `parse_results.py` parses trace TSV, filtering by topic to prevent cross-phase leakage

**Timing Model** (node-internal clocks, no process-spawn overhead):

| Protocol | Baseline Time | Latency Meaning |
|----------|--------------|-----------------|
| **mump2p** | First `NEW_SHARD` timestamp | Encoding + mesh propagation + decoding |
| **GossipSub** | Earliest `DELIVER_MESSAGE` | Inter-node propagation spread (first → last receiver) |
| **libp2p Direct** | In-process `time.Now()` | Pure TCP stream write → read |

## Benchmark Results (Local Docker, 100KB, 8 nodes)

| Protocol | Avg Latency | Min | Max | Measurement |
|----------|------------|-----|-----|-------------|
| **libp2p Direct** | **~1ms** | <0.1ms | ~12ms | Pure TCP transport |
| **GossipSub** | **~42ms** | 0ms | ~142ms | Inter-node spread (first→last deliver) |
| **mump2p (RLNC)** | **~94ms** | ~43ms | ~181ms | Shard encode → propagate → decode |

> Local Docker bridge has ~0ms RTT. GossipSub is faster than mump2p because store-and-forward has no encoding overhead.
> The ~52ms delta in mump2p comes from RLNC encoding/decoding CPU cost, which is offset by multi-path/loss-resilience advantages in high-latency WAN environments.

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
| Local Docker latency | ~1ms | ~42ms | ~94ms |
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

### 3. gRPC backpressure stalls consecutive sends
`p2p-multi-publish -count=N` blocks after 7-8 consecutive large messages. Mitigated by sending one message per process invocation with 3s intervals between calls, ensuring a fresh gRPC connection for each message.

### 4. Topic cross-phase leakage
P2P nodes broadcast all topic messages to gRPC subscribers. Filtering by the `topic` field in trace TSV prevents contamination between benchmark phases.

### 5. Docker network/port residuals
After `docker-compose down` between phases, a 5s+ wait is needed for TIME_WAIT sockets. The benchmark proactively runs `docker network prune`.

### 6. GossipSub has no NEW_SHARD events
GossipSub traces only contain `DELIVER_MESSAGE`. Latency baseline is the spread of the same `msg_id`'s earliest to latest DELIVER (measuring inter-node propagation, not end-to-end). This is not directly comparable to mump2p's `NEW_SHARD` baseline, which includes encoding time.

### 7. First message GRAFT delay
The first message in each phase suffers from incomplete topic subscription GRAFT propagation, resulting in abnormally high latency (~1-3s). It is automatically excluded from statistics.

### 8. Large message + WAN latency combination failure
900KB messages work fine without latency, but adding `--latency wan` causes GossipSub/mump2p to receive nothing. Likely due to internal gossip protocol message propagation timeouts being exceeded by the large message + high latency combination. **Use `--msg-size 100kb` for WAN tests**.

### 9. macOS Docker Desktop does not support `tc netem` latency simulation
`--latency` relies on the Linux kernel's `tc netem` (traffic control) to inject network delay inside containers. On macOS Docker Desktop, the `getoptimum/p2pnode` image is amd64-only and runs via Rosetta emulation, where the `rtnetlink` kernel interface required by `tc qdisc` is not supported (`Cannot talk to rtnetlink: Not supported`).

**Symptom**: Results with `--latency wan` are identical to `--latency off` — delay is not applied.

**Workaround**:
- The benchmark automatically detects `tc` support at startup and prints a warning when unavailable, skipping delay injection
- For real network latency simulation, run on a **Linux host**
