"""Versioned shared data schemas for network-tester (schema_version 1).

Defines validation for the three documents exchanged between components:

- topology JSON: produced by the CLI from the MAAS API, attached as the
  ``topology`` Juju resource, consumed by the probe payload on each node;
- probe-output JSON: written by the probe-runner on each node, returned by
  the ``collect-results`` action;
- report JSON: produced by the CLI report generator.

Validation is stdlib-only because this module ships inside the charm payload.
Validators return a list of error strings; an empty list means the document
is valid.

This module is maintained at ``cli/schemas.py`` (canonical) with a
byte-identical copy at ``charm/payload/schemas.py``, because the charm payload
cannot import an installed package on nodes. Edit the cli copy and run
``make sync-shared``; tests assert the copies stay byte-identical.
"""

SCHEMA_VERSION = "1"

ROLES = frozenset({"rack-controller", "bmc-oam", "data"})
FABRIC_CLASSES = frozenset({"management", "data"})
SCOPE_MODES = frozenset({"all", "rack", "nodes"})

PROBE_STATUSES = frozenset({"complete", "timeout", "cancelled"})
VALIDATOR_STATUSES = frozenset({"complete", "skipped", "not_started", "timeout", "cancelled"})
OBSERVATION_STATUSES = frozenset({"success", "failure", "inconclusive", "timeout", "cancelled"})

CLASSIFICATIONS = frozenset({"definitive", "inferred", "inconclusive", "informational"})
FINDING_SCOPES = frozenset({"interface", "node", "rack-pair"})
TARGET_ROLES = frozenset({"representative", "fallback"})

MISSING_NODE_REASONS = frozenset(
    {
        "placement-failed",
        "deployment-timeout",
        "probe-timeout",
        "no-probe-output",
        "stale-probe-output",
    }
)

RULE_L2 = "l2-same-fabric-vlan"
RULE_BMC_OAM = "bmc-oam-restricted"
RULE_CROSS_RACK = "cross-rack-data-routing"
RULE_IDS = frozenset({RULE_L2, RULE_BMC_OAM, RULE_CROSS_RACK})

VALIDATOR_SECTIONS = (
    "bond_validator",
    "vlan_neighbor_validator",
    "mtu_validator",
    "bgp_inference",
)

# Per-path record lists owned by the cross-rack validators.
PATH_LIST_KEYS = {"mtu_validator": "cross_rack_mtu", "bgp_inference": "paths"}


def _check(doc, key, types, errors, ctx, required=True, nullable=False):
    """Append an error unless doc[key] exists with one of the expected types.

    Returns the value (or None when absent/invalid) for further checks.
    """
    if not isinstance(doc, dict):
        errors.append(f"{ctx}: expected an object")
        return None
    if key not in doc:
        if required:
            errors.append(f"{ctx}: missing required key '{key}'")
        return None
    value = doc[key]
    if value is None and nullable:
        return None
    if not isinstance(value, types):
        type_names = (
            types.__name__ if isinstance(types, type) else "/".join(t.__name__ for t in types)
        )
        errors.append(f"{ctx}.{key}: expected {type_names}, got {type(value).__name__}")
        return None
    return value


def _check_enum(doc, key, allowed, errors, ctx, required=True, nullable=False):
    value = _check(doc, key, str, errors, ctx, required=required, nullable=nullable)
    if value is not None and value not in allowed:
        errors.append(f"{ctx}.{key}: '{value}' not in {sorted(allowed)}")
    return value


def _check_schema_version(doc, errors, ctx):
    version = _check(doc, "schema_version", str, errors, ctx)
    if version is not None and version != SCHEMA_VERSION:
        errors.append(f"{ctx}.schema_version: expected '{SCHEMA_VERSION}', got '{version}'")


def validate_finding(finding, ctx="finding"):
    """Validate the common finding envelope shared by all validators."""
    errors = []
    if not isinstance(finding, dict):
        return [f"{ctx}: expected an object"]
    _check(finding, "type", str, errors, ctx)
    _check_enum(finding, "classification", CLASSIFICATIONS, errors, ctx)
    _check_enum(finding, "scope", FINDING_SCOPES, errors, ctx)
    _check(finding, "hint", str, errors, ctx)
    _check(finding, "details", dict, errors, ctx)
    return errors


def _validate_findings_list(section, key, errors, ctx, required=True):
    findings = _check(section, key, list, errors, ctx, required=required)
    if findings:
        for i, finding in enumerate(findings):
            errors.extend(validate_finding(finding, f"{ctx}.{key}[{i}]"))


def _validate_interface(interface, errors, ctx):
    _check(interface, "name", str, errors, ctx)
    _check(interface, "mac", str, errors, ctx)
    _check(interface, "fabric", str, errors, ctx)
    _check_enum(interface, "fabric_class", FABRIC_CLASSES, errors, ctx)
    _check(interface, "vlan_tag", int, errors, ctx, nullable=True)
    _check(interface, "subnet_cidr", str, errors, ctx, required=False, nullable=True)
    _check(interface, "ip", str, errors, ctx, required=False, nullable=True)
    _check(interface, "gateway_ip", str, errors, ctx, required=False, nullable=True)
    if interface.get("type") == "bond":
        _check(interface, "bond_mode", str, errors, ctx)
        members = _check(interface, "bond_members", list, errors, ctx)
        if members:
            for i, member in enumerate(members):
                _check(member, "name", str, errors, f"{ctx}.bond_members[{i}]")
                _check(member, "mac", str, errors, f"{ctx}.bond_members[{i}]")


def _validate_selection(params, key, expected_strategy, errors, ctx):
    selection = _check(params, key, dict, errors, ctx)
    if selection is None:
        return
    sctx = f"{ctx}.{key}"
    strategy = _check(selection, "strategy", str, errors, sctx)
    if strategy is not None and strategy != expected_strategy:
        errors.append(f"{sctx}.strategy: expected '{expected_strategy}', got '{strategy}'")
    field = _check(selection, "field", str, errors, sctx)
    if field is not None and field != "system_id":
        errors.append(f"{sctx}.field: expected 'system_id', got '{field}'")
    in_scope = _check(selection, "in_scope", bool, errors, sctx)
    if in_scope is not None and in_scope is not True:
        errors.append(f"{sctx}.in_scope: expected true")


def _validate_cross_rack_rule_params(params, errors, ctx):
    probe_scope = _check(params, "probe_scope", str, errors, ctx)
    if probe_scope is not None and probe_scope != "rack-pair-representative":
        errors.append(f"{ctx}.probe_scope: expected 'rack-pair-representative'")
    role = _check(params, "applicable_role", str, errors, ctx)
    if role is not None and role != "data":
        errors.append(f"{ctx}.applicable_role: expected 'data'")
    iface_class = _check(params, "interface_class", str, errors, ctx)
    if iface_class is not None and iface_class != "data":
        errors.append(f"{ctx}.interface_class: expected 'data'")
    _validate_selection(params, "source_selection", "lexicographic-lowest", errors, ctx)
    _validate_selection(params, "target_selection", "lexicographic-lowest", errors, ctx)
    _validate_selection(params, "fallback_selection", "next-lexicographic", errors, ctx)


def _validate_reachability_model(model, errors, ctx):
    rules = _check(model, "rules", dict, errors, ctx)
    if rules is None:
        return
    for rule_id in RULE_IDS:
        if rule_id not in rules:
            errors.append(f"{ctx}.rules: missing required rule '{rule_id}'")
    for rule_id, rule in rules.items():
        rctx = f"{ctx}.rules[{rule_id}]"
        declared_id = _check(rule, "id", str, errors, rctx)
        if declared_id is not None and declared_id != rule_id:
            errors.append(f"{rctx}.id: '{declared_id}' does not match rule key '{rule_id}'")
        _check(rule, "description", str, errors, rctx)
        roles = _check(rule, "applicable_roles", list, errors, rctx)
        if roles is not None:
            for role in roles:
                if role not in ROLES:
                    errors.append(f"{rctx}.applicable_roles: unknown role '{role}'")
        params = _check(rule, "parameters", dict, errors, rctx)
        if rule_id == RULE_CROSS_RACK and params is not None:
            _validate_cross_rack_rule_params(params, errors, f"{rctx}.parameters")


def validate_topology(doc):
    """Validate a topology document; returns a list of error strings."""
    errors = []
    if not isinstance(doc, dict):
        return ["topology: expected an object"]
    ctx = "topology"
    _check_schema_version(doc, errors, ctx)

    scope = _check(doc, "scope", dict, errors, ctx)
    if scope is not None:
        _check_enum(scope, "mode", SCOPE_MODES, errors, f"{ctx}.scope")
        _check(scope, "args", list, errors, f"{ctx}.scope")

    fabrics = _check(doc, "fabrics", list, errors, ctx)
    if fabrics is not None:
        for i, fabric in enumerate(fabrics):
            fctx = f"{ctx}.fabrics[{i}]"
            _check(fabric, "name", str, errors, fctx)
            _check_enum(fabric, "class", FABRIC_CLASSES, errors, fctx)

    machines = _check(doc, "machines", list, errors, ctx)
    if machines is not None:
        for i, machine in enumerate(machines):
            mctx = f"{ctx}.machines[{i}]"
            _check(machine, "system_id", str, errors, mctx)
            _check(machine, "hostname", str, errors, mctx)
            _check(machine, "rack", str, errors, mctx)
            _check_enum(machine, "role", ROLES, errors, mctx)
            _check(machine, "in_scope", bool, errors, mctx)
            interfaces = _check(machine, "interfaces", list, errors, mctx)
            if interfaces is not None:
                for j, interface in enumerate(interfaces):
                    _validate_interface(interface, errors, f"{mctx}.interfaces[{j}]")

    model = _check(doc, "reachability_model", dict, errors, ctx)
    if model is not None:
        _validate_reachability_model(model, errors, f"{ctx}.reachability_model")
    return errors


def _validate_path_record(record, common_keys, errors, ctx):
    for key in common_keys:
        _check(record, key, str, errors, ctx)
    _check_enum(record, "observation_status", OBSERVATION_STATUSES, errors, ctx)


def _validate_mtu_section(section, errors, ctx):
    records = _check(section, "cross_rack_mtu", list, errors, ctx)
    if records is None:
        return
    for i, record in enumerate(records):
        rctx = f"{ctx}.cross_rack_mtu[{i}]"
        _validate_path_record(
            record, ("source_rack", "source_node", "target_rack", "target_node"), errors, rctx
        )
        _check(record, "observed_path_mtu_bytes", int, errors, rctx, nullable=True)
    if section.get("validator_status") == "not_started" and records:
        errors.append(f"{ctx}: not_started section must have empty cross_rack_mtu")


def _validate_bgp_section(section, errors, ctx):
    paths = _check(section, "paths", list, errors, ctx)
    if paths is None:
        return
    for i, record in enumerate(paths):
        rctx = f"{ctx}.paths[{i}]"
        _validate_path_record(
            record,
            ("source_rack", "source_node", "target_rack", "representative_target"),
            errors,
            rctx,
        )
        _check(record, "fallback_target", str, errors, rctx, required=False, nullable=True)
        _check(record, "reachable", bool, errors, rctx, nullable=True)
        if not isinstance(record, dict):
            continue
        target_role = record.get("target_role")
        if target_role is not None and target_role not in TARGET_ROLES:
            errors.append(f"{rctx}.target_role: '{target_role}' not in {sorted(TARGET_ROLES)}")
        if "target_role" not in record:
            errors.append(f"{rctx}: missing required key 'target_role'")
        finding = record.get("finding")
        if finding is not None:
            errors.extend(validate_finding(finding, f"{rctx}.finding"))
    if section.get("validator_status") == "not_started" and paths:
        errors.append(f"{ctx}: not_started section must have empty paths")


def validate_probe_output(doc):
    """Validate a per-unit probe-output document; returns a list of error strings."""
    errors = []
    if not isinstance(doc, dict):
        return ["probe_output: expected an object"]
    ctx = "probe_output"
    _check_schema_version(doc, errors, ctx)
    # Additive since the collector's stale-output cross-check: the payload
    # always writes it; optional here so older documents stay valid.
    _check(doc, "probe_run_id", str, errors, ctx, required=False)
    _check_enum(doc, "status", PROBE_STATUSES, errors, ctx)

    node = _check(doc, "node", dict, errors, ctx)
    if node is not None:
        nctx = f"{ctx}.node"
        _check(node, "system_id", str, errors, nctx)
        _check(node, "hostname", str, errors, nctx)
        _check(node, "interfaces", list, errors, nctx)

    for name in VALIDATOR_SECTIONS:
        section = _check(doc, name, dict, errors, ctx)
        if section is None:
            continue
        sctx = f"{ctx}.{name}"
        status = _check_enum(section, "validator_status", VALIDATOR_STATUSES, errors, sctx)
        if status == "skipped":
            _check(section, "skip_reason", str, errors, sctx)
        _validate_findings_list(section, "findings", errors, sctx)
        if status == "not_started" and section.get("findings"):
            errors.append(f"{sctx}: not_started section must have empty findings")
        if name == "bond_validator":
            _check(section, "bonds", list, errors, sctx)
        if name == "vlan_neighbor_validator":
            _check(section, "observations", list, errors, sctx)
        if name == "mtu_validator":
            _validate_mtu_section(section, errors, sctx)
        if name == "bgp_inference":
            _validate_bgp_section(section, errors, sctx)
    return errors


REPORT_LIST_FIELDS = (
    "definitive_failures",
    "inferred_failures",
    "warnings",
    "inconclusive_checks",
    "skipped_checks",
    "observations",
    "missing_nodes",
)

SUMMARY_COUNT_FIELDS = ("passed_count", "failed", "skipped", "inconclusive", "warnings")


def validate_report(doc):
    """Validate a final report document; returns a list of error strings."""
    errors = []
    if not isinstance(doc, dict):
        return ["report: expected an object"]
    ctx = "report"
    _check_schema_version(doc, errors, ctx)
    _check(doc, "generated_at", str, errors, ctx)

    summary = _check(doc, "summary", dict, errors, ctx)
    if summary is not None:
        for key in SUMMARY_COUNT_FIELDS:
            value = _check(summary, key, int, errors, f"{ctx}.summary")
            if isinstance(value, bool):
                errors.append(f"{ctx}.summary.{key}: expected int, got bool")

    for field in REPORT_LIST_FIELDS:
        entries = _check(doc, field, list, errors, ctx)
        if entries is None:
            continue
        for i, entry in enumerate(entries):
            ectx = f"{ctx}.{field}[{i}]"
            if not isinstance(entry, dict):
                errors.append(f"{ectx}: expected an object")
                continue
            if field in ("definitive_failures", "inferred_failures"):
                errors.extend(validate_finding(entry, ectx))
            if field == "missing_nodes":
                _check(entry, "system_id", str, errors, ectx)
                _check(entry, "hostname", str, errors, ectx)
                _check_enum(entry, "reason", MISSING_NODE_REASONS, errors, ectx)

    if "passed_checks" in doc:
        _check(doc, "passed_checks", list, errors, ctx)
    return errors


def ensure_valid(doc, validator, what):
    """Raise ValueError listing all errors when doc fails the given validator."""
    errors = validator(doc)
    if errors:
        raise ValueError(f"invalid {what}:\n" + "\n".join(errors))
    return doc
