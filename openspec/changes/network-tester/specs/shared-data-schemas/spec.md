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

### Requirement: Define the reachability_model structure
The topology schema SHALL define the `reachability_model` block as a set of named rule definitions, each with a rule identifier, a description, and the roles it applies to. At minimum version 1 SHALL define: `l2-same-fabric-vlan` (machines sharing a fabric name and vlan_tag are expected L2 peers unless a role rule forbids it), `bmc-oam-restricted` (a `bmc-oam` node's expected peer set is only the `rack-controller` in its rack; other same-VLAN adjacency is forbidden), and `cross-rack-data-routing` (data-role machines in different racks are expected L3-reachable via data-fabric interfaces, with active MTU/BGP probes initiated only by one deterministic source representative per rack against deterministic remote-rack representative targets). Validators SHALL resolve concrete peer sets at runtime from the machines list plus these named rules; the file stores no pre-enumerated per-pair rules.

#### Scenario: Reachability model validates against schema
- **WHEN** a topology JSON is produced by the fetcher
- **THEN** schema validation SHALL verify `reachability_model` contains the three version-1 named rules with identifier, description, and applicable roles; the representative-selection semantics are carried in the `cross-rack-data-routing` rule description and asserted by golden-fixture content tests, not by schema validation

### Requirement: Provide golden fixtures for schemas and report generation
The project SHALL include golden sample fixtures for at least: a single-rack topology, a two-rack topology, a mixed in-scope/out-of-scope topology, a mixed management+data host topology, representative per-unit probe outputs, and a final report. Tests SHALL load these fixtures and validate them against the shared schemas.

#### Scenario: Golden fixtures remain compatible
- **WHEN** the test suite runs
- **THEN** every golden topology, probe-output, and report fixture SHALL validate against the current versioned schema
