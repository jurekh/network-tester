"""Bond validator: LACP PDU capture and bond mode/cabling checks.

Captures LACP PDUs (EtherType 0x8809) per bond member interface via tcpdump,
parses them with stdlib struct, and detects bond-mode-mismatch and
asymmetric-bond-cable faults. Output conforms to the shared probe-output
schema (see schemas.py).

Contract with the probe runner: mutate ``section`` in place, register
subprocesses with ``cancellation``, check ``cancellation.is_set()`` between
probe iterations, and set a terminal ``validator_status`` only on normal
completion (the runner assigns timeout/cancelled when interrupted).
"""

import re
import struct
import subprocess
from pathlib import Path

BONDING_DIR = Path("/proc/net/bonding")

# LACP slow rate sends one PDU every 30s, so the capture window must exceed
# 30s or a correctly configured LACP switch port records as advertising
# nothing. All member captures run concurrently for a single window.
CAPTURE_WINDOW_SECONDS = 35

ETHERTYPE_LACP = 0x8809
ETHERTYPE_VLAN = 0x8100
LACP_SUBTYPE = 0x01

# LACPDU actor TLV starts at offset 2 in the LACP payload; the actor system
# ID is at +4, key at +10, port at +14, state at +16; the partner TLV
# follows at offset 22 with the same internal layout.
_ACTOR_TLV = 2
_PARTNER_TLV = 22


def _parse_bond_proc(name, text):
    """Parse one /proc/net/bonding/<bond> file into a bond descriptor."""
    mode_match = re.search(r"Bonding Mode:\s*(.+)", text)
    mode = mode_match.group(1).strip() if mode_match else ""
    active_match = re.search(r"LACP active:\s*(\w+)", text)
    # An absent setting means active, the kernel default.
    lacp_active = active_match.group(1).lower() != "off" if active_match else True
    members = re.findall(r"Slave Interface:\s*(\S+)", text)
    return {"name": name, "mode": mode, "lacp_active": lacp_active, "members": members}


def _enumerate_bonds():
    """Read every bond descriptor from /proc/net/bonding/."""
    if not BONDING_DIR.is_dir():
        return []
    bonds = []
    for path in sorted(BONDING_DIR.iterdir()):
        bonds.append(_parse_bond_proc(path.name, path.read_text()))
    return bonds


def _decode_state(byte):
    """Decode the IEEE 802.3ad actor/partner state flag octet."""
    return {
        "active": bool(byte & 0x01),
        "timeout_short": bool(byte & 0x02),
        "aggregation": bool(byte & 0x04),
        "in_sync": bool(byte & 0x08),
        "collecting": bool(byte & 0x10),
        "distributing": bool(byte & 0x20),
        "defaulted": bool(byte & 0x40),
        "expired": bool(byte & 0x80),
    }


def _parse_lacpdu(payload):
    """Decode one LACP payload (after the ethernet header). None if invalid."""
    if len(payload) < _PARTNER_TLV + 17:
        return None
    if payload[0] != LACP_SUBTYPE:
        return None
    actor_system_id = ":".join(f"{b:02x}" for b in payload[_ACTOR_TLV + 4 : _ACTOR_TLV + 10])
    actor_key = struct.unpack(">H", payload[_ACTOR_TLV + 10 : _ACTOR_TLV + 12])[0]
    actor_port = struct.unpack(">H", payload[_ACTOR_TLV + 14 : _ACTOR_TLV + 16])[0]
    actor_state = payload[_ACTOR_TLV + 16]
    partner_system_id = ":".join(f"{b:02x}" for b in payload[_PARTNER_TLV + 4 : _PARTNER_TLV + 10])
    partner_key = struct.unpack(">H", payload[_PARTNER_TLV + 10 : _PARTNER_TLV + 12])[0]
    partner_state = payload[_PARTNER_TLV + 16]
    return {
        "actor_system_id": actor_system_id,
        "actor_port_key": actor_key,
        "actor_port": actor_port,
        "actor_state": _decode_state(actor_state),
        "partner_system_id": partner_system_id,
        "partner_port_key": partner_key,
        "partner_state": _decode_state(partner_state),
    }


def parse_lacp_pcap(data):
    """Extract decoded LACP PDUs from raw pcap capture bytes."""
    if len(data) < 24:
        return []
    magic = data[:4]
    if magic in (b"\xd4\xc3\xb2\xa1", b"\x4d\x3c\xb2\xa1"):
        endian = "<"
    elif magic in (b"\xa1\xb2\xc3\xd4", b"\xa1\xb2\x3c\x4d"):
        endian = ">"
    else:
        return []
    pdus = []
    offset = 24
    while offset + 16 <= len(data):
        incl_len = struct.unpack(endian + "I", data[offset + 8 : offset + 12])[0]
        frame = data[offset + 16 : offset + 16 + incl_len]
        offset += 16 + incl_len
        if len(frame) < 14:
            continue
        ethertype = struct.unpack(">H", frame[12:14])[0]
        payload = frame[14:]
        if ethertype == ETHERTYPE_VLAN and len(payload) >= 4:
            ethertype = struct.unpack(">H", payload[2:4])[0]
            payload = payload[4:]
        if ethertype != ETHERTYPE_LACP:
            continue
        pdu = _parse_lacpdu(payload)
        if pdu is not None:
            pdus.append(pdu)
    return pdus


def _start_capture(iface, cancellation):
    # -Q in restricts capture to inbound frames: tcpdump otherwise also sees
    # the host bond's own outgoing LACPDUs, which would mask a static switch
    # (the requirement is to detect what the switch advertises, so only
    # inbound PDUs count).
    proc = subprocess.Popen(
        [
            "tcpdump",
            "-i",
            iface,
            "-Q",
            "in",
            "ether",
            "proto",
            "0x8809",
            "-c",
            "10",
            "-w",
            "-",
            "--immediate-mode",
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
    )
    cancellation.register(proc)
    return proc


def _stop_capture(proc):
    if proc.poll() is None:
        proc.terminate()
    try:
        data, _ = proc.communicate(timeout=5)
    except subprocess.TimeoutExpired:
        proc.kill()
        data, _ = proc.communicate()
    return parse_lacp_pcap(data or b"")


def _finding(ftype, hint, details):
    return {
        "type": ftype,
        "classification": "definitive",
        "scope": "node",
        "hint": hint,
        "details": details,
    }


def _flush_unattempted(section, bonds, attempted, reason):
    """Record derived-but-unattempted bonds when interrupted."""
    for bond in bonds:
        if bond["name"] not in attempted:
            section["bonds"].append({"bond": bond["name"], "observation_status": reason})


def run(topology, node, section, cancellation):
    """Capture LACP per bond member and verify bond mode and cabling."""
    bonds = _enumerate_bonds()
    if cancellation.is_set():
        _flush_unattempted(section, bonds, set(), cancellation.reason)
        return
    if not bonds:
        section["validator_status"] = "complete"
        return

    captures = {}
    for bond in bonds:
        for member in bond["members"]:
            captures[(bond["name"], member)] = _start_capture(member, cancellation)

    # Hold every concurrent capture open for one window; an early return
    # means cancellation fired (timeout or SIGTERM).
    interrupted = cancellation.wait(CAPTURE_WINDOW_SECONDS)
    pdus_by = {key: _stop_capture(proc) for key, proc in captures.items()}
    if interrupted:
        _flush_unattempted(section, bonds, set(), cancellation.reason)
        return

    for bond in bonds:
        is_lacp = "802.3ad" in bond["mode"]
        members_info = []
        actor_ids = set()
        any_pdu = False
        for member in bond["members"]:
            pdus = pdus_by[(bond["name"], member)]
            if pdus:
                any_pdu = True
                actor_ids.update(p["actor_system_id"] for p in pdus)
            members_info.append({"interface": member, "lacp_advertised": bool(pdus), "pdus": pdus})
        bond_entry = {
            "bond": bond["name"],
            "mode": bond["mode"],
            "lacp_active": bond["lacp_active"],
            "members": members_info,
            "bond_mode": "pass",
            "bond_cabling": "pass",
        }

        if is_lacp and not any_pdu:
            bond_entry["bond_mode"] = "fail"
            if not bond["lacp_active"]:
                hint = (
                    "No LACP PDUs received and host bond is LACP-passive (lacp_active off); "
                    "the switch port is either static or also passive, so neither end "
                    "initiates negotiation. Set lacp_active on for the host bond or verify "
                    "the switch port-channel mode."
                )
            else:
                hint = "Switch port may be configured for static bonding; verify switch port-channel mode"
            section["findings"].append(
                _finding("bond-mode-mismatch", hint, {"bond": bond["name"], "mode": bond["mode"]})
            )
        elif not is_lacp and any_pdu:
            bond_entry["bond_mode"] = "fail"
            section["findings"].append(
                _finding(
                    "bond-mode-mismatch",
                    "Host bond mode is static but switch is sending LACP; update host bond "
                    "configuration to mode=802.3ad",
                    {"bond": bond["name"], "mode": bond["mode"]},
                )
            )

        if len(actor_ids) > 1:
            bond_entry["bond_cabling"] = "fail"
            section["findings"].append(
                _finding(
                    "asymmetric-bond-cable",
                    f"Bond members are connected to different switches; check physical "
                    f"cabling for {bond['name']}",
                    {"bond": bond["name"], "switch_system_ids": sorted(actor_ids)},
                )
            )

        section["bonds"].append(bond_entry)

    section["validator_status"] = "complete"
