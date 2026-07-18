"""BGP inference: representative-sampled cross-rack reachability and traceroute.

From each rack's source representative (see representatives.py), probes the
representative and fallback data nodes per remote rack via ICMP and runs
traceroute on dual failure to infer where cross-rack traffic stops, emitting
rack-pair paths[] records. Output conforms to the shared probe-output schema
(see schemas.py).

Contract with the probe runner: mutate ``section`` in place, register
subprocesses with ``cancellation``, check ``cancellation.is_set()`` between
per-rack probe iterations, and set a terminal ``validator_status`` only on
normal completion (the runner assigns timeout/cancelled when interrupted).
"""

import ipaddress
import re
import subprocess

import representatives

# 30 hops at one query and a 2-second wait is 60s worst case; cap at 75s.
TRACEROUTE_CAP_SECONDS = 75
# Cross-rack reachability ICMP probe shape (`ping -c COUNT -W WAIT`). The
# timeout-budget test derives the per-rack BGP cost (representative + fallback
# probes plus the capped traceroute) from these constants.
ICMP_COUNT = 2
ICMP_WAIT_SECONDS = 2


def _data_iface(machine):
    for iface in machine.get("interfaces", []):
        if iface.get("fabric_class") == "data" and iface.get("ip"):
            return iface
    return {}


def _icmp_ok(ip, cancellation):
    """True when the target answers ICMP echo."""
    proc = subprocess.Popen(
        ["ping", "-c", str(ICMP_COUNT), "-W", str(ICMP_WAIT_SECONDS), ip],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    cancellation.register(proc)
    try:
        return proc.wait(timeout=10) == 0
    except subprocess.TimeoutExpired:
        proc.kill()
        return False


def _parse_traceroute(out):
    """Parse `traceroute -n` output into hop records (ip None for `*`)."""
    hops = []
    for line in (out or "").splitlines():
        match = re.match(r"\s*(\d+)\s+(.*)", line)
        if not match:
            continue
        hop = int(match.group(1))
        ip_match = re.search(r"(\d+\.\d+\.\d+\.\d+)", match.group(2))
        rtt_match = re.search(r"([\d.]+)\s*ms", match.group(2))
        hops.append(
            {
                "hop": hop,
                "ip": ip_match.group(1) if ip_match else None,
                "rtt_ms": float(rtt_match.group(1)) if rtt_match else None,
            }
        )
    return hops


def _traceroute(ip, cancellation):
    """Run traceroute to ip; returns (hops, truncated)."""
    proc = subprocess.Popen(
        ["traceroute", "-n", "-m", "30", "-q", "1", "-w", "2", ip],
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        text=True,
    )
    cancellation.register(proc)
    truncated = False
    try:
        out, _ = proc.communicate(timeout=TRACEROUTE_CAP_SECONDS)
    except subprocess.TimeoutExpired:
        proc.kill()
        out, _ = proc.communicate()
        truncated = True
    return _parse_traceroute(out), truncated


def _in_subnet(ip, cidr):
    if not ip or not cidr:
        return False
    try:
        return ipaddress.ip_address(ip) in ipaddress.ip_network(cidr, strict=False)
    except ValueError:
        return False


def _classify(hops, truncated, local_gateway, target_iface, source_rack, target_rack):
    """Build the rack-pair finding from a failed path's traceroute hops."""
    responding = [h for h in hops if h.get("ip")]
    details = {
        "rack_pair": [source_rack, target_rack],
        "traceroute_hops": hops,
        "traceroute_truncated": truncated,
    }
    if not responding:
        return _finding(
            "icmp-blocked",
            "inconclusive",
            f"All traceroute hops non-responding between {source_rack} and {target_rack}; "
            "ICMP may be rate-limited or filtered end-to-end. Manual verification required.",
            details,
        )
    last = responding[-1]["ip"]
    if last == local_gateway:
        hint = (
            f"Traffic from {source_rack} stops at ToR (IP {last}); BGP session between "
            f"{source_rack} ToR and upstream may be down. Verify BGP configuration and "
            "peering on the ToR switch."
        )
        return _finding("likely-bgp-failure", "inferred", hint, details)
    if _in_subnet(last, target_iface.get("subnet_cidr")):
        hint = (
            f"Cross-rack routing works but target node is unreachable within {target_rack}; "
            "check VLAN and host configuration on the target"
        )
        return _finding("intra-rack-routing", "inferred", hint, details)
    hint = (
        f"Traffic stops at intermediate hop {last}; investigate routing between {last} "
        f"and {target_rack}"
    )
    return _finding("routing-failure", "inferred", hint, details)


def _finding(ftype, confidence, hint, details):
    return {
        "type": ftype,
        "classification": confidence,
        "scope": "rack-pair",
        "diagnosis_confidence": confidence,
        "hint": hint,
        "details": details,
    }


def _path(source_rack, source_node, target_rack, rep_target, fallback_target):
    return {
        "source_rack": source_rack,
        "source_node": source_node,
        "target_rack": target_rack,
        "representative_target": rep_target,
        "fallback_target": fallback_target,
        "reachable": None,
        "target_role": None,
        "observation_status": "failure",
    }


def run(topology, node, section, cancellation):
    """Probe cross-rack reachability from the rack representative."""
    params = representatives.cross_rack_rule(topology)
    machines = topology.get("machines", [])
    local_rack = node["rack"]
    source = representatives.source_representative(machines, local_rack, params)
    if node.get("role") != params.get("applicable_role") or node["system_id"] != source:
        section["validator_status"] = "skipped"
        section["skip_reason"] = "not-rack-representative"
        return

    by_sid = {m["system_id"]: m for m in machines}
    local_gateway = _data_iface(node).get("gateway_ip")
    source_node = node["system_id"]
    remotes = representatives.remote_racks(machines, local_rack, params)
    for i, remote in enumerate(remotes):
        rep_sid = representatives.target_representative(machines, remote, params)
        fb_sid = representatives.fallback_target(machines, remote, params)
        if cancellation.is_set():
            for rack in remotes[i:]:
                rsid = representatives.target_representative(machines, rack, params)
                fsid = representatives.fallback_target(machines, rack, params)
                rec = _path(local_rack, source_node, rack, rsid, fsid)
                rec["observation_status"] = cancellation.reason
                section["paths"].append(rec)
            return

        record = _path(local_rack, source_node, remote, rep_sid, fb_sid)
        rep_iface = _data_iface(by_sid.get(rep_sid, {}))
        if _icmp_ok(rep_iface.get("ip"), cancellation):
            record.update(
                reachable=True, target_role="representative", observation_status="success"
            )
        elif fb_sid and _icmp_ok(_data_iface(by_sid.get(fb_sid, {})).get("ip"), cancellation):
            record.update(reachable=True, target_role="fallback", observation_status="success")
        else:
            hops, truncated = _traceroute(rep_iface.get("ip"), cancellation)
            record.update(reachable=False, target_role=None, observation_status="failure")
            record["finding"] = _classify(
                hops, truncated, local_gateway, rep_iface, local_rack, remote
            )
            record["traceroute_hops"] = hops
        section["paths"].append(record)

    section["validator_status"] = "complete"
