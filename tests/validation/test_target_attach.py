"""Target attach: target discoverable, login, expected active paths, device."""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.validation

MOUNT_PATH = "/mnt/labbench/lun0"


def test_target_discoverable(driver, lab):
    target = lab.targets[0]
    discovery = driver.discover_targets(target.portals[0])
    assert discovery.get("ok", True), discovery
    assert discovery.get("targets") or discovery.get("stdout"), discovery


def test_login_and_active_paths(driver, lab, thresholds):
    target = lab.targets[0]
    driver.login(target.iqn, target.portals)
    paths = driver.paths(target.iqn)
    assert paths.get("active_paths", 0) >= thresholds["attach"]["expected_paths"], paths


def test_device_present_after_mount(driver, lab, thresholds):
    target = lab.targets[0]
    driver.login(target.iqn, target.portals)
    mount = driver.mount(thresholds["attach"]["lun"], MOUNT_PATH)
    assert mount.get("device"), mount
