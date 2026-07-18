## ADDED Requirements

### Requirement: Identify the node's own machine record on install via MAC matching
On install, each unit SHALL read its interface MAC addresses from `ip link show` and match them against the topology model to find its own machine record. If no match is found, the install hook SHALL fail with a clear error listing the observed MACs and noting they were not found in the topology.

#### Scenario: MAC match found
- **WHEN** at least one interface MAC on the unit matches a machine record in the topology model
- **THEN** the unit SHALL bind its identity to that machine record and proceed with installation

#### Scenario: No MAC match found
- **WHEN** none of the unit's interface MACs match any machine in the topology model
- **THEN** the install hook SHALL fail with: "Node identity not found in topology. Observed MACs: [list]. These MACs do not match any machine in the topology model."

### Requirement: Charm reads topology from Juju resource on install
Each unit SHALL read the topology JSON from the attached Juju resource named `topology` during the install hook, before any peer relation hooks fire. Node identity resolution (MAC matching) SHALL use the topology loaded from this resource.

#### Scenario: Topology resource present at install
- **WHEN** the charm is installed with the topology resource attached
- **THEN** the unit SHALL read and parse the topology JSON from the resource path and store it locally

#### Scenario: Topology resource missing at install
- **WHEN** the charm is installed without the topology resource attached
- **THEN** the install hook SHALL fail with a clear error message indicating the topology resource is required

### Requirement: Leader propagates probe-run-id to peer relation data when application config changes
The leader unit SHALL watch the `config-changed` hook for the `probe-run-id` application config value (default: empty string). When `probe-run-id` is set to a non-empty value, the leader SHALL write it to peer relation data under the key `probe-run-id`. The CLI wrapper sets this value once all units reach `active/idle` or after a configurable timeout; each CLI invocation uses a unique value (e.g. `YYYYMMDD-HHMMSS`) so re-runs are always distinguishable.

#### Scenario: probe-run-id config set
- **WHEN** the CLI wrapper sets `probe-run-id: <timestamp>` via `juju config`
- **THEN** the leader's `config-changed` hook fires and the leader SHALL write the same value to peer relation data

#### Scenario: Units join but probe-run-id not yet set
- **WHEN** all units have joined the peer relation but `probe-run-id` in relation data is absent or empty
- **THEN** no unit SHALL begin probing

### Requirement: Each unit begins probing only when a new probe-run-id differs from its last-executed run
Every unit (leader and non-leader) SHALL run probes from its own `config-changed` hook when `probe-run-id` is non-empty and differs from the value stored in `/var/lib/network-tester/last-probe-run-id`, writing the new run-id to that file after completing. Probing from `config-changed` rather than from the leader's relation-data write keeps capture windows concurrent across units: relation data written by the leader only commits when the leader's hook exits, so units waiting on `peer-relation-changed` would start a full probe-duration after the leader and passive cross-observation (e.g. unexpected-l2-neighbor) would structurally miss the leader's traffic. The `peer-relation-changed` path SHALL remain as a catch-up trigger with identical semantics (same last-run-id dedup guard) for units that missed the config event. If `probe-run-id` is absent or empty, the unit SHALL NOT begin probing; units SHALL NOT send any probe traffic before a new `probe-run-id` is observed.

#### Scenario: Unit sees no probe-run-id yet
- **WHEN** a unit's `config-changed` or `peer-relation-changed` hook fires and `probe-run-id` is absent or empty
- **THEN** the unit SHALL return from the hook without probing

#### Scenario: Unit receives new probe-run-id
- **WHEN** `probe-run-id` is set to a value that differs from the unit's last-executed run-id
- **THEN** the unit SHALL begin its probe sequence from its own `config-changed` hook, concurrently with the other units

#### Scenario: Unit sees same probe-run-id as last run
- **WHEN** `probe-run-id` in relation data matches the value in `/var/lib/network-tester/last-probe-run-id`
- **THEN** the unit SHALL NOT re-run probes (this run was already completed)

### Requirement: Set explicit Juju unit status for readiness and probe state
The charm SHALL use Juju unit status to make CLI polling deterministic. During package installation and topology loading it SHALL set maintenance status. After the topology resource is loaded, node identity is resolved, and the unit is ready to observe `probe-run-id`, it SHALL set active status with a message indicating readiness. While the payload is running it SHALL set maintenance status indicating the active probe run-id. If topology loading, package installation, or identity resolution fails, it SHALL set blocked or error status with the failure reason before raising. After payload completion, including timeout- or cancelled-status payload output, it SHALL return to active status so `collect-results` can run.

#### Scenario: Unit ready for probe trigger
- **WHEN** install completes, required packages are present, the topology is stored locally, and MAC matching resolves the node identity
- **THEN** the unit SHALL report active status and the CLI may treat it as ready for `probe-run-id`

#### Scenario: Probe running
- **WHEN** a unit starts the payload for a new `probe-run-id`
- **THEN** the unit SHALL report maintenance status until the payload exits, then return to active status

### Requirement: Expose a collect-results Juju action that retrieves probe output from a unit
Each unit SHALL implement a `collect-results` Juju action that reads the unit's probe output JSON from disk and returns it as the action result.

#### Scenario: collect-results action invoked on a unit
- **WHEN** the `collect-results` action is run on a unit that completed probing
- **THEN** the action result SHALL contain the full probe output JSON for that unit

### Requirement: collect-results action returns missing status when probing did not complete
If a unit's probe output file does not exist when `collect-results` is invoked, the action SHALL return a result with `status: missing` rather than erroring, so the CLI wrapper can distinguish incomplete units from units that failed to respond.

#### Scenario: Probe output file absent
- **WHEN** the `collect-results` action is run on a unit that did not complete probing
- **THEN** the action result SHALL contain `{"status": "missing", "unit": "<unit-name>"}` and exit without error

### Requirement: Invoke probe payload with topology path, probe-timeout, run-id, and start instant after new probe-run-id is observed
When a unit observes a new `probe-run-id` (via its own `config-changed` hook or the `peer-relation-changed` catch-up path), the unit SHALL invoke the probe payload from the installed charm source path (`payload/probe.py`) passing the topology JSON path, the `probe-timeout` config value, the run-id, and the `probe-start-at` config value (`0` when unset). The probe-timeout default is 240 seconds and MUST stay below the Juju hook timeout of 300 seconds with margin for the payload's 5-second flush window and the rendezvous wait (clamped to 45 seconds by the payload), since the hook waits for the payload synchronously. The payload is packaged with the charm source, not fetched as a Juju resource. The unit SHALL wait for the payload to exit before the hook completes, then write the run-id to `/var/lib/network-tester/last-probe-run-id`.

#### Scenario: Payload invoked with correct arguments
- **WHEN** a new `probe-run-id` is observed
- **THEN** the unit SHALL invoke the installed charm source `payload/probe.py <topology-path> <probe-timeout> <probe-run-id> <probe-start-at>` and wait for it to exit

#### Scenario: Payload exits with timeout or cancelled status
- **WHEN** the payload writes probe-output.json with `"status": "timeout"` or `"status": "cancelled"` and exits 0
- **THEN** the unit SHALL become active/idle normally so the collect-results action can retrieve the partial output
