"""Provisioning idempotence test.

Brings up the Docker lab (or reuses a running one), runs the Ansible playbook
twice, and asserts the second PLAY RECAP reports ``changed=0`` for every host.

Skipped with a clear reason when Docker (or ansible-playbook) is unavailable.
"""

from __future__ import annotations

import shutil
import subprocess

import pytest

from labbench import paths
from labbench.topology import load_lab

pytestmark = pytest.mark.integration


def _docker_reason() -> str | None:
    if not shutil.which("docker"):
        return "docker CLI not found; skipping lab provisioning idempotence test"
    try:
        proc = subprocess.run(
            ["docker", "info", "--format", "{{.ServerVersion}}"],
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
    except Exception as exc:  # noqa: BLE001
        return f"docker daemon not reachable ({exc}); skipping"
    if proc.returncode != 0:
        return "docker daemon not reachable; skipping lab provisioning idempotence test"
    return None


def _ansible_reason() -> str | None:
    try:
        from labbench.ansible_runner import _playbook_exe

        _playbook_exe()
    except Exception as exc:  # noqa: BLE001
        return f"ansible-playbook unavailable ({exc}); skipping"
    return None


def test_provision_is_idempotent() -> None:
    for reason in (_docker_reason(), _ansible_reason()):
        if reason:
            pytest.skip(reason)

    from labbench import lab as lab_mod
    from labbench.ansible_runner import run_provision

    lab = load_lab(paths.default_lab_path())
    started_here = False
    if lab_mod.status(lab)["state"] != "running":
        lab_mod.up(lab, timeout=300)
        started_here = True

    try:
        first = run_provision(lab)
        assert first["playbook"]["failed"] == 0, (
            f"first provision run failed: {first['playbook']}"
        )
        second = run_provision(lab)
        assert second["playbook"]["failed"] == 0, (
            f"second provision run failed: {second['playbook']}"
        )
        assert second["playbook"]["changed"] == 0, (
            "provisioning is not idempotent; second run reported "
            f"changed={second['playbook']['changed']} "
            f"per-host={[(h['host'], h['changed']) for h in second['hosts']]}"
        )
    finally:
        if started_here:
            lab_mod.down(lab, volumes=True)
