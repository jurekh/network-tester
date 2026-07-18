"""Probe payload: argument handling, identity, sequencing, timeout, SIGTERM."""

import json
import os
import signal
import stat
import subprocess
import threading
import time

import probe
import probe_runner
import pytest
import schemas
from conftest import FIXTURES, PING_OK, load_fixture

TOPOLOGY = FIXTURES / "topology_mixed_scope.json"

# Tools the real validators spawn; mocks must be terminated on cancellation.
PROBE_TOOLS = ("tcpdump", "arping", "ping", "traceroute")


def complete_stub(topology, node, section, cancellation):
    section["validator_status"] = "complete"


def make_stuck_tool(tmp_path, name):
    """A mock probe tool that hangs until terminated."""
    path = tmp_path / name
    path.write_text("#!/bin/sh\nexec sleep 60\n")
    path.chmod(path.stat().st_mode | stat.S_IXUSR)
    return path


def make_stuck_validator(tmp_path, tools, derived_record=None, list_key="observations"):
    """A validator that spawns stuck tool mocks and waits on them.

    Mimics the real-validator contract: registers subprocesses with the
    cancellation registry, optionally flushes a derived-but-not-attempted
    record into its section, and returns cooperatively (without setting a
    terminal status) once its subprocesses die.
    """
    procs = []

    def stuck(topology, node, section, cancellation):
        if derived_record is not None:
            section[list_key].append(derived_record)
        for tool in tools:
            proc = subprocess.Popen([str(make_stuck_tool(tmp_path, tool))])
            procs.append(proc)
            cancellation.register(proc)
        for proc in procs:
            proc.wait()

    return stuck, procs


def run_args(funcs, tmp_path, timeout=60):
    topology = load_fixture(TOPOLOGY)
    node = next(m for m in topology["machines"] if m["system_id"] == "aaa001")
    output = tmp_path / "log" / "probe-output.json"
    status = probe_runner.run_probe(topology, node, timeout, funcs=funcs, output_path=output)
    return status, json.loads(output.read_text())


# --- entry point arguments and identity (4.1, 4.2) ------------------------------


def test_missing_arguments_exit_nonzero(capsys):
    assert probe.main([]) != 0
    assert "usage:" in capsys.readouterr().err


def test_non_integer_timeout_exits_nonzero(capsys):
    assert probe.main([str(TOPOLOGY), "soon"]) != 0
    assert "positive integer" in capsys.readouterr().err


def test_missing_topology_file_exits_nonzero(tmp_path, capsys):
    missing = tmp_path / "nope.json"
    assert probe.main([str(missing), "30"]) != 0
    err = capsys.readouterr().err
    assert f"Topology file not found at {missing}" in err
    assert "charm install hook" in err


def test_invalid_topology_file_exits_nonzero(tmp_path, capsys):
    bad = tmp_path / "topology.json"
    bad.write_text('{"schema_version": "1"}')
    assert probe.main([str(bad), "30"]) != 0
    assert "is not valid" in capsys.readouterr().err


def test_no_mac_match_exits_with_observed_macs(monkeypatch, capsys):
    monkeypatch.setattr(probe, "local_macs", lambda: {"de:ad:be:ef:00:01"})
    assert probe.main([str(TOPOLOGY), "30"]) != 0
    err = capsys.readouterr().err
    assert "Node identity not found in topology" in err
    assert "de:ad:be:ef:00:01" in err


def test_find_node_matches_bond_member_mac():
    topology = load_fixture(TOPOLOGY)
    bond = next(m for m in topology["machines"] if m["system_id"] == "aaa001")
    member_mac = bond["interfaces"][0]["bond_members"][0]["mac"]
    assert probe.find_node(topology, {member_mac.upper().lower()}) is bond


def test_local_macs_parses_ip_link_output(monkeypatch):
    sample = (
        "1: lo: <LOOPBACK,UP> mtu 65536 ...\\    link/loopback 00:00:00:00:00:00 brd ...\n"
        "2: eth0: <BROADCAST,UP> mtu 1500 ...\\    link/ether 52:54:00:AA:01:01 brd ...\n"
    )
    monkeypatch.setattr(
        probe.subprocess,
        "run",
        lambda *a, **k: subprocess.CompletedProcess(a, 0, stdout=sample, stderr=""),
    )
    assert "52:54:00:aa:01:01" in probe.local_macs()


# --- full run with stub validators (4.3, 4.9, 4.10) -----------------------------


def test_full_probe_run_writes_schema_valid_output(monkeypatch, tmp_path, capsys, fake_tools):
    output = tmp_path / "var" / "log" / "probe-output.json"
    monkeypatch.setattr(probe_runner, "OUTPUT_PATH", output)
    monkeypatch.setattr(probe, "local_macs", lambda: {"52:54:00:01:01:01"})
    # the vlan validator's expected peer (aaa002) answers ARP and ICMP
    fake_tools["arp"]["10.20.1.12"] = True
    fake_tools["ping3"]["10.20.1.12"] = PING_OK

    assert probe.main([str(TOPOLOGY), "30"]) == 0

    doc = json.loads(output.read_text())
    assert schemas.validate_probe_output(doc) == []
    assert doc["schema_version"] == "1"
    assert doc["status"] == "complete"
    assert doc["node"]["system_id"] == "aaa001"
    assert doc["node"]["hostname"] == "r1-data-01"
    assert doc["node"]["interfaces"]
    for section in schemas.VALIDATOR_SECTIONS:
        assert doc[section]["validator_status"] == "complete"
        assert doc[section]["findings"] == []
    # structured cross-rack path record lists exist even when empty
    assert doc["mtu_validator"]["cross_rack_mtu"] == []
    assert doc["bgp_inference"]["paths"] == []
    assert "status complete" in capsys.readouterr().out


def test_phase_one_runs_concurrently_before_phase_two(tmp_path):
    """bond and vlan overlap; mtu starts only after both finish; bgp after mtu."""
    events = []
    lock = threading.Lock()
    barrier = threading.Barrier(2, timeout=10)

    def phased(name, sync=False):
        def run(topology, node, section, cancellation):
            with lock:
                events.append(f"{name}-start")
            if sync:
                barrier.wait()  # both phase-1 validators must be alive at once
            with lock:
                events.append(f"{name}-end")
            section["validator_status"] = "complete"

        return run

    funcs = {
        "bond_validator": phased("bond", sync=True),
        "vlan_neighbor_validator": phased("vlan", sync=True),
        "mtu_validator": phased("mtu"),
        "bgp_inference": phased("bgp"),
    }
    status, doc = run_args(funcs, tmp_path)
    assert status == "complete"
    phase1_end = max(events.index("bond-end"), events.index("vlan-end"))
    assert events.index("mtu-start") > phase1_end
    assert events.index("bgp-start") > events.index("mtu-end")


# --- timeout enforcement (4.6, 4.7) ----------------------------------------------


def test_timeout_terminates_stuck_tools_and_writes_partial_output(tmp_path):
    derived = {"peer": "r1-data-02", "observation_status": "timeout"}
    stuck, procs = make_stuck_validator(tmp_path, PROBE_TOOLS, derived_record=derived)
    funcs = {
        "bond_validator": complete_stub,
        "vlan_neighbor_validator": stuck,
        "mtu_validator": complete_stub,
        "bgp_inference": complete_stub,
    }
    start = time.monotonic()
    status, doc = run_args(funcs, tmp_path, timeout=1)
    elapsed = time.monotonic() - start

    assert status == "timeout"
    assert doc["status"] == "timeout"
    assert elapsed < 10
    assert len(procs) == len(PROBE_TOOLS)
    for proc in procs:
        assert proc.poll() is not None, "stuck probe tool was not terminated"
    assert doc["bond_validator"]["validator_status"] == "complete"
    # interrupted validator keeps its flushed derived-but-not-attempted record
    assert doc["vlan_neighbor_validator"]["validator_status"] == "timeout"
    assert doc["vlan_neighbor_validator"]["observations"] == [derived]
    # never-started validators: not_started with empty lists, no synthesized records
    assert doc["mtu_validator"]["validator_status"] == "not_started"
    assert doc["mtu_validator"]["cross_rack_mtu"] == []
    assert doc["bgp_inference"]["validator_status"] == "not_started"
    assert doc["bgp_inference"]["paths"] == []
    assert schemas.validate_probe_output(doc) == []


def test_register_after_cancel_terminates_immediately(tmp_path):
    cancellation = probe_runner.Cancellation()
    cancellation.cancel("timeout")
    proc = subprocess.Popen([str(make_stuck_tool(tmp_path, "tcpdump"))])
    cancellation.register(proc)
    assert proc.wait(timeout=5) != 0


def test_validator_crash_without_cancellation_raises(tmp_path):
    def broken(topology, node, section, cancellation):
        raise RuntimeError("validator bug")

    funcs = {
        "bond_validator": complete_stub,
        "vlan_neighbor_validator": broken,
        "mtu_validator": complete_stub,
        "bgp_inference": complete_stub,
    }
    with pytest.raises(RuntimeError, match="validator bug"):
        run_args(funcs, tmp_path)


# --- SIGTERM handling (4.8) -------------------------------------------------------


def test_sigterm_flushes_cancelled_output_within_grace(tmp_path):
    stuck, procs = make_stuck_validator(tmp_path, ("tcpdump", "ping"))
    funcs = {
        "bond_validator": stuck,
        "vlan_neighbor_validator": complete_stub,
        "mtu_validator": complete_stub,
        "bgp_inference": complete_stub,
    }
    killer = threading.Timer(0.3, os.kill, (os.getpid(), signal.SIGTERM))
    killer.start()
    try:
        start = time.monotonic()
        status, doc = run_args(funcs, tmp_path, timeout=60)
        elapsed = time.monotonic() - start
    finally:
        killer.cancel()

    assert status == "cancelled"
    assert doc["status"] == "cancelled"
    assert elapsed < 0.3 + probe_runner.FLUSH_GRACE_SECONDS + 2
    for proc in procs:
        assert proc.poll() is not None
    assert doc["bond_validator"]["validator_status"] == "cancelled"
    assert doc["vlan_neighbor_validator"]["validator_status"] == "complete"
    assert doc["mtu_validator"]["validator_status"] == "not_started"
    assert doc["bgp_inference"]["validator_status"] == "not_started"
    assert schemas.validate_probe_output(doc) == []


def test_sigterm_handler_restored_after_run(tmp_path):
    previous = signal.getsignal(signal.SIGTERM)
    funcs = dict.fromkeys(probe_runner.VALIDATOR_FUNCS, complete_stub)
    run_args(funcs, tmp_path)
    assert signal.getsignal(signal.SIGTERM) is previous
