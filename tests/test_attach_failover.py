"""Unit tests for the attach and failover state machines (FakeDriver)."""

from __future__ import annotations

from fakes import FakeDriver
from labbench.thresholds import load_thresholds
from labbench.topology import Host, Lab, Lun, Target
from labbench.workflows.attach import run_attach
from labbench.workflows.failover import run_failover


def _lab() -> Lab:
    hosts = [
        Host(name="linux-host-1", os_family="linux", address="127.0.0.1", connection="ssh"),
        Host(
            name="win-host-1",
            os_family="windows",
            address="127.0.0.1",
            connection="winrm",
            user="Administrator",
        ),
    ]
    target = Target(
        name="storage-target",
        iqn="iqn.2026-01.labbench:storage.target1",
        luns=[Lun(id=0, size_gb=8)],
        portals=["172.28.0.10:3260", "172.29.0.10:3260"],
    )
    return Lab(name="unit", hosts=hosts, targets=[target])


def _factory(**fake_kwargs):
    def make(host, lab):
        return FakeDriver(host, lab, **fake_kwargs)

    return make


def test_attach_all_hosts_ready() -> None:
    result = run_attach(_lab(), driver_factory=_factory())
    assert result["total"] == 2
    assert result["ok"] == 2
    for entry in result["hosts"]:
        assert entry["active_paths"] == 2
        assert entry["checksum"] == "baseline-checksum-0000"
        assert entry["write_verify_ok"] is True


def test_attach_flags_single_path() -> None:
    result = run_attach(_lab(), driver_factory=_factory(start_paths=1))
    assert result["ok"] == 0
    assert all(entry["active_paths"] == 1 for entry in result["hosts"])


def test_failover_measures_recovery_time() -> None:
    thresholds = load_thresholds()
    result = run_failover(
        _lab(),
        iterations=3,
        driver_factory=_factory(failover_delay=0.12, restore_delay=0.06),
        thresholds=thresholds,
    )
    assert result["total"] == 2
    assert result["passed"] == 2, result["hosts"]
    for host in result["hosts"]:
        assert host["pass"] is True
        assert host["recovery_ms"]["count"] == 3
        assert host["recovery_ms"]["median"] >= 100  # ~120ms delay
        assert host["restore_ms"]["median"] >= 40  # ~60ms delay
        assert host["corrupted"] is False


def test_failover_flags_corruption() -> None:
    result = run_failover(
        _lab(),
        iterations=2,
        driver_factory=_factory(corrupt=True),
        thresholds=load_thresholds(),
    )
    assert all(host["pass"] is False for host in result["hosts"])
    assert all(host["corrupted"] is True for host in result["hosts"])


def test_failover_reports_artifact_path() -> None:
    result = run_failover(_lab(), iterations=1, driver_factory=_factory())
    assert result["artifact"].endswith(".json")
    assert "failover_" in result["artifact"]
