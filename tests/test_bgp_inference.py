"""BGP inference: representative selection, rep/fallback ICMP, traceroute classification."""

import bgp_inference as bgp
import probe_runner
from conftest import FIXTURES, load_fixture

TOPOLOGY = load_fixture(FIXTURES / "topology_two_rack.json")
REP = next(m for m in TOPOLOGY["machines"] if m["system_id"] == "aaa001")  # rack-1 rep
NON_REP = next(m for m in TOPOLOGY["machines"] if m["system_id"] == "aaa002")
RC = next(m for m in TOPOLOGY["machines"] if m["system_id"] == "rc0001")
REP_TARGET = "10.20.2.11"  # bbb001
FALLBACK_TARGET = "10.20.2.12"  # bbb002


def run(node=REP, topology=TOPOLOGY):
    section = probe_runner.empty_section("bgp_inference")
    bgp.run(topology, node, section, probe_runner.Cancellation())
    return section


def setup_probes(monkeypatch, reachable=(), hops=None, truncated=False):
    """Mock ICMP (set of reachable IPs) and traceroute (hop list)."""
    monkeypatch.setattr(bgp, "_icmp_ok", lambda ip, c: ip in reachable)
    monkeypatch.setattr(bgp, "_traceroute", lambda ip, c: (hops or [], truncated))


def path(section, target_rack="rack-2"):
    return next(p for p in section["paths"] if p["target_rack"] == target_rack)


# --- skip cases (7.6) -------------------------------------------------------------


def test_non_representative_skips(monkeypatch):
    setup_probes(monkeypatch)
    section = run(node=NON_REP)
    assert section["validator_status"] == "skipped"
    assert section["skip_reason"] == "not-rack-representative"
    assert section["paths"] == []


def test_non_data_node_skips(monkeypatch):
    setup_probes(monkeypatch)
    section = run(node=RC)
    assert section["validator_status"] == "skipped"
    assert section["paths"] == []


# --- representative / fallback reachability (7.6) ---------------------------------


def test_representative_reachable(monkeypatch):
    setup_probes(monkeypatch, reachable={REP_TARGET})
    section = run()
    assert section["validator_status"] == "complete"
    p = path(section)
    assert p["reachable"] is True
    assert p["target_role"] == "representative"
    assert p["observation_status"] == "success"
    assert p["representative_target"] == "bbb001"
    assert "finding" not in p or p["finding"] is None


def test_fallback_reachable_when_representative_fails(monkeypatch):
    setup_probes(monkeypatch, reachable={FALLBACK_TARGET})
    section = run()
    p = path(section)
    assert p["reachable"] is True
    assert p["target_role"] == "fallback"
    assert p["observation_status"] == "success"
    assert p["fallback_target"] == "bbb002"


# --- traceroute classification on dual failure (7.7, 7.8, 7.9) --------------------


def test_both_fail_likely_bgp_failure_at_local_tor(monkeypatch):
    # last responding hop is the local rack gateway (ToR)
    hops = [{"hop": 1, "ip": "10.20.1.1", "rtt_ms": 0.1}, {"hop": 2, "ip": None, "rtt_ms": None}]
    setup_probes(monkeypatch, reachable=set(), hops=hops)
    section = run()
    p = path(section)
    assert p["reachable"] is False
    assert p["target_role"] is None
    assert p["observation_status"] == "failure"
    f = p["finding"]
    assert f["type"] == "likely-bgp-failure"
    assert f["classification"] == "inferred"
    assert f["scope"] == "rack-pair"
    assert f["diagnosis_confidence"] == "inferred"
    assert f["details"]["traceroute_hops"] == hops


def run_with(monkeypatch, hops, reachable=frozenset()):
    setup_probes(monkeypatch, reachable=reachable, hops=hops)
    return path(run())


def test_both_fail_routing_failure_beyond_tor(monkeypatch):
    hops = [
        {"hop": 1, "ip": "10.20.1.1", "rtt_ms": 0.1},
        {"hop": 2, "ip": "10.99.0.1", "rtt_ms": 0.5},
    ]
    f = run_with(monkeypatch, hops).get("finding")
    assert f["type"] == "routing-failure"
    assert f["classification"] == "inferred"
    assert "10.99.0.1" in f["hint"]


def test_both_fail_intra_rack_routing(monkeypatch):
    # last hop is inside the target rack subnet but not the target node
    hops = [
        {"hop": 1, "ip": "10.20.1.1", "rtt_ms": 0.1},
        {"hop": 2, "ip": "10.20.2.1", "rtt_ms": 0.5},
    ]
    f = run_with(monkeypatch, hops).get("finding")
    assert f["type"] == "intra-rack-routing"
    assert f["classification"] == "inferred"


def test_all_hops_nonresponding_icmp_blocked(monkeypatch):
    hops = [{"hop": h, "ip": None, "rtt_ms": None} for h in range(1, 4)]
    f = run_with(monkeypatch, hops).get("finding")
    assert f["type"] == "icmp-blocked"
    assert f["classification"] == "inconclusive"
    assert f["diagnosis_confidence"] == "inconclusive"


def test_single_node_rack_failure_triggers_traceroute(monkeypatch):
    # rack-2 with a single data node: representative is the only target
    topo = load_fixture(FIXTURES / "topology_two_rack.json")
    topo["machines"] = [m for m in topo["machines"] if m["system_id"] not in ("bbb002", "bbb003")]
    hops = [{"hop": 1, "ip": "10.20.1.1", "rtt_ms": 0.1}]
    setup_probes(monkeypatch, reachable=set(), hops=hops)
    section = probe_runner.empty_section("bgp_inference")
    bgp.run(topo, REP, section, probe_runner.Cancellation())
    p = next(p for p in section["paths"] if p["target_rack"] == "rack-2")
    assert p["fallback_target"] is None
    assert p["reachable"] is False
    assert p["finding"]["type"] == "likely-bgp-failure"


# --- cancellation (7.6) -----------------------------------------------------------


def test_cancellation_flushes_remaining_paths():
    section = probe_runner.empty_section("bgp_inference")
    cancellation = probe_runner.Cancellation()
    cancellation.cancel("timeout")
    bgp.run(TOPOLOGY, REP, section, cancellation)
    assert [p["target_rack"] for p in section["paths"]] == ["rack-2"]
    assert section["paths"][0]["observation_status"] == "timeout"
    assert section["paths"][0]["reachable"] is None
