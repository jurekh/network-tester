"""Topology fetcher: role derivation, scoping, pre-flight, serialization."""

import pytest
from maas_fakes import (
    FakeClient,
    data_machine,
    iface,
    link,
    machine,
    mgmt_machine,
    mixed_machine,
    rack_controller,
    vlan,
)

from cli import maas_topology, schemas
from cli.maas_topology import MaasError, build_topology, fetch_topology, preflight


def fetch(machines, rack_controllers=None, mode="all", args=()):
    rcs = rack_controllers if rack_controllers is not None else [rack_controller()]
    return fetch_topology(FakeClient(rcs, machines), mode, args)


def by_hostname(topology):
    return {m["hostname"]: m for m in topology["machines"]}


# --- role derivation and fabric identification (3.2, 3.11, 3.12) -------------


def test_rack_controller_role_assigned_without_inference():
    topology = fetch([mgmt_machine("aaa001", "bmc-01")])
    rc = by_hostname(topology)["rack1-ctl"]
    assert rc["role"] == "rack-controller"
    assert rc["in_scope"] is False


def test_management_only_machine_is_bmc_oam():
    topology = fetch([mgmt_machine("aaa001", "bmc-01")])
    assert by_hostname(topology)["bmc-01"]["role"] == "bmc-oam"


def test_data_only_machine_is_data():
    topology = fetch([data_machine("aaa001", "data-01")])
    assert by_hostname(topology)["data-01"]["role"] == "data"


def test_mixed_interface_machine_is_data_with_data_fabric_interfaces():
    topology = fetch([mixed_machine("aaa001", "mixed-01")])
    record = by_hostname(topology)["mixed-01"]
    assert record["role"] == "data"
    classes = {i["name"]: i["fabric_class"] for i in record["interfaces"]}
    assert classes == {"eth-mgmt": "management", "eth-data": "data"}
    data_ifaces = [i for i in record["interfaces"] if i["fabric_class"] == "data"]
    assert data_ifaces[0]["subnet_cidr"] == "10.20.1.0/24"


def test_fabric_classification_from_rack_controller_membership():
    topology = fetch([data_machine("aaa001", "data-01"), mgmt_machine("aaa002", "bmc-01")])
    fabrics = {f["name"]: f["class"] for f in topology["fabrics"]}
    assert fabrics["fabric-mgmt"] == "management"
    assert fabrics["fabric-data"] == "data"


def test_no_rack_controllers_is_fatal():
    with pytest.raises(MaasError, match="No rack controllers found"):
        fetch([mgmt_machine("aaa001", "bmc-01")], rack_controllers=[])


def test_rack_derived_from_vlan_primary_rack():
    topology = fetch([data_machine("aaa001", "data-01")])
    assert by_hostname(topology)["data-01"]["rack"] == "rack1-ctl"


# --- interface details (3.3) ---------------------------------------------------


def test_bond_interface_records_mode_and_members():
    topology = fetch([data_machine("aaa001", "data-01")])
    bond = by_hostname(topology)["data-01"]["interfaces"][0]
    assert bond["type"] == "bond"
    assert bond["bond_mode"] == "802.3ad"
    assert [m["name"] for m in bond["bond_members"]] == ["eno1", "eno2"]
    assert all(m["mac"] for m in bond["bond_members"])
    assert bond["fabric"] == "fabric-data"
    assert bond["vlan_tag"] == 100
    assert bond["subnet_cidr"] == "10.20.1.0/24"
    assert bond["gateway_ip"] == "10.20.1.1"


def test_bond_members_are_not_separate_interfaces():
    topology = fetch([data_machine("aaa001", "data-01")])
    names = [i["name"] for i in by_hostname(topology)["data-01"]["interfaces"]]
    assert names == ["bond0"]


def test_tagged_vlan_interface_recorded_with_tag_and_fabric():
    # A tagged 802.1q interface (e.g. a controller's management-VLAN presence
    # over a trunk) must be a recognized peer with its own fabric and vlan_tag,
    # not dropped; otherwise nodes on that VLAN flag its MAC as unexpected.
    rc = rack_controller(
        ifaces=[
            iface(
                "br0",
                "52:54:00:00:00:01",
                vlan("fabric-mgmt", 0),
                links=[link("10.10.1.0/24")],
            ),
            iface(
                "br0.30",
                "52:54:00:00:00:30",
                vlan("fabric-mgmt", 30),
                type="vlan",
                links=[link("10.10.4.0/24", "10.10.4.1")],
                parents=["br0"],
            ),
        ]
    )
    topology = fetch([data_machine("aaa001", "data-01")], rack_controllers=[rc])
    names = {i["name"]: i for i in by_hostname(topology)["rack1-ctl"]["interfaces"]}
    assert "br0.30" in names
    assert names["br0.30"]["vlan_tag"] == 30
    assert names["br0.30"]["fabric"] == "fabric-mgmt"
    assert names["br0.30"]["ip"] == "10.10.4.1"


# --- anchors and scoping (3.1, 3.4) -------------------------------------------


def test_rack_controller_anchor_retained_regardless_of_state():
    rc = rack_controller()
    assert rc["status_name"] != "Ready"
    topology = fetch([mgmt_machine("aaa001", "bmc-01")], rack_controllers=[rc])
    assert "rack1-ctl" in by_hostname(topology)


def test_all_scope_selects_only_ready_machines():
    topology = fetch(
        [
            data_machine("aaa001", "data-01"),
            data_machine("aaa002", "data-02", status="Commissioning"),
        ]
    )
    records = by_hostname(topology)
    assert records["data-01"]["in_scope"] is True
    # not Ready, but retained as a known data peer for classification
    assert records["data-02"]["in_scope"] is False


def test_nodes_scope_selects_by_system_id_or_hostname():
    topology = fetch(
        [data_machine("aaa001", "data-01"), data_machine("aaa002", "data-02")],
        mode="nodes",
        args=["aaa001", "data-02"],
    )
    records = by_hostname(topology)
    assert records["data-01"]["in_scope"] is True
    assert records["data-02"]["in_scope"] is True


def test_nodes_scope_unknown_node_is_fatal():
    with pytest.raises(MaasError, match="Unknown node: nope"):
        fetch([data_machine("aaa001", "data-01")], mode="nodes", args=["nope"])


def test_nodes_scope_not_ready_is_fatal():
    with pytest.raises(MaasError, match="not in Ready state"):
        fetch(
            [data_machine("aaa001", "data-01", status="Deployed")],
            mode="nodes",
            args=["data-01"],
        )


def test_rack_scope_unknown_rack_lists_available():
    with pytest.raises(MaasError, match="Unknown rack: rack-x. Available racks: rack1-ctl"):
        fetch([data_machine("aaa001", "data-01")], mode="rack", args=["rack-x"])


def test_rack_scope_is_case_sensitive():
    with pytest.raises(MaasError, match="Unknown rack"):
        fetch([data_machine("aaa001", "data-01")], mode="rack", args=["Rack1-Ctl"])


def test_out_of_scope_same_segment_peer_retained():
    topology = fetch(
        [mgmt_machine("aaa001", "bmc-01"), mgmt_machine("aaa002", "bmc-02")],
        mode="nodes",
        args=["bmc-01"],
    )
    records = by_hostname(topology)
    assert records["bmc-01"]["in_scope"] is True
    assert records["bmc-02"]["in_scope"] is False


def test_out_of_scope_data_machine_retained_for_cross_rack():
    rc2 = rack_controller("rcsys2", "rack2-ctl")
    remote = data_machine("bbb001", "r2-data-01", vid=200, cidr="10.20.2.0/24")
    for interface in remote["interface_set"]:
        if interface["vlan"]:
            interface["vlan"]["primary_rack"] = "rcsys2"
    topology = fetch(
        [data_machine("aaa001", "data-01"), remote],
        rack_controllers=[rack_controller(), rc2],
        mode="rack",
        args=["rack1-ctl"],
    )
    records = by_hostname(topology)
    assert records["data-01"]["in_scope"] is True
    assert records["r2-data-01"]["in_scope"] is False
    assert records["r2-data-01"]["rack"] == "rack2-ctl"


# --- pre-flight validation (3.5, 3.13) ----------------------------------------


def test_preflight_passes_with_complete_config():
    topology = fetch([data_machine("aaa001", "data-01"), mgmt_machine("aaa002", "bmc-01")])
    assert preflight(topology) == []


def test_preflight_flags_interface_without_vlan():
    broken = machine(
        "aaa001",
        "data-01",
        [
            iface(
                "eth0", "52:54:00:01:01:01", vlan("fabric-data", 100), links=[link("10.20.1.0/24")]
            ),
            iface("eth1", "52:54:00:01:01:02", None),
        ],
    )
    failures = preflight(fetch([broken]))
    assert [(f["hostname"], f["field"]) for f in failures] == [("data-01", "vlan")]
    assert "eth1" in failures[0]["description"]


def test_preflight_flags_missing_subnet_link():
    broken = machine(
        "aaa001",
        "data-01",
        [iface("eth0", "52:54:00:01:01:01", vlan("fabric-data", 100), links=[])],
    )
    failures = preflight(fetch([broken]))
    assert [(f["field"], f["system_id"]) for f in failures] == [("subnet", "aaa001")]


def test_preflight_flags_link_up_only_interface():
    """MAAS represents an unlinked interface as a mode=link_up link that still
    references the subnet; that is not addressing configuration."""
    broken = machine(
        "aaa001",
        "data-01",
        [
            iface(
                "eth0",
                "52:54:00:01:01:01",
                vlan("fabric-data", 100),
                links=[link("10.20.1.0/24", mode="link_up")],
            )
        ],
    )
    failures = preflight(fetch([broken]))
    assert [(f["field"], f["system_id"]) for f in failures] == [("subnet", "aaa001")]


def test_preflight_flags_bond_without_mode():
    broken = data_machine("aaa001", "data-01")
    bond = broken["interface_set"][0]
    bond["params"] = {}
    failures = preflight(fetch([broken]))
    assert [f["field"] for f in failures] == ["bond_mode"]


def test_preflight_ignores_out_of_scope_machines():
    broken = machine(
        "aaa002",
        "bmc-02",
        [iface("eth0", "52:54:00:01:01:02", vlan("fabric-mgmt", 0), links=[])],
    )
    topology = fetch([mgmt_machine("aaa001", "bmc-01"), broken], mode="nodes", args=["bmc-01"])
    assert by_hostname(topology)["bmc-02"]["in_scope"] is False
    assert preflight(topology) == []


# --- serialization (3.6, 3.14) -------------------------------------------------


def test_serialized_topology_has_required_top_level_keys():
    topology = fetch([data_machine("aaa001", "data-01")])
    assert set(topology) >= {
        "schema_version",
        "scope",
        "fabrics",
        "machines",
        "reachability_model",
    }
    assert topology["schema_version"] == "1"


def test_serialized_topology_validates_against_schema():
    topology = fetch(
        [
            data_machine("aaa001", "data-01"),
            mgmt_machine("aaa002", "bmc-01"),
            mixed_machine("aaa003", "mixed-01"),
        ],
    )
    assert schemas.validate_topology(topology) == []


def test_serialized_topology_mixed_scope_and_retained_peers():
    topology = fetch(
        [data_machine("aaa001", "data-01"), data_machine("aaa002", "data-02")],
        mode="nodes",
        args=["data-01"],
    )
    flags = {m["hostname"]: m["in_scope"] for m in topology["machines"]}
    assert flags == {"rack1-ctl": False, "data-01": True, "data-02": False}
    assert topology["scope"] == {"mode": "nodes", "args": ["data-01"]}
    rules = topology["reachability_model"]["rules"]
    assert set(rules) == {"l2-same-fabric-vlan", "bmc-oam-restricted", "cross-rack-data-routing"}


# --- API error handling --------------------------------------------------------


def test_unreachable_api_raises_maas_error_with_url():
    client = maas_topology.MaasClient("http://192.0.2.1:5240/MAAS", "a:b:c", timeout=0.1)
    with pytest.raises(MaasError, match="MAAS API unreachable at http://192.0.2.1"):
        client.get("machines/")


def test_malformed_api_key_is_fatal():
    with pytest.raises(MaasError, match="consumer.*token.*secret"):
        maas_topology.MaasClient("http://example/MAAS", "not-a-key")


def test_build_topology_excludes_rc_from_machine_selection():
    """A rack controller also present in machines/ must not be double-counted."""
    rc = rack_controller()
    rc_as_machine = dict(rc)
    topology = build_topology([rc], [rc_as_machine, mgmt_machine("aaa001", "bmc-01")], "all", ())
    roles = [m["role"] for m in topology["machines"] if m["hostname"] == "rack1-ctl"]
    assert roles == ["rack-controller"]
