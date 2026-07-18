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

### Requirement: Verify expected L2 neighbors via repeated targeted ARP sweeps
The vlan-neighbor-validator SHALL probe every expected in-scope peer with targeted `arping -I <iface> -c 1 -w 2 <peer-ip>` sweeps that repeat every 5 seconds until the 30-second capture window closes, where `<iface>` is the interface on which that peer is expected (matching fabric name and vlan_tag). Within one sweep all peer probes SHALL run concurrently (spawned before any is reaped), so sweep wall time is bounded by a single probe timeout rather than the peer count. A peer counts as observed when any sweep's probe is answered. The sweep doubles as this node's transmission that concurrent observers passively classify: probe start times skew across units by a few seconds of hook dispatch, so a single burst at window-open would be invisible to an observer whose window opened slightly later or earlier. The `-I` flag is required to bind the probe to the correct interface on multi-homed nodes; without it the kernel routes the probe via the default route, which may be a different interface. All responding MAC addresses and IPs SHALL be recorded.

#### Scenario: Expected neighbor responds to ARP
- **WHEN** a node expected to be on the same VLAN responds to a targeted arping on the correct interface during any sweep
- **THEN** the validator SHALL record the neighbor's IP and MAC on that interface as observed

#### Scenario: Expected neighbor absent from ARP responses
- **WHEN** an expected peer does not respond to any sweep's `arping -I <iface>`
- **THEN** the validator SHALL record a `missing-l2-neighbor` failure for that peer

#### Scenario: Sweeps repeat across the capture window
- **WHEN** the first sweep completes before the 30-second capture window has closed
- **THEN** the validator SHALL run further sweeps at 5-second intervals until the window closes, keeping this node's ARP traffic visible to peers whose capture windows opened at a slightly different time

### Requirement: Detect unexpected L2 neighbors via passive ARP capture
The vlan-neighbor-validator SHALL run `tcpdump -i <iface> arp -c 2000 -w - --immediate-mode` on each active non-loopback interface concurrently with the arping sweeps, held open for the full 30-second capture window. The packet cap is a memory bound only and SHALL be large enough that normal background ARP volume cannot exhaust it within the window (a cap reached early ends the capture and silently masks later traffic). All source MAC addresses seen in ARP traffic during this window SHALL be cross-referenced against the expected in-scope, expected-but-out-of-scope, and known-forbidden topology for that interface. Any MAC absent from all known sets SHALL be recorded as an `unexpected-l2-neighbor` definitive failure. A MAC present only in the expected-but-out-of-scope set SHALL be recorded as a known skipped observation, not a failure. A MAC present in the known-forbidden set SHALL be recorded as a `forbidden-l2-neighbor` definitive failure.

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

### Requirement: Report execution status per the shared probe-output schema
The `vlan_neighbor_validator` probe-output section SHALL include `validator_status` as defined by the shared probe-output schema: `complete` when probing and analysis finish, `timeout` or `cancelled` when interrupted by the probe-runner; the probe-runner writes `not_started` when the validator never ran. Completion SHALL be explicit; an empty findings list alone does not indicate success.

#### Scenario: Completed run with no findings
- **WHEN** the vlan-neighbor-validator finishes with no failures
- **THEN** its probe-output section SHALL include `validator_status: "complete"`

### Requirement: Fall back to system ping if raw socket unavailable
If raw ICMP socket creation fails due to insufficient privileges, the validator SHALL fall back to the system `ping` binary.

#### Scenario: Raw socket creation fails
- **WHEN** creating a raw ICMP socket raises PermissionError
- **THEN** the validator SHALL invoke `ping -c 3 -W 2` as a subprocess and parse its output for RTT and loss

