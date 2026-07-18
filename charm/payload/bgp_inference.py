"""BGP inference: representative-sampled cross-rack reachability and traceroute.

From each rack's source representative (see representatives.py), probes the
representative and fallback data nodes per remote rack via ICMP and runs
traceroute on dual failure to infer where cross-rack traffic stops, emitting
rack-pair paths[] records. Output conforms to the shared probe-output schema
(see schemas.py).

Walking-skeleton stub; the real implementation arrives in the multi-rack
stage and must keep the same contract: mutate ``section`` in place, register
subprocesses with ``cancellation``, check ``cancellation.is_set()`` between
per-rack probe iterations, and set a terminal ``validator_status`` on normal
completion.
"""


def run(topology, node, section, cancellation):
    """Record an empty, complete result set."""
    section["validator_status"] = "complete"
