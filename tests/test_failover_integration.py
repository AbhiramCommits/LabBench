"""Integration test: one real failover iteration through the lab drivers.

Requires a running Docker lab (brought up automatically) and the simulated
iSCSI path layer. Skipped with a clear reason when Docker is unavailable.
"""

from __future__ import annotations

import shutil
import subprocess

import pytest

from labbench import paths
from labbench.topology import load_lab
from labbench.workflows.failover import run_failover

pytestmark = pytest.mark.integration


def _docker_reason() -> str | None:
    if not shutil.which("docker"):
        return "docker CLI not found; skipping failover integration test"
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
        return "docker daemon not reachable; skipping failover integration test"
    return None


def test_single_real_failover_iteration() -> None:
    reason = _docker_reason()
    if reason:
        pytest.skip(reason)

    from labbench import lab as lab_mod

    lab = load_lab(paths.default_lab_path())
    started_here = False
    if lab_mod.status(lab)["state"] != "running":
        lab_mod.up(lab, timeout=300)
        started_here = True

    try:
        result = run_failover(lab, only={"linux-host-1"}, iterations=1)
        assert result["failed"] == 0, result["hosts"]
        host = result["hosts"][0]
        assert host["recovery_ms"]["count"] == 1
        assert host["recovery_ms"]["median"] is not None
        assert host["restore_ms"]["median"] is not None
        assert host["corrupted"] is False
    finally:
        if started_here:
            lab_mod.down(lab, volumes=True)
