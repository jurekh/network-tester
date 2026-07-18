## 1. Repository Setup

- [ ] 1.1 Initialise Juju machine charm with `charmcraft init --profile machine` in `charm/`
- [ ] 1.2 Create probe payload layout under charm source: `charm/payload/probe.py` (entry point), `charm/payload/bond_validator.py`, `charm/payload/vlan_neighbor_validator.py`, `charm/payload/mtu_validator.py`, `charm/payload/bgp_inference.py`, `charm/payload/probe_runner.py`, `tests/`
- [ ] 1.3 Define peer relation `network-tester-peers` in `charm/metadata.yaml`
- [ ] 1.4 Declare `topology` Juju resource (type: file, required) in `charm/metadata.yaml`; no `mac-manifest` or payload charm resources (manifest is CLI-side only; payload is packaged in charm source)
- [ ] 1.5 Add charm config options: `probe-run-id` (default empty string), `probe-timeout` (default 240, seconds; must stay below the 300s Juju hook timeout with flush margin)
- [ ] 1.6 Add charm action: `collect-results` in `charm/actions.yaml`
- [ ] 1.7 Add Python dependencies in the correct components: charm requirements for `ops`; CLI requirements for MAAS API and Juju client libraries; no scapy
- [ ] 1.8 Add `Makefile` with targets: `test`, `lint`, `build` (charmcraft pack), `clean`
- [ ] 1.9 Probe runtime dependencies are all in Ubuntu main; `tcpdump`, `arping` (iputils-arping), and `traceroute` must be installed by the charm's install hook via `apt install -y tcpdump iputils-arping traceroute`; `ping` is provided by iputils-ping

## 2. MAAS Topology Fetcher (CLI-side)

- [ ] 2.1 Implement `cli/maas_topology.py`: fetch machines in `ready` state from MAAS API using `--maas-url` and `--maas-key` CLI args; support `--all`, `--rack`, `--nodes` scoping with `in_scope` flags and retained known peers for classification
- [ ] 2.2 Implement role derivation: query `/MAAS/api/2.0/rackcontrollers/` to get MAAS-designated rack controllers (these are `role: rack-controller` without further inference); use their fabric memberships to classify management vs. data fabrics; classify remaining machines as `bmc-oam` (management fabric only) or `data` (data fabric only)
- [ ] 2.3 Fetch and record per-interface details: fabric name, VLAN tag, subnet CIDR, IP, MAC, gateway IP, bond members and mode from MAAS
- [ ] 2.4 Implement pre-flight validation: check all selected machines have VLAN, fabric, and bond config present in MAAS; return structured list of failures
- [ ] 2.5 Serialize topology model to JSON including `reachability_model` block and per-machine `in_scope`; write test asserting required top-level keys (`schema_version`, `scope`, `fabrics`, `machines`, `reachability_model`) and mixed in-scope/out-of-scope machines

## 3. Bond Validator

- [ ] 3.1 Implement `bond_validator.py`: enumerate bonded interfaces from `/proc/net/bonding/` and read configured bond mode per bond
- [ ] 3.2 Implement LACP PDU capture using tcpdump subprocess: run `tcpdump -i <iface> ether proto 0x8809 -c 10 -w - --immediate-mode` with a 35-second window per member interface, all member captures running concurrently (LACP slow rate sends one PDU per 30s; a shorter window misses PDUs on correct LACP ports); collect pcap output
- [ ] 3.3 Implement LACP PDU parser: parse pcap output using Python `struct`; extract actor system ID, actor port key, actor state flags, partner system ID, and partner port key per IEEE 802.3ad TLV layout
- [ ] 3.4 Implement bond mode comparison: compare host configured mode against LACP PDU presence; record `bond-mode-mismatch` failure with remediation hint if mismatch
- [ ] 3.5 Implement asymmetric cable detection: compare inbound LACP actor system IDs/port keys (remote switch identity) across member interfaces of the same bond; do not use partner system ID as the primary remote-switch identity; record `asymmetric-bond-cable` failure if remote switch identities differ
- [ ] 3.6 Record `bonds: []` and exit without failure when `/proc/net/bonding/` contains no bond files
- [ ] 3.7 Write unit test: mock tcpdump subprocess output with synthesized pcap bytes containing two LACP frames from different system IDs; assert `asymmetric-bond-cable` failure recorded
- [ ] 3.8 Read `lacp_active` from `/proc/net/bonding/<bond>` and decode the LACP activity flag from captured PDUs; when the host bond is 802.3ad with `lacp_active off` and no PDUs arrive, extend the bond-mode-mismatch hint to cover the both-passive case; record the activity flag in raw PDU audit output; unit test the passive-switch/active-host pass case and the passive-host/no-PDU hint

## 4. VLAN Neighbor Validator

- [ ] 4.1 Implement `vlan_neighbor_validator.py`: derive expected in-scope peer set and known out-of-scope peer set from machines list (same fabric and vlan_tag; rack-controller only for bmc-oam nodes); enumerate active non-loopback interfaces and their subnet CIDRs
- [ ] 4.2 Implement ARP probing: (a) targeted `arping -I <iface> -c 1 -w 2 <peer-ip>` for each expected in-scope peer on the interface where that peer is expected (same fabric and vlan_tag); `-I <iface>` is required to bind to the correct interface on multi-homed nodes; (b) concurrent `tcpdump -i <iface> arp -c 50 -w - --immediate-mode` passive capture for the duration of the arping phase (max 30s) to collect all observed MACs for unexpected-neighbor detection; no subnet sweep
- [ ] 4.3 Implement unexpected neighbor detection: cross-reference ARP responses against expected in-scope and known out-of-scope peer sets; flag MACs absent from both sets as `unexpected-l2-neighbor`; record known out-of-scope peers as skipped observations, not failures; for each unexpected neighbor, attempt one `ping -c 1 -W 2 <ip>` follow-up; if it succeeds, also record `unexpected-reachability` for that IP
- [ ] 4.4 Implement ICMP reachability probe for each expected in-scope peer: try raw socket, fall back to `ping -c 3 -W 2` subprocess on PermissionError; record RTT and loss
- [ ] 4.5 Implement unexpected reachability detection: flag successful ICMP to peers absent from both expected in-scope and known out-of-scope peer sets as `unexpected-reachability`
- [ ] 4.6 Write unit tests: (a) assert arping is called with `-I <iface>` matching the interface where the in-scope peer is expected; (b) mock unexpected ARP response followed by successful ping -- assert both `unexpected-l2-neighbor` and `unexpected-reachability` are recorded; (c) mock unexpected ARP response followed by failing ping -- assert only `unexpected-l2-neighbor` is recorded; (d) mock known out-of-scope ARP response -- assert no unexpected-neighbor failure and one skipped observation

## 5. MTU Validator

- [ ] 5.1 Implement `mtu_validator.py`: derive cross-rack peer set from in-scope machines with `role: data` and `rack` != local node's rack; run `ping -M do -s <size> -c 1 -W 2 <target>` at 1472 and 8972 byte ICMP payload sizes; record observed path MTU as payload size + 28 without pass/fail comparison
- [ ] 5.2 Parse ping exit code and stderr to detect ICMP type 3 code 4 (fragmentation needed); extract MTU from fragmentation-needed messages when available
- [ ] 5.3 Implement binary search refinement: if 8972 fails and 1472 succeeds, binary search between 1472 and 8972 payload bytes to find effective path MTU (max 5 iterations)
- [ ] 5.4 Record `inconclusive` when all probe sizes are dropped with no ICMP error response
- [ ] 5.5 Write unit test: mock ICMP responses at different sizes; assert binary search and inconclusive handling

## 6. BGP Inference

- [ ] 6.1 Implement `bgp_inference.py`: derive remote rack set from in-scope machines (distinct `rack` values of machines with `role: data` where rack != local rack); probe the representative in-scope data node per remote rack (lexicographically lowest `system_id`) via ICMP; if it fails, probe one fallback in-scope data node in that rack (next lexicographic `system_id`) before flagging rack-pair unreachable
- [ ] 6.2 When both representative and fallback cross-rack ICMP probes fail, run `traceroute -n -m 30 -q 1 -w 2` subprocess to the representative target with a 75-second wall-clock cap (terminate on expiry, keep collected hops, set `traceroute_truncated: true`) and parse hop list: IP and RTT per hop, `*` for non-responding hops
- [ ] 6.3 Classify traceroute result at rack-pair granularity: last-hop matches local subnet gateway IP from topology model (ToR) -> `likely-bgp-failure`; last-hop is beyond local ToR gateway -> `routing-failure`; reaches remote rack subnet but not target -> `intra-rack-routing`
- [ ] 6.4 Set `scope: rack-pair` on all BGP findings; set `diagnosis_confidence: inferred` for classified BGP/routing failures and `diagnosis_confidence: inconclusive` for `icmp-blocked`; include raw `traceroute_hops` array in output JSON
- [ ] 6.5 Write unit test: mock representative failure with fallback success, representative+fallback failure with traceroute output for each hop classification, and assert correct finding type per case

## 7. Probe Runner

- [ ] 7.1 Implement `probe_runner.py`: accept topology JSON path and probe-timeout as arguments; exit non-zero with clear error if topology file is missing or unreadable
- [ ] 7.2 Resolve node identity: read interface MACs from `ip link show`, match against topology JSON; exit non-zero with observed MACs if no match found
- [ ] 7.3 Run bond-validator and vlan-neighbor-validator concurrently using `threading`; wait for both to complete before proceeding
- [ ] 7.4 Run mtu-validator then bgp-inference sequentially after the first phase completes
- [ ] 7.5 Enforce probe-timeout: if elapsed, terminate running validators, write partial results with `status: timeout`
- [ ] 7.6 Register SIGTERM handler: set timeout flag, wait up to 5 seconds, write probe-output.json
- [ ] 7.7 Write probe output to `/var/log/network-tester/probe-output.json`: create directory if absent; include `schema_version: "1"`, `status`, `node`, and per-validator finding sections

## 8. Juju Coordinator (Charm)

- [ ] 8.1 In install hook: run `apt install -y tcpdump iputils-arping traceroute`; then read topology JSON from Juju resource path (`self.model.resources.fetch('topology')`); store to `/var/lib/network-tester/topology.json`; fail with clear error if resource absent
- [ ] 8.2 In install hook: resolve node identity by matching local interface MACs from `ip link show` against the topology machines list; set blocked status listing observed MACs if no match (asserted by 11.10)
- [ ] 8.3 In leader's `config-changed` hook: when `probe-run-id` config changes to a non-empty value, write it to peer relation data under key `probe-run-id`
- [ ] 8.4 In `peer-relation-changed` hook: if `probe-run-id` in relation data is non-empty and differs from `/var/lib/network-tester/last-probe-run-id`, invoke the installed charm source `payload/probe.py /var/lib/network-tester/topology.json <probe-timeout>`; after payload exits write the new run-id to `/var/lib/network-tester/last-probe-run-id`; if run-id is absent or unchanged, return from hook without action
- [ ] 8.5 Implement `collect-results` action: read `/var/log/network-tester/probe-output.json` and return contents as action result; return `{"status": "missing", "unit": "<unit-name>"}` if file absent (no error)

## 9. Report Generator (CLI-side)

- [ ] 9.1 Implement `cli/report_generator.py`: load all unit probe output JSONs from collected action results
- [ ] 9.2 Cross-reference bidirectional observations: mark edges `confirmed: bidirectional` when both endpoints recorded each other; `confirmed: unidirectional` otherwise
- [ ] 9.3 Diff aggregated observations against expected peer sets derived from the topology's `in_scope: true` machines and `reachability_model` rules; classify active checks as `pass`, `fail`, or `inconclusive`; classify rules involving `in_scope: false` peers as `skip`
- [ ] 9.4 Group skipped checks by missing peer: emit one skip entry per `in_scope: false` peer with count of affected checks
- [ ] 9.5 Separate BGP inference findings by confidence: `diagnosis_confidence: inferred` entries go into `inferred_failures` (never `definitive_failures`); `diagnosis_confidence: inconclusive` entries go into `inconclusive_checks`; aggregate findings sharing the same (source rack, target rack) pair across units into one rack-pair entry with an `observed_by` node list and per-node traceroute data preserved
- [ ] 9.6 Auto-save JSON report to `network-test-<ISO timestamp>.json` and text summary to `network-test-<ISO timestamp>.txt` in current directory; also print text summary to stdout
- [ ] 9.7 JSON report structure: `schema_version`, `generated_at`, `summary` (including `passed_count`, failed/skipped/inconclusive/warning counts), `definitive_failures`, `inferred_failures`, `warnings`, `inconclusive_checks`, `skipped_checks`, `missing_nodes` (each entry with a `reason`: `placement-failed`, `deployment-timeout`, `probe-timeout`, or `no-probe-output`); omit top-level `passed_checks` unless `--verbose` is set
- [ ] 9.8 Implement optional MAC manifest loading: if `--mac-manifest` was passed to the CLI, load the manifest JSON from that path (operator's workstation only; not a charm resource) and cross-reference observed MACs in the collected probe outputs; flag symmetric swaps as informational findings in the `observations` list
- [ ] 9.9 Implement `observations` list in the JSON report: include MTU probe results (source node, target node, `observed_path_mtu_bytes` or `null` with `status: "inconclusive"`) as informational entries with no pass/fail verdict; include symmetric-bond-swap findings here if MAC manifest was provided

## 10. CLI Wrapper

- [ ] 10.1 Implement `network-tester` CLI script (Python, `argparse`): subcommands `run`, `status`
- [ ] 10.2 `run` subcommand: support normal deployment mode requiring exactly one of `--all`, `--rack <name>...`, `--nodes <id>...`; support reuse mode via mutually exclusive `--reuse-model`; add `--dry-run`, `--keep-model`, `--mac-manifest`, `--maas-url`, `--maas-key`, `--wait-timeout`, `--verbose` flags
- [ ] 10.3 `run --dry-run`: fetch topology with `in_scope` flags, run pre-flight on in-scope machines, print selected nodes with roles, would-run checks, and would-skip checks with reasons; exit without deploying
- [ ] 10.4 `run` pre-flight: call maas_topology pre-flight validation for in-scope machines; print failures and exit non-zero if any selected machine has incomplete config
- [ ] 10.5 `run` deployment: serialize topology with retained known peers to JSON, attach as `topology` Juju resource; deploy charm rack-by-rack using python-libjuju; print timestamped progress lines as each rack deploys; do NOT attach mac-manifest or payload as charm resources
- [ ] 10.6 `run` coordination: poll Juju model until all in-scope units reach `active/idle` (or `--wait-timeout` elapses); set `probe-run-id=<YYYYMMDD-HHMMSS>` via `juju config`; run `collect-results` action against all units in parallel via python-libjuju; write raw per-unit results to local temp directory
- [ ] 10.7 `run` startup: auto-generate model name `network-test-<YYYYMMDD-HHMMSS>`; on startup list existing `network-test-*` models and exit with guidance if any exist (unless `--reuse-model` is specified); register SIGINT handler to print model name and destroy instructions
- [ ] 10.8 `run` cleanup: invoke report generator on collected results; then auto-destroy Juju model unless `--keep-model` specified; if `--keep-model`, print model name and reminder
- [ ] 10.9 `run --reuse-model <name>`: bypass targeting flags; load topology JSON from the existing model's `topology` resource; skip MAAS topology fetch, pre-flight, and deployment; set a new `probe-run-id=<YYYYMMDD-HHMMSS>` via `juju config`; wait for restartable units to return to `active/idle` (or `--wait-timeout`); collect results and generate report with the loaded topology; warn about units in non-restartable states and record them as missing
- [ ] 10.10 `status` subcommand: list all Juju models matching `network-test-*`; show unit statuses and probe completion state for each; if exactly one model, show directly; if multiple, prompt operator to select

## 11. Tests

- [ ] 11.1 Unit test MAAS topology fetcher: mock MAAS API responses; assert correct role classification and fabric identification for each role
- [ ] 11.2 Unit test pre-flight validation: mock machines with missing VLAN and bond config; assert correct failure list returned
- [ ] 11.3 Unit test topology serialization: assert required top-level keys including `reachability_model` block and per-machine `in_scope`; assert out-of-scope known peers are retained
- [ ] 11.4 Unit test LACP PDU parser: synthesize pcap bytes for mode mismatch and asymmetric cable cases (covered in 3.7)
- [ ] 11.5 Unit test vlan-neighbor-validator: mock ARP and ICMP results; assert unexpected neighbor and unexpected reachability detections; assert known out-of-scope peers do not produce unexpected-neighbor failures (covered in 4.6)
- [ ] 11.6 Unit test MTU validator: mock ICMP probe responses at different payload sizes; assert payload+28 MTU recording, binary search, and inconclusive handling (covered in 5.5)
- [ ] 11.7 Unit test BGP inference: mock representative/fallback ICMP outcomes and traceroute subprocess output for each hop classification; assert rack-pair scoped finding type per case (covered in 6.5)
- [ ] 11.8 Integration test probe-runner: provide a synthetic topology JSON with 2 in-scope nodes and at least 1 out-of-scope known peer; mock all subprocess calls; assert probe-output.json is written with correct structure and status
- [ ] 11.9 Unit test report generator: provide synthetic probe outputs from 3 nodes; assert bidirectional edge confirmation, skip grouping from `in_scope: false` peers, and diff against expected topology
- [ ] 11.10 Integration test Juju coordinator hooks: use `ops.testing.Harness`; assert unit reads topology resource, resolves identity via MAC matching, and leader sets `probe-run-id` in relation data when `probe-run-id` application config is set; assert installed charm source payload is invoked with correct topology path and probe-timeout; assert unit does not re-run probes when `probe-run-id` in relation data matches `/var/lib/network-tester/last-probe-run-id`
- [ ] 11.11 **[DEFERRED - requires LXD + Juju CI]** Charm integration test using jubilant: deploy charm to LXD-backed Juju model with a synthetic topology resource and a stub `probe.py` that writes canned output; set `probe-run-id` via `juju.config`; assert `collect-results` action returns expected output; use `jubilant.temp_model` for model lifecycle; this catches real hook ordering and relation data propagation that Harness mocks away

## 12. Packaging and Documentation

- [ ] 12.1 Verify `charmcraft pack` includes the following payload Python files from charm source: `charm/payload/probe.py` (entry point), `charm/payload/probe_runner.py`, `charm/payload/bond_validator.py`, `charm/payload/vlan_neighbor_validator.py`, `charm/payload/mtu_validator.py`, `charm/payload/bgp_inference.py`; no third-party wheels are included (stdlib only); runtime tools (`tcpdump`, `arping`, `ping`, `traceroute`) are Ubuntu main packages and are not bundled
- [ ] 12.2 Verify the payload runs correctly on a fresh Ubuntu 22.04 install after installing `tcpdump iputils-arping traceroute`
- [ ] 12.3 Write `README.md`: prerequisites (Juju controller connected to MAAS, MAAS API credentials, Terraform-configured networking, commissioned nodes), how to run with examples for each targeting mode, report format explanation, exit code reference
- [ ] 12.4 Write `charm/config.yaml` defaults and descriptions for all config options including `probe-timeout`
- [ ] 12.5 **[MANUAL RUNBOOK - requires real MAAS + 2 racks]** Test full charm deployment against a real MAAS instance in ephemeral mode on at least 2 nodes across 2 racks, including one injected fault (bond mode mismatch or asymmetric cable swap), before marking complete; this is not a test suite item - execute manually and record results


## 13. Spec Tightening Before Implementation

- [ ] 13.1 Define shared versioned topology/probe-output/report schemas and golden fixtures before implementing validators; include mixed in-scope/out-of-scope and mixed management+data host examples; the schemas must define the `reachability_model` block structure (named rules: `l2-same-fabric-vlan`, `bmc-oam-restricted`, `cross-rack-data-routing`) and the common finding envelope (`type`, `classification`, `scope`, `hint`, `details`)
- [ ] 13.2 Specify and implement explicit Juju placement of each selected MAAS machine; add tests/mocks that prove `--nodes` never allows arbitrary ready-machine substitution
- [ ] 13.3 Add normal-run post-trigger wait: after setting `probe-run-id`, wait for units to complete probes and return active/idle before collecting results
- [ ] 13.4 Fix LACP parser tests to prove inbound switch PDUs use actor system ID/port key as the remote switch identity and do not compare partner system ID for asymmetric-cable detection
- [ ] 13.5 Add mixed-interface role derivation tests: management-only -> `bmc-oam`, data-only -> `data`, management+data -> `data` with data-fabric target IP selection
- [ ] 13.6 Fetch and retain rack-controller anchor records regardless of `ready` state; only selected deploy/probe targets are constrained to `ready`
- [ ] 13.7 Implement a shared cancellation/subprocess registry API for validators; add timeout/SIGTERM tests with stuck tcpdump, arping, ping, and traceroute mocks
- [ ] 13.8 Clarify known-but-forbidden peers in VLAN validation; add tests showing bmc-oam seeing a non-rack-controller same-VLAN host is a failure, not a skip
- [ ] 13.9 Add Juju unit-status behavior tests: maintenance during setup/probing, active when ready/complete, blocked/error on topology/package/identity failures
- [ ] 13.10 Empirically test Juju topology resource size and action result size with synthetic 200-node topology/probe outputs; record limits and add chunking/compression if needed
- [ ] 13.11 Define project packaging before implementation: `pyproject.toml`, CLI/charm dependency sets, pytest config, charmcraft.yaml, Makefile targets, and CI commands
- [ ] 13.12 Add a timeout-budget test: with a representative synthetic topology, compute worst-case validator wall-clock (35s concurrent LACP/ARP phase, ~14s per cross-rack MTU peer, two 2s ICMP probes plus one 75s-capped traceroute per remote rack) and assert it fits within the default probe-timeout of 240s; assert validators check the shared cancellation event between peer iterations
- [ ] 13.13 Verify LACP slow-rate capture empirically during the manual runbook (12.5): confirm a 35s capture window records at least one PDU on a bond with `lacp_rate slow`
