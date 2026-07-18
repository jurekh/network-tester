"""CLI argument handling, dry-run output, and pre-flight control flow."""

import pytest
from maas_fakes import FakeClient, data_machine, mgmt_machine, rack_controller

from cli import main as cli_main


def run_cli(argv, monkeypatch, machines=None, rack_controllers=None):
    rcs = rack_controllers if rack_controllers is not None else [rack_controller()]
    fake = FakeClient(rcs, machines or [])
    monkeypatch.setattr(cli_main.maas_topology, "MaasClient", lambda url, key: fake)
    return cli_main.main(argv)


MAAS_ARGS = ["--maas-url", "http://maas:5240/MAAS", "--maas-key", "a:b:c"]


# --- targeting flag validation (3.8) -------------------------------------------


def test_run_requires_exactly_one_targeting_mode(capsys, monkeypatch):
    assert run_cli(["run", *MAAS_ARGS], monkeypatch) == 2
    assert "exactly one targeting mode" in capsys.readouterr().err


def test_run_rejects_multiple_targeting_modes(capsys, monkeypatch):
    code = run_cli(["run", "--all", "--nodes", "n1", *MAAS_ARGS], monkeypatch)
    assert code == 2
    assert "exactly one targeting mode" in capsys.readouterr().err


def test_reuse_model_conflicts_with_targeting(capsys, monkeypatch):
    code = run_cli(["run", "--reuse-model", "network-test-1", "--all", *MAAS_ARGS], monkeypatch)
    assert code == 2
    assert "--reuse-model cannot be combined" in capsys.readouterr().err


def test_run_requires_maas_credentials(capsys, monkeypatch):
    assert run_cli(["run", "--all"], monkeypatch) == 2
    assert "--maas-url and --maas-key are required" in capsys.readouterr().err


def test_run_accepts_all_documented_flags(monkeypatch, tmp_path):
    parser = cli_main.build_parser()
    args = parser.parse_args(
        [
            "run",
            "--all",
            "--dry-run",
            "--keep-model",
            "--mac-manifest",
            str(tmp_path / "m.json"),
            "--maas-url",
            "http://maas:5240/MAAS",
            "--maas-key",
            "a:b:c",
            "--wait-timeout",
            "120",
            "--verbose",
            "--charm",
            "network-tester.charm",
            "--cloud",
            "maas-testbed",
        ]
    )
    assert args.wait_timeout == 120
    assert args.verbose is True
    assert args.charm == "network-tester.charm"
    assert args.cloud == "maas-testbed"


# --- dry-run behavior (3.9) ----------------------------------------------------


def test_dry_run_prints_nodes_roles_and_check_plan(capsys, monkeypatch):
    code = run_cli(
        ["run", "--dry-run", "--all", *MAAS_ARGS],
        monkeypatch,
        machines=[data_machine("aaa001", "data-01"), mgmt_machine("aaa002", "bmc-01")],
    )
    out = capsys.readouterr().out
    assert code == 0
    assert "Pre-flight validation passed for 2 nodes" in out
    assert "data-01 (aaa001) rack=rack1-ctl role=data" in out
    assert "bmc-01 (aaa002) rack=rack1-ctl role=bmc-oam" in out
    assert "Would run:" in out
    assert "bond-validator on data-01: LACP capture on bond0" in out
    assert "Would skip:" in out
    # bmc-oam expects only the (never in scope) rack controller: a skip
    assert "vlan-neighbor-validator on bmc-01: peer rack1-ctl" in out
    # single data rack: cross-rack checks are skipped
    assert "no cross-rack data peers" in out


def test_dry_run_skips_out_of_scope_peer(capsys, monkeypatch):
    code = run_cli(
        ["run", "--dry-run", "--nodes", "data-01", *MAAS_ARGS],
        monkeypatch,
        machines=[data_machine("aaa001", "data-01"), data_machine("aaa002", "data-02")],
    )
    out = capsys.readouterr().out
    assert code == 0
    assert "Selected nodes (1):" in out
    assert "vlan-neighbor-validator on data-01: peer data-02 (data) not selected" in out


def test_dry_run_would_run_peer_probe_when_in_scope(capsys, monkeypatch):
    code = run_cli(
        ["run", "--dry-run", "--all", *MAAS_ARGS],
        monkeypatch,
        machines=[data_machine("aaa001", "data-01"), data_machine("aaa002", "data-02")],
    )
    out = capsys.readouterr().out
    assert code == 0
    assert "vlan-neighbor-validator on data-01: probe data-02" in out
    assert "vlan-neighbor-validator on data-02: probe data-01" in out


# --- pre-flight control flow (3.10) ---------------------------------------------


def test_run_preflight_failure_exits_nonzero_naming_machine(capsys, monkeypatch):
    broken = data_machine("aaa001", "data-01")
    broken["interface_set"][0]["links"] = []
    code = run_cli(["run", "--all", *MAAS_ARGS], monkeypatch, machines=[broken])
    captured = capsys.readouterr()
    assert code == 1
    assert "Pre-flight validation failed" in captured.err
    assert "data-01 (aaa001): subnet" in captured.err


def test_run_without_charm_stops_before_deployment(capsys, monkeypatch):
    code = run_cli(
        ["run", "--all", *MAAS_ARGS],
        monkeypatch,
        machines=[data_machine("aaa001", "data-01")],
    )
    captured = capsys.readouterr()
    assert code == 2
    assert "--charm <path> is required to deploy" in captured.err


def test_unknown_rack_error_propagates(capsys, monkeypatch):
    code = run_cli(
        ["run", "--rack", "nope", *MAAS_ARGS],
        monkeypatch,
        machines=[data_machine("aaa001", "data-01")],
    )
    captured = capsys.readouterr()
    assert code == 1
    assert "Unknown rack: nope. Available racks: rack1-ctl" in captured.err


# --- deployment wiring (4.22, 4.26, 4.27) ----------------------------------------


class RecordingFacade:
    """Facade standing in for LibjujuFacade in CLI-level tests."""

    instances = []

    def __init__(self):
        self.destroyed = []
        self.connected = False
        self.list_models_calls = 0
        RecordingFacade.instances.append(self)

    async def connect(self):
        self.connected = True

    async def disconnect(self):
        self.connected = False

    async def destroy_model(self, name):
        self.destroyed.append(name)

    async def list_models(self):
        # destruction has completed by the time the CLI confirms it
        self.list_models_calls += 1
        return []


def deploy_cli(argv, monkeypatch, tmp_path, keep=False):
    """Run the CLI with fake MAAS and a stubbed juju_run.run_new."""
    RecordingFacade.instances = []
    monkeypatch.setattr(cli_main.juju_run, "LibjujuFacade", RecordingFacade)

    async def fake_run_new(
        facade, topology, charm_path, wait_timeout, cloud=None, poll=10, probe_timeout=None
    ):
        fake_run_new.calls.append((charm_path, wait_timeout, cloud, probe_timeout))
        return "network-test-fake", {}, []

    fake_run_new.calls = []
    monkeypatch.setattr(cli_main.juju_run, "run_new", fake_run_new)
    monkeypatch.chdir(tmp_path)
    code = run_cli(argv, monkeypatch, machines=[data_machine("aaa001", "data-01")])
    return code, fake_run_new.calls


def test_run_deploys_reports_and_destroys_model(capsys, monkeypatch, tmp_path):
    code, calls = deploy_cli(
        ["run", "--all", "--charm", "nt.charm", "--cloud", "maas-x", *MAAS_ARGS],
        monkeypatch,
        tmp_path,
    )
    assert code == 0
    assert calls == [("nt.charm", 600, "maas-x", None)]
    facade = RecordingFacade.instances[0]
    assert facade.destroyed == ["network-test-fake"]
    assert facade.list_models_calls > 0  # waited for destruction to complete
    assert facade.connected is False
    assert list(tmp_path.glob("network-test-*.json"))
    assert list(tmp_path.glob("network-test-*.txt"))
    assert "All 0 checks passed." in capsys.readouterr().out


def test_run_threads_probe_timeout_to_run_new(monkeypatch, tmp_path):
    code, calls = deploy_cli(
        ["run", "--all", "--charm", "nt.charm", "--probe-timeout", "30", *MAAS_ARGS],
        monkeypatch,
        tmp_path,
    )
    assert code == 0
    assert calls[0][3] == 30


def test_run_keep_model_skips_destroy(capsys, monkeypatch, tmp_path):
    code, _calls = deploy_cli(
        ["run", "--all", "--keep-model", "--charm", "nt.charm", *MAAS_ARGS],
        monkeypatch,
        tmp_path,
    )
    assert code == 0
    facade = RecordingFacade.instances[0]
    assert facade.destroyed == []
    out = capsys.readouterr().out
    assert "Model network-test-fake kept" in out
    assert "juju destroy-model network-test-fake" in out


# --- subcommand surface (3.7) ----------------------------------------------------


def test_status_subcommand_exists():
    args = cli_main.build_parser().parse_args(["status"])
    assert args.command == "status"
    assert args.model is None


def test_command_is_required():
    with pytest.raises(SystemExit):
        cli_main.build_parser().parse_args([])
