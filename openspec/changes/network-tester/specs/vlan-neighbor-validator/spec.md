## ADDED Requirements

### Requirement: Derive expected peer set from topology at runtime
The vlan-neighbor-validator SHALL derive three peer sets from the topology before probing: expected in-scope peers, expected-but-out-of-scope peers, and known-forbidden peers. For each of this node's interfaces, expected in-scope L2 peers are other machines with `in_scope: true` that have an interface with the same fabric name and vlan_tag and are allowed by the reachability model. Expected-but-out-of-scope peers are machines with `in_scope: false` that would be expected by the reachability model if selected; they SHALL NOT be actively probed, but their MACs SHALL be recognized as known skipped observations. Known-forbidden peers are machines present in the topology whose MACs are known but whose adjacency is forbidden by a role-specific reachability rule; observing them SHALL be reported as a failure, not skipped. For a node with role `bmc-oam`, the expected peer set is restricted to the `rack-controller` in the same rack when that rack-controller is in scope; other bmc-oam or data nodes on the same VLAN are known-forbidden unless the reachability model explicitly allows them.

#### Scenario: Expected L2 peers derived by VLAN membership
- **WHEN** another machine in the topology has an interface with the same fabric name and vlan_tag as this node's interface
- **THEN** that machine SHALL be included in the expected in-scope peer set for that interface

#### Scenario: bmc-oam peer set restricted to rack-controller
- **WHEN** this node has role `bmc-oam`
- **THEN** the expected in-scope peer set SHALL contain only the `rack-controller` in this node's rack, regardless of shared VLAN membership

#### Scenario: Expected out-of-scope L2 peer retained
- **WHEN** another machine in the topology has `in_scope: false`, shares the same fabric name and vlan_tag as this node's interface, and is allowed by the reachability model
- **THEN** that machine SHALL be included in the expected-but-out-of-scope peer set and SHALL NOT be actively probed

#### Scenario: Known but forbidden L2 peer observed
- **WHEN** another machine in the topology shares the same fabric name and vlan_tag but is forbidden by the reachability model for this node role
- **THEN** that machine SHALL be included in the known-forbidden peer set; if its MAC is observed, the validator SHALL record a definitive `forbidden-l2-neighbor` failure rather than a skipped observation

### Requirement: Verify expected L2 neighbors via targeted ARP probes
The vlan-neighbor-validator SHALL send one targeted `arping -I <iface> -c 1 -w 2 <peer-ip>` per expected in-scope peer, where `<iface>` is the interface on which that peer is expected (matching fabric name and vlan_tag). The `-I` flag is required to bind the probe to the correct interface on multi-homed nodes; without it the kernel routes the probe via the default route, which may be a different interface. All responding MAC addresses and IPs SHALL be recorded.

#### Scenario: Expected neighbor responds to ARP
- **WHEN** a node expected to be on the same VLAN responds to a targeted arping on the correct interface
- **THEN** the validator SHALL record the neighbor's IP and MAC on that interface as observed

#### Scenario: Expected neighbor absent from ARP responses
- **WHEN** an expected peer does not respond to `arping -I <iface>` within 2 seconds
- **THEN** the validator SHALL record a `missing-l2-neighbor` failure for that peer

### Requirement: Detect unexpected L2 neighbors via passive ARP capture
The vlan-neighbor-validator SHALL run `tcpdump -i <iface> arp -c 50 -w - --immediate-mode` on each active non-loopback interface concurrently with the targeted arping probes, for the duration of the arping phase (capped at 30 seconds). All source MAC addresses seen in ARP traffic during this window SHALL be cross-referenced against the expected in-scope, expected-but-out-of-scope, and known-forbidden topology for that interface. Any MAC absent from all known sets SHALL be recorded as an `unexpected-l2-neighbor` definitive failure. A MAC present only in the expected-but-out-of-scope set SHALL be recorded as a known skipped observation, not a failure. A MAC present in the known-forbidden set SHALL be recorded as a `forbidden-l2-neighbor` definitive failure.

#### Scenario: Unexpected neighbor seen in ARP traffic
- **WHEN** a MAC address appears in passive ARP capture that is absent from both the expected in-scope peer set and the known out-of-scope peer set for that VLAN
- **THEN** the validator SHALL record an `unexpected-l2-neighbor` definitive failure with hint "Unexpected L2 neighbor MAC X (IP Y) on interface Z; this device is not in the expected topology for this VLAN"

#### Scenario: No unexpected neighbors observed
- **WHEN** all source MACs seen in ARP capture are present in the expected in-scope or known out-of-scope topology for that interface
- **THEN** no `unexpected-l2-neighbor` finding is recorded for those MACs

### Requirement: Verify L3 reachability to expected peers via ICMP
For each peer in the derived expected in-scope peer set, the vlan-neighbor-validator SHALL send ICMP echo requests and record RTT (min/avg/max) and packet loss.

#### Scenario: Expected peer is reachable
- **WHEN** ICMP echo requests to an expected peer return within timeout
- **THEN** the validator SHALL record the peer as `icmp-reachable: true` with RTT data

#### Scenario: Expected peer is not reachable
- **WHEN** ICMP echo requests to an expected peer time out with 100% loss
- **THEN** the validator SHALL record an `icmp-unreachable` failure with hint "Node X is not reachable from Y on interface Z; verify VLAN assignment and switch port config"

### Requirement: Confirm unexpected L2 neighbors are L3-reachable via follow-up ICMP
After the passive ARP capture phase completes, the vlan-neighbor-validator SHALL attempt one `ping -c 1 -W 2 <ip>` to each unexpected L2 neighbor observed (MAC absent from both the expected in-scope and known out-of-scope peer sets for that interface). If the ping succeeds, the finding SHALL be recorded as `unexpected-reachability` in addition to `unexpected-l2-neighbor`. If the ping fails, only `unexpected-l2-neighbor` is recorded. No additional probe scope is introduced: follow-up ICMP targets are limited to IPs already observed in the passive ARP capture window.

#### Scenario: Unexpected ARP neighbor is also L3-reachable
- **WHEN** passive ARP capture reveals an unexpected MAC X with IP Y, and `ping -c 1 -W 2 Y` succeeds
- **THEN** the validator SHALL record both an `unexpected-l2-neighbor` finding and an `unexpected-reachability` failure with hint "Unexpected node (MAC X, IP Y) is reachable on interface Z; node may be on incorrect VLAN"

#### Scenario: Unexpected ARP neighbor does not respond to ICMP
- **WHEN** passive ARP capture reveals an unexpected MAC X with IP Y, and `ping -c 1 -W 2 Y` times out
- **THEN** the validator SHALL record only an `unexpected-l2-neighbor` finding; no `unexpected-reachability` failure is recorded

### Requirement: Fall back to system ping if raw socket unavailable
If raw ICMP socket creation fails due to insufficient privileges, the validator SHALL fall back to the system `ping` binary.

#### Scenario: Raw socket creation fails
- **WHEN** creating a raw ICMP socket raises PermissionError
- **THEN** the validator SHALL invoke `ping -c 3 -W 2` as a subprocess and parse its output for RTT and loss

