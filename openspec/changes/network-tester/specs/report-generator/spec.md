## ADDED Requirements

### Requirement: Aggregate per-unit probe results and cross-reference observations
The report-generator SHALL load probe output JSON from all units, correlate bidirectional observations (node A saw node B as a neighbor; node B's inbound records confirm), and produce a unified set of findings with confirmed edges marked as bidirectional.

#### Scenario: Both nodes record each other as ICMP-reachable neighbors
- **WHEN** node A records node B as `icmp-reachable: true` AND node B records node A as `icmp-reachable: true`
- **THEN** the generator SHALL mark the A-B link as `confirmed: bidirectional`

#### Scenario: Only one node records the other
- **WHEN** node A records node B as a neighbor but node B's output does not include node A
- **THEN** the generator SHALL mark the observation as `confirmed: unidirectional` and add a warning: "Node A recorded node B but B did not record A - possible asymmetric path or probe timing issue"

### Requirement: Diff aggregated observations against expected topology
The report-generator SHALL compare aggregated observations against the reachability rules derived from the MAAS topology model. It SHALL evaluate active pass/fail/inconclusive checks only for `in_scope: true` machines. Rules involving `in_scope: false` peers SHALL be classified as `skip` and grouped by missing peer.

#### Scenario: Observed behavior matches expected rule
- **WHEN** a reachability rule says node A SHALL reach node B, and the probe confirms ICMP reachability
- **THEN** the generator SHALL record the rule as `pass`

#### Scenario: Observed behavior violates expected rule
- **WHEN** a reachability rule says node A SHALL reach node B, but the probe shows ICMP unreachable
- **THEN** the generator SHALL record the rule as `fail` with the associated remediation hint from the validator that produced the finding

### Requirement: Record skipped checks with grouped remediation hints
Checks where a required peer machine is present in the topology with `in_scope: false` because it was not included in the node selection SHALL be recorded as `skip` in the report. Skips for the same missing peer SHALL be grouped into a single entry rather than one entry per skipped check. Machines absent from the topology are unknown inventory and SHALL NOT be used to generate skip entries.

#### Scenario: Multiple checks skipped due to same missing peer
- **WHEN** 5 reachability checks are skippable because node-07 was not selected
- **THEN** the report SHALL contain one skip entry: "5 checks skipped - add node-07 to test these paths" rather than 5 separate entries

#### Scenario: No skipped checks
- **WHEN** all peers required for checks were included in the node selection with `in_scope: true`
- **THEN** the `skipped_checks` field SHALL be an empty list

### Requirement: Auto-save timestamped report files
The report-generator SHALL automatically write both the JSON report and the text summary to files in the current working directory, named with an ISO 8601 timestamp (e.g. `network-test-2026-05-14T14:32:00.json` and `network-test-2026-05-14T14:32:00.txt`). The text summary SHALL also be printed to stdout. No `--output` flag is required; the files are always saved.

#### Scenario: Report files auto-saved
- **WHEN** report generation completes
- **THEN** both `.json` and `.txt` files SHALL exist in the current directory with the same timestamp prefix, and the text summary SHALL have been printed to stdout

### Requirement: Classify unexpected-l2-neighbor findings as definitive failures
The report-generator SHALL classify `unexpected-l2-neighbor` findings in `definitive_failures`. A node responding to ARP on a VLAN where it should not be present indicates a misconfiguration requiring investigation.

#### Scenario: unexpected-l2-neighbor finding classified
- **WHEN** a validator produced an `unexpected-l2-neighbor` finding
- **THEN** it SHALL appear in `definitive_failures` with hint "Unexpected L2 neighbor MAC X (IP Y) on interface Z; this device is not in the expected topology for this VLAN"

### Requirement: Produce a structured JSON report
The report-generator SHALL write a JSON report containing: `schema_version`, `generated_at` timestamp, `summary` (with integer counts: `passed_count`, `failed`, `skipped`, `inconclusive`, `warnings`), `definitive_failures` list, `inferred_failures` list (BGP), `warnings` list (unidirectional observations and other anomalies that are not definitive failures), `inconclusive_checks` list (active checks whose result could not be determined), `observations` list (informational MTU results with no pass/fail verdict), `skipped_checks` list, and `missing_nodes` list. Each `missing_nodes` entry SHALL carry a `reason` field with one of: `placement-failed` (Juju could not allocate or place the machine), `deployment-timeout` (unit never reached active/idle before the trigger), `probe-timeout` (unit was triggered but did not return to active/idle before the post-trigger wait expired), or `no-probe-output` (unit responded but `collect-results` returned `status: missing`). The `passed_checks` array is emitted as a top-level field only when `--verbose` is passed; it is absent otherwise.

#### Scenario: Report written successfully
- **WHEN** aggregation and diffing complete without fatal errors
- **THEN** a valid JSON file SHALL be written with the auto-generated timestamped filename containing all required top-level fields

#### Scenario: Missing nodes recorded
- **WHEN** one or more expected machines did not deploy or did not return probe results
- **THEN** those machines SHALL be listed in `missing_nodes` with their MAAS system_id, hostname, and a `reason` distinguishing placement failure from deployment timeout, probe timeout, or absent probe output

#### Scenario: passed_checks in verbose mode
- **WHEN** `--verbose` is passed to the CLI
- **THEN** the JSON report SHALL include a top-level `passed_checks` array with one entry per passed check

#### Scenario: passed_checks in default (non-verbose) mode
- **WHEN** `--verbose` is not specified
- **THEN** the JSON report SHALL NOT include a `passed_checks` field; `summary.passed_count` carries the integer count; the text summary SHALL print only failures, warnings, skips, and the total pass count

### Requirement: Produce a human-readable text summary alongside the JSON report
The report-generator SHALL print a text summary to stdout listing failures, warnings, skips, observations, and the total pass count. Full pass list is omitted unless `--verbose` is specified. The text summary sections appear in order: FAILED CHECKS, INFERRED FAILURES, WARNINGS, SKIPPED CHECKS, OBSERVATIONS, then the pass count.

#### Scenario: Failures present
- **WHEN** one or more checks fail
- **THEN** stdout SHALL include a "FAILED CHECKS" section listing each failure with node identifiers, failure type, and remediation hint

#### Scenario: All checks pass
- **WHEN** no failures are found
- **THEN** stdout SHALL print "All N checks passed." followed by a skips section if any checks were skipped

### Requirement: Exit with defined exit codes
The CLI SHALL exit with a code that reflects the overall result: 0 if zero definitive failures, zero inferred failures, zero warnings, and zero inconclusive active checks; 1 if any definitive failures; 2 if zero definitive failures but inferred failures, warnings, or inconclusive active checks are present. An inconclusive active check is a pass/fail check on an `in_scope: true` machine whose result could not be determined because the relevant probe data is missing or partial (for example, a unit reported `status: timeout`, or a peer is listed in `missing_nodes`); these are the entries of the `inconclusive_checks` list. Informational observations, including inconclusive MTU observations, SHALL NOT affect the exit code. Exit codes SHALL be documented in the README.

#### Scenario: All definitive checks pass, no warnings
- **WHEN** `definitive_failures`, `inferred_failures`, and `warnings` are empty, and there are no inconclusive active checks
- **THEN** the process SHALL exit 0

#### Scenario: One or more definitive failures
- **WHEN** `definitive_failures` is non-empty
- **THEN** the process SHALL exit 1

#### Scenario: No definitive failures but non-definitive issues present
- **WHEN** `definitive_failures` is empty but `inferred_failures` is non-empty, `warnings` is non-empty, or inconclusive active checks exist
- **THEN** the process SHALL exit 2

### Requirement: Label BGP findings separately from definitive failures
The report-generator SHALL separate bgp-inference findings from definitive failures. BGP findings with `diagnosis_confidence: inferred` SHALL appear under `inferred_failures` in JSON and under "Inferred failures (manual verification recommended)" in text output. BGP findings with `diagnosis_confidence: inconclusive` SHALL appear under `inconclusive_checks`, not under `inferred_failures`. Because per-unit BGP findings are rack-pair scoped, findings from multiple units that share the same (source rack, target rack) pair SHALL be aggregated into a single entry per rack pair, carrying an `observed_by` list of the reporting nodes and preserving each node's traceroute hop data.

#### Scenario: BGP inference finding included in report
- **WHEN** the bgp-inference module produced a finding with `diagnosis_confidence: inferred`
- **THEN** it SHALL appear under `inferred_failures` in JSON and under "Inferred failures (manual verification recommended)" in text output, never under `definitive_failures`

#### Scenario: BGP inconclusive finding included in report
- **WHEN** the bgp-inference module produced a finding with `diagnosis_confidence: inconclusive`
- **THEN** it SHALL appear under `inconclusive_checks`, never under `definitive_failures` or `inferred_failures`

#### Scenario: Same rack-pair failure observed by multiple units
- **WHEN** three data nodes in rack A each produce a `likely-bgp-failure` finding for the rack-A-to-rack-B pair
- **THEN** the report SHALL contain one `inferred_failures` entry for the rack-A-to-rack-B pair with all three nodes listed in `observed_by`, not three separate entries

### Requirement: Accept MAC-to-port manifest via CLI flag for optional symmetric swap detection
If `--mac-manifest <path>` is passed to the CLI, the report-generator SHALL load the manifest JSON from that path and compare observed MAC addresses per interface (from collected probe outputs) against the manifest, flagging symmetric bond cable swaps where the switch port has swapped cables relative to the manifest. The manifest is not distributed to nodes; it is read locally by the CLI.

#### Scenario: MAC manifest provided, symmetric swap detected
- **WHEN** `--mac-manifest` is provided and a bond member interface's observed MAC does not match the expected MAC for that port
- **THEN** the generator SHALL record a `symmetric-bond-swap` finding (informational, not a failure) with the expected and observed MACs

### Requirement: Include MTU observations as informational results, not check verdicts
MTU probe results from the mtu-validator SHALL be included in the `observations` list in the JSON report. Each entry SHALL include the source node, target node, and `observed_path_mtu_bytes` (or `null` with `status: "inconclusive"`). MTU observations SHALL NOT appear in `definitive_failures`, `inferred_failures`, or `passed_checks`; no pass/fail verdict is assigned in v1.

#### Scenario: MTU observations present
- **WHEN** one or more units returned mtu-validator results
- **THEN** the JSON report SHALL include an `observations` list with one entry per probed peer pair

#### Scenario: Text summary with MTU observations
- **WHEN** the text summary is printed
- **THEN** MTU observations SHALL appear under an "OBSERVATIONS" section after the failures and warnings sections, listing source, target, and observed path MTU
