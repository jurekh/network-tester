"""Probe runner: validator sequencing, timeout enforcement, output writing.

Runs bond_validator and vlan_neighbor_validator concurrently, then
mtu_validator and bgp_inference sequentially; enforces the overall probe
timeout with cooperative cancellation; writes probe-output.json conforming
to the shared probe-output schema (see schemas.py).

Implemented in the walking-skeleton stage.
"""
