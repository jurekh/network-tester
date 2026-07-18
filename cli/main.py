"""network-tester CLI: argparse entry point with run and status subcommands."""

import argparse
import asyncio
import json
import signal
import sys
from pathlib import Path

from cli import juju_run, maas_topology, report_generator, representatives


def build_parser():
    parser = argparse.ArgumentParser(
        prog="network-tester",
        description="Validate datacenter network cabling against the MAAS topology.",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    run = sub.add_parser("run", help="fetch topology, deploy probes, and report")
    run.add_argument("--all", action="store_true", help="target all machines in Ready state")
    run.add_argument(
        "--rack", nargs="+", metavar="NAME", help="target machines in one or more racks"
    )
    run.add_argument(
        "--nodes",
        nargs="+",
        metavar="ID",
        help="target hand-picked machines (system_id or hostname)",
    )
    run.add_argument(
        "--reuse-model",
        metavar="MODEL",
        help="re-trigger probes on an existing network-test model",
    )
    run.add_argument(
        "--dry-run",
        action="store_true",
        help="show selected nodes and planned checks without deploying",
    )
    run.add_argument(
        "--keep-model", action="store_true", help="do not destroy the Juju model after the report"
    )
    run.add_argument(
        "--mac-manifest",
        metavar="PATH",
        help="MAC-to-port manifest JSON for symmetric swap detection",
    )
    run.add_argument(
        "--charm",
        metavar="PATH",
        help="packed network-tester charm to deploy (e.g. charm/network-tester_amd64.charm)",
    )
    run.add_argument(
        "--cloud",
        metavar="NAME",
        help="Juju cloud to create the test model on (default: controller default)",
    )
    run.add_argument("--maas-url", metavar="URL", help="MAAS API URL, e.g. http://maas:5240/MAAS")
    run.add_argument(
        "--maas-key", metavar="KEY", help="MAAS API key (<consumer>:<token>:<secret>)"
    )
    run.add_argument(
        "--wait-timeout",
        type=int,
        default=600,
        metavar="SECONDS",
        help="how long to wait for units (default: 600)",
    )
    run.add_argument(
        "--probe-timeout",
        type=int,
        metavar="SECONDS",
        help="override the charm probe-timeout for this run (default: charm config, 240)",
    )
    run.add_argument(
        "--verbose",
        action="store_true",
        help="include the full passed-checks list in the JSON report",
    )

    status = sub.add_parser("status", help="show network-test model status")
    status.add_argument("model", nargs="?", metavar="MODEL")
    return parser


def _validate_targeting(args):
    targeting = [bool(args.all), bool(args.rack), bool(args.nodes)]
    if args.reuse_model:
        if any(targeting):
            return "--reuse-model cannot be combined with --all, --rack, or --nodes"
        return None
    if sum(targeting) != 1:
        return (
            "exactly one targeting mode is required: --all, --rack <name>..., "
            "or --nodes <id>... (or --reuse-model <name> to collect from an "
            "existing run)"
        )
    return None


def _scope(args):
    if args.all:
        return "all", []
    if args.rack:
        return "rack", args.rack
    return "nodes", args.nodes


def plan_checks(topology):
    """Derive would-run and would-skip check lists from the topology."""
    machines = topology["machines"]
    in_scope = [m for m in machines if m["in_scope"]]
    would_run = []
    would_skip = []

    for machine in in_scope:
        bonds = [i for i in machine["interfaces"] if i["type"] == "bond"]
        if bonds:
            names = ", ".join(b["name"] for b in bonds)
            would_run.append(f"bond-validator on {machine['hostname']}: LACP capture on {names}")
        else:
            would_skip.append(f"bond-validator on {machine['hostname']}: no bonded interfaces")

    by_segment = {}
    for machine in machines:
        for interface in machine["interfaces"]:
            key = (interface["fabric"], interface["vlan_tag"])
            by_segment.setdefault(key, []).append(machine)

    for machine in in_scope:
        if machine["role"] == "bmc-oam":
            peers = [
                m
                for m in machines
                if m["role"] == "rack-controller" and m["rack"] == machine["rack"]
            ]
        else:
            peers = []
            seen = {machine["system_id"]}
            for interface in machine["interfaces"]:
                for peer in by_segment.get((interface["fabric"], interface["vlan_tag"]), []):
                    if peer["system_id"] not in seen:
                        seen.add(peer["system_id"])
                        peers.append(peer)
        for peer in peers:
            if peer["in_scope"]:
                would_run.append(
                    f"vlan-neighbor-validator on {machine['hostname']}: probe {peer['hostname']}"
                )
            else:
                would_skip.append(
                    f"vlan-neighbor-validator on {machine['hostname']}: "
                    f"peer {peer['hostname']} ({peer['role']}) not selected for probing"
                )

    selections = representatives.select_representatives(topology)
    all_data_racks = sorted({m["rack"] for m in machines if m["role"] == "data" and m["rack"]})
    in_scope_racks = set(selections)
    for rack, entry in sorted(selections.items()):
        for remote, targets in sorted(entry["targets"].items()):
            would_run.append(
                f"mtu-validator {rack} -> {remote}: {entry['source']} probes "
                f"{targets['representative']}"
            )
            fallback = targets["fallback"] or "no fallback"
            would_run.append(
                f"bgp-inference {rack} -> {remote}: {entry['source']} probes "
                f"{targets['representative']} (fallback: {fallback})"
            )
        for remote in all_data_racks:
            if remote != rack and remote not in in_scope_racks:
                would_skip.append(
                    f"cross-rack checks {rack} -> {remote}: no in-scope data nodes in {remote}"
                )
    if len(in_scope_racks) == 1 and len(all_data_racks) <= 1:
        only = next(iter(in_scope_racks))
        would_skip.append(f"cross-rack checks from {only}: no cross-rack data peers")
    return would_run, would_skip


def _print_plan(topology, would_run, would_skip):
    in_scope = [m for m in topology["machines"] if m["in_scope"]]
    print(f"Selected nodes ({len(in_scope)}):")
    for machine in in_scope:
        print(
            f"  {machine['hostname']} ({machine['system_id']}) "
            f"rack={machine['rack']} role={machine['role']}"
        )
    print("Would run:")
    for line in would_run or ["(none)"]:
        print(f"  {line}")
    print("Would skip:")
    for line in would_skip or ["(none)"]:
        print(f"  {line}")


def _install_sigint_handler(model_holder):
    def handler(signum, frame):
        name = model_holder.get("model")
        if name:
            print(
                f"\nInterrupted. Model: {name}; run `juju destroy-model {name}` to release nodes",
                file=sys.stderr,
            )
        else:
            print("\nInterrupted before model creation; nothing to clean up", file=sys.stderr)
        sys.exit(130)

    signal.signal(signal.SIGINT, handler)


def _load_mac_manifest(path):
    """Load the optional MAC-to-port manifest read locally on the workstation."""
    if not path:
        return None
    return json.loads(Path(path).read_text())


def _finish_run(collected, missing, verbose, topology=None, mac_manifest=None):
    report = report_generator.generate_report(
        list(collected.values()),
        missing_nodes=missing,
        verbose=verbose,
        topology=topology,
        mac_manifest=mac_manifest,
    )
    json_path, _text_path = report_generator.save_report(report)
    juju_run.log(f"report written to {json_path}")
    return report


async def _deploy_and_report(facade, args, topology, model_holder):
    model_name, collected, missing = await juju_run.run_new(
        facade,
        topology,
        args.charm,
        args.wait_timeout,
        cloud=args.cloud,
        probe_timeout=args.probe_timeout,
    )
    model_holder["model"] = model_name
    report = _finish_run(
        collected,
        missing,
        args.verbose,
        topology=topology,
        mac_manifest=_load_mac_manifest(args.mac_manifest),
    )
    if args.keep_model:
        print(
            f"Model {model_name} kept for inspection; run "
            f"`juju destroy-model {model_name}` when done"
        )
    else:
        juju_run.log(f"destroying model {model_name}")
        await facade.destroy_model(model_name)
        await juju_run.wait_model_destroyed(facade, model_name)
        juju_run.log(f"model {model_name} destroyed")
        model_holder.pop("model", None)
    return report_generator.exit_code(report)


async def _reuse_and_report(facade, args, model_holder):
    model_holder["model"] = args.reuse_model
    topology, collected, missing, _warnings = await juju_run.run_reuse(
        facade, args.reuse_model, args.wait_timeout, probe_timeout=args.probe_timeout
    )
    report = _finish_run(
        collected,
        missing,
        args.verbose,
        topology=topology,
        mac_manifest=_load_mac_manifest(args.mac_manifest),
    )
    print(
        f"Model {args.reuse_model} kept (reuse mode); run "
        f"`juju destroy-model {args.reuse_model}` when done"
    )
    return report_generator.exit_code(report)


async def _with_facade(coro_func, *args):
    facade = juju_run.LibjujuFacade()
    await facade.connect()
    try:
        return await coro_func(facade, *args)
    finally:
        await facade.disconnect()


def cmd_run(args):
    error = _validate_targeting(args)
    if error:
        print(f"error: {error}", file=sys.stderr)
        return 2
    model_holder = {}
    if args.reuse_model:
        _install_sigint_handler(model_holder)
        try:
            return asyncio.run(_with_facade(_reuse_and_report, args, model_holder))
        except juju_run.JujuError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 1
    if not args.maas_url or not args.maas_key:
        print("error: --maas-url and --maas-key are required", file=sys.stderr)
        return 2

    mode, scope_args = _scope(args)
    try:
        client = maas_topology.MaasClient(args.maas_url, args.maas_key)
        topology = maas_topology.fetch_topology(client, mode, scope_args)
    except maas_topology.MaasError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    failures = maas_topology.preflight(topology)
    if failures:
        print("Pre-flight validation failed:", file=sys.stderr)
        for failure in failures:
            print(
                f"  {failure['hostname']} ({failure['system_id']}): "
                f"{failure['field']}: {failure['description']}",
                file=sys.stderr,
            )
        return 1
    in_scope_count = sum(1 for m in topology["machines"] if m["in_scope"])
    print(f"Pre-flight validation passed for {in_scope_count} nodes")

    would_run, would_skip = plan_checks(topology)
    _print_plan(topology, would_run, would_skip)
    if args.dry_run:
        return 0
    if not args.charm:
        print("error: --charm <path> is required to deploy", file=sys.stderr)
        return 2

    _install_sigint_handler(model_holder)
    try:
        return asyncio.run(_with_facade(_deploy_and_report, args, topology, model_holder))
    except juju_run.JujuError as exc:
        print(f"error: {exc}", file=sys.stderr)
        name = model_holder.get("model")
        if name:
            print(
                f"Model {name} may hold nodes; run `juju destroy-model {name}` to release them",
                file=sys.stderr,
            )
        return 1


async def _status(facade, model_arg):
    return await juju_run.status_lines(facade, model_arg)


def cmd_status(args):
    try:
        code, lines = asyncio.run(_with_facade(_status, args.model))
    except juju_run.JujuError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    for line in lines:
        print(line)
    return code


def main(argv=None):
    args = build_parser().parse_args(argv)
    if args.command == "run":
        return cmd_run(args)
    return cmd_status(args)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
