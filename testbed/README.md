# network-tester testbed

Single-machine nested-virtualization testbed (design D18). Everything runs
inside one outer LXD VM named `nt-testbed`: MAAS (region+rack snap backed by
the `maas-test-db` snap), an inner LXD registered as a MAAS VM host that
composes the node VMs, Open vSwitch bridges acting as ToR switches, and a
Juju controller bootstrapped on the inner LXD with the testbed MAAS
registered as the `maas-testbed` cloud. The controller container sits on the
management/PXE bridge, outside the topology under test, so data-fabric fault
injection cannot sever Juju connectivity. The testbed VM also plays the
operator-workstation role: the working tree is synced into it and the
network-tester CLI runs from there.

## Host prerequisites

- LXD installed and initialised (`lxc` usable by your user)
- uv (runs the `nt-testbed` script and its PyYAML dependency)
- Nested KVM support: `/dev/kvm` present and
  `cat /sys/module/kvm_*/parameters/nested` reports `Y`
- Default profile: ~24 GB free RAM and ~100 GB free disk
  (the VM is 8 CPU / 16 GiB RAM / 100 GiB disk; the composed nodes run
  inside it)

Reduced profile for smaller hosts: edit `topology.yaml` and lower `vm.cpu`
to 4 and `vm.memory` to `12GiB`; MAAS plus the two 2 GiB nodes still fit,
but commissioning is slower.

The MAAS version is pinned by `maas.channel` in `topology.yaml`
(e.g. `3.7/stable`); the `maas-test-db` snap follows the same channel.
Changing the channel does not affect an existing testbed: `up` warns about
the mismatch and you rebuild with `nt-testbed down && nt-testbed up`.

## Usage

```sh
testbed/nt-testbed up                  # create/refresh everything (idempotent)
testbed/nt-testbed verify foundation   # assert composed machines are Ready
testbed/nt-testbed verify topology     # dry-run + pre-flight fault round-trip
testbed/nt-testbed verify skeleton     # full deploy/probe/collect/report cycle
testbed/nt-testbed verify vlan         # VLAN neighbor checks + wrong-vlan fault
testbed/nt-testbed verify bond         # LACP bond checks + bond faults
testbed/nt-testbed status              # VM / MAAS / machine state
testbed/nt-testbed shell               # shell inside the testbed VM
testbed/nt-testbed fault clear         # restore topology
testbed/nt-testbed down                # delete the VM, assert no host leftovers
```

## Fault catalog

`fault <name> <hostname>` injects a fault; `fault clear` restores all state
from `topology.yaml`.

- `incomplete-config <node>` - removes a node's boot-interface subnet link
  (pre-flight failure).
- `wrong-vlan <node>` - retags a node's data NIC onto the oam VLAN.
- `bond-static <node>` - disables LACP on the node's OVS bond
  (`bond-mode-mismatch`, static-switch hint).
- `bond-passive <node>` - sets the OVS bond and the deployed node's kernel
  bond both LACP-passive (`bond-mode-mismatch`, both-passive hint).
- `bond-swap <node>` - moves one bond member into a second OVS bond with a
  different LACP system id (`asymmetric-bond-cable`).

`up` is idempotent and resumable: every step checks current state before
acting, so re-running it after a failure continues where it stopped. The
first `up` downloads MAAS boot images and commissions the nodes; expect
30-60 minutes depending on mirror speed. Subsequent `up` runs on an existing
testbed take seconds and re-sync the working tree.

The MAAS UI is reachable from the host at `http://<vm-address>:5240/MAAS`
(credentials `admin` / `testbed`); `up` and `status` print the address.

## State and teardown

All testbed state lives inside the `nt-testbed` VM. `down` runs
`lxc delete --force nt-testbed` and then asserts no host-side leftovers
(no extra bridges, profiles, or networks). The only host-side artifact LXD
keeps is its cached `ubuntu:24.04` image, which is LXD's normal image cache
and not testbed state.

## Layout

- `topology.yaml` - declarative testbed config: outer VM profile, PXE
  subnet/DHCP range, racks and nodes. Later stages extend this file with
  data VLANs, bonds, rack 2, and inter-rack links.
- `nt-testbed` - idempotent entry script (Python, runs via uv). Later stages
  only add `verify` stages and `fault` cases.

## verify skeleton

`verify skeleton` exercises the full walking-skeleton pipeline inside the
VM: it packs the charm (on the host when charmcraft is available, otherwise
inside the VM with `--destructive-mode`), runs the jubilant charm
integration test against an LXD-backed model, then runs
`network-tester run --all` on the `maas-testbed` cloud twice - once
verifying report files, model auto-destroy, and nodes returning to Ready;
once with `--keep-model` followed by `status` and `--reuse-model`. Each
MAAS deploy cycle takes 10-25 minutes on nested virtualization.

## verify bond

`verify bond` deploys with `--keep-model`, confirms the bond-validator
captures real LACP PDUs from the OVS bond within the 35 s slow-rate window
(reporting `bond_mode` and `bond_cabling` as `pass`), then injects
`bond-static`, `bond-passive`, and `bond-swap` in turn and re-triggers via
`--reuse-model`, asserting each produces its expected finding and hint.

Each data node's two extra member NICs are aggregated into an OVS bond
(`lacp=active`, `other_config:lacp-time=slow`) carrying the data VLAN; the
node runs a matching MAAS 802.3ad bond with `bond_lacp_rate=slow`. The boot
NIC stays on the mgmt VLAN so commissioning and the Juju agent never depend
on the bonded data fabric.

### OVS-vs-real-switch caveats

The OVS bond emulates a switch port-channel, which differs from real switch
hardware in ways worth keeping in mind:

- OVS bonds require at least two member interfaces, so `bond-swap` pads each
  half with an internal dummy interface; a real swap simply moves a cable.
- The "wrong switch" identity in `bond-swap` is faked with
  `other_config:lacp-system-id`; on real hardware the distinct identity comes
  from a physically different switch chassis.
- OVS speaks LACP toward a single host's two veths, whereas a real
  port-channel aggregates links between two switches. The negotiated result
  (actor system id, port keys, activity flags) is what the validator reads,
  and that is faithful; the underlying medium is not.
- `lacp-time=slow` on OVS plus `bond_lacp_rate=slow` on the host means one
  PDU every 30 s, so the 35 s capture window is the minimum that reliably
  records a PDU. Real-switch confirmation of this window stays in the stage 9
  runbook.
