# Real-hardware manual runbook (phase 9.6 gate)

This is the operator procedure for the one manual gate that cannot run in the
emulated testbed: validate the full charm deployment against a **real MAAS
instance** with **at least two nodes across two racks**, confirm the 35-second
LACP capture window records at least one PDU on a **real switch bond with
`lacp_rate slow`**, and confirm an injected fault is reported correctly.

This is not a test-suite item. Execute it by hand on real hardware and record
the results in the template at the end. The testbed (`testbed/README.md`)
already exercises the deploy/probe/collect/report pipeline against
OVS-emulated switches; this gate confirms the bits OVS cannot fully emulate -
real switch LACP timing and real cabling.

## Why this gate is manual

The testbed emulates switches with Open vSwitch. The negotiated LACP/BGP/MTU
results the validators read are faithful, but the medium is not real switch
hardware. In particular, `lacp-time=slow` (OVS) and `bond_lacp_rate=slow`
(host) mean one LACP PDU roughly every 30 seconds, so the 35-second capture
window is the minimum that reliably records a PDU. This runbook confirms that
window holds against a real switch's slow-rate LACP, where PDU jitter and
switch behavior differ from OVS.

## Prerequisites

- A real **MAAS** region/rack deployment managing the hardware.
- At least **two machines commissioned to `Ready`, in two different MAAS
  racks** (one per rack minimum; more is fine). Each machine that should be
  bond-checked must have a real 802.3ad bond configured in MAAS with
  `bond_lacp_rate=slow`, landing on a switch **port-channel / bond with LACP
  enabled at the slow rate** (`lacp_rate slow` / 30s PDUs).
- The two racks must have cross-rack data routing (so MTU + BGP-inference
  checks have something to measure); if the fabric is single-subnet, the
  cross-rack checks will report inconclusive/skip - that is acceptable for this
  gate, which centers on the real-switch LACP confirmation plus one fault.
- A **Juju controller** able to deploy to the MAAS cloud.
- **MAAS API** URL and key (`maas apikey --username <user>`).
- The **packed charm**: `make build` -> `charm/network-tester_amd64.charm`.
- The `network-tester` CLI runnable via `uv` (Python >= 3.10).
- Switch admin access to inject the bond fault (or physical access to move a
  cable for the asymmetric-swap variant).

## Procedure

Set convenience variables:

```sh
export MAAS_URL=http://<maas>:5240/MAAS
export MAAS_KEY=<consumer>:<token>:<secret>
export CHARM=charm/network-tester_amd64.charm
```

### Step 1 - Pre-flight (dry run)

Confirm targeting and that the intended checks would run, without deploying:

```sh
uv run network-tester run --all --dry-run --maas-url "$MAAS_URL" --maas-key "$MAAS_KEY"
```

Confirm the selected nodes span both racks and that `bond-validator` appears
under "Would run" for the bonded nodes. Use `--rack <a> <b>` or
`--nodes <id...>` instead of `--all` to scope to exactly the two racks/nodes.

### Step 2 - Clean baseline run (ephemeral)

Run in ephemeral mode (the default: the Juju model is created, probed, and
destroyed, releasing the nodes back to MAAS). Keep the model so you can inspect
per-unit output and re-trigger for the fault step:

```sh
uv run network-tester run --rack <rack-a> <rack-b> \
  --maas-url "$MAAS_URL" --maas-key "$MAAS_KEY" --charm "$CHARM" \
  --keep-model --verbose
```

Record the printed report path (`network-test-<timestamp>.json/.txt`) and the
kept model name (`network-test-<timestamp>`).

Expected on a correctly cabled fabric:

- Exit code `0` (clean) or `2` if only cross-rack checks are inconclusive on a
  flat fabric; **no `definitive_failures`**.
- For each bonded node, `bond-validator` reports `bond_mode` and `bond_cabling`
  as `pass`.

### Step 3 - Confirm the 35s LACP capture on the real switch bond

This is the core of the gate. For a bonded node, inspect the per-unit probe
output captured during the run:

```sh
juju ssh -m <model> network-tester/<n> \
  sudo cat /var/log/network-tester/probe-output.json | python3 -m json.tool
```

In the `bond_validator` section confirm:

- the bond negotiated 802.3ad (actor/partner in sync), and
- at least one LACP PDU was observed within the 35-second window (the validator
  reports `bond_mode: pass`; the captured-PDU evidence is what proves the
  slow-rate window held against the real switch).

Cross-check on the switch that the port-channel runs LACP at the slow rate
(e.g. `show lacp <id> ... ` / `show etherchannel detail` - vendor specific) and
that the actor system id / port keys match what the report shows.

### Step 4 - Inject one fault

Pick **one** of the two fault variants:

- **Bond mode mismatch** (easier, no recabling): change the switch
  port-channel LACP mode so it no longer matches the node, e.g. set the
  switch side to static/`on` (no LACP) while the node stays 802.3ad, or set one
  side passive while the other is also passive. Expected finding:
  `bond-mode-mismatch` with the corresponding hint (static-switch or
  both-passive).
- **Asymmetric cable swap**: physically move one bond member cable to a port on
  a different switch/port-channel (a different LACP system id). Expected
  finding: `asymmetric-bond-cable`.

### Step 5 - Re-run and confirm the fault is reported

Re-trigger the probe on the kept model and collect again (no redeploy):

```sh
uv run network-tester run --reuse-model <model> --verbose
```

Confirm:

- the report now contains the expected finding from Step 4
  (`bond-mode-mismatch` or `asymmetric-bond-cable`) under
  `definitive_failures`, with exit code `1`, and
- the finding identifies the correct node and bond.

### Step 6 - Restore and tear down

- Revert the switch/cable change from Step 4.
- Destroy the model to release the nodes back to MAAS:

```sh
juju destroy-model <model> --no-prompt --destroy-storage
```

(An ephemeral run without `--keep-model` does this automatically. Confirm the
nodes return to `Ready` / are released in MAAS.)

## Results to record

Copy this block into the change record / PR and fill it in:

```
Date:
Operator:
MAAS version:
Juju version:
Charm artifact (sha256):
Nodes (hostname / rack / system_id):
Switch model + firmware:
Port-channel / LACP config (rate):

Step 2 baseline:
  report path:
  exit code:
  bond_mode / bond_cabling per node:
  cross-rack MTU observation:
  cross-rack BGP inference:

Step 3 LACP capture:
  PDU observed within 35s window? (Y/N):
  negotiated actor/partner system ids:
  evidence (probe-output.json excerpt):

Step 4 fault injected (mode-mismatch | asymmetric-swap):
  exact change made:

Step 5 re-run:
  report path:
  exit code (expect 1):
  finding reported:
  correct node/bond identified? (Y/N):

Step 6 teardown:
  switch/cable restored? (Y/N):
  nodes released to Ready? (Y/N):

Overall result (PASS/FAIL):
Notes / anomalies:
```

When complete, attach the filled template (and the report `.json`/`.txt` files)
to the change, and check off tasks 9.6 and the runbook portion of 9.7 in
`openspec/changes/network-tester/tasks.md`.
