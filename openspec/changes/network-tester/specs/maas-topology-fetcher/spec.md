## ADDED Requirements

### Requirement: Run on the operator workstation, not on nodes
The maas-topology-fetcher SHALL run as part of the CLI wrapper on the operator's workstation, which has MAAS API access. It SHALL NOT be invoked from within the Juju charm or from ephemeral nodes.

#### Scenario: Topology fetch runs before any deployment
- **WHEN** the operator runs `network-tester run`
- **THEN** the topology fetch SHALL complete and pass pre-flight validation before any Juju deployment commands are issued

### Requirement: Fetch node inventory and roles from MAAS API
The maas-topology-fetcher SHALL query the MAAS API to retrieve all target machines that are eligible for deployment in `ready` state, including each machine's system_id, hostname, and rack controller assignment. Rack-controller anchor records and known out-of-scope inventory used for classification SHALL be fetched independently of target deployability and SHALL NOT be dropped merely because they are not in `ready` state.

#### Scenario: Full datacenter fetch
- **WHEN** `--all` is specified
- **THEN** the fetcher SHALL return all machines in `ready` state across all racks managed by the MAAS instance

#### Scenario: Single or multiple rack fetch
- **WHEN** one or more rack names are specified via `--rack`
- **THEN** the fetcher SHALL return only machines in `ready` state assigned to those rack controllers

#### Scenario: Hand-picked node fetch
- **WHEN** specific node IDs or system_ids are specified via `--nodes`
- **THEN** the fetcher SHALL return only those machines and scope all reachability rules to that set

#### Scenario: MAAS API unreachable
- **WHEN** the MAAS API endpoint does not respond within 10 seconds
- **THEN** the fetcher SHALL raise a fatal error with the API URL and HTTP status, and halt execution

### Requirement: Identify management and data fabrics using the MAAS-designated rack controller as anchor
Before classifying node roles, the fetcher SHALL query the MAAS rack controllers API (`/MAAS/api/2.0/rackcontrollers/`) to retrieve the set of machines MAAS has designated as rack controllers. These machines are authoritative: MAAS is the source of truth for which node is the rack controller in each rack. The fetcher SHALL use each rack controller's fabric memberships to classify fabrics: fabrics where the rack controller has an interface are management fabrics; fabrics containing machines that have no interface in common with any rack controller are data fabrics.

#### Scenario: Rack controllers fetched from MAAS
- **WHEN** the fetcher queries `/MAAS/api/2.0/rackcontrollers/`
- **THEN** it SHALL use the returned machines as rack-controller anchors for fabric classification and assign them `role: rack-controller` without further derivation

#### Scenario: No rack controllers registered in MAAS
- **WHEN** the rack controllers API returns an empty list
- **THEN** the fetcher SHALL raise a fatal error: "No rack controllers found in MAAS. At least one rack controller must be registered before running this tool."

### Requirement: Derive node roles from MAAS network assignments
The fetcher SHALL classify each non-rack-controller machine based on its MAAS interface assignments and subnet memberships. Role derivation SHALL use fabric and VLAN membership, not hostname patterns. A non-rack-controller machine with one or more data-fabric interfaces SHALL be classified as `data` even if it also has management/OAM interfaces. A non-rack-controller machine with only management-fabric interfaces SHALL be classified as `bmc-oam`. Machines whose interfaces cannot be classified into management or data fabrics SHALL fail pre-flight with a clear role-classification error.

#### Scenario: Machine connected only to management fabric
- **WHEN** all of a machine's interfaces are assigned to management fabric VLANs
- **THEN** the fetcher SHALL classify it as `bmc-oam`

#### Scenario: Machine connected only to data fabric
- **WHEN** all of a machine's interfaces are assigned to data fabric VLANs
- **THEN** the fetcher SHALL classify it as `data`

#### Scenario: Machine connected to both management and data fabrics
- **WHEN** a non-rack-controller machine has at least one data-fabric interface and one or more management-fabric interfaces
- **THEN** the fetcher SHALL classify it as `data`, retain both interface classes in the topology, and mark data-fabric interfaces as eligible for MTU and BGP probing; representative and fallback selection among eligible machines happens at runtime in the validators, not in the fetcher

### Requirement: Fetch fabric, VLAN, subnet, and gateway assignments per interface
For each machine interface, the fetcher SHALL record the assigned fabric, VLAN tag, subnet CIDR, IP address, and the subnet gateway IP from MAAS. The gateway IP per subnet is used by the bgp-inference module to identify the ToR switch deterministically. Each interface record SHALL also identify its fabric class (`management` or `data`) so validators can select the correct source and target IPs on multi-homed machines.

#### Scenario: Interface with VLAN assignment
- **WHEN** a machine interface is assigned to a VLAN in MAAS
- **THEN** the fetcher SHALL record the VLAN tag, fabric name, subnet CIDR, interface MAC, and the gateway IP of that subnet as stored in MAAS

#### Scenario: Bonded interface
- **WHEN** a machine has a bond interface in MAAS with member physical interfaces listed
- **THEN** the fetcher SHALL record the bond name, bond mode as configured in MAAS, and all member interface MACs

### Requirement: Perform pre-flight validation before reporting ready
The fetcher SHALL implement pre-flight validation and expose it to the CLI wrapper as a function returning a structured list of failures (machine, missing field, description); the CLI wrapper invokes it before any deployment and owns printing failures and aborting. The fetcher SHALL validate that all selected machines have complete MAAS network configuration: at least one fabric/VLAN assignment per interface (untagged native VLAN membership is valid), fabric membership recorded, and bond mode specified for any bonded interfaces.

#### Scenario: All selected nodes have complete config
- **WHEN** every selected machine has VLAN, fabric, and bond configuration present in MAAS
- **THEN** pre-flight validation SHALL pass and the CLI wrapper SHALL proceed to deployment

#### Scenario: One or more nodes have incomplete config
- **WHEN** a selected machine has an interface with no VLAN assignment in MAAS
- **THEN** pre-flight validation SHALL fail with a list of affected machines and missing fields, and no deployment SHALL occur

### Requirement: Export topology model as JSON for attachment as a Juju resource
The fetcher SHALL serialize the topology model as a JSON file that the CLI wrapper attaches as a Juju resource when deploying the charm. The file SHALL include all selected machines plus known MAAS machines needed to classify observations for the selected scope, including same-fabric/VLAN peers, rack-controller anchors, and data machines in racks relevant to cross-rack checks. Each machine SHALL include `in_scope: true` when selected for deployment/probing and `in_scope: false` when present only for classification and skip reporting. The file SHALL include a structured `reachability_model` block defining the peer derivation rules validators use at runtime: L2 peers share the same fabric name and vlan_tag unless a role-specific rule forbids that adjacency; rack-level L3 MTU and BGP checks use data-fabric interfaces and v1 representative-sampled rack-pair semantics (`probe_scope: "rack-pair-representative"`, lexicographically lowest in-scope data `system_id` for source/target representatives, next lexicographic BGP fallback); `bmc-oam` nodes reach only the `rack-controller` in their rack. No pre-enumerated per-pair rules are stored in the file.

#### Scenario: Topology model serialized and attached
- **WHEN** the fetch and pre-flight validation complete successfully
- **THEN** the CLI wrapper SHALL attach the topology JSON as a Juju resource named `topology` before any units start probing

#### Scenario: Out-of-scope peer retained for classification
- **WHEN** a selected node shares a fabric and VLAN with a ready machine that was not selected for probing
- **THEN** the topology JSON SHALL include the unselected machine with `in_scope: false` so validators can classify its MAC as known-but-not-probed rather than unexpected
