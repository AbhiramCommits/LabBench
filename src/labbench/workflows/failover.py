"""Multipath failover workflow with recovery-time measurement.

For each host and N iterations: sever path A, continuously probe I/O and
measure the time until I/O resumes on the surviving path, then restore path A
and measure the time until both paths are active again. Results (min/median/
max/p95) are written to ``artifacts/failover_<timestamp>.json``. Corrupted
checksums or I/O outages beyond the configured ``no_path_retry`` window are
failures.
"""

from __future__ import annotations

import json
import math
import statistics
import threading
import time
from collections.abc import Callable
from typing import Any

from .. import paths
from ..drivers import driver_for
from ..logging_setup import get_logger, log_event
from ..thresholds import load_thresholds
from ..topology import Lab
from .attach import MOUNT_PATH

log = get_logger("failover")


class IOMonitor(threading.Thread):
    """Continuously probe I/O and record (timestamp, ok) samples."""

    def __init__(self, driver: Any, interval_s: float) -> None:
        super().__init__(daemon=True)
        self.driver = driver
        self.interval = interval_s
        self.samples: list[tuple[float, bool]] = []
        self._stop_event = threading.Event()
        self._lock = threading.Lock()

    def run(self) -> None:
        while not self._stop_event.is_set():
            t = time.monotonic()
            try:
                result = self.driver.io_probe()
                ok = bool(result.get("ok"))
            except Exception:  # noqa: BLE001
                ok = False
            with self._lock:
                self.samples.append((t, ok))
            self._stop_event.wait(self.interval)

    def stop(self) -> list[tuple[float, bool]]:
        self._stop_event.set()
        self.join(timeout=5)
        with self._lock:
            return list(self.samples)

    def recovery_after(self, t0: float) -> float | None:
        """Seconds from ``t0`` until I/O resumes, requiring a prior failure."""

        seen_failure = False
        with self._lock:
            samples = list(self.samples)
        for t, ok in samples:
            if t < t0:
                continue
            if not ok:
                seen_failure = True
            elif seen_failure:
                return t - t0
        return None

    def failures_after(self, t0: float) -> int:
        with self._lock:
            return sum(1 for t, ok in self.samples if t >= t0 and not ok)

    def wait_for_recovery(self, t0: float, timeout_s: float, poll_s: float = 0.01) -> float | None:
        """Poll until I/O resumes after an outage that started at ``t0``."""

        deadline = time.monotonic() + timeout_s
        while time.monotonic() < deadline:
            recovery = self.recovery_after(t0)
            if recovery is not None:
                return recovery
            time.sleep(poll_s)
        return self.recovery_after(t0)


def _limit(fo: dict[str, Any], os_family: str, key: str) -> float:
    """Return the per-OS override for a failover guardrail, else the global one."""

    return float(fo.get("by_os", {}).get(os_family, {}).get(key, fo[key]))


def _stats(values: list[float]) -> dict[str, float | None]:
    clean = [v for v in values if v is not None]
    if not clean:
        return {"min": None, "median": None, "max": None, "p95": None, "count": 0}
    ordered = sorted(clean)
    idx = max(0, min(len(ordered) - 1, math.ceil(0.95 * len(ordered)) - 1))
    return {
        "min": round(min(clean), 2),
        "median": round(statistics.median(clean), 2),
        "max": round(max(clean), 2),
        "p95": round(ordered[idx], 2),
        "count": len(clean),
    }


def _failover_host(
    driver: Any,
    target: Any,
    iterations: int,
    probe_interval_s: float,
    window_s: float,
) -> dict[str, Any]:
    portal_a = target.portals[0]
    driver.login(target.iqn, target.portals)
    driver.mount(0, MOUNT_PATH)

    baseline = driver.write_verify(MOUNT_PATH, 64)
    baseline_checksum = baseline.get("checksum")

    per_iteration: list[dict[str, Any]] = []
    recoveries: list[float] = []
    restores: list[float] = []
    total_errors = 0
    corrupted = False

    for i in range(iterations):
        driver.wait_for_active_paths(2, timeout_s=15)
        monitor = IOMonitor(driver, probe_interval_s)
        monitor.start()
        time.sleep(0.1)  # let the monitor establish a success baseline

        t_down = time.monotonic()
        driver.fail_path(portal_a)
        recovery = monitor.wait_for_recovery(t_down, timeout_s=window_s + 1.0)
        errors = monitor.failures_after(t_down)

        t_restore = time.monotonic()
        driver.restore_path(portal_a)
        restore_s = driver.wait_for_active_paths(2, timeout_s=15)
        samples = monitor.stop()
        restore_s = max(restore_s, time.monotonic() - t_restore)

        per_iteration.append(
            {
                "iteration": i,
                "recovery_ms": round(recovery * 1000, 2) if recovery is not None else None,
                "restore_ms": round(restore_s * 1000, 2),
                "io_errors": errors,
                "samples": len(samples),
            }
        )
        total_errors += errors
        if recovery is not None:
            recoveries.append(recovery)
        restores.append(restore_s)
        if recovery is None or recovery > window_s:
            corrupted = True  # I/O did not resume within no_path_retry window

    final = driver.write_verify(MOUNT_PATH, 64)
    if final.get("checksum") != baseline_checksum or not final.get("ok"):
        corrupted = True

    return {
        "iterations": per_iteration,
        "recovery_ms": _stats([r * 1000 for r in recoveries]),
        "restore_ms": _stats([r * 1000 for r in restores]),
        "io_errors": total_errors,
        "corrupted": corrupted,
        "baseline_checksum": baseline_checksum,
        "final_checksum": final.get("checksum"),
    }


def run_failover(
    lab: Lab,
    only: set[str] | None = None,
    iterations: int | None = None,
    driver_factory: Callable[[Any, Any], Any] | None = None,
    thresholds: dict[str, Any] | None = None,
) -> dict[str, Any]:
    thresholds = thresholds or load_thresholds()
    fo = thresholds["failover"]
    iterations = iterations or int(fo["iterations"])
    probe_interval_s = float(fo["probe_interval_ms"]) / 1000.0
    window_s = float(fo["no_path_retry_window_ms"]) / 1000.0
    factory = driver_factory or driver_for

    target = lab.targets[0]
    hosts = [h for h in lab.hosts if not only or h.name in only]
    host_reports: list[dict[str, Any]] = []
    mode = "unknown"

    for host in hosts:
        driver = factory(host, lab)
        mode = driver.mode
        report: dict[str, Any] = {"host": host.name, "os_family": host.os_family, "pass": False}
        try:
            result = _failover_host(driver, target, iterations, probe_interval_s, window_s)
            report.update(result)
            rec = result["recovery_ms"]
            rst = result["restore_ms"]
            rec_limit = _limit(fo, host.os_family, "recovery_p95_ms")
            rst_limit = _limit(fo, host.os_family, "restore_p95_ms")
            report["thresholds"] = {"recovery_p95_ms": rec_limit, "restore_p95_ms": rst_limit}
            report["pass"] = (
                not result["corrupted"]
                and rec["p95"] is not None
                and rec["p95"] <= rec_limit
                and rst["p95"] is not None
                and rst["p95"] <= rst_limit
            )
        except Exception as exc:  # noqa: BLE001
            report["error"] = f"{type(exc).__name__}: {exc}"
            log_event(log, "failover_host_error", host=host.name, error=report["error"])
        finally:
            driver.close()
        host_reports.append(report)
        log_event(
            log,
            "failover_host",
            host=host.name,
            pass_=report.get("pass"),
            recovery_p95=report.get("recovery_ms", {}).get("p95"),
        )

    artifact = paths.artifacts_dir() / f"failover_{paths.timestamp()}.json"
    payload = {
        "timestamp": paths.timestamp(),
        "mode": mode,
        "iterations": iterations,
        "portal_a": target.portals[0],
        "portal_b": target.portals[1] if len(target.portals) > 1 else None,
        "thresholds": fo,
        "hosts": host_reports,
    }
    artifact.write_text(json.dumps(payload, indent=2), encoding="utf-8")

    passed = sum(1 for h in host_reports if h.get("pass"))
    return {
        "artifact": str(artifact),
        "hosts": host_reports,
        "passed": passed,
        "total": len(host_reports),
        "failed": len(host_reports) - passed,
        "mode": mode,
    }
