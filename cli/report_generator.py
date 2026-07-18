"""Report generator: aggregate per-unit probe outputs into JSON and text reports.

Classifies findings by their classification field, surfaces non-complete
probe statuses as inconclusive checks, and writes the timestamped report
files. When a topology is provided, it also diffs aggregated observations
against the expected check universe derived from the reachability model:
VLAN edges between in-scope same-fabric/VLAN machines (bidirectional
confirmation, skip grouping by out-of-scope peer) and representative-sampled
cross-rack BGP/MTU rack-pair paths. Directional BGP reconciliation and
health gating arrive in stage 7.
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


def _classify_cross_rack(report, topology, by_sid, passed):
    """Representative-sampled rack-pair checks from explicit path records.

    Only the source-rack representative's unit is consulted per directed
    rack pair; non-representative skips never create coverage gaps.
    """
    try:
        selections = representatives.select_representatives(topology)
    except ValueError:
        return
    for source_rack, entry in sorted(selections.items()):
        source_sid = entry["source"]
        for remote in sorted(entry["targets"]):
            rack_pair = [source_rack, remote]
            bgp = _path_record(by_sid, source_sid, "bgp_inference", "paths", remote)
            if bgp is None:
                report["inconclusive_checks"].append(
                    {
                        "type": "bgp-reachability",
                        "note": (
                            f"expected rack-pair path {source_rack} -> {remote} has no "
                            f"record from representative {source_sid}"
                        ),
                        "details": {"rack_pair": rack_pair, "source": source_sid},
                    }
                )
            elif bgp.get("observation_status") == "success":
                passed.append(
                    {"type": "bgp-reachability", "rack_pair": rack_pair, "source": source_sid}
                )
            elif bgp.get("observation_status") == "failure":
                finding = bgp.get("finding")
                if finding and finding.get("classification") == "inferred":
                    entry_doc = dict(finding)
                    entry_doc.setdefault("node", source_sid)
                    report["inferred_failures"].append(entry_doc)
                else:
                    report["inconclusive_checks"].append(
                        {
                            "type": "bgp-reachability",
                            "note": f"rack-pair path {source_rack} -> {remote} failed "
                            "without a classified finding",
                            "details": {"rack_pair": rack_pair, "source": source_sid},
                        }
                    )
            else:  # timeout, cancelled, inconclusive
                report["inconclusive_checks"].append(
                    {
                        "type": "bgp-reachability",
                        "note": (
                            f"rack-pair path {source_rack} -> {remote} was not determined "
                            f"({bgp.get('observation_status')})"
                        ),
                        "details": {"rack_pair": rack_pair, "source": source_sid},
                    }
                )

            mtu = _path_record(by_sid, source_sid, "mtu_validator", "cross_rack_mtu", remote)
            if mtu is not None:
                report["observations"].append(
                    {
                        "type": "cross-rack-mtu",
                        "source_node": mtu.get("source_node"),
                        "source_rack": mtu.get("source_rack"),
                        "target_node": mtu.get("target_node"),
                        "target_rack": mtu.get("target_rack"),
                        "observed_path_mtu_bytes": mtu.get("observed_path_mtu_bytes"),
                        "observation_status": mtu.get("observation_status"),
                    }
                )
            if mtu is None or mtu.get("observation_status") in ("timeout", "cancelled"):
                status = mtu.get("observation_status") if mtu else "no record"
                report["inconclusive_checks"].append(
                    {
                        "type": "cross-rack-mtu",
                        "note": (
                            f"expected MTU path {source_rack} -> {remote} was not "
                            f"measured ({status})"
                        ),
                        "details": {"rack_pair": rack_pair, "source": source_sid},
                    }
                )


def generate_report(
    probe_outputs, missing_nodes=(), verbose=False, topology=None, now=time.localtime
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
    if topology is not None:
        by_sid = {output["node"]["system_id"]: output for output in probe_outputs}
        _classify_vlan_edges(report, topology, by_sid, passed)
        _classify_cross_rack(report, topology, by_sid, passed)
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
