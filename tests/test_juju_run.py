"""Juju orchestration against a fake facade: placement, waits, collection."""

import asyncio
import json
import time

import pytest
from conftest import FIXTURES, load_fixture

from cli import juju_run
from cli.juju_run import JujuError, run_new, run_reuse, status_lines

TOPOLOGY = load_fixture(FIXTURES / "topology_mixed_scope.json")
IN_SCOPE_HOSTNAMES = ["r1-data-01", "r1-data-02", "r2-data-02", "r2-data-03"]


def probe_output_for(machine):
    doc = load_fixture(FIXTURES / "probe_output_complete.json")
    doc["node"]["system_id"] = machine["system_id"]
    doc["node"]["hostname"] = machine["hostname"]
    return doc


class FakeFacade:
    """Scripted facade: placements succeed unless listed in fail_placement;
    units become active/idle immediately unless listed in stuck_units; after
    set_config(probe-run-id) units report the probe-complete message."""

    def __init__(
        self,
        models=(),
        fail_placement=(),
        stuck_units=(),
        missing_output_units=(),
        stale_output_units=(),
        unit_files=None,
        existing_units=None,
    ):
        self.models = list(models)
        self.fail_placement = set(fail_placement)
        self.stuck_units = set(stuck_units)
        self.missing_output_units = set(missing_output_units)
        self.stale_output_units = set(stale_output_units)
        self.unit_files = unit_files or {}
        self.added_units = []  # (model, placement)
        self.config_calls = []
        self.actions = []
        self.destroyed = []
        self._machines = {}  # machine id -> hostname
        self._units = dict(existing_units or {})  # unit -> status dict
        self._next = 0
        self._run_id = None
        self._machines_by_hostname = {m["hostname"]: m for m in TOPOLOGY["machines"]}

    async def list_models(self):
        return list(self.models)

    async def add_model(self, name, cloud=None):
        self.models.append(name)
        self.cloud = cloud

    async def destroy_model(self, name):
        self.destroyed.append(name)
        self.models.remove(name)

    async def deploy(self, name, charm_path, resources):
        self.deployed = (name, charm_path, dict(resources))

    async def add_unit(self, name, placement):
        if placement in self.fail_placement:
            raise JujuError(f"MAAS cannot allocate {placement}")
        self.added_units.append((name, placement))
        machine_id = str(self._next)
        self._machines[machine_id] = placement
        self._next += 1
        unit = f"network-tester/{machine_id}"
        self._units[unit] = {
            "workload": "active",
            "message": "ready",
            "agent": "idle",
            "machine": machine_id,
            "hostname": placement,
        }
        return unit

    async def set_config(self, name, values):
        self.config_calls.append((name, dict(values)))
        self._run_id = values.get("probe-run-id")
        for unit, status in self._units.items():
            if unit in self.stuck_units:
                continue
            if status["workload"] == "active" and status["agent"] == "idle":
                status["message"] = f"probe {self._run_id} complete"

    async def unit_statuses(self, name):
        return {unit: dict(status) for unit, status in self._units.items()}

    async def run_action(self, name, unit, action):
        self.actions.append((name, unit, action))
        if unit in self.missing_output_units:
            return {"status": "missing", "unit": unit}
        hostname = self._units[unit]["hostname"]
        machine = self._machines_by_hostname.get(
            hostname, {"system_id": "x", "hostname": hostname}
        )
        doc = probe_output_for(machine)
        # the charm stamps the triggered run-id into the payload output
        doc["probe_run_id"] = (
            "19990101-000000" if unit in self.stale_output_units else self._run_id
        )
        return {"probe-output": json.dumps(doc)}

    async def cat_file(self, name, unit, path):
        key = (unit, path)
        if key not in self.unit_files:
            raise JujuError(f"no such file on {unit}")
        return self.unit_files[key]


def run(coro):
    return asyncio.run(coro)


@pytest.fixture(autouse=True)
def _raw_results_under_tmp_path(tmp_path, monkeypatch):
    """Keep the raw-results directory out of the real /tmp during tests."""

    def fake_mkdtemp(prefix=""):
        raw = tmp_path / f"{prefix}raw"
        raw.mkdir(exist_ok=True)
        return str(raw)

    monkeypatch.setattr(juju_run.tempfile, "mkdtemp", fake_mkdtemp)


# --- explicit placement (4.22, 4.23) ---------------------------------------------


def test_run_new_places_exactly_the_selected_machines():
    facade = FakeFacade()
    model, collected, missing = run(run_new(facade, TOPOLOGY, "nt.charm", 60, poll=0))

    placements = [p for _, p in facade.added_units]
    assert placements == IN_SCOPE_HOSTNAMES  # rack-1 then rack-2, nothing else
    assert "r1-data-03" not in placements  # out of scope, never substituted
    assert "rack1-controller" not in placements
    assert missing == []
    assert model.startswith("network-test-")
    assert sorted(d["node"]["hostname"] for d in collected.values()) == IN_SCOPE_HOSTNAMES


def test_run_new_attaches_topology_resource_and_cloud():
    facade = FakeFacade()
    run(run_new(facade, TOPOLOGY, "nt.charm", 60, cloud="maas-testbed", poll=0))
    name, charm_path, resources = facade.deployed
    assert charm_path == "nt.charm"
    assert "topology" in resources
    assert facade.cloud == "maas-testbed"


def test_run_new_refuses_when_existing_model_present():
    facade = FakeFacade(models=["network-test-20260601-000000"])
    with pytest.raises(JujuError, match="Existing test model"):
        run(run_new(facade, TOPOLOGY, "nt.charm", 60, poll=0))


def test_placement_failure_recorded_and_other_nodes_proceed():
    facade = FakeFacade(fail_placement={"r1-data-02"})
    _, collected, missing = run(run_new(facade, TOPOLOGY, "nt.charm", 60, poll=0))
    assert [p for _, p in facade.added_units] == [
        "r1-data-01",
        "r2-data-02",
        "r2-data-03",
    ]
    assert [(m["hostname"], m["reason"]) for m in missing] == [("r1-data-02", "placement-failed")]
    assert sorted(d["node"]["hostname"] for d in collected.values()) == [
        "r1-data-01",
        "r2-data-02",
        "r2-data-03",
    ]


# --- waits, trigger, and collection (4.24, 4.25) ----------------------------------


def test_probe_run_id_set_once_after_units_ready():
    facade = FakeFacade()
    run(run_new(facade, TOPOLOGY, "nt.charm", 60, poll=0))
    assert len(facade.config_calls) == 1
    _, values = facade.config_calls[0]
    assert values["probe-run-id"]


def test_probe_timeout_set_with_run_id_when_provided():
    facade = FakeFacade()
    run(run_new(facade, TOPOLOGY, "nt.charm", 60, poll=0, probe_timeout=30))
    _, values = facade.config_calls[0]
    assert values["probe-run-id"]
    assert values["probe-timeout"] == "30"


def test_probe_timeout_omitted_when_not_provided():
    facade = FakeFacade()
    run(run_new(facade, TOPOLOGY, "nt.charm", 60, poll=0))
    _, values = facade.config_calls[0]
    assert "probe-timeout" not in values


def test_stuck_unit_recorded_as_probe_timeout():
    facade = FakeFacade(stuck_units={"network-tester/0"})
    _, collected, missing = run(run_new(facade, TOPOLOGY, "nt.charm", 0.2, poll=0))
    reasons = {m["hostname"]: m["reason"] for m in missing}
    assert reasons == {"r1-data-01": "probe-timeout"}
    assert "network-tester/0" not in collected
    assert len(collected) == 3


def test_missing_probe_output_recorded():
    facade = FakeFacade(missing_output_units={"network-tester/1"})
    _, collected, missing = run(run_new(facade, TOPOLOGY, "nt.charm", 60, poll=0))
    assert [(m["hostname"], m["reason"]) for m in missing] == [("r1-data-02", "no-probe-output")]
    assert len(collected) == 3


def test_stale_probe_output_recorded_not_aggregated():
    """A unit whose collected output carries a different run-id than the one
    just triggered must not contribute results (the status-message gate is
    the primary freshness guard; this is the cross-check on the document)."""
    facade = FakeFacade(stale_output_units={"network-tester/1"})
    _, collected, missing = run(run_new(facade, TOPOLOGY, "nt.charm", 60, poll=0))
    assert [(m["hostname"], m["reason"]) for m in missing] == [
        ("r1-data-02", "stale-probe-output")
    ]
    assert len(collected) == 3


def test_probe_start_at_set_with_run_id_when_delay_given():
    facade = FakeFacade()
    before = int(time.time())
    run(run_new(facade, TOPOLOGY, "nt.charm", 60, poll=0, probe_start_delay=30))
    _, values = facade.config_calls[0]
    assert int(values["probe-start-at"]) >= before + 30


def test_probe_start_at_omitted_when_delay_zero():
    facade = FakeFacade()
    run(run_new(facade, TOPOLOGY, "nt.charm", 60, poll=0, probe_start_delay=0))
    _, values = facade.config_calls[0]
    assert "probe-start-at" not in values


# --- reuse mode (4.28) -------------------------------------------------------------


def reuse_facade(units, files=True):
    model = "network-test-20260612-000000"
    unit_files = {}
    if files:
        for unit in units:
            unit_files[(unit, juju_run.TOPOLOGY_ON_NODE)] = json.dumps(TOPOLOGY)
    facade = FakeFacade(models=[model], unit_files=unit_files, existing_units=units)
    return model, facade


def test_reuse_retriggers_and_collects():
    units = {
        "network-tester/0": {
            "workload": "active",
            "message": "probe 20260611-090000 complete",
            "agent": "idle",
            "machine": "0",
            "hostname": "r1-data-01",
        },
        "network-tester/1": {
            "workload": "active",
            "message": "probe 20260611-090000 complete",
            "agent": "idle",
            "machine": "1",
            "hostname": "r1-data-02",
        },
    }
    model, facade = reuse_facade(units)
    facade._machines = {"0": "r1-data-01", "1": "r1-data-02"}
    topology, collected, missing, warnings = run(run_reuse(facade, model, 60, poll=0))
    assert topology["machines"]
    assert len(facade.config_calls) == 1
    assert sorted(collected) == ["network-tester/0", "network-tester/1"]
    assert missing == [] and warnings == []


def test_reuse_warns_and_records_bad_state_units():
    units = {
        "network-tester/0": {
            "workload": "active",
            "message": "ready",
            "agent": "idle",
            "machine": "0",
            "hostname": "r1-data-01",
        },
        "network-tester/1": {
            "workload": "error",
            "message": "hook failed",
            "agent": "idle",
            "machine": "1",
            "hostname": "r1-data-02",
        },
    }
    model, facade = reuse_facade(units)
    facade._machines = {"0": "r1-data-01", "1": "r1-data-02"}
    topology, collected, missing, warnings = run(run_reuse(facade, model, 60, poll=0))
    assert sorted(collected) == ["network-tester/0"]
    assert [(m["hostname"], m["reason"]) for m in missing] == [("r1-data-02", "no-probe-output")]
    assert any("network-tester/1" in w for w in warnings)


def test_reuse_unknown_model_fails():
    facade = FakeFacade()
    with pytest.raises(JujuError, match="not found"):
        run(run_reuse(facade, "network-test-nope", 60, poll=0))


def test_reuse_without_topology_fails_with_guidance():
    units = {
        "network-tester/0": {
            "workload": "active",
            "message": "ready",
            "agent": "idle",
            "machine": "0",
            "hostname": "r1-data-01",
        }
    }
    model, facade = reuse_facade(units, files=False)
    with pytest.raises(JujuError, match="topology resource is unavailable"):
        run(run_reuse(facade, model, 60, poll=0))


# --- status subcommand (4.29) ------------------------------------------------------


def status_units():
    return {
        "network-tester/0": {
            "workload": "active",
            "message": "probe 20260612-103000 complete",
            "agent": "idle",
            "machine": "0",
            "hostname": "r1-data-01",
        }
    }


def test_status_no_models():
    facade = FakeFacade()
    code, lines = run(status_lines(facade))
    assert code == 0
    assert lines == ["No network-test models found."]


def test_status_single_model_shown_directly():
    facade = FakeFacade(models=["network-test-1"], existing_units=status_units())
    code, lines = run(status_lines(facade))
    assert code == 0
    assert lines[0].startswith("Model network-test-1")
    assert any("probe complete" in line for line in lines)


def test_status_multiple_models_requires_choice():
    facade = FakeFacade(models=["network-test-1", "network-test-2"])
    code, lines = run(status_lines(facade))
    assert code == 2
    assert "network-test-1" in lines and "network-test-2" in lines
    assert lines[-1] == "Specify a model name: `network-tester status <model-name>`"


def test_status_explicit_model_argument():
    facade = FakeFacade(models=["network-test-1", "network-test-2"], existing_units=status_units())
    code, lines = run(status_lines(facade, "network-test-2"))
    assert code == 0
    assert lines[0].startswith("Model network-test-2")


# --- destruction completion (4.27) -------------------------------------------------


class DyingModelFacade:
    """list_models keeps reporting the model for `gone_after` calls, like a
    libjuju controller whose destroy_models call only initiated destruction."""

    def __init__(self, name, gone_after):
        self.name = name
        self.gone_after = gone_after
        self.calls = 0

    async def list_models(self):
        self.calls += 1
        if self.calls > self.gone_after:
            return []
        return [self.name]


def test_wait_model_destroyed_polls_until_model_gone():
    facade = DyingModelFacade("network-test-1", gone_after=3)
    run(juju_run.wait_model_destroyed(facade, "network-test-1", timeout=5, poll=0))
    assert facade.calls == 4


def test_wait_model_destroyed_times_out_with_guidance():
    facade = DyingModelFacade("network-test-1", gone_after=10**6)
    with pytest.raises(JujuError, match="network-test-1"):
        run(juju_run.wait_model_destroyed(facade, "network-test-1", timeout=0, poll=0))
