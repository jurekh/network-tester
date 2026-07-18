# Phase 10: single-stage testbed construction findings (task 10.2)

Grounded in the MAAS source at `../maas`.

## Why the testbed had two commissions

The testbed composes node VMs through MAAS's LXD VM-host driver. MAAS can only
attach a composed VM to a host interface its resource scanner reports, and that
scanner does not report OVS datapath bridges / internal ports as host
interfaces. So compose must target the Linux bridge `br-pxe`; it cannot place a
VM on the OVS bridges (`br-rack1`, `br-rack2`) that carry the tagged
mgmt/oam/data VLANs and the LACP bonds. (Targeting an OVS bridge makes compose
fall back to unusable macvlan NICs that cannot reach DHCP.)

The dual stage was a workaround:

1. Compose each node on `br-pxe`. The compose API auto-commissions, so the node
   first-booted and commissioned once on the untagged PXE segment.
2. `ensure_node_networking` then moved/retagged the boot NIC onto its final
   segment (rack-1 mgmt VLAN, rack-2 PXE access VLAN on `br-rack2`), attached
   the data bond-member NICs (OVS, untagged) via LXD, and **recommissioned** so
   MAAS recorded the real per-segment topology.

The recommission (the second commission) existed only to record the NICs that
were attached after the auto-commission.

## The constraint that removes the second commission

`maas vm-host compose` accepts `skip_commissioning=true`
(`ComposeMachineForm`, `src/maasserver/forms/pods.py:475`). With it,
`BMC.create_machine` (`src/maasserver/models/bmc.py:953`) lands the machine in
`NODE_STATUS.READY` (line 964) and does **not** call `start_commissioning`
(line 1030). The composed LXD instance is created (stopped) and its boot NIC is
recorded from the discovered hardware, but the node never boots and never
auto-commissions.

`compose` also accepts an `interfaces` constraint map (fabric / subnet /
vlan / name), but it can only match host interfaces MAAS already knows about, so
it cannot target the OVS bridges either. It does not remove the OVS limitation.

Hardware-sync records post-deploy NIC changes, but the testbed needs the full
topology recorded while nodes are Ready (before deploy), so it does not apply.

## Chosen single-stage approach

Compose without auto-commission, attach the final NIC set via LXD while the node
is Ready and powered off, then commission exactly once:

1. `maas vm-host compose ... skip_commissioning=true` -> node Ready, powered
   off, boot NIC recorded on `br-pxe`. No commission.
2. Move/retag the boot NIC to its final segment and attach the data bond-member
   NICs via `lxc config device` on the stopped instance (unchanged from the old
   step-2 logic).
3. `maas machine commission` once. This first and only boot records the complete
   per-segment topology. Rack-2 nodes boot via `nt-rack2`, so MAAS derives rack
   2 on this single commission.

This eliminates the second commission for every node. Result: exactly one
commission per node, satisfying gate 10.8.

### Why the boot-NIC move stays (documented per the 10.3 fallback)

The boot NIC is still composed on `br-pxe` and moved to its final OVS segment
before the single commission. Composing directly onto the final boot segment is
not possible because those segments live on OVS bridges MAAS's scanner ignores;
giving rack 1 a MAAS-visible Linux bridge for the mgmt VLAN would be a fabric
redesign with no effect on the commission count. The move is now a pre-commission
LXD operation, not a separate commission cycle, so the measurable dual-stage cost
(the recommission) is gone while the boot NIC stays off the data fabric.

## Empirical result (first single-stage `nt-testbed up`, 2026-06-19)

Baseline (dual-stage) recorded first: 2 commissions per node (5 auto + 5
recommission), construction phase ~30 min (incl. one-time image import), node
topology saved to `phase10-baseline-topology.json`.

Single-stage run outcome, split by rack:

- **Rack 2: single-stage works.** Both nodes composed with skip_commissioning,
  had their boot NIC moved to `br-rack2` (vid-99) and bond members attached, then
  commissioned exactly once and reached Ready with the full per-segment topology
  (enp5s0 on 10.101.1.0/24 + two bond members). Zero recommissions.

- **Rack 1: single-stage fails as implemented.** The same flow moved the boot NIC
  onto the mgmt VLAN (vid-30) before the first commission, but the fresh VMs
  never network-booted: their taps showed only inbound MAAS beacons, no DHCP
  DISCOVER outbound, and the nodes stayed in Commissioning. The boot NIC, OVS
  access tag, `br-rack1.30` subinterface, mgmt subnet `dhcp_on=True`,
  `primary_rack`, and node `netboot=True` were all verified correct.

### Root cause: first-boot PXE must be served over a real Linux netdev

The two racks differ only in how DHCP/PXE is served on the boot segment:

- compose/enlist on `br-pxe` (a Linux bridge), and rack-2 on `nt-rack2`'s `eth1`
  (a real veth on the OVS access port) -> DHCP is served over a real netdev ->
  fresh-node PXE works.
- rack-1 mgmt/oam are served off `br-rack1.<tag>` -- an 802.1q subinterface on
  the OVS bridge LOCAL port -> a fresh node cannot PXE against it.

The dual stage hid this because rack-1's first commission was on `br-pxe` (Linux
bridge); only the second boot (recommission) used the OVS mgmt VLAN, by which
point the node was already enlisted. PXE over OVS itself is fine (rack 2 proves
it) as long as the DHCP server binds a real netdev.

### Recommended fix for full rack-1 single-stage (next iteration)

Back rack-1's boot/mgmt segments with a real netdev instead of the OVS LOCAL-port
subinterface: replace each `br-rack1.<tag>` 802.1q subinterface with a veth pair
-- one end an OVS access port on `br-rack1` (tag 30 mgmt, tag 10 oam), the other
end the testbed-VM netdev that carries the gateway IP and that the region+rack
rackd binds for DHCP/PXE (mirroring `nt-rack2`'s `eth1`). Boot NICs stay on
`br-rack1` vid-30/10 (data isolation preserved), but first-boot PXE is now served
over a Linux netdev, so rack-1 nodes commission once. This is a change to
`ensure_vlan_networks` and needs a full `up` to validate.

Until that lands, the branch-first fallback applies: keep rack-1 on the dual
stage (compose+enlist on the `br-pxe` Linux bridge, then retag to mgmt and
recommission) and take the rack-2 single-commission win.

## Veth-netdev attempt for rack-1 (second run, 2026-06-19 evening) - BLOCKED

Implemented the netdev fix: `ensure_vlan_networks` now backs each rack-1
management VLAN with a veth pair - the netdev end (`vrc<tag>`) holds the gateway
and is bound by rackd for DHCP/PXE, the peer (`vov<tag>`) is an OVS access port
(tag=<tag>) on `br-rack1`. (Code is on the branch, uncommitted.)

Result on `nt-testbed up`:

- The management VLANs configured correctly: MAAS discovered `vrc30` (mgmt,
  10.100.4.0/24) and `vrc10` (oam, 10.100.3.0/24) as physical interfaces on
  **separate fabrics** (fabric 3 vlan 5004, fabric 2 vlan 5003), distinct from
  `br-pxe` (fabric 0), and enabled DHCP on each. So the L2/VLAN model is right
  and dhcpd binds the netdevs.
- **But compose never ran:** `_wait_pod_networks` timed out -
  `FATAL: timed out waiting for rack controller to report the PXE bridge`.
  `br-pxe` was dropped from rackd's reported interface set and did not return
  even after a manual MAAS restart + 110s. `br-pxe` is healthy in the kernel
  (UP, pinned MAC, members).

Root cause: adding the veth ends as OVS ports puts four extra netdevs
(`vov30/vov10/vrc30/vrc10`) in the region+rack controller's own namespace.
rackd's interface monitor then tracks them and the region throws
`ValidationError: {'__all__': ['Interface with this Node config and Name already
exists.']}` during interface sync (network.py update_physical_interface ->
PhysicalInterface get_or_create), and `br-pxe` falls out of the report. The
extra controller-namespace netdevs disrupt rackd's interface model.

Why rack-2's veth does not hit this: `nt-rack2`'s `eth1` veth lives inside the
container's namespace, so it never appears in the region+rack controller's
interface list. The rack-1 veth puts both ends in the controller namespace.

### Revised path for rack-1 single-stage

Serve rack-1's mgmt/oam PXE from a netdev in a **separate namespace** rather than
the controller's own - i.e. a dedicated rack-controller container for rack-1
(the `nt-rack2` shape) with veth NICs on `br-rack1` tags 30/10. This keeps the
controller namespace clean (no `vov*` pollution) and serves PXE over a real
netdev. Implication to design: rack-1 nodes would then derive that controller's
rack rather than the region+rack node, which the fetcher/multirack checks must
still resolve to "rack 1" - verify before committing. This is an architecture
change needing iterative `up` validation, not a one-line fix.

## Resolution: a Linux bridge per rack-1 management VLAN (third run, works)

The fix is simpler than the separate-namespace idea: do not put the rack-1
boot/management segments on OVS at all. Give mgmt and oam each their own Linux
bridge (`br-mgmt`, `br-oam`) on the testbed VM, served by the region+rack rackd
exactly like `br-pxe`. Only the data VLAN stays on OVS (`br-rack1`), where the
LACP bond needs it. PXE over OVS itself is fine; the real requirement is that
DHCP/PXE be served over a real Linux netdev, and a dedicated Linux bridge is the
cleanest such netdev (no LOCAL-port subinterface, no veth-into-OVS, no
controller-namespace pollution).

Implementation:
- `topology.yaml`: `vlans.mgmt`/`oam` gain `bridge: br-mgmt`/`br-oam` and drop
  `tag`; `data` keeps `tag: 100`. Rack-1 boot NICs point at `br-mgmt`/`br-oam`.
- `ensure_vlan_networks`: create each Linux bridge (pinned MAC, gateway, NAT),
  then bind the MAAS subnet/range/DHCP to the discovered bridge (subnet follows
  interface) - the same pattern as br-pxe.
- boot-NIC tag logic, `_node_tagged_nics`, and the `wrong-vlan` fault handle
  bridge-backed (untagged) VLANs; `wrong-vlan` now moves a data node's bond
  members onto `br-oam` (the bonded-topology equivalent of the old retag).
- `skip_commissioning` kept for both racks.

Validated on a clean `nt-testbed up` (2026-06-19/20):
- br-pxe, br-mgmt, br-oam all reported by rackd; **zero** ValidationErrors.
- Rack-1 nodes PXE-boot on br-mgmt/br-oam and reach Ready on a single
  commission; rack-2 likewise on br-rack2. **5 single commissions, 0
  recommissions.**
- Fetcher parity: per-node interfaces/subnets/IPs/bond structure identical to
  the dual-stage baseline; rack derivation unchanged (rack-1 -> nt-testbed,
  rack-2 -> nt-rack2).
- Wall-clock ~28 min start-to-"testbed is up" (full, incl. Juju), not worse than
  the ~30 min dual-stage construction; `up` is idempotent on re-run.

The boot-NIC move from br-pxe to its management bridge remains (compose still
targets the MAAS-visible br-pxe), but it is a pre-commission LXD operation, not
a second commission. The dual stage (the recommission) is eliminated for every
node.

## Deploy/validation pass; node loss at destroy is environmental (not phase 10)

Initial misread (corrected): `verify skeleton` lost all 5 nodes at the Juju
model auto-destroy, and I wrongly attributed it to the Linux-bridge management.
An isolating test disproved that:

- `network-tester run --all --keep-model` (deploy -> probe -> report, **no**
  destroy): all 5 units reach active/idle, probes run, **"All 4 checks passed"**,
  report written, **nodes stay at 5** the whole time. So construction, deploy,
  and the validators all work on the br-mgmt/br-oam topology.
- A clean `juju destroy-model` (no `--force`) on that kept model then deletes
  all 5 nodes (machines read -> 0, inner LXD VMs decomposed).

Why this is **not** caused by the single-stage change:
- The nodes are `dynamic=False` (confirmed in the MAAS DB) and
  `enable_disk_erasing_on_release` is off. Per MAAS `Node._finalize_release`
  (node.py: `if self.dynamic: delete() else: READY`), release of a dynamic=False
  machine sets it **Ready**, and with erase off the node is not even booted on
  release. So MAAS's own release path would not delete these nodes and cannot
  re-trigger any controller-interface issue (no node boot happens).
- Therefore the delete is **Juju's `destroy-model` decomposing the pod-acquired
  machines** - a Juju<->MAAS-pod behavior. `nt-testbed` runs the *same*
  `vm-host compose` (dynamic=False) and the *same* Juju deploy/destroy for dual
  and single stage; the single-stage change touches none of that code. So this
  decompose is identical for the dual stage.

The `Failed processing commissioning data: Interface ... already exists` events
the controller logs are non-fatal commissioning-time noise (node taps on the
controller's management bridges); `up`, deploy, and validation all succeed
through them.

**Consequence:** the single-stage construction is validated *through deploy and
validation*. `verify skeleton`'s post-destroy `wait_nodes_ready` cannot pass
while `juju destroy-model` decomposes the nodes - but that is an environmental /
testbed-infra issue that hits the dual stage identically, orthogonal to phase
10. To run the full matrix, the destroy-decompose must be resolved (understand
why Juju decomposes these static pod machines, or recompose between deploy
stages). A direct dual-stage deploy+destroy would confirm the decompose is
identical there.

**Branch state:** `testbed-single-stage` - single-stage construction + deploy +
validation proven; 10.8 full matrix blocked only by the environmental
destroy-decompose, not by the single-stage change.

## NEXT (deferred per user, 2026-06-20): definitive confirmation before deciding

Before the final choice (keep single-stage vs revert to dual stage), run the
direct confirmation: revert the working tree to the committed dual stage,
`nt-testbed down && up`, deploy via `network-tester run --all --keep-model`, then
`juju destroy-model` and check `maas admin machines read`. Expected: the dual
stage decomposes the nodes identically (confirming the destroy-decompose is
environmental and the single-stage change is innocent). Deferred this session
(out of usage window); testbed torn down.
