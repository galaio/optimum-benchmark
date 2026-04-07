#!/usr/bin/env python3
"""
Dynamically generate N-node docker-compose files for GossipSub and mump2p benchmarks.

Produces:
  - docker-compose-gossipsub.yml   (NODE_MODE=gossipsub)
  - docker-compose-optimum.yml     (NODE_MODE=optimum)
"""

import argparse
import os

import yaml


SUBNET = "172.28.0.0/16"
BASE_IP_OCTET = 12
PROXY_IP_OCTET = 10
SIDECAR_PORT = 33212
API_PORT = 9090
GOSSIPSUB_PORT = 6060
OPTIMUM_PORT = 7070
HOST_SIDECAR_BASE = 33221
HOST_API_BASE = 9091
MAX_MSG_SIZE = 1048576


def node_ip(index: int) -> str:
    return f"172.28.0.{BASE_IP_OCTET + index}"


def build_p2pnode(
    index: int,
    n_nodes: int,
    mode: str,
    bootstrap_peer_id: str,
    latency_ms: int | None = None,
    bandwidth_mbit: int | None = None,
    shard_factor: int = 4,
) -> tuple[str, dict]:
    name = f"p2pnode-{index + 1}"
    ip = node_ip(index)

    env = [
        "LOG_LEVEL=debug",
        "CLUSTER_ID=${CLUSTER_ID}",
        f"SIDECAR_PORT={SIDECAR_PORT}",
        f"API_PORT={API_PORT}",
        "IDENTITY_DIR=/identity",
        f"NODE_MODE={mode}",
    ]

    mesh_target = min(6, n_nodes - 1)
    mesh_min = min(3, n_nodes - 1)
    mesh_max = min(12, n_nodes)

    if mode == "gossipsub":
        env += [
            f"GOSSIPSUB_PORT={GOSSIPSUB_PORT}",
            f"GOSSIPSUB_MAX_MSG_SIZE={MAX_MSG_SIZE}",
            f"GOSSIPSUB_MESH_TARGET={mesh_target}",
            f"GOSSIPSUB_MESH_MIN={mesh_min}",
            f"GOSSIPSUB_MESH_MAX={mesh_max}",
        ]
        if index > 0:
            env.append(
                f"BOOTSTRAP_PEERS=/ip4/{node_ip(0)}/tcp/{GOSSIPSUB_PORT}/p2p/{bootstrap_peer_id}"
            )
    else:
        env += [
            f"OPTIMUM_PORT={OPTIMUM_PORT}",
            f"OPTIMUM_MAX_MSG_SIZE={MAX_MSG_SIZE}",
            f"OPTIMUM_MESH_TARGET={mesh_target}",
            f"OPTIMUM_MESH_MIN={mesh_min}",
            f"OPTIMUM_MESH_MAX={mesh_max}",
            f"OPTIMUM_SHARD_FACTOR={shard_factor}",
            "OPTIMUM_SHARD_MULT=1.5",
            "OPTIMUM_THRESHOLD=0.75",
        ]
        if index > 0:
            env.append(
                f"BOOTSTRAP_PEERS=/ip4/{node_ip(0)}/tcp/{OPTIMUM_PORT}/p2p/{bootstrap_peer_id}"
            )

    svc: dict = {
        "image": "getoptimum/p2pnode:${P2P_NODE_VERSION:-latest}",
        "platform": "linux/amd64",
        "environment": env,
        "networks": {"bench-network": {"ipv4_address": ip}},
        "ports": [
            f"{HOST_SIDECAR_BASE + index}:{SIDECAR_PORT}",
            f"{HOST_API_BASE + index}:{API_PORT}",
        ],
    }

    if index == 0:
        svc["volumes"] = ["./identity:/identity"]
    else:
        svc["depends_on"] = ["p2pnode-1"]

    needs_tc = (latency_ms is not None and latency_ms > 0) or (
        bandwidth_mbit is not None and bandwidth_mbit > 0
    )
    if needs_tc:
        svc["cap_add"] = ["NET_ADMIN"]
        tc_cmds = "apk add --no-cache iproute2 > /dev/null 2>&1 || true"
        if latency_ms and latency_ms > 0 and bandwidth_mbit and bandwidth_mbit > 0:
            tc_cmds += f"; tc qdisc add dev eth0 root handle 1: netem delay {latency_ms}ms || true"
            tc_cmds += f"; tc qdisc add dev eth0 parent 1:1 handle 10: tbf rate {bandwidth_mbit}mbit burst 256kb latency 100ms || true"
        elif latency_ms and latency_ms > 0:
            tc_cmds += f"; tc qdisc add dev eth0 root netem delay {latency_ms}ms || true"
        elif bandwidth_mbit and bandwidth_mbit > 0:
            tc_cmds += f"; tc qdisc add dev eth0 root tbf rate {bandwidth_mbit}mbit burst 256kb latency 100ms || true"
        tc_cmds += "; exec /p2pnode/p2pnode"
        svc["entrypoint"] = ["/bin/sh", "-c", tc_cmds]

    return name, svc


def build_proxy(n_nodes: int) -> tuple[str, dict]:
    p2p_nodes = ",".join(f"p2pnode-{i + 1}:{SIDECAR_PORT}" for i in range(n_nodes))
    depends = [f"p2pnode-{i + 1}" for i in range(n_nodes)]

    svc = {
        "image": "getoptimum/proxy:${PROXY_VERSION:-latest}",
        "platform": "linux/amd64",
        "ports": ["8081:8080", "50051:50051"],
        "environment": [
            "PROXY_PORT=:8080",
            "PROXY_GRPC_PORT=:50051",
            "CLUSTER_ID=${CLUSTER_ID}",
            "ENABLE_AUTH=false",
            "LOG_LEVEL=debug",
            f"P2P_NODES={p2p_nodes}",
        ],
        "networks": {"bench-network": {"ipv4_address": f"172.28.0.{PROXY_IP_OCTET}"}},
        "depends_on": depends,
    }
    return "proxy-1", svc


def generate(
    n_nodes: int,
    mode: str,
    bootstrap_peer_id: str,
    outdir: str,
    latencies: list[int] | None = None,
    bandwidth_mbit: int | None = None,
    shard_factor: int = 4,
) -> str:
    services: dict = {}

    proxy_name, proxy_svc = build_proxy(n_nodes)
    services[proxy_name] = proxy_svc

    for i in range(n_nodes):
        lat = latencies[i % len(latencies)] if latencies else None
        name, svc = build_p2pnode(
            i, n_nodes, mode, bootstrap_peer_id,
            latency_ms=lat, bandwidth_mbit=bandwidth_mbit,
            shard_factor=shard_factor,
        )
        services[name] = svc

    compose = {
        "services": services,
        "networks": {
            "bench-network": {
                "driver": "bridge",
                "ipam": {"config": [{"subnet": SUBNET}]},
            }
        },
    }

    fname = f"docker-compose-{'gossipsub' if mode == 'gossipsub' else 'optimum'}.yml"
    path = os.path.join(outdir, fname)
    os.makedirs(outdir, exist_ok=True)
    with open(path, "w") as f:
        yaml.dump(compose, f, default_flow_style=False, sort_keys=False)

    return path


def main():
    parser = argparse.ArgumentParser(description="Generate benchmark docker-compose files")
    parser.add_argument("--nodes", type=int, default=8)
    parser.add_argument("--mode", choices=["gossipsub", "optimum", "both"], default="both")
    parser.add_argument("--peer-id", default="12D3KooWD5RtEPmMR9Yb2ku5VuxqK7Yj1Y5Gv8DmffJ6Ei8maU44")
    parser.add_argument("--outdir", default=".")
    parser.add_argument("--latency", help="Comma-separated ms values per node (cycled)")
    parser.add_argument("--bandwidth", type=int, help="Per-node bandwidth cap in mbit")
    parser.add_argument("--shard-factor", type=int, default=4)
    args = parser.parse_args()

    latencies = None
    if args.latency:
        latencies = [int(x) for x in args.latency.split(",")]

    modes = ["gossipsub", "optimum"] if args.mode == "both" else [args.mode]
    for m in modes:
        path = generate(
            args.nodes, m, args.peer_id, args.outdir,
            latencies=latencies, bandwidth_mbit=args.bandwidth,
            shard_factor=args.shard_factor,
        )
        print(f"Generated: {path}")


if __name__ == "__main__":
    main()
