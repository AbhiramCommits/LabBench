"""Run the LabBench Ansible playbook and parse the PLAY RECAP.

Wraps ``ansible-playbook`` (installed alongside LabBench) with the dynamic
inventory, captures the per-host recap, and writes
``artifacts/provision_<timestamp>.json``.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import time
import urllib.request
from pathlib import Path
from typing import Any

from . import paths
from .logging_setup import get_logger, log_event
from .topology import Lab

log = get_logger("provision")

RECAP_RE = re.compile(
    r"^(?P<host>\S+)\s*:\s*ok=(?P<ok>\d+)\s+changed=(?P<changed>\d+)\s+"
    r"unreachable=(?P<unreachable>\d+)\s+failed=(?P<failed>\d+)\s+"
    r"skipped=(?P<skipped>\d+)\s+rescued=(?P<rescued>\d+)\s+ignored=(?P<ignored>\d+)",
    re.MULTILINE,
)


def _playbook_exe() -> str:
    candidate = Path(sys.executable).parent / "ansible-playbook"
    if candidate.is_file():
        return str(candidate)
    found = shutil.which("ansible-playbook")
    if not found:
        raise RuntimeError(
            "ansible-playbook not found. Install dev requirements: pip install -e '.[dev]'"
        )
    return found


def _mode(lab: Lab) -> str:
    try:
        with urllib.request.urlopen(f"{lab.simulator_url}/mode", timeout=3) as resp:
            return json.loads(resp.read()).get("mode", "unknown")
    except Exception:  # noqa: BLE001
        return "unknown"


def run_provision(lab: Lab, limit: str | None = None, check: bool = False) -> dict[str, Any]:
    ansible_dir = paths.ansible_dir()
    exe = _playbook_exe()
    cmd = [exe, "playbooks/site.yml", "-i", "inventory/labbench_inventory.py"]
    if limit:
        cmd += ["--limit", limit]
    if check:
        cmd += ["--check"]
    if not check:
        cmd += ["--diff"]

    env = dict(os.environ)
    env["PATH"] = str(Path(sys.executable).parent) + os.pathsep + env.get("PATH", "")
    env["ANSIBLE_NOCOLOR"] = "1"
    env["ANSIBLE_FORCE_COLOR"] = "0"
    env["LABBENCH_LAB"] = str(paths.default_lab_path())
    env["PYTHONUNBUFFERED"] = "1"

    start = time.monotonic()
    log_event(log, "ansible_start", cmd=" ".join(cmd), cwd=str(ansible_dir))
    proc = subprocess.run(
        cmd,
        cwd=str(ansible_dir),
        env=env,
        capture_output=True,
        text=True,
        timeout=3600,
        check=False,
    )
    duration = time.monotonic() - start
    output = (proc.stdout or "") + "\n" + (proc.stderr or "")

    hosts: list[dict[str, Any]] = []
    totals = {"ok": 0, "changed": 0, "failed": 0, "unreachable": 0, "skipped": 0, "rescued": 0}
    for m in RECAP_RE.finditer(output):
        host = {
            "host": m.group("host"),
            "ok": int(m.group("ok")),
            "changed": int(m.group("changed")),
            "failed": int(m.group("failed")),
            "unreachable": int(m.group("unreachable")),
            "skipped": int(m.group("skipped")),
            "rescued": int(m.group("rescued")),
        }
        hosts.append(host)
        for key in totals:
            totals[key] += host[key]

    playbook = {
        "ok": totals["ok"],
        "changed": totals["changed"],
        "failed": totals["failed"],
        "unreachable": totals["unreachable"],
        "skipped": totals["skipped"],
        "duration_seconds": duration,
        "returncode": proc.returncode,
    }

    artifact = paths.artifacts_dir() / f"provision_{paths.timestamp()}.json"
    payload = {
        "timestamp": paths.timestamp(),
        "mode": _mode(lab),
        "duration_seconds": duration,
        "limit": limit,
        "check": check,
        "command": cmd,
        "playbook": playbook,
        "hosts": hosts,
    }
    artifact.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    log_event(
        log,
        "ansible_done",
        duration=round(duration, 2),
        changed=totals["changed"],
        failed=totals["failed"],
        unreachable=totals["unreachable"],
        artifact=str(artifact),
    )

    if not hosts:
        # No recap parsed -> surface the tail of the output for debugging.
        log_event(log, "ansible_no_recap", tail=output.strip()[-2000:])

    return {"playbook": playbook, "artifact": str(artifact), "hosts": hosts, "output": output}
