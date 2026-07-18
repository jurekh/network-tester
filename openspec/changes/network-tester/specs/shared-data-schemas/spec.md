## ADDED Requirements

### Requirement: Define versioned shared data schemas
The project SHALL define versioned, canonical data models for the topology JSON, per-unit probe-output JSON, and final report JSON before implementing validators or CLI/reporting code. Schemas may be expressed as JSON Schema files, typed Python dataclasses with validation helpers, or Pydantic models, but they SHALL be shared by the CLI, charm payload, validators, and report generator. Each serialized document SHALL include `schema_version: "1"`.

#### Scenario: Topology schema validates required fields
- **WHEN** a topology JSON file is produced by the MAAS topology fetcher
- **THEN** schema validation SHALL verify machine identity, scope, rack, role, interface fabric class, fabric/VLAN/subnet fields, and reachability model fields before the CLI attaches it as a Juju resource

#### Scenario: Probe output schema validates validator output
- **WHEN** a validator returns findings to the probe-runner
- **THEN** schema validation SHALL verify the required top-level fields and validator-specific finding fields before writing `/var/log/network-tester/probe-output.json`

#### Scenario: Report schema validates generated output
- **WHEN** report generation completes
- **THEN** schema validation SHALL verify the final JSON report shape before the file is saved

### Requirement: Define a common finding envelope shared by all validators
The schemas SHALL define a common finding envelope that every validator finding conforms to, with at least: `type` (machine-readable finding identifier such as `bond-mode-mismatch`, `unexpected-l2-neighbor`, `likely-bgp-failure`), `classification` (`definitive`, `inferred`, `inconclusive`, or `informational`), `scope` (`interface`, `node`, or `rack-pair`), `hint` (remediation text), and a `details` object for validator-specific fields (raw PDUs, traceroute hops, observed MTU). The report-generator SHALL route findings into report sections using `classification` and `scope` from the envelope, not per-validator special cases.

#### Scenario: Validator finding conforms to envelope
- **WHEN** any validator emits a finding into probe output
- **THEN** schema validation SHALL verify the envelope fields are present and `classification` and `scope` use the enumerated values

### Requirement: Stamp probe output with the triggering run-id
The probe-output document SHALL carry a `probe_run_id` string field stamped by the probe-runner from the charm-provided run-id, so the CLI collector can reject documents left over from an earlier run (`stale-probe-output` in `missing_nodes`). The field is additive: schema validation SHALL accept documents without it (they predate this field), but the payload always writes it.

#### Scenario: Collector rejects a stale document
- **WHEN** a collected probe-output's `probe_run_id` differs from the run-id the CLI just triggered
- **THEN** the CLI SHALL exclude that document from the report and record the node in `missing_nodes` with reason `stale-probe-output`

### Requirement: Define per-validator execution status and cross-rack path observations
The probe-output schema SHALL require every validator section (`bond_validator`, `vlan_neighbor_validator`, `mtu_validator`, `bgp_inference`) to include a `validator_status` value from `complete`, `skipped`, `not_started`, `timeout`, or `cancelled`, plus optional `skip_reason` or `timeout_reason` fields when applicable. Absence of a failure finding SHALL NOT be used to infer that a validator completed successfully. The cross-rack validators (`mtu_validator`, `bgp_inference`) SHALL additionally emit a structured per-path observation for each expected rack-pair item they derive, with an `observation_status` value from `success`, `failure`, `inconclusive`, `timeout`, or `cancelled`, so the report-generator can distinguish healthy paths from unattempted paths. Phase-1 validators (`bond_validator`, `vlan_neighbor_validator`) carry `validator_status` plus their existing findings and skipped-observation structures; no per-path records are required of them.

For cross-rack validators, the schema SHALL define first-class rack-pair path records. `bgp_inference.paths[]` records SHALL include `source_rack`, `source_node`, `target_rack`, `representative_target`, optional `fallback_target`, `reachable`, `target_role` (`representative`, `fallback`, or `null`), `observation_status`, and any attached finding or traceroute data. `mtu_validator.cross_rack_mtu[]` records SHALL include `source_rack`, `source_node`, `target_rack`, `target_node`, `observed_path_mtu_bytes` (nullable), `observation_status`, and any inconclusive/timeout reason. A started cross-rack validator SHALL emit these records for expected rack-pairs it did not attempt due to timeout or cancellation, with `observation_status: "timeout"` or `"cancelled"`. A validator that never started carries `validator_status: "not_started"` with an empty record list; the report-generator derives the expected rack-pair universe from the structured `cross-rack-data-routing` rule and classifies the missing active checks as inconclusive.

#### Scenario: Validator success is explicit
- **WHEN** a validator completes with no findings
- **THEN** its probe-output section SHALL include `validator_status: "complete"`; the report-generator SHALL NOT infer completion from an empty findings list alone

#### Scenario: Cross-rack path unattempted by a started validator is still represented
- **WHEN** a started representative validator times out or is cancelled before an expected remote rack is probed
- **THEN** the validator SHALL emit a rack-pair path record for that remote rack with `observation_status` set to `timeout` or `cancelled`, allowing the report-generator to classify the missing active check as inconclusive

#### Scenario: Never-started validator carries no synthesized path records
- **WHEN** a cross-rack validator never started before the probe-runner wrote the output
- **THEN** its section SHALL contain `validator_status: "not_started"` with an empty record list, and the report-generator SHALL derive the expected representative-sampled rack-pairs from the topology rule and classify them as inconclusive active checks

### Requirement: Define the reachability_model structure
The topology schema SHALL define the `reachability_model` block as a set of named rule definitions, each with a rule identifier, description, applicable roles, and machine-readable rule parameters. At minimum version 1 SHALL define: `l2-same-fabric-vlan` (machines sharing a fabric name and vlan_tag are expected L2 peers unless a role rule forbids it), `bmc-oam-restricted` (a `bmc-oam` node's expected peer set is only the `rack-controller` in its rack; other same-VLAN adjacency is forbidden), and `cross-rack-data-routing` (data-role machines in different racks are expected L3-reachable via data-fabric interfaces, while v1 active MTU/BGP verdicts are representative-sampled rack-pair checks). The `cross-rack-data-routing` rule SHALL include structured parameters: `probe_scope: "rack-pair-representative"`, `applicable_role: "data"`, `interface_class: "data"`, `source_selection: {"strategy": "lexicographic-lowest", "field": "system_id", "in_scope": true}`, `target_selection: {"strategy": "lexicographic-lowest", "field": "system_id", "in_scope": true}`, and `fallback_selection` for BGP with `strategy: "next-lexicographic"`. Validators SHALL resolve concrete peer sets at runtime from the machines list plus these named rules; the file stores no pre-enumerated per-pair rules.

#### Scenario: Reachability model validates against schema
- **WHEN** a topology JSON is produced by the fetcher
- **THEN** schema validation SHALL verify `reachability_model` contains the three version-1 named rules with identifier, description, applicable roles, and the structured `cross-rack-data-routing` representative-selection parameters; golden fixtures SHALL assert that those structured parameters select the expected source and target representatives

### Requirement: Provide golden fixtures for schemas and report generation
The project SHALL include golden sample fixtures for at least: a single-rack topology, a two-rack topology, a mixed in-scope/out-of-scope topology, a mixed management+data host topology, representative per-unit probe outputs, and a final report. Tests SHALL load these fixtures and validate them against the shared schemas.

#### Scenario: Golden fixtures remain compatible
- **WHEN** the test suite runs
- **THEN** every golden topology, probe-output, and report fixture SHALL validate against the current versioned schema
