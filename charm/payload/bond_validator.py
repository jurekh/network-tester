"""Bond validator: LACP PDU capture and bond mode/cabling checks.

Captures LACP PDUs (EtherType 0x8809) per bond member interface via tcpdump,
parses them with stdlib struct, and detects bond-mode-mismatch and
asymmetric-bond-cable faults. Output conforms to the shared probe-output
schema (see schemas.py).

Stub until the bond validation stage; a schema-conforming no-op stub arrives
with the walking skeleton.
"""
