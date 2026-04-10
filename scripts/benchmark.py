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
import hashlib
import json
import os
import secrets
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
from generate_compose import BASE_IP_OCTET, DEFAULT_LAN_SECOND_OCTET, generate
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

# p2pnode rc16 + tc netem: payloads above this often never show up in GossipSub/mump2p traces.
P2P_WAN_SAFE_MSG_BYTES = 32 * 1024
# Default netem limit=1000 packets often tail-drops bursty pubsub under delay; raise for benchmarks.
NETEM_PACKET_LIMIT = "50000"

P2PNODE_IMAGE = "getoptimum/p2pnode:v0.0.1-rc16"

YELLOW = "\033[1;33m"
NC = "\033[0m"

_TC_SBIN_PATHS = ["/sbin/tc", "/usr/sbin/tc", "/usr/local/sbin/tc"]
_NSENTER_PATHS = ["/usr/bin/nsenter", "/usr/sbin/nsenter", "/sbin/nsenter"]


def _find_bin(name: str, extra_paths: list[str] | None = None) -> str | None:
    """Find an executable by name, checking PATH and common sbin directories."""
    found = shutil.which(name)
    if found:
        return found
    for p in (extra_paths or []):
        if os.path.isfile(p) and os.access(p, os.X_OK):
            return p
    return None


def _image_has_tc(image: str = P2PNODE_IMAGE) -> bool:
    """Check whether a Docker image already contains the tc binary."""
    r = subprocess.run(
        ["docker", "run", "--rm", "--entrypoint=",
         image, "sh", "-c", "command -v tc"],
        capture_output=True, text=True, timeout=30,
    )
    return r.returncode == 0 and r.stdout.strip() != ""


def check_tc_support() -> bool:
    """Detect whether tc netem works inside Docker containers on this host.

    First tries the host tc + nsenter approach (most reliable on Linux).
    Falls back to testing the container's own tc.
    """
    tc_path = _find_bin("tc", _TC_SBIN_PATHS)
    nsenter_path = _find_bin("nsenter", _NSENTER_PATHS)

    # Strategy 1: nsenter + host tc  (does NOT need tc inside the container)
    if tc_path and nsenter_path:
        try:
            run_result = subprocess.run(
                ["docker", "run", "-d", "--rm", "--cap-add", "NET_ADMIN",
                 P2PNODE_IMAGE, "sleep", "30"],
                capture_output=True, text=True, timeout=30,
            )
            if run_result.returncode != 0:
                return False
            cid = run_result.stdout.strip()
            try:
                pid_r = subprocess.run(
                    ["docker", "inspect", "-f", "{{.State.Pid}}", cid],
                    capture_output=True, text=True, timeout=10,
                )
                pid = pid_r.stdout.strip()
                subprocess.run(["modprobe", "sch_netem"],
                               capture_output=True, timeout=10)
                tc_r = subprocess.run(
                    [nsenter_path, "-t", pid, "-n", tc_path,
                     "qdisc", "add", "dev", "eth0", "root", "netem", "delay", "1ms"],
                    capture_output=True, text=True, timeout=10,
                )
                output = tc_r.stdout + tc_r.stderr
                if tc_r.returncode == 0 and "Not supported" not in output:
                    return True
            finally:
                subprocess.run(["docker", "rm", "-f", cid],
                               capture_output=True, timeout=10)
        except Exception:
            pass

    # Strategy 2: docker exec + container tc  (needs tc in the image)
    has_tc = False
    try:
        has_tc = _image_has_tc()
    except Exception:
        pass

    install_cmd = ""
    if not has_tc:
        install_cmd = "apk add --no-cache iproute2 > /dev/null 2>&1 && "

    try:
        result = subprocess.run(
            [
                "docker", "run", "--rm", "--cap-add", "NET_ADMIN",
                "--entrypoint=", P2PNODE_IMAGE,
                "sh", "-c",
                f"{install_cmd}"
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


def _netem_delay_tokens(lat_ms: int) -> list[str]:
    return ["netem", "delay", f"{lat_ms}ms", "limit", NETEM_PACKET_LIMIT]


def p2p_subprocess_env(n_nodes: int, latencies: list[int] | None) -> dict[str, str]:
    """Extra env for run_test.sh when tc latency is on (longer mesh/settle; user can override)."""
    env = os.environ.copy()
    if not latencies:
        return env
    # Default per-round wait is 8+N*3; under WAN use at least 12+N*5 so large frames clear the mesh.
    lan_wait = 8 + n_nodes * 3
    wan_wait = 12 + n_nodes * 5
    env.setdefault("BENCH_PER_ROUND_WAIT_SECS", str(max(lan_wait, wan_wait)))
    env.setdefault("BENCH_MESH_STABILIZE_SECS", "28")
    env.setdefault("BENCH_TOPIC_GRAFT_WAIT_SECS", "28")
    return env


def apply_wan_p2p_msg_cap(msg_size: int, latencies: list[int] | None) -> int:
    """Shrink payload when emulating WAN so Docker GossipSub/mump2p phases can deliver."""
    if not latencies or msg_size <= P2P_WAN_SAFE_MSG_BYTES:
        return msg_size
    print(
        f"{YELLOW}  Auto-capping MsgSize {msg_size}B → {P2P_WAN_SAFE_MSG_BYTES}B for WAN emulation "
        f"(p2pnode rc16: larger payloads + netem typically yield empty GossipSub/mump2p traces).{NC}\n"
    )
    return P2P_WAN_SAFE_MSG_BYTES


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


def pick_docker_lan_second_octet(outdir: str) -> int:
    """Deterministic 172.x.0.0/16 from path (for reproducible local runs)."""
    digest = hashlib.sha256(os.path.abspath(outdir).encode()).hexdigest()
    h = int(digest[:8], 16)
    return 16 + (h % 239)


def pick_random_docker_lan_octet() -> int:
    """Random 172.x.0.0/16 per phase to avoid Docker 'pool overlaps' vs stale/other networks."""
    return 16 + secrets.randbelow(239)


# ─── Docker helpers ──────────────────────────────────────────────────────────

def docker_cleanup_bench_leftovers() -> None:
    """Remove containers matching name=bench-* and prune unused networks.

    Same intent as:
      docker ps -a --filter name=bench- -q | xargs -r docker rm -f
      docker network prune -f
    Implemented in Python so macOS (no ``xargs -r``) and Linux both work.
    """
    try:
        ls = subprocess.run(
            ["docker", "ps", "-a", "--filter", "name=bench-", "-q"],
            capture_output=True,
            text=True,
            timeout=60,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return
    if ls.returncode != 0:
        return
    ids = [line.strip() for line in ls.stdout.splitlines() if line.strip()]
    removed = 0
    if ids:
        rm = subprocess.run(
            ["docker", "rm", "-f", *ids],
            capture_output=True,
            text=True,
            timeout=180,
        )
        if rm.returncode == 0:
            removed = len(ids)
        else:
            err = (rm.stderr or rm.stdout or "").strip()
            print(f"  {YELLOW}Docker cleanup: docker rm -f failed ({err}){NC}")
    subprocess.run(
        ["docker", "network", "prune", "-f"],
        capture_output=True,
        text=True,
        timeout=60,
    )
    if removed:
        print(f"  Docker cleanup: removed {removed} bench-* container(s); pruned unused networks")
    else:
        print("  Docker cleanup: pruned unused Docker networks (no bench-* containers found)")


def docker_compose_up(compose_file: str, project_name: str):
    print(f"\n{'='*60}")
    print(f"  Starting cluster: {project_name}")
    print(f"  Compose: {compose_file}")
    print(f"{'='*60}\n")
    # Tear down same project first so stale networks/containers cannot block `up`.
    subprocess.run(
        [
            "docker", "compose", "-f", compose_file, "-p", project_name,
            "down", "-v", "--remove-orphans",
        ],
        check=False,
        capture_output=True,
    )
    subprocess.run(
        ["docker", "compose", "-f", compose_file, "-p", project_name, "pull"],
        check=False,
    )
    subprocess.run(
        [
            "docker", "compose", "-f", compose_file, "-p", project_name,
            "up", "-d", "--force-recreate",
        ],
        check=True,
    )


def _ensure_sch_netem():
    """Try to load the sch_netem kernel module on the host (requires root)."""
    result = subprocess.run(
        ["modprobe", "sch_netem"],
        capture_output=True, text=True,
    )
    if result.returncode == 0:
        print("  Loaded kernel module: sch_netem")
    else:
        print(f"  {YELLOW}modprobe sch_netem failed: {result.stderr.strip()}")
        print(f"  tc netem delay may not work without this kernel module.{NC}")


def _tc_delete_root_nsenter(
    container: str, nsenter_bin: str, tc_bin: str,
) -> None:
    """Remove existing root qdisc so a fresh netem rule can be added (avoids EEXIST)."""
    pid = _get_container_pid(container)
    if not pid:
        return
    subprocess.run(
        [nsenter_bin, "-t", pid, "-n", tc_bin,
         "qdisc", "del", "dev", "eth0", "root"],
        capture_output=True, text=True,
    )


def _get_container_pid(container: str) -> str | None:
    result = subprocess.run(
        ["docker", "inspect", "--format", "{{.State.Pid}}", container],
        capture_output=True, text=True,
    )
    pid = result.stdout.strip()
    return pid if result.returncode == 0 and pid and pid != "0" else None


def _run_tc_cmd(cmd: list[str], label: str = "") -> bool:
    """Run a tc command, print stderr on failure, return success."""
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode != 0:
        err = (r.stderr + r.stdout).strip()
        prefix = f"    [{label}] " if label else "    "
        print(f"{prefix}{YELLOW}tc failed (rc={r.returncode}): {err}{NC}")
        return False
    return True


def _apply_tc_via_nsenter(
    container: str, lat: int | None, bandwidth_mbit: int | None,
    nsenter_bin: str = "nsenter", tc_bin: str = "tc",
) -> bool:
    """Apply tc rules using host's tc via nsenter into container's network namespace."""
    pid = _get_container_pid(container)
    if not pid:
        print(f"    {YELLOW}Cannot get PID for {container}{NC}")
        return False

    base = [nsenter_bin, "-t", pid, "-n", tc_bin]
    delay_tc = _netem_delay_tokens(lat) if lat and lat > 0 else []
    if lat and lat > 0 and bandwidth_mbit and bandwidth_mbit > 0:
        _tc_delete_root_nsenter(container, nsenter_bin, tc_bin)
        if not _run_tc_cmd(
            base + ["qdisc", "add", "dev", "eth0", "root", "handle", "1:"] + delay_tc,
            container,
        ):
            if not _run_tc_cmd(
                base + ["qdisc", "replace", "dev", "eth0", "root", "handle", "1:"] + delay_tc,
                container,
            ):
                return False
        return _run_tc_cmd(
            base + ["qdisc", "add", "dev", "eth0",
                    "parent", "1:1", "handle", "10:", "tbf",
                    "rate", f"{bandwidth_mbit}mbit", "burst", "256kb", "latency", "100ms"],
            container,
        )
    elif lat and lat > 0:
        # replace swaps the root qdisc atomically (avoids RTNETLINK File exists after failed del).
        if _run_tc_cmd(
            base + ["qdisc", "replace", "dev", "eth0", "root"] + delay_tc,
            container,
        ):
            return True
        _tc_delete_root_nsenter(container, nsenter_bin, tc_bin)
        return _run_tc_cmd(
            base + ["qdisc", "add", "dev", "eth0", "root"] + delay_tc,
            container,
        )
    elif bandwidth_mbit and bandwidth_mbit > 0:
        if _run_tc_cmd(
            base + ["qdisc", "replace", "dev", "eth0", "root", "tbf",
                    "rate", f"{bandwidth_mbit}mbit", "burst", "256kb", "latency", "100ms"],
            container,
        ):
            return True
        _tc_delete_root_nsenter(container, nsenter_bin, tc_bin)
        cmd = base + ["qdisc", "add", "dev", "eth0", "root", "tbf",
                      "rate", f"{bandwidth_mbit}mbit", "burst", "256kb", "latency", "100ms"]
        return _run_tc_cmd(cmd, container)
    else:
        return True


def _container_has_tc(container: str) -> bool:
    """Check whether a running container already has tc installed."""
    r = subprocess.run(
        ["docker", "exec", container, "sh", "-c", "command -v tc"],
        capture_output=True, text=True, timeout=10,
    )
    return r.returncode == 0 and r.stdout.strip() != ""


def _apply_tc_via_docker_exec(container: str, lat: int | None, bandwidth_mbit: int | None) -> bool:
    """Fallback: apply tc rules inside the container using its own tc binary."""
    if not _container_has_tc(container):
        install = subprocess.run(
            ["docker", "exec", container, "sh", "-c",
             "apk add --no-cache iproute2 2>&1"],
            capture_output=True, text=True, timeout=60,
        )
        if install.returncode != 0:
            print(f"    {YELLOW}apk add iproute2 failed in {container}: "
                  f"{install.stderr.strip()}{NC}")
            return False
        if not _container_has_tc(container):
            print(f"    {YELLOW}tc still missing after iproute2 install in {container}{NC}")
            return False

    reset = "tc qdisc del dev eth0 root 2>/dev/null || true; "
    if lat and lat > 0 and bandwidth_mbit and bandwidth_mbit > 0:
        netem_d = f"netem delay {lat}ms limit {NETEM_PACKET_LIMIT}"
        tc_cmds = (
            reset
            + f"tc qdisc add dev eth0 root handle 1: {netem_d}"
            f" || tc qdisc replace dev eth0 root handle 1: {netem_d}"
            f" && tc qdisc add dev eth0 parent 1:1 handle 10: tbf"
            f" rate {bandwidth_mbit}mbit burst 256kb latency 100ms"
        )
    elif lat and lat > 0:
        netem_d = f"netem delay {lat}ms limit {NETEM_PACKET_LIMIT}"
        tc_cmds = (
            f"tc qdisc replace dev eth0 root {netem_d} || "
            f"({reset}tc qdisc add dev eth0 root {netem_d})"
        )
    elif bandwidth_mbit and bandwidth_mbit > 0:
        tc_cmds = (
            f"tc qdisc replace dev eth0 root tbf rate {bandwidth_mbit}mbit burst 256kb latency 100ms || "
            f"({reset}tc qdisc add dev eth0 root tbf rate {bandwidth_mbit}mbit burst 256kb latency 100ms)"
        )
    else:
        return True

    r = subprocess.run(
        ["docker", "exec", container, "sh", "-c", tc_cmds + " 2>&1"],
        capture_output=True, text=True,
    )
    if r.returncode != 0:
        err = (r.stdout + r.stderr).strip()
        print(f"    {YELLOW}docker exec tc failed in {container} (rc={r.returncode}): {err}{NC}")
        return False

    verify = subprocess.run(
        ["docker", "exec", container, "tc", "qdisc", "show", "dev", "eth0"],
        capture_output=True, text=True,
    )
    if lat and lat > 0 and "delay" not in verify.stdout:
        print(f"    {YELLOW}tc returned 0 but delay not in qdisc for {container}: "
              f"{verify.stdout.strip()}{NC}")
        return False

    return True


def apply_network_shaping(
    project_name: str,
    n_nodes: int,
    latencies: list[int] | None,
    bandwidth_mbit: int | None,
    lan_second_octet: int = DEFAULT_LAN_SECOND_OCTET,
):
    """Apply tc netem rules to running containers.

    Prefers nsenter (host tc binary, avoids version mismatch with kernel).
    Falls back to docker exec (container tc) if nsenter is unavailable.
    """
    if not latencies and not bandwidth_mbit:
        return

    print(f"\n  Applying network shaping (tc netem)...")
    _ensure_sch_netem()

    tc_path = _find_bin("tc", _TC_SBIN_PATHS)
    nsenter_path = _find_bin("nsenter", _NSENTER_PATHS)
    use_nsenter = tc_path is not None and nsenter_path is not None

    if use_nsenter:
        print(f"  Strategy: nsenter + host tc ({tc_path}, {nsenter_path})")
    else:
        missing = []
        if not tc_path:
            missing.append("tc")
        if not nsenter_path:
            missing.append("nsenter")
        print(f"  Strategy: docker exec + container tc "
              f"(host missing: {', '.join(missing) if missing else 'none'})")
        print(f"  {YELLOW}TIP: Install iproute2 on the host for reliable tc netem:")
        print(f"    RHEL/CentOS/Amazon Linux: yum install -y iproute iproute-tc")
        print(f"    Debian/Ubuntu:            apt install -y iproute2")
        print(f"    Alpine:                   apk add iproute2{NC}")

    success = 0
    for i in range(n_nodes):
        container = f"{project_name}-p2pnode-{i + 1}-1"
        lat = latencies[i % len(latencies)] if latencies else None

        needs_tc = (lat is not None and lat > 0) or (
            bandwidth_mbit is not None and bandwidth_mbit > 0
        )
        if not needs_tc:
            continue

        if use_nsenter:
            ok = _apply_tc_via_nsenter(container, lat, bandwidth_mbit,
                                          nsenter_bin=nsenter_path, tc_bin=tc_path)
        else:
            ok = _apply_tc_via_docker_exec(container, lat, bandwidth_mbit)

        label = f"delay={lat}ms" if lat else ""
        if bandwidth_mbit:
            label += f" bw={bandwidth_mbit}mbit"

        if ok and use_nsenter and lat and lat > 0:
            pid = _get_container_pid(container)
            if pid:
                v = subprocess.run(
                    [nsenter_path, "-t", pid, "-n", tc_path, "qdisc", "show", "dev", "eth0"],
                    capture_output=True, text=True,
                )
                if "delay" not in v.stdout:
                    print(f"    {YELLOW}p2pnode-{i + 1}: tc returned 0 but qdisc has no delay "
                          f"[{v.stdout.strip()}]{NC}")
                    ok = False

        if ok:
            print(f"    p2pnode-{i + 1}: {label.strip()}")
            success += 1
        else:
            print(f"    {YELLOW}p2pnode-{i + 1}: FAILED{NC}")

    if success == 0:
        print(f"  {YELLOW}WARNING: tc netem failed on all nodes.{NC}")
        return

    print(f"  Network shaping applied to {success}/{n_nodes} nodes")

    first_container = f"{project_name}-p2pnode-1-1"
    pid = _get_container_pid(first_container)
    if use_nsenter and pid:
        qdisc_result = subprocess.run(
            [nsenter_path, "-t", pid, "-n", tc_path, "qdisc", "show", "dev", "eth0"],
            capture_output=True, text=True,
        )
        qdisc_out = qdisc_result.stdout.strip()
    else:
        qdisc_result = subprocess.run(
            ["docker", "exec", first_container, "tc", "qdisc", "show", "dev", "eth0"],
            capture_output=True, text=True,
        )
        qdisc_out = qdisc_result.stdout.strip()
    print(f"  Verify qdisc on p2pnode-1: {qdisc_out}")

    if "delay" not in qdisc_out:
        print(f"  {YELLOW}WARNING: delay parameter missing in qdisc!{NC}")

    if n_nodes >= 2:
        # p2pnode-2 is index 1 → BASE_IP_OCTET + 1
        target_ip = f"172.{lan_second_octet}.0.{BASE_IP_OCTET + 1}"
        ping_result = subprocess.run(
            ["docker", "exec", first_container, "sh", "-c",
             f"apk add --no-cache iputils > /dev/null 2>&1; "
             f"ping -c 3 -W 5 {target_ip} 2>&1 | tail -1"],
            capture_output=True, text=True, timeout=30,
        )
        ping_out = ping_result.stdout.strip()
        print(f"  Ping p2pnode-1 → p2pnode-2: {ping_out}")

        try:
            avg_str = ping_out.split("/")[4] if "/" in ping_out else "0"
            avg_rtt = float(avg_str)
            if avg_rtt < 10.0:
                print(f"  {YELLOW}WARNING: RTT {avg_rtt:.1f}ms too low — tc delay not effective.{NC}")
            else:
                print(f"  tc delay confirmed working (RTT {avg_rtt:.1f}ms)")
        except (ValueError, IndexError):
            pass
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

def _apply_vendor_grpc_client_patch() -> None:
    """Overlay patched shared/utils.go so clones work without unpublished submodule commits."""
    patch_src = os.path.join(
        PROJECT_ROOT, "vendor-patches", "optimum-dev-setup-guide",
        "grpc_p2p_client", "shared", "utils.go",
    )
    patch_dst = os.path.join(SETUP_GUIDE, "grpc_p2p_client", "shared", "utils.go")
    if not os.path.isfile(patch_src) or not os.path.isdir(os.path.dirname(patch_dst)):
        return
    shutil.copy2(patch_src, patch_dst)


def ensure_client_binaries() -> str:
    client_dir = os.path.join(SETUP_GUIDE, "grpc_p2p_client")
    p2p_client = os.path.join(client_dir, "p2p-client")
    multi_sub = os.path.join(client_dir, "p2p-multi-subscribe")

    _apply_vendor_grpc_client_patch()

    patch_src = os.path.join(
        PROJECT_ROOT, "vendor-patches", "optimum-dev-setup-guide",
        "grpc_p2p_client", "shared", "utils.go",
    )
    stale = False
    if os.path.isfile(patch_src) and os.path.isfile(multi_sub):
        if os.path.getmtime(patch_src) > os.path.getmtime(multi_sub):
            stale = True

    if os.path.isfile(p2p_client) and os.path.isfile(multi_sub) and not stale:
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
    lan_second_octet: int = DEFAULT_LAN_SECOND_OCTET,
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
        apply_network_shaping(
            project, n_nodes, latencies, bandwidth_mbit,
            lan_second_octet=lan_second_octet,
        )

        run_test_sh = os.path.join(SCRIPT_DIR, "run_test.sh")
        # Per-round settle + health/mesh waits — allow headroom (WAN uses longer defaults).
        run_timeout = max(900, 300 + count * 50)
        if latencies:
            run_timeout = max(1400, 420 + count * 95)
        subprocess.run(
            [
                "bash", run_test_sh,
                protocol, str(n_nodes), str(msg_size), str(count),
                topic, client_dir, trace_out, ips_file,
            ],
            check=True,
            timeout=run_timeout,
            env=p2p_subprocess_env(n_nodes, latencies),
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

    msg_size = apply_wan_p2p_msg_cap(msg_size, latencies)

    print(f"\n{'='*60}")
    print(f"  Optimum Benchmark — 3-Way Compare")
    print(f"  Nodes: {n_nodes}  MsgSize: {msg_size}B  Count: {count}")
    bw_label = f"{bandwidth} mbit" if bandwidth else "unlimited"
    lat_label = args.latency if tc_ok else f"{args.latency} (SKIPPED — tc unsupported)"
    print(f"  Latency: {lat_label}  Bandwidth: {bw_label}")
    print(f"  Output: {outdir}")
    print(f"{'='*60}")

    docker_cleanup_bench_leftovers()
    ensure_identity()
    client_dir = ensure_client_binaries()
    ips_file = write_ips_file(n_nodes, outdir)

    results: list[LatencyResult] = []

    # Phase 1: libp2p Direct
    r_direct = run_direct_phase(n_nodes, msg_size, count, outdir)
    results.append(r_direct)

    # Phase 2: GossipSub (random /16 per phase avoids Docker address-pool clashes)
    compose_dir = os.path.join(outdir, "gossipsub")
    os.makedirs(compose_dir, exist_ok=True)
    lan_gs = pick_random_docker_lan_octet()
    print(f"  Docker bench LAN (gossipsub): 172.{lan_gs}.0.0/16")
    gs_compose = generate(
        n_nodes, "gossipsub", peer_id, compose_dir,
        latencies=latencies, bandwidth_mbit=bandwidth,
        lan_second_octet=lan_gs,
    )
    r_gs = run_p2p_phase(
        "gossipsub", n_nodes, msg_size, count,
        gs_compose, client_dir, ips_file, outdir,
        latencies=latencies, bandwidth_mbit=bandwidth,
        lan_second_octet=lan_gs,
    )
    results.append(r_gs)

    # Phase 3: mump2p (RLNC)
    compose_dir = os.path.join(outdir, "optimum")
    os.makedirs(compose_dir, exist_ok=True)
    lan_opt = pick_random_docker_lan_octet()
    print(f"  Docker bench LAN (optimum): 172.{lan_opt}.0.0/16")
    opt_compose = generate(
        n_nodes, "optimum", peer_id, compose_dir,
        latencies=latencies, bandwidth_mbit=bandwidth,
        lan_second_octet=lan_opt,
    )
    r_opt = run_p2p_phase(
        "optimum", n_nodes, msg_size, count,
        opt_compose, client_dir, ips_file, outdir,
        latencies=latencies, bandwidth_mbit=bandwidth,
        lan_second_octet=lan_opt,
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

    msg_size = apply_wan_p2p_msg_cap(msg_size, latencies)

    print(f"\n{'='*60}")
    print(f"  Optimum Benchmark — Shard Factor Sweep")
    print(f"  Nodes: {n_nodes}  Latency: {latency_preset}")
    print(f"  Shard factors: {shard_factors}")
    print(f"  Output: {outdir}")
    print(f"{'='*60}")

    docker_cleanup_bench_leftovers()
    ensure_identity()
    client_dir = ensure_client_binaries()
    ips_file = write_ips_file(n_nodes, outdir)

    all_results = []

    for sf in shard_factors:
        print(f"\n  ═══ Shard factor = {sf} ═══")
        compose_dir = os.path.join(outdir, f"sf{sf}")
        os.makedirs(compose_dir, exist_ok=True)
        lan_octet = pick_random_docker_lan_octet()
        print(f"  Docker bench LAN (optimum sf={sf}): 172.{lan_octet}.0.0/16")

        opt_compose = generate(
            n_nodes, "optimum", peer_id, compose_dir,
            latencies=latencies, bandwidth_mbit=bandwidth,
            shard_factor=sf,
            lan_second_octet=lan_octet,
        )
        result = run_p2p_phase(
            "optimum", n_nodes, msg_size, count,
            opt_compose, client_dir, ips_file, outdir,
            latencies=latencies, bandwidth_mbit=bandwidth,
            lan_second_octet=lan_octet,
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
