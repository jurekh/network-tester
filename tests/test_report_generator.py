"""Report generator: aggregation, file output, text summary, exit codes."""

import json

from conftest import FIXTURES, load_fixture

from cli import report_generator, schemas

COMPLETE = FIXTURES / "probe_output_complete.json"
FINDINGS = FIXTURES / "probe_output_findings.json"
TIMEOUT = FIXTURES / "probe_output_timeout.json"


def test_clean_run_produces_schema_valid_report():
    report = report_generator.generate_report([load_fixture(COMPLETE)])
    assert schemas.validate_report(report) == []
    assert report["schema_version"] == "1"
    assert report["generated_at"]
    assert report["definitive_failures"] == []
    assert report["missing_nodes"] == []
    assert report["summary"] == {
        "passed_count": 0,
        "failed": 0,
        "skipped": 0,
        "inconclusive": 0,
        "warnings": 0,
    }
    assert report_generator.exit_code(report) == 0


def test_definitive_findings_classified_and_exit_one():
    report = report_generator.generate_report([load_fixture(FINDINGS)])
    types = {f["type"] for f in report["definitive_failures"]}
    assert types == {"bond-mode-mismatch", "unexpected-l2-neighbor"}
    assert all(f["node"] == "r1-data-01" for f in report["definitive_failures"])
    assert report["summary"]["failed"] == 2
    assert report_generator.exit_code(report) == 1


def test_timeout_output_is_inconclusive_and_exit_two():
    report = report_generator.generate_report([load_fixture(TIMEOUT)])
    assert report["definitive_failures"] == []
    notes = [e["note"] for e in report["inconclusive_checks"]]
    assert any("status timeout" in n for n in notes)
    assert report["summary"]["inconclusive"] == 1
    assert report_generator.exit_code(report) == 2


def test_missing_nodes_recorded_with_reason():
    missing = [{"system_id": "aaa002", "hostname": "r1-data-02", "reason": "deployment-timeout"}]
    report = report_generator.generate_report([load_fixture(COMPLETE)], missing_nodes=missing)
    assert report["missing_nodes"] == missing
    assert schemas.validate_report(report) == []
    summary = report_generator.text_summary(report)
    assert "MISSING NODES (1):" in summary
    assert "r1-data-02 (aaa002): deployment-timeout" in summary
    # a missing expected node is an inconclusive active check, not a clean pass
    notes = [e["note"] for e in report["inconclusive_checks"]]
    assert any("deployment-timeout" in n for n in notes)
    assert report["summary"]["inconclusive"] == 1
    assert report_generator.exit_code(report) == 2


def test_verbose_includes_passed_checks_default_omits():
    outputs = [load_fixture(COMPLETE)]
    assert "passed_checks" not in report_generator.generate_report(outputs)
    verbose = report_generator.generate_report(outputs, verbose=True)
    assert verbose["passed_checks"] == []


def test_text_summary_sections_in_spec_order():
    report = report_generator.generate_report([load_fixture(FINDINGS), load_fixture(TIMEOUT)])
    summary = report_generator.text_summary(report)
    failed = summary.index("FAILED CHECKS")
    inconclusive_absent = "INCONCLUSIVE" not in summary  # no dedicated text section
    assert failed >= 0 and inconclusive_absent
    assert summary.rstrip().endswith("Passed checks: 0")


def test_clean_summary_prints_all_passed():
    report = report_generator.generate_report([load_fixture(COMPLETE)])
    assert report_generator.text_summary(report).rstrip() == "All 0 checks passed."


def test_save_report_writes_timestamped_files_and_prints(tmp_path, capsys):
    report = report_generator.generate_report([load_fixture(COMPLETE)])
    json_path, text_path = report_generator.save_report(report, directory=tmp_path)
    stamp = report["generated_at"]
    assert json_path.name == f"network-test-{stamp}.json"
    assert text_path.name == f"network-test-{stamp}.txt"
    assert json.loads(json_path.read_text()) == report
    out = capsys.readouterr().out
    assert text_path.read_text() == out
    assert "All 0 checks passed." in out


# --- classification core: expected-universe diffing (5.8-5.11) ----------------------

TOPOLOGY = load_fixture(FIXTURES / "topology_mixed_scope.json")


def vlan_observation(peer_sid, peer_ip, interface="eth0"):
    return {
        "type": "expected-peer-observed",
        "interface": interface,
        "peer_system_id": peer_sid,
        "peer_ip": peer_ip,
        "peer_mac": "52:54:00:00:00:00",
        "arp_observed": True,
        "icmp_reachable": True,
        "rtt_ms": {"min": 0.2, "avg": 0.3, "max": 0.5},
        "loss_pct": 0,
    }


def probe_doc(sid, hostname, vlan=None, bgp=None, mtu=None, representative=False):
    """Synthetic probe output with given vlan observations and path records."""
    skipped = {
        "validator_status": "skipped",
        "skip_reason": "not-rack-representative",
        "findings": [],
    }
    return {
        "schema_version": "1",
        "status": "complete",
        "node": {"system_id": sid, "hostname": hostname, "interfaces": []},
        "bond_validator": {"validator_status": "complete", "bonds": [], "findings": []},
        "vlan_neighbor_validator": {
            "validator_status": "complete",
            "findings": (vlan or {}).get("findings", []),
            "observations": (vlan or {}).get("observations", []),
        },
        "mtu_validator": (
            {"validator_status": "complete", "cross_rack_mtu": mtu or [], "findings": []}
            if representative
            else dict(skipped, cross_rack_mtu=[])
        ),
        "bgp_inference": (
            {"validator_status": "complete", "paths": bgp or [], "findings": []}
            if representative
            else dict(skipped, paths=[])
        ),
    }


def bgp_path(source_rack, source, target_rack, target, status="success", reachable=True):
    return {
        "source_rack": source_rack,
        "source_node": source,
        "target_rack": target_rack,
        "representative_target": target,
        "fallback_target": None,
        "reachable": reachable,
        "target_role": "representative" if reachable else None,
        "observation_status": status,
    }


def mtu_record(source_rack, source, target_rack, target, mtu=9000, status="success"):
    return {
        "source_rack": source_rack,
        "source_node": source,
        "target_rack": target_rack,
        "target_node": target,
        "observed_path_mtu_bytes": mtu,
        "observation_status": status,
    }


def three_node_outputs():
    """aaa001+aaa002 (rack-1) and bbb002 (rack-2) report; bbb003 is missing."""
    return [
        probe_doc(
            "aaa001",
            "r1-data-01",
            vlan={
                "observations": [
                    vlan_observation("aaa002", "10.20.1.12", "bond0"),
                    {
                        "type": "known-out-of-scope-peer-observed",
                        "interface": "bond0",
                        "peer_system_id": "aaa003",
                        "peer_mac": "52:54:00:01:03:01",
                        "skipped": True,
                    },
                ]
            },
            bgp=[bgp_path("rack-1", "aaa001", "rack-2", "bbb002")],
            mtu=[mtu_record("rack-1", "aaa001", "rack-2", "bbb002")],
            representative=True,
        ),
        probe_doc(
            "aaa002",
            "r1-data-02",
            vlan={"observations": [vlan_observation("aaa001", "10.20.1.11")]},
        ),
        probe_doc(
            "bbb002",
            "r2-data-02",
            vlan={"observations": [vlan_observation("bbb003", "10.20.2.13")]},
            bgp=[bgp_path("rack-2", "bbb002", "rack-1", "aaa001")],
            mtu=[mtu_record("rack-2", "bbb002", "rack-1", "aaa001", mtu=1500)],
            representative=True,
        ),
    ]


def test_bidirectional_confirmation_and_pass_counting():
    missing = [{"system_id": "bbb003", "hostname": "r2-data-03", "reason": "no-probe-output"}]
    report = report_generator.generate_report(
        three_node_outputs(), missing_nodes=missing, topology=TOPOLOGY, verbose=True
    )
    assert schemas.validate_report(report) == []
    assert report["definitive_failures"] == []
    passed = {(e["type"], e.get("confirmed")) for e in report["passed_checks"]}
    # vlan aaa001<->aaa002 bidirectional; vlan bbb002->bbb003 unidirectional;
    # bgp rack-1->rack-2 and rack-2->rack-1
    assert ("vlan-reachability", "bidirectional") in passed
    assert ("vlan-reachability", "unidirectional") in passed
    assert len([e for e in report["passed_checks"] if e["type"] == "bgp-reachability"]) == 2
    assert report["summary"]["passed_count"] == 4
    # unidirectional edge produces a warning naming both nodes
    uni = [w for w in report["warnings"] if w["type"] == "unidirectional-observation"]
    assert len(uni) == 1
    assert "r2-data-02" in uni[0]["message"] and "r2-data-03" in uni[0]["message"]


def test_skips_grouped_by_missing_peer():
    report = report_generator.generate_report(three_node_outputs(), topology=TOPOLOGY)
    skips = {e["peer"]: e for e in report["skipped_checks"]}
    # aaa003 blocks 2 rack-1 edges; bbb001 blocks 2 rack-2 edges
    assert skips["aaa003"]["count"] == 2
    assert skips["bbb001"]["count"] == 2
    assert skips["aaa003"]["message"] == "2 checks skipped - add aaa003 to test these paths"
    assert report["summary"]["skipped"] == 2


def test_vlan_edge_failure_not_counted_as_pass():
    outputs = three_node_outputs()
    outputs[0]["vlan_neighbor_validator"]["observations"] = []
    outputs[0]["vlan_neighbor_validator"]["findings"] = [
        {
            "type": "missing-l2-neighbor",
            "classification": "definitive",
            "scope": "interface",
            "hint": "Expected L2 neighbor r1-data-02 (10.20.1.12) did not respond to ARP",
            "details": {"interface": "bond0", "peer_system_id": "aaa002"},
        }
    ]
    outputs[1]["vlan_neighbor_validator"]["observations"] = []
    report = report_generator.generate_report(outputs, topology=TOPOLOGY, verbose=True)
    assert [f["type"] for f in report["definitive_failures"]] == ["missing-l2-neighbor"]
    assert not [
        e
        for e in report["passed_checks"]
        if e["type"] == "vlan-reachability" and set(e["nodes"]) == {"r1-data-01", "r1-data-02"}
    ]
    assert report_generator.exit_code(report) == 1


def test_vlan_edge_without_any_data_is_inconclusive():
    outputs = three_node_outputs()
    outputs[0]["vlan_neighbor_validator"]["observations"] = []
    outputs[1]["vlan_neighbor_validator"]["validator_status"] = "not_started"
    outputs[1]["vlan_neighbor_validator"]["observations"] = []
    report = report_generator.generate_report(outputs, topology=TOPOLOGY)
    entries = [e for e in report["inconclusive_checks"] if e["type"] == "vlan-reachability"]
    assert len(entries) == 1
    assert "r1-data-01" in entries[0]["note"] and "r1-data-02" in entries[0]["note"]


def test_expected_bgp_path_without_record_is_inconclusive():
    # rack-2 representative bbb002 never reports: its expected path is inconclusive
    outputs = three_node_outputs()[:2]
    missing = [{"system_id": "bbb002", "hostname": "r2-data-02", "reason": "probe-timeout"}]
    report = report_generator.generate_report(
        outputs, missing_nodes=missing, topology=TOPOLOGY, verbose=True
    )
    entries = [e for e in report["inconclusive_checks"] if e["type"] == "bgp-reachability"]
    assert len(entries) == 1
    # rack_pair is the (sorted) link; the note text carries the missing direction
    assert entries[0]["details"]["rack_pair"] == ["rack-1", "rack-2"]
    assert "rack-2 -> rack-1" in entries[0]["note"]
    # the reported direction still passes
    assert len([e for e in report["passed_checks"] if e["type"] == "bgp-reachability"]) == 1


def test_mtu_records_become_observations_and_timeouts_inconclusive():
    outputs = three_node_outputs()
    outputs[2]["mtu_validator"]["cross_rack_mtu"] = [
        mtu_record("rack-2", "bbb002", "rack-1", "aaa001", mtu=None, status="timeout")
    ]
    report = report_generator.generate_report(outputs, topology=TOPOLOGY)
    obs = [o for o in report["observations"] if o["type"] == "cross-rack-mtu"]
    assert len(obs) == 2
    assert {o["observation_status"] for o in obs} == {"success", "timeout"}
    entries = [e for e in report["inconclusive_checks"] if e["type"] == "cross-rack-mtu"]
    assert len(entries) == 1
    assert entries[0]["details"]["rack_pair"] == ["rack-2", "rack-1"]


def test_non_representative_skips_create_no_coverage_gaps():
    report = report_generator.generate_report(three_node_outputs(), topology=TOPOLOGY)
    assert not [
        e
        for e in report["inconclusive_checks"]
        if e["type"] in ("bgp-reachability", "cross-rack-mtu")
    ]
    # aaa002's skipped cross-rack sections add nothing to skips either
    assert {e["peer"] for e in report["skipped_checks"]} == {"aaa003", "bbb001"}


# --- MAC manifest symmetric swap detection (6.10) ----------------------------------


def _bond_pdu(switch="aa:bb:cc:dd:ee:01", port=1):
    return {
        "actor_system_id": switch,
        "actor_port_key": 1,
        "actor_port": port,
        "actor_state": {"active": True},
        "partner_system_id": "00:00:00:00:00:00",
        "partner_port_key": 0,
        "partner_state": {"active": False},
    }


def _bond_doc(member_pdus, interfaces):
    members = [
        {"interface": iface, "lacp_advertised": bool(pdus), "pdus": pdus}
        for iface, pdus in member_pdus.items()
    ]
    return {
        "schema_version": "1",
        "status": "complete",
        "node": {"system_id": "aaa001", "hostname": "r1-data-01", "interfaces": interfaces},
        "bond_validator": {
            "validator_status": "complete",
            "findings": [],
            "bonds": [{"bond": "bond0", "members": members}],
        },
        "vlan_neighbor_validator": {
            "validator_status": "complete",
            "findings": [],
            "observations": [],
        },
        "mtu_validator": {"validator_status": "not_started", "findings": [], "cross_rack_mtu": []},
        "bgp_inference": {"validator_status": "not_started", "findings": [], "paths": []},
    }


IFACES = [
    {"name": "eno1", "mac": "52:54:00:01:01:01"},
    {"name": "eno2", "mac": "52:54:00:01:01:02"},
]


def test_mac_manifest_flags_symmetric_swap():
    out = _bond_doc({"eno1": [_bond_pdu(port=1)]}, IFACES)
    # the manifest says port aa:bb:cc:dd:ee:01:1 should carry eno2's MAC, but
    # eno1 (a different MAC) was observed there -> swapped cabling.
    manifest = {"ports": {"aa:bb:cc:dd:ee:01:1": "52:54:00:01:01:02"}}
    report = report_generator.generate_report([out], mac_manifest=manifest)
    swaps = [o for o in report["observations"] if o["type"] == "symmetric-bond-swap"]
    assert len(swaps) == 1
    assert swaps[0]["classification"] == "informational"
    assert swaps[0]["expected_mac"] == "52:54:00:01:01:02"
    assert swaps[0]["observed_mac"] == "52:54:00:01:01:01"
    assert swaps[0]["node"] == "r1-data-01"
    # informational findings do not change the exit code
    assert report_generator.exit_code(report) == 0
    assert schemas.validate_report(report) == []


def test_mac_manifest_no_swap_when_observed_matches_expected():
    out = _bond_doc({"eno1": [_bond_pdu(port=1)]}, IFACES)
    manifest = {"ports": {"aa:bb:cc:dd:ee:01:1": "52:54:00:01:01:01"}}
    report = report_generator.generate_report([out], mac_manifest=manifest)
    assert [o for o in report["observations"] if o["type"] == "symmetric-bond-swap"] == []


def test_no_manifest_skips_swap_detection():
    out = _bond_doc({"eno1": [_bond_pdu(port=1)]}, IFACES)
    report = report_generator.generate_report([out])
    assert [o for o in report["observations"] if o["type"] == "symmetric-bond-swap"] == []


# --- directional reconciliation (7.11-7.16) ----------------------------------------


def bgp_fail_path(source_rack, source, target_rack, target, ftype="likely-bgp-failure"):
    hops = [{"hop": 1, "ip": "10.20.1.1", "rtt_ms": 0.1}]
    return {
        "source_rack": source_rack,
        "source_node": source,
        "target_rack": target_rack,
        "representative_target": target,
        "fallback_target": None,
        "reachable": False,
        "target_role": None,
        "observation_status": "failure",
        "finding": {
            "type": ftype,
            "classification": "inferred",
            "scope": "rack-pair",
            "diagnosis_confidence": "inferred",
            "hint": f"traffic from {source_rack} stops at ToR",
            "details": {"rack_pair": [source_rack, target_rack], "traceroute_hops": hops},
        },
    }


def bgp_fallback_path(source_rack, source, target_rack, rep, fallback):
    return {
        "source_rack": source_rack,
        "source_node": source,
        "target_rack": target_rack,
        "representative_target": rep,
        "fallback_target": fallback,
        "reachable": True,
        "target_role": "fallback",
        "observation_status": "success",
    }


DEFINITIVE_VLAN = {
    "type": "missing-l2-neighbor",
    "classification": "definitive",
    "scope": "interface",
    "hint": "expected peer did not respond",
    "details": {"peer_system_id": "x"},
}


def rep_doc(sid, hostname, bgp=None, mtu=None, phase1_fail=False):
    doc = probe_doc(sid, hostname, bgp=bgp or [], mtu=mtu or [], representative=True)
    if phase1_fail:
        doc["vlan_neighbor_validator"]["findings"] = [dict(DEFINITIVE_VLAN)]
    return doc


def bgp_passes(report):
    return [e for e in report["passed_checks"] if e["type"] == "bgp-reachability"]


def test_both_directions_fail_one_inferred_failure():
    outputs = [
        rep_doc(
            "aaa001", "r1-data-01", bgp=[bgp_fail_path("rack-1", "aaa001", "rack-2", "bbb002")]
        ),
        rep_doc(
            "bbb002", "r2-data-02", bgp=[bgp_fail_path("rack-2", "bbb002", "rack-1", "aaa001")]
        ),
    ]
    report = report_generator.generate_report(outputs, topology=TOPOLOGY, verbose=True)
    assert len(report["inferred_failures"]) == 1
    inf = report["inferred_failures"][0]
    assert inf["scope"] == "rack-pair"
    assert set(inf["observed_by"]) == {"aaa001", "bbb002"}
    assert set(inf["details"]["per_node_traceroute"]) == {"aaa001", "bbb002"}
    assert bgp_passes(report) == []
    assert report_generator.exit_code(report) == 2


def test_one_direction_fails_healthy_reverse_is_warning():
    outputs = [
        rep_doc(
            "aaa001", "r1-data-01", bgp=[bgp_fail_path("rack-1", "aaa001", "rack-2", "bbb002")]
        ),
        rep_doc("bbb002", "r2-data-02", bgp=[bgp_path("rack-2", "bbb002", "rack-1", "aaa001")]),
    ]
    report = report_generator.generate_report(outputs, topology=TOPOLOGY, verbose=True)
    assert report["inferred_failures"] == []
    warns = [w for w in report["warnings"] if w["type"] == "bgp-directional"]
    assert len(warns) == 1 and warns[0]["details"]["node"] == "aaa001"
    assert len(bgp_passes(report)) == 1  # healthy reverse still passes


def test_one_direction_fails_missing_reverse_is_inconclusive():
    outputs = [
        rep_doc(
            "aaa001", "r1-data-01", bgp=[bgp_fail_path("rack-1", "aaa001", "rack-2", "bbb002")]
        ),
    ]
    report = report_generator.generate_report(outputs, topology=TOPOLOGY)
    assert report["inferred_failures"] == []
    inc = [e for e in report["inconclusive_checks"] if e["type"] == "bgp-reachability"]
    assert len(inc) == 1
    assert "cannot confirm a fabric failure" in inc[0]["note"]


def test_fallback_success_emits_single_target_warning():
    outputs = [
        rep_doc(
            "aaa001",
            "r1-data-01",
            bgp=[bgp_fallback_path("rack-1", "aaa001", "rack-2", "bbb002", "bbb003")],
        ),
        rep_doc("bbb002", "r2-data-02", bgp=[bgp_path("rack-2", "bbb002", "rack-1", "aaa001")]),
    ]
    report = report_generator.generate_report(outputs, topology=TOPOLOGY, verbose=True)
    warns = [w for w in report["warnings"] if w["type"] == "target-representative-unreachable"]
    assert len(warns) == 1 and warns[0]["details"]["node"] == "bbb002"
    assert len(bgp_passes(report)) == 2  # both directions positive


def test_source_phase1_failure_gates_bgp_to_inconclusive():
    outputs = [
        rep_doc(
            "aaa001",
            "r1-data-01",
            bgp=[bgp_path("rack-1", "aaa001", "rack-2", "bbb002")],
            mtu=[mtu_record("rack-1", "aaa001", "rack-2", "bbb002")],
            phase1_fail=True,
        ),
        rep_doc("bbb002", "r2-data-02", bgp=[bgp_path("rack-2", "bbb002", "rack-1", "aaa001")]),
    ]
    report = report_generator.generate_report(outputs, topology=TOPOLOGY)
    inc = [e for e in report["inconclusive_checks"] if e["type"] == "bgp-reachability"]
    assert any("definitive phase-1 failure" in e["note"] for e in inc)
    # the gated source's MTU observation is annotated with the same reference
    gated_obs = [o for o in report["observations"] if o.get("source_node") == "aaa001"]
    assert gated_obs and "phase-1 failure" in gated_obs[0].get("note", "")


def test_target_phase1_failure_attributes_failure_to_target():
    # aaa001 -> rack-2 fails toward bbb002, which has its own phase-1 failure
    outputs = [
        rep_doc(
            "aaa001", "r1-data-01", bgp=[bgp_fail_path("rack-1", "aaa001", "rack-2", "bbb002")]
        ),
        rep_doc(
            "bbb002",
            "r2-data-02",
            bgp=[bgp_path("rack-2", "bbb002", "rack-1", "aaa001")],
            phase1_fail=True,
        ),
    ]
    report = report_generator.generate_report(outputs, topology=TOPOLOGY)
    assert report["inferred_failures"] == []
    warns = [w for w in report["warnings"] if w["type"] == "target-representative-unhealthy"]
    assert len(warns) == 1 and warns[0]["details"]["node"] == "bbb002"


def test_mtu_asymmetry_warning():
    outputs = [
        rep_doc(
            "aaa001",
            "r1-data-01",
            bgp=[bgp_path("rack-1", "aaa001", "rack-2", "bbb002")],
            mtu=[mtu_record("rack-1", "aaa001", "rack-2", "bbb002", mtu=9000)],
        ),
        rep_doc(
            "bbb002",
            "r2-data-02",
            bgp=[bgp_path("rack-2", "bbb002", "rack-1", "aaa001")],
            mtu=[mtu_record("rack-2", "bbb002", "rack-1", "aaa001", mtu=1500)],
        ),
    ]
    report = report_generator.generate_report(outputs, topology=TOPOLOGY)
    warns = [w for w in report["warnings"] if w["type"] == "mtu-asymmetry"]
    assert len(warns) == 1
    assert warns[0]["details"]["observed"] == {"rack-1": 9000, "rack-2": 1500}


def test_not_started_bgp_section_is_inconclusive():
    # aaa001 reports but its cross-rack sections never started (empty paths)
    aaa = probe_doc("aaa001", "r1-data-01", representative=True)
    aaa["bgp_inference"] = {"validator_status": "not_started", "paths": [], "findings": []}
    aaa["mtu_validator"] = {
        "validator_status": "not_started",
        "cross_rack_mtu": [],
        "findings": [],
    }
    bbb = rep_doc("bbb002", "r2-data-02", bgp=[bgp_path("rack-2", "bbb002", "rack-1", "aaa001")])
    report = report_generator.generate_report([aaa, bbb], topology=TOPOLOGY)
    inc = [e for e in report["inconclusive_checks"] if e["type"] == "bgp-reachability"]
    # rack-1 -> rack-2 has no record (not_started) -> its direction is inconclusive
    assert any("rack-1 -> rack-2" in e["note"] for e in inc)
    mtu_inc = [e for e in report["inconclusive_checks"] if e["type"] == "cross-rack-mtu"]
    assert any("rack-1 -> rack-2" in e["note"] for e in mtu_inc)
