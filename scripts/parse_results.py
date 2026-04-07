#!/usr/bin/env python3
"""
Parse benchmark trace TSV files and compute latency statistics.

Trace TSV format (tab-separated, no header):
  type  peerID  recvID  msgID  topic  timestamp_ns

For mump2p:   latency = per-message (all NEW_SHARD timestamps) max - min
For GossipSub: latency = per-message (all DELIVER_MESSAGE timestamps) max - min
For libp2p Direct: separate simple TSV with latency_ns column

Outputs a summary dict per protocol suitable for benchmark.py to aggregate.
"""

import csv
import os
import statistics
import sys
from collections import defaultdict
from typing import NamedTuple


class LatencyResult(NamedTuple):
    protocol: str
    avg_ms: float
    min_ms: float
    max_ms: float
    p50_ms: float
    p95_ms: float
    p99_ms: float
    count: int
    raw_ms: list[float]


def ns_to_ms(ns: float) -> float:
    return ns / 1_000_000


def parse_p2p_trace(filepath: str, protocol: str, topic_filter: str | None = None) -> LatencyResult:
    """Parse mump2p or GossipSub trace TSV.

    For mump2p we look at NEW_SHARD events; for GossipSub we look at DELIVER_MESSAGE.
    Per message, latency = max(timestamp) - min(timestamp) across all receiving peers.
    """
    if protocol == "optimum":
        event_type = "NEW_SHARD"
    else:
        event_type = "DELIVER_MESSAGE"

    msg_timestamps: dict[str, list[int]] = defaultdict(list)

    if not os.path.isfile(filepath):
        return LatencyResult(protocol, 0, 0, 0, 0, 0, 0, 0, [])

    with open(filepath) as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            parts = line.split("\t")
            if len(parts) < 6:
                continue

            evt_type, _peer, _recv, msg_id, topic, ts_str = parts[:6]

            if evt_type != event_type:
                continue
            if topic_filter and topic and topic != topic_filter:
                continue

            try:
                ts = int(ts_str)
            except ValueError:
                continue

            msg_timestamps[msg_id].append(ts)

    if not msg_timestamps:
        return LatencyResult(protocol, 0, 0, 0, 0, 0, 0, 0, [])

    latencies_ns = []
    for msg_id, timestamps in msg_timestamps.items():
        if len(timestamps) < 2:
            continue
        spread = max(timestamps) - min(timestamps)
        latencies_ns.append(spread)

    if not latencies_ns:
        return LatencyResult(protocol, 0, 0, 0, 0, 0, 0, 0, [])

    latencies_ms = [ns_to_ms(x) for x in latencies_ns]

    # Drop the first message (GRAFT delay anomaly)
    if len(latencies_ms) > 2:
        latencies_ms = latencies_ms[1:]

    return LatencyResult(
        protocol=protocol,
        avg_ms=statistics.mean(latencies_ms),
        min_ms=min(latencies_ms),
        max_ms=max(latencies_ms),
        p50_ms=statistics.median(latencies_ms),
        p95_ms=_percentile(latencies_ms, 95),
        p99_ms=_percentile(latencies_ms, 99),
        count=len(latencies_ms),
        raw_ms=latencies_ms,
    )


def parse_direct_tsv(filepath: str) -> LatencyResult:
    """Parse libp2p-direct benchmark TSV.

    Expected format (tab-separated, with header):
      msg_index  size_bytes  latency_ns
    """
    latencies_ms = []

    if not os.path.isfile(filepath):
        return LatencyResult("libp2p-direct", 0, 0, 0, 0, 0, 0, 0, [])

    with open(filepath) as f:
        reader = csv.reader(f, delimiter="\t")
        header = next(reader, None)
        for row in reader:
            if len(row) < 3:
                continue
            try:
                lat_ns = int(row[2])
                latencies_ms.append(ns_to_ms(lat_ns))
            except (ValueError, IndexError):
                continue

    if not latencies_ms:
        return LatencyResult("libp2p-direct", 0, 0, 0, 0, 0, 0, 0, [])

    return LatencyResult(
        protocol="libp2p-direct",
        avg_ms=statistics.mean(latencies_ms),
        min_ms=min(latencies_ms),
        max_ms=max(latencies_ms),
        p50_ms=statistics.median(latencies_ms),
        p95_ms=_percentile(latencies_ms, 95),
        p99_ms=_percentile(latencies_ms, 99),
        count=len(latencies_ms),
        raw_ms=latencies_ms,
    )


def _percentile(data: list[float], pct: int) -> float:
    if not data:
        return 0
    sorted_data = sorted(data)
    idx = int(len(sorted_data) * pct / 100)
    idx = min(idx, len(sorted_data) - 1)
    return sorted_data[idx]


def format_table(results: list[LatencyResult]) -> str:
    lines = []
    header = f"{'Protocol':<20} {'Avg':>10} {'Min':>10} {'Max':>10} {'P50':>10} {'P95':>10} {'P99':>10} {'Count':>6}"
    lines.append(header)
    lines.append("-" * len(header))

    for r in results:
        if r.count == 0:
            lines.append(f"{r.protocol:<20} {'(no data)':>10}")
            continue
        lines.append(
            f"{r.protocol:<20} {r.avg_ms:>9.2f}ms {r.min_ms:>9.2f}ms {r.max_ms:>9.2f}ms "
            f"{r.p50_ms:>9.2f}ms {r.p95_ms:>9.2f}ms {r.p99_ms:>9.2f}ms {r.count:>6}"
        )

    return "\n".join(lines)


def format_markdown_table(results: list[LatencyResult]) -> str:
    lines = [
        "| Protocol | Avg Latency | Min | Max | P50 | P95 | P99 | Samples |",
        "|----------|------------|-----|-----|-----|-----|-----|---------|",
    ]
    for r in results:
        if r.count == 0:
            lines.append(f"| **{r.protocol}** | (no data) | - | - | - | - | - | 0 |")
            continue
        lines.append(
            f"| **{r.protocol}** | ~{r.avg_ms:.1f}ms | {r.min_ms:.1f}ms | {r.max_ms:.1f}ms "
            f"| {r.p50_ms:.1f}ms | {r.p95_ms:.1f}ms | {r.p99_ms:.1f}ms | {r.count} |"
        )
    return "\n".join(lines)


if __name__ == "__main__":
    if len(sys.argv) < 3:
        print("Usage: parse_results.py <protocol> <trace_file> [topic_filter]")
        print("  protocol: optimum | gossipsub | direct")
        sys.exit(1)

    protocol = sys.argv[1]
    filepath = sys.argv[2]
    topic = sys.argv[3] if len(sys.argv) > 3 else None

    if protocol == "direct":
        result = parse_direct_tsv(filepath)
    else:
        result = parse_p2p_trace(filepath, protocol, topic_filter=topic)

    print(format_table([result]))
