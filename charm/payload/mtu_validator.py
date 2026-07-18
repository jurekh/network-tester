"""MTU validator: representative-sampled cross-rack path MTU observation.

From each rack's source representative (see representatives.py), probes one
representative data node per remote rack with oversized DF-bit ICMP and
records observed rack-pair path MTU as informational cross_rack_mtu records.
Output conforms to the shared probe-output schema (see schemas.py).

Contract with the probe runner: mutate ``section`` in place, register
subprocesses with ``cancellation``, check ``cancellation.is_set()`` between
per-rack probe iterations, and set a terminal ``validator_status`` only on
normal completion (the runner assigns timeout/cancelled when interrupted).
"""

import re
import subprocess

import representatives

# Two initial ICMP payload sizes: standard Ethernet (1472 + 28 = 1500) and
# jumbo (8972 + 28 = 9000). The observed path MTU is the payload size + 28.
HEADER_BYTES = 28
STD_PAYLOAD = 1472
JUMBO_PAYLOAD = 8972
BINARY_SEARCH_ITERATIONS = 5

INCONCLUSIVE_NOTE = "ICMP may be filtered on this path; manual MTU verification required"


def _data_ip(machine):
    """Data-fabric IP of a machine, or None (management/OAM IPs are ignored)."""
    for iface in machine.get("interfaces", []):
        if iface.get("fabric_class") == "data" and iface.get("ip"):
            return iface["ip"]
    return None


def _ping_df(ip, size, cancellation):
    """One DF-bit ICMP probe; returns (success, fragmentation_needed_mtu|None)."""
    proc = subprocess.Popen(
        ["ping", "-M", "do", "-s", str(size), "-c", "1", "-W", "2", ip],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    cancellation.register(proc)
    try:
        out, _ = proc.communicate(timeout=10)
    except subprocess.TimeoutExpired:
        proc.kill()
        out, _ = proc.communicate()
    success = proc.returncode == 0
    # iputils reports the next-hop MTU on a fragmentation-needed error, e.g.
    # "Frag needed and DF set (mtu = 1500)" or "message too long, mtu=1500".
    frag = re.search(r"mtu\s*=?\s*(\d+)", out or "")
    frag_mtu = int(frag.group(1)) if frag else None
    return success, frag_mtu


def _measure_mtu(ip, cancellation):
    """Return (observed_path_mtu_bytes|None, observation_status) for one path.

    The standard-size probe (1500 total) is sent first: it fits a standard
    1500 interface, so it actually leaves the host and any fragmentation it
    triggers reflects a real downstream bottleneck (e.g. a 1400 inter-rack
    link). A jumbo-first probe cannot see a sub-1500 path MTU on a 1500
    interface, because the oversized packet fragments at the local interface
    and the kernel reports the local 1500 MTU, masking the smaller path.
    """
    lo_ok, lo_frag = _ping_df(ip, STD_PAYLOAD, cancellation)
    if not lo_ok:
        if lo_frag:
            return lo_frag, "success"
        return None, "inconclusive"

    # Standard size passes; probe jumbo to detect a larger (jumbo) path MTU.
    hi_ok, hi_frag = _ping_df(ip, JUMBO_PAYLOAD, cancellation)
    if hi_ok:
        return JUMBO_PAYLOAD + HEADER_BYTES, "success"
    if hi_frag and hi_frag > STD_PAYLOAD + HEADER_BYTES:
        # The probe left a jumbo-capable interface and fragmented at a real
        # downstream hop above 1500: that reported MTU is the path MTU.
        return hi_frag, "success"
    if hi_frag:
        # The jumbo probe fragmented at the local interface (report <= 1500):
        # the path MTU is the standard size that already passed.
        return STD_PAYLOAD + HEADER_BYTES, "success"

    # Jumbo failed without a fragmentation report (ICMP frag-needed filtered):
    # binary search the payload range for the largest size that still passes;
    # the path MTU is that size + 28.
    best = STD_PAYLOAD
    low, high = STD_PAYLOAD, JUMBO_PAYLOAD
    for _ in range(BINARY_SEARCH_ITERATIONS):
        mid = (low + high) // 2
        if mid <= low:
            break
        ok, frag = _ping_df(ip, mid, cancellation)
        if ok:
            best = mid
            low = mid
        elif frag and frag > STD_PAYLOAD + HEADER_BYTES:
            return frag, "success"
        else:
            high = mid
    return best + HEADER_BYTES, "success"


def _record(source_rack, source_node, target_rack, target_node, mtu_bytes, status, note=None):
    record = {
        "source_rack": source_rack,
        "source_node": source_node,
        "target_rack": target_rack,
        "target_node": target_node,
        "observed_path_mtu_bytes": mtu_bytes,
        "observation_status": status,
    }
    if note:
        record["note"] = note
    return record


def run(topology, node, section, cancellation):
    """Probe path MTU from the rack representative to each remote rack."""
    params = representatives.cross_rack_rule(topology)
    machines = topology.get("machines", [])
    local_rack = node["rack"]
    source = representatives.source_representative(machines, local_rack, params)
    if node.get("role") != params.get("applicable_role") or node["system_id"] != source:
        section["validator_status"] = "skipped"
        section["skip_reason"] = "not-rack-representative"
        return

    remotes = representatives.remote_racks(machines, local_rack, params)
    if not remotes:
        section["validator_status"] = "skipped"
        section["skip_reason"] = "no cross-rack data peers"
        return

    by_sid = {m["system_id"]: m for m in machines}
    source_node = node["system_id"]
    for i, remote in enumerate(remotes):
        target_sid = representatives.target_representative(machines, remote, params)
        if cancellation.is_set():
            for rack in remotes[i:]:
                tsid = representatives.target_representative(machines, rack, params)
                section["cross_rack_mtu"].append(
                    _record(local_rack, source_node, rack, tsid, None, cancellation.reason)
                )
            return
        target_ip = _data_ip(by_sid.get(target_sid, {}))
        mtu_bytes, status = _measure_mtu(target_ip, cancellation)
        note = INCONCLUSIVE_NOTE if status == "inconclusive" else None
        section["cross_rack_mtu"].append(
            _record(local_rack, source_node, remote, target_sid, mtu_bytes, status, note)
        )

    section["validator_status"] = "complete"
