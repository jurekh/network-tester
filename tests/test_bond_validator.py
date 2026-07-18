"""Bond validator: /proc parsing, LACP PDU capture/parse, mode and cabling checks."""

import bond_validator as bond
import probe_runner
import pytest
from conftest import FIXTURES, lacp_pcap, load_fixture

TOPOLOGY = load_fixture(FIXTURES / "topology_single_rack.json")
NODE = TOPOLOGY["machines"][0]

SWITCH_A = "aa:bb:cc:dd:ee:01"
SWITCH_B = "aa:bb:cc:dd:ee:02"

PROC_8023AD = """Ethernet Channel Bonding Driver: v6.8.0

Bonding Mode: IEEE 802.3ad Dynamic link aggregation
Transmit Hash Policy: layer2 (0)
MII Status: up

802.3ad info
LACP active: on
LACP rate: slow
Min links: 0

Slave Interface: eno1
MII Status: up
Permanent HW addr: 52:54:00:01:01:01
Slave queue ID: 0

Slave Interface: eno2
MII Status: up
Permanent HW addr: 52:54:00:01:01:02
Slave queue ID: 0
"""

PROC_8023AD_PASSIVE = PROC_8023AD.replace("LACP active: on", "LACP active: off")

PROC_STATIC = """Ethernet Channel Bonding Driver: v6.8.0

Bonding Mode: load balancing (xor)
Transmit Hash Policy: layer2 (0)
MII Status: up

Slave Interface: eno1
MII Status: up
Permanent HW addr: 52:54:00:01:01:01
Slave queue ID: 0

Slave Interface: eno2
MII Status: up
Permanent HW addr: 52:54:00:01:01:02
Slave queue ID: 0
"""


@pytest.fixture
def fake_bond(monkeypatch):
    """Mock bond enumeration and tcpdump capture; returns config dict.

    bonds: list of bond dicts returned by _enumerate_bonds.
    captures: maps member interface name -> pcap bytes returned by tcpdump.
    """
    from conftest import FakeProc

    state = {"calls": [], "bonds": [], "captures": {}}

    def fake_popen(cmd, *args, **kwargs):
        state["calls"].append(list(cmd))
        if cmd[0] == "tcpdump":
            iface = cmd[cmd.index("-i") + 1]
            return FakeProc(cmd, 0, state["captures"].get(iface, b""))
        raise AssertionError(f"unexpected tool: {cmd}")

    monkeypatch.setattr(bond.subprocess, "Popen", fake_popen)
    monkeypatch.setattr(bond, "_enumerate_bonds", lambda: state["bonds"])
    monkeypatch.setattr(bond, "CAPTURE_WINDOW_SECONDS", 0)
    return state


def one_bond(mode="802.3ad", lacp_active=True, members=("eno1", "eno2")):
    return {"name": "bond0", "mode": mode, "lacp_active": lacp_active, "members": list(members)}


def run_validator(fake):
    section = probe_runner.empty_section("bond_validator")
    bond.run(TOPOLOGY, NODE, section, probe_runner.Cancellation())
    return section


def finding_types(section):
    return [f["type"] for f in section["findings"]]


# --- /proc/net/bonding parsing (6.1) -----------------------------------------------


def test_parse_bond_proc_reads_mode_members_and_lacp_active():
    parsed = bond._parse_bond_proc("bond0", PROC_8023AD)
    assert "802.3ad" in parsed["mode"]
    assert parsed["lacp_active"] is True
    assert parsed["members"] == ["eno1", "eno2"]


def test_parse_bond_proc_decodes_lacp_active_off():
    parsed = bond._parse_bond_proc("bond0", PROC_8023AD_PASSIVE)
    assert parsed["lacp_active"] is False


def test_parse_bond_proc_static_mode_defaults_lacp_active_true():
    parsed = bond._parse_bond_proc("bond0", PROC_STATIC)
    assert "802.3ad" not in parsed["mode"]
    assert parsed["lacp_active"] is True  # absent setting means active (kernel default)


# --- LACP PDU parsing (6.2, 6.3) ---------------------------------------------------


def test_parse_lacp_pcap_extracts_actor_and_partner_fields():
    data = lacp_pcap(
        {
            "actor_system_id": SWITCH_A,
            "actor_key": 17,
            "actor_state": 0x3D,
            "partner_system_id": "52:54:00:01:01:01",
            "partner_key": 9,
            "partner_state": 0x3C,
        }
    )
    pdus = bond.parse_lacp_pcap(data)
    assert len(pdus) == 1
    pdu = pdus[0]
    assert pdu["actor_system_id"] == SWITCH_A
    assert pdu["actor_port_key"] == 17
    assert pdu["partner_system_id"] == "52:54:00:01:01:01"
    assert pdu["partner_port_key"] == 9
    assert pdu["actor_state"]["active"] is True
    assert pdu["partner_state"]["active"] is False


def test_parse_lacp_pcap_tolerates_garbage():
    assert bond.parse_lacp_pcap(b"") == []
    assert bond.parse_lacp_pcap(b"\x00" * 8) == []


# --- no bonds (6.6) ----------------------------------------------------------------


def test_no_bonds_completes_with_empty_list(fake_bond):
    section = run_validator(fake_bond)
    assert section["validator_status"] == "complete"
    assert section["bonds"] == []
    assert section["findings"] == []
    assert not [c for c in fake_bond["calls"] if c[0] == "tcpdump"]


# --- capture command and concurrency (6.2) -----------------------------------------


def test_capture_runs_per_member_with_lacp_filter(fake_bond):
    fake_bond["bonds"] = [one_bond()]
    fake_bond["captures"] = {
        "eno1": lacp_pcap({"actor_system_id": SWITCH_A}),
        "eno2": lacp_pcap({"actor_system_id": SWITCH_A}),
    }
    run_validator(fake_bond)
    captures = [c for c in fake_bond["calls"] if c[0] == "tcpdump"]
    expected = ["-Q", "in", "ether", "proto", "0x8809", "-c", "10", "-w", "-", "--immediate-mode"]
    assert captures == [
        ["tcpdump", "-i", "eno1", *expected],
        ["tcpdump", "-i", "eno2", *expected],
    ]


# --- bond mode mismatch (6.4) ------------------------------------------------------


def test_lacp_host_lacp_switch_passes(fake_bond):
    fake_bond["bonds"] = [one_bond()]
    fake_bond["captures"] = {
        "eno1": lacp_pcap({"actor_system_id": SWITCH_A}),
        "eno2": lacp_pcap({"actor_system_id": SWITCH_A}),
    }
    section = run_validator(fake_bond)
    assert section["validator_status"] == "complete"
    assert finding_types(section) == []
    assert section["bonds"][0]["bond_mode"] == "pass"


def test_lacp_host_static_switch_records_mismatch(fake_bond):
    fake_bond["bonds"] = [one_bond()]  # 802.3ad, lacp_active on, no PDUs captured
    section = run_validator(fake_bond)
    assert finding_types(section) == ["bond-mode-mismatch"]
    finding = section["findings"][0]
    assert finding["classification"] == "definitive"
    assert "static bonding" in finding["hint"]
    assert section["bonds"][0]["members"][0]["lacp_advertised"] is False


def test_static_host_lacp_switch_records_mismatch(fake_bond):
    fake_bond["bonds"] = [one_bond(mode="load balancing (xor)")]
    fake_bond["captures"] = {
        "eno1": lacp_pcap({"actor_system_id": SWITCH_A}),
        "eno2": lacp_pcap({"actor_system_id": SWITCH_A}),
    }
    section = run_validator(fake_bond)
    assert finding_types(section) == ["bond-mode-mismatch"]
    assert "mode=802.3ad" in section["findings"][0]["hint"]


# --- LACP passive accounting (6.7) -------------------------------------------------


def test_both_passive_no_pdu_extends_hint(fake_bond):
    fake_bond["bonds"] = [one_bond(lacp_active=False)]  # host passive, no PDUs
    section = run_validator(fake_bond)
    assert finding_types(section) == ["bond-mode-mismatch"]
    hint = section["findings"][0]["hint"]
    assert "lacp_active off" in hint
    assert "passive" in hint


def test_passive_switch_active_host_passes_and_records_flag(fake_bond):
    fake_bond["bonds"] = [one_bond()]  # host active
    fake_bond["captures"] = {
        "eno1": lacp_pcap({"actor_system_id": SWITCH_A, "actor_state": 0x3C}),  # switch passive
        "eno2": lacp_pcap({"actor_system_id": SWITCH_A, "actor_state": 0x3C}),
    }
    section = run_validator(fake_bond)
    assert finding_types(section) == []
    assert section["bonds"][0]["bond_mode"] == "pass"
    pdu = section["bonds"][0]["members"][0]["pdus"][0]
    assert pdu["actor_state"]["active"] is False


# --- asymmetric cable detection (6.5, 6.8, 6.9) ------------------------------------


def test_members_from_different_switches_flag_asymmetric_cable(fake_bond):
    fake_bond["bonds"] = [one_bond()]
    fake_bond["captures"] = {
        "eno1": lacp_pcap({"actor_system_id": SWITCH_A}),
        "eno2": lacp_pcap({"actor_system_id": SWITCH_B}),
    }
    section = run_validator(fake_bond)
    assert "asymmetric-bond-cable" in finding_types(section)
    finding = next(f for f in section["findings"] if f["type"] == "asymmetric-bond-cable")
    assert finding["classification"] == "definitive"
    assert "bond0" in finding["hint"]
    assert section["bonds"][0]["bond_cabling"] == "fail"


def test_members_from_same_switch_pass_cabling(fake_bond):
    fake_bond["bonds"] = [one_bond()]
    fake_bond["captures"] = {
        "eno1": lacp_pcap({"actor_system_id": SWITCH_A}),
        "eno2": lacp_pcap({"actor_system_id": SWITCH_A}),
    }
    section = run_validator(fake_bond)
    assert "asymmetric-bond-cable" not in finding_types(section)
    assert section["bonds"][0]["bond_cabling"] == "pass"


def test_asymmetric_uses_actor_not_partner_system_id(fake_bond):
    # Both members hear the same switch (actor) but the switch reports
    # different partners (the two local member MACs); this is NOT asymmetric.
    fake_bond["bonds"] = [one_bond()]
    fake_bond["captures"] = {
        "eno1": lacp_pcap({"actor_system_id": SWITCH_A, "partner_system_id": SWITCH_A}),
        "eno2": lacp_pcap({"actor_system_id": SWITCH_A, "partner_system_id": SWITCH_B}),
    }
    section = run_validator(fake_bond)
    assert "asymmetric-bond-cable" not in finding_types(section)


# --- cancellation contract (4.6 carry-over) ----------------------------------------


def test_cancellation_flushes_unattempted_bonds():
    section = probe_runner.empty_section("bond_validator")
    cancellation = probe_runner.Cancellation()
    cancellation.cancel("timeout")

    import bond_validator as b

    orig = b._enumerate_bonds
    b._enumerate_bonds = lambda: [one_bond()]
    try:
        b.run(TOPOLOGY, NODE, section, cancellation)
    finally:
        b._enumerate_bonds = orig
    assert section["validator_status"] == "not_started"  # runner assigns terminal status
    flushed = [e for e in section["bonds"] if e.get("observation_status") == "timeout"]
    assert [e["bond"] for e in flushed] == ["bond0"]
