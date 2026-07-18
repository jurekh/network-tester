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
testbed/nt-testbed status              # VM / MAAS / machine state
testbed/nt-testbed shell               # shell inside the testbed VM
testbed/nt-testbed fault clear         # restore topology (faults arrive in later stages)
testbed/nt-testbed down                # delete the VM, assert no host leftovers
```

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
