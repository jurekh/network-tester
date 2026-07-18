"""Report generator: aggregate per-unit probe outputs into JSON and text reports.

Walking-skeleton scope: classify findings by their classification field,
surface non-complete probe statuses as inconclusive checks, and write the
timestamped report files. Rule diffing, bidirectional reconciliation, and
skip grouping arrive with the report classification core (stage 5).
"""

import json
import time
from pathlib import Path

from cli import schemas

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


def generate_report(probe_outputs, missing_nodes=(), verbose=False, now=time.localtime):
    """Build the report document from collected per-unit probe outputs.

    probe_outputs: iterable of parsed probe-output documents.
    missing_nodes: iterable of {system_id, hostname, reason} entries.
    """
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
    report["summary"] = {
        "passed_count": 0,
        "failed": len(report["definitive_failures"]),
        "skipped": len(report["skipped_checks"]),
        "inconclusive": len(report["inconclusive_checks"]),
        "warnings": len(report["warnings"]),
    }
    if verbose:
        report["passed_checks"] = []
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
