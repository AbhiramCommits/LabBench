"""Initiator configuration: iqn present, multipath/MPIO enabled, disk timeout."""

from __future__ import annotations

import json

import pytest

pytestmark = pytest.mark.validation


def test_initiator_iqn_present(driver):
    info = driver.os_info()
    iqn = info.get("initiator", "")
    assert iqn.startswith("iqn."), f"missing/!valid initiator iqn: {iqn!r}"


def test_multipath_or_mpio_enabled(driver):
    topology = driver.disk_topology()
    mp = topology.get("multipath", {}) or {}
    assert mp.get("enabled") or mp.get("mpio_enabled"), f"multipath/MPIO not enabled: {mp}"


def test_disk_timeout_configured(driver, thresholds):
    expected = thresholds["drift"]["disk_timeout_s"]
    if driver.os_family == "windows":
        res = driver.run("Get-Content /var/labbench/win/tuning.json -Raw")
        data = json.loads(res.stdout.strip()) if res.stdout.strip() else {}
        assert (
            data.get("disk_timeout_s") == expected
        ), f"windows disk timeout {data.get('disk_timeout_s')} != {expected}"
    else:
        res = driver.run("cat /etc/multipath.conf")
        assert "no_path_retry" in res.stdout, "multipath.conf missing no_path_retry"
