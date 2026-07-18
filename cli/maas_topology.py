"""MAAS topology fetcher: build the expected-topology model from the MAAS API.

Runs on the operator workstation (never on nodes). Fetches machines and
rack controllers, classifies fabrics and roles, applies the operator's
scope (--all / --rack / --nodes) as in_scope flags while retaining known
peers for classification, and serializes the versioned topology document
defined by cli.schemas.
"""

import requests
from requests_oauthlib import OAuth1

from cli import schemas

API_TIMEOUT_SECONDS = 10

REACHABILITY_MODEL_V1 = {
    "rules": {
        "l2-same-fabric-vlan": {
            "id": "l2-same-fabric-vlan",
            "description": (
                "Machines sharing a fabric name and vlan_tag are expected L2 "
                "peers unless a role rule forbids the adjacency."
            ),
            "applicable_roles": ["rack-controller", "bmc-oam", "data"],
            "parameters": {},
        },
        "bmc-oam-restricted": {
            "id": "bmc-oam-restricted",
            "description": (
                "A bmc-oam node's expected peer set is only the rack-controller "
                "in its rack; other same-VLAN adjacency is forbidden."
            ),
            "applicable_roles": ["bmc-oam"],
            "parameters": {"allowed_peer_roles": ["rack-controller"], "same_rack_only": True},
        },
        "cross-rack-data-routing": {
            "id": "cross-rack-data-routing",
            "description": (
                "Data machines in different racks are expected L3-reachable via "
                "data-fabric interfaces; v1 active checks are "
                "representative-sampled rack-pair probes."
            ),
            "applicable_roles": ["data"],
            "parameters": {
                "probe_scope": "rack-pair-representative",
                "applicable_role": "data",
                "interface_class": "data",
                "source_selection": {
                    "strategy": "lexicographic-lowest",
                    "field": "system_id",
                    "in_scope": True,
                },
                "target_selection": {
                    "strategy": "lexicographic-lowest",
                    "field": "system_id",
                    "in_scope": True,
                },
                "fallback_selection": {
                    "strategy": "next-lexicographic",
                    "field": "system_id",
                    "in_scope": True,
                },
            },
        },
    }
}


class MaasError(Exception):
    """Fatal MAAS API or topology derivation error."""


class MaasClient:
    """Minimal OAuth1 client for the MAAS REST API."""

    def __init__(self, url, key, timeout=API_TIMEOUT_SECONDS):
        self.url = url.rstrip("/")
        try:
            consumer_key, token_key, token_secret = key.split(":")
        except ValueError:
            raise MaasError(
                "MAAS API key must have the form <consumer>:<token>:<secret>"
            ) from None
        self._auth = OAuth1(
            consumer_key, "", token_key, token_secret, signature_method="PLAINTEXT"
        )
        self.timeout = timeout

    def get(self, path):
        url = f"{self.url}/api/2.0/{path}"
        try:
            response = requests.get(url, auth=self._auth, timeout=self.timeout)
        except requests.RequestException as exc:
            raise MaasError(f"MAAS API unreachable at {url}: {exc}") from None
        if response.status_code != 200:
            raise MaasError(
                f"MAAS API error at {url}: HTTP {response.status_code}: {response.text[:200]}"
            )
        return response.json()


def fetch_topology(client, scope_mode, scope_args=()):
    """Fetch machines and rack controllers and build the topology document."""
    rack_controllers = client.get("rackcontrollers/")
    if not rack_controllers:
        raise MaasError(
            "No rack controllers found in MAAS. At least one rack controller "
            "must be registered before running this tool."
        )
    machines = client.get("machines/")
    return build_topology(rack_controllers, machines, scope_mode, scope_args)


# --- derivation helpers -------------------------------------------------------


def _interface_fabric(interface):
    vlan = interface.get("vlan")
    return vlan.get("fabric") if vlan else None


def _management_fabrics(rack_controllers):
    fabrics = set()
    for controller in rack_controllers:
        for interface in controller.get("interface_set", []):
            fabric = _interface_fabric(interface)
            if fabric:
                fabrics.add(fabric)
    return fabrics


def _machine_rack(node, rc_hostnames):
    """Derive the rack name: hostname of the primary rack controller of the
    node's boot-interface VLAN (falling back to any interface, then to a
    single registered rack controller)."""
    interfaces = node.get("interface_set", [])
    boot = node.get("boot_interface") or {}
    ordered = [i for i in interfaces if i.get("id") == boot.get("id")] + interfaces
    for interface in ordered:
        vlan = interface.get("vlan")
        if vlan and vlan.get("primary_rack") in rc_hostnames:
            return rc_hostnames[vlan["primary_rack"]]
    if len(rc_hostnames) == 1:
        return next(iter(rc_hostnames.values()))
    return ""


def _derive_role(node, management_fabrics):
    """data if any data-fabric interface; bmc-oam if management-only; None if
    the machine's fabrics cannot be classified."""
    classes = set()
    for interface in node.get("interface_set", []):
        fabric = _interface_fabric(interface)
        if fabric:
            classes.add("management" if fabric in management_fabrics else "data")
    if "data" in classes:
        return "data"
    if classes == {"management"}:
        return "bmc-oam"
    return None


def _bond_member_names(interface_set):
    members = set()
    for interface in interface_set:
        if interface.get("type") == "bond":
            for parent in interface.get("parents", []):
                members.add(parent)
    return members


def _addressed_links(interface):
    """Links that configure addressing on a subnet. MAAS represents a
    link-less interface with a mode=link_up entry that still references the
    VLAN's subnet; link_up assigns no address, so it does not count."""
    return [
        ln for ln in interface.get("links", []) if ln.get("subnet") and ln.get("mode") != "link_up"
    ]


def _build_interface(interface, interface_set, management_fabrics):
    vlan = interface["vlan"]
    link = next(iter(_addressed_links(interface)), None)
    subnet = link["subnet"] if link else None
    record = {
        "name": interface["name"],
        "type": "bond" if interface.get("type") == "bond" else "physical",
        "mac": interface.get("mac_address") or "",
        "fabric": vlan["fabric"],
        "fabric_class": "management" if vlan["fabric"] in management_fabrics else "data",
        "vlan_tag": vlan.get("vid"),
        "subnet_cidr": subnet["cidr"] if subnet else None,
        "ip": (link.get("ip_address") or None) if link else None,
        "gateway_ip": subnet.get("gateway_ip") if subnet else None,
    }
    if record["type"] == "bond":
        params = interface.get("params") or {}
        if not isinstance(params, dict):
            params = {}
        by_name = {i["name"]: i for i in interface_set}
        record["bond_mode"] = params.get("bond_mode", "")
        record["bond_members"] = [
            {"name": name, "mac": by_name.get(name, {}).get("mac_address") or ""}
            for name in interface.get("parents", [])
        ]
    return record


def _build_machine(node, role, rack, in_scope, management_fabrics):
    interface_set = node.get("interface_set", [])
    members = _bond_member_names(interface_set)
    interfaces = []
    unconfigured = []
    for interface in interface_set:
        if interface.get("type") not in ("physical", "bond"):
            continue
        if interface.get("name") in members:
            continue
        if not interface.get("vlan"):
            unconfigured.append(interface["name"])
            continue
        interfaces.append(_build_interface(interface, interface_set, management_fabrics))
    return {
        "system_id": node["system_id"],
        "hostname": node["hostname"],
        "rack": rack,
        "role": role,
        "in_scope": in_scope,
        "interfaces": interfaces,
        "unconfigured_interfaces": unconfigured,
    }


def _select_in_scope(machines, roles, racks, scope_mode, scope_args, rc_hostnames):
    """Return the set of selected system_ids. Selected machines must be Ready."""
    ready = {m["system_id"] for m in machines if m.get("status_name") == "Ready"}
    if scope_mode == "all":
        return ready
    if scope_mode == "rack":
        available = sorted(rc_hostnames.values())
        for name in scope_args:
            if name not in available:
                raise MaasError(f"Unknown rack: {name}. Available racks: {', '.join(available)}")
        return {sid for sid in ready if racks.get(sid) in scope_args}
    if scope_mode == "nodes":
        by_key = {}
        for m in machines:
            by_key[m["system_id"]] = m
            by_key[m["hostname"]] = m
        selected = set()
        for key in scope_args:
            node = by_key.get(key)
            if node is None:
                raise MaasError(f"Unknown node: {key}")
            if node["system_id"] not in ready:
                raise MaasError(
                    f"Node {key} is not in Ready state (status: {node.get('status_name')})"
                )
            selected.add(node["system_id"])
        return selected
    raise MaasError(f"unknown scope mode: {scope_mode}")


def _retained_peers(machines, roles, in_scope_ids):
    """system_ids of out-of-scope machines retained for classification:
    data machines (cross-rack relevance) and machines sharing a fabric+vlan
    with any in-scope machine."""
    in_scope_segments = set()
    for m in machines:
        if m["system_id"] not in in_scope_ids:
            continue
        for interface in m.get("interface_set", []):
            vlan = interface.get("vlan")
            if vlan:
                in_scope_segments.add((vlan.get("fabric"), vlan.get("vid")))
    retained = set()
    for m in machines:
        sid = m["system_id"]
        if sid in in_scope_ids:
            continue
        if roles.get(sid) == "data":
            retained.add(sid)
            continue
        for interface in m.get("interface_set", []):
            vlan = interface.get("vlan")
            if vlan and (vlan.get("fabric"), vlan.get("vid")) in in_scope_segments:
                retained.add(sid)
                break
    return retained


def build_topology(rack_controllers, machines, scope_mode, scope_args=()):
    """Build and validate the topology document from raw MAAS API payloads."""
    management_fabrics = _management_fabrics(rack_controllers)
    rc_hostnames = {c["system_id"]: c["hostname"] for c in rack_controllers}
    rc_ids = set(rc_hostnames)
    machines = [m for m in machines if m["system_id"] not in rc_ids]

    roles = {m["system_id"]: _derive_role(m, management_fabrics) for m in machines}
    racks = {m["system_id"]: _machine_rack(m, rc_hostnames) for m in machines}
    in_scope_ids = _select_in_scope(machines, roles, racks, scope_mode, scope_args, rc_hostnames)
    retained = _retained_peers(machines, roles, in_scope_ids)

    records = []
    for controller in rack_controllers:
        records.append(
            _build_machine(
                controller,
                "rack-controller",
                controller["hostname"],
                False,
                management_fabrics,
            )
        )
    for m in machines:
        sid = m["system_id"]
        if sid not in in_scope_ids and sid not in retained:
            continue
        records.append(
            _build_machine(
                m,
                roles[sid] or "data",
                racks[sid],
                sid in in_scope_ids,
                management_fabrics,
            )
        )
        # preserve the unclassifiable-role marker for pre-flight
        if roles[sid] is None:
            records[-1]["role_unclassified"] = True
    records.sort(key=lambda r: r["system_id"])

    fabric_names = set()
    for record in records:
        for interface in record["interfaces"]:
            fabric_names.add((interface["fabric"], interface["fabric_class"]))
    fabrics = [{"name": name, "class": cls} for name, cls in sorted(fabric_names)]

    topology = {
        "schema_version": schemas.SCHEMA_VERSION,
        "scope": {"mode": scope_mode, "args": list(scope_args)},
        "fabrics": fabrics,
        "machines": records,
        "reachability_model": REACHABILITY_MODEL_V1,
    }
    schemas.ensure_valid(topology, schemas.validate_topology, "topology")
    return topology


def preflight(topology):
    """Validate in-scope machines have complete MAAS network configuration.

    Returns a structured list of failures: dicts with system_id, hostname,
    field, and description. The CLI owns presentation and control flow.
    """
    failures = []

    def fail(machine, field, description):
        failures.append(
            {
                "system_id": machine["system_id"],
                "hostname": machine["hostname"],
                "field": field,
                "description": description,
            }
        )

    for machine in topology["machines"]:
        if not machine["in_scope"]:
            continue
        if machine.get("role_unclassified"):
            fail(
                machine,
                "role",
                "interfaces cannot be classified into management or data fabrics",
            )
        if not machine["rack"]:
            fail(machine, "rack", "cannot determine rack controller assignment")
        for name in machine.get("unconfigured_interfaces", []):
            fail(machine, "vlan", f"interface {name} has no VLAN assignment")
        if not machine["interfaces"] and not machine.get("unconfigured_interfaces"):
            fail(machine, "interfaces", "no network interfaces recorded in MAAS")
        for interface in machine["interfaces"]:
            if not interface.get("subnet_cidr"):
                fail(
                    machine,
                    "subnet",
                    f"interface {interface['name']} has no addressed subnet link",
                )
            if interface["type"] == "bond" and not interface.get("bond_mode"):
                fail(
                    machine,
                    "bond_mode",
                    f"bond {interface['name']} has no bond mode configured",
                )
    return failures
