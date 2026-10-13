"""OS baseline: host reachable, expected family/version range, time sync."""


from __future__ import annotations

import re
import time

import pytest

pytestmark = pytest.mark.validation


def _version_tuple(text: str) -> tuple[int, int] | None:
    match = re.search(r"(\d+)\.(\d+)", text or "")
    if not match:
        return None
    return (int(match.group(1)), int(match.group(2)))


def test_os_reachable_and_family(driver):
    info = driver.os_info()
    assert info.get("ok", True), info
    assert info.get("os_family") == driver.os_family, info


def test_os_version_in_range(driver, thresholds):
    info = driver.os_info()
    spec = thresholds["os"][driver.os_family]
    text = info.get("os_version", "") if driver.os_family == "linux" else info.get("kernel", "")
    version = _version_tuple(text)
    assert version is not None, f"could not parse version from {text!r}"
    low = tuple(int(x) for x in spec["version_range"][0].split(".")[:2])
    high = tuple(int(x) for x in spec["version_range"][1].split(".")[:2])
    assert low <= version <= high, f"{text!r} ({version}) not within {spec['version_range']}"


def test_time_sync(driver):
    skew = abs(driver.utc_epoch() - time.time())
    assert skew < 120, f"clock skew {skew:.1f}s exceeds 120s"
