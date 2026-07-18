"""Deterministic rack representative selection for cross-rack probing.

Implements the machine-readable selection parameters of the
``cross-rack-data-routing`` reachability rule: for each rack, the source and
target representative is the in-scope data machine with the lexicographically
lowest ``system_id``; the BGP fallback target is the next lexicographic
in-scope data machine in the target rack. Used by mtu-validator,
bgp-inference, and the CLI report-generator so all three derive identical
representative-sampled rack-pair universes.

This file is duplicated at ``cli/representatives.py`` because the charm
payload cannot import an installed package on nodes. Tests assert the copies
are byte-identical; edit both together.
"""

RULE_CROSS_RACK = "cross-rack-data-routing"


def cross_rack_rule(topology):
    """Return the cross-rack-data-routing rule parameters from a topology."""
    try:
        return topology["reachability_model"]["rules"][RULE_CROSS_RACK]["parameters"]
    except (KeyError, TypeError):
        raise ValueError(f"topology has no usable '{RULE_CROSS_RACK}' rule") from None


def _candidates(machines, rack, params, selection):
    """Return system_ids eligible under a selection, sorted by the selection field."""
    strategy = selection.get("strategy")
    if strategy not in ("lexicographic-lowest", "next-lexicographic"):
        raise ValueError(f"unknown selection strategy: {strategy!r}")
    field = selection.get("field")
    if field != "system_id":
        raise ValueError(f"unknown selection field: {field!r}")
    role = params.get("applicable_role")
    require_in_scope = selection.get("in_scope", True)
    eligible = [
        m[field]
        for m in machines
        if m.get("rack") == rack
        and m.get("role") == role
        and (not require_in_scope or m.get("in_scope") is True)
    ]
    return sorted(eligible)


def source_representative(machines, rack, params):
    """Return the source representative system_id for a rack, or None."""
    candidates = _candidates(machines, rack, params, params["source_selection"])
    return candidates[0] if candidates else None


def target_representative(machines, rack, params):
    """Return the target representative system_id for a rack, or None."""
    candidates = _candidates(machines, rack, params, params["target_selection"])
    return candidates[0] if candidates else None


def fallback_target(machines, rack, params):
    """Return the BGP fallback target system_id for a rack, or None.

    The fallback is the next lexicographic eligible system_id after the
    target representative; None when the rack has fewer than two eligible
    machines.
    """
    candidates = _candidates(machines, rack, params, params["fallback_selection"])
    return candidates[1] if len(candidates) > 1 else None


def data_racks(machines, params):
    """Return the sorted racks that have at least one eligible data machine."""
    role = params.get("applicable_role")
    racks = {
        m.get("rack")
        for m in machines
        if m.get("role") == role and m.get("in_scope") is True and m.get("rack")
    }
    return sorted(racks)


def remote_racks(machines, local_rack, params):
    """Return the sorted remote racks for a source rack."""
    return [rack for rack in data_racks(machines, params) if rack != local_rack]


def select_representatives(topology):
    """Derive the full representative-sampled rack-pair universe from a topology.

    Returns ``{source_rack: {"source": system_id, "targets": {remote_rack:
    {"representative": system_id, "fallback": system_id | None}}}}`` with one
    entry per rack that has an eligible source representative.
    """
    params = cross_rack_rule(topology)
    machines = topology.get("machines", [])
    selections = {}
    for rack in data_racks(machines, params):
        source = source_representative(machines, rack, params)
        if source is None:
            continue
        targets = {}
        for remote in remote_racks(machines, rack, params):
            targets[remote] = {
                "representative": target_representative(machines, remote, params),
                "fallback": fallback_target(machines, remote, params),
            }
        selections[rack] = {"source": source, "targets": targets}
    return selections
