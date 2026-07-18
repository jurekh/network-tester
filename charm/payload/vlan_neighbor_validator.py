"""VLAN neighbor validator: ARP/ICMP verification of expected L2 peer sets.

Derives expected in-scope, expected-but-out-of-scope, and known-forbidden
peer sets from the topology, probes expected peers with targeted arping and
ICMP, and detects unexpected or forbidden neighbors via passive ARP capture.
Output conforms to the shared probe-output schema (see schemas.py).

Walking-skeleton stub; the real implementation arrives in the VLAN
validation stage and must keep the same contract: mutate ``section`` in
place, register subprocesses with ``cancellation``, check
``cancellation.is_set()`` between probe iterations, and set a terminal
``validator_status`` on normal completion.
"""


def run(topology, node, section, cancellation):
    """Record an empty, complete result set."""
    section["validator_status"] = "complete"
