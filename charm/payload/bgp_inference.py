"""BGP inference: representative-sampled cross-rack reachability and traceroute.

From each rack's source representative (see representatives.py), probes the
representative and fallback data nodes per remote rack via ICMP and runs
traceroute on dual failure to infer where cross-rack traffic stops, emitting
rack-pair paths[] records. Output conforms to the shared probe-output schema
(see schemas.py).

Stub until the multi-rack stage; a schema-conforming no-op stub arrives with
the walking skeleton.
"""
