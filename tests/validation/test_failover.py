"""Failover: sever path A, I/O continues on the survivor, recovery in window."""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.validation


def test_path_down_io_continues_and_recovers(host, lab, thresholds, failover_session):
    report = next(h for h in failover_session["hosts"] if h["host"] == host.name)
    assert "error" not in report, report
    assert report.get("pass") is True, report
    recovery = report["recovery_ms"]["p95"]
    assert recovery is not None, report
    limit = (
        thresholds["failover"]
        .get("by_os", {})
        .get(host.os_family, {})
        .get("recovery_p95_ms", thresholds["failover"]["recovery_p95_ms"])
    )
    assert recovery <= limit, report
    assert report["corrupted"] is False, report
