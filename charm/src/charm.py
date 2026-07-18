#!/usr/bin/env python3
# Copyright 2026 jerzy.husakowski@canonical.com
# See LICENSE file for licensing details.

"""Juju machine charm coordinating the network-tester probe payload."""

import json
import logging
import re
import subprocess
import sys
from pathlib import Path

import ops

logger = logging.getLogger(__name__)

# Probe runtime tools; all in Ubuntu main. ping is provided by iputils-ping,
# which is part of the standard server image.
PROBE_PACKAGES = ["tcpdump", "iputils-arping", "traceroute"]

PROBE_OUTPUT_PATH = Path("/var/log/network-tester/probe-output.json")
STATE_DIR = Path("/var/lib/network-tester")
TOPOLOGY_PATH = STATE_DIR / "topology.json"
LAST_RUN_PATH = STATE_DIR / "last-probe-run-id"

PEER_RELATION = "network-tester-peers"


class NetworkTesterCharm(ops.CharmBase):
    """Charm orchestrating probe runs on each node."""

    def __init__(self, framework: ops.Framework):
        super().__init__(framework)
        framework.observe(self.on.install, self._on_install)
        framework.observe(self.on.start, self._on_start)
        framework.observe(self.on.config_changed, self._on_config_changed)
        framework.observe(self.on[PEER_RELATION].relation_changed, self._on_peer_relation_changed)
        framework.observe(self.on["collect-results"].action, self._on_collect_results)

    def _on_install(self, event: ops.InstallEvent):
        """Install probe packages, store the topology, resolve node identity."""
        self.unit.status = ops.MaintenanceStatus("installing probe packages")
        subprocess.run(["apt", "install", "-y", *PROBE_PACKAGES], check=True)

        self.unit.status = ops.MaintenanceStatus("loading topology resource")
        try:
            resource_path = self.model.resources.fetch("topology")
        except Exception as exc:
            self.unit.status = ops.BlockedStatus(
                "topology resource is required but not attached"
            )
            raise RuntimeError("topology resource is required but not attached") from exc
        topology = json.loads(Path(resource_path).read_text())
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        TOPOLOGY_PATH.write_text(json.dumps(topology))

        node = _find_node(topology, _local_macs())
        if node is None:
            macs = sorted(_local_macs())
            message = (
                f"Node identity not found in topology. Observed MACs: {macs}. "
                "These MACs do not match any machine in the topology model."
            )
            self.unit.status = ops.BlockedStatus(message)
            raise RuntimeError(message)
        logger.info("node identity: %s (%s)", node["hostname"], node["system_id"])

    def _on_start(self, event: ops.StartEvent):
        """Report the unit ready to observe probe-run-id."""
        self.unit.status = ops.ActiveStatus("ready")

    def _on_config_changed(self, event: ops.ConfigChangedEvent):
        """Run the probe on a non-empty probe-run-id; leader also propagates it.

        Every unit probes from config-changed, not from the leader's relation
        write: the app-databag write only commits when the leader's hook exits,
        and the leader used to probe inside that same hook, so peers started a
        full probe-duration later and passive capture windows never overlapped
        the leader's traffic. config-changed reaches all units within seconds
        of the CLI's `juju config`, keeping the windows concurrent. The
        relation write stays as the catch-up path for units that missed the
        config event (the last-run guard dedups the double trigger).
        """
        run_id = str(self.config["probe-run-id"])
        if not run_id:
            return
        if self.unit.is_leader():
            relation = self.model.get_relation(PEER_RELATION)
            if relation is not None:
                relation.data[self.app]["probe-run-id"] = run_id
        self._maybe_run_probe(run_id)

    def _on_peer_relation_changed(self, event: ops.RelationChangedEvent):
        """Run the probe payload when a new probe-run-id appears."""
        run_id = event.relation.data[self.app].get("probe-run-id", "")
        self._maybe_run_probe(run_id)

    def _maybe_run_probe(self, run_id: str):
        if not run_id:
            return
        last = LAST_RUN_PATH.read_text().strip() if LAST_RUN_PATH.is_file() else ""
        if run_id == last:
            return
        self.unit.status = ops.MaintenanceStatus(f"running probe {run_id}")
        probe = self.charm_dir / "payload" / "probe.py"
        timeout = int(self.config["probe-timeout"])
        subprocess.run(
            [sys.executable, str(probe), str(TOPOLOGY_PATH), str(timeout)], check=True
        )
        LAST_RUN_PATH.parent.mkdir(parents=True, exist_ok=True)
        LAST_RUN_PATH.write_text(run_id)
        self.unit.status = ops.ActiveStatus(f"probe {run_id} complete")

    def _on_collect_results(self, event: ops.ActionEvent):
        """Return the unit's probe output, or a missing marker if absent."""
        if not PROBE_OUTPUT_PATH.is_file():
            event.set_results({"status": "missing", "unit": self.unit.name})
            return
        event.set_results({"probe-output": PROBE_OUTPUT_PATH.read_text()})


def _local_macs():
    """Interface MAC addresses reported by `ip link show`."""
    result = subprocess.run(
        ["ip", "-o", "link", "show"], check=True, capture_output=True, text=True
    )
    return {mac.lower() for mac in re.findall(r"link/ether ([0-9A-Fa-f:]{17})", result.stdout)}


def _find_node(topology, macs):
    """Return the machine record whose interface MACs overlap the local set."""
    for machine in topology["machines"]:
        machine_macs = set()
        for interface in machine["interfaces"]:
            machine_macs.add(interface["mac"].lower())
            for member in interface.get("bond_members", []):
                machine_macs.add(member["mac"].lower())
        if machine_macs & macs:
            return machine
    return None


if __name__ == "__main__":  # pragma: nocover
    ops.main(NetworkTesterCharm)
