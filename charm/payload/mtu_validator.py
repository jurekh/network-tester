"""MTU validator: representative-sampled cross-rack path MTU observation.

From each rack's source representative (see representatives.py), probes one
representative data node per remote rack with oversized DF-bit ICMP and
records observed rack-pair path MTU as informational cross_rack_mtu records.
Output conforms to the shared probe-output schema (see schemas.py).

Stub until the multi-rack stage; a schema-conforming no-op stub arrives with
the walking skeleton.
"""
