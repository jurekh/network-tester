"""Probe runner: validator sequencing, timeout enforcement, output writing.

Runs bond_validator and vlan_neighbor_validator concurrently, then
mtu_validator and bgp_inference sequentially; enforces the overall probe
timeout with cooperative cancellation (design D17); writes probe-output.json
conforming to the shared probe-output schema (see schemas.py).
"""

import json
import signal
import threading
import time
from pathlib import Path

import bgp_inference
import bond_validator
import mtu_validator
import schemas
import vlan_neighbor_validator

OUTPUT_PATH = Path("/var/log/network-tester/probe-output.json")

# Grace period for interrupted validators to flush partial records; the
# SIGTERM requirement is that output is written within 5 seconds.
FLUSH_GRACE_SECONDS = 5.0

# findings is common to all sections; the second key is the validator's
# structured record list (see schemas.validate_probe_output).
SECTION_LISTS = {
    "bond_validator": ("findings", "bonds"),
    "vlan_neighbor_validator": ("findings", "observations"),
    "mtu_validator": ("findings", "cross_rack_mtu"),
    "bgp_inference": ("findings", "paths"),
}

VALIDATOR_FUNCS = {
    "bond_validator": bond_validator.run,
    "vlan_neighbor_validator": vlan_neighbor_validator.run,
    "mtu_validator": mtu_validator.run,
    "bgp_inference": bgp_inference.run,
}

# Phase 1 validators use different protocols (LACP capture vs ARP/ICMP) and
# run concurrently; the phase 2 validators both generate ICMP traffic and
# run one after the other to avoid intermediate-device rate limiting.
PHASES = (
    ("bond_validator", "vlan_neighbor_validator"),
    ("mtu_validator",),
    ("bgp_inference",),
)


class Cancellation:
    """Shared cancellation flag plus registry of validator child subprocesses.

    Validators must register every subprocess they spawn (tcpdump, arping,
    ping, traceroute, ...) before waiting on it and must check is_set()
    between per-peer or per-rack probe iterations. Python threads are never
    forcibly killed; cancellation works by terminating registered
    subprocesses and letting validator code return cooperatively.
    """

    def __init__(self):
        self._event = threading.Event()
        self._lock = threading.Lock()
        self._processes = []
        self.reason = None  # "timeout" or "cancelled" once cancel() runs

    def is_set(self):
        return self._event.is_set()

    def wait(self, timeout):
        return self._event.wait(timeout)

    def register(self, process):
        with self._lock:
            self._processes.append(process)
            cancelled = self._event.is_set()
        if cancelled:
            # Lost the race with cancel(): kill it now rather than leaking it.
            self._terminate(process)

    def cancel(self, reason):
        with self._lock:
            if self.reason is None:
                self.reason = reason
        self._event.set()
        self.terminate_processes()

    def terminate_processes(self):
        with self._lock:
            processes = list(self._processes)
        for process in processes:
            self._terminate(process)

    @staticmethod
    def _terminate(process):
        if process.poll() is None:
            try:
                process.terminate()
            except OSError:
                pass


def empty_section(name):
    section = {"validator_status": "not_started"}
    for key in SECTION_LISTS[name]:
        section[key] = []
    return section


class _ValidatorRun:
    """One validator executing in its own thread, mutating its section."""

    def __init__(self, name, func, topology, node, cancellation):
        self.name = name
        self.section = empty_section(name)
        self.error = None
        self.started = False
        self._thread = threading.Thread(
            target=self._invoke,
            args=(func, topology, node, cancellation),
            name=f"validator-{name}",
            daemon=True,
        )

    def start(self):
        self.started = True
        self._thread.start()

    def _invoke(self, func, topology, node, cancellation):
        try:
            func(topology, node, self.section, cancellation)
        except Exception as exc:  # noqa: BLE001 - re-raised by run_probe
            self.error = exc

    def alive(self):
        return self._thread.is_alive()

    def join(self, timeout):
        self._thread.join(timeout)


def _wait_for_phase(phase_runs, deadline, cancellation, clock):
    while True:
        alive = [run for run in phase_runs if run.alive()]
        if not alive or cancellation.is_set():
            return
        if clock() >= deadline:
            cancellation.cancel("timeout")
            return
        alive[0].join(min(0.1, max(deadline - clock(), 0.01)))


def _flush_interrupted(runs, clock):
    """Give interrupted validators a short window to flush partial records."""
    deadline = clock() + FLUSH_GRACE_SECONDS
    for run in runs:
        if run.started and run.alive():
            run.join(max(deadline - clock(), 0))


def run_probe(
    topology,
    node,
    timeout_seconds,
    funcs=None,
    output_path=None,
    clock=time.monotonic,
    run_id="",
):
    """Run all validators against the bound node record; write probe output.

    Returns the top-level probe status (complete, timeout, or cancelled).
    ``run_id`` is stamped into the output so the collector can reject stale
    documents. ``funcs`` and ``output_path`` are injectable for tests.
    """
    funcs = VALIDATOR_FUNCS if funcs is None else funcs
    output_path = OUTPUT_PATH if output_path is None else output_path
    cancellation = Cancellation()
    deadline = clock() + timeout_seconds
    runs = {
        name: _ValidatorRun(name, funcs[name], topology, node, cancellation)
        for phase in PHASES
        for name in phase
    }

    previous_handler = signal.getsignal(signal.SIGTERM)
    signal.signal(signal.SIGTERM, lambda signum, frame: cancellation.cancel("cancelled"))
    try:
        for phase in PHASES:
            if cancellation.is_set():
                break
            phase_runs = [runs[name] for name in phase]
            for run in phase_runs:
                run.start()
            _wait_for_phase(phase_runs, deadline, cancellation, clock)
        if cancellation.is_set():
            cancellation.terminate_processes()
            _flush_interrupted(runs.values(), clock)
    finally:
        signal.signal(signal.SIGTERM, previous_handler)

    # A validator crash without cancellation is a probe bug: fail loudly
    # instead of writing output that silently lacks its findings.
    if cancellation.reason is None:
        for run in runs.values():
            if run.error is not None:
                raise run.error

    status = cancellation.reason or "complete"
    for run in runs.values():
        if not run.started:
            run.section["validator_status"] = "not_started"
        elif run.alive() or run.section["validator_status"] == "not_started":
            # Interrupted before the validator set its own terminal status.
            run.section["validator_status"] = status
    write_output(
        output_path, status, node, {name: run.section for name, run in runs.items()}, run_id
    )
    return status


def write_output(path, status, node, sections, run_id=""):
    doc = {
        "schema_version": schemas.SCHEMA_VERSION,
        "probe_run_id": run_id,
        "status": status,
        "node": {
            "system_id": node["system_id"],
            "hostname": node["hostname"],
            "interfaces": node["interfaces"],
        },
    }
    doc.update(sections)
    schemas.ensure_valid(doc, schemas.validate_probe_output, "probe output")
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(doc, indent=2) + "\n")
    return doc
