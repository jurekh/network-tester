# Grill: Network Tester Design

## Status note

This file is historical design-capture context. The authoritative current specification is `openspec/changes/network-tester/`. Several questions below have since been resolved there: auto-destroy is the default with `--keep-model`, reports are timestamped JSON plus text, and node targeting is explicit via `--all`, `--rack`, or `--nodes`.

Date: 2026-05-14

## Intent

Validate that the physical cabling and switch/host configuration of a MAAS-managed datacenter matches the intended rack design. This is not topology discovery - the topology is known from MAAS (fabrics, VLANs, subnets, node roles, ASNs). The tool detects deviations from that known topology that actually affect traffic, and produces a structured report with remediation hints.

## Constraints

- Runs on ephemerally-deployed Ubuntu nodes via MAAS (after commissioning, before to-disk deployment) - no persistent installation
- No access to ToR switch management ports - all observations are server-side only
- Juju controller is available and connected to MAAS as a cloud - this is the orchestration layer
- Expected topology comes from MAAS API only (fabrics, subnets, VLANs, node roles); MAC-to-port mapping may not be available
- No external dependencies beyond standard Ubuntu packages on the nodes
- Target scale: 50-200 nodes, 5-20 racks (small to medium datacenter)

## Key decisions

- Decision: Use Juju machine charm for orchestration, not a hand-rolled SSH orchestrator. Reason: Juju leader/unit coordination solves the "all nodes ready, begin probing" problem cleanly, and Juju actions handle result collection naturally. The Juju controller is already present. Alternative considered: CLI tool that polls MAAS for node readiness then SSHes in - functional but fragile coordination.

- Decision: Deploy rack-by-rack (progressive batching) but hold all nodes in ephemeral until all racks are deployed and all tests complete before releasing. Reason: cross-rack checks (BGP inference, east-west MTU) require nodes from multiple racks to be reachable simultaneously. Alternative considered: two-phase approach (intra-rack first, then cross-rack with one node per rack) - adds complexity without meaningful benefit given Juju coordination is available.

- Decision: Derive expected topology from MAAS API, not a hand-authored config file. Reason: MAAS already knows node roles, fabrics, VLANs, subnets, and ASN assignments. Duplicating this in a config file creates drift. Alternative considered: YAML config file authored by operator - rejected because MAAS is already the source of truth.

- Decision: BGP failure detection is inferential (cross-rack reachability + traceroute hop analysis), not definitive. Reason: BGP terminates on ToR switches, not servers; no switch access is available. FRR-on-host approach requires pre-configured switch acceptance of test peers, which is an unreliable external dependency. Result: tool reports "traffic stops at ToR, likely BGP issue" rather than "BGP session down". Alternative considered: FRR BGP daemon on ephemeral node - deferred until switches can be pre-configured for test peers, or until hosts run BGP natively (planned for data fabric later).

- Decision: Start as standalone tool, not a built-in MAAS feature. Reason: the tool requires orchestrating across multiple nodes simultaneously, which MAAS does not currently support. Integration into MAAS is a future possibility. Alternative considered: MAAS commissioning script - rejected because commissioning is single-node and cross-rack coordination is essential.

## Surfaced assumptions

- The topology is "known but unverified" - MAAS has the intended configuration, the question is whether wires and switch configs match it.
- Symmetrically swapped bond cables (both host and switch agree on the swap) are not a problem and need not be flagged unless a MAC-to-port manifest is provided.
- BGP terminates on ToRs in v1; host-based BGP (FRR + loopback peer) is planned for the data fabric but not in scope now.
- MAAS rack controller is present in each rack, connected to all ToR switches, and can reach all BMC/OAM NICs - this is a known, validated anchor point for topology checks.
- The tool has MAAS API credentials and Juju controller access from wherever it runs (operator workstation or CI).

## Open questions

Resolved in OpenSpec:
- Auto-destroy is the default after report generation, with `--keep-model` as the debugging opt-out.
- Subset selection is operator-controlled via `--all`, one or more exact `--rack` names, or hand-picked `--nodes`.
- Reports are timestamped JSON plus text summary; text is printed to stdout and both files are saved automatically.

Still open / future:
- FRR-based BGP detection: when hosts eventually run BGP natively, does the test payload need to establish a real BGP session with the ToR, or just verify that the host's BGP daemon has an established session?

## Out of scope

- Continuous monitoring or periodic re-validation (this is a setup-time tool)
- Switch management access or switch-side BGP session introspection in v1
- Wireless network probing
- Production traffic generation or load testing
- MTU/BGP validation for subnets larger than /16 (ARP sweep bounded by configurable prefix length, default /24)
- Automatic remediation - the tool reports and hints, the operator fixes

## Failure modes the tool must catch

1. Asymmetric bond cable swap (one end crossed) - traffic breaks; detect via LACP PDU capture showing disagreement between host and switch bond mode, or via ARP/ICMP asymmetry
2. Server plugged into wrong ToR port (wrong VLAN) - detect via unexpected neighbor adjacency or failure to reach expected peers
3. Bond mode mismatch between host and switch (e.g. host LACP, switch static) - detect definitively via LACP PDU capture on the interface
4. BGP session not established between rack and upstream - infer via traceroute showing traffic stopping at ToR when cross-rack ICMP fails
5. MTU mismatch on a path - detect via oversized ICMP with DF-bit set (path MTU discovery)

Optional (requires MAC-to-port manifest as input):
- Symmetric bond cable swap - detectable when expected MACs are known per port
