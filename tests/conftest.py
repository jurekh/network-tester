import importlib.util
import json
import socket
import struct
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
FIXTURES = REPO_ROOT / "tests" / "fixtures"
PAYLOAD_DIR = REPO_ROOT / "charm" / "payload"

# Payload modules import each other by bare name (the payload runs as plain
# scripts on nodes); make them importable the same way in tests. The names
# do not clash with the cli package because cli modules are imported as
# cli.<name>.
sys.path.insert(0, str(PAYLOAD_DIR))

TOPOLOGY_FIXTURES = sorted(FIXTURES.glob("topology_*.json"))
PROBE_OUTPUT_FIXTURES = sorted(FIXTURES.glob("probe_output_*.json"))
REPORT_FIXTURES = sorted(FIXTURES.glob("report_*.json"))


def load_fixture(path):
    return json.loads(Path(path).read_text())


def load_module_from_path(path, name):
    """Import a module from an explicit file path under a unique name.

    Used to load the charm payload copies of the shared modules without
    clashing with the cli package imports of the same module names.
    """
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


# --- fake probe tools for the vlan neighbor validator -----------------------------

PING_OK = (
    "3 packets transmitted, 3 received, 0% packet loss, time 2002ms\n"
    "rtt min/avg/max/mdev = 0.211/0.330/0.490/0.114 ms\n"
)
PING_LOST = "3 packets transmitted, 0 received, 100% packet loss, time 2031ms\n"


def mac_bytes(mac):
    return bytes(int(part, 16) for part in mac.split(":"))


def arp_pcap(*entries):
    """Synthesize pcap bytes with one ARP request per (mac, ip) entry."""
    chunks = [struct.pack("<IHHiIII", 0xA1B2C3D4, 2, 4, 0, 0, 65535, 1)]
    for mac, ip in entries:
        eth = b"\xff" * 6 + mac_bytes(mac) + b"\x08\x06"
        arp = (
            struct.pack(">HHBBH", 1, 0x0800, 6, 4, 1)
            + mac_bytes(mac)
            + socket.inet_aton(ip)
            + b"\x00" * 6
            + socket.inet_aton("0.0.0.0")
        )
        frame = eth + arp
        chunks.append(struct.pack("<IIII", 0, 0, len(frame), len(frame)) + frame)
    return b"".join(chunks)


class FakeProc:
    def __init__(self, cmd, returncode=0, out=b""):
        self.cmd = cmd
        self.returncode = None
        self._rc = returncode
        self._out = out

    def poll(self):
        return self.returncode

    def wait(self, timeout=None):
        self.returncode = self._rc
        return self._rc

    def terminate(self):
        self.returncode = self._rc

    def kill(self):
        self.returncode = self._rc

    def communicate(self, timeout=None):
        self.returncode = self._rc
        empty = "" if isinstance(self._out, str) else b""
        return (self._out, empty)


@pytest.fixture
def fake_tools(monkeypatch):
    """Mock every subprocess the vlan validator spawns; returns the call log.

    Behavior is configured through the returned dict: arp maps peer IP to
    arping success, capture is the pcap returned by tcpdump, ping3/ping1 map
    target IP to ping stdout (for -c 3) or success bool (for -c 1).
    """
    import vlan_neighbor_validator as vlan

    state = {"calls": [], "arp": {}, "capture": arp_pcap(), "ping3": {}, "ping1": {}}

    def fake_popen(cmd, *args, **kwargs):
        state["calls"].append(list(cmd))
        tool = cmd[0]
        if tool == "tcpdump":
            return FakeProc(cmd, 0, state["capture"])
        if tool == "arping":
            return FakeProc(cmd, 0 if state["arp"].get(cmd[-1]) else 1)
        if tool == "ping":
            count = cmd[cmd.index("-c") + 1]
            ip = cmd[-1]
            if count == "1":
                return FakeProc(cmd, 0 if state["ping1"].get(ip) else 1)
            return FakeProc(cmd, 0, state["ping3"].get(ip, PING_LOST))
        raise AssertionError(f"unexpected tool: {cmd}")

    monkeypatch.setattr(vlan.subprocess, "Popen", fake_popen)
    monkeypatch.setattr(vlan, "_iface_names_by_mac", lambda: {})
    monkeypatch.setattr(vlan, "CAPTURE_WINDOW_SECONDS", 0)
    monkeypatch.setattr(
        vlan, "_raw_icmp_probe", lambda *a, **k: (_ for _ in ()).throw(PermissionError())
    )
    return state
