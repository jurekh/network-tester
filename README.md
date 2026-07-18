# network-tester

Validate datacenter network cabling and configuration against the intended
topology recorded in MAAS. `network-tester` fetches the topology from the MAAS
API, deploys an ephemeral probe charm to the target machines with Juju, runs a
self-contained probe payload on each unit, collects the per-unit results, and
emits a classified report.

The probe checks:

- **Bond / LACP state** - each bonded interface negotiates 802.3ad and the
  captured LACP PDUs match the intended aggregation (no mode mismatch, no
  asymmetric cabling).
- **VLAN neighbor sets** - each node sees exactly the L2 neighbors the topology
  says it should on each VLAN.
- **Cross-rack path MTU** - the measured MTU between representative nodes in
  different racks matches the data-VLAN MTU.
- **Inferred BGP reachability** - cross-rack data routing works in both
  directions; one-directional loss is reconciled into a single inferred link
  finding.

## Repository layout

- `cli/` - the operator-workstation CLI (`network-tester`): MAAS topology
  fetch, Juju deploy/collect, report generation.
- `charm/` - the probe charm (`charmcraft.yaml`) and its stdlib-only probe
  payload under `charm/payload/`.
- `testbed/` - a single-machine nested-virtualization testbed that stands up
  MAAS + Juju + an emulated two-rack fabric for end-to-end validation. See
  [`testbed/README.md`](testbed/README.md).
- `docs/` - operator documentation, including the
  [real-hardware manual runbook](docs/manual-hardware-runbook.md).
- `tests/` - CLI/unit/integration tests. `charm/tests/` holds the charm unit
  and jubilant integration tests.

## Prerequisites

To run `network-tester` against a MAAS-managed fabric you need:

- A **Juju controller** bootstrapped against, or otherwise able to deploy to,
  the MAAS cloud that manages the nodes under test.
- **MAAS API access**: the MAAS URL (e.g. `http://maas:5240/MAAS`) and an API
  key (`<consumer>:<token>:<secret>`, from `maas apikey --username <user>`).
- **Networking configured** (typically via Terraform): fabrics, VLANs, subnets,
  and DHCP set up so the intended topology is what MAAS records.
- **Commissioned nodes** in MAAS `Ready` state, with interfaces, bonds, and
  subnet links recorded (the probe resolves each node's identity by MAC and
  derives expected peers from the topology).
- The **packed charm** (`make build` -> `charm/network-tester_amd64.charm`).
- `uv` to run the CLI; Python >= 3.10.

The deployed nodes need `tcpdump`, `iputils-arping`, `iputils-ping`, and
`traceroute`, all Ubuntu main packages. The probe payload is stdlib-only and
is not bundled with third-party wheels; these tools are the only runtime
dependencies and are expected on the deployed image (verified on 22.04 and
24.04).

## Usage

The CLI runs as `network-tester` (via `uv run network-tester ...`). It has two
subcommands: `run` and `status`.

```sh
uv run network-tester run <targeting> \
  --maas-url http://maas:5240/MAAS \
  --maas-key <consumer>:<token>:<secret> \
  --charm charm/network-tester_amd64.charm
```

`run` creates a fresh `network-test-<timestamp>` Juju model, deploys the charm
to the selected nodes, triggers one probe run, collects results, writes the
report, and destroys the model (releasing the nodes back to MAAS) unless
`--keep-model` is given.

### Targeting modes

Exactly one targeting mode is required (mutually exclusive):

| Mode | Flag | Selects |
|------|------|---------|
| All | `--all` | every machine in MAAS `Ready` state |
| Rack | `--rack NAME [NAME ...]` | machines in one or more named racks (see below) |
| Nodes | `--nodes ID [ID ...]` | hand-picked machines by `system_id` or hostname |
| Reuse | `--reuse-model MODEL` | re-trigger probes on an existing model and collect (no new deploy) |

A **rack NAME is a rack controller's hostname**. A machine's rack is derived,
not stored: it is the hostname of the **primary rack controller of the
machine's boot-interface VLAN** (falling back to any other interface's VLAN,
and to the sole rack controller if only one is registered). This mirrors MAAS,
where the controller that serves a VLAN's DHCP/PXE defines its rack. Names are
matched exactly (case-sensitive); an unknown name fails with
`Unknown rack: X. Available racks: ...`. Only machines in MAAS `Ready` state are
eligible. To see each machine's derived rack, run with `--dry-run` (or `--all
--dry-run`): the plan prints `rack=<name>` per selected node.

Examples:

```sh
# All Ready machines
uv run network-tester run --all --maas-url $MAAS_URL --maas-key $MAAS_KEY \
  --charm charm/network-tester_amd64.charm

# Two racks
uv run network-tester run --rack rack-1 rack-2 --maas-url $MAAS_URL \
  --maas-key $MAAS_KEY --charm charm/network-tester_amd64.charm

# Specific nodes
uv run network-tester run --nodes node-abc xyz123 --maas-url $MAAS_URL \
  --maas-key $MAAS_KEY --charm charm/network-tester_amd64.charm

# Plan only: show selected nodes and the checks that would run, no deploy
uv run network-tester run --all --dry-run --maas-url $MAAS_URL --maas-key $MAAS_KEY

# Keep the model, then re-run probes on it and collect again
uv run network-tester run --all --keep-model --maas-url $MAAS_URL \
  --maas-key $MAAS_KEY --charm charm/network-tester_amd64.charm
uv run network-tester run --reuse-model network-test-20260618-120000

# Model status
uv run network-tester status                 # list network-test models
uv run network-tester status <model-name>    # status of one model
```

### Useful run flags

- `--dry-run` - print selected nodes and would-run/would-skip checks, then exit
  0 without deploying.
- `--keep-model` - leave the Juju model up after the report for inspection /
  reuse (otherwise the model is destroyed and nodes return to MAAS).
- `--probe-timeout SECONDS` - override the charm `probe-timeout` for this run
  (default: charm config, 240s). Must stay below the Juju hook timeout.
- `--wait-timeout SECONDS` - how long to wait for units to settle (default 600).
- `--mac-manifest PATH` - MAC-to-port manifest JSON for symmetric cable-swap
  detection (read locally on the workstation).
- `--cloud NAME` - Juju cloud to create the model on (default: controller
  default).
- `--verbose` - include the full passed-checks list in the JSON report.

## Report format

Each run writes two files to the current directory:

- `network-test-<timestamp>.json` - the machine-readable report.
- `network-test-<timestamp>.txt` - a human-readable summary (also printed to
  stdout).

The JSON report has these top-level keys:

| Key | Meaning |
|-----|---------|
| `schema_version` | report schema version |
| `generated_at` | ISO-8601 timestamp |
| `summary` | counts per category |
| `definitive_failures` | observed, unambiguous failures (e.g. bond mode mismatch) |
| `inferred_failures` | reconciled cross-node findings (e.g. one-directional BGP loss -> likely-bgp-failure) |
| `warnings` | non-fatal anomalies |
| `inconclusive_checks` | checks that could not be completed (timeout, cancelled, missing node, partial probe) |
| `skipped_checks` | checks not applicable to the selection (e.g. peer not in scope) |
| `observations` | raw measurements (e.g. measured cross-rack MTU) |
| `missing_nodes` | expected nodes that did not report |

A node that times out or is cancelled is classified `inconclusive`, never
silently dropped: unattempted cross-rack pairs are listed as inconclusive
rather than missing or failed.

## Exit codes

| Code | Meaning |
|------|---------|
| `0` | clean - no failures, inferred failures, warnings, or inconclusive checks |
| `1` | definitive failures present |
| `2` | non-definitive issues present (inferred failures, warnings, or inconclusive checks), or a usage/pre-flight error |
| `130` | interrupted (SIGINT); the message names the model to `juju destroy-model` if one was created |

`status` returns `0` on success and `2` when a model name is required but not
given.

## Configuration

Charm config options are declared in `charm/charmcraft.yaml` under `config:`
(this project uses the unified charmcraft.yaml; there is no separate
`config.yaml`):

| Option | Type | Default | Description |
|--------|------|---------|-------------|
| `probe-run-id` | string | `""` | Unique id for a probe run (set by the CLI). The leader propagates a non-empty value over the peer relation; units run the payload when it differs from their last executed run-id. |
| `probe-timeout` | int | `240` | Overall probe payload timeout in seconds. Must stay below the Juju hook timeout (300s default) with margin for the 5s partial-result flush. Override per run with `--probe-timeout`. |

## Development

```sh
make lint        # ruff check + format check
make test        # CLI/unit tests + charm unit tests
make build       # charmcraft pack -> charm/network-tester_amd64.charm
```

See [`testbed/README.md`](testbed/README.md) for the end-to-end testbed and the
`verify` matrix, and [`docs/manual-hardware-runbook.md`](docs/manual-hardware-runbook.md)
for the real-hardware validation runbook.
