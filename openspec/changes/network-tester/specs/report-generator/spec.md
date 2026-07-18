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

For v1 cross-rack MTU and BGP, the active check universe SHALL be rack-pair representative-sampled, not every possible cross-rack data-node pair. The generator SHALL derive one expected directed rack-pair path per source rack representative and remote in-scope rack from the structured `cross-rack-data-routing` rule. Non-representative units that report `skip_reason: "not-rack-representative"` SHALL NOT create skipped or inconclusive checks for unsampled data-node pairs. Expected representative-sampled paths for which no path record exists, because the source representative's unit is in `missing_nodes` or its validator section is `not_started`, SHALL be classified as inconclusive active checks. Generic L3 reachability between all data-role machines remains a topology assumption, but it SHALL NOT be reported as a passed/failed/inconclusive active check in v1 unless a representative rack-pair path record exists for it or is expected by the representative-sampled rule.

#### Scenario: Observed behavior matches expected rule
- **WHEN** a reachability rule says node A SHALL reach node B, and the probe confirms ICMP reachability
- **THEN** the generator SHALL record the rule as `pass`

#### Scenario: Observed behavior violates expected rule
- **WHEN** a reachability rule says node A SHALL reach node B, but the probe shows ICMP unreachable
- **THEN** the generator SHALL record the rule as `fail` with the associated remediation hint from the validator that produced the finding

#### Scenario: Non-representative cross-rack skip is not a coverage gap
- **WHEN** a data node is not the deterministic rack representative and its MTU/BGP validators report `skip_reason: "not-rack-representative"`
- **THEN** the report-generator SHALL NOT create skipped, missing, or inconclusive checks for that node's unsampled cross-rack data-node pairs

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
The report-generator SHALL separate bgp-inference path findings from definitive failures. The generator SHALL consume `bgp_inference.paths[]` records, not infer success from an absence of BGP findings. BGP paths with `diagnosis_confidence: inferred` SHALL appear under `inferred_failures` in JSON and under "Inferred failures (manual verification recommended)" in text output after directional reconciliation. BGP paths with `diagnosis_confidence: inconclusive` or `observation_status: "timeout"` or `"cancelled"`, and expected paths with no record because the source unit is missing or its `bgp_inference` section is `not_started`, SHALL appear under `inconclusive_checks`, not under `inferred_failures`. Reachable paths, including paths that succeeded via fallback target, SHALL be used as positive evidence during directional reconciliation. The fallback-success warning emitted by bgp-inference and any target-representative health annotation for the same unreachable target SHALL be merged into a single `warnings` entry per target representative per rack-pair link, not duplicated. Because only the source-rack representative probes each directed (source rack, target rack) pair, each directed pair has at most one reporting unit. The two directed path records covering the same rack-pair link (A-to-B and B-to-A) SHALL be merged into a single entry per rack-pair link after directional reconciliation, carrying an `observed_by` list of the reporting representatives and preserving each representative's traceroute hop data.

#### Scenario: BGP inference finding included in report
- **WHEN** the bgp-inference module produced a path record with `diagnosis_confidence: inferred`
- **THEN** it SHALL appear under `inferred_failures` in JSON and under "Inferred failures (manual verification recommended)" in text output after directional reconciliation, never under `definitive_failures`

#### Scenario: BGP inconclusive finding included in report
- **WHEN** the bgp-inference module produced a path record with `diagnosis_confidence: inconclusive` or `observation_status` of `timeout` or `cancelled`, or an expected path has no record because the source unit is missing or its section is `not_started`
- **THEN** it SHALL appear under `inconclusive_checks`, never under `definitive_failures` or `inferred_failures`

#### Scenario: Same rack-pair link reported from both directions
- **WHEN** rack A's representative produces a `likely-bgp-failure` finding for the A-to-B direction AND rack B's representative produces a `likely-bgp-failure` finding for the B-to-A direction
- **THEN** the report SHALL contain one `inferred_failures` entry for the A-B rack-pair link with both representatives listed in `observed_by`, not two separate entries

#### Scenario: Representative target failure with fallback success is preserved
- **WHEN** a BGP path record has `reachable: true` and `target_role: "fallback"`
- **THEN** the report SHALL use the path as successful reverse/forward evidence and SHALL surface the probe-emitted warning naming the unreachable representative target as a single `warnings` entry

### Requirement: Reconcile directional rack-pair findings before assigning final confidence
Each rack-pair link (A, B) has up to two probe perspectives: rack A's representative probing rack B, and rack B's representative probing rack A. The report-generator SHALL reconcile the two directions before assigning final confidence using explicit `bgp_inference.paths[]` records. When both directions report failure, the rack-pair entry SHALL be reported under `inferred_failures` (subject to the phase-1 health gating requirement). When one direction reports failure but the reverse direction succeeded (directly or via the fallback target), the report-generator SHALL NOT report a rack-pair failure; because ICMP is round-trip, a working reverse path means the inter-rack path forwards traffic in both directions, so the asymmetry points at the failing source representative. The generator SHALL instead emit a node-scoped warning naming the failing source representative as the suspect, preserving its traceroute hop data. When one direction's results are missing, not started, timed out, cancelled, or the source representative appears in `missing_nodes`, a failing single perspective SHALL be recorded under `inconclusive_checks` with a note that the reverse direction is unavailable, not under `inferred_failures`. When both directions are absent or unattempted, the rack-pair SHALL be recorded as inconclusive only if the representative-sampled rule expected that rack-pair; no per-node-pair inconclusive checks are generated.

#### Scenario: Both directions fail
- **WHEN** rack A's representative reports the A-to-B direction unreachable AND rack B's representative reports the B-to-A direction unreachable
- **THEN** the report SHALL contain one `inferred_failures` entry for the A-B rack-pair link

#### Scenario: One direction fails, reverse direction succeeds
- **WHEN** rack A's representative reports the A-to-B direction unreachable but rack B's representative reached rack A directly or via the fallback target
- **THEN** the report SHALL NOT contain a rack-pair failure for A-B; it SHALL contain a `warnings` entry naming rack A's representative as the suspect node, with that node's traceroute hop data attached

#### Scenario: One direction missing, the other fails
- **WHEN** rack A's representative reports the A-to-B direction unreachable and rack B's representative produced no usable bgp-inference path result because its unit is missing, not started, timed out, or cancelled
- **THEN** the A-B rack-pair SHALL be recorded under `inconclusive_checks` with a note that the reverse direction is unavailable, not under `inferred_failures`

### Requirement: Downgrade cross-rack findings from a source representative with definitive phase-1 failures
If a rack's source representative has one or more definitive phase-1 failures (bond-validator or vlan-neighbor-validator findings classified in `definitive_failures`), the report-generator SHALL move that representative's bgp-inference path findings to `inconclusive_checks`, annotated with a reference to the node's phase-1 failure, and SHALL annotate that rack's outbound MTU observations with the same reference. A broken NIC, bond, or VLAN assignment on the source representative SHALL NOT present as a fabric-wide BGP or routing failure.

#### Scenario: Source representative has a definitive bond failure
- **WHEN** rack A's representative has a `bond-mode-mismatch` finding in `definitive_failures` and also produced `likely-bgp-failure` findings for remote racks
- **THEN** those BGP findings SHALL appear under `inconclusive_checks` with a reference to the bond failure on the source representative, not under `inferred_failures`

### Requirement: Annotate cross-rack observations affected by target representative health
If a target rack representative has one or more definitive phase-1 failures, the report-generator SHALL annotate BGP and MTU observations that targeted that representative with a reference to the target node fault. For BGP, a successful fallback target SHALL keep the rack-pair path usable for directional reconciliation but SHALL add a node-scoped warning naming the unhealthy target representative; this warning SHALL be merged with the probe-emitted fallback warning for the same target into one `warnings` entry, annotated with the phase-1 failure reference. If no fallback target succeeds or exists and the failed BGP path targeted an unhealthy representative, the generator SHALL NOT promote that single target-representative failure to a rack-pair inferred failure by itself; it SHALL downgrade the affected direction to `inconclusive_checks` or a node-scoped target warning unless independent healthy evidence from the opposite direction and other validator results supports a fabric-wide rack-pair failure. For MTU, because v1 uses a single deterministic target and no MTU fallback, observations involving an unhealthy target representative SHALL be annotated as target-health-affected; if the MTU result is inconclusive, the report SHALL explain that the target representative health prevents treating the rack-pair MTU as measured.

#### Scenario: BGP fallback succeeds because target representative is unhealthy
- **WHEN** a BGP path reaches a fallback target and the remote representative has definitive phase-1 failures
- **THEN** the report SHALL use the path as successful rack-pair evidence and SHALL emit one merged warning naming the unreachable target representative and referencing its phase-1 failure, not separate fallback and target-health entries

#### Scenario: BGP target representative is unhealthy and no fallback succeeds
- **WHEN** a BGP path fails against an unhealthy target representative and no fallback target succeeds or exists
- **THEN** the report SHALL annotate the failed direction with the target representative fault and SHALL classify it as inconclusive or node-scoped target warning unless independent healthy evidence supports a rack-pair fabric failure

#### Scenario: MTU target representative is unhealthy
- **WHEN** an MTU observation targets a representative that has definitive phase-1 failures
- **THEN** the observation SHALL be annotated with the target representative fault; if no MTU was measured, it SHALL be reported as inconclusive/unavailable due to target representative health, not as a rack-pair MTU measurement

### Requirement: Accept MAC-to-port manifest via CLI flag for optional symmetric swap detection
If `--mac-manifest <path>` is passed to the CLI, the report-generator SHALL load the manifest JSON from that path and compare observed MAC addresses per interface (from collected probe outputs) against the manifest, flagging symmetric bond cable swaps where the switch port has swapped cables relative to the manifest. The manifest is not distributed to nodes; it is read locally by the CLI.

#### Scenario: MAC manifest provided, symmetric swap detected
- **WHEN** `--mac-manifest` is provided and a bond member interface's observed MAC does not match the expected MAC for that port
- **THEN** the generator SHALL record a `symmetric-bond-swap` finding (informational, not a failure) with the expected and observed MACs

### Requirement: Include MTU observations as informational results, not check verdicts
MTU probe results from the mtu-validator SHALL be included in the `observations` list in the JSON report. Each entry SHALL include the source node, target node, source rack, target rack, `observed_path_mtu_bytes` (nullable), and `observation_status`. MTU observations SHALL NOT appear in `definitive_failures`, `inferred_failures`, or `passed_checks`; no pass/fail verdict is assigned in v1. `observation_status: "inconclusive"` is informational and SHALL NOT affect the exit code. When both directions of the same rack-pair link report an observed MTU and the values disagree, the generator SHALL add a `warnings` entry noting the asymmetric observation. When an expected representative-sampled MTU path has `observation_status` of `timeout` or `cancelled`, or has no record because the source unit is missing or its `mtu_validator` section is `not_started`, the generator SHALL add an `inconclusive_checks` entry for that rack-pair path because the active check was expected but not attempted.

#### Scenario: MTU observations present
- **WHEN** one or more units returned mtu-validator results
- **THEN** the JSON report SHALL include an `observations` list with one entry per reported rack-pair path record; expected paths with no record are represented via `inconclusive_checks`, not `observations`

#### Scenario: Text summary with MTU observations
- **WHEN** the text summary is printed
- **THEN** MTU observations SHALL appear under an "OBSERVATIONS" section after the failures and warnings sections, listing source, target, observed path MTU, and observation status

#### Scenario: Directional MTU observations disagree
- **WHEN** rack A's representative observed path MTU 9000 toward rack B and rack B's representative observed path MTU 1500 toward rack A
- **THEN** the report SHALL include both observations and a `warnings` entry noting the asymmetric MTU observation for the A-B rack-pair link

#### Scenario: Expected MTU path not attempted
- **WHEN** a representative-sampled MTU path record has `observation_status: "timeout"` or `"cancelled"`, or no record exists because the source unit is missing or its `mtu_validator` section is `not_started`
- **THEN** the report SHALL add an `inconclusive_checks` entry for the unmeasured rack-pair path, including the record in `observations` when one exists
