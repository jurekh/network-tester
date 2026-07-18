# Copyright 2026 jerzy.husakowski@canonical.com
# See LICENSE file for licensing details.

"""Deploy the packed charm to a real LXD-backed Juju model via jubilant.

Catches hook ordering, resource attachment, and relation-data propagation
that ops.testing mocks away. Needs a bootstrapped Juju controller whose
default cloud can add LXD machines (the testbed VM provides one); the packed
charm is located via NT_CHARM_PATH or charm/*.charm.
"""

import json
import os
import re
import shutil
import time
from pathlib import Path

import jubilant
import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
TOPOLOGY_FIXTURE = REPO_ROOT / "tests" / "fixtures" / "topology_single_rack.json"
APP = "network-tester"
UNIT = f"{APP}/0"
BASE = "ubuntu@24.04"

# The juju CLI is a strictly confined snap: it cannot read /root or /tmp, so
# deploy inputs (charm, resource) are staged under its snap-common directory.
JUJU_SNAP_COMMON = Path.home() / "snap" / "juju" / "common"


def find_charm():
    env = os.environ.get("NT_CHARM_PATH")
    if env:
        return Path(env)
    charms = sorted((REPO_ROOT / "charm").glob("*.charm"))
    return charms[0] if charms else None


CHARM = find_charm()

pytestmark = pytest.mark.skipif(
    CHARM is None, reason="no packed charm found (set NT_CHARM_PATH or run charmcraft pack)"
)


def wait_for_machine(juju, machine, timeout=600):
    """Wait until the machine agent answers exec; return `ip -o link show` output."""
    deadline = time.time() + timeout
    last_error = None
    while time.time() < deadline:
        try:
            return juju.exec("ip", "-o", "link", "show", machine=machine).stdout
        except Exception as exc:  # noqa: BLE001 - agent not up yet
            last_error = exc
            time.sleep(10)
    raise TimeoutError(f"machine {machine} never became reachable: {last_error}")


def test_deploy_trigger_and_collect(juju):
    # Place the machine first so its real MAC can go into the topology
    # resource; this exercises genuine MAC-based identity resolution.
    juju.cli("add-machine", "--base", BASE)
    link_output = wait_for_machine(juju, 0)
    macs = re.findall(r"link/ether ([0-9a-f:]{17})", link_output)
    assert macs, f"no MACs visible on machine 0:\n{link_output}"

    topology = json.loads(TOPOLOGY_FIXTURE.read_text())
    node = next(m for m in topology["machines"] if m["system_id"] == "bmc001")
    node["interfaces"][0]["mac"] = macs[0]

    staging = JUJU_SNAP_COMMON / "nt-integration-test"
    staging.mkdir(parents=True, exist_ok=True)
    try:
        charm_path = staging / CHARM.name
        shutil.copy(CHARM, charm_path)
        topology_path = staging / "topology.json"
        topology_path.write_text(json.dumps(topology))

        juju.deploy(
            charm_path,
            APP,
            base=BASE,
            num_units=1,
            to="0",
            resources={"topology": str(topology_path)},
        )
        status = juju.wait(
            lambda s: jubilant.all_active(s, APP), error=jubilant.any_error, timeout=900
        )
    finally:
        shutil.rmtree(staging, ignore_errors=True)
    assert status.apps[APP].units[UNIT].workload_status.message == "ready"

    # before any probe run, collect-results reports missing output
    task = juju.run(UNIT, "collect-results")
    assert task.results == {"status": "missing", "unit": UNIT}

    run_id = time.strftime("%Y%m%d-%H%M%S")
    juju.config(APP, {"probe-run-id": run_id})
    juju.wait(
        lambda s: (
            s.apps[APP].units[UNIT].workload_status.message == f"probe {run_id} complete"
            and jubilant.all_agents_idle(s, APP)
        ),
        error=jubilant.any_error,
        timeout=300,
    )

    task = juju.run(UNIT, "collect-results")
    output = json.loads(task.results["probe-output"])
    assert output["schema_version"] == "1"
    assert output["status"] == "complete"
    assert output["node"]["system_id"] == "bmc001"
    assert output["node"]["hostname"] == "r1-bmc-01"
    for section in ("bond_validator", "vlan_neighbor_validator", "mtu_validator", "bgp_inference"):
        assert output[section]["validator_status"] == "complete"
        assert output[section]["findings"] == []

    # re-applying the same run-id must not re-run the probe (hook no-ops)
    juju.config(APP, {"probe-run-id": run_id})
    juju.wait(lambda s: jubilant.all_agents_idle(s, APP), timeout=120)
    final = juju.status()
    assert final.apps[APP].units[UNIT].workload_status.message == f"probe {run_id} complete"
