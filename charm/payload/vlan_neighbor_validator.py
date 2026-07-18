"""VLAN neighbor validator: ARP/ICMP verification of expected L2 peer sets.

Derives expected in-scope, expected-but-out-of-scope, and known-forbidden
peer sets from the topology, probes expected peers with targeted arping and
ICMP, and detects unexpected or forbidden neighbors via passive ARP capture.
Output conforms to the shared probe-output schema (see schemas.py).

Stub until the VLAN validation stage; a schema-conforming no-op stub arrives
with the walking skeleton.
"""
