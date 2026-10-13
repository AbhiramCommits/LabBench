"""iSCSI attach workflow.

For every host: discover targets, log in to both portals, confirm the
multipath/MPIO device shows the expected number of active paths, mount the
LUN, write a file and verify its checksum.
"""

from __future__ import annotations

from typing import Any, Callable

from ..drivers import driver_for
from ..logging_setup import get_logger, log_event
from ..thresholds import load_thresholds
from ..topology import Lab

log = get_logger("attach")

MOUNT_PATH = "/mnt/labbench/lun0"


def run_attach(
    lab: Lab,
    only: set[str] | None = None,
    driver_factory: Callable[[Any, Any], Any] | None = None,
    size_mb: int | None = None,
) -> dict[str, Any]:
    thresholds = load_thresholds()
    expected_paths = int(thresholds["attach"]["expected_paths"])
    lun = int(thresholds["attach"]["lun"])
    size_mb = size_mb or int(thresholds["io"]["size_mb"])
    factory = driver_factory or driver_for

    target = lab.targets[0]
    hosts = [h for h in lab.hosts if not only or h.name in only]
    results: list[dict[str, Any]] = []
    ok = 0
    mode = "unknown"

    for host in hosts:
        driver = factory(host, lab)
        mode = driver.mode
        entry: dict[str, Any] = {
            "host": host.name,
            "os_family": host.os_family,
            "mode": driver.mode,
            "active_paths": 0,
            "expected_paths": expected_paths,
            "device": None,
            "checksum": None,
            "ok": False,
        }
        try:
            info = driver.os_info()
            entry["os_version"] = info.get("os_version")
            driver.discover_targets(target.portals[0])
            driver.login(target.iqn, target.portals)
            paths = driver.paths(target.iqn)
            active = int(paths.get("active_paths", 0))
            entry["active_paths"] = active
            mount = driver.mount(lun, MOUNT_PATH)
            entry["device"] = mount.get("device")
            write = driver.write_verify(MOUNT_PATH, size_mb)
            entry["checksum"] = write.get("checksum")
            entry["bytes"] = write.get("bytes")
            entry["mbps"] = write.get("mbps")
            entry["write_verify_ok"] = bool(write.get("ok"))
            entry["ok"] = active >= expected_paths and bool(write.get("ok"))
            if entry["ok"]:
                ok += 1
        except Exception as exc:  # noqa: BLE001
            entry["error"] = f"{type(exc).__name__}: {exc}"
            log_event(log, "attach_host_error", host=host.name, error=entry["error"])
        finally:
            driver.close()
        results.append(entry)
        log_event(log, "attach_host", host=host.name, ok=entry["ok"], paths=entry["active_paths"])

    return {"ok": ok, "total": len(hosts), "hosts": results, "mode": mode}
