"""LabBench command-line interface.

Top-level commands::

    labbench lab up        # build & start the Docker lab
    labbench lab down      # tear the lab down
    labbench lab status    # show topology + per-service health
    labbench provision     # run the Ansible provisioning playbook
    labbench attach        # discover/login/mount iSCSI and verify multipath
    labbench collect       # fan out OS/disk collectors into the lab inventory
    labbench validate      # run the shared pytest validation suite
    labbench report        # render HTML/Markdown reports + triage summary
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any, Iterable, Sequence

import click

from . import __version__, paths
from .logging_setup import get_logger, setup_logging
from .topology import Lab, LabSchemaError, load_lab

log = get_logger("cli")

PASS = "PASS"
FAIL = "FAIL"


def _resolve_lab_path(lab_path: str | None) -> Path:
    if lab_path:
        return Path(lab_path)
    return paths.default_lab_path()


def _print_table(headers: Sequence[str], rows: Iterable[Sequence[Any]]) -> None:
    rows = [[str(c) for c in row] for row in rows]
    widths = [len(h) for h in headers]
    for row in rows:
        for i, cell in enumerate(row):
            widths[i] = max(widths[i], len(cell))
    fmt = "  ".join(f"{{:<{w}}}" for w in widths)
    click.echo(fmt.format(*headers))
    click.echo(fmt.format(*["-" * w for w in widths]))
    for row in rows:
        click.echo(fmt.format(*row))


def _load_lab_or_die(ctx: click.Context) -> Lab:
    path = _resolve_lab_path(ctx.obj.get("lab_path"))
    try:
        return load_lab(path)
    except LabSchemaError as exc:
        raise click.ClickException(f"invalid lab topology ({path}): {exc}") from exc


# --------------------------------------------------------------------------- #
# Root group
# --------------------------------------------------------------------------- #
@click.group(context_settings={"help_option_names": ["-h", "--help"]})
@click.version_option(__version__, prog_name="labbench")
@click.option(
    "--lab",
    "lab_path",
    default=None,
    help="Path to a lab topology YAML (default: labs/dev-lab.yml).",
)
@click.option("--log-level", default="INFO", show_default=True, help="Log level.")
@click.pass_context
def cli(ctx: click.Context, lab_path: str | None, log_level: str) -> None:
    """LabBench: cross-platform storage test-lab provisioner and validation harness."""

    setup_logging(log_level)
    ctx.ensure_object(dict)
    ctx.obj["lab_path"] = lab_path
    ctx.obj["log_level"] = log_level


# --------------------------------------------------------------------------- #
# lab up / down / status
# --------------------------------------------------------------------------- #
@cli.group()
def lab() -> None:
    """Manage the Dockerized mixed-OS storage lab."""


@lab.command("up")
@click.option("--timeout", default=180, show_default=True, help="Readiness timeout (seconds).")
@click.option("--no-build", is_flag=True, help="Reuse existing images.")
@click.pass_context
def lab_up(ctx: click.Context, timeout: int, no_build: bool) -> None:
    """Build and start the lab, waiting for every service to become healthy."""

    from .lab import up

    lab_obj = _load_lab_or_die(ctx)
    result = up(lab_obj, timeout=timeout, build=not no_build)
    click.echo(
        f"lab '{lab_obj.name}' up in {result['bringup_seconds']:.1f}s "
        f"(mode: {result['mode']})"
    )
    for svc in result["services"]:
        click.echo(f"  {svc['name']:<16} {svc['state']:<10} {svc['detail']}")


@lab.command("down")
@click.option("--volumes", is_flag=True, default=True, help="Remove named volumes (default).")
@click.option("--prune-images", is_flag=True, help="Also prune dangling images.")
@click.pass_context
def lab_down(ctx: click.Context, volumes: bool, prune_images: bool) -> None:
    """Tear the lab down and remove leftover containers/volumes."""

    from .lab import down

    lab_obj = _load_lab_or_die(ctx)
    result = down(lab_obj, volumes=volumes, prune_images=prune_images)
    click.echo(
        f"lab '{lab_obj.name}' down; removed {result['containers']} container(s), "
        f"{result['networks']} network(s), {result['volumes']} volume(s)"
    )


@lab.command("status")
@click.option("--json", "as_json", is_flag=True, help="Emit machine-readable JSON.")
@click.pass_context
def lab_status(ctx: click.Context, as_json: bool) -> None:
    """Show the parsed topology table and (if running) per-service health."""

    import json as _json

    lab_obj = _load_lab_or_die(ctx)
    from .lab import status as lab_status_fn

    status_data = lab_status_fn(lab_obj)

    if as_json:
        click.echo(_json.dumps(status_data, indent=2, default=str))
        return

    click.echo(f"Lab: {lab_obj.name}")
    click.echo("")
    click.echo("Hosts")
    _print_table(
        ["name", "os_family", "connection", "address", "port", "roles"],
        [
            [h.name, h.os_family, h.connection, h.address, h.port, ",".join(h.roles)]
            for h in lab_obj.hosts
        ],
    )
    click.echo("")
    click.echo("Targets")
    _print_table(
        ["name", "iqn", "portals", "luns"],
        [
            [t.name, t.iqn, "; ".join(t.portals), ",".join(str(l.id) for l in t.luns)]
            for t in lab_obj.targets
        ],
    )
    click.echo("")
    click.echo(f"Mode: {status_data.get('mode', 'unknown')}")
    click.echo(f"Lab compose state: {status_data.get('state', 'unknown')}")
    click.echo("")
    _print_table(
        ["service", "state", "health", "detail"],
        [
            [s["name"], s["state"], s.get("health", "-"), s.get("detail", "")]
            for s in status_data.get("services", [])
        ],
    )


# --------------------------------------------------------------------------- #
# provision / attach / collect / validate / report
# --------------------------------------------------------------------------- #
@cli.command()
@click.option("--limit", default=None, help="Ansible --limit pattern (group or host).")
@click.option("--check", is_flag=True, help="Run ansible-playbook in check mode.")
@click.pass_context
def provision(ctx: click.Context, limit: str | None, check: bool) -> None:
    """Provision host-side storage config with Ansible (SSH + WinRM)."""

    from .ansible_runner import run_provision

    lab_obj = _load_lab_or_die(ctx)
    result = run_provision(lab_obj, limit=limit, check=check)
    play = result["playbook"]
    click.echo(
        f"provision complete in {play['duration_seconds']:.1f}s: "
        f"changed={play['changed']} ok={play['ok']} failed={play['failed']} unreachable={play['unreachable']}"
    )
    click.echo(f"recap: {result['artifact']}")
    if play["failed"] or play["unreachable"]:
        raise SystemExit(3)


@cli.command()
@click.option("--host", "hosts", multiple=True, help="Limit to specific host name(s).")
@click.option("--size-mb", default=None, type=int, help="I/O verification size (default: config).")
@click.pass_context
def attach(ctx: click.Context, hosts: tuple[str, ...], size_mb: int | None) -> None:
    """Discover, login, mount iSCSI targets and verify multipath state."""

    from .workflows.attach import run_attach

    lab_obj = _load_lab_or_die(ctx)
    result = run_attach(lab_obj, only=set(hosts) or None, size_mb=size_mb)
    click.echo(f"attach: {result['ok']}/{result['total']} host(s) ready (mode: {result['mode']})")
    for h in result["hosts"]:
        click.echo(
            f"  {h['host']:<14} paths={h['active_paths']} "
            f"device={h.get('device', '-')} checksum={h.get('checksum', '-')}"
        )
    if result["ok"] != result["total"]:
        raise SystemExit(3)


@cli.command()
@click.option("--host", "hosts", multiple=True, help="Limit to specific host name(s).")
@click.pass_context
def collect(ctx: click.Context, hosts: tuple[str, ...]) -> None:
    """Collect OS/disk/driver inventory from every host."""

    from .collector import run_collect

    lab_obj = _load_lab_or_die(ctx)
    result = run_collect(lab_obj, only=set(hosts) or None)
    click.echo(f"inventory: {result['artifact']}")
    click.echo(f"markdown : {result['markdown']}")
    click.echo(f"drift findings: {result['drift_count']}")


@cli.command()
@click.option("--only", default=None, help="Run a subset of test files (comma-separated).")
@click.option("--host", "hosts", multiple=True, help="Limit to specific host name(s).")
@click.option("-n", "--numprocesses", default=None, help="pytest-xdist workers.")
@click.pass_context
def validate(
    ctx: click.Context,
    only: str | None,
    hosts: tuple[str, ...],
    numprocesses: str | None,
) -> None:
    """Run the shared cross-OS pytest validation suite."""

    from .validation import run_validation

    lab_obj = _load_lab_or_die(ctx)
    result = run_validation(
        lab_obj,
        only=[s.strip() for s in only.split(",")] if only else None,
        hosts=set(hosts) or None,
        numprocesses=numprocesses,
    )
    click.echo(f"JUnit XML: {result['junit']}")
    _print_table(
        ["host", "passed", "failed", "skipped", "total"],
        [
            [h["host"], h["passed"], h["failed"], h["skipped"], h["total"]]
            for h in result["matrix"]
        ],
    )
    click.echo(f"TOTAL: passed={result['passed']} failed={result['failed']} "
               f"skipped={result['skipped']} total={result['total']}")
    if result["failed"]:
        raise SystemExit(3)


@cli.command()
@click.option("--open", "open_report", is_flag=True, help="Print path for the HTML report.")
@click.pass_context
def report(ctx: click.Context, open_report: bool) -> None:
    """Render HTML/Markdown reports and a failure triage summary."""

    from .report import run_report

    result = run_report()
    click.echo(f"HTML: {result['html']}")
    click.echo(f"MD  : {result['markdown']}")
    if result.get("triage"):
        click.echo("")
        click.echo(result["triage"])
    if result["failed"]:
        raise SystemExit(3)


def main() -> None:
    """Entry point used by the ``labbench`` console script."""

    cli(prog_name="labbench")


if __name__ == "__main__":  # pragma: no cover
    main()
