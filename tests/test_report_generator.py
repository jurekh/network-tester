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
