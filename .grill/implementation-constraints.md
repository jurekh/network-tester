# Grill: Implementation Constraints and Operator UX

## Status note

This is historical design-capture context. The authoritative current specification is `openspec/changes/network-tester/`. Where this file conflicts with OpenSpec, OpenSpec wins. The notes below have been updated where practical to point at the accepted decisions.

Date: 2026-05-14

## Intent

Build a Juju machine charm that validates datacenter network topology against MAAS-configured expected state, starting with the Juju architecture from day one (no SSH prototype), targeting a real datacenter with bonded interfaces and multi-rack BGP for initial experimentation and fault-injection validation.

## Constraints

- Ubuntu LTS main repo packages only on nodes; no scapy (universe only)
- Charmcraft can bundle ops library and pure-Python dependencies (e.g. python-libmaas)
- LACP PDU capture uses `tcpdump` subprocess (EtherType 0x8809 filter, pcap output parsed with Python `struct`); raw `socket.AF_PACKET` was considered and rejected because manual IEEE 802.3ad TLV parsing produces silent wrong results on off-by-one errors
- ARP observation uses `tcpdump` passive capture during targeted `arping`; ICMP and traceroute use subprocess calls to `ping` and `traceroute`. No scapy or Ubuntu universe packages are used on nodes
- MAAS API is only reachable from the operator's workstation, not from ephemeral nodes
- Terraform + MAAS provider pre-configures networking in MAAS (VLANs, fabrics, bonds) before the tool runs; this is a stated pre-requisite, not something the tool does
- Juju controller is already connected to the MAAS instance as a cloud

## Key decisions

- Decision: Topology fetch runs in the CLI wrapper on the operator workstation, not in the Juju leader unit. Topology is passed to the charm as a Juju resource (file). Reason: ephemeral nodes do not have reliable MAAS API access. Alternative considered: leader unit fetches via MAAS metadata proxy - unreliable, not a supported path.

- Decision: Juju machine charm from the start, not an SSH-based prototype first. Reason: user has time, expects to outgrow SSH approach quickly, and wants to iterate on the Juju architecture directly. Alternative considered: single Python script via SSH - faster to first result but produces throwaway work.

- Decision: Use `tcpdump` subprocesses for LACP and passive ARP capture, then parse pcap output with Python `struct`. Reason: scapy is in Ubuntu universe, and raw `socket.AF_PACKET` packet parsing is too easy to get subtly wrong. Alternative considered: raw AF_PACKET capture - rejected for LACP and not required for ARP in the current OpenSpec.

- Decision: Auto-destroy Juju model and release nodes to MAAS after report generation, with `--keep-model` opt-out. Reason: leaving ephemeral nodes running is an expensive default mistake. Alternative considered: manual destroy with reminder prompt - too easy to forget.

- Decision: Pre-flight validation in CLI wrapper before any deployment. Reason: prevents wasting node deployment time when MAAS network config is incomplete (e.g. Terraform apply partially failed). CLI wrapper checks all selected nodes have complete VLAN/fabric/bond config in MAAS and refuses to proceed if not.

- Decision: Three-way check status: pass, fail, skip. Skips occur when a meaningful check can't be performed because a required peer wasn't included in the node selection. Skips are reported concisely and grouped (e.g. "3 intra-rack neighbor checks skipped - add node-04, node-07, node-12 to test these paths"). Alternative considered: silently exclude unrunnable checks - rejected because operator loses visibility into coverage gaps.

- Decision: Flexible node targeting: `--all`, `--rack <name>` (one or more), `--nodes <id,...>` (hand-picked), with rack-by-rack deployment but all nodes held until all cross-rack tests complete. Reason: operator needs to test subsets of a live datacenter without disturbing all nodes.

- Decision: `--dry-run` flag shows selected nodes, which checks would run, and which checks would be skipped with reasons - without deploying anything.

- Decision: Reports auto-saved to timestamped files in the current directory (`network-test-<ISO timestamp>.json` and `.txt`), text summary also printed to stdout. Reason: operator shouldn't have to redirect output to preserve results.

## Surfaced assumptions

- Terraform + MAAS provider has been applied and MAAS has complete network config for all selected nodes before the tool runs
- Ephemeral nodes have internet access or access to an Ubuntu package mirror for the charm install hook to install `tcpdump`, `iputils-arping`, and `traceroute` from Ubuntu main
- The test environment has bonded interfaces and multi-rack BGP, making it suitable for validating all target failure modes
- Fault injection for validation will include both config-level faults (bond mode, MTU) and physical cable swaps (asymmetric bond cables)

## Open questions

- Juju resource size limits: verify that the topology JSON for a 200-node datacenter fits within Juju resource constraints (tracked in OpenSpec task 13.10)
- MAAS API client: choose and pin the CLI-side MAAS client (`python-libmaas` or raw requests) as part of project packaging setup (tracked in OpenSpec task 13.11)

## Out of scope

- Scapy or any Ubuntu universe packages on nodes
- Switch management access or direct BGP session introspection
- Continuous monitoring or periodic re-validation
- Automatic remediation
- SSH-based prototype path
