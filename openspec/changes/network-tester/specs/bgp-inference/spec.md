## ADDED Requirements

### Requirement: Test cross-rack reachability at rack-pair granularity with representative and fallback targets
The bgp-inference module SHALL report BGP-related reachability at rack-pair granularity, not per node-pair, and SHALL run only from the deterministic representative data node for the local rack. A rack representative is the in-scope data-role machine in that rack with the lexicographically lowest `system_id`; sorting by `system_id` ensures deterministic selection across runs regardless of MAAS API response ordering. If the local node is not `role: data` or is not the selected representative for its rack, the module SHALL record an empty result with skip reason `not-rack-representative` and SHALL NOT send cross-rack probes. Remote racks are derived from the machines list by collecting distinct `rack` values of machines with `in_scope: true` and `role: data` where `rack` differs from the local node's rack. Source and target IP addresses SHALL be selected from data-fabric interfaces only; management/OAM IPs SHALL NOT be used for BGP inference. The representative target for each remote rack is the in-scope data node in that rack with the lexicographically lowest `system_id`. If the representative target probe fails, the module SHALL probe one fallback in-scope data node in that rack, selected as the next lexicographically lowest `system_id`. Only when both representative and fallback probes fail SHALL traceroute analysis be triggered and the rack-pair flagged as unreachable. If the remote rack has only one in-scope data node, the representative is the only target and its failure is sufficient to trigger traceroute. This limits cross-rack BGP probes to at most `2 * (num_racks - 1)` per source-rack representative, and only one source-rack representative runs per rack.

#### Scenario: Cross-rack ICMP to representative node succeeds
- **WHEN** an ICMP probe from rack A's representative data node reaches the representative data node in rack B
- **THEN** the module SHALL record the rack-A-to-rack-B path as `cross-rack-reachable: true` with `target_role: representative`

#### Scenario: Representative fails but fallback succeeds
- **WHEN** rack A's representative data node fails to reach rack B's representative target but the fallback in-scope data node in rack B responds
- **THEN** the module SHALL record the rack-A-to-rack-B path as `cross-rack-reachable: true` with `target_role: fallback` and add a warning that the representative host was unreachable

#### Scenario: Representative and fallback fail
- **WHEN** ICMP probes from rack A's representative data node to both the representative and fallback targets in rack B time out
- **THEN** the module SHALL record the rack-A-to-rack-B path as `cross-rack-reachable: false` and trigger traceroute analysis to the representative target

### Requirement: Run traceroute to identify where cross-rack traffic stops
When both representative and fallback cross-rack probes fail, the bgp-inference module SHALL run `traceroute -n -m 30 -q 1 -w 2` from the failing source node to the representative target and record each hop's IP and RTT, up to a maximum of 30 hops. The traceroute subprocess SHALL be terminated if it exceeds a 75-second wall-clock cap (30 hops at 1 query and a 2-second wait is 60 seconds worst case, plus margin); hops collected before termination SHALL be kept and the finding marked with `traceroute_truncated: true`.

#### Scenario: Traffic stops at the local ToR hop
- **WHEN** traceroute shows the last responding hop matches the local subnet's gateway IP from the topology model
- **THEN** the module SHALL record a `bgp-inference: likely-bgp-failure` finding with hint "Traffic from rack A stops at ToR (IP X); BGP session between rack A ToR and upstream may be down. Verify BGP configuration and peering on the ToR switch."

#### Scenario: Traffic stops beyond the local ToR
- **WHEN** traceroute shows the last responding hop is beyond the local ToR gateway but not at the destination
- **THEN** the module SHALL record a `bgp-inference: routing-failure` finding with the last responding hop IP and hint "Traffic stops at intermediate hop X; investigate routing between X and the destination rack"

#### Scenario: Traceroute reaches destination rack ToR but not the target node
- **WHEN** traceroute reaches an IP in the target rack's subnet but not the target node itself
- **THEN** the module SHALL record a `bgp-inference: intra-rack-routing` finding with hint "Cross-rack routing works but target node is unreachable within rack B; check VLAN and host configuration on the target"

#### Scenario: All traceroute hops non-responding
- **WHEN** all traceroute hops are non-responding (`* * *` for every hop up to the maximum)
- **THEN** the module SHALL record a `bgp-inference: icmp-blocked` finding with `diagnosis_confidence: inconclusive`, `scope: rack-pair`, and hint "All traceroute hops non-responding between this node and rack B; ICMP may be rate-limited or filtered end-to-end. Manual verification required."

### Requirement: Include raw traceroute hop data in findings
The bgp-inference module SHALL include the full traceroute hop list (IP, RTT per hop, or `*` for non-responding hops) in the probe output JSON alongside the finding, so operators can verify the inference without re-running the tool.

#### Scenario: Traceroute hops recorded in output
- **WHEN** a traceroute is run
- **THEN** the probe output SHALL contain a `traceroute_hops` array with one entry per hop including `hop`, `ip`, and `rtt_ms` fields

### Requirement: Label BGP findings separately from definitive failures
BGP findings SHALL include `scope: rack-pair` and SHALL NOT appear in definitive failures. Each source-rack representative unit records at most one finding per remote rack, expressed from its own perspective as a (source rack, target rack) pair; cross-unit aggregation of findings for the same rack pair into a single report entry is the report-generator's responsibility, not the probe's. Per-unit findings are single-perspective: the module SHALL NOT escalate or suppress its own findings based on assumptions about the reverse direction; the report-generator reconciles the two directions of each rack-pair link and assigns final confidence. Findings with `diagnosis_confidence: inferred` SHALL be reported as inferred failures requiring manual verification. Findings with `diagnosis_confidence: inconclusive` SHALL be reported as warnings or inconclusive active checks, not as inferred failures.

#### Scenario: BGP finding labeled as inferred
- **WHEN** a bgp-inference finding has `diagnosis_confidence: inferred`
- **THEN** the report SHALL display it under a section labeled "Inferred failures (manual verification recommended)" distinct from definitive check failures

#### Scenario: BGP finding labeled as inconclusive
- **WHEN** a bgp-inference finding has `diagnosis_confidence: inconclusive`
- **THEN** the report SHALL display it as an inconclusive active check or warning, not as a definitive or inferred failure
