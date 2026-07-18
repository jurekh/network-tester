#!/usr/bin/env python3
"""Probe payload entry point.

Invoked by the charm as ``probe.py <topology-json-path> <probe-timeout-seconds>``.
Loads the topology, resolves node identity by MAC matching, runs the
validators via the probe runner, and writes
/var/log/network-tester/probe-output.json.

Stdlib only: this script runs on MAAS ephemeral nodes with no third-party
packages installed.
"""

import re
import subprocess
import sys
from pathlib import Path

if __package__ in (None, ""):
    # Invoked as a script: make sibling payload modules importable by name.
    sys.path.insert(0, str(Path(__file__).resolve().parent))

import json

import probe_runner
import schemas


def local_macs():
    """Interface MAC addresses reported by `ip link show`."""
    result = subprocess.run(
        ["ip", "-o", "link", "show"], check=True, capture_output=True, text=True
    )
    return {mac.lower() for mac in re.findall(r"link/ether ([0-9A-Fa-f:]{17})", result.stdout)}


def machine_macs(machine):
    macs = set()
    for interface in machine["interfaces"]:
        macs.add(interface["mac"].lower())
        for member in interface.get("bond_members", []):
            macs.add(member["mac"].lower())
    return macs


def find_node(topology, macs):
    """Return the machine record whose interface MACs overlap the local set."""
    for machine in topology["machines"]:
        if machine_macs(machine) & macs:
            return machine
    return None


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    if len(argv) != 2:
        print("usage: probe.py <topology-json-path> <probe-timeout-seconds>", file=sys.stderr)
        return 2
    topology_path = Path(argv[0])
    try:
        timeout = int(argv[1])
    except ValueError:
        timeout = 0
    if timeout <= 0:
        print(f"probe-timeout must be a positive integer, got '{argv[1]}'", file=sys.stderr)
        return 2
    if not topology_path.is_file():
        print(
            f"Topology file not found at {topology_path}; charm install hook "
            "must write it before invoking the payload",
            file=sys.stderr,
        )
        return 2
    try:
        topology = json.loads(topology_path.read_text())
        schemas.ensure_valid(topology, schemas.validate_topology, "topology")
    except ValueError as exc:
        print(f"Topology file at {topology_path} is not valid: {exc}", file=sys.stderr)
        return 2

    macs = local_macs()
    node = find_node(topology, macs)
    if node is None:
        print(
            f"Node identity not found in topology. Observed MACs: {sorted(macs)}.",
            file=sys.stderr,
        )
        return 2

    status = probe_runner.run_probe(topology, node, timeout)
    print(f"probe finished with status {status}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
