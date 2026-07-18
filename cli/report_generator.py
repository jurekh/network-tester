"""Report generator: aggregate per-unit probe outputs into JSON and text reports.

Classifies findings by their classification field, surfaces non-complete
probe statuses as inconclusive checks, and writes the timestamped report
files. When a topology is provided, it also diffs aggregated observations
against the expected check universe derived from the reachability model:
VLAN edges between in-scope same-fabric/VLAN machines (bidirectional
confirmation, skip grouping by out-of-scope peer) and representative-sampled
cross-rack BGP/MTU rack-pair paths. BGP results are reconciled across both
directions of each rack-pair link, with source/target phase-1 health gating,
and MTU records become informational observations with asymmetry warnings.
"""

import json
import time
from pathlib import Path

from cli import representatives, schemas

RULE_BMC_OAM = "bmc-oam-restricted"

VLAN_EDGE_FAIL_TYPES = ("missing-l2-neighbor", "icmp-unreachable", "forbidden-l2-neighbor")

CLASSIFICATION_FIELDS = {
    "definitive": "definitive_failures",
    "inferred": "inferred_failures",
    "inconclusive": "inconclusive_checks",
    "informational": "observations",
}

TEXT_SECTIONS = (
    ("FAILED CHECKS", "definitive_failures"),
    ("INFERRED FAILURES", "inferred_failures"),
    ("WARNINGS", "warnings"),
    ("SKIPPED CHECKS", "skipped_checks"),
    ("OBSERVATIONS", "observations"),
)


def _expected_l2_edges(topology):
    """Expected L2 adjacency edges between topology machines.

    An edge exists when two machines share an interface fabric name and
    vlan_tag and the adjacency is allowed by the reachability model (the
    bmc-oam-restricted rule forbids bmc-oam adjacency to anything but the
    same-rack rack-controller). Edges with both ends out of scope are not
    part of the check universe.
    """
    rules = topology.get("reachability_model", {}).get("rules", {})
    bmc_params = rules.get(RULE_BMC_OAM, {}).get("parameters", {})
    allowed_peer_roles = set(bmc_params.get("allowed_peer_roles", ["rack-controller"]))
    same_rack_only = bmc_params.get("same_rack_only", True)

    def allowed(a, b):
        for this, other in ((a, b), (b, a)):
            if this["role"] != "bmc-oam":
                continue
            if other["role"] not in allowed_peer_roles:
                return False
            if same_rack_only and other["rack"] != this["rack"]:
                return False
        return True

    machines = topology["machines"]
    edges = []
    for i, a in enumerate(machines):
        for b in machines[i + 1 :]:
            if not (a["in_scope"] or b["in_scope"]):
                continue
            shared_segment = any(
                ia.get("fabric") == ib.get("fabric") and ia.get("vlan_tag") == ib.get("vlan_tag")
                for ia in a["interfaces"]
                for ib in b["interfaces"]
            )
            if shared_segment and allowed(a, b):
                edges.append((a, b))
    return edges


def _vlan_direction_state(by_sid, src, dst):
    """One edge direction: did src's validator fail, confirm, or stay silent."""
    output = by_sid.get(src["system_id"])
    if output is None:
        return "nodata"
    section = output.get("vlan_neighbor_validator") or {}
    for finding in section.get("findings", []):
        if finding.get("type") not in VLAN_EDGE_FAIL_TYPES:
            continue
        if finding.get("details", {}).get("peer_system_id") == dst["system_id"]:
            return "failed"
    for obs in section.get("observations", []):
        if (
            obs.get("type") == "expected-peer-observed"
            and obs.get("peer_system_id") == dst["system_id"]
            and (obs.get("arp_observed") or obs.get("icmp_reachable"))
        ):
            return "confirmed"
    return "nodata"


def _classify_vlan_edges(report, topology, by_sid, passed):
    skip_counts = {}
    for a, b in _expected_l2_edges(topology):
        if not (a["in_scope"] and b["in_scope"]):
            peer = a if not a["in_scope"] else b
            skip_counts[peer["system_id"]] = skip_counts.get(peer["system_id"], 0) + 1
            continue
        forward = _vlan_direction_state(by_sid, a, b)
        backward = _vlan_direction_state(by_sid, b, a)
        if "failed" in (forward, backward):
            continue  # the findings themselves are already in definitive_failures
        if "confirmed" not in (forward, backward):
            report["inconclusive_checks"].append(
                {
                    "type": "vlan-reachability",
                    "note": (
                        f"no probe data for expected edge {a['hostname']} <-> "
                        f"{b['hostname']}; neither endpoint recorded the other"
                    ),
                    "details": {"nodes": [a["hostname"], b["hostname"]]},
                }
            )
            continue
        confirmed = "bidirectional" if forward == backward == "confirmed" else "unidirectional"
        passed.append(
            {
                "type": "vlan-reachability",
                "nodes": sorted([a["hostname"], b["hostname"]]),
                "confirmed": confirmed,
            }
        )
        if confirmed == "unidirectional":
            source, silent = (a, b) if forward == "confirmed" else (b, a)
            report["warnings"].append(
                {
                    "type": "unidirectional-observation",
                    "scope": "node",
                    "message": (
                        f"Node {source['hostname']} recorded node {silent['hostname']} but "
                        f"{silent['hostname']} did not record {source['hostname']} - "
                        "possible asymmetric path or probe timing issue"
                    ),
                    "details": {"source": source["hostname"], "peer": silent["hostname"]},
                }
            )
    for peer_sid in sorted(skip_counts):
        count = skip_counts[peer_sid]
        report["skipped_checks"].append(
            {
                "peer": peer_sid,
                "count": count,
                "message": f"{count} checks skipped - add {peer_sid} to test these paths",
            }
        )


def _path_record(by_sid, source_sid, section_name, list_key, remote_rack):
    output = by_sid.get(source_sid)
    if output is None:
        return None
    section = output.get(section_name) or {}
    for record in section.get(list_key, []):
        if record.get("target_rack") == remote_rack:
            return record
    return None


PHASE1_SECTIONS = ("bond_validator", "vlan_neighbor_validator")


def _rep_unhealthy(by_sid, sid):
    """True when a representative has a definitive phase-1 (bond/vlan) failure."""
    output = by_sid.get(sid)
    if not output:
        return False
    for name in PHASE1_SECTIONS:
        for finding in (output.get(name) or {}).get("findings", []):
            if finding.get("classification") == "definitive":
                return True
    return False


def _bgp_verdict(selections, by_sid, src_rack, dst_rack):
    """Reduce one directed rack-pair BGP path to a verdict for reconciliation.

    States: success, fallback (positive but representative target down), failure
    (definitive inferred fabric failure), inconclusive (timeout/cancelled/no
    classified finding), missing (source rep never reported), gated (source rep
    has a definitive phase-1 failure so its cross-rack result is unreliable).
    """
    src_sid = selections[src_rack]["source"]
    rep_target = selections[src_rack]["targets"][dst_rack]["representative"]
    verdict = {
        "src_rack": src_rack,
        "dst_rack": dst_rack,
        "src": src_sid,
        "rep_target": rep_target,
    }
    if _rep_unhealthy(by_sid, src_sid):
        verdict["state"] = "gated"
        return verdict
    rec = _path_record(by_sid, src_sid, "bgp_inference", "paths", dst_rack)
    verdict["rec"] = rec
    if rec is None:
        verdict["state"] = "missing"
        return verdict
    status = rec.get("observation_status")
    if status == "success":
        verdict["state"] = "fallback" if rec.get("target_role") == "fallback" else "success"
    elif status == "failure" and (rec.get("finding") or {}).get("classification") == "inferred":
        verdict["state"] = "failure"
        verdict["finding"] = rec["finding"]
    else:
        verdict["state"] = "inconclusive"
        verdict["status"] = status
    return verdict


def _bgp_inconclusive(rack_pair, verdict, note):
    return {
        "type": "bgp-reachability",
        "note": note,
        "details": {"rack_pair": rack_pair, "source": verdict["src"]},
    }


def _reconcile_bgp_link(report, selections, by_sid, a, b, passed, fallback_warned):
    directions = {}
    for src, dst in ((a, b), (b, a)):
        if dst in selections.get(src, {}).get("targets", {}):
            directions[(src, dst)] = _bgp_verdict(selections, by_sid, src, dst)
    states = {k: v["state"] for k, v in directions.items()}
    failures = [k for k, s in states.items() if s == "failure"]
    healthy = [k for k, s in states.items() if s in ("success", "fallback")]
    indeterminate = [k for k, s in states.items() if s in ("inconclusive", "missing", "gated")]
    pair = sorted([a, b])

    if len(failures) == 2:
        report["inferred_failures"].append(_link_inferred_failure(pair, directions, failures))
    elif failures:
        verdict = directions[failures[0]]
        if _rep_unhealthy(by_sid, verdict["rep_target"]):
            # 7.14: the failure points at a target rep with its own phase-1
            # failure; attribute it to that target node, not the fabric.
            _target_node_warning(report, verdict)
            indeterminate = [k for k in indeterminate if k != _reverse(failures[0])]
        elif healthy:
            _source_directional_warning(report, verdict)
        else:
            report["inconclusive_checks"].append(
                _bgp_inconclusive(
                    pair,
                    verdict,
                    f"rack-pair {pair[0]} <-> {pair[1]} failed from {verdict['src']} but the "
                    "reverse direction is unavailable; cannot confirm a fabric failure",
                )
            )
            indeterminate = [k for k in indeterminate if k != _reverse(failures[0])]

    for key in healthy:
        verdict = directions[key]
        passed.append(
            {"type": "bgp-reachability", "rack_pair": list(key), "source": verdict["src"]}
        )
        if states[key] == "fallback":
            _fallback_target_warning(report, verdict, fallback_warned)

    for key in indeterminate:
        report["inconclusive_checks"].append(_indeterminate_note(directions[key]))


def _reverse(key):
    return (key[1], key[0])


def _link_inferred_failure(pair, directions, failures):
    findings = [directions[k]["finding"] for k in failures]
    base = dict(findings[0])
    base["scope"] = "rack-pair"
    base["classification"] = "inferred"
    base["observed_by"] = [directions[k]["src"] for k in failures]
    base["node"] = directions[failures[0]]["src"]
    details = dict(base.get("details") or {})
    details["rack_pair"] = pair
    details["per_node_traceroute"] = {
        directions[k]["src"]: (directions[k]["finding"].get("details") or {}).get(
            "traceroute_hops"
        )
        for k in failures
    }
    base["details"] = details
    return base


def _link_pair(verdict):
    return sorted([verdict["src_rack"], verdict["dst_rack"]])


def _source_directional_warning(report, verdict):
    """One direction failed while the reverse is healthy: source/asymmetric issue."""
    report["warnings"].append(
        {
            "type": "bgp-directional",
            "scope": "node",
            "message": (
                f"cross-rack path {verdict['src_rack']} -> {verdict['dst_rack']} failed from "
                f"{verdict['src']} but the reverse direction is healthy; possible asymmetric "
                "routing or a source-node issue, not a confirmed rack-pair failure"
            ),
            "details": {"node": verdict["src"], "rack_pair": _link_pair(verdict)},
        }
    )


def _target_node_warning(report, verdict):
    """The failure points at a target rep that has its own phase-1 failure."""
    report["warnings"].append(
        {
            "type": "target-representative-unhealthy",
            "scope": "node",
            "message": (
                f"cross-rack path {verdict['src_rack']} -> {verdict['dst_rack']} failed toward "
                f"representative {verdict['rep_target']}, which has a definitive phase-1 failure; "
                "treating as a target-node issue, not a fabric failure"
            ),
            "details": {"node": verdict["rep_target"], "rack_pair": _link_pair(verdict)},
        }
    )


def _fallback_target_warning(report, verdict, fallback_warned):
    key = (verdict["dst_rack"], verdict["rep_target"])
    if key in fallback_warned:
        return
    fallback_warned.add(key)
    report["warnings"].append(
        {
            "type": "target-representative-unreachable",
            "scope": "node",
            "message": (
                f"representative target {verdict['rep_target']} in {verdict['dst_rack']} was "
                "unreachable; a fallback data node answered, so the rack-pair is reachable but "
                "the representative host needs attention"
            ),
            "details": {"node": verdict["rep_target"], "rack": verdict["dst_rack"]},
        }
    )


def _indeterminate_note(verdict):
    pair = sorted([verdict["src_rack"], verdict["dst_rack"]])
    if verdict["state"] == "gated":
        note = (
            f"rack-pair path {verdict['src_rack']} -> {verdict['dst_rack']} is inconclusive: "
            f"source representative {verdict['src']} has a definitive phase-1 failure"
        )
    elif verdict["state"] == "missing":
        note = (
            f"expected rack-pair path {verdict['src_rack']} -> {verdict['dst_rack']} has no "
            f"record from representative {verdict['src']}"
        )
    else:
        note = (
            f"rack-pair path {verdict['src_rack']} -> {verdict['dst_rack']} was not determined "
            f"({verdict.get('status', 'inconclusive')})"
        )
    return {
        "type": "bgp-reachability",
        "note": note,
        "details": {"rack_pair": pair, "source": verdict["src"]},
    }


def _classify_mtu(report, selections, by_sid):
    """Per-direction MTU observations plus a warning on asymmetric values."""
    observed = {}  # (rack_pair tuple) -> {src_rack: mtu_bytes}
    for src_rack, entry in sorted(selections.items()):
        src_sid = entry["source"]
        gated = _rep_unhealthy(by_sid, src_sid)
        for remote in sorted(entry["targets"]):
            rack_pair = [src_rack, remote]
            mtu = _path_record(by_sid, src_sid, "mtu_validator", "cross_rack_mtu", remote)
            if mtu is not None:
                obs = {
                    "type": "cross-rack-mtu",
                    "source_node": mtu.get("source_node"),
                    "source_rack": mtu.get("source_rack"),
                    "target_node": mtu.get("target_node"),
                    "target_rack": mtu.get("target_rack"),
                    "observed_path_mtu_bytes": mtu.get("observed_path_mtu_bytes"),
                    "observation_status": mtu.get("observation_status"),
                }
                if gated:
                    obs["note"] = (
                        f"source representative {src_sid} has a definitive phase-1 failure"
                    )
                report["observations"].append(obs)
                if mtu.get("observation_status") == "success":
                    observed.setdefault(tuple(sorted(rack_pair)), {})[src_rack] = mtu.get(
                        "observed_path_mtu_bytes"
                    )
            if mtu is None or mtu.get("observation_status") in ("timeout", "cancelled"):
                status = mtu.get("observation_status") if mtu else "no record"
                report["inconclusive_checks"].append(
                    {
                        "type": "cross-rack-mtu",
                        "note": (
                            f"expected MTU path {src_rack} -> {remote} was not measured ({status})"
                        ),
                        "details": {"rack_pair": rack_pair, "source": src_sid},
                    }
                )
    for pair, values in sorted(observed.items()):
        if len(values) == 2 and len(set(values.values())) > 1:
            report["warnings"].append(
                {
                    "type": "mtu-asymmetry",
                    "scope": "rack-pair",
                    "message": (
                        f"rack-pair {pair[0]} <-> {pair[1]} reports disagreeing path MTU "
                        f"per direction: {values}"
                    ),
                    "details": {"rack_pair": list(pair), "observed": values},
                }
            )


def _classify_cross_rack(report, topology, by_sid, passed):
    """Representative-sampled rack-pair checks with directional reconciliation.

    BGP results are reconciled across both directions of each rack-pair link
    before assigning final confidence (both fail -> one inferred failure; one
    fails with a healthy reverse -> directional warning; missing/gated/timeout
    -> inconclusive). MTU records become informational observations with a
    warning on asymmetric values. Source and target phase-1 health gate the
    cross-rack verdicts.
    """
    try:
        selections = representatives.select_representatives(topology)
    except ValueError:
        return
    links = set()
    for src_rack, entry in selections.items():
        for remote in entry["targets"]:
            links.add(tuple(sorted([src_rack, remote])))
    fallback_warned = set()
    for a, b in sorted(links):
        _reconcile_bgp_link(report, selections, by_sid, a, b, passed, fallback_warned)
    _classify_mtu(report, selections, by_sid)


def _classify_mac_manifest(report, manifest, by_sid):
    """Cross-reference observed switch-port MACs against the MAC manifest.

    The manifest maps a switch port (``<actor_system_id>:<actor_port>`` from
    the captured LACP PDU) to the host member MAC expected on that port. When a
    bond member's observed MAC differs from the manifest entry for the switch
    port it landed on, the cable is swapped relative to the design. This is an
    informational finding, not a failure: asymmetric swaps are already caught
    definitively by the bond-validator; the manifest catches symmetric swaps
    that LACP PDU comparison alone cannot see.
    """
    ports = manifest.get("ports", manifest)
    for output in by_sid.values():
        node = output["node"]
        mac_by_iface = {
            iface["name"]: iface.get("mac", "").lower() for iface in node.get("interfaces", [])
        }
        section = output.get("bond_validator") or {}
        for bond in section.get("bonds", []):
            for member in bond.get("members", []):
                pdus = member.get("pdus") or []
                if not pdus:
                    continue
                pdu = pdus[0]
                port_id = f"{pdu['actor_system_id']}:{pdu['actor_port']}"
                if port_id not in ports:
                    continue
                expected = ports[port_id].lower()
                observed = mac_by_iface.get(member["interface"], "")
                if observed and observed != expected:
                    report["observations"].append(
                        {
                            "type": "symmetric-bond-swap",
                            "classification": "informational",
                            "node": node["hostname"],
                            "interface": member["interface"],
                            "switch_port": port_id,
                            "expected_mac": expected,
                            "observed_mac": observed,
                        }
                    )


def generate_report(
    probe_outputs,
    missing_nodes=(),
    verbose=False,
    topology=None,
    mac_manifest=None,
    now=time.localtime,
):
    """Build the report document from collected per-unit probe outputs.

    probe_outputs: iterable of parsed probe-output documents.
    missing_nodes: iterable of {system_id, hostname, reason} entries.
    topology: optional topology document; when given, observations are
    diffed against the expected check universe it defines.
    """
    probe_outputs = list(probe_outputs)
    report = {
        "schema_version": schemas.SCHEMA_VERSION,
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%S", now()),
        "summary": {},
        "definitive_failures": [],
        "inferred_failures": [],
        "warnings": [],
        "inconclusive_checks": [],
        "skipped_checks": [],
        "observations": [],
        "missing_nodes": list(missing_nodes),
    }
    for output in probe_outputs:
        node = output["node"]
        if output["status"] != "complete":
            report["inconclusive_checks"].append(
                {
                    "type": "probe-incomplete",
                    "node": node["hostname"],
                    "system_id": node["system_id"],
                    "note": f"probe ended with status {output['status']}; results are partial",
                }
            )
        for section_name in schemas.VALIDATOR_SECTIONS:
            section = output.get(section_name, {})
            for finding in section.get("findings", []):
                entry = dict(finding)
                entry.setdefault("node", node["hostname"])
                field = CLASSIFICATION_FIELDS.get(finding.get("classification"), "warnings")
                report[field].append(entry)
    for entry in report["missing_nodes"]:
        report["inconclusive_checks"].append(
            {
                "type": "node-missing",
                "node": entry.get("hostname", "unknown"),
                "system_id": entry.get("system_id", "unknown"),
                "note": f"expected node did not report: {entry.get('reason', 'unknown')}",
            }
        )
    passed = []
    by_sid = {output["node"]["system_id"]: output for output in probe_outputs}
    if topology is not None:
        _classify_vlan_edges(report, topology, by_sid, passed)
        _classify_cross_rack(report, topology, by_sid, passed)
    if mac_manifest is not None:
        _classify_mac_manifest(report, mac_manifest, by_sid)
    report["summary"] = {
        "passed_count": len(passed),
        "failed": len(report["definitive_failures"]),
        "skipped": len(report["skipped_checks"]),
        "inconclusive": len(report["inconclusive_checks"]),
        "warnings": len(report["warnings"]),
    }
    if verbose:
        report["passed_checks"] = passed
    schemas.ensure_valid(report, schemas.validate_report, "report")
    return report


def _entry_line(entry):
    if "hint" in entry:
        return f"{entry.get('node', '?')}: {entry.get('type', 'finding')}: {entry['hint']}"
    if "note" in entry:
        return f"{entry.get('node', '?')}: {entry.get('type', '')}: {entry['note']}"
    return json.dumps(entry, sort_keys=True)


def text_summary(report):
    """Human-readable summary; section order is fixed by the report spec."""
    lines = []
    for title, field in TEXT_SECTIONS:
        entries = report[field]
        if not entries:
            continue
        lines.append(f"{title} ({len(entries)}):")
        lines.extend(f"  {_entry_line(entry)}" for entry in entries)
    if report["missing_nodes"]:
        lines.append(f"MISSING NODES ({len(report['missing_nodes'])}):")
        lines.extend(
            f"  {entry['hostname']} ({entry['system_id']}): {entry['reason']}"
            for entry in report["missing_nodes"]
        )
    passed = report["summary"]["passed_count"]
    if report["definitive_failures"]:
        lines.append(f"Passed checks: {passed}")
    else:
        lines.append(f"All {passed} checks passed.")
    return "\n".join(lines) + "\n"


def save_report(report, directory=None):
    """Write network-test-<timestamp>.json/.txt and print the text summary."""
    directory = Path(directory) if directory is not None else Path.cwd()
    stamp = report["generated_at"]
    json_path = directory / f"network-test-{stamp}.json"
    text_path = directory / f"network-test-{stamp}.txt"
    summary = text_summary(report)
    json_path.write_text(json.dumps(report, indent=2) + "\n")
    text_path.write_text(summary)
    print(summary, end="")
    return json_path, text_path


def exit_code(report):
    """0 clean; 1 definitive failures; 2 non-definitive issues present."""
    if report["definitive_failures"]:
        return 1
    if report["inferred_failures"] or report["warnings"] or report["inconclusive_checks"]:
        return 2
    return 0
