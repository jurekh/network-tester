## ADDED Requirements

### Requirement: Manage Juju model lifecycle
The CLI SHALL auto-generate a Juju model name using the pattern `network-test-<YYYYMMDD-HHMMSS>`. On startup, the CLI SHALL list existing models matching the `network-test-*` pattern; if any exist, it SHALL print their names and prompt the operator to destroy them manually or specify `--reuse-model <name>` to collect results from an existing run. The CLI SHALL register a SIGINT handler that prints the current model name and "run `juju destroy-model <name>` to release nodes" before exiting non-zero.

#### Scenario: No existing network-test models
- **WHEN** no models matching `network-test-*` exist
- **THEN** the CLI SHALL proceed to create a new model with the auto-generated name

#### Scenario: Existing network-test model found
- **WHEN** one or more models matching `network-test-*` already exist
- **THEN** the CLI SHALL print their names and exit non-zero with message "Existing test model(s) found. Destroy them or use --reuse-model <name>."

### Requirement: --reuse-model skips deployment and re-triggers probing on existing units
When `--reuse-model <name>` is specified, the CLI SHALL treat reuse as a separate run mode and SHALL NOT require `--all`, `--rack`, or `--nodes`. It SHALL skip MAAS topology fetch, pre-flight validation, and deployment. Instead it SHALL load the topology JSON that was attached to the existing model's `topology` resource, set a new `probe-run-id=<YYYYMMDD-HHMMSS>` via `juju config` to trigger a fresh probe cycle on existing units, wait for all restartable units to return to `active/idle` (or `--wait-timeout` elapses), run `collect-results` actions against all units, and generate the report using the loaded topology. Units that are not in a state where probing can restart SHALL be listed as warnings in the output and their results recorded as missing.

#### Scenario: Reuse model with all units active
- **WHEN** `--reuse-model network-test-20260514-143200` is specified and all units are active/idle
- **THEN** the CLI SHALL load the existing model's `topology` resource, set a new `probe-run-id` value via `juju config`, wait for units to complete probing again, collect results, and generate the report without any deployment step

#### Scenario: Reuse model with some units in bad state
- **WHEN** `--reuse-model` is specified and one or more units are in error or unknown state
- **THEN** the CLI SHALL print a warning listing those units, proceed with remaining units, and record the bad-state units as missing in the report

#### Scenario: Reuse model topology unavailable
- **WHEN** `--reuse-model` is specified but the existing model's `topology` resource cannot be loaded
- **THEN** the CLI SHALL exit non-zero with "Cannot reuse model <name>: topology resource is unavailable; run a new deployment or provide a valid network-test model"

#### Scenario: SIGINT received during run
- **WHEN** the operator presses Ctrl+C during deployment or probing
- **THEN** the CLI SHALL print the model name and "run `juju destroy-model <name>` to release nodes" and exit non-zero

### Requirement: Support flexible node targeting via mutually exclusive flags
The CLI wrapper SHALL accept normal deployment targeting via exactly one of: `--all` (all machines in `ready` state in the MAAS instance), `--rack <name> [<name>...]` (one or more rack names), or `--nodes <id> [<id>...]` (hand-picked machine system_ids or hostnames). Exactly one targeting mode SHALL be required for new deployments; `--reuse-model <name>` is mutually exclusive with all targeting flags and bypasses this requirement. The CLI SHALL error if a new deployment has none or more than one targeting flag, or if `--reuse-model` is combined with a targeting flag.

#### Scenario: All nodes targeted
- **WHEN** `--all` is specified
- **THEN** the CLI SHALL fetch all machines in `ready` state from MAAS and mark all ready machines `in_scope: true`

#### Scenario: Specific racks targeted
- **WHEN** `--rack rack-a rack-b` is specified
- **THEN** the CLI SHALL fetch machines assigned to rack-a and rack-b rack controllers, mark those machines `in_scope: true`, retain relevant known peers with `in_scope: false`, and match rack names as exact case-sensitive strings; if a specified name does not match any rack controller the CLI SHALL print "Unknown rack: X. Available racks: Y, Z" and exit non-zero

#### Scenario: Hand-picked nodes targeted
- **WHEN** `--nodes node-01 node-02 node-07` is specified
- **THEN** the CLI SHALL fetch those three machines, mark them `in_scope: true`, retain relevant known peers with `in_scope: false`, and scope probe execution to the selected machines only

#### Scenario: No deployment mode provided
- **WHEN** the operator runs `network-tester run` without a targeting flag and without `--reuse-model`
- **THEN** the CLI SHALL print a usage error listing the required targeting options and `--reuse-model`, then exit non-zero

### Requirement: Support dry-run mode that shows planned actions without deploying
When `--dry-run` is specified, the CLI SHALL fetch topology, run pre-flight validation, then print: the list of selected nodes with their roles, the list of checks that would run, and the list of checks that would be skipped with reasons. No Juju deployment SHALL occur.

#### Scenario: Dry run with skippable checks
- **WHEN** `--dry-run` is used with a node selection that leaves some checks without required peers
- **THEN** stdout SHALL list all would-run checks and all would-skip checks with the missing peer names, then exit zero without deploying

### Requirement: Deploy selected MAAS machines with explicit Juju placement
The CLI SHALL deploy the charm only onto the MAAS machines selected by `--all`, `--rack`, or `--nodes`. For each selected machine, the CLI SHALL create or target a Juju machine placement that maps to that MAAS machine's `system_id` or hostname, then deploy the network-tester unit to that explicit placement. The CLI SHALL NOT rely on Juju's automatic MAAS allocator to choose arbitrary ready machines. If Juju cannot allocate or place a selected machine, that machine SHALL be recorded in `missing_nodes` with reason `placement-failed` and the report SHALL explain the placement/allocation failure.

#### Scenario: Hand-picked nodes are placed exactly
- **WHEN** `--nodes node-01 node-02` is specified
- **THEN** the CLI SHALL deploy units only to the MAAS machines matching node-01 and node-02, and SHALL NOT allow Juju to substitute other ready machines

#### Scenario: Selected machine placement fails
- **WHEN** Juju cannot place a unit on one selected MAAS machine
- **THEN** the CLI SHALL continue with placeable selected machines when possible, record the failed machine in `missing_nodes`, and include the Juju placement error in the report

### Requirement: Deploy nodes rack-by-rack and hold all until cross-rack tests complete
The CLI SHALL deploy selected machines to Juju/MAAS ephemerally one rack at a time to avoid overloading the MAAS rack controller, but SHALL NOT release any nodes until all racks are deployed, all probes complete, and the report is generated. Rack batching changes only deployment order; it does not change explicit per-machine placement.

#### Scenario: Multi-rack deployment
- **WHEN** nodes from 3 racks are targeted
- **THEN** the CLI SHALL explicitly place and deploy rack 1's selected machines, then rack 2's selected machines, then rack 3's selected machines, then wait for all units to reach `active/idle` before triggering probing

### Requirement: Auto-destroy Juju model after report generation
After the report is generated, the CLI SHALL automatically destroy the Juju model and release all nodes back to MAAS ready state. `--keep-model` SHALL suppress destruction and print the Juju model name for manual inspection.

#### Scenario: Default auto-destroy
- **WHEN** report generation completes and `--keep-model` was not specified
- **THEN** the CLI SHALL run `juju destroy-model` and confirm all nodes returned to MAAS ready state

#### Scenario: Keep model for debugging
- **WHEN** `--keep-model` is specified
- **THEN** the CLI SHALL skip model destruction, print the model name, and remind the operator to destroy it manually when done

### Requirement: Perform pre-flight validation before deployment
Before issuing any Juju deployment commands, the CLI SHALL invoke the maas-topology-fetcher's pre-flight validation, which implements the checks and returns a structured failure list; the CLI owns presentation and control flow. If any machine fails validation, the CLI SHALL print the list of affected machines with missing fields and exit non-zero without deploying. The CLI SHALL NOT re-implement the validation checks itself.

#### Scenario: Pre-flight passes
- **WHEN** all selected machines have complete VLAN, fabric, and bond config in MAAS
- **THEN** the CLI SHALL print "Pre-flight validation passed for N nodes" and proceed to deployment

#### Scenario: Pre-flight fails
- **WHEN** one or more selected machines have incomplete MAAS network config
- **THEN** the CLI SHALL print the affected machine names and missing fields, and exit non-zero without deploying

### Requirement: Set probe-run-id via juju config after all units are ready
After all units reach `active/idle` (or after `--wait-timeout` seconds, default 1800 -- bare-metal deployment routinely takes 15-25 minutes), the CLI SHALL set the `probe-run-id` application config to the current timestamp string (`YYYYMMDD-HHMMSS`) via `juju config <app> probe-run-id=<value>`. Each invocation uses a unique value; this ensures re-runs are always distinguishable by charm units. Unless `--probe-start-delay` is 0, the CLI SHALL set `probe-start-at=<epoch+delay>` (default delay 30 seconds) in the same config change, so every unit's payload starts its capture windows at the same NTP-shared instant regardless of hook dispatch skew. The CLI wrapper owns the timeout decision; the charm does not implement a timer.

#### Scenario: All units active before timeout
- **WHEN** all expected units reach `active/idle` before `--wait-timeout` elapses
- **THEN** the CLI SHALL set `probe-run-id=<timestamp>` immediately and record no missing nodes

#### Scenario: Timeout elapses before all units active
- **WHEN** `--wait-timeout` elapses before all units reach `active/idle`
- **THEN** the CLI SHALL set `probe-run-id=<timestamp>` with available units and record which machines did not deploy in the report

### Requirement: Wait for probe completion after triggering a run
After setting `probe-run-id`, the CLI SHALL wait for every restartable unit that observed the run-id to return to `active/idle` after probe execution, or until `--wait-timeout` elapses. The CLI SHALL NOT invoke `collect-results` immediately after changing `probe-run-id`. Units that never become ready before the initial trigger SHALL be recorded in `missing_nodes` with reason `deployment-timeout`; units that were triggered but do not return to `active/idle` before the post-trigger wait expires SHALL be recorded with reason `probe-timeout`; units whose `collect-results` action returns `status: missing` SHALL be recorded with reason `no-probe-output`. Collected documents whose `probe_run_id` differs from the run-id just triggered SHALL NOT be aggregated and SHALL be recorded with reason `stale-probe-output` (the unit status message is the primary freshness gate; this cross-check prevents a lingering output file from an earlier run being reported as current results).

#### Scenario: Probes complete before collection
- **WHEN** all units return to `active/idle` after the new `probe-run-id` is set
- **THEN** the CLI SHALL run `collect-results` actions and generate the report

#### Scenario: Post-trigger wait expires
- **WHEN** one or more triggered units do not return to `active/idle` before `--wait-timeout` expires
- **THEN** the CLI SHALL still run `collect-results` against responsive units, record non-responsive or missing-output units separately from deployment failures, and include the wait timeout in `missing_nodes` or warnings

### Requirement: Support --verbose flag for full passed-checks output
The `run` subcommand SHALL accept a `--verbose` flag. The JSON report SHALL always include `passed_count` (integer) in the `summary` block. When `--verbose` is set, the JSON report SHALL also include a top-level `passed_checks` array with one entry per passed check. Without `--verbose`, `passed_checks` is omitted from the JSON entirely. The text summary always prints only failures, warnings, skips, and the total pass count regardless of `--verbose`.

#### Scenario: Verbose output requested
- **WHEN** `--verbose` is specified
- **THEN** the JSON report SHALL include a top-level `passed_checks` array with one entry per passed check, in addition to `summary.passed_count`

#### Scenario: Default (non-verbose) output
- **WHEN** `--verbose` is not specified
- **THEN** the JSON report SHALL include `summary.passed_count` as an integer and SHALL NOT include a `passed_checks` field

### Requirement: Print progress updates during deployment and probing
The CLI SHALL print timestamped status lines to stdout during long-running operations: as each rack's deployment starts and completes, when all units are ready, when probing starts, when results are collected.

#### Scenario: Deployment progress visible
- **WHEN** deploying 3 racks sequentially
- **THEN** stdout SHALL show a status line as each rack starts and finishes deploying, and when the Juju model shows all units active

### Requirement: status subcommand discovers models by pattern
The `status` subcommand SHALL accept an optional `<model-name>` positional argument. If provided, it SHALL show unit statuses for that specific model directly. If omitted, the subcommand SHALL list all Juju models matching the `network-test-*` pattern: if exactly one exists, show its status directly; if multiple exist, print their names and exit with "Specify a model name: `network-tester status <model-name>`"; if none exist, print "No network-test models found."

#### Scenario: Model name provided explicitly
- **WHEN** `network-tester status network-test-20260514-143200` is specified
- **THEN** the CLI SHALL show unit statuses and probe completion state for that model without prompting

#### Scenario: One existing model, no argument
- **WHEN** no model name is given and exactly one `network-test-*` model exists
- **THEN** the CLI SHALL show unit statuses and probe completion state for that model

#### Scenario: Multiple existing models, no argument
- **WHEN** no model name is given and more than one `network-test-*` model exists
- **THEN** the CLI SHALL list their names and print "Specify a model name: `network-tester status <model-name>`" and exit non-zero
