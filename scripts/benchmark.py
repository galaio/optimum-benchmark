#!/usr/bin/env python3
"""
Optimum Benchmark — 3-way protocol performance comparison

Modes:
  compare   Run libp2p Direct / GossipSub / mump2p side by side
  sweep     Vary shard factor across multiple latency presets

Usage:
  python3 benchmark.py --mode compare
  python3 benchmark.py --mode compare --latency wan --nodes 8
  python3 benchmark.py --mode compare --latency 20,50,100 --bandwidth 1000
  python3 benchmark.py --mode sweep --latency wan
"""

import argparse
import datetime
import json
import os
import platform
import shutil
import subprocess
import sys
import time

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECT_ROOT = os.path.dirname(SCRIPT_DIR)
SETUP_GUIDE = os.path.join(PROJECT_ROOT, "optimum-dev-setup-guide")
DIRECT_BENCH = os.path.join(PROJECT_ROOT, "tools", "libp2p-direct")

sys.path.insert(0, SCRIPT_DIR)
from generate_compose import generate
from parse_results import (
    LatencyResult,
    format_markdown_table,
    format_table,
    parse_direct_tsv,
    parse_p2p_trace,
)

LATENCY_PRESETS = {
    "off": [],
    "wan": [20, 50, 100, 50, 80, 30, 60, 40],
    "high": [50, 100, 200, 100, 150, 80, 120, 90],
    "extreme": [100, 200, 500, 200, 300, 150, 250, 180],
}

DEFAULT_NODES = 8
DEFAULT_MSG_SIZE = "100kb"
DEFAULT_COUNT = 10
DEFAULT_BANDWIDTH = 10000

YELLOW = "\033[1;33m"
NC = "\033[0m"


def check_tc_support() -> bool:
    """Detect whether tc netem works inside Docker containers on this host."""
    try:
        result = subprocess.run(
            [
                "docker", "run", "--rm", "--cap-add", "NET_ADMIN",
                "--entrypoint=", "getoptimum/p2pnode:v0.0.1-rc16",
                "sh", "-c",
                "apk add --no-cache iproute2 > /dev/null 2>&1 && "
                "tc qdisc add dev eth0 root netem delay 1ms 2>&1",
            ],
            capture_output=True, text=True, timeout=120,
        )
        output = result.stdout + result.stderr
        if result.returncode != 0 or "Not supported" in output:
            return False
        return True
    except Exception:
        return False


def parse_msg_size(s: str) -> int:
    s = s.strip().lower()
    if s.endswith("kb"):
        return int(s[:-2]) * 1024
    if s.endswith("mb"):
        return int(s[:-2]) * 1024 * 1024
    if s.endswith("b"):
        return int(s[:-1])
    return int(s)


def resolve_latencies(spec: str, n_nodes: int) -> list[int] | None:
    if not spec or spec == "off":
        return None
    if spec in LATENCY_PRESETS:
        base = LATENCY_PRESETS[spec]
        return [base[i % len(base)] for i in range(n_nodes)]
    parts = [int(x.strip()) for x in spec.split(",")]
    return [parts[i % len(parts)] for i in range(n_nodes)]


def resolve_bandwidths(spec: str | None) -> list[int] | None:
    if not spec:
        return None
    parts = [int(x.strip()) for x in spec.split(",")]
    return parts


# ─── Docker helpers ──────────────────────────────────────────────────────────

def docker_compose_up(compose_file: str, project_name: str):
    print(f"\n{'='*60}")
    print(f"  Starting cluster: {project_name}")
    print(f"  Compose: {compose_file}")
    print(f"{'='*60}\n")
    subprocess.run(
        ["docker", "compose", "-f", compose_file, "-p", project_name, "pull"],
        check=False,
    )
    subprocess.run(
        ["docker", "compose", "-f", compose_file, "-p", project_name, "up", "-d"],
        check=True,
    )


def apply_network_shaping(
    project_name: str,
    n_nodes: int,
    latencies: list[int] | None,
    bandwidth_mbit: int | None,
):
    """Apply tc netem rules to running containers via docker exec.

    Called AFTER docker compose up so the containers have network access
    for installing iproute2. Errors are reported instead of silently ignored.
    """
    if not latencies and not bandwidth_mbit:
        return

    print(f"\n  Applying network shaping (tc netem)...")
    success = 0

    for i in range(n_nodes):
        container = f"{project_name}-p2pnode-{i + 1}-1"
        lat = latencies[i % len(latencies)] if latencies else None

        needs_tc = (lat is not None and lat > 0) or (
            bandwidth_mbit is not None and bandwidth_mbit > 0
        )
        if not needs_tc:
            continue

        tc_cmds = "apk add --no-cache iproute2 > /dev/null 2>&1"
        if lat and lat > 0 and bandwidth_mbit and bandwidth_mbit > 0:
            tc_cmds += f" && tc qdisc add dev eth0 root handle 1: netem delay {lat}ms"
            tc_cmds += f" && tc qdisc add dev eth0 parent 1:1 handle 10: tbf rate {bandwidth_mbit}mbit burst 256kb latency 100ms"
        elif lat and lat > 0:
            tc_cmds += f" && tc qdisc add dev eth0 root netem delay {lat}ms"
        elif bandwidth_mbit and bandwidth_mbit > 0:
            tc_cmds += f" && tc qdisc add dev eth0 root tbf rate {bandwidth_mbit}mbit burst 256kb latency 100ms"

        result = subprocess.run(
            ["docker", "exec", container, "sh", "-c", tc_cmds],
            capture_output=True, text=True,
        )
        if result.returncode == 0:
            label = f"delay={lat}ms" if lat else ""
            if bandwidth_mbit:
                label += f" bw={bandwidth_mbit}mbit"
            print(f"    p2pnode-{i + 1}: {label.strip()}")
            success += 1
        else:
            err = (result.stderr + result.stdout).strip()
            print(f"    {YELLOW}p2pnode-{i + 1}: FAILED — {err}{NC}")

    if success == 0:
        print(f"  {YELLOW}WARNING: tc netem failed on all nodes. "
              f"Latency simulation will not take effect.{NC}")
        print(f"  {YELLOW}Check that the host kernel has sch_netem loaded: "
              f"modprobe sch_netem{NC}")
        return

    print(f"  Network shaping applied to {success}/{n_nodes} nodes")

    # Verify: show actual qdisc on one node and run a ping test
    verify_container = f"{project_name}-p2pnode-1-1"
    qdisc_result = subprocess.run(
        ["docker", "exec", verify_container, "tc", "qdisc", "show", "dev", "eth0"],
        capture_output=True, text=True,
    )
    print(f"  Verify qdisc on p2pnode-1: {qdisc_result.stdout.strip()}")

    if n_nodes >= 2:
        target_ip = f"172.28.0.{13}"  # p2pnode-2
        ping_result = subprocess.run(
            ["docker", "exec", verify_container, "sh", "-c",
             f"apk add --no-cache iputils > /dev/null 2>&1; "
             f"ping -c 3 -W 5 {target_ip} 2>&1 | tail -1"],
            capture_output=True, text=True, timeout=30,
        )
        print(f"  Ping p2pnode-1 → p2pnode-2: {ping_result.stdout.strip()}")
    print()


def docker_compose_down(compose_file: str, project_name: str):
    print(f"\n  Stopping cluster: {project_name} ...")
    subprocess.run(
        ["docker", "compose", "-f", compose_file, "-p", project_name, "down", "-v", "--remove-orphans"],
        check=False,
    )
    subprocess.run(["docker", "network", "prune", "-f"], check=False, capture_output=True)
    time.sleep(5)


# ─── Client binary helpers ───────────────────────────────────────────────────

def ensure_client_binaries() -> str:
    client_dir = os.path.join(SETUP_GUIDE, "grpc_p2p_client")
    p2p_client = os.path.join(client_dir, "p2p-client")
    multi_sub = os.path.join(client_dir, "p2p-multi-subscribe")

    if os.path.isfile(p2p_client) and os.path.isfile(multi_sub):
        return client_dir

    print("Building gRPC client binaries...")
    subprocess.run(["make", "build"], cwd=SETUP_GUIDE, check=True)
    return client_dir


def ensure_identity() -> str:
    identity_dir = os.path.join(SETUP_GUIDE, "identity")
    key_file = os.path.join(identity_dir, "p2p.key")

    if os.path.isfile(key_file):
        return identity_dir

    print("Generating P2P identity...")
    os.makedirs(identity_dir, exist_ok=True)
    subprocess.run(
        ["go", "run", "generate_p2p_key.go"],
        cwd=os.path.join(SETUP_GUIDE, "keygen"),
        check=True,
    )
    return identity_dir


def get_bootstrap_peer_id() -> str:
    """Extract the real peer ID from the generated identity key file."""
    key_file = os.path.join(SETUP_GUIDE, "identity", "p2p.key")
    if os.path.isfile(key_file):
        with open(key_file, "rb") as f:
            raw = f.read()
        text = raw.decode("utf-8", errors="ignore")
        import re
        m = re.search(r'"ID"\s*:\s*"([^"]+)"', text)
        if m:
            peer_id = m.group(1)
            print(f"  Bootstrap peer ID (from identity): {peer_id}")
            return peer_id

    return "12D3KooWD5RtEPmMR9Yb2ku5VuxqK7Yj1Y5Gv8DmffJ6Ei8maU44"


def write_ips_file(n_nodes: int, outdir: str) -> str:
    path = os.path.join(outdir, "ips.txt")
    with open(path, "w") as f:
        for i in range(n_nodes):
            f.write(f"127.0.0.1:{33221 + i}\n")
    return path


# ─── Phase runners ───────────────────────────────────────────────────────────

def run_direct_phase(
    n_nodes: int,
    msg_size: int,
    count: int,
    outdir: str,
) -> LatencyResult:
    print(f"\n{'#'*60}")
    print(f"  Phase: libp2p Direct (transport baseline)")
    print(f"{'#'*60}")

    tsv_path = os.path.join(outdir, "direct.tsv")
    bench_script = os.path.join(DIRECT_BENCH, "run_direct_bench.sh")

    if not os.path.isfile(bench_script):
        print("  [SKIP] libp2p-direct benchmark not found, using placeholder")
        return LatencyResult("libp2p-direct", 0, 0, 0, 0, 0, 0, 0, [])

    try:
        subprocess.run(
            ["bash", bench_script, str(n_nodes), str(msg_size), str(count), "500", tsv_path],
            check=True,
            timeout=300,
        )
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired, FileNotFoundError) as e:
        print(f"  [WARN] Direct benchmark failed: {e}")
        return LatencyResult("libp2p-direct", 0, 0, 0, 0, 0, 0, 0, [])

    return parse_direct_tsv(tsv_path)


def run_p2p_phase(
    protocol: str,
    n_nodes: int,
    msg_size: int,
    count: int,
    compose_file: str,
    client_dir: str,
    ips_file: str,
    outdir: str,
    latencies: list[int] | None = None,
    bandwidth_mbit: int | None = None,
) -> LatencyResult:
    label = "mump2p (RLNC)" if protocol == "optimum" else "GossipSub"
    print(f"\n{'#'*60}")
    print(f"  Phase: {label}")
    print(f"{'#'*60}")

    project = f"bench-{protocol}"
    topic = f"bench-{protocol}-{int(time.time())}"
    trace_out = os.path.join(outdir, f"trace-{protocol}.tsv")

    # Copy identity into the compose working directory
    identity_src = os.path.join(SETUP_GUIDE, "identity")
    compose_dir = os.path.dirname(compose_file)
    identity_dst = os.path.join(compose_dir, "identity")
    if os.path.isdir(identity_src) and not os.path.isdir(identity_dst):
        shutil.copytree(identity_src, identity_dst)

    peer_id = get_bootstrap_peer_id()
    env_dst = os.path.join(compose_dir, ".env")
    with open(env_dst, "w") as f:
        f.write(f"BOOTSTRAP_PEER_ID={peer_id}\n")
        f.write("CLUSTER_ID=docker-dev-cluster\n")
        f.write("PROXY_VERSION=v0.0.1-rc16\n")
        f.write("P2P_NODE_VERSION=v0.0.1-rc16\n")

    try:
        docker_compose_up(compose_file, project)
        apply_network_shaping(project, n_nodes, latencies, bandwidth_mbit)

        run_test_sh = os.path.join(SCRIPT_DIR, "run_test.sh")
        subprocess.run(
            [
                "bash", run_test_sh,
                protocol, str(n_nodes), str(msg_size), str(count),
                topic, client_dir, trace_out, ips_file,
            ],
            check=True,
            timeout=600,
        )
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as e:
        print(f"  [ERROR] Phase {protocol} failed: {e}")
    finally:
        docker_compose_down(compose_file, project)

    return parse_p2p_trace(trace_out, protocol, topic_filter=topic)


# ─── Modes ───────────────────────────────────────────────────────────────────

def mode_compare(args):
    ts = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    outdir = os.path.join(PROJECT_ROOT, "results", f"compare_{ts}")
    os.makedirs(outdir, exist_ok=True)

    n_nodes = args.nodes
    msg_size = parse_msg_size(args.msg_size)
    count = args.count
    latencies = resolve_latencies(args.latency, n_nodes)
    bw_list = resolve_bandwidths(args.bandwidth)
    bandwidth = bw_list[0] if bw_list else None
    peer_id = get_bootstrap_peer_id()

    tc_ok = True
    if latencies:
        tc_ok = check_tc_support()
        if not tc_ok:
            print(f"\n{YELLOW}{'!'*60}")
            print(f"  WARNING: tc netem is NOT supported on this host.")
            print(f"  Possible causes: missing sch_netem kernel module,")
            print(f"  or macOS Docker Desktop Rosetta emulation.")
            print(f"  Try: modprobe sch_netem (Linux) or run on native Linux.")
            print(f"  --latency {args.latency} will be IGNORED — running without delay.")
            print(f"{'!'*60}{NC}\n")
            latencies = None

    print(f"\n{'='*60}")
    print(f"  Optimum Benchmark — 3-Way Compare")
    print(f"  Nodes: {n_nodes}  MsgSize: {msg_size}B  Count: {count}")
    bw_label = f"{bandwidth} mbit" if bandwidth else "unlimited"
    lat_label = args.latency if tc_ok else f"{args.latency} (SKIPPED — tc unsupported)"
    print(f"  Latency: {lat_label}  Bandwidth: {bw_label}")
    print(f"  Output: {outdir}")
    print(f"{'='*60}")

    ensure_identity()
    client_dir = ensure_client_binaries()
    ips_file = write_ips_file(n_nodes, outdir)

    results: list[LatencyResult] = []

    # Phase 1: libp2p Direct
    r_direct = run_direct_phase(n_nodes, msg_size, count, outdir)
    results.append(r_direct)

    # Phase 2: GossipSub
    compose_dir = os.path.join(outdir, "gossipsub")
    os.makedirs(compose_dir, exist_ok=True)
    gs_compose = generate(
        n_nodes, "gossipsub", peer_id, compose_dir,
        latencies=latencies, bandwidth_mbit=bandwidth,
    )
    r_gs = run_p2p_phase(
        "gossipsub", n_nodes, msg_size, count,
        gs_compose, client_dir, ips_file, outdir,
        latencies=latencies, bandwidth_mbit=bandwidth,
    )
    results.append(r_gs)

    # Phase 3: mump2p (RLNC)
    compose_dir = os.path.join(outdir, "optimum")
    os.makedirs(compose_dir, exist_ok=True)
    opt_compose = generate(
        n_nodes, "optimum", peer_id, compose_dir,
        latencies=latencies, bandwidth_mbit=bandwidth,
    )
    r_opt = run_p2p_phase(
        "optimum", n_nodes, msg_size, count,
        opt_compose, client_dir, ips_file, outdir,
        latencies=latencies, bandwidth_mbit=bandwidth,
    )
    results.append(r_opt)

    # ─── Summary ────────────────────────────────────────────────────────────
    print(f"\n{'='*60}")
    print(f"  RESULTS  (Nodes={n_nodes}, MsgSize={msg_size}B, Latency={args.latency})")
    print(f"{'='*60}\n")
    print(format_table(results))
    print()

    md = format_markdown_table(results)
    md_path = os.path.join(outdir, "results.md")
    with open(md_path, "w") as f:
        f.write(f"# Benchmark Results\n\n")
        f.write(f"- **Date**: {ts}\n")
        f.write(f"- **Nodes**: {n_nodes}\n")
        f.write(f"- **Message Size**: {msg_size} bytes\n")
        f.write(f"- **Count**: {count}\n")
        f.write(f"- **Latency**: {args.latency}\n")
        f.write(f"- **Bandwidth**: {bandwidth or 'unlimited'} mbit\n\n")
        f.write(md)
        f.write("\n")
    print(f"  Markdown report: {md_path}")

    summary = {
        "timestamp": ts,
        "nodes": n_nodes,
        "msg_size_bytes": msg_size,
        "count": count,
        "latency_preset": args.latency,
        "bandwidth_mbit": bandwidth,
        "results": [
            {
                "protocol": r.protocol,
                "avg_ms": round(r.avg_ms, 3),
                "min_ms": round(r.min_ms, 3),
                "max_ms": round(r.max_ms, 3),
                "p50_ms": round(r.p50_ms, 3),
                "p95_ms": round(r.p95_ms, 3),
                "p99_ms": round(r.p99_ms, 3),
                "count": r.count,
            }
            for r in results
        ],
    }
    json_path = os.path.join(outdir, "results.json")
    with open(json_path, "w") as f:
        json.dump(summary, f, indent=2)
    print(f"  JSON report:     {json_path}\n")


def mode_sweep(args):
    ts = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    outdir = os.path.join(PROJECT_ROOT, "results", f"sweep_{ts}")
    os.makedirs(outdir, exist_ok=True)

    n_nodes = args.nodes
    msg_size = parse_msg_size(args.msg_size)
    count = args.count
    peer_id = get_bootstrap_peer_id()
    shard_factors = [2, 4, 6, 8]
    latency_preset = args.latency or "wan"
    latencies = resolve_latencies(latency_preset, n_nodes)
    bw_list = resolve_bandwidths(args.bandwidth)
    bandwidth = bw_list[0] if bw_list else None

    if latencies and not check_tc_support():
        print(f"\n{YELLOW}{'!'*60}")
        print(f"  WARNING: tc netem is NOT supported on this host.")
        print(f"  Try: modprobe sch_netem (Linux) or run on native Linux.")
        print(f"  --latency {latency_preset} will be IGNORED.")
        print(f"{'!'*60}{NC}\n")
        latencies = None

    print(f"\n{'='*60}")
    print(f"  Optimum Benchmark — Shard Factor Sweep")
    print(f"  Nodes: {n_nodes}  Latency: {latency_preset}")
    print(f"  Shard factors: {shard_factors}")
    print(f"  Output: {outdir}")
    print(f"{'='*60}")

    ensure_identity()
    client_dir = ensure_client_binaries()
    ips_file = write_ips_file(n_nodes, outdir)

    all_results = []

    for sf in shard_factors:
        print(f"\n  ═══ Shard factor = {sf} ═══")
        compose_dir = os.path.join(outdir, f"sf{sf}")
        os.makedirs(compose_dir, exist_ok=True)

        opt_compose = generate(
            n_nodes, "optimum", peer_id, compose_dir,
            latencies=latencies, bandwidth_mbit=bandwidth,
            shard_factor=sf,
        )
        result = run_p2p_phase(
            "optimum", n_nodes, msg_size, count,
            opt_compose, client_dir, ips_file, outdir,
            latencies=latencies, bandwidth_mbit=bandwidth,
        )

        labeled = LatencyResult(
            protocol=f"mump2p (sf={sf})",
            avg_ms=result.avg_ms,
            min_ms=result.min_ms,
            max_ms=result.max_ms,
            p50_ms=result.p50_ms,
            p95_ms=result.p95_ms,
            p99_ms=result.p99_ms,
            count=result.count,
            raw_ms=result.raw_ms,
        )
        all_results.append(labeled)

    print(f"\n{'='*60}")
    print(f"  SWEEP RESULTS  (Latency={latency_preset})")
    print(f"{'='*60}\n")
    print(format_table(all_results))

    summary = {
        "timestamp": ts,
        "mode": "sweep",
        "nodes": n_nodes,
        "msg_size_bytes": msg_size,
        "latency_preset": latency_preset,
        "shard_factors": shard_factors,
        "results": [
            {
                "shard_factor": sf,
                "avg_ms": round(r.avg_ms, 3),
                "min_ms": round(r.min_ms, 3),
                "max_ms": round(r.max_ms, 3),
                "p50_ms": round(r.p50_ms, 3),
                "p95_ms": round(r.p95_ms, 3),
                "count": r.count,
            }
            for sf, r in zip(shard_factors, all_results)
        ],
    }
    json_path = os.path.join(outdir, "sweep_results.json")
    with open(json_path, "w") as f:
        json.dump(summary, f, indent=2)
    print(f"\n  JSON report: {json_path}\n")


# ─── CLI ─────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="Optimum Benchmark — protocol performance comparison",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python3 benchmark.py --mode compare
  python3 benchmark.py --mode compare --latency wan
  python3 benchmark.py --mode compare --nodes 20 --latency 20,50,100 --bandwidth 1000
  python3 benchmark.py --mode sweep --latency wan
        """,
    )

    parser.add_argument(
        "--mode", choices=["compare", "sweep"], default="compare",
        help="Benchmark mode (default: compare)",
    )
    parser.add_argument(
        "--nodes", type=int, default=DEFAULT_NODES,
        help=f"Total P2P nodes — 1 publisher + (N-1) receivers (default: {DEFAULT_NODES})",
    )
    parser.add_argument(
        "--msg-size", default=DEFAULT_MSG_SIZE,
        help=f"Message payload size, e.g. 100kb, 900kb (default: {DEFAULT_MSG_SIZE}, max ~1MB)",
    )
    parser.add_argument(
        "--count", type=int, default=DEFAULT_COUNT,
        help=f"Messages per test (default: {DEFAULT_COUNT})",
    )
    parser.add_argument(
        "--latency", default="off",
        help="Latency preset (off/wan/high/extreme) or comma-separated ms values (default: off)",
    )
    parser.add_argument(
        "--bandwidth", default=None,
        help="Per-node bandwidth cap in mbit, comma-separated (default: unlimited)",
    )

    args = parser.parse_args()

    if args.mode == "compare":
        mode_compare(args)
    elif args.mode == "sweep":
        mode_sweep(args)


if __name__ == "__main__":
    main()
