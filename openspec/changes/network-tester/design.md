## Context

MAAS-managed datacenters have a planned rack design: node roles (rack controller, BMC/OAM, data), VLANs per port, bonded interfaces, BGP ASNs per rack, and known expected reachability (e.g. BMC/OAM nodes can only reach the rack controller; data nodes route cross-rack via BGP). After physical installation, cabling errors, misconfigured bonds, and BGP misconfigurations are common and hard to diagnose manually across 50-200 nodes.

The tool needs to:
1. Fetch the intended topology from MAAS (authoritative source of truth)
2. Deploy a probe charm ephemerally to all nodes across multiple racks
3. Coordinate probing so all nodes are up before any start sending traffic
4. Collect and compare observations against expected topology
5. Produce a structured report with specific remediation hints

Constraints: no switch management access; all observations are server-side; Juju controller is available and connected to MAAS; nodes are standard Ubuntu after MAAS commissioning.

## Goals / Non-Goals

**Goals:**
- Detect asymmetric bond cable swaps (LACP PDU disagreement)
- Detect bond mode mismatch between host config and switch behavior (LACP PDU capture)
- Detect servers placed on wrong VLAN (unexpected ARP neighbors, failed ICMP to expected peers)
- Measure observed cross-rack path MTU and record inconclusive MTU paths without pass/fail verdicts in v1
- Infer BGP failures from cross-rack reachability and traceroute analysis
- Optionally detect symmetric bond cable swaps when a MAC-to-port manifest is provided
- Produce per-check pass/fail with remediation hints and informational observations in a structured report
- Support rack-by-rack progressive deployment with all nodes held until cross-rack tests complete

**Non-Goals:**
- Switch management access or direct BGP session introspection
- Continuous monitoring or periodic re-validation
- Automatic remediation
- Wireless network probing
- ARP sweep of subnets larger than /16
- UDP probe traffic in v1

## Decisions

### D1: Juju machine charm over hand-rolled SSH orchestrator

Juju's leader/unit model solves the cross-node coordination problem directly: units signal readiness via peer relation data; the leader propagates a `probe-run-id` value to relation data once the CLI signals readiness. Result collection uses Juju actions, which fan out to all units. This eliminates building a custom coordination protocol.

The timeout decision (how long to wait for units before proceeding with partial deployment) belongs in the CLI wrapper, not the charm. Juju hooks only fire on events; there is no reliable "sleep N seconds then fire" mechanism in charms. The CLI wrapper polls the Juju model for unit readiness and sets `probe-run-id: <YYYYMMDD-HHMMSS>` via `juju config` once all units reach `active/idle` or after `--wait-timeout` elapses. The leader's `config-changed` hook propagates the run-id to peer relation data. Using a unique run-id string rather than a boolean ensures that re-runs (including `--reuse-model`) reliably trigger a new probe cycle without relying on units seeing a false->true transition.

Alternatives considered:
- CLI tool polls MAAS for node readiness, then SSHes in: fragile timing, no built-in fanout for result collection
- Cloud-init auto-start with a configurable delay: racy, no guarantee all nodes are up before probing starts
- Timer in charm using `event.defer()`: fragile, generates sustained log noise, hits Juju retry limits; rejected

### D2: MAAS API topology fetch runs in CLI wrapper, delivered to charm as Juju resource

MAAS already knows node roles, fabric/VLAN/subnet assignments, and machine MACs (post-commissioning). The CLI wrapper (running on the operator workstation) fetches topology, serializes it to JSON, and attaches it as a Juju resource when deploying the charm. Each unit reads the resource file on install and uses MAC matching to identify itself. Nodes do not need MAAS API access.

The topology file contains all MAAS machines needed to classify selected-node observations safely, not only selected machines. Each machine record carries `in_scope: true` when the node is targeted for deployment/probing and `in_scope: false` when it is known inventory used only for peer classification and skip reporting. Validators probe only in-scope machines, but they can distinguish known out-of-scope peers from truly unexpected devices. The `reachability_model` block describes peer derivation rules rather than pre-enumerated per-pair rules, keeping the file O(n) in node count regardless of cross-rack pair count.

Alternatives considered:
- Leader unit fetches from MAAS API: ephemeral nodes may not have network access to the MAAS API; not a reliable path
- Pre-enumerate all expected peer pairs in the topology file: produces O(n^2) file size for cross-rack rules; redundant since the same information is derivable from the machine inventory and `in_scope` flags
- Operator-authored YAML config: requires duplication of MAAS state; drift-prone; rejected in favor of MAAS as source of truth

### D3: Rack-by-rack deployment, all nodes held until cross-rack tests complete

Deploying all racks simultaneously would overload the MAAS rack controller in large installations. Deploying rack-by-rack and keeping nodes up avoids this while ensuring all nodes are present for cross-rack probing (BGP inference, east-west MTU). The Juju model accumulates units as racks are deployed; the leader defers probing until all expected units have joined.

Alternatives considered:
- Two-phase approach (intra-rack then cross-rack with one node per rack): adds sequencing complexity without meaningful benefit given Juju coordination is available
- Full simultaneous deployment: practical once MAAS scales to handle it; the tool's batch mode is forward-compatible with this

### D4: LACP PDU capture via tcpdump subprocess

Bond mode mismatches and asymmetric cable swaps are not visible to ICMP probing. Capturing LACP PDUs (EtherType 0x8809) on each bonded interface directly reveals what the switch is advertising: static vs. LACP, active vs. passive, port key. Comparing this against the host's configured bond mode gives a definitive diagnosis without switch access.

Capture uses `tcpdump -i <iface> ether proto 0x8809 -c 10 -w - --immediate-mode` as a subprocess with a 35-second window, run concurrently across all member interfaces; the pcap output is parsed with Python `struct` to extract LACP TLV fields. The window must exceed 30 seconds because the default LACP slow rate transmits one PDU every 30 seconds - a shorter window misclassifies correctly configured LACP ports as static. `tcpdump` is in Ubuntu main and is installed by the charm install hook to avoid image-specific assumptions. This eliminates a hand-rolled IEEE 802.3ad TLV decoder operating directly on AF_PACKET socket data, where a subtle off-by-one in TLV parsing produces silent wrong results.

Alternatives considered:
- AF_PACKET raw socket + manual TLV decoder: no subprocess, but highest-risk code path; parsing bugs produce silent wrong results
- scapy: richer API but in Ubuntu universe; excluded by package policy
- ethtool only: shows host-side config, not what the switch is doing

### D5: BGP failure as inferred from traceroute, not FRR daemon

BGP terminates on ToR switches, not servers. Running FRR on ephemeral nodes to establish a BGP test session requires the switch to be pre-configured to accept test peers - an unreliable external dependency. Traceroute analysis (traffic stops at ToR hop when cross-rack ICMP fails) provides a strong, actionable pointer to BGP without any switch pre-configuration. BGP inference is reported at rack-pair granularity: the probe tries a deterministic representative data node per remote rack and one fallback data node before inferring a rack-pair failure.

FRR-on-host is deferred until hosts run BGP natively for the data fabric, at which point the charm can check the BGP daemon's session state directly.

Alternatives considered:
- FRR BGP daemon on ephemeral node: requires switch pre-configuration per test run; deferred
- Assume BGP works if cross-rack ICMP works: insufficient - masks partial failures

### D6: CLI wrapper fans out collect-results to all units; no leader aggregation in charm

Each Juju unit writes its probe results as a JSON file and exposes a `collect-results` action that returns that file's contents. The CLI wrapper (which already has a Juju client) runs `collect-results` against all units in parallel using python-libjuju, then assembles and diffs the aggregate - which is where report generation already lives. The leader unit requires no collection or aggregation logic.

Running per-unit actions from the CLI is the standard Juju pattern and avoids having charm code invoke peer unit actions, which is awkward in the ops library and has no clean API. No hard per-result size limit was found in Juju 3.x documentation (Juju moved from MongoDB to dqlite; the 16MB BSON cap no longer applies). Verify empirically during implementation with a sample report.

Alternatives considered:
- Leader unit `collect-all-results` action fans out to peers: no clean ops API for a charm to invoke peer actions; unnecessary complexity when the CLI can fan out directly
- Nodes push results to a central HTTP endpoint: thundering herd on collection; collector is a SPOF
- Pull via SSH from CLI: bypasses Juju; loses the coordination guarantees

### D7: Three-way check status: pass, fail, skip

Checks that cannot be performed because a required peer was not included in the node selection are recorded as `skip` rather than silently omitted or falsely failed. Skips are grouped in the report (e.g. "3 cross-rack checks skipped - rack-03 was not in this run") to preserve coverage visibility without spamming the output.

Skips are produced when a machine that would be an expected peer by VLAN membership or rack/role is present in the topology with `in_scope: false`. The validator/report-generator derives this from the machine inventory and scope flag; absent machines are treated as unknown inventory, not as skippable expected peers.

Alternatives considered:
- Silently exclude: operator loses visibility into coverage gaps
- Flag as failure: incorrect - the check was never runnable, not a detected problem
- Pre-annotate every skipped pair in the topology file: produces O(n^2) file size; skip status is derivable at runtime from machine inventory and `in_scope` flags

### D8: CLI wrapper auto-destroys Juju model after report, with opt-out

Leaving ephemeral nodes running in a Juju model is an expensive default mistake on a 50-200 node deployment. The CLI wrapper destroys the model and returns nodes to MAAS ready state automatically after report generation. `--keep-model` opt-out flag preserves the model for debugging.

### D9: Pre-flight validation before any deployment

The CLI wrapper validates that all selected nodes have complete MAAS network configuration (VLAN, fabric, bond config) before deploying any nodes. This prevents wasting deployment time when Terraform has partially failed or MAAS config is incomplete.

### D10: Flexible node targeting in CLI

Operators need to run the tool on subsets of a live datacenter. Supported targeting: `--all` (all nodes in `ready` state), `--rack <name>` (one or more racks), `--nodes <id,...>` (hand-picked). `--dry-run` shows selected nodes and runnable/skipped checks without deploying.

### D11: Probe logic lives in a self-contained payload, not in charm hooks

The charm handles orchestration (deploy, coordinate, collect). The probe logic lives in `payload/probe.py`, a standalone Python script included in the charm source tree. It receives the topology JSON as a file path argument, runs validators, and writes `probe-output.json`. The charm invokes it as a subprocess from the installed charm path.

This separation means the probe code has no ops library dependency, can be tested without a Juju harness, and can be iterated independently of the charm lifecycle. The payload is packaged with the charm source, not as a separate Juju resource; only the topology JSON is delivered as a Juju resource.

Alternatives considered:
- Embed probe logic directly in charm hooks: couples probe logic to Juju event model; harder to test; makes the charm responsible for both orchestration and measurement

### D12: Python over compiled binary for probe payload

Python 3 is present on all Ubuntu installations. A compiled binary would require cross-compilation or build infrastructure per architecture. Python allows readable probe logic with no build step and is sufficient for subprocess-based probing.

### D13: bond-validator and vlan-neighbor-validator run concurrently; mtu and bgp run after

bond-validator (passive LACP capture) and vlan-neighbor-validator (ARP/ICMP) do not interfere with each other. Running them concurrently reduces minimum probe time. mtu-validator and bgp-inference both generate ICMP traffic and run after the first phase completes to avoid concurrent ICMP probes that could trigger intermediate-device rate limiting.

### D14: Overall probe timeout enforced by probe-runner, not Juju

The charm passes the `probe-timeout` config value (default 240s) to the payload at invocation. The probe-runner enforces this timeout: if it elapses before all validators complete, it terminates running validators, writes partial results with `status: timeout`, and exits. This ensures the charm unit returns to `active/idle` and `collect-results` can still retrieve partial output.

The default is sized against per-command caps: phase 1 is bounded by the 35s LACP capture window and the 30s ARP passive-capture cap (concurrent), MTU probing costs at most ~14s per cross-rack peer (7 probes at 2s each), and BGP inference costs two 2s ICMP probes plus one 75s-capped traceroute per remote rack. The default must stay below the Juju hook timeout (300s default) with margin for the 5s flush window, because the hook invoking the payload would otherwise be hard-killed without partial-result flushing; raising `probe-timeout` past that requires raising the hook timeout too.

Alternatives considered:
- Juju hook timeout: hooks time out at 300s by default; this is a hard kill with no partial result flushing

### D15: Explicit Juju placement by selected MAAS machine

The CLI must map selected MAAS machines to explicit Juju placements rather than allowing Juju's automatic MAAS allocator to choose arbitrary ready machines. This preserves the operator's requested scope, keeps `in_scope` truthful, and lets report failures point back to the exact selected hardware.

### D16: Versioned shared data schemas before implementation

Topology JSON, per-unit probe-output JSON, and final report JSON are contracts between independently testable components. They must be defined as shared versioned schemas/models with golden fixtures before implementation so CLI, charm payload, validators, and report generator cannot drift.

### D17: Cooperative cancellation for probe timeout

Validator timeout handling uses cooperative cancellation: a shared cancellation event, tracked subprocesses, and per-command timeouts. Python validator threads are not forcibly killed. On timeout or SIGTERM, the runner terminates tracked subprocesses, lets validators flush partial findings, and writes a timeout-status probe output.

## Risks / Trade-offs

- [Nodes in different racks deployed at very different times leave early units idle waiting for late units] -> Mitigation: configurable `--wait-timeout` in CLI wrapper; if all expected units don't reach active/idle within timeout, the CLI sets probe-run-id and the report flags missing nodes
- [LACP PDU capture requires CAP_NET_RAW or root] -> Mitigation: charm runs as root in MAAS ephemeral environment; document this requirement
- [MTU probe with DF-bit may be ICMP-rate-limited by intermediate devices] -> Mitigation: use multiple probe sizes (1472, 8972 payload bytes; binary search); flag inconclusive results rather than false-passing
- [BGP inference is not definitive] -> Mitigation: label classified BGP/routing findings as inferred, label no-hop traceroute results as inconclusive, and include traceroute hop data so the operator can verify manually
- [Terraform partially applied leaves MAAS config incomplete] -> Mitigation: pre-flight validation catches this before deployment begins
- [probe-timeout may expire before all validators complete] -> Mitigation: write partial results with `status: timeout`; report-generator treats missing checks as inconclusive

## Open Questions

- When hosts run BGP natively (future data fabric), should the charm check BGP daemon session state in addition to or instead of traceroute inference?

## Resolved

- `--rack` uses exact case-sensitive string match against MAAS rack controller names; CLI prints available rack names if a specified name does not match.
