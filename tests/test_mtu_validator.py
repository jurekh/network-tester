"""MTU validator: representative selection, DF-bit probing, binary search."""

import mtu_validator as mtu
import probe_runner
from conftest import FIXTURES, load_fixture

TOPOLOGY = load_fixture(FIXTURES / "topology_two_rack.json")
# rack-1 representative (lexicographically lowest in-scope data system_id)
REP = next(m for m in TOPOLOGY["machines"] if m["system_id"] == "aaa001")
NON_REP = next(m for m in TOPOLOGY["machines"] if m["system_id"] == "aaa002")
RC = next(m for m in TOPOLOGY["machines"] if m["system_id"] == "rc0001")
TARGET_IP = "10.20.2.11"  # bbb001, rack-2 representative


def run(node=REP, fake=None):
    section = probe_runner.empty_section("mtu_validator")
    mtu.run(TOPOLOGY, node, section, probe_runner.Cancellation())
    return section


class FakePing:
    """Records probed sizes; returns success/frag per a size predicate."""

    def __init__(self, ok_if=lambda size: True, frag=None):
        self.ok_if = ok_if
        self.frag = frag  # dict size->mtu, or None
        self.sizes = []

    def __call__(self, ip, size, cancellation):
        self.sizes.append(size)
        frag_mtu = (self.frag or {}).get(size)
        return self.ok_if(size), frag_mtu


# --- skip cases (7.1 representative selection) -------------------------------------


def test_non_representative_data_node_skips(monkeypatch):
    fake = FakePing()
    monkeypatch.setattr(mtu, "_ping_df", fake)
    section = run(node=NON_REP)
    assert section["validator_status"] == "skipped"
    assert section["skip_reason"] == "not-rack-representative"
    assert section["cross_rack_mtu"] == []
    assert fake.sizes == []  # no probing


def test_non_data_node_skips(monkeypatch):
    monkeypatch.setattr(mtu, "_ping_df", FakePing())
    section = run(node=RC)
    assert section["validator_status"] == "skipped"
    assert section["skip_reason"] == "not-rack-representative"


def test_no_cross_rack_peers_skips(monkeypatch):
    monkeypatch.setattr(mtu, "_ping_df", FakePing())
    single = load_fixture(FIXTURES / "topology_two_rack.json")
    single["machines"] = [m for m in single["machines"] if m["rack"] == "rack-1"]
    section = probe_runner.empty_section("mtu_validator")
    mtu.run(single, REP, section, probe_runner.Cancellation())
    assert section["validator_status"] == "skipped"
    assert section["skip_reason"] == "no cross-rack data peers"


# --- measurement (7.1, 7.3, 7.4) --------------------------------------------------


def test_jumbo_success_records_9000(monkeypatch):
    monkeypatch.setattr(mtu, "_ping_df", FakePing(ok_if=lambda s: True))
    section = run()
    assert section["validator_status"] == "complete"
    recs = section["cross_rack_mtu"]
    assert len(recs) == 1
    r = recs[0]
    assert r["source_rack"] == "rack-1" and r["target_rack"] == "rack-2"
    assert r["source_node"] == "aaa001" and r["target_node"] == "bbb001"
    assert r["observed_path_mtu_bytes"] == 9000
    assert r["observation_status"] == "success"


def test_standard_ethernet_binary_search_records_1500(monkeypatch):
    # 8972 fails, 1472 succeeds, nothing between succeeds -> 1500
    fake = FakePing(ok_if=lambda s: s <= 1472)
    monkeypatch.setattr(mtu, "_ping_df", fake)
    section = run()
    r = section["cross_rack_mtu"][0]
    assert r["observed_path_mtu_bytes"] == 1500
    assert r["observation_status"] == "success"
    assert 1472 in fake.sizes and 8972 in fake.sizes
    assert len(fake.sizes) <= 7  # 2 initial + max 5 binary-search


def test_binary_search_finds_intermediate_mtu(monkeypatch):
    fake = FakePing(ok_if=lambda s: s <= 4000)
    monkeypatch.setattr(mtu, "_ping_df", fake)
    section = run()
    r = section["cross_rack_mtu"][0]
    observed = r["observed_path_mtu_bytes"]
    assert 1500 < observed <= 4028  # largest successful refined probe + 28
    assert observed - 28 in fake.sizes
    assert r["observation_status"] == "success"


def test_frag_needed_records_indicated_mtu(monkeypatch):
    # 8972 fails with an ICMP fragmentation-needed reporting mtu 1500
    fake = FakePing(ok_if=lambda s: False, frag={8972: 1500})
    monkeypatch.setattr(mtu, "_ping_df", fake)
    section = run()
    r = section["cross_rack_mtu"][0]
    assert r["observed_path_mtu_bytes"] == 1500
    assert r["observation_status"] == "success"


def test_all_dropped_is_inconclusive(monkeypatch):
    monkeypatch.setattr(mtu, "_ping_df", FakePing(ok_if=lambda s: False))
    section = run()
    r = section["cross_rack_mtu"][0]
    assert r["observed_path_mtu_bytes"] is None
    assert r["observation_status"] == "inconclusive"
    assert "manual MTU verification" in r.get("note", "")


# --- cancellation (7.1 timeout/cancel records) ------------------------------------


def test_cancellation_flushes_remaining_racks():
    section = probe_runner.empty_section("mtu_validator")
    cancellation = probe_runner.Cancellation()
    cancellation.cancel("timeout")
    mtu.run(TOPOLOGY, REP, section, cancellation)
    # runner assigns the terminal status; records carry the reason
    recs = section["cross_rack_mtu"]
    assert [r["target_rack"] for r in recs] == ["rack-2"]
    assert recs[0]["observation_status"] == "timeout"
    assert recs[0]["observed_path_mtu_bytes"] is None
