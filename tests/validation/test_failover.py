"""Failover: sever path A, I/O continues on the survivor, recovery in window."""


from __future__ import annotations

from labbench.workflows.failover import run_failover

import pytest

pytestmark = pytest.mark.validation


def test_path_down_io_continues_and_recovers(host, lab, thresholds):
    result = run_failover(lab, only={host.name}, iterations=1, thresholds=thresholds)
    report = result["hosts"][0]
    assert "error" not in report, report
    assert report.get("pass") is True, report
    recovery = report["recovery_ms"]["p95"]
    assert recovery is not None, report
    assert recovery <= thresholds["failover"]["recovery_p95_ms"], report
    assert report["corrupted"] is False, report
