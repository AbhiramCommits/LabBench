"""Fan out OS/disk/driver collectors and merge the lab inventory.

Runs ``scripts/collect_lab_config.sh`` (Linux) and
``scripts/Collect-LabConfig.ps1`` (Windows) on every host through the driver
abstraction, validates the JSON against ``schemas/lab_config.schema.json``,
merges everything into ``artifacts/lab_inventory.json`` and a diff-friendly
``artifacts/lab_inventory.md`` table, and flags configuration drift against the
expectations in ``labs/dev-lab.yml`` / ``config/thresholds.yml``.
"""

from __future__ import annotations

import base64
import json
from collections.abc import Callable
from typing import Any

import jsonschema

from . import paths
from .drivers import driver_for
from .logging_setup import get_logger, log_event
from .thresholds import load_thresholds
from .topology import Host, Lab

log = get_logger("collect")


def _schema() -> dict[str, Any]:
    return json.loads((paths.schemas_dir() / "lab_config.schema.json").read_text(encoding="utf-8"))


def _last_json(res: Any) -> dict[str, Any]:
    text = res.stdout or ""
    if isinstance(text, bytes):
        text = text.decode("utf-8", errors="replace")
    text = text.strip()
    if not text:
        return {"error": "no output", "stderr": (res.stderr or "")[-400:]}
    for line in reversed(text.splitlines()):
        line = line.strip()
        if line.startswith("{"):
            try:
                return json.loads(line)
            except json.JSONDecodeError:
                continue
    return {"error": "no JSON object in output", "raw": text[-400:]}


def _collect_linux(driver: Any) -> dict[str, Any]:
    script = (paths.scripts_dir() / "collect_lab_config.sh").read_text(encoding="utf-8")
    b64 = base64.b64encode(script.encode()).decode()
    cmd = (
        f"printf '%s' {b64} | base64 -d > /tmp/labbench_collect.sh && "
        f"LABBENCH_SIMULATOR={driver.simulator_url} LABBENCH_INITIATOR={driver.initiator} "
        f"bash /tmp/labbench_collect.sh"
    )
    return _last_json(driver.run(cmd))


def _collect_windows(driver: Any) -> dict[str, Any]:
    script = (paths.scripts_dir() / "Collect-LabConfig.ps1").read_text(encoding="utf-8")
    b64 = base64.b64encode(script.encode()).decode()
    ps = (
        f"$c=[Text.Encoding]::UTF8.GetString([Convert]::FromBase64String('{b64}')); "
        f"Set-Content -Path /tmp/LabBench-Collect.ps1 -Value $c -Encoding UTF8 -Force; "
        f"& /tmp/LabBench-Collect.ps1 -Sim '{driver.simulator_url}' -Initiator '{driver.initiator}'"
    )
    return _last_json(driver.run(ps))


def _drift(host: Host, cfg: dict[str, Any], thresholds: dict[str, Any]) -> list[str]:
    findings: list[str] = []
    expected_paths = int(thresholds["drift"]["path_count"])
    for disk in cfg.get("disks", []):
        if int(disk.get("path_count", 0)) != expected_paths:
            findings.append(
                f"{host.name}: disk {disk.get('name')} path_count="
                f"{disk.get('path_count')} (expected {expected_paths})"
            )
    if not cfg.get("multipath", {}).get("enabled"):
        findings.append(f"{host.name}: multipath/MPIO not enabled")
    expected_iqn = f"iqn.2026-01.labbench:{host.name}"
    if cfg.get("initiator") != expected_iqn:
        findings.append(
            f"{host.name}: initiator={cfg.get('initiator')!r} (expected {expected_iqn!r})"
        )
    if host.is_windows:
        timeout = cfg.get("disk_timeout_s")
        if timeout is not None and timeout != thresholds["drift"]["disk_timeout_s"]:
            findings.append(
                f"{host.name}: disk_timeout_s={timeout} "
                f"(expected {thresholds['drift']['disk_timeout_s']})"
            )
    return findings


def _markdown(inventory: dict[str, Any], drift: list[str]) -> str:
    lines = [
        "# LabBench Lab Inventory",
        "",
        f"- Mode: `{inventory['mode']}`",
        f"- Collected at: {inventory['timestamp']}",
        f"- Hosts: {len(inventory['hosts'])}",
        f"- Drift findings: {len(drift)}",
        "",
        "| host | os_family | os_version | kernel | cpu | mem_mb | paths | multipath | initiator | disk_timeout_s |",
        "| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |",
    ]
    for name, cfg in inventory["hosts"].items():
        if "error" in cfg:
            lines.append(f"| {name} | - | ERROR: {cfg['error']} | - | - | - | - | - | - | - |")
            continue
        disks = cfg.get("disks", [])
        paths = ",".join(str(d.get("path_count")) for d in disks) or "0"
        mp = cfg.get("multipath", {})
        mp_state = "enabled" if mp.get("enabled") else "disabled"
        lines.append(
            f"| {name} | {cfg.get('os_family')} | {cfg.get('os_version')} | {cfg.get('kernel')} "
            f"| {cfg.get('cpu_count')} | {cfg.get('mem_mb')} | {paths} | {mp_state} "
            f"| {cfg.get('initiator')} | {cfg.get('disk_timeout_s')} |"
        )
    if drift:
        lines += ["", "## Configuration drift", ""]
        lines += [f"- {d}" for d in drift]
    return "\n".join(lines) + "\n"


def run_collect(
    lab: Lab,
    only: set[str] | None = None,
    driver_factory: Callable[[Any, Any], Any] | None = None,
) -> dict[str, Any]:
    thresholds = load_thresholds()
    schema = _schema()
    factory = driver_factory or driver_for
    hosts = [h for h in lab.hosts if not only or h.name in only]

    inventory: dict[str, Any] = {}
    drift: list[str] = []
    schema_errors: list[str] = []
    mode = "unknown"
    target = lab.targets[0] if lab.targets else None

    for host in hosts:
        driver = factory(host, lab)
        mode = driver.mode
        try:
            # Log in first so the collector observes the real path count.
            if target is not None:
                try:
                    driver.login(target.iqn, target.portals)
                except Exception as exc:  # noqa: BLE001
                    log_event(log, "collect_login_failed", host=host.name, error=str(exc))
            cfg = _collect_windows(driver) if host.is_windows else _collect_linux(driver)
            try:
                jsonschema.validate(instance=cfg, schema=schema)
            except jsonschema.ValidationError as exc:
                schema_errors.append(f"{host.name}: {exc.message}")
                cfg["_schema_valid"] = False
            else:
                cfg["_schema_valid"] = True
            inventory[host.name] = cfg
            drift.extend(_drift(host, cfg, thresholds))
        except Exception as exc:  # noqa: BLE001
            inventory[host.name] = {"error": f"{type(exc).__name__}: {exc}"}
        finally:
            driver.close()
        log_event(
            log,
            "collect_host",
            host=host.name,
            schema_valid=inventory[host.name].get("_schema_valid"),
        )

    payload = {
        "timestamp": paths.timestamp(),
        "mode": mode,
        "hosts": inventory,
        "drift": drift,
        "schema_errors": schema_errors,
    }
    artifact = paths.artifacts_dir() / "lab_inventory.json"
    artifact.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    md = paths.artifacts_dir() / "lab_inventory.md"
    md.write_text(_markdown(payload, drift), encoding="utf-8")

    return {
        "artifact": str(artifact),
        "markdown": str(md),
        "drift": drift,
        "drift_count": len(drift),
        "schema_errors": schema_errors,
        "hosts": inventory,
        "mode": mode,
    }
