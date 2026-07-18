"""Bond validator: LACP PDU capture and bond mode/cabling checks.

Captures LACP PDUs (EtherType 0x8809) per bond member interface via tcpdump,
parses them with stdlib struct, and detects bond-mode-mismatch and
asymmetric-bond-cable faults. Output conforms to the shared probe-output
schema (see schemas.py).

Walking-skeleton stub; the real implementation arrives in the bond
validation stage and must keep the same contract: mutate ``section`` in
place, register subprocesses with ``cancellation``, check
``cancellation.is_set()`` between probe iterations, and set a terminal
``validator_status`` on normal completion.
"""


def run(topology, node, section, cancellation):
    """Record an empty, complete result set."""
    section["validator_status"] = "complete"
