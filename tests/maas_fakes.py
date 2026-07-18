"""Builders for fake MAAS API payloads used by the topology fetcher tests."""

RC_SYSTEM_ID = "rcsys1"

_iface_id = [0]


def vlan(fabric, vid=0, primary_rack=RC_SYSTEM_ID):
    return {
        "id": 5000 + vid,
        "name": "untagged" if vid == 0 else str(vid),
        "vid": vid,
        "fabric": fabric,
        "fabric_id": 1,
        "primary_rack": primary_rack,
    }


def link(cidr, ip="", gateway=None, mode="auto"):
    return {
        "id": 100,
        "mode": mode,
        "ip_address": ip,
        "subnet": {
            "id": 1,
            "cidr": cidr,
            "gateway_ip": gateway or cidr.rsplit(".", 1)[0] + ".1",
        },
    }


def iface(name, mac, vlan=None, type="physical", links=(), params=None, parents=()):
    _iface_id[0] += 1
    return {
        "id": _iface_id[0],
        "name": name,
        "type": type,
        "mac_address": mac,
        "vlan": vlan,
        "links": list(links),
        "params": params if params is not None else "",
        "parents": list(parents),
        "children": [],
    }


def machine(system_id, hostname, ifaces, status="Ready"):
    return {
        "system_id": system_id,
        "hostname": hostname,
        "status_name": status,
        "interface_set": ifaces,
        "boot_interface": ifaces[0] if ifaces else None,
    }


def rack_controller(system_id=RC_SYSTEM_ID, hostname="rack1-ctl", ifaces=None):
    if ifaces is None:
        ifaces = [
            iface(
                "eth0",
                "52:54:00:00:00:01",
                vlan("fabric-mgmt", 0, primary_rack=system_id),
                links=[link("10.10.1.0/24")],
            )
        ]
    return {
        "system_id": system_id,
        "hostname": hostname,
        "status_name": "Deployed",
        "interface_set": ifaces,
        "boot_interface": ifaces[0] if ifaces else None,
    }


def mgmt_machine(system_id, hostname, status="Ready"):
    """Machine with a single management-fabric interface."""
    return machine(
        system_id,
        hostname,
        [
            iface(
                "eth0",
                f"52:54:00:bb:00:{system_id[-2:]}",
                vlan("fabric-mgmt", 0),
                links=[link("10.10.1.0/24")],
            )
        ],
        status=status,
    )


def data_machine(system_id, hostname, status="Ready", vid=100, cidr="10.20.1.0/24"):
    """Machine with a bonded data-fabric interface."""
    eno1 = iface("eno1", f"52:54:00:dd:01:{system_id[-2:]}", None)
    eno2 = iface("eno2", f"52:54:00:dd:02:{system_id[-2:]}", None)
    bond = iface(
        "bond0",
        eno1["mac_address"],
        vlan("fabric-data", vid),
        type="bond",
        links=[link(cidr)],
        params={"bond_mode": "802.3ad"},
        parents=["eno1", "eno2"],
    )
    return machine(system_id, hostname, [bond, eno1, eno2], status=status)


def mixed_machine(system_id, hostname, status="Ready"):
    """Machine with one management and one data interface."""
    mgmt = iface(
        "eth-mgmt",
        f"52:54:00:cc:00:{system_id[-2:]}",
        vlan("fabric-mgmt", 0),
        links=[link("10.10.1.0/24")],
    )
    data = iface(
        "eth-data",
        f"52:54:00:cc:01:{system_id[-2:]}",
        vlan("fabric-data", 100),
        links=[link("10.20.1.0/24")],
    )
    return machine(system_id, hostname, [mgmt, data], status=status)


class FakeClient:
    """MaasClient stand-in returning canned payloads per API path."""

    def __init__(self, rack_controllers, machines):
        self.payloads = {
            "rackcontrollers/": rack_controllers,
            "machines/": machines,
        }

    def get(self, path):
        return self.payloads[path]
