"""Unit tests for report triage grouping and cause classification."""

from __future__ import annotations

from labbench.report import _suspect, _triage


def test_suspect_classification() -> None:
    assert "corruption" in _suspect("checksum mismatch on labbench_io.bin").lower()
    assert "no_path_retry" in _suspect("I/O did not resume within the no_path_retry window").lower()
    assert "unreachable" in _suspect("Connection refused").lower()
    assert "disk timeout" in _suspect("windows disk timeout 30 != 60").lower()


def test_triage_groups_by_os_case_signature() -> None:
    failures = [
        {
            "host": "win-host-1",
            "case": "test_failover",
            "signature": "AssertionError: recovery None",
        },
        {
            "host": "win-host-2",
            "case": "test_failover",
            "signature": "AssertionError: recovery None",
        },
        {"host": "linux-host-1", "case": "test_io_integrity", "signature": "checksum mismatch"},
    ]
    os_by_host = {"win-host-1": "windows", "win-host-2": "windows", "linux-host-1": "linux"}
    triage = _triage(failures, os_by_host)
    assert len(triage) == 2
    windows = next(t for t in triage if t["os_family"] == "windows")
    assert windows["count"] == 2
    assert "win-host-1" in windows["hosts"] and "win-host-2" in windows["hosts"]
    assert windows["rerun"].startswith("labbench validate --only test_failover")
    assert triage[0]["suspected_cause"]
