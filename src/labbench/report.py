"""Render HTML/Markdown reports and a failure triage summary.

Consumes the artifacts produced earlier in the pipeline:
``artifacts/junit.xml``, the latest ``failover_*.json``,
``lab_inventory.json`` and ``provision_*.json``.
"""

from __future__ import annotations

import json
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any

from jinja2 import Environment, FileSystemLoader, select_autoescape

from . import paths
from .logging_setup import get_logger, log_event
from .validation import HOST_ID_RE

log = get_logger("report")

TEMPLATE_DIR = Path(__file__).parent / "templates"

CAUSE_RULES: list[tuple[tuple[str, ...], str]] = [
    (
        ("checksum", "corrupt"),
        "Data corruption on the multipath device (stale path or bad failover)",
    ),
    (
        ("no path", "no_path_retry", "did not resume", "recovery"),
        "I/O did not resume within the no_path_retry window",
    ),
    (
        ("connection refused", "unreachable", "timed out", "refused"),
        "Host or storage simulator unreachable",
    ),
    (("disk timeout", "disk_timeout"), "Provisioning drift: disk timeout not applied"),
    (("active_paths", "path_count", "multipath"), "Multipath/MPIO shows fewer paths than expected"),
    (("iqn", "initiator"), "iSCSI initiator name missing or wrong"),
    (("version", "clock", "skew"), "OS version or clock outside the expected baseline"),
]


def _suspect(message: str) -> str:
    low = message.lower()
    for needles, cause in CAUSE_RULES:
        if any(n in low for n in needles):
            return cause
    return "Investigate the validation log for this configuration"


def _latest(pattern: str) -> Path | None:
    matches = sorted(paths.artifacts_dir().glob(pattern))
    return matches[-1] if matches else None


def _load_json(path: Path | None) -> dict[str, Any]:
    if not path or not path.is_file():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {}


def _junit_data(junit: Path) -> tuple[dict[str, dict[str, int]], list[dict[str, str]]]:
    matrix: dict[str, dict[str, int]] = {}
    failures: list[dict[str, str]] = []
    if not junit.is_file():
        return matrix, failures
    root = ET.parse(junit).getroot()
    for case in root.iter("testcase"):
        name = case.get("name", "")
        match = HOST_ID_RE.search(name)
        host = match.group(1) if match else "unknown"
        row = matrix.setdefault(host, {"passed": 0, "failed": 0, "skipped": 0, "total": 0})
        row["total"] += 1
        failure = case.find("failure")
        error = case.find("error")
        if failure is not None or error is not None:
            node = failure if failure is not None else error
            row["failed"] += 1
            message = (node.get("message") or node.text or "").strip().splitlines()
            failures.append(
                {
                    "host": host,
                    "case": case.get("classname", "").split(".")[-1] or name.split("[")[0],
                    "signature": (message[0] if message else "unknown failure")[:240],
                }
            )
        elif case.find("skipped") is not None:
            row["skipped"] += 1
        else:
            row["passed"] += 1
    return matrix, failures


def _failover_rows(failover: dict[str, Any]) -> list[dict[str, Any]]:
    rows = []
    for host in failover.get("hosts", []):
        rec = host.get("recovery_ms", {}) or {}
        rst = host.get("restore_ms", {}) or {}
        rows.append(
            {
                "host": host.get("host"),
                "os_family": host.get("os_family"),
                "recovery_min": rec.get("min"),
                "recovery_median": rec.get("median"),
                "recovery_p95": rec.get("p95"),
                "recovery_max": rec.get("max"),
                "restore_median": rst.get("median"),
                "restore_p95": rst.get("p95"),
                "io_errors": host.get("io_errors"),
                "corrupted": host.get("corrupted"),
                "pass": host.get("pass"),
            }
        )
    return rows


def _triage(failures: list[dict[str, str]], os_by_host: dict[str, str]) -> list[dict[str, str]]:
    groups: dict[tuple[str, str, str], dict[str, Any]] = {}
    for failure in failures:
        host = failure["host"]
        os_family = os_by_host.get(host, "unknown")
        key = (os_family, failure["case"], failure["signature"])
        group = groups.setdefault(
            key,
            {
                "os_family": os_family,
                "case": failure["case"],
                "signature": failure["signature"],
                "hosts": [],
            },
        )
        group["hosts"].append(host)
    triage = []
    for group in groups.values():
        host = sorted(group["hosts"])[0]
        triage.append(
            {
                "os_family": group["os_family"],
                "case": group["case"],
                "error_signature": group["signature"],
                "hosts": ", ".join(sorted(group["hosts"])),
                "count": len(group["hosts"]),
                "suspected_cause": _suspect(group["signature"]),
                "rerun": f"labbench validate --only {group['case']} --host {host}",
            }
        )
    return sorted(triage, key=lambda t: (t["os_family"], t["case"]))


def _markdown(context: dict[str, Any]) -> str:
    lines = [
        "# LabBench Validation Report",
        "",
        f"- Run: {context['run_timestamp']}",
        f"- Mode: `{context['mode']}`",
        f"- Lab: {context['lab_name']}",
        f"- Validation: passed={context['totals']['passed']} failed={context['totals']['failed']} "
        f"skipped={context['totals']['skipped']} total={context['totals']['total']}",
        "",
        "## Per-host pass/fail matrix",
        "",
        "| host | os_family | passed | failed | skipped | total |",
        "| --- | --- | --- | --- | --- | --- |",
    ]
    for row in context["matrix"]:
        lines.append(
            f"| {row['host']} | {row['os_family']} | {row['passed']} | {row['failed']} "
            f"| {row['skipped']} | {row['total']} |"
        )

    lines += [
        "",
        "## Multipath failover recovery times (ms)",
        "",
        "| host | os_family | min | median | p95 | max | restore median | io_errors | corrupted | pass |",
        "| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |",
    ]
    for row in context["failover"]:
        lines.append(
            f"| {row['host']} | {row['os_family']} | {row['recovery_min']} | {row['recovery_median']} "
            f"| {row['recovery_p95']} | {row['recovery_max']} | {row['restore_median']} "
            f"| {row['io_errors']} | {row['corrupted']} | {row['pass']} |"
        )

    lines += ["", "## Configuration drift", ""]
    if context["drift"]:
        lines += [f"- {d}" for d in context["drift"]]
    else:
        lines.append("- none")

    lines += ["", "## TRIAGE", ""]
    if context["triage"]:
        lines += [
            "| os_family | failing case | error signature | hosts | suspected cause | rerun |",
            "| --- | --- | --- | --- | --- | --- |",
        ]
        for t in context["triage"]:
            lines.append(
                f"| {t['os_family']} | {t['case']} | {t['error_signature'][:80]} | {t['hosts']} "
                f"| {t['suspected_cause']} | `{t['rerun']}` |"
            )
    else:
        lines.append("No failures. All configurations passed.")
    return "\n".join(lines) + "\n"


def build_report_context() -> dict[str, Any]:
    junit = paths.artifacts_dir() / "junit.xml"
    matrix_rows, failures = _junit_data(junit)
    inventory = _load_json(paths.artifacts_dir() / "lab_inventory.json")
    failover = _load_json(_latest("failover_*.json"))
    provision = _load_json(_latest("provision_*.json"))

    os_by_host = {
        name: cfg.get("os_family", "unknown")
        for name, cfg in (inventory.get("hosts", {}) or {}).items()
    }
    matrix = [
        {"host": host, "os_family": os_by_host.get(host, "unknown"), **row}
        for host, row in sorted(matrix_rows.items())
    ]
    totals = {
        "passed": sum(r["passed"] for r in matrix),
        "failed": sum(r["failed"] for r in matrix),
        "skipped": sum(r["skipped"] for r in matrix),
        "total": sum(r["total"] for r in matrix),
    }
    triage = _triage(failures, os_by_host)
    return {
        "run_timestamp": paths.timestamp(),
        "mode": failover.get("mode") or inventory.get("mode") or provision.get("mode") or "unknown",
        "lab_name": "dev-lab",
        "totals": totals,
        "matrix": matrix,
        "failover": _failover_rows(failover),
        "drift": inventory.get("drift", []),
        "triage": triage,
        "provision": provision.get("playbook", {}),
        "failover_thresholds": failover.get("thresholds", {}),
    }


def render_markdown(context: dict[str, Any]) -> str:
    return _markdown(context)


def render_html(context: dict[str, Any]) -> str:
    env = Environment(
        loader=FileSystemLoader(str(TEMPLATE_DIR)),
        autoescape=select_autoescape(["html", "xml"]),
    )
    template = env.get_template("report.html.j2")
    return template.render(**context)


def run_report() -> dict[str, Any]:
    context = build_report_context()
    markdown = render_markdown(context)
    html = render_html(context)

    md_path = paths.artifacts_dir() / "report.md"
    html_path = paths.artifacts_dir() / "report.html"
    md_path.write_text(markdown, encoding="utf-8")
    html_path.write_text(html, encoding="utf-8")

    triage_text = ""
    if context["triage"]:
        triage_text = "TRIAGE: " + "; ".join(
            f"{t['os_family']}/{t['case']} x{t['count']} ({t['suspected_cause']})"
            for t in context["triage"]
        )
    else:
        triage_text = "TRIAGE: no failures"

    failed = context["totals"]["failed"] > 0
    log_event(
        log, "report_done", failed=context["totals"]["failed"], total=context["totals"]["total"]
    )
    return {
        "html": str(html_path),
        "markdown": str(md_path),
        "triage": triage_text,
        "failed": failed,
        "context": context,
    }
