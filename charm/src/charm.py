#!/usr/bin/env python3
# Copyright 2026 jerzy.husakowski@canonical.com
# See LICENSE file for licensing details.

"""Juju machine charm coordinating the network-tester probe payload."""

import logging
import subprocess
from pathlib import Path

import ops

logger = logging.getLogger(__name__)

# Probe runtime tools; all in Ubuntu main. ping is provided by iputils-ping,
# which is part of the standard server image.
PROBE_PACKAGES = ["tcpdump", "iputils-arping", "traceroute"]

PROBE_OUTPUT_PATH = Path("/var/log/network-tester/probe-output.json")


class NetworkTesterCharm(ops.CharmBase):
    """Charm orchestrating probe runs on each node."""

    def __init__(self, framework: ops.Framework):
        super().__init__(framework)
        framework.observe(self.on.install, self._on_install)
        framework.observe(self.on.start, self._on_start)
        framework.observe(self.on["collect-results"].action, self._on_collect_results)

    def _on_install(self, event: ops.InstallEvent):
        """Install probe runtime dependencies."""
        self.unit.status = ops.MaintenanceStatus("installing probe packages")
        subprocess.run(["apt", "install", "-y", *PROBE_PACKAGES], check=True)

    def _on_start(self, event: ops.StartEvent):
        """Report the unit ready."""
        self.unit.status = ops.ActiveStatus()

    def _on_collect_results(self, event: ops.ActionEvent):
        """Return the unit's probe output, or a missing marker if absent."""
        if not PROBE_OUTPUT_PATH.is_file():
            event.set_results({"status": "missing", "unit": self.unit.name})
            return
        event.set_results({"probe-output": PROBE_OUTPUT_PATH.read_text()})


if __name__ == "__main__":  # pragma: nocover
    ops.main(NetworkTesterCharm)
