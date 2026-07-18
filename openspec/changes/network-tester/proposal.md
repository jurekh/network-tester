## Why

When standing up a MAAS-managed datacenter, the physical cabling and switch/host configuration must match the intended rack design - but verifying this today requires manual inspection or ad-hoc tooling. Wiring errors (swapped bond cables, servers on wrong VLAN), configuration mismatches (bond mode disagreement between host and switch), and routing failures (BGP not established between racks) are common and expensive to debug after the fact. This tool automates that validation as a structured pre-deployment check, run after MAAS commissioning but before any to-disk deployments.

## What Changes

- New CLI tool that fetches expected topology from MAAS API (fabrics, VLANs, subnets, node roles), performs pre-flight validation, and orchestrates deployment
- Flexible node targeting: `--all`, one or more `--rack` names, or hand-picked `--nodes`; `--dry-run` shows what would run without deploying
- CLI deploys Juju machine charm rack-by-rack, holds all nodes until cross-rack tests complete, auto-destroys model and releases nodes after report (opt-out via `--keep-model`)
- CLI auto-saves timestamped JSON and text reports to current directory; text summary also printed to stdout
- Each charm unit reads the topology Juju resource directly during install (not via MAAS API from nodes); the Juju peer relation carries only probe coordination state such as `probe-run-id`
- Charm invokes a self-contained probe payload (`payload/probe.py`) on each node; the payload receives the topology JSON as a file argument, runs all validators, and writes `probe-output.json`
- Probe payload runs network probes using Python stdlib parsing/socket code and standard Ubuntu tools: tcpdump for LACP/ARP capture, arping/ping for neighbor and VLAN validation, oversized ping with DF-bit for MTU, traceroute for cross-rack BGP inference
- Juju leader coordinates probe timing: all units report ready before probing begins
- Juju actions collect per-node results; reporter aggregates into a structured report with pass/fail/skip per check and remediation hints for failures

## Capabilities

### New Capabilities

- `maas-topology-fetcher`: CLI-side component that queries MAAS API to build the expected topology model - node roles, fabrics, VLANs, subnets, reachability model, and pre-flight validation; delivers topology to the charm as a Juju resource
- `cli-wrapper`: Operator-facing CLI tool providing flexible node targeting, dry-run mode, rack-by-rack deployment orchestration, Juju model lifecycle management (auto-destroy), and auto-saved timestamped reports
- `probe-runner`: Payload entry point that loads the topology JSON, resolves node identity via MAC matching, runs validators in the correct sequence (bond-validator and vlan-neighbor-validator concurrently, then mtu-validator and bgp-inference sequentially), enforces probe-timeout, and writes probe-output.json
- `bond-validator`: Captures LACP PDUs on each interface to detect bond mode mismatches between host and switch, and identifies asymmetric bond cable swaps
- `vlan-neighbor-validator`: Uses ARP and ICMP to verify each node sees the expected L2 neighbors on the expected interfaces, and only those neighbors (detects wrong-VLAN placement)
- `mtu-validator`: Probes path MTU to expected cross-rack peers using oversized ICMP with DF-bit set; reports observed per-path MTU as informational observations without pass/fail verdicts in v1
- `bgp-inference`: Tests cross-rack reachability and runs traceroute to detect where traffic stops; infers BGP session failures when traffic is blackholed at the ToR hop
- `juju-coordinator`: Juju leader/follower coordination logic ensuring all units are ready before probing starts; exposes Juju actions to trigger probing and collect results
- `report-generator`: Aggregates per-node probe results into a structured report listing passed checks and specific remediation hints for each failure

### Modified Capabilities

## Impact

- Requires Juju controller connected to MAAS as a cloud
- Requires MAAS API credentials (on operator workstation only; nodes do not need MAAS API access)
- Requires Terraform + MAAS provider to have pre-configured networking (VLANs, fabrics, bonds) in MAAS before the tool runs
- Nodes must have completed MAAS commissioning before this tool runs
- Nodes are held in MAAS ephemeral state for the duration of testing; auto-released afterward
- No switch management access required or assumed
- No persistent installation on nodes; charm and payload are ephemeral
- Probe payload requires root (MAAS ephemeral environment; CAP_NET_RAW required for LACP capture)
- No Ubuntu universe packages required on nodes: tcpdump, arping (iputils-arping), ping (iputils-ping), traceroute are all in Ubuntu main; tcpdump, iputils-arping, and traceroute are installed by the charm's install hook; ping is provided by iputils-ping
