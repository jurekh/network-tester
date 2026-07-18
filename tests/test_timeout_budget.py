"""Timeout budget (phase 8.1): worst-case validator wall-clock vs probe-timeout.

Derives per-phase wall-clock bounds from the validators' own timing constants
and asserts the hardening contract:

- phase 1 (concurrent LACP + ARP capture) is bounded by a fixed window and is
  independent of node count;
- a source-rack representative's second phase scales with the number of remote
  racks, not the number of nodes (representative-sampled probing);
- scenarios whose budget fits under the 240s default probe-timeout complete,
  while larger or failure-heavy rack counts overrun and must either be reported
  as timeout/inconclusive by the runner or be given an explicit higher
  probe-timeout (which, past the Juju hook timeout minus the flush margin, also
  requires raising the hook timeout) rather than silently overrunning;
- the cross-rack validators check the shared cancellation event between remote
  rack iterations and flush the unattempted racks.

The budget is computed from the real module constants so it tracks the code: a
change that lengthens a probe cap is caught here.
"""

import bgp_inference
import bond_validator
import mtu_validator
import probe_runner
import representatives
import vlan_neighbor_validator
import yaml
from conftest import REPO_ROOT, load_fixture

# Operating envelope (design D14). The probe-timeout default lives in the charm
# config; test_default_probe_timeout_matches_charm_config keeps this in sync.
DEFAULT_PROBE_TIMEOUT = 240
JUJU_HOOK_TIMEOUT = 300  # Juju default hook timeout; a hard kill with no flush
FLUSH_MARGIN = 5  # payload partial-result flush window before the hook timeout

CROSS_RACK_RULE = load_fixture(REPO_ROOT / "tests" / "fixtures" / "topology_two_rack.json")[
    "reachability_model"
]["rules"]["cross-rack-data-routing"]


# --- budget model derived from the validators' constants --------------------------


def _ping_seconds(count, wait):
    """Upper-bound wall-clock for `ping -c count -W wait`: each packet may wait
    the full deadline."""
    return count * wait


def phase1_budget():
    """Concurrent phase-1 capture: the longer of the LACP and ARP windows."""
    return max(
        bond_validator.CAPTURE_WINDOW_SECONDS,
        vlan_neighbor_validator.CAPTURE_CAP_SECONDS,
        vlan_neighbor_validator.CAPTURE_WINDOW_SECONDS,
    )


def mtu_seconds_per_rack():
    """Two initial DF probes plus the binary-search refinements, each -W bound."""
    probes = 2 + mtu_validator.BINARY_SEARCH_ITERATIONS
    return probes * _ping_seconds(1, mtu_validator.PROBE_WAIT_SECONDS)


def bgp_seconds_per_rack():
    """Representative + fallback ICMP, then the capped traceroute on dual fail."""
    icmp = 2 * _ping_seconds(bgp_inference.ICMP_COUNT, bgp_inference.ICMP_WAIT_SECONDS)
    return icmp + bgp_inference.TRACEROUTE_CAP_SECONDS


def representative_budget(remote_racks):
    return phase1_budget() + remote_racks * (mtu_seconds_per_rack() + bgp_seconds_per_rack())


def non_representative_budget():
    return phase1_budget()


def max_remote_racks_under(timeout):
    """Largest remote-rack count whose representative budget fits in `timeout`."""
    remotes = 0
    while representative_budget(remotes + 1) <= timeout:
        remotes += 1
    return remotes


# --- synthetic topology -----------------------------------------------------------


def make_topology(num_racks, data_nodes_per_rack):
    """A topology of `num_racks` racks, each with `data_nodes_per_rack` in-scope
    data nodes on a data fabric, plus the cross-rack-data-routing rule."""
    machines = []
    for r in range(1, num_racks + 1):
        rack = f"rack-{r}"
        for n in range(1, data_nodes_per_rack + 1):
            sid = f"r{r}d{n:02d}"
            machines.append(
                {
                    "system_id": sid,
                    "hostname": sid,
                    "rack": rack,
                    "role": "data",
                    "in_scope": True,
                    "interfaces": [
                        {
                            "name": "bond0",
                            "type": "bond",
                            "mac": f"52:54:00:{r:02x}:{n:02x}:01",
                            "fabric": f"data-{rack}",
                            "fabric_class": "data",
                            "vlan_tag": 100,
                            "subnet_cidr": f"10.{r}.0.0/24",
                            "ip": f"10.{r}.0.{n}",
                            "gateway_ip": f"10.{r}.0.254",
                            "bond_members": [],
                        }
                    ],
                }
            )
    return {
        "schema_version": "1",
        "scope": {"selector": "all"},
        "fabrics": [],
        "machines": machines,
        "reachability_model": {"rules": {"cross-rack-data-routing": CROSS_RACK_RULE}},
    }


def _rep_node(topology, rack="rack-1"):
    params = representatives.cross_rack_rule(topology)
    sid = representatives.source_representative(topology["machines"], rack, params)
    return next(m for m in topology["machines"] if m["system_id"] == sid)


# --- phase-1 bound is node-count independent --------------------------------------


def test_phase1_budget_is_fixed_and_node_count_independent():
    # The concurrent capture window does not grow with the number of nodes.
    assert non_representative_budget() == phase1_budget()
    assert phase1_budget() == bond_validator.CAPTURE_WINDOW_SECONDS  # the longer window
    assert non_representative_budget() < DEFAULT_PROBE_TIMEOUT


def test_non_representative_skips_second_phase(monkeypatch):
    topology = make_topology(num_racks=3, data_nodes_per_rack=3)
    non_rep = topology["machines"][1]  # r1d02, not the lexicographically lowest
    calls = []
    monkeypatch.setattr(mtu_validator, "_ping_df", lambda *a, **k: calls.append(a) or (True, None))
    monkeypatch.setattr(bgp_inference, "_icmp_ok", lambda *a, **k: calls.append(a) or True)

    for mod in (mtu_validator, bgp_inference):
        section = probe_runner.empty_section(mod.__name__)
        mod.run(topology, non_rep, section, probe_runner.Cancellation())
        assert section["validator_status"] == "skipped"
        assert section["skip_reason"] == "not-rack-representative"
    assert calls == []  # non-representatives probe nothing


# --- representative second phase scales with rack count, not node count ----------


def _run_mtu(topology, monkeypatch):
    probed = []
    monkeypatch.setattr(
        mtu_validator, "_ping_df", lambda ip, size, c: probed.append(ip) or (True, None)
    )
    section = probe_runner.empty_section("mtu_validator")
    mtu_validator.run(topology, _rep_node(topology), section, probe_runner.Cancellation())
    return section


def test_second_phase_bounded_by_rack_count_not_node_count(monkeypatch):
    # Same rack count, very different node counts -> identical cross-rack work.
    few = _run_mtu(make_topology(num_racks=3, data_nodes_per_rack=2), monkeypatch)
    many = _run_mtu(make_topology(num_racks=3, data_nodes_per_rack=50), monkeypatch)
    assert len(few["cross_rack_mtu"]) == len(many["cross_rack_mtu"]) == 2  # remote racks
    # More racks -> proportionally more records.
    more = _run_mtu(make_topology(num_racks=5, data_nodes_per_rack=2), monkeypatch)
    assert len(more["cross_rack_mtu"]) == 4


def test_bgp_probes_one_representative_per_remote_rack(monkeypatch):
    topology = make_topology(num_racks=4, data_nodes_per_rack=10)
    probed = []
    monkeypatch.setattr(bgp_inference, "_icmp_ok", lambda ip, c: probed.append(ip) or True)
    section = probe_runner.empty_section("bgp_inference")
    bgp_inference.run(topology, _rep_node(topology), section, probe_runner.Cancellation())
    assert len(section["paths"]) == 3  # 3 remote racks
    assert len(probed) == 3  # one representative ICMP per remote rack, no per-node fan-out


# --- fits-under-default vs overruns -----------------------------------------------


def test_fitting_rack_count_completes_under_default():
    fits = max_remote_racks_under(DEFAULT_PROBE_TIMEOUT)
    assert fits >= 2  # the design's two-remote-rack case fits
    assert representative_budget(fits) <= DEFAULT_PROBE_TIMEOUT


def test_larger_rack_count_overruns_default_and_needs_explicit_increase():
    fits = max_remote_racks_under(DEFAULT_PROBE_TIMEOUT)
    over = fits + 1
    # It cannot silently fit: the budget exceeds the default.
    assert representative_budget(over) > DEFAULT_PROBE_TIMEOUT
    # Fitting it needs probe-timeout >= the budget; once that passes the hook
    # timeout minus the flush margin, the Juju hook timeout must be raised too.
    required = representative_budget(over)
    assert required > JUJU_HOOK_TIMEOUT - FLUSH_MARGIN


def test_default_probe_timeout_leaves_flush_margin_under_hook_timeout():
    assert DEFAULT_PROBE_TIMEOUT + FLUSH_MARGIN <= JUJU_HOOK_TIMEOUT


def test_default_probe_timeout_matches_charm_config():
    config = yaml.safe_load((REPO_ROOT / "charm" / "charmcraft.yaml").read_text())
    assert config["config"]["options"]["probe-timeout"]["default"] == DEFAULT_PROBE_TIMEOUT


# --- runner reports overruns rather than silently exceeding -----------------------


def test_runner_overrun_writes_timeout_accounting(tmp_path):
    topology = make_topology(num_racks=2, data_nodes_per_rack=2)
    node = _rep_node(topology)

    def slow(topology, node, section, cancellation):
        cancellation.wait(5)  # wakes when the watchdog cancels
        if cancellation.is_set():
            return  # cooperative cancel: leave the terminal status to the runner
        section["validator_status"] = "complete"

    def quick(topology, node, section, cancellation):
        section["validator_status"] = "complete"

    funcs = dict.fromkeys(probe_runner.VALIDATOR_FUNCS, quick)
    funcs["mtu_validator"] = slow
    out = tmp_path / "probe-output.json"
    status = probe_runner.run_probe(
        topology, node, timeout_seconds=0.2, funcs=funcs, output_path=str(out)
    )
    assert status == "timeout"
    written = load_fixture(out)
    assert written["status"] == "timeout"
    assert written["mtu_validator"]["validator_status"] == "timeout"


# --- cancellation is checked between remote-rack iterations ------------------------


def test_mtu_checks_cancellation_between_racks(monkeypatch):
    topology = make_topology(num_racks=4, data_nodes_per_rack=2)  # 3 remote racks
    cancellation = probe_runner.Cancellation()
    measured = []

    def fake_measure(ip, c):
        measured.append(ip)
        cancellation.cancel("timeout")  # cancel during the first rack's probe
        return 1500, "success"

    monkeypatch.setattr(mtu_validator, "_measure_mtu", fake_measure)
    section = probe_runner.empty_section("mtu_validator")
    mtu_validator.run(topology, _rep_node(topology), section, cancellation)

    assert len(measured) == 1  # only the first rack was probed
    recs = section["cross_rack_mtu"]
    assert len(recs) == 3  # every remote rack still has a record
    assert recs[0]["observation_status"] == "success"
    assert [r["observation_status"] for r in recs[1:]] == ["timeout", "timeout"]


def test_bgp_checks_cancellation_between_racks(monkeypatch):
    topology = make_topology(num_racks=4, data_nodes_per_rack=2)  # 3 remote racks
    cancellation = probe_runner.Cancellation()
    probed = []

    def fake_icmp(ip, c):
        probed.append(ip)
        cancellation.cancel("timeout")  # cancel during the first rack's probe
        return True

    monkeypatch.setattr(bgp_inference, "_icmp_ok", fake_icmp)
    section = probe_runner.empty_section("bgp_inference")
    bgp_inference.run(topology, _rep_node(topology), section, cancellation)

    assert len(probed) == 1  # only the first rack was probed
    paths = section["paths"]
    assert len(paths) == 3  # every remote rack still has a path record
    assert [p["observation_status"] for p in paths[1:]] == ["timeout", "timeout"]
