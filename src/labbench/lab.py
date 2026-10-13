"""Docker lab lifecycle: up / down / status.

Wraps ``docker compose`` for the LabBench mixed-OS lab and adds readiness
probes so bring-up is deterministic and measured.
"""

from __future__ import annotations

import json
import logging
import socket
import subprocess
import time
import urllib.error
import urllib.request
from typing import Any

from . import paths
from .logging_setup import get_logger, log_event
from .topology import Host, Lab

log = get_logger("lab")

PROJECT = "labbench"
SIM_URL = "http://127.0.0.1:8080"
SIM_PORT = 8080


def _compose_file() -> str:
    return str(paths.docker_dir() / "compose.yml")


def _run(cmd: list[str], timeout: int = 1800) -> tuple[int, str, str]:
    log_event(log, "exec", cmd=" ".join(cmd))
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, check=False)
    return proc.returncode, proc.stdout, proc.stderr


def _base() -> list[str]:
    return ["docker", "compose", "-f", _compose_file(), "-p", PROJECT]


def _http_ok(url: str, timeout: float = 3.0) -> tuple[bool, str]:
    try:
        with urllib.request.urlopen(url, timeout=timeout) as resp:
            return resp.status == 200, f"HTTP {resp.status}"
    except urllib.error.HTTPError as exc:
        return False, f"HTTP {exc.code}"
    except Exception as exc:  # noqa: BLE001
        return False, type(exc).__name__


def _tcp_ok(host: str, port: int, timeout: float = 3.0) -> tuple[bool, str]:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True, "tcp open"
    except OSError as exc:
        return False, type(exc).__name__


def probe_host(host: Host) -> tuple[bool, str]:
    """Probe a single host's management endpoint."""

    if host.is_windows:
        return _http_ok(f"http://{host.address}:{host.port}/health")
    return _tcp_ok(host.address, host.port)


def simulator_mode() -> str:
    ok, _ = _http_ok(f"{SIM_URL}/health")
    if not ok:
        return "unknown"
    try:
        with urllib.request.urlopen(f"{SIM_URL}/mode", timeout=3) as resp:
            return json.loads(resp.read()).get("mode", "unknown")
    except Exception:  # noqa: BLE001
        return "unknown"


def up(lab: Lab, timeout: int = 180, build: bool = True) -> dict[str, Any]:
    """Build and start the lab, waiting for every service to be healthy."""

    start = time.monotonic()
    down(lab, volumes=False, quiet=True)  # ensure a clean slate

    cmd = _base() + ["up", "-d", "--remove-orphans"]
    if build:
        cmd.append("--build")
    rc, out, err = _run(cmd)
    if rc != 0:
        raise RuntimeError(f"docker compose up failed:\n{err.strip()}\n{out.strip()}")

    services = _await_ready(lab, timeout)
    elapsed = time.monotonic() - start
    mode = simulator_mode()
    healthy = all(s["state"] == "healthy" for s in services)
    if not healthy:
        raise RuntimeError(
            "lab did not become healthy within "
            f"{timeout}s: {[s['name'] for s in services if s['state'] != 'healthy']}"
        )
    log_event(log, "lab_up", seconds=round(elapsed, 2), mode=mode)
    return {"bringup_seconds": elapsed, "mode": mode, "services": services}


def _await_ready(lab: Lab, timeout: int) -> list[dict[str, Any]]:
    deadline = time.monotonic() + timeout
    services: list[dict[str, Any]] = []
    while True:
        services = []
        all_ok = True
        for host in lab.hosts:
            ok, detail = probe_host(host)
            all_ok = all_ok and ok
            services.append(
                {
                    "name": host.name,
                    "state": "healthy" if ok else "starting",
                    "health": "healthy" if ok else "starting",
                    "detail": f"{host.connection} {host.address}:{host.port} ({detail})",
                }
            )
        sim_ok, sim_detail = _http_ok(f"{SIM_URL}/health")
        all_ok = all_ok and sim_ok
        services.insert(
            0,
            {
                "name": "storage-target",
                "state": "healthy" if sim_ok else "starting",
                "health": "healthy" if sim_ok else "starting",
                "detail": f"api 127.0.0.1:{SIM_PORT} ({sim_detail})",
            },
        )
        if all_ok:
            return services
        if time.monotonic() > deadline:
            return services
        time.sleep(2)


def _compose_ps() -> list[dict[str, Any]]:
    rc, out, err = _run(_base() + ["ps", "--format", "json"])
    if rc != 0:
        return []
    text = out.strip()
    if not text:
        return []
    try:
        data = json.loads(text)
        if isinstance(data, list):
            return data
        if isinstance(data, dict):
            return [data]
    except json.JSONDecodeError:
        pass
    rows = []
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rows.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return rows


def status(lab: Lab) -> dict[str, Any]:
    """Return per-service health, mode and uptime for the running lab."""

    rows = _compose_ps()
    by_service = {r.get("Service"): r for r in rows if isinstance(r, dict)}
    services: list[dict[str, Any]] = []

    sim_ok, sim_detail = _http_ok(f"{SIM_URL}/health")
    services.append(
        {
            "name": "storage-target",
            "state": by_service.get("storage-target", {}).get("State", "not-created"),
            "health": "healthy" if sim_ok else "unreachable",
            "detail": f"api 127.0.0.1:{SIM_PORT} ({sim_detail})",
        }
    )

    for host in lab.hosts:
        ok, detail = probe_host(host)
        row = by_service.get(host.tags.get("container", host.name), {})
        services.append(
            {
                "name": host.name,
                "state": row.get("State", "up" if ok else "not-created"),
                "health": "healthy" if ok else "unreachable",
                "detail": f"{host.connection} {host.address}:{host.port} ({detail})",
            }
        )

    running = bool(rows)
    return {
        "lab": lab.name,
        "mode": simulator_mode() if sim_ok else "down",
        "state": "running" if running else "not-created",
        "uptime": _uptime(by_service),
        "services": services,
    }


def _uptime(by_service: dict[str, Any]) -> str | None:
    for row in by_service.values():
        uptime = row.get("RunningFor") or row.get("Status")
        if uptime:
            return str(uptime)
    return None


def down(
    lab: Lab, volumes: bool = True, prune_images: bool = False, quiet: bool = False
) -> dict[str, Any]:
    """Tear the lab down and remove leftover containers/volumes."""

    containers = _count("container", "com.docker.compose.project")
    vol_count = _count("volume", "com.docker.compose.project")
    networks = _count("network", "com.docker.compose.project")

    cmd = _base() + ["down", "--remove-orphans"]
    if volumes:
        cmd.append("-v")
    if quiet:
        subprocess.run(cmd, capture_output=True, text=True, timeout=600, check=False)
    else:
        rc, out, err = _run(cmd)
        if rc != 0:
            log_event(
                log, "compose_down_nonzero", level=logging.WARNING, rc=rc, err=err.strip()[-500:]
            )

    if prune_images:
        subprocess.run(
            ["docker", "image", "prune", "-f"], capture_output=True, text=True, check=False
        )

    log_event(log, "lab_down", containers=containers, networks=networks, volumes=vol_count)
    return {
        "containers": 0 if quiet else containers,
        "networks": networks,
        "volumes": vol_count,
    }


def _count(kind: str, label: str) -> int:
    rc, out, err = _run(
        (
            ["docker", kind, "ls", "-aq", "--filter", f"label={label}={PROJECT}"]
            if kind == "container"
            else ["docker", kind, "ls", "-q", "--filter", f"label={label}={PROJECT}"]
        ),
        timeout=60,
    )
    if rc != 0:
        return 0
    return len([line for line in out.splitlines() if line.strip()])
