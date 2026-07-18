#!/usr/bin/env python3
"""Probe payload entry point.

Invoked by the charm as ``probe.py <topology-json-path> <probe-timeout-seconds>``.
Loads the topology, resolves node identity by MAC matching, runs the
validators via the probe runner, and writes
/var/log/network-tester/probe-output.json.

Stdlib only: this script runs on MAAS ephemeral nodes with no third-party
packages installed.
"""

import sys


def main(argv=None):
    """Run the probe; implemented in the walking-skeleton stage."""
    print("network-tester probe payload is not implemented yet", file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
