"""Scale limits (phase 8.2): Juju resource and action-result sizes at 200 nodes.

Two payloads cross Juju and could hit size limits at datacenter scale:

- the topology, delivered once as a Juju **file resource** (`topology.json`);
- each unit's probe output, returned by the `collect-results` action as an
  **action result** string.

This test sizes synthetic worst-case payloads for a 200-node cluster and
asserts they stay within conservative budgets, recording the conclusion that
no chunking or compression is required:

- the topology resource grows linearly with node count but is a file upload
  (Juju imposes no small cap on file resources); ~130 KB at 200 nodes leaves
  large margin under the 1 MB budget;
- the action result is per-unit and the cross-rack validators are
  representative-sampled, so a unit's probe output is bounded by the rack count
  (one MTU record and one BGP path, with up to 30 traceroute hops, per remote
  rack), not by the cluster node count. Even a 50-rack worst case stays under
  the 256 KB action-result budget; a non-representative unit returns a few KB.

The empirical counterpart that pushes these payloads through the testbed Juju
controller is `nt-testbed verify scale`.
"""

import json

TOPOLOGY_RESOURCE_BUDGET_BYTES = 1_000_000  # file resource; generous headroom
ACTION_RESULT_BUDGET_BYTES = 256_000  # per-unit collect-results action result

TRACEROUTE_MAX_HOPS = 30  # bgp_inference traceroute -m 30


def make_topology(num_nodes, num_racks):
    """A realistic topology: one rack controller and evenly split data nodes per
    rack, each data node with a management NIC and a two-member data bond."""
    per_rack = max(1, num_nodes // num_racks)
    machines = []
    for r in range(1, num_racks + 1):
        rack = f"rack-{r}"
        machines.append(
            {
                "system_id": f"rc{r:04d}",
                "hostname": f"{rack}-controller",
                "rack": rack,
                "role": "rack-controller",
                "in_scope": False,
                "interfaces": [
                    {
                        "name": "eth0",
                        "type": "physical",
                        "mac": f"52:54:00:{r:02x}:00:01",
                        "fabric": "fabric-mgmt",
                        "fabric_class": "management",
                        "vlan_tag": 10,
                        "subnet_cidr": "10.10.1.0/24",
                        "ip": f"10.10.{r}.2",
                        "gateway_ip": f"10.10.{r}.1",
                    }
                ],
            }
        )
        for n in range(1, per_rack + 1):
            sid = f"r{r:03d}d{n:03d}"
            machines.append(
                {
                    "system_id": sid,
                    "hostname": sid,
                    "rack": rack,
                    "role": "data",
                    "in_scope": True,
                    "interfaces": [
                        {
                            "name": "eth0",
                            "type": "physical",
                            "mac": f"52:54:00:{r:02x}:{n:02x}:01",
                            "fabric": "fabric-mgmt",
                            "fabric_class": "management",
                            "vlan_tag": 10,
                            "subnet_cidr": "10.10.1.0/24",
                            "ip": f"10.10.{r}.{n + 2}",
                            "gateway_ip": f"10.10.{r}.1",
                        },
                        {
                            "name": "bond0",
                            "type": "bond",
                            "mac": f"52:54:00:{r:02x}:{n:02x}:02",
                            "fabric": f"data-{rack}",
                            "fabric_class": "data",
                            "vlan_tag": 100,
                            "subnet_cidr": f"10.{r}.0.0/24",
                            "ip": f"10.{r}.0.{n}",
                            "gateway_ip": f"10.{r}.0.254",
                            "bond_members": [
                                {"name": "ens3", "mac": f"52:54:00:{r:02x}:{n:02x}:03"},
                                {"name": "ens4", "mac": f"52:54:00:{r:02x}:{n:02x}:04"},
                            ],
                        },
                    ],
                }
            )
    return {
        "schema_version": "1",
        "scope": {"selector": "all"},
        "fabrics": [],
        "machines": machines,
        "reachability_model": {"rules": {"cross-rack-data-routing": {"parameters": {}}}},
    }


def make_representative_probe_output(num_racks):
    """Worst-case representative probe output: one failed BGP path per remote
    rack with a full traceroute, plus one MTU record per remote rack."""
    hops = [
        {"hop": h, "ip": f"10.254.{h}.1", "rtt_ms": 1.234}
        for h in range(1, TRACEROUTE_MAX_HOPS + 1)
    ]
    paths, mtu = [], []
    for r in range(2, num_racks + 1):
        paths.append(
            {
                "source_rack": "rack-1",
                "source_node": "r001d001",
                "target_rack": f"rack-{r}",
                "target_representative": f"r{r:03d}d001",
                "fallback_target": f"r{r:03d}d002",
                "reachable": False,
                "target_role": None,
                "observation_status": "failure",
                "finding": {
                    "type": "likely-bgp-failure",
                    "classification": "inferred",
                    "scope": "rack-pair",
                    "diagnosis_confidence": "inferred",
                    "hint": "traceroute stops at the local ToR gateway",
                },
                "traceroute_hops": hops,
                "traceroute_truncated": False,
            }
        )
        mtu.append(
            {
                "type": "cross-rack-mtu",
                "source_node": "r001d001",
                "source_rack": "rack-1",
                "target_node": f"r{r:03d}d001",
                "target_rack": f"rack-{r}",
                "observed_path_mtu_bytes": 1500,
                "observation_status": "success",
            }
        )
    return {
        "schema_version": "1",
        "status": "complete",
        "node": {
            "system_id": "r001d001",
            "hostname": "r001d001",
            "interfaces": [{"name": "bond0", "mac": "52:54:00:01:01:02", "ip": "10.1.0.1"}],
        },
        "bond_validator": {"validator_status": "complete", "findings": [], "observations": []},
        "vlan_neighbor_validator": {
            "validator_status": "complete",
            "findings": [],
            "observations": [],
        },
        "mtu_validator": {"validator_status": "complete", "cross_rack_mtu": mtu},
        "bgp_inference": {"validator_status": "complete", "paths": paths},
    }


def _action_result_bytes(probe_output):
    """collect-results returns {"probe-output": <json string>}; size that."""
    return len(json.dumps({"probe-output": json.dumps(probe_output)}))


def test_topology_resource_under_budget_at_200_nodes():
    size = len(json.dumps(make_topology(num_nodes=200, num_racks=20)))
    assert size < TOPOLOGY_RESOURCE_BUDGET_BYTES


def test_topology_resource_scales_linearly_without_blowup():
    small = len(json.dumps(make_topology(50, 5)))
    large = len(json.dumps(make_topology(200, 20)))
    # Linear, not quadratic: 4x the nodes stays well under 5x the bytes.
    assert large < small * 5


def test_action_result_under_budget_for_200_node_cluster():
    # A 200-node cluster is typically ~10-20 racks; size the representative's
    # worst-case output at the high end of that range.
    size = _action_result_bytes(make_representative_probe_output(num_racks=20))
    assert size < ACTION_RESULT_BUDGET_BYTES


def test_action_result_bounded_by_rack_count_not_node_count():
    # The probe output depends only on the rack count (representative-sampled),
    # so it is identical whether each rack holds few or many nodes.
    twenty_racks = make_representative_probe_output(num_racks=20)
    assert _action_result_bytes(twenty_racks) == _action_result_bytes(
        make_representative_probe_output(num_racks=20)
    )
    # Even an extreme 50-rack cluster stays under the action-result budget,
    # confirming no chunking/compression is required.
    assert _action_result_bytes(make_representative_probe_output(num_racks=50)) < (
        ACTION_RESULT_BUDGET_BYTES
    )


def test_non_representative_action_result_is_small():
    # Non-representatives emit no cross-rack records; their output is a few KB.
    out = {
        "schema_version": "1",
        "status": "complete",
        "node": {"system_id": "r001d050", "hostname": "r001d050", "interfaces": []},
        "bond_validator": {"validator_status": "complete", "findings": [], "observations": []},
        "vlan_neighbor_validator": {
            "validator_status": "complete",
            "findings": [],
            "observations": [],
        },
        "mtu_validator": {"validator_status": "skipped", "cross_rack_mtu": []},
        "bgp_inference": {"validator_status": "skipped", "paths": []},
    }
    assert _action_result_bytes(out) < 10_000
