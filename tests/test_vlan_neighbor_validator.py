"""VLAN neighbor validator: peer derivation, ARP/ICMP probing, classification."""

import json

import probe_runner
import vlan_neighbor_validator as vlan
from conftest import FIXTURES, PING_OK, arp_pcap, load_fixture

TOPOLOGY = load_fixture(FIXTURES / "topology_mixed_scope.json")

# node under test: r1-data-02 (eth0, fabric-data vlan 100, 10.20.1.12)
NODE = next(m for m in TOPOLOGY["machines"] if m["system_id"] == "aaa002")
PEER_IP = "10.20.1.11"  # aaa001, expected in-scope peer
OOS_MAC = "52:54:00:01:03:01"  # aaa003, expected but out of scope
STRANGER_MAC = "52:54:00:99:99:99"
STRANGER_IP = "10.20.1.99"


def run_validator(topology=TOPOLOGY, node=NODE, fake=None):
    section = probe_runner.empty_section("vlan_neighbor_validator")
    cancellation = probe_runner.Cancellation()
    vlan.run(topology, node, section, cancellation)
    return section


def finding_types(section):
    return [f["type"] for f in section["findings"]]


# --- peer set derivation (5.1, 5.6) ------------------------------------------------


def test_expected_peers_share_fabric_and_vlan():
    plans = vlan.derive_peer_sets(TOPOLOGY, NODE)
    assert len(plans) == 1
    expected_ids = {peer["system_id"] for peer in plans[0]["expected"]}
    assert expected_ids == {"aaa001"}  # same fabric+vlan, in scope
    assert OOS_MAC in plans[0]["out_of_scope"]  # aaa003 recognized, not probed
    # rack-2 machines are on a different vlan_tag: not peers at all
    known = set(plans[0]["out_of_scope"]) | {m for p in plans[0]["expected"] for m in p["macs"]}
    assert "52:54:00:02:02:01" not in known


def bmc_oam_topology():
    """rack-1: bmc-oam node, out-of-scope rack-controller, forbidden bmc peer."""
    doc = load_fixture(FIXTURES / "topology_mixed_scope.json")
    iface = {
        "name": "eth0",
        "type": "physical",
        "fabric": "fabric-mgmt",
        "fabric_class": "management",
        "vlan_tag": 10,
        "subnet_cidr": "10.10.1.0/24",
        "gateway_ip": "10.10.1.1",
    }
    doc["machines"] = [
        next(m for m in doc["machines"] if m["system_id"] == "rc0001"),
        {
            "system_id": "oam001",
            "hostname": "r1-oam-01",
            "rack": "rack-1",
            "role": "bmc-oam",
            "in_scope": True,
            "interfaces": [dict(iface, mac="52:54:00:0a:00:01", ip="10.10.1.5")],
        },
        {
            "system_id": "oam002",
            "hostname": "r1-oam-02",
            "rack": "rack-1",
            "role": "bmc-oam",
            "in_scope": False,
            "interfaces": [dict(iface, mac="52:54:00:0a:00:02", ip="10.10.1.6")],
        },
    ]
    return doc, doc["machines"][1]


def test_bmc_oam_expected_set_restricted_to_rack_controller():
    topology, node = bmc_oam_topology()
    plans = vlan.derive_peer_sets(topology, node)
    assert len(plans) == 1
    # rack-controller is out of scope: recognized but not actively probed
    assert plans[0]["expected"] == []
    assert "52:54:00:01:00:01" in plans[0]["out_of_scope"]
    # the other bmc-oam machine is known-forbidden, not skipped
    assert "52:54:00:0a:00:02" in plans[0]["forbidden"]


def test_bmc_oam_observing_forbidden_peer_is_failure_not_skip(fake_tools):
    topology, node = bmc_oam_topology()
    fake_tools["capture"] = arp_pcap(("52:54:00:0a:00:02", "10.10.1.6"))
    section = probe_runner.empty_section("vlan_neighbor_validator")
    vlan.run(topology, node, section, probe_runner.Cancellation())
    assert finding_types(section) == ["forbidden-l2-neighbor"]
    finding = section["findings"][0]
    assert finding["classification"] == "definitive"
    assert finding["details"]["peer_system_id"] == "oam002"
    assert not [o for o in section["observations"] if o.get("skipped")]


# --- ARP probing (5.2, 5.7a) -------------------------------------------------------


def test_arping_binds_to_interface_where_peer_expected(fake_tools):
    fake_tools["arp"][PEER_IP] = True
    fake_tools["ping3"][PEER_IP] = PING_OK
    run_validator()
    arpings = [c for c in fake_tools["calls"] if c[0] == "arping"]
    assert arpings == [["arping", "-I", "eth0", "-c", "1", "-w", "2", PEER_IP]]


def test_capture_runs_on_active_interface(fake_tools):
    fake_tools["arp"][PEER_IP] = True
    fake_tools["ping3"][PEER_IP] = PING_OK
    run_validator()
    captures = [c for c in fake_tools["calls"] if c[0] == "tcpdump"]
    expected = ["tcpdump", "-i", "eth0", "arp", "-c", "2000", "-w", "-", "--immediate-mode"]
    assert captures == [expected]


def test_sweep_arpings_all_peers_concurrently(fake_tools):
    """One sweep spawns every peer's arping before reaping any of them, so
    sweep wall time is bounded by the slowest single arping (-w 2), not the
    sum over peers; cadence survives segments with many expected peers."""
    topology = load_fixture(FIXTURES / "topology_mixed_scope.json")
    peer = next(m for m in topology["machines"] if m["system_id"] == "aaa001")
    extra = json.loads(json.dumps(peer))
    extra["system_id"] = "aaa009"
    extra["hostname"] = "r1-data-09"
    extra["interfaces"][0]["mac"] = "52:54:00:01:09:01"
    extra["interfaces"][0]["ip"] = "10.20.1.19"
    topology["machines"].append(extra)
    fake_tools["arp"][PEER_IP] = True
    section = probe_runner.empty_section("vlan_neighbor_validator")
    vlan.run(topology, NODE, section, probe_runner.Cancellation())
    arpings = [c for c in fake_tools["calls"] if c[0] == "arping"]
    assert sorted(c[-1] for c in arpings) == [PEER_IP, "10.20.1.19"]
    # the unanswered peer is a missing-neighbor failure, the answered one is not
    missing = [
        f["details"]["peer_system_id"]
        for f in section["findings"]
        if f["type"] == "missing-l2-neighbor"
    ]
    assert missing == ["aaa009"]


def test_missing_expected_peer_recorded_as_failure(fake_tools):
    # no ARP response and no ICMP response from the expected peer
    section = run_validator(fake=fake_tools)
    types = finding_types(section)
    assert "missing-l2-neighbor" in types
    assert "icmp-unreachable" in types
    missing = next(f for f in section["findings"] if f["type"] == "missing-l2-neighbor")
    assert missing["classification"] == "definitive"
    assert missing["scope"] == "interface"
    assert missing["details"]["peer_system_id"] == "aaa001"


# --- unexpected neighbor detection (5.3, 5.7b, 5.7c, 5.7d) --------------------------


def test_unexpected_neighbor_reachable_records_both_findings(fake_tools):
    fake_tools["arp"][PEER_IP] = True
    fake_tools["ping3"][PEER_IP] = PING_OK
    fake_tools["capture"] = arp_pcap((STRANGER_MAC, STRANGER_IP))
    fake_tools["ping1"][STRANGER_IP] = True
    section = run_validator(fake=fake_tools)
    types = finding_types(section)
    assert types.count("unexpected-l2-neighbor") == 1
    assert types.count("unexpected-reachability") == 1
    unexpected = next(f for f in section["findings"] if f["type"] == "unexpected-l2-neighbor")
    assert STRANGER_MAC in unexpected["hint"] and STRANGER_IP in unexpected["hint"]
    assert unexpected["details"] == {"interface": "eth0", "mac": STRANGER_MAC, "ip": STRANGER_IP}


def test_unexpected_neighbor_unreachable_records_only_l2_finding(fake_tools):
    fake_tools["arp"][PEER_IP] = True
    fake_tools["ping3"][PEER_IP] = PING_OK
    fake_tools["capture"] = arp_pcap((STRANGER_MAC, STRANGER_IP))
    fake_tools["ping1"][STRANGER_IP] = False
    section = run_validator(fake=fake_tools)
    types = finding_types(section)
    assert types == ["unexpected-l2-neighbor"]
    follow_ups = [c for c in fake_tools["calls"] if c[0] == "ping" and "1" == c[c.index("-c") + 1]]
    assert follow_ups == [["ping", "-c", "1", "-W", "2", STRANGER_IP]]


def test_known_out_of_scope_peer_skipped_not_failed(fake_tools):
    fake_tools["arp"][PEER_IP] = True
    fake_tools["ping3"][PEER_IP] = PING_OK
    fake_tools["capture"] = arp_pcap((OOS_MAC, "10.20.1.13"))
    section = run_validator(fake=fake_tools)
    assert finding_types(section) == []
    skipped = [o for o in section["observations"] if o.get("skipped")]
    assert len(skipped) == 1
    assert skipped[0]["type"] == "known-out-of-scope-peer-observed"
    assert skipped[0]["peer_system_id"] == "aaa003"
    # no follow-up ping for a known out-of-scope peer
    assert not [c for c in fake_tools["calls"] if c[0] == "ping" and c[-1] == "10.20.1.13"]


def test_own_macs_in_capture_are_ignored(fake_tools):
    fake_tools["arp"][PEER_IP] = True
    fake_tools["ping3"][PEER_IP] = PING_OK
    fake_tools["capture"] = arp_pcap((NODE["interfaces"][0]["mac"], "10.20.1.12"))
    section = run_validator(fake=fake_tools)
    assert finding_types(section) == []


# --- ICMP reachability (5.4, 5.7e) --------------------------------------------------


def test_clean_run_sets_complete_with_observation(fake_tools):
    fake_tools["arp"][PEER_IP] = True
    fake_tools["ping3"][PEER_IP] = PING_OK
    section = run_validator(fake=fake_tools)
    assert section["validator_status"] == "complete"
    assert finding_types(section) == []
    obs = [o for o in section["observations"] if o["type"] == "expected-peer-observed"]
    assert len(obs) == 1
    assert obs[0]["peer_system_id"] == "aaa001"
    assert obs[0]["arp_observed"] is True
    assert obs[0]["icmp_reachable"] is True
    assert obs[0]["rtt_ms"] == {"min": 0.211, "avg": 0.33, "max": 0.49}
    assert obs[0]["loss_pct"] == 0


def test_ping_fallback_used_when_raw_socket_denied(fake_tools):
    fake_tools["arp"][PEER_IP] = True
    fake_tools["ping3"][PEER_IP] = PING_OK
    run_validator(fake=fake_tools)
    pings = [c for c in fake_tools["calls"] if c[0] == "ping" and c[c.index("-c") + 1] == "3"]
    assert pings == [["ping", "-c", "3", "-W", "2", PEER_IP]]


def test_icmp_unreachable_expected_peer_failure(fake_tools):
    fake_tools["arp"][PEER_IP] = True  # ARP fine, ICMP 100% loss
    section = run_validator(fake=fake_tools)
    types = finding_types(section)
    assert "icmp-unreachable" in types
    assert "missing-l2-neighbor" not in types
    finding = next(f for f in section["findings"] if f["type"] == "icmp-unreachable")
    assert "r1-data-01" in finding["hint"]


class _FakeClock:
    """monotonic() driven by the patched cancellation.wait, so window-length
    behavior is testable without real sleeps."""

    def __init__(self):
        self.now = 0.0

    def monotonic(self):
        return self.now


def _run_with_fake_window(fake_tools, monkeypatch, window):
    fake_tools["arp"][PEER_IP] = True
    fake_tools["ping3"][PEER_IP] = PING_OK
    section = probe_runner.empty_section("vlan_neighbor_validator")
    cancellation = probe_runner.Cancellation()
    clock = _FakeClock()
    waits = []

    def fake_wait(timeout):
        waits.append(timeout)
        clock.now += timeout
        return False

    cancellation.wait = fake_wait
    monkeypatch.setattr(vlan.time, "monotonic", clock.monotonic)
    monkeypatch.setattr(vlan, "CAPTURE_WINDOW_SECONDS", window)
    vlan.run(TOPOLOGY, NODE, section, cancellation)
    return section, waits


def test_capture_window_held_open_for_full_cap(fake_tools, monkeypatch):
    """The passive capture stays open for the whole window even when the
    arping phase finishes early, so nodes with no expected peers still
    detect strangers (the wrong-vlan fault relies on this)."""
    section, waits = _run_with_fake_window(fake_tools, monkeypatch, 30)
    assert section["validator_status"] == "complete"
    assert sum(waits) > 25  # held nearly the whole 30s window


def test_arping_sweep_repeats_across_capture_window(fake_tools, monkeypatch):
    """The arping sweep repeats until the window closes: the sweep is this
    node's transmission that concurrent observers passively classify, and
    probe start times skew across units, so a single burst at window-open
    would be invisible to an observer whose window opened slightly later."""
    _run_with_fake_window(fake_tools, monkeypatch, 30)
    arpings = [c for c in fake_tools["calls"] if c[0] == "arping"]
    assert len(arpings) >= 4  # one sweep per SWEEP_INTERVAL_SECONDS, not one total
    assert all(c == ["arping", "-I", "eth0", "-c", "1", "-w", "2", PEER_IP] for c in arpings)


# --- cancellation contract (4.6 carry-over) -----------------------------------------


def test_cancellation_flushes_unattempted_peers(fake_tools):
    section = probe_runner.empty_section("vlan_neighbor_validator")
    cancellation = probe_runner.Cancellation()
    cancellation.cancel("timeout")
    vlan.run(TOPOLOGY, NODE, section, cancellation)
    assert section["validator_status"] == "not_started"  # runner assigns terminal status
    unattempted = [o for o in section["observations"] if o.get("observation_status") == "timeout"]
    assert [o["peer_system_id"] for o in unattempted] == ["aaa001"]


# --- pcap parsing -------------------------------------------------------------------


def test_parse_arp_pcap_extracts_sender_mac_and_ip():
    data = arp_pcap((STRANGER_MAC, STRANGER_IP), (OOS_MAC, "10.20.1.13"))
    assert vlan.parse_arp_pcap(data) == [
        (STRANGER_MAC, STRANGER_IP),
        (OOS_MAC, "10.20.1.13"),
    ]


def test_parse_arp_pcap_tolerates_garbage():
    assert vlan.parse_arp_pcap(b"") == []
    assert vlan.parse_arp_pcap(b"\x00" * 10) == []


# --- data-fabric gateway exclusion (stage 7) ---------------------------------------


def test_data_gateway_mac_is_infrastructure_not_unexpected(fake_tools):
    # the data subnet gateway (FRR ToR) appears in ARP capture; it is expected
    # infrastructure, not an unexpected L2 neighbor
    gw_ip = NODE["interfaces"][0].get("gateway_ip")
    assert gw_ip  # the node-under-test's data interface carries a gateway
    fake_tools["arp"][PEER_IP] = True
    fake_tools["ping3"][PEER_IP] = PING_OK
    fake_tools["capture"] = arp_pcap(("52:54:00:ff:ff:fe", gw_ip))
    section = run_validator(fake=fake_tools)
    assert "unexpected-l2-neighbor" not in finding_types(section)
    infra = [o for o in section["observations"] if o["type"] == "infrastructure-gateway-observed"]
    assert len(infra) == 1 and infra[0]["ip"] == gw_ip
