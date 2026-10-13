"""Multipath failover workflow with recovery-time measurement.

For each host and N iterations: sever path A, continuously poll I/O and
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


def _poll_until_recovery(
    driver: Any, t_down: float, window_s: float, interval_s: float
) -> tuple[float | None, int]:
    """Poll I/O until it succeeds again. Returns (recovery_seconds, io_errors)."""

    deadline = t_down + window_s + 5.0
    errors = 0
    while time.monotonic() < deadline:
        t = time.monotonic()
        try:
            ok = bool(driver.io_probe().get("ok"))
        except Exception:  # noqa: BLE001
            ok = False
        if ok:
            return t - t_down, errors
        errors += 1
        time.sleep(interval_s)
    return None, errors


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
        driver.io_probe()  # warm the connection and confirm I/O is healthy

        t_down = time.monotonic()
        driver.fail_path(portal_a)
        recovery, errors = _poll_until_recovery(driver, t_down, window_s, probe_interval_s)

        t_restore = time.monotonic()
        driver.restore_path(portal_a)
        restore_s = driver.wait_for_active_paths(2, timeout_s=20)
        restore_s = max(restore_s, time.monotonic() - t_restore)

        per_iteration.append(
            {
                "iteration": i,
                "recovery_ms": round(recovery * 1000, 2) if recovery is not None else None,
                "restore_ms": round(restore_s * 1000, 2),
                "io_errors": errors,
            }
        )
        total_errors += errors
        if recovery is not None:
            recoveries.append(recovery)
        restores.append(restore_s)
        if recovery is None or recovery > window_s:
            corrupted = True  # I/O did not resume within the no_path_retry window

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
