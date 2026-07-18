"""Golden fixtures validate against the shared schemas; mutations fail."""

import copy

import pytest
from conftest import (
    PROBE_OUTPUT_FIXTURES,
    REPORT_FIXTURES,
    TOPOLOGY_FIXTURES,
    load_fixture,
)

from cli import schemas


def fixture_ids(paths):
    return [p.stem for p in paths]


@pytest.mark.parametrize("path", TOPOLOGY_FIXTURES, ids=fixture_ids(TOPOLOGY_FIXTURES))
def test_topology_fixture_valid(path):
    assert schemas.validate_topology(load_fixture(path)) == []


@pytest.mark.parametrize("path", PROBE_OUTPUT_FIXTURES, ids=fixture_ids(PROBE_OUTPUT_FIXTURES))
def test_probe_output_fixture_valid(path):
    assert schemas.validate_probe_output(load_fixture(path)) == []


@pytest.mark.parametrize("path", REPORT_FIXTURES, ids=fixture_ids(REPORT_FIXTURES))
def test_report_fixture_valid(path):
    assert schemas.validate_report(load_fixture(path)) == []


def test_fixture_coverage():
    """The fixture set covers the combinations required by the spec."""
    names = {p.stem for p in TOPOLOGY_FIXTURES}
    assert {
        "topology_single_rack",
        "topology_two_rack",
        "topology_mixed_scope",
        "topology_mixed_fabric_host",
    } <= names
    assert PROBE_OUTPUT_FIXTURES, "at least one probe-output fixture required"
    assert REPORT_FIXTURES, "at least one report fixture required"


@pytest.fixture
def topology():
    return load_fixture(TOPOLOGY_FIXTURES[0].parent / "topology_two_rack.json")


@pytest.fixture
def probe_output():
    return load_fixture(PROBE_OUTPUT_FIXTURES[0].parent / "probe_output_complete.json")


@pytest.fixture
def report():
    return load_fixture(REPORT_FIXTURES[0].parent / "report_sample.json")


def test_topology_missing_schema_version(topology):
    del topology["schema_version"]
    assert any("schema_version" in e for e in schemas.validate_topology(topology))


def test_topology_wrong_schema_version(topology):
    topology["schema_version"] = "2"
    assert any("schema_version" in e for e in schemas.validate_topology(topology))


def test_topology_missing_required_top_level_keys(topology):
    for key in ("scope", "fabrics", "machines", "reachability_model"):
        broken = copy.deepcopy(topology)
        del broken[key]
        assert any(key in e for e in schemas.validate_topology(broken)), key


def test_topology_bad_role(topology):
    topology["machines"][0]["role"] = "switch"
    assert any(".role" in e for e in schemas.validate_topology(topology))


def test_topology_missing_in_scope(topology):
    del topology["machines"][0]["in_scope"]
    assert any("in_scope" in e for e in schemas.validate_topology(topology))


def test_topology_bond_requires_mode_and_members(topology):
    bond = topology["machines"][2]["interfaces"][0]
    assert bond["type"] == "bond"
    del bond["bond_mode"]
    del bond["bond_members"]
    errors = schemas.validate_topology(topology)
    assert any("bond_mode" in e for e in errors)
    assert any("bond_members" in e for e in errors)


def test_topology_missing_rule(topology):
    del topology["reachability_model"]["rules"]["cross-rack-data-routing"]
    assert any("cross-rack-data-routing" in e for e in schemas.validate_topology(topology))


def test_topology_bad_selection_strategy(topology):
    rule = topology["reachability_model"]["rules"]["cross-rack-data-routing"]
    rule["parameters"]["source_selection"]["strategy"] = "random"
    assert any("source_selection.strategy" in e for e in schemas.validate_topology(topology))


def test_topology_missing_fallback_selection(topology):
    rule = topology["reachability_model"]["rules"]["cross-rack-data-routing"]
    del rule["parameters"]["fallback_selection"]
    assert any("fallback_selection" in e for e in schemas.validate_topology(topology))


def test_probe_output_bad_status(probe_output):
    probe_output["status"] = "done"
    assert any(".status" in e for e in schemas.validate_probe_output(probe_output))


def test_probe_output_missing_validator_section(probe_output):
    for section in schemas.VALIDATOR_SECTIONS:
        broken = copy.deepcopy(probe_output)
        del broken[section]
        assert any(section in e for e in schemas.validate_probe_output(broken)), section


def test_probe_output_bad_validator_status(probe_output):
    probe_output["bond_validator"]["validator_status"] = "ok"
    assert any("validator_status" in e for e in schemas.validate_probe_output(probe_output))


def test_probe_output_skipped_requires_reason(probe_output):
    probe_output["mtu_validator"]["validator_status"] = "skipped"
    probe_output["mtu_validator"]["cross_rack_mtu"] = []
    assert any("skip_reason" in e for e in schemas.validate_probe_output(probe_output))


def test_probe_output_not_started_must_have_empty_paths(probe_output):
    probe_output["bgp_inference"]["validator_status"] = "not_started"
    errors = schemas.validate_probe_output(probe_output)
    assert any("not_started" in e for e in errors)


def test_probe_output_finding_envelope_enforced(probe_output):
    probe_output["bond_validator"]["findings"] = [
        {"type": "bond-mode-mismatch", "classification": "fatal", "scope": "interface"}
    ]
    errors = schemas.validate_probe_output(probe_output)
    assert any("classification" in e for e in errors)
    assert any("hint" in e for e in errors)
    assert any("details" in e for e in errors)


def test_probe_output_bad_observation_status(probe_output):
    probe_output["mtu_validator"]["cross_rack_mtu"][0]["observation_status"] = "passed"
    assert any("observation_status" in e for e in schemas.validate_probe_output(probe_output))


def test_probe_output_bad_target_role(probe_output):
    probe_output["bgp_inference"]["paths"][0]["target_role"] = "primary"
    assert any("target_role" in e for e in schemas.validate_probe_output(probe_output))


def test_report_missing_list_fields(report):
    for field in schemas.REPORT_LIST_FIELDS:
        broken = copy.deepcopy(report)
        del broken[field]
        assert any(field in e for e in schemas.validate_report(broken)), field


def test_report_summary_counts_required(report):
    for key in schemas.SUMMARY_COUNT_FIELDS:
        broken = copy.deepcopy(report)
        del broken["summary"][key]
        assert any(key in e for e in schemas.validate_report(broken)), key


def test_report_bad_missing_node_reason(report):
    report["missing_nodes"][0]["reason"] = "unknown"
    assert any("reason" in e for e in schemas.validate_report(report))


def test_report_failure_entries_must_conform_to_envelope(report):
    report["definitive_failures"].append({"type": "bond-mode-mismatch"})
    errors = schemas.validate_report(report)
    assert any("classification" in e for e in errors)


def test_ensure_valid_raises_with_all_errors(topology):
    del topology["machines"]
    del topology["fabrics"]
    with pytest.raises(ValueError) as excinfo:
        schemas.ensure_valid(topology, schemas.validate_topology, "topology")
    assert "machines" in str(excinfo.value)
    assert "fabrics" in str(excinfo.value)
