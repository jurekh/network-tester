## ADDED Requirements

### Requirement: Accept topology path, probe-timeout, run-id, and start instant as arguments
The probe-runner SHALL accept two required arguments at invocation -- the path to the topology JSON file and the probe timeout in seconds -- plus two optional arguments: the probe run-id and an epoch-seconds start instant (all provided by the charm). If a required argument is missing or the topology file is not readable, the probe-runner SHALL exit non-zero with a clear error before probing begins. The run-id SHALL be stamped into the probe output as `probe_run_id` so the collector can reject stale documents. When the start instant is set and in the future, the probe-runner SHALL wait until that instant before starting validators, clamping the wait to 45 seconds (nodes share MAAS NTP, so the instant is common-mode across units and aligns their capture windows; the clamp protects the charm hook from a skewed workstation clock). The probe timeout budget starts after the wait.

#### Scenario: Valid invocation
- **WHEN** invoked with a readable topology JSON path and a positive integer timeout
- **THEN** the probe-runner SHALL load the topology, resolve node identity via MAC matching, and proceed to run validators

#### Scenario: Topology file missing
- **WHEN** invoked with a topology path that does not exist
- **THEN** the probe-runner SHALL exit non-zero with: "Topology file not found at <path>; charm install hook must write it before invoking the payload"

#### Scenario: Future start instant
- **WHEN** invoked with a start instant N seconds in the future (N <= 45)
- **THEN** the probe-runner SHALL wait ~N seconds before starting validators and then run with the full probe-timeout budget

#### Scenario: Start instant absent, past, or unparsable
- **WHEN** the start instant argument is missing, empty, `0`, in the past, or not a number
- **THEN** the probe-runner SHALL start validators immediately

### Requirement: Resolve node identity from topology via MAC matching before probing
Before invoking any validator, the probe-runner SHALL read interface MAC addresses from `ip link show` and match them against the topology JSON to find this node's machine record. If no match is found, the probe-runner SHALL exit non-zero with the list of observed MACs.

#### Scenario: MAC match found
- **WHEN** at least one interface MAC matches a machine record in the topology
- **THEN** the probe-runner SHALL bind to that machine record and proceed

#### Scenario: No MAC match found
- **WHEN** none of the node's MACs match any machine in the topology
- **THEN** the probe-runner SHALL exit non-zero with: "Node identity not found in topology. Observed MACs: [list]."

### Requirement: Run bond-validator and vlan-neighbor-validator concurrently, then mtu-validator and bgp-inference sequentially
The probe-runner SHALL start bond-validator and vlan-neighbor-validator concurrently (they use different protocols and do not interfere). Once both complete, the probe-runner SHALL run mtu-validator followed by bgp-inference sequentially (both generate ICMP traffic; concurrent execution risks intermediate-device rate limiting).

#### Scenario: Concurrent first phase
- **WHEN** probing begins
- **THEN** bond-validator (LACP capture) and vlan-neighbor-validator (ARP/ICMP) SHALL start simultaneously

#### Scenario: Sequential second phase
- **WHEN** both bond-validator and vlan-neighbor-validator have completed
- **THEN** mtu-validator SHALL run to completion before bgp-inference begins

### Requirement: Enforce overall probe timeout
The probe-runner SHALL enforce the timeout passed at invocation. Validators SHALL accept a shared cancellation event, SHALL register all child subprocesses (tcpdump, arping, ping, traceroute, and any future subprocesses) with the probe-runner before waiting on them, and SHALL check the cancellation event between per-peer or per-remote-rack probe iterations so cancellation takes effect promptly. Each validator's runtime is bounded by per-command caps (35-second LACP capture window, 2-second per-ICMP-probe timeout, 75-second traceroute wall-clock cap); the default probe-timeout (240 seconds, set by charm config) is sized against these caps and MUST remain below the Juju hook timeout (300 seconds by default) with margin for the 5-second flush window, because the charm hook invoking the payload would otherwise be hard-killed without partial-result flushing. If the timeout elapses before all validators complete, the probe-runner SHALL set the cancellation event with timeout reason, terminate registered child subprocesses, wait briefly for validator threads to flush partial findings, write whatever findings have been collected so far, and set `"status": "timeout"` in the output. Validator sections that had not started SHALL be written with `validator_status: "not_started"` and empty findings/observation lists; the probe-runner SHALL NOT synthesize per-path records for validators that never ran, because the report-generator derives the expected path universe from the topology rule. Validators interrupted by timeout SHALL be written with `validator_status: "timeout"` and SHALL include `observation_status: "timeout"` records for expected peer/path items they had derived but not attempted. If SIGTERM or operator cancellation occurs, the probe-runner SHALL set the cancellation event with cancellation reason and write `"status": "cancelled"`, with interrupted validators marked `validator_status: "cancelled"` (including `observation_status: "cancelled"` records for derived-but-unattempted items) and never-started validators marked `validator_status: "not_started"` with empty lists. The implementation SHALL NOT rely on forcibly killing Python threads. If all validators complete before the timeout, `"status"` SHALL be `"complete"` and each validator that ran SHALL report `validator_status: "complete"` or `"skipped"`.

#### Scenario: Probing completes within timeout
- **WHEN** all validators finish before the timeout elapses
- **THEN** probe-output.json SHALL be written with `"status": "complete"` and per-validator `validator_status` values

#### Scenario: Timeout elapses mid-run
- **WHEN** the timeout elapses while a validator is still running
- **THEN** the probe-runner SHALL stop all validators, write partial findings, set `"status": "timeout"` in probe-output.json, preserve `observation_status: "timeout"` records from the interrupted validator for items it had derived but not attempted, and mark validators that never started as `validator_status: "not_started"` with empty lists

### Requirement: Write probe-output.json with schema_version, node identity, and per-validator findings
The probe-runner SHALL write the complete probe output to `/var/log/network-tester/probe-output.json`. The output SHALL include: `schema_version: "1"`, `status` (complete, timeout, or cancelled), `node` (system_id, hostname, interfaces), and findings/observations from each validator in a top-level key per validator (`bond_validator`, `vlan_neighbor_validator`, `mtu_validator`, `bgp_inference`). Every validator section SHALL include `validator_status`; skipped validators SHALL include `skip_reason`; timeout/cancelled validators SHALL include timeout or cancellation context and preserve structured observations for peer/path items they had derived but not attempted; `not_started` sections carry empty lists. The directory SHALL be created if absent.

#### Scenario: Output written on success
- **WHEN** probing completes normally
- **THEN** `/var/log/network-tester/probe-output.json` SHALL exist with all required fields, including per-validator `validator_status` values and structured path observations for cross-rack validators

#### Scenario: Output written on timeout
- **WHEN** the timeout elapses
- **THEN** `/var/log/network-tester/probe-output.json` SHALL exist with `"status": "timeout"`, findings from whichever validators completed before the timeout, `observation_status: "timeout"` records from the interrupted validator, and `validator_status: "not_started"` sections for validators that never ran

### Requirement: Register SIGTERM handler to flush partial results before exit
The probe-runner SHALL register a SIGTERM handler that sets the cancellation flag, sends SIGTERM to all tracked child subprocesses (tcpdump, arping, ping, traceroute Popen objects), waits up to 5 seconds for running validators to flush their current state, then writes probe-output.json with whatever findings are available, top-level `"status": "cancelled"`, interrupted validator sections marked `validator_status: "cancelled"` with `observation_status: "cancelled"` records for items they had derived but not attempted, and never-started validator sections marked `validator_status: "not_started"` with empty lists.

#### Scenario: SIGTERM received mid-probe
- **WHEN** SIGTERM is received during probing
- **THEN** the probe-runner SHALL terminate all child subprocesses, write partial results with cancellation status, and exit within 5 seconds of receiving the signal
