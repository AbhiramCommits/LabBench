#!/usr/bin/env python3
"""LabBench simulated iSCSI target + path layer.

This is a *real component*: it holds target/LUN/session/path state and models
multipath failover with wall-clock timers. It exposes a tiny HTTP/JSON API that
the LabBench drivers and the on-host sim agents speak to.

Modes
-----
``mode: real``
    A kernel LIO target is available (``/sys/kernel/config/target`` + targetcli).
    The simulator still serves the management API but path/session state is
    sourced from the real target stack by the host-side drivers.
``mode: simulated``
    The kernel iSCSI target stack is unavailable (typical inside Docker on
    macOS/CI). This process owns the full state machine and models:

    * ``path down <portal>``  -> I/O pauses for ``--failover-delay-ms``
      (multipath queueing / ``no_path_retry``) then resumes on the survivor.
    * ``path up <portal>``    -> path returns to service after
      ``--restore-delay-ms`` (multipath ``failback``).
"""

from __future__ import annotations

import argparse
import json
import os
import random
import shutil
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import parse_qs, urlparse

ACTIVE = "active"
FAILED = "failed"
RECOVERING = "recovering"
UP = "up"
DOWN = "down"


def detect_mode(force: str | None = None) -> str:
    """Return ``real`` when a kernel LIO target is usable, else ``simulated``."""

    if force in ("real", "simulated"):
        return force
    if os.environ.get("LABBENCH_MODE") in ("real", "simulated"):
        return os.environ["LABBENCH_MODE"]
    has_targetcli = shutil.which("targetcli") is not None
    has_configfs = os.path.isdir("/sys/kernel/config/target")
    if has_targetcli and has_configfs:
        return "real"
    return "simulated"


class Simulator:
    def __init__(
        self,
        mode: str,
        failover_delay_ms: float,
        restore_delay_ms: float,
        jitter: float = 0.15,
    ) -> None:
        self.mode = mode
        self.failover_delay_ms = failover_delay_ms
        self.restore_delay_ms = restore_delay_ms
        self.jitter = jitter
        self.started = time.time()
        self.lock = threading.RLock()
        self.targets: dict[str, dict[str, Any]] = {}
        self.portals: dict[str, dict[str, Any]] = {}
        self.sessions: dict[str, dict[str, Any]] = {}
        self.events: list[dict[str, Any]] = []

    # -- helpers ---------------------------------------------------------- #
    def _jitter(self, ms: float) -> float:
        lo, hi = 1.0 - self.jitter, 1.0 + self.jitter
        return ms * random.uniform(lo, hi)

    def add_portal(self, portal: str) -> None:
        address, _, port = portal.rpartition(":")
        with self.lock:
            self.portals[portal] = {
                "portal": portal,
                "address": address,
                "port": int(port or 3260),
                "state": UP,
                "down_at": None,
                "io_resume_at": None,
                "restore_at": None,
            }

    def add_target(self, name: str, iqn: str, luns: list[dict[str, Any]]) -> None:
        with self.lock:
            self.targets[name] = {"name": name, "iqn": iqn, "luns": luns}

    def _refresh(self, now: float) -> None:
        for p in self.portals.values():
            if p["state"] == RECOVERING and p["restore_at"] and now >= p["restore_at"]:
                p["state"] = UP
                p["restore_at"] = None
                self.events.append({"ts": now, "event": "path_restored", "portal": p["portal"]})

    def _path_state(self, p: dict[str, Any], now: float) -> str:
        if p["state"] == UP:
            return ACTIVE
        if p["state"] == RECOVERING:
            return RECOVERING
        return FAILED

    def paths(self, initiator: str | None) -> dict[str, Any]:
        now = time.time()
        with self.lock:
            self._refresh(now)
            out = []
            for portal, p in self.portals.items():
                if initiator:
                    sess = self.sessions.get(initiator, {})
                    if portal not in sess.get("portals", {}):
                        continue
                state = self._path_state(p, now)
                out.append({"portal": portal, "state": state})
            active = sum(1 for e in out if e["state"] in (ACTIVE, RECOVERING))
            return {"paths": out, "active_paths": active, "total_paths": len(out)}

    def probe(self, initiator: str | None) -> tuple[int, dict[str, Any]]:
        now = time.time()
        with self.lock:
            self._refresh(now)
            active = self.paths(initiator)["active_paths"]
            if active == 0:
                return 503, {"ok": False, "reason": "no-path", "active_paths": 0}
            pausing = any(
                p["state"] == DOWN and p["io_resume_at"] and now < p["io_resume_at"]
                for p in self.portals.values()
            )
            if pausing:
                resume_in = max(
                    (p["io_resume_at"] - now)
                    for p in self.portals.values()
                    if p["state"] == DOWN and p["io_resume_at"]
                )
                return 503, {
                    "ok": False,
                    "reason": "failover-in-progress",
                    "resume_in_ms": round(resume_in * 1000, 2),
                }
            return 200, {"ok": True, "active_paths": active, "ts": now}

    def path_down(self, portal: str, initiator: str | None = None) -> dict[str, Any]:
        now = time.time()
        with self.lock:
            p = self.portals.get(portal)
            if not p:
                raise KeyError(portal)
            p["state"] = DOWN
            p["down_at"] = now
            p["io_resume_at"] = now + self._jitter(self.failover_delay_ms) / 1000.0
            p["restore_at"] = None
            self.events.append(
                {"ts": now, "event": "path_down", "portal": portal, "initiator": initiator}
            )
            return {
                "portal": portal,
                "state": DOWN,
                "io_resume_at": p["io_resume_at"],
                "failover_delay_ms": round((p["io_resume_at"] - now) * 1000, 2),
            }

    def path_up(self, portal: str) -> dict[str, Any]:
        now = time.time()
        with self.lock:
            p = self.portals.get(portal)
            if not p:
                raise KeyError(portal)
            p["state"] = RECOVERING
            p["restore_at"] = now + self._jitter(self.restore_delay_ms) / 1000.0
            p["down_at"] = None
            p["io_resume_at"] = None
            self.events.append({"ts": now, "event": "path_up", "portal": portal})
            return {
                "portal": portal,
                "state": RECOVERING,
                "restore_at": p["restore_at"],
                "restore_delay_ms": round((p["restore_at"] - now) * 1000, 2),
            }

    def login(self, initiator: str, iqn: str, portal: str) -> dict[str, Any]:
        with self.lock:
            if portal not in self.portals:
                raise KeyError(portal)
            sess = self.sessions.setdefault(initiator, {"initiator": initiator, "iqn": iqn, "portals": {}})
            sess["iqn"] = iqn
            sess["portals"][portal] = {
                "portal": portal,
                "sid": uuid.uuid4().int % 100000,
                "state": "LOGGED_IN",
            }
            return {"initiator": initiator, "iqn": iqn, "portal": portal, "state": "LOGGED_IN"}

    def logout(self, initiator: str, portal: str | None = None) -> dict[str, Any]:
        with self.lock:
            if initiator not in self.sessions:
                return {"initiator": initiator, "logged_out": []}
            if portal:
                self.sessions[initiator]["portals"].pop(portal, None)
                return {"initiator": initiator, "logged_out": [portal]}
            self.sessions[initiator]["portals"].clear()
            return {"initiator": initiator, "logged_out": "all"}

    def sessions_view(self, initiator: str | None) -> dict[str, Any]:
        with self.lock:
            out = []
            for iqn_key, sess in self.sessions.items():
                if initiator and iqn_key != initiator:
                    continue
                for portal, info in sess["portals"].items():
                    out.append(
                        {
                            "initiator": iqn_key,
                            "iqn": sess["iqn"],
                            "portal": portal,
                            "sid": info["sid"],
                            "state": info["state"],
                        }
                    )
            return {"sessions": out}

    def reset(self) -> None:
        with self.lock:
            self.sessions.clear()
            for p in self.portals.values():
                p.update(
                    {"state": UP, "down_at": None, "io_resume_at": None, "restore_at": None}
                )
            self.events.clear()


def make_handler(sim: Simulator) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"
        server_version = "LabBenchISCSISim/1.0"

        def log_message(self, fmt: str, *args: Any) -> None:  # quieter logs
            if os.environ.get("LABBENCH_SIM_DEBUG"):
                super().log_message(fmt, *args)

        # -- plumbing ---------------------------------------------------- #
        def _send(self, code: int, payload: dict[str, Any]) -> None:
            body = json.dumps(payload, default=str).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _json_body(self) -> dict[str, Any]:
            length = int(self.headers.get("Content-Length") or 0)
            if not length:
                return {}
            raw = self.rfile.read(length)
            try:
                return json.loads(raw or b"{}")
            except json.JSONDecodeError:
                return {}

        def _query(self) -> dict[str, str]:
            return {k: v[-1] for k, v in parse_qs(urlparse(self.path).query).items()}

        # -- routes ------------------------------------------------------ #
        def do_GET(self) -> None:  # noqa: N802
            path = urlparse(self.path).path
            q = self._query()
            if path == "/health":
                self._send(
                    200,
                    {
                        "status": "ok",
                        "mode": sim.mode,
                        "uptime_s": round(time.time() - sim.started, 3),
                        "failover_delay_ms": sim.failover_delay_ms,
                        "restore_delay_ms": sim.restore_delay_ms,
                    },
                )
            elif path == "/mode":
                self._send(200, {"mode": sim.mode})
            elif path == "/targets":
                with sim.lock:
                    self._send(200, {"targets": list(sim.targets.values())})
            elif path == "/portals":
                with sim.lock:
                    self._send(200, {"portals": list(sim.portals.values())})
            elif path == "/sessions":
                self._send(200, sim.sessions_view(q.get("initiator")))
            elif path == "/paths":
                self._send(200, sim.paths(q.get("initiator")))
            elif path == "/io/probe":
                code, payload = sim.probe(q.get("initiator"))
                self._send(code, payload)
            elif path == "/events":
                with sim.lock:
                    self._send(200, {"events": sim.events})
            else:
                self._send(404, {"error": "not found", "path": path})

        def do_POST(self) -> None:  # noqa: N802
            path = urlparse(self.path).path
            body = self._json_body()
            try:
                if path == "/targets":
                    sim.add_target(
                        body["name"], body.get("iqn", ""), body.get("luns", [])
                    )
                    self._send(201, {"created": body["name"]})
                elif path == "/login":
                    self._send(
                        200,
                        sim.login(
                            body["initiator"], body.get("iqn", ""), body["portal"]
                        ),
                    )
                elif path == "/logout":
                    self._send(200, sim.logout(body["initiator"], body.get("portal")))
                elif path == "/path/down":
                    self._send(200, sim.path_down(body["portal"], body.get("initiator")))
                elif path == "/path/up":
                    self._send(200, sim.path_up(body["portal"]))
                elif path == "/reset":
                    sim.reset()
                    self._send(200, {"reset": True})
                else:
                    self._send(404, {"error": "not found", "path": path})
            except KeyError as exc:
                self._send(400, {"error": f"unknown key: {exc}"})

    return Handler


def setup_real_target(sim: Simulator) -> None:
    """Best-effort configuration of a *real* LIO target via targetcli.

    Only attempted when :func:`detect_mode` reports ``real``. Errors are logged
    and non-fatal so the management API still comes up.
    """

    import subprocess

    targetcli = shutil.which("targetcli")
    if not targetcli:
        print(json.dumps({"event": "real-setup-skipped", "reason": "no targetcli"}), flush=True)
        return
    target = next(iter(sim.targets.values()))
    iqn = target["iqn"]
    commands = []
    for lun in target["luns"]:
        name = f"{iqn.replace(':', '-')}-lun{lun['id']}"
        commands.append(
            f"backstores/fileio create {name} /var/lib/labbench/{name}.img {lun['size_gb']}G"
        )
    commands.append(f"iscsi/ create {iqn}")
    for lun in target["luns"]:
        name = f"{iqn.replace(':', '-')}-lun{lun['id']}"
        commands.append(f"iscsi/{iqn}/tpg1/luns create /backstores/fileio/{name}")
    for portal in sim.portals:
        addr, _, port = portal.partition(":")
        commands.append(f"iscsi/{iqn}/tpg1/portals create {addr} {port or 3260}")
    commands.append(f"iscsi/{iqn}/tpg1 set attribute authentication=0 demo_mode_write_protect=0")
    script = "\n".join(commands + ["saveconfig"])
    try:
        proc = subprocess.run(
            [targetcli], input=script, capture_output=True, text=True, timeout=120, check=False
        )
        print(
            json.dumps(
                {
                    "event": "real-setup",
                    "iqn": iqn,
                    "commands": len(commands),
                    "returncode": proc.returncode,
                    "stderr": (proc.stderr or "").strip()[-500:],
                }
            ),
            flush=True,
        )
    except Exception as exc:  # pragma: no cover - only on real hosts
        print(json.dumps({"event": "real-setup-failed", "error": str(exc)}), flush=True)


def build_sim_from_env() -> Simulator:
    mode = detect_mode()
    sim = Simulator(
        mode=mode,
        failover_delay_ms=float(os.environ.get("LABBENCH_FAILOVER_DELAY_MS", "2000")),
        restore_delay_ms=float(os.environ.get("LABBENCH_RESTORE_DELAY_MS", "1200")),
        jitter=float(os.environ.get("LABBENCH_JITTER", "0.15")),
    )
    portal_a = os.environ.get("LABBENCH_PORTAL_A", "172.28.0.10:3260")
    portal_b = os.environ.get("LABBENCH_PORTAL_B", "172.29.0.10:3260")
    for p in (portal_a, portal_b):
        sim.add_portal(p)
    sim.add_target(
        os.environ.get("LABBENCH_TARGET_NAME", "storage-target"),
        os.environ.get("LABBENCH_TARGET_IQN", "iqn.2026-01.labbench:storage.target1"),
        [
            {"id": 0, "size_gb": 8, "backstore": "fileio"},
            {"id": 1, "size_gb": 16, "backstore": "fileio"},
        ],
    )
    return sim


def main() -> None:
    parser = argparse.ArgumentParser(description="LabBench iSCSI target simulator")
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=int(os.environ.get("LABBENCH_SIM_PORT", "8080")))
    parser.add_argument("--mode", choices=["real", "simulated"], default=None)
    args = parser.parse_args()

    sim = build_sim_from_env()
    if args.mode:
        sim.mode = args.mode
    if sim.mode == "real":
        setup_real_target(sim)
    print(
        json.dumps(
            {
                "event": "startup",
                "component": "storage-target",
                "mode": sim.mode,
                "failover_delay_ms": sim.failover_delay_ms,
                "restore_delay_ms": sim.restore_delay_ms,
                "portals": list(sim.portals),
                "listen": f"{args.host}:{args.port}",
            }
        ),
        flush=True,
    )
    httpd = ThreadingHTTPServer((args.host, args.port), make_handler(sim))
    httpd.serve_forever()


if __name__ == "__main__":
    main()
