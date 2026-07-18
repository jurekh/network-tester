## ADDED Requirements

### Requirement: Capture LACP PDUs on bonded interfaces using tcpdump
The bond-validator SHALL capture inbound LACP PDUs (EtherType 0x8809) on each physical member interface of every bond by running `tcpdump -i <iface> -Q in ether proto 0x8809 -c 10 -w - --immediate-mode` as a subprocess with a 35-second wall-clock timeout; the capture terminates when 10 packets are received or 35 seconds elapse, whichever comes first. `-Q in` restricts the capture to received frames: without it tcpdump also records the host bond's own outgoing LACPDUs, which would mask a static switch port (the requirement is to detect what the switch advertises). The window MUST exceed 30 seconds because a partner transmitting at the default LACP slow rate sends one PDU every 30 seconds; a shorter window would record `lacp_advertised: false` on correctly configured LACP switch ports. Captures for all member interfaces of all bonds SHALL run concurrently, so total capture wall-clock time is one window regardless of member count. The captured pcap output SHALL be parsed using Python `struct` to extract LACP TLV fields: actor system ID, actor port key, actor state flags (active/passive, aggregatable, in-sync), and partner system ID and port key. No third-party packet capture or parsing libraries SHALL be required.

#### Scenario: Switch advertising LACP on a member interface
- **WHEN** the switch port is configured for LACP
- **THEN** the validator SHALL record at least one LACP PDU with the switch's system ID, port key, and state flags within the capture window

#### Scenario: No LACP PDUs received on a member interface
- **WHEN** no LACP PDUs are received within 35 seconds on a member interface
- **THEN** the validator SHALL record the interface as `lacp_advertised: false`, indicating the switch port is configured for static bonding or for passive LACP facing a passive host

### Requirement: Produce empty bond list when node has no bonded interfaces
If a node has no bonded interfaces (e.g., a bmc-oam node or a data node with a single NIC), the bond-validator SHALL record `validator_status: "complete"` with `bonds: []` in the probe output and exit without failure. This keeps the output schema consistent for the report generator.

#### Scenario: Node with no bonded interfaces
- **WHEN** `/proc/net/bonding/` contains no bond files
- **THEN** the validator SHALL write `{"validator_status": "complete", "bonds": []}` to the probe output and exit with status 0

### Requirement: Detect bond mode mismatch between host configuration and switch behavior
The bond-validator SHALL compare the host's configured bond mode (read from `/proc/net/bonding/<bond>` or `ip link`) against what the switch advertises via LACP PDUs. A mismatch SHALL be recorded as a failure with a remediation hint.

#### Scenario: Host configured LACP, switch advertising static
- **WHEN** the host bond is configured with `mode=802.3ad` but no LACP PDUs are received from the switch
- **THEN** the validator SHALL record a `bond-mode-mismatch` failure with hint "Switch port may be configured for static bonding; verify switch port-channel mode"

#### Scenario: Host configured static, switch advertising LACP
- **WHEN** the host bond is configured with `mode=balance-xor` or `mode=active-backup` but LACP PDUs are received from the switch
- **THEN** the validator SHALL record a `bond-mode-mismatch` failure with hint "Host bond mode is static but switch is sending LACP; update host bond configuration to mode=802.3ad"

#### Scenario: Host and switch both using LACP
- **WHEN** the host bond is `mode=802.3ad` and LACP PDUs are received with active state flags
- **THEN** the validator SHALL record the bond as `bond_mode: pass`

### Requirement: Account for LACP passive mode in bond mode diagnosis
The bond-validator SHALL decode the LACP activity flag (active vs passive) from captured switch PDUs and SHALL read the host bond's `lacp_active` setting from `/proc/net/bonding/<bond>` (an absent setting means active, the kernel default). A passive end does not transmit PDUs until it hears from an active partner, so two passive ends never form an aggregate and produce no capturable PDUs. The diagnosis SHALL distinguish this case from a static switch port in remediation hints, and the activity flag SHALL be recorded in the raw PDU audit data.

#### Scenario: Host LACP-passive and no PDUs received
- **WHEN** the host bond is `mode=802.3ad` with `lacp_active off` and no LACP PDUs are received within the capture window
- **THEN** the validator SHALL record a `bond-mode-mismatch` failure with hint "No LACP PDUs received and host bond is LACP-passive (lacp_active off); the switch port is either static or also passive, so neither end initiates negotiation. Set lacp_active on for the host bond or verify the switch port-channel mode."

#### Scenario: Switch passive, host active
- **WHEN** the host bond is `mode=802.3ad` with LACP active and captured PDUs have the activity flag unset
- **THEN** the validator SHALL record the bond as `bond_mode: pass` (an active host drives negotiation with a passive switch) and include the switch's passive activity flag in the raw PDU audit data

### Requirement: Detect asymmetric bond cable swaps via LACP port key and interface assignment
The bond-validator SHALL check that each physical member interface receives LACP PDUs from a consistent remote switch identity. For inbound LACP PDUs captured on a host interface, the transmitting switch is the LACP actor, so the remote switch identity SHALL be decoded from the actor system ID and actor port key. The partner system ID in an inbound switch PDU describes the switch's view of the local host and SHALL NOT be used as the primary remote-switch identity. If interface eth0 and eth1 are both members of bond0 but receive inbound PDUs with different actor system IDs, this indicates a cable is connected to a different switch than expected.

#### Scenario: Bond member interfaces receive PDUs from different switch system IDs
- **WHEN** eth0 receives an inbound LACP PDU with actor system ID AA:BB:CC:DD:EE:01 and eth1 receives one with actor system ID AA:BB:CC:DD:EE:02
- **THEN** the validator SHALL record an `asymmetric-bond-cable` failure with hint "Bond members are connected to different switches; check physical cabling for bond0"

#### Scenario: Both bond members receive PDUs from the same switch system ID
- **WHEN** both eth0 and eth1 receive inbound LACP PDUs with the same actor system ID
- **THEN** the validator SHALL record the bond as `bond_cabling: pass`

### Requirement: Report execution status per the shared probe-output schema
The `bond_validator` probe-output section SHALL include `validator_status` as defined by the shared probe-output schema: `complete` when capture and analysis finish (including the no-bonds case), `timeout` or `cancelled` when interrupted by the probe-runner; the probe-runner writes `not_started` when the validator never ran. Completion SHALL be explicit; an empty findings list alone does not indicate success.

#### Scenario: Completed run with no findings
- **WHEN** the bond-validator finishes with no failures
- **THEN** its probe-output section SHALL include `validator_status: "complete"`

### Requirement: Record raw LACP PDU data in probe output for audit
The bond-validator SHALL include the raw decoded LACP PDU fields in the probe output JSON alongside the pass/fail verdict, to allow manual review without re-running the tool.

#### Scenario: LACP PDU fields recorded
- **WHEN** an LACP PDU is captured
- **THEN** the output SHALL include actor system ID, actor port key, actor state, partner system ID, partner port key, and partner state for that PDU
