"""Juju orchestration: model lifecycle, explicit placement, trigger, collection.

The orchestration functions take a facade object so tests can substitute a
fake; LibjujuFacade is the python-libjuju implementation. All deployment uses
explicit per-machine placement by MAAS hostname (design D15): Juju's
automatic MAAS allocator must never substitute arbitrary ready machines.
"""

import asyncio
import json
import tempfile
import time
from pathlib import Path

MODEL_PREFIX = "network-test-"
APP_NAME = "network-tester"
TOPOLOGY_ON_NODE = "/var/lib/network-tester/topology.json"


class JujuError(Exception):
    """Deployment or Juju API failure with an operator-readable message."""


def timestamp():
    return time.strftime("%Y%m%d-%H%M%S")


def log(message):
    print(f"[{time.strftime('%H:%M:%S')}] {message}", flush=True)


def in_scope_machines(topology):
    return [m for m in topology["machines"] if m["in_scope"]]


def racks_in_order(machines):
    racks = {}
    for machine in machines:
        racks.setdefault(machine["rack"], []).append(machine)
    return sorted(racks.items())


def missing_entry(machine, reason, detail=""):
    entry = {
        "system_id": machine.get("system_id", "unknown"),
        "hostname": machine.get("hostname", "unknown"),
        "reason": reason,
    }
    if detail:
        entry["detail"] = detail
    return entry


async def ensure_no_existing_models(facade):
    existing = sorted(m for m in await facade.list_models() if m.startswith(MODEL_PREFIX))
    if existing:
        raise JujuError(
            "Existing test model(s) found: "
            + ", ".join(existing)
            + ". Destroy them or use --reuse-model <name>."
        )


async def wait_model_destroyed(facade, name, timeout=600, poll=5):
    """Block until the controller no longer lists the model.

    libjuju's destroy_models only initiates destruction; nodes are not
    released back to MAAS until the model is actually gone.
    """
    deadline = time.monotonic() + timeout
    while name in await facade.list_models():
        if time.monotonic() >= deadline:
            raise JujuError(
                f"model {name} still exists after {timeout}s; "
                f"run `juju destroy-model {name}` to release nodes"
            )
        await asyncio.sleep(poll)


async def wait_for_units(facade, model_name, units, deadline, predicate, poll=10):
    """Poll unit statuses until predicate holds for all units or deadline passes.

    Returns (satisfied, pending) unit-name lists.
    """
    units = set(units)
    while True:
        statuses = await facade.unit_statuses(model_name)
        pending = [u for u in units if not predicate(statuses.get(u, {}))]
        if not pending:
            return sorted(units), []
        if time.monotonic() >= deadline:
            return sorted(units - set(pending)), sorted(pending)
        await asyncio.sleep(poll)


def _active_idle(status):
    return status.get("workload") == "active" and status.get("agent") == "idle"


def _probe_complete(run_id):
    def predicate(status):
        return _active_idle(status) and status.get("message") == f"probe {run_id} complete"

    return predicate


async def _trigger_and_collect(facade, model_name, unit_machines, wait_deadline, missing, poll):
    """Set a fresh probe-run-id, wait for completion, collect results.

    unit_machines: {unit_name: machine record} for units eligible to probe.
    Returns {unit_name: probe output document}; appends to missing.
    """
    run_id = timestamp()
    log(f"triggering probe run {run_id} on {len(unit_machines)} unit(s)")
    await facade.set_config(model_name, {"probe-run-id": run_id})

    done, stragglers = await wait_for_units(
        facade, model_name, unit_machines, wait_deadline, _probe_complete(run_id), poll=poll
    )
    for unit in stragglers:
        missing.append(
            missing_entry(
                unit_machines[unit],
                "probe-timeout",
                f"unit {unit} did not return to active/idle after probe trigger",
            )
        )

    log(f"collecting results from {len(done)} unit(s)")
    results = await asyncio.gather(
        *(facade.run_action(model_name, unit, "collect-results") for unit in done)
    )
    collected = {}
    raw_dir = Path(tempfile.mkdtemp(prefix="network-tester-results-"))
    for unit, result in zip(done, results):
        raw_path = raw_dir / (unit.replace("/", "-") + ".json")
        raw_path.write_text(json.dumps(result, indent=2) + "\n")
        if "probe-output" not in result:
            missing.append(
                missing_entry(
                    unit_machines[unit],
                    "no-probe-output",
                    f"collect-results on {unit} returned no probe output",
                )
            )
            continue
        collected[unit] = json.loads(result["probe-output"])
    log(f"raw per-unit results saved under {raw_dir}")
    return collected


async def run_new(facade, topology, charm_path, wait_timeout, cloud=None, poll=10):
    """Deploy, trigger, and collect a new probe run.

    Returns (model_name, collected outputs by unit, missing-node entries).
    """
    await ensure_no_existing_models(facade)
    model_name = MODEL_PREFIX + timestamp()
    log(f"creating model {model_name}")
    await facade.add_model(model_name, cloud)
    deadline = time.monotonic() + wait_timeout
    missing = []

    with tempfile.NamedTemporaryFile(
        "w", prefix="network-tester-topology-", suffix=".json"
    ) as topology_file:
        json.dump(topology, topology_file)
        topology_file.flush()
        log(f"deploying {APP_NAME} (charm {charm_path})")
        await facade.deploy(model_name, charm_path, {"topology": topology_file.name})

        # Placement uses `add-unit --to <maas-hostname>`: the unit's machine
        # is provisioned with the charm's base, and the MAAS allocator can
        # never substitute a different ready machine (design D15).
        unit_machines = {}  # unit name -> topology machine record
        for rack, rack_machines in racks_in_order(in_scope_machines(topology)):
            log(f"deploying rack {rack} ({len(rack_machines)} machine(s))")
            for machine in rack_machines:
                try:
                    unit = await facade.add_unit(model_name, machine["hostname"])
                except JujuError as exc:
                    log(f"placement failed for {machine['hostname']}: {exc}")
                    missing.append(missing_entry(machine, "placement-failed", str(exc)))
                    continue
                unit_machines[unit] = machine
            log(f"rack {rack} deployment issued")

        log(f"waiting for {len(unit_machines)} unit(s) to reach active/idle")
        ready, not_ready = await wait_for_units(
            facade, model_name, unit_machines, deadline, _active_idle, poll=poll
        )
        for unit in not_ready:
            missing.append(
                missing_entry(
                    unit_machines[unit],
                    "deployment-timeout",
                    f"unit {unit} did not reach active/idle before the probe trigger",
                )
            )
        log("all expected units ready" if not not_ready else f"{len(not_ready)} unit(s) not ready")

        collected = await _trigger_and_collect(
            facade,
            model_name,
            {u: unit_machines[u] for u in ready},
            time.monotonic() + wait_timeout,
            missing,
            poll,
        )
    return model_name, collected, missing


async def run_reuse(facade, model_name, wait_timeout, poll=10):
    """Re-trigger probing on an existing model and collect results.

    Returns (topology, collected outputs by unit, missing entries, warnings).
    """
    models = await facade.list_models()
    if model_name not in models:
        raise JujuError(f"Model {model_name} not found")
    statuses = await facade.unit_statuses(model_name)
    topology = None
    for unit in sorted(statuses):
        try:
            topology = json.loads(await facade.cat_file(model_name, unit, TOPOLOGY_ON_NODE))
            break
        except (JujuError, ValueError):
            continue
    if topology is None:
        raise JujuError(
            f"Cannot reuse model {model_name}: topology resource is unavailable; "
            "run a new deployment or provide a valid network-test model"
        )
    machines_by_hostname = {m["hostname"]: m for m in topology["machines"]}

    def machine_for(unit):
        hostname = statuses[unit].get("hostname")
        return machines_by_hostname.get(hostname, {"hostname": hostname or unit})

    missing = []
    warnings = []
    restartable = {u: machine_for(u) for u, s in statuses.items() if _active_idle(s)}
    for unit in sorted(set(statuses) - set(restartable)):
        status = statuses[unit]
        warnings.append(
            f"unit {unit} is {status.get('workload')}/{status.get('agent')} "
            "and cannot restart probing; recording as missing"
        )
        missing.append(
            missing_entry(machine_for(unit), "no-probe-output", f"unit {unit} not restartable")
        )
    for line in warnings:
        log(f"warning: {line}")

    collected = await _trigger_and_collect(
        facade, model_name, restartable, time.monotonic() + wait_timeout, missing, poll
    )
    return topology, collected, missing, warnings


async def status_lines(facade, model_arg=None):
    """Resolve the target model and return (exit code, lines to print)."""
    models = sorted(m for m in await facade.list_models() if m.startswith(MODEL_PREFIX))
    if model_arg:
        target = model_arg
    elif not models:
        return 0, ["No network-test models found."]
    elif len(models) == 1:
        target = models[0]
    else:
        return 2, [*models, "Specify a model name: `network-tester status <model-name>`"]

    statuses = await facade.unit_statuses(target)
    lines = [f"Model {target}: {len(statuses)} unit(s)"]
    for unit, status in sorted(statuses.items()):
        message = status.get("message") or ""
        if message.startswith("probe ") and message.endswith(" complete"):
            probe_state = "probe complete"
        elif message.startswith("running probe"):
            probe_state = "probe running"
        else:
            probe_state = "no probe run"
        lines.append(
            f"  {unit}: {status.get('workload')}/{status.get('agent')} [{probe_state}] {message}"
        )
    return 0, lines


class LibjujuFacade:
    """python-libjuju implementation of the orchestration facade."""

    def __init__(self):
        from juju.controller import Controller

        self._controller = Controller()
        self._models = {}

    async def connect(self):
        await self._controller.connect()

    async def disconnect(self):
        for model in self._models.values():
            await model.disconnect()
        self._models = {}
        await self._controller.disconnect()

    async def _model(self, name):
        if name not in self._models:
            self._models[name] = await self._controller.get_model(name)
        return self._models[name]

    async def list_models(self):
        return await self._controller.list_models()

    async def add_model(self, name, cloud=None):
        self._models[name] = await self._controller.add_model(name, cloud_name=cloud)

    async def destroy_model(self, name):
        model = self._models.pop(name, None)
        if model is not None:
            await model.disconnect()
        await self._controller.destroy_models(name)

    async def deploy(self, name, charm_path, resources):
        model = await self._model(name)
        # the path must be absolute or libjuju treats it as a Charmhub URL
        await model.deploy(
            str(Path(charm_path).resolve()),
            application_name=APP_NAME,
            num_units=0,
            resources=resources,
        )

    async def add_unit(self, name, placement):
        model = await self._model(name)
        # MAAS hostname placement needs the model-UUID scope; libjuju parses
        # a bare string as a placement scope and the controller rejects it
        to = f"{model.uuid}:{placement}"
        try:
            units = await model.applications[APP_NAME].add_unit(count=1, to=to)
        except Exception as exc:
            raise JujuError(f"cannot place a unit on {placement}: {exc}") from exc
        return units[0].name

    async def set_config(self, name, values):
        model = await self._model(name)
        await model.applications[APP_NAME].set_config(
            {key: str(value) for key, value in values.items()}
        )

    async def unit_statuses(self, name):
        model = await self._model(name)
        status = await model.get_status()
        application = (status.applications or {}).get(APP_NAME)
        if application is None or not application.units:
            return {}
        machines = status.machines or {}
        result = {}
        for unit_name, unit in application.units.items():
            machine_id = unit.machine
            machine = machines.get(machine_id)
            result[unit_name] = {
                "workload": unit.workload_status.status,
                "message": unit.workload_status.info or "",
                "agent": unit.agent_status.status,
                "machine": machine_id,
                "hostname": getattr(machine, "hostname", None),
            }
        return result

    async def run_action(self, name, unit_name, action):
        model = await self._model(name)
        action_obj = await model.units[unit_name].run_action(action)
        await action_obj.wait()
        return dict(action_obj.results or {})

    async def cat_file(self, name, unit_name, path):
        model = await self._model(name)
        action = await model.units[unit_name].run(f"cat {path}")
        await action.wait()
        results = action.results or {}
        code = str(results.get("return-code", 1))
        if code != "0":
            raise JujuError(f"cannot read {path} on {unit_name}: {results}")
        return results.get("stdout", "")
