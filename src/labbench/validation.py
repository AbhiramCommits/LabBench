"""Run the shared cross-OS pytest validation suite and summarise results.

Invokes pytest on ``tests/validation`` with JUnit XML output, then parses the
XML into a per-host pass/fail matrix.
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
import xml.etree.ElementTree as ET
from collections import defaultdict
from typing import Any

from . import paths
from .logging_setup import get_logger, log_event
from .topology import Lab

log = get_logger("validate")

CASE_FILES = {
    "os_baseline": "test_os_baseline.py",
    "initiator_config": "test_initiator_config.py",
    "target_attach": "test_target_attach.py",
    "io_integrity": "test_io_integrity.py",
    "failover": "test_failover.py",
}

HOST_ID_RE = re.compile(r"\[([^\]]+)\]")


def _case_paths(only: list[str] | None) -> list[str]:
    vdir = paths.repo_root() / "tests" / "validation"
    if not only:
        return [str(vdir)]
    files: list[str] = []
    for item in only:
        item = item.strip()
        if item.endswith(".py"):
            files.append(str(vdir / item))
        elif item in CASE_FILES:
            files.append(str(vdir / CASE_FILES[item]))
        else:
            raise ValueError(f"unknown validation case {item!r}; known: {sorted(CASE_FILES)}")
    return files


def _parse_junit(junit: str) -> tuple[list[dict[str, Any]], dict[str, int]]:
    per_host: dict[str, dict[str, int]] = defaultdict(
        lambda: {"passed": 0, "failed": 0, "skipped": 0, "total": 0}
    )
    path = paths.repo_root() / junit if not os.path.isabs(junit) else junit
    try:
        root = ET.parse(path).getroot()
    except (OSError, ET.ParseError):
        return [], {"passed": 0, "failed": 0, "skipped": 0, "total": 0}

    for case in root.iter("testcase"):
        name = case.get("name", "")
        match = HOST_ID_RE.search(name)
        host = match.group(1) if match else "unknown"
        row = per_host[host]
        row["total"] += 1
        if case.find("failure") is not None or case.find("error") is not None:
            row["failed"] += 1
        elif case.find("skipped") is not None:
            row["skipped"] += 1
        else:
            row["passed"] += 1

    matrix = [{"host": host, **row} for host, row in sorted(per_host.items())]
    totals = {
        "passed": sum(r["passed"] for r in matrix),
        "failed": sum(r["failed"] for r in matrix),
        "skipped": sum(r["skipped"] for r in matrix),
        "total": sum(r["total"] for r in matrix),
    }
    return matrix, totals


def run_validation(
    lab: Lab,
    only: list[str] | None = None,
    hosts: set[str] | None = None,
    numprocesses: str | None = None,
) -> dict[str, Any]:
    paths.artifacts_dir()
    junit = paths.artifacts_dir() / "junit.xml"
    targets = _case_paths(only)

    args = [
        sys.executable,
        "-m",
        "pytest",
        *targets,
        f"--junitxml={junit}",
        "-p",
        "no:cacheprovider",
        "-o",
        "addopts=",
    ]
    if numprocesses:
        args += ["-n", str(numprocesses)]

    env = dict(os.environ)
    env["LABBENCH_LAB"] = str(paths.default_lab_path())
    env["PYTHONUNBUFFERED"] = "1"
    if hosts:
        env["LABBENCH_HOSTS"] = ",".join(sorted(hosts))

    log_event(log, "validate_start", cmd=" ".join(args), hosts=sorted(hosts) if hosts else None)
    proc = subprocess.run(args, env=env, capture_output=True, text=True, timeout=5400, check=False)
    matrix, totals = _parse_junit(str(junit))

    result = {
        "junit": str(junit),
        "matrix": matrix,
        "returncode": proc.returncode,
        "output": (proc.stdout or "") + (proc.stderr or ""),
        **totals,
    }
    log_event(
        log,
        "validate_done",
        passed=totals["passed"],
        failed=totals["failed"],
        total=totals["total"],
    )
    return result
