#!/usr/bin/env python3
"""LabBench Ansible dynamic inventory.

Reads a lab topology YAML (``labs/dev-lab.yml`` by default, override with the
``LABBENCH_LAB`` environment variable) and emits Ansible inventory JSON with
three groups: ``linux``, ``windows`` and ``storage``.

Because the Windows nodes are WinRM stubs backed by pwsh (not real Windows),
the ``windows`` group uses ``ansible_connection=winrm`` with the ``basic``
transport; the playbook uses ``raw`` PowerShell there (see roles). The
``storage`` group runs locally and drives the target through its management
API.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
SRC = REPO_ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

try:
    from labbench.topology import LabSchemaError, load_lab
except Exception as exc:  # pragma: no cover - surfaced as inventory error
    print(json.dumps({"_meta": {"hostvars": {}}, "all": {"hosts": []}}))
    print(f"labbench_inventory: cannot import labbench.topology: {exc}", file=sys.stderr)
    sys.exit(0)


def lab_path() -> Path:
    env = os.environ.get("LABBENCH_LAB")
    if env:
        return Path(env)
    return REPO_ROOT / "labs" / "dev-lab.yml"


def build_inventory() -> dict:
    lab = load_lab(lab_path())
    hostvars: dict[str, dict] = {}
    linux_hosts: list[str] = []
    windows_hosts: list[str] = []
    all_hosts: list[str] = []

    for host in lab.hosts:
        name = host.name
        all_hosts.append(name)
        initiator = host.tags.get("initiator", f"iqn.2026-01.labbench:{name}")
        common = {
            "ansible_host": host.address,
            "ansible_port": host.port,
            "ansible_user": host.user,
            "ansible_password": host.tags.get("password", "labbench"),
            "host_os_family": host.os_family,
            "labbench_roles": host.roles,
            "labbench_tags": host.tags,
            "labbench_initiator": initiator,
            "labbench_container": host.tags.get("container", name),
            "labbench_disk_timeout_s": host.tags.get("disk_timeout_s", 60),
        }
        if host.os_family == "linux":
            linux_hosts.append(name)
            common.update(
                {
                    "ansible_connection": "paramiko",
                    "ansible_python_interpreter": "/usr/bin/python3",
                    "ansible_ssh_common_args": (
                        "-o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null"
                    ),
                    "labbench_simulator_url": host.tags.get(
                        "simulator_url", "http://storage-target:8080"
                    ),
                }
            )
        else:
            windows_hosts.append(name)
            common.update(
                {
                    "ansible_connection": "winrm",
                    "ansible_shell_type": "powershell",
                    "ansible_winrm_transport": "basic",
                    "ansible_winrm_scheme": "http",
                    "ansible_winrm_server_cert_validation": "ignore",
                    "ansible_winrm_message_encryption": "never",
                    "ansible_winrm_operation_timeout_sec": 120,
                    "ansible_winrm_read_timeout_sec": 150,
                    "ansible_python_interpreter": "auto_silent",
                    "labbench_simulator_url": host.tags.get(
                        "simulator_url", "http://storage-target:8080"
                    ),
                }
            )
        hostvars[name] = common

    # Storage target group: provisioned locally through the management API.
    storage_host = lab.targets[0].name if lab.targets else "storage-target"
    hostvars[storage_host] = {
        "ansible_connection": "local",
        "host_os_family": "storage",
        "ansible_python_interpreter": sys.executable,
        "labbench_simulator_url": lab.simulator_url.replace("127.0.0.1", "127.0.0.1"),
        "labbench_target_iqn": lab.targets[0].iqn if lab.targets else "",
        "labbench_target_portals": lab.targets[0].portals if lab.targets else [],
        "labbench_target_luns": [
            {"id": l.id, "size_gb": l.size_gb, "backstore": l.backstore}
            for l in (lab.targets[0].luns if lab.targets else [])
        ],
    }

    return {
        "linux": {"hosts": linux_hosts, "vars": {}},
        "windows": {"hosts": windows_hosts, "vars": {}},
        "storage": {"hosts": [storage_host], "vars": {}},
        "labbench": {"hosts": all_hosts, "vars": {}},
        "_meta": {"hostvars": hostvars},
    }


def main() -> None:
    if "--host" in sys.argv:
        idx = sys.argv.index("--host")
        name = sys.argv[idx + 1] if idx + 1 < len(sys.argv) else ""
        inv = build_inventory()
        print(json.dumps(inv["_meta"]["hostvars"].get(name, {})))
        return
    try:
        print(json.dumps(build_inventory(), indent=2))
    except LabSchemaError as exc:
        print(f"labbench_inventory: bad lab schema: {exc}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
