"""MTU validator: representative-sampled cross-rack path MTU observation.

From each rack's source representative (see representatives.py), probes one
representative data node per remote rack with oversized DF-bit ICMP and
records observed rack-pair path MTU as informational cross_rack_mtu records.
Output conforms to the shared probe-output schema (see schemas.py).

Walking-skeleton stub; the real implementation arrives in the multi-rack
stage and must keep the same contract: mutate ``section`` in place, register
subprocesses with ``cancellation``, check ``cancellation.is_set()`` between
per-rack probe iterations, and set a terminal ``validator_status`` on normal
completion.
"""


def run(topology, node, section, cancellation):
    """Record an empty, complete result set."""
    section["validator_status"] = "complete"
