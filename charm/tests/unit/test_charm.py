# Copyright 2026 jerzy.husakowski@canonical.com
# See LICENSE file for licensing details.

import json
import sys
from unittest.mock import patch

import pytest
from ops import testing

from charm import PROBE_PACKAGES, NetworkTesterCharm

NODE_MAC = "52:54:00:01:01:01"
TOPOLOGY = {
    "machines": [
        {
            "system_id": "aaa001",
            "hostname": "r1-data-01",
            "interfaces": [{"name": "eth0", "mac": NODE_MAC}],
        }
    ]
}
RUN_ID = "20260612-103000"
CONFIG = {"probe-run-id": "", "probe-timeout": 240}


@pytest.fixture
def paths(tmp_path, monkeypatch):
    """Redirect the charm's on-node state paths into tmp_path."""
    state_dir = tmp_path / "var" / "lib" / "network-tester"
    monkeypatch.setattr("charm.STATE_DIR", state_dir)
    monkeypatch.setattr("charm.TOPOLOGY_PATH", state_dir / "topology.json")
    monkeypatch.setattr("charm.LAST_RUN_PATH", state_dir / "last-probe-run-id")
    return state_dir


@pytest.fixture
def topology_resource(tmp_path):
    path = tmp_path / "topology-resource.json"
    path.write_text(json.dumps(TOPOLOGY))
    return testing.Resource(name="topology", path=path)


def make_context():
    return testing.Context(NetworkTesterCharm)


# --- install: packages, topology resource, identity (4.11, 4.12, 4.16) ----------


def test_install_installs_packages_and_stores_topology(paths, topology_resource, monkeypatch):
    monkeypatch.setattr("charm._local_macs", lambda: {NODE_MAC})
    ctx = make_context()
    with patch("charm.subprocess.run") as run:
        ctx.run(
            ctx.on.install(),
            testing.State(config=CONFIG, resources={topology_resource}),
        )
    cmd = run.call_args[0][0]
    assert cmd[:3] == ["apt", "install", "-y"]
    assert set(PROBE_PACKAGES) <= set(cmd)
    stored = json.loads((paths / "topology.json").read_text())
    assert stored == TOPOLOGY


def test_install_without_topology_resource_blocks_and_fails(paths, monkeypatch):
    monkeypatch.setattr("charm._local_macs", lambda: {NODE_MAC})
    ctx = make_context()
    with patch("charm.subprocess.run"):
        # the charm sets BlockedStatus with the same message before raising;
        # scenario keeps the status current (not in history) when the hook fails
        with pytest.raises(Exception, match="topology resource is required but not attached"):
            ctx.run(ctx.on.install(), testing.State(config=CONFIG))


def test_install_with_unmatched_macs_blocks_listing_observed(
    paths, topology_resource, monkeypatch
):
    monkeypatch.setattr("charm._local_macs", lambda: {"de:ad:be:ef:00:01"})
    ctx = make_context()
    with patch("charm.subprocess.run"):
        with pytest.raises(Exception, match="Node identity not found in topology") as excinfo:
            ctx.run(
                ctx.on.install(),
                testing.State(config=CONFIG, resources={topology_resource}),
            )
    # the raised message is the same string the charm set as BlockedStatus
    assert "de:ad:be:ef:00:01" in str(excinfo.value)
    assert "do not match any machine in the topology model" in str(excinfo.value)


def test_install_status_progression(paths, topology_resource, monkeypatch):
    monkeypatch.setattr("charm._local_macs", lambda: {NODE_MAC})
    ctx = make_context()
    with patch("charm.subprocess.run"):
        state_out = ctx.run(
            ctx.on.install(),
            testing.State(config=CONFIG, resources={topology_resource}),
        )
    maintenance = [
        s.message for s in ctx.unit_status_history if isinstance(s, testing.MaintenanceStatus)
    ]
    assert "installing probe packages" in maintenance
    # the most recent status set during install stays current until start
    assert state_out.unit_status == testing.MaintenanceStatus("loading topology resource")


def test_start_sets_active_ready():
    ctx = make_context()
    state_out = ctx.run(ctx.on.start(), testing.State(config=CONFIG))
    assert state_out.unit_status == testing.ActiveStatus("ready")


# --- probe-run-id propagation and payload invocation (4.13, 4.14, 4.17) ---------


def assert_payload_invoked(run_mock, expected_timeout="240"):
    cmd = run_mock.call_args[0][0]
    assert cmd[0] == sys.executable
    assert cmd[1].endswith("payload/probe.py")
    assert cmd[2].endswith("topology.json")
    assert cmd[3] == expected_timeout


def test_leader_config_changed_propagates_run_id_and_probes(paths, monkeypatch):
    relation = testing.PeerRelation("network-tester-peers")
    ctx = make_context()
    with patch("charm.subprocess.run") as run:
        state_out = ctx.run(
            ctx.on.config_changed(),
            testing.State(
                leader=True,
                config={"probe-run-id": RUN_ID, "probe-timeout": 240},
                relations={relation},
            ),
        )
    out_relation = state_out.get_relation(relation.id)
    assert out_relation.local_app_data["probe-run-id"] == RUN_ID
    assert_payload_invoked(run)
    assert (paths / "last-probe-run-id").read_text() == RUN_ID
    assert state_out.unit_status == testing.ActiveStatus(f"probe {RUN_ID} complete")
    maintenance = [
        s.message for s in ctx.unit_status_history if isinstance(s, testing.MaintenanceStatus)
    ]
    assert f"running probe {RUN_ID}" in maintenance


def test_leader_config_changed_with_empty_run_id_does_nothing(paths):
    relation = testing.PeerRelation("network-tester-peers")
    ctx = make_context()
    with patch("charm.subprocess.run") as run:
        state_out = ctx.run(
            ctx.on.config_changed(),
            testing.State(leader=True, config=CONFIG, relations={relation}),
        )
    assert "probe-run-id" not in state_out.get_relation(relation.id).local_app_data
    run.assert_not_called()


def test_non_leader_config_changed_does_not_write_or_probe(paths):
    relation = testing.PeerRelation("network-tester-peers")
    ctx = make_context()
    with patch("charm.subprocess.run") as run:
        state_out = ctx.run(
            ctx.on.config_changed(),
            testing.State(
                leader=False,
                config={"probe-run-id": RUN_ID, "probe-timeout": 240},
                relations={relation},
            ),
        )
    assert "probe-run-id" not in state_out.get_relation(relation.id).local_app_data
    run.assert_not_called()


def test_peer_relation_changed_runs_payload_for_new_run_id(paths):
    relation = testing.PeerRelation(
        "network-tester-peers", local_app_data={"probe-run-id": RUN_ID}, peers_data={1: {}}
    )
    ctx = make_context()
    with patch("charm.subprocess.run") as run:
        state_out = ctx.run(
            ctx.on.relation_changed(relation, remote_unit=1),
            testing.State(config=CONFIG, relations={relation}),
        )
    assert_payload_invoked(run)
    assert (paths / "last-probe-run-id").read_text() == RUN_ID
    assert state_out.unit_status == testing.ActiveStatus(f"probe {RUN_ID} complete")


def test_peer_relation_changed_respects_probe_timeout_config(paths):
    relation = testing.PeerRelation(
        "network-tester-peers", local_app_data={"probe-run-id": RUN_ID}, peers_data={1: {}}
    )
    ctx = make_context()
    with patch("charm.subprocess.run") as run:
        ctx.run(
            ctx.on.relation_changed(relation, remote_unit=1),
            testing.State(
                config={"probe-run-id": "", "probe-timeout": 120}, relations={relation}
            ),
        )
    assert_payload_invoked(run, expected_timeout="120")


def test_peer_relation_changed_skips_already_executed_run_id(paths):
    paths.mkdir(parents=True)
    (paths / "last-probe-run-id").write_text(RUN_ID)
    relation = testing.PeerRelation(
        "network-tester-peers", local_app_data={"probe-run-id": RUN_ID}, peers_data={1: {}}
    )
    ctx = make_context()
    with patch("charm.subprocess.run") as run:
        ctx.run(
            ctx.on.relation_changed(relation, remote_unit=1),
            testing.State(config=CONFIG, relations={relation}),
        )
    run.assert_not_called()


def test_peer_relation_changed_without_run_id_does_nothing(paths):
    relation = testing.PeerRelation("network-tester-peers", peers_data={1: {}})
    ctx = make_context()
    with patch("charm.subprocess.run") as run:
        ctx.run(
            ctx.on.relation_changed(relation, remote_unit=1),
            testing.State(config=CONFIG, relations={relation}),
        )
    run.assert_not_called()
    assert not (paths / "last-probe-run-id").exists()


# --- collect-results action (4.15) -----------------------------------------------


def test_collect_results_missing(tmp_path, monkeypatch):
    monkeypatch.setattr("charm.PROBE_OUTPUT_PATH", tmp_path / "probe-output.json")
    ctx = make_context()
    ctx.run(ctx.on.action("collect-results"), testing.State(config=CONFIG))
    assert ctx.action_results is not None
    assert ctx.action_results["status"] == "missing"
    assert ctx.action_results["unit"].startswith("network-tester/")


def test_collect_results_returns_probe_output(tmp_path, monkeypatch):
    output = tmp_path / "probe-output.json"
    output.write_text('{"schema_version": "1"}')
    monkeypatch.setattr("charm.PROBE_OUTPUT_PATH", output)
    ctx = make_context()
    ctx.run(ctx.on.action("collect-results"), testing.State(config=CONFIG))
    assert ctx.action_results is not None
    assert ctx.action_results["probe-output"] == '{"schema_version": "1"}'


if __name__ == "__main__":  # pragma: nocover
    pytest.main([__file__])
