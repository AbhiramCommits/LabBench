"""I/O integrity: write/read a large block, verify checksum, check throughput."""


from __future__ import annotations

import pytest

pytestmark = pytest.mark.validation

MOUNT_PATH = "/mnt/labbench/lun0"


def test_io_write_read_checksum(driver, lab, thresholds):
    target = lab.targets[0]
    driver.login(target.iqn, target.portals)
    driver.mount(thresholds["attach"]["lun"], MOUNT_PATH)
    result = driver.write_verify(MOUNT_PATH, thresholds["io"]["size_mb"])
    assert result.get("ok"), result
    assert result.get("checksum"), result
    if result.get("verify_checksum"):
        assert result["checksum"] == result["verify_checksum"], result


def test_io_throughput_above_floor(driver, lab, thresholds):
    target = lab.targets[0]
    driver.login(target.iqn, target.portals)
    driver.mount(thresholds["attach"]["lun"], MOUNT_PATH)
    result = driver.write_verify(MOUNT_PATH, thresholds["io"]["size_mb"])
    mbps = result.get("mbps")
    assert mbps is not None and mbps >= thresholds["io"]["min_mbps"], result
