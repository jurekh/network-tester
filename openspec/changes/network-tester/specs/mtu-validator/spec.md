## ADDED Requirements

### Requirement: Probe path MTU to cross-rack peers using oversized ICMP with DF-bit
The mtu-validator SHALL probe the effective path MTU only from local machines with `role: data` to cross-rack in-scope data peers. If the local node is not `role: data`, the validator SHALL skip MTU probing with an explicit empty result and skip reason. Cross-rack peers are derived from the machines list by selecting machines with `in_scope: true`, `role: data`, and `rack` different from the local node's rack. Source and target IP addresses SHALL be selected from data-fabric interfaces only; management/OAM IPs SHALL NOT be used for MTU probing. Cross-rack peers are the relevant target because MTU observations matter on routed paths; L2 neighbor pairs share link MTU and do not require MTU probing. Probing SHALL use `ping -M do -s <size> -c 1 -W 2 <target>` (iputils-ping) at multiple ICMP payload sizes: 1472 bytes (standard Ethernet: 1472 + 28-byte IP/ICMP header = 1500-byte frame) and 8972 bytes (jumbo: 8972 + 28 = 9000-byte frame), plus a binary-search refinement step if initial probes show a boundary. The validator SHALL record the final observed path MTU for each probed peer pair without pass/fail comparison; verdicting is deferred to v2. At most 7 probes are sent per peer (two initial sizes plus up to 5 binary-search iterations), bounding per-peer probe time to about 14 seconds at the 2-second per-probe timeout; the validator SHALL check the shared cancellation event between peers and stop promptly when it is set.

#### Scenario: Path supports jumbo frames end-to-end
- **WHEN** an 8972-byte `ping -M do` probe reaches the peer and returns a reply
- **THEN** the validator SHALL record `observed_path_mtu_bytes: 9000` for that peer (8972 payload + 28 header = 9000-byte frame)

#### Scenario: Path MTU limited to standard Ethernet
- **WHEN** the 8972-byte probe fails but the 1472-byte probe succeeds
- **THEN** the validator SHALL use 1500 bytes as the initial lower bound and trigger binary search refinement; the final recorded `observed_path_mtu_bytes` SHALL be the largest successful refined probe size plus 28

#### Scenario: Binary search refinement
- **WHEN** the 1472-byte probe succeeds and the 8972-byte probe fails
- **THEN** the validator SHALL binary search between 1472 and 8972 payload bytes (max 5 iterations) to find the effective path MTU; the recorded `observed_path_mtu_bytes` is the largest probe size that succeeded plus 28

#### Scenario: ICMP fragmentation-needed message received
- **WHEN** an intermediate device returns ICMP type 3 code 4 (fragmentation needed) in response to a DF-bit probe
- **THEN** the validator SHALL record the MTU indicated in the ICMP message as the effective path MTU

### Requirement: Skip MTU probing when no cross-rack peers exist
If the cross-rack peer set is empty (because the local node is not `role: data`, or no in-scope data nodes from other racks are in the topology's machines list), the mtu-validator SHALL record `{"cross_rack_mtu": [], "skip_reason": "no cross-rack data peers"}` and exit without failure.

#### Scenario: Node with no cross-rack data peers
- **WHEN** the topology machines list contains no in-scope data nodes with a different `rack` value than the local node
- **THEN** the validator SHALL write `{"cross_rack_mtu": []}` and exit with status 0

### Requirement: Record inconclusive MTU results distinctly
If ICMP probes are dropped by intermediate devices in a way that prevents determining actual path MTU, the validator SHALL record the result as `inconclusive` rather than a specific MTU value.

#### Scenario: All sized probes dropped without fragmentation-needed response
- **WHEN** both small and large ICMP probes are dropped with no response and no ICMP error returned
- **THEN** the validator SHALL record `observed_path_mtu_bytes: null, status: "inconclusive"` with note "ICMP may be filtered on this path; manual MTU verification required"
