"""VLAN neighbor validator: ARP/ICMP verification of expected L2 peer sets.

Derives expected in-scope, expected-but-out-of-scope, and known-forbidden
peer sets from the topology, probes expected peers with targeted arping and
ICMP, and detects unexpected or forbidden neighbors via passive ARP capture.
Output conforms to the shared probe-output schema (see schemas.py).

Contract with the probe runner: mutate ``section`` in place, register
subprocesses with ``cancellation``, check ``cancellation.is_set()`` between
probe iterations, and set a terminal ``validator_status`` only on normal
completion (the runner assigns timeout/cancelled when interrupted).
"""

import os
import re
import select
import socket
import struct
import subprocess
import time

RULE_BMC_OAM = "bmc-oam-restricted"

# The passive ARP capture phase is capped at 30 seconds. The window is held
# open for the full cap even when the targeted arpings finish early: a node
# with no expected peers must still observe stranger ARP traffic (design
# budget D14 already reserves the 30s; the wrong-vlan fault detection on
# otherwise-idle nodes relies on it).
CAPTURE_CAP_SECONDS = 30
CAPTURE_WINDOW_SECONDS = 30

ETHERTYPE_ARP = 0x0806
ETHERTYPE_VLAN = 0x8100


def _iface_names_by_mac():
    """Map MAC -> local interface name for active non-loopback interfaces."""
    result = subprocess.run(
        ["ip", "-o", "link", "show", "up"], check=True, capture_output=True, text=True
    )
    names = {}
    for line in result.stdout.splitlines():
        match = re.match(r"\d+:\s+([^:@]+)[^\\]*\\\s+link/ether\s+([0-9A-Fa-f:]{17})", line)
        if match:
            names[match.group(2).lower()] = match.group(1).strip()
    return names


def _interface_macs(interface):
    macs = {interface["mac"].lower()}
    for member in interface.get("bond_members", []):
        macs.add(member["mac"].lower())
    return macs


def derive_peer_sets(topology, node):
    """Return one probe plan per node interface with derived peer sets.

    Each plan carries ``expected`` (in-scope peers to probe actively),
    ``out_of_scope`` (MAC -> peer info recognized as skipped observations),
    and ``forbidden`` (MAC -> peer info reported as definitive failures).
    """
    rules = topology.get("reachability_model", {}).get("rules", {})
    bmc_params = rules.get(RULE_BMC_OAM, {}).get("parameters", {})
    allowed_peer_roles = set(bmc_params.get("allowed_peer_roles", ["rack-controller"]))
    same_rack_only = bmc_params.get("same_rack_only", True)

    def adjacency_allowed(a, b):
        for this, other in ((a, b), (b, a)):
            if this["role"] != "bmc-oam":
                continue
            if other["role"] not in allowed_peer_roles:
                return False
            if same_rack_only and other["rack"] != this["rack"]:
                return False
        return True

    plans = []
    for interface in node["interfaces"]:
        plan = {
            "interface": interface["name"],
            "mac": interface["mac"].lower(),
            "own_macs": _interface_macs(interface),
            "gateway": interface.get("gateway_ip"),
            "expected": [],
            "out_of_scope": {},
            "forbidden": {},
        }
        for machine in topology["machines"]:
            if machine["system_id"] == node["system_id"]:
                continue
            for peer_iface in machine["interfaces"]:
                if peer_iface.get("fabric") != interface.get("fabric") or peer_iface.get(
                    "vlan_tag"
                ) != interface.get("vlan_tag"):
                    continue
                info = {
                    "system_id": machine["system_id"],
                    "hostname": machine["hostname"],
                    "ip": peer_iface.get("ip"),
                    "macs": _interface_macs(peer_iface),
                }
                if not adjacency_allowed(node, machine):
                    for mac in info["macs"]:
                        plan["forbidden"][mac] = info
                elif machine["in_scope"] and info["ip"]:
                    plan["expected"].append(info)
                else:
                    for mac in info["macs"]:
                        plan["out_of_scope"][mac] = info
        plans.append(plan)
    return plans


def parse_arp_pcap(data):
    """Extract (sender_mac, sender_ip) pairs from raw pcap ARP capture bytes."""
    if len(data) < 24:
        return []
    magic = data[:4]
    if magic in (b"\xd4\xc3\xb2\xa1", b"\x4d\x3c\xb2\xa1"):
        endian = "<"
    elif magic in (b"\xa1\xb2\xc3\xd4", b"\xa1\xb2\x3c\x4d"):
        endian = ">"
    else:
        return []
    pairs = []
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
        if ethertype != ETHERTYPE_ARP or len(payload) < 28:
            continue
        sender_mac = ":".join(f"{b:02x}" for b in payload[8:14])
        sender_ip = socket.inet_ntoa(payload[14:18])
        pairs.append((sender_mac, sender_ip))
    return pairs


def _start_capture(iface, cancellation):
    proc = subprocess.Popen(
        ["tcpdump", "-i", iface, "arp", "-c", "50", "-w", "-", "--immediate-mode"],
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
    return parse_arp_pcap(data or b"")


def _arping(iface, ip, cancellation):
    """One targeted ARP probe bound to the interface; True when answered."""
    proc = subprocess.Popen(
        ["arping", "-I", iface, "-c", "1", "-w", "2", ip],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    cancellation.register(proc)
    try:
        return proc.wait(timeout=10) == 0
    except subprocess.TimeoutExpired:
        proc.kill()
        return False


def _ping_once(ip, cancellation):
    proc = subprocess.Popen(
        ["ping", "-c", "1", "-W", "2", ip],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    cancellation.register(proc)
    try:
        return proc.wait(timeout=10) == 0
    except subprocess.TimeoutExpired:
        proc.kill()
        return False


def _icmp_checksum(data):
    if len(data) % 2:
        data += b"\x00"
    total = sum(struct.unpack(f">{len(data) // 2}H", data))
    total = (total >> 16) + (total & 0xFFFF)
    total += total >> 16
    return ~total & 0xFFFF


def _raw_icmp_probe(ip, count=3, timeout_s=2.0):
    """ICMP echo via raw socket; returns (loss_pct, rtt_ms dict or None).

    Raises PermissionError when raw sockets are unavailable; the caller
    falls back to the system ping binary.
    """
    ident = os.getpid() & 0xFFFF
    rtts = []
    with socket.socket(socket.AF_INET, socket.SOCK_RAW, socket.IPPROTO_ICMP) as sock:
        sock.setblocking(False)
        for seq in range(count):
            header = struct.pack(">BBHHH", 8, 0, 0, ident, seq)
            packet = struct.pack(">BBHHH", 8, 0, _icmp_checksum(header), ident, seq)
            sent = time.monotonic()
            sock.sendto(packet, (ip, 0))
            deadline = sent + timeout_s
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                readable, _, _ = select.select([sock], [], [], remaining)
                if not readable:
                    break
                data, addr = sock.recvfrom(2048)
                icmp = data[20:28]  # skip the IP header
                if len(icmp) < 8:
                    continue
                rtype, _code, _cksum, rident, rseq = struct.unpack(">BBHHH", icmp)
                if rtype == 0 and rident == ident and rseq == seq and addr[0] == ip:
                    rtts.append((time.monotonic() - sent) * 1000)
                    break
    loss_pct = round(100 * (count - len(rtts)) / count)
    if not rtts:
        return loss_pct, None
    rtt = {
        "min": round(min(rtts), 3),
        "avg": round(sum(rtts) / len(rtts), 3),
        "max": round(max(rtts), 3),
    }
    return loss_pct, rtt


def _ping_subprocess(ip, cancellation):
    """System ping fallback; returns (loss_pct, rtt_ms dict or None)."""
    proc = subprocess.Popen(
        ["ping", "-c", "3", "-W", "2", ip],
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        text=True,
    )
    cancellation.register(proc)
    try:
        out, _ = proc.communicate(timeout=20)
    except subprocess.TimeoutExpired:
        proc.kill()
        out, _ = proc.communicate()
    loss_match = re.search(r"(\d+(?:\.\d+)?)% packet loss", out or "")
    loss_pct = round(float(loss_match.group(1))) if loss_match else 100
    rtt_match = re.search(r"rtt [^=]*= ([\d.]+)/([\d.]+)/([\d.]+)", out or "")
    rtt = None
    if rtt_match:
        rtt = {
            "min": float(rtt_match.group(1)),
            "avg": float(rtt_match.group(2)),
            "max": float(rtt_match.group(3)),
        }
    return loss_pct, rtt


def _icmp_probe(ip, cancellation):
    try:
        return _raw_icmp_probe(ip)
    except PermissionError:
        return _ping_subprocess(ip, cancellation)


def _finding(ftype, hint, details):
    return {
        "type": ftype,
        "classification": "definitive",
        "scope": "interface",
        "hint": hint,
        "details": details,
    }


def _flush_unattempted(section, plans, attempted, reason):
    """Record derived-but-unattempted expected peers when interrupted."""
    for plan in plans:
        for peer in plan["expected"]:
            key = (plan["interface"], peer["system_id"])
            if key not in attempted:
                section["observations"].append(
                    {
                        "type": "expected-peer-unattempted",
                        "interface": plan["interface"],
                        "peer_system_id": peer["system_id"],
                        "observation_status": reason,
                    }
                )


def run(topology, node, section, cancellation):
    """Probe expected peers and detect unexpected neighbors on each interface."""
    plans = derive_peer_sets(topology, node)
    local_names = _iface_names_by_mac()
    for plan in plans:
        plan["local_iface"] = local_names.get(plan["mac"], plan["interface"])

    attempted = set()
    if cancellation.is_set():
        _flush_unattempted(section, plans, attempted, cancellation.reason)
        return

    # Phase A: passive ARP capture on every interface for the duration of
    # the targeted arping probes (capped at CAPTURE_CAP_SECONDS).
    captures = [(plan, _start_capture(plan["local_iface"], cancellation)) for plan in plans]
    phase_start = time.monotonic()
    arp_observed = {}
    interrupted = False
    for plan in plans:
        for peer in plan["expected"]:
            if cancellation.is_set() or time.monotonic() - phase_start > CAPTURE_CAP_SECONDS:
                interrupted = cancellation.is_set()
                break
            attempted.add((plan["interface"], peer["system_id"]))
            arp_observed[(plan["interface"], peer["system_id"])] = _arping(
                plan["local_iface"], peer["ip"], cancellation
            )
        if interrupted:
            break
    if not interrupted:
        remaining = CAPTURE_WINDOW_SECONDS - (time.monotonic() - phase_start)
        if remaining > 0:
            cancellation.wait(remaining)
            interrupted = cancellation.is_set()
    observed = {plan["interface"]: _stop_capture(proc) for plan, proc in captures}
    if interrupted:
        _flush_unattempted(section, plans, attempted, cancellation.reason)
        return

    # Phase B: classify every MAC seen in passive capture.
    for plan in plans:
        expected_macs = set().union(*(p["macs"] for p in plan["expected"]), set())
        seen = {}
        for mac, ip in observed.get(plan["interface"], []):
            seen.setdefault(mac, ip)
        for mac, ip in seen.items():
            if mac in plan["own_macs"] or mac in expected_macs:
                continue
            if mac in plan["forbidden"]:
                peer = plan["forbidden"][mac]
                section["findings"].append(
                    _finding(
                        "forbidden-l2-neighbor",
                        f"Forbidden L2 neighbor {peer['hostname']} (MAC {mac}) on interface "
                        f"{plan['interface']}; this adjacency is restricted by the "
                        "bmc-oam-restricted reachability rule",
                        {
                            "interface": plan["interface"],
                            "mac": mac,
                            "ip": ip,
                            "peer_system_id": peer["system_id"],
                        },
                    )
                )
            elif mac in plan["out_of_scope"]:
                peer = plan["out_of_scope"][mac]
                section["observations"].append(
                    {
                        "type": "known-out-of-scope-peer-observed",
                        "interface": plan["interface"],
                        "peer_system_id": peer["system_id"],
                        "peer_mac": mac,
                        "skipped": True,
                    }
                )
            elif plan.get("gateway") and ip == plan["gateway"]:
                # The data-fabric subnet gateway (the ToR/FRR router since
                # stage 7) is expected infrastructure, not an unexpected host.
                section["observations"].append(
                    {
                        "type": "infrastructure-gateway-observed",
                        "interface": plan["interface"],
                        "ip": ip,
                        "mac": mac,
                        "skipped": True,
                    }
                )
            else:
                section["findings"].append(
                    _finding(
                        "unexpected-l2-neighbor",
                        f"Unexpected L2 neighbor MAC {mac} (IP {ip}) on interface "
                        f"{plan['interface']}; this device is not in the expected topology "
                        "for this VLAN",
                        {"interface": plan["interface"], "mac": mac, "ip": ip},
                    )
                )
                if not cancellation.is_set() and _ping_once(ip, cancellation):
                    section["findings"].append(
                        _finding(
                            "unexpected-reachability",
                            f"Unexpected node (MAC {mac}, IP {ip}) is reachable on interface "
                            f"{plan['interface']}; node may be on incorrect VLAN",
                            {"interface": plan["interface"], "mac": mac, "ip": ip},
                        )
                    )

    # Phase C: ICMP reachability for every expected in-scope peer.
    for plan in plans:
        for peer in plan["expected"]:
            if cancellation.is_set():
                _flush_unattempted(section, plans, attempted, cancellation.reason)
                return
            key = (plan["interface"], peer["system_id"])
            attempted.add(key)
            loss_pct, rtt = _icmp_probe(peer["ip"], cancellation)
            reachable = loss_pct < 100
            arped = arp_observed.get(key, False)
            if not arped:
                section["findings"].append(
                    _finding(
                        "missing-l2-neighbor",
                        f"Expected L2 neighbor {peer['hostname']} ({peer['ip']}) did not "
                        f"respond to ARP on interface {plan['interface']}; verify VLAN "
                        "assignment and switch port config",
                        {
                            "interface": plan["interface"],
                            "peer_system_id": peer["system_id"],
                            "peer_ip": peer["ip"],
                        },
                    )
                )
            if not reachable:
                section["findings"].append(
                    _finding(
                        "icmp-unreachable",
                        f"Node {peer['hostname']} is not reachable from {node['hostname']} "
                        f"on interface {plan['interface']}; verify VLAN assignment and "
                        "switch port config",
                        {
                            "interface": plan["interface"],
                            "peer_system_id": peer["system_id"],
                            "peer_ip": peer["ip"],
                        },
                    )
                )
            if arped or reachable:
                section["observations"].append(
                    {
                        "type": "expected-peer-observed",
                        "interface": plan["interface"],
                        "peer_system_id": peer["system_id"],
                        "peer_ip": peer["ip"],
                        "peer_mac": sorted(peer["macs"])[0],
                        "arp_observed": arped,
                        "icmp_reachable": reachable,
                        "rtt_ms": rtt,
                        "loss_pct": loss_pct,
                    }
                )

    section["validator_status"] = "complete"
