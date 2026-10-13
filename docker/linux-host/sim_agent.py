#!/usr/bin/env python3
"""LabBench on-host storage agent (Linux).

The SSH/paramiko driver invokes this script remotely. It translates the
driver's OS-agnostic actions into either real iSCSI tooling (``iscsiadm``,
``multipath``, ``lsblk``) or calls into the LabBench simulator when running in
``mode: simulated``. All output is JSON on stdout.

Usage::

    sim_agent.py <action> [--sim URL] [--initiator IQN] [--portal H:P]
                  [--lun N] [--path DIR] [--device DEV] [--size-mb N]
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import shutil
import subprocess
import time
import urllib.error
import urllib.request
from typing import Any


def http_get(url: str, timeout: float = 5.0) -> tuple[int, dict[str, Any]]:
    req = urllib.request.Request(url, headers={"Accept": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, json.loads(resp.read() or b"{}")
    except urllib.error.HTTPError as exc:  # pragma: no cover - depends on sim
        try:
            return exc.code, json.loads(exc.read() or b"{}")
        except Exception:
            return exc.code, {"ok": False, "reason": "http-error"}


def http_post(url: str, payload: dict[str, Any], timeout: float = 10.0) -> dict[str, Any]:
    data = json.dumps(payload).encode()
    req = urllib.request.Request(
        url, data=data, headers={"Content-Type": "application/json"}, method="POST"
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read() or b"{}")


def read_os_release() -> dict[str, str]:
    out: dict[str, str] = {}
    try:
        with open("/etc/os-release", encoding="utf-8") as fh:
            for line in fh:
                if "=" in line:
                    k, _, v = line.strip().partition("=")
                    out[k] = v.strip('"')
    except OSError:
        pass
    return out


def tool_version(cmd: list[str]) -> str:
    exe = shutil.which(cmd[0])
    if not exe:
        return "not-installed"
    try:
        proc = subprocess.run(
            cmd, capture_output=True, text=True, timeout=5, check=False
        )
        text = (proc.stdout or proc.stderr).strip().splitlines()
        return text[0] if text else "unknown"
    except Exception:
        return "unknown"


def total_mem_mb() -> int:
    try:
        with open("/proc/meminfo", encoding="utf-8") as fh:
            for line in fh:
                if line.startswith("MemTotal:"):
                    return int(line.split()[1]) // 1024
    except OSError:
        pass
    return 0


def action_info(args: argparse.Namespace) -> dict[str, Any]:
    code, mode = http_get(f"{args.sim}/mode")
    return {
        "ok": True,
        "hostname": platform.node(),
        "os_family": "linux",
        "os_version": read_os_release().get("PRETTY_NAME", platform.platform()),
        "kernel": platform.release(),
        "cpu_count": os.cpu_count(),
        "mem_mb": total_mem_mb(),
        "initiator": args.initiator,
        "mode": mode.get("mode", "simulated"),
        "drivers": {
            "iscsiadm": tool_version(["iscsiadm", "--version"]),
            "multipathd": tool_version(["multipathd", "-v"]),
            "sg3_utils": tool_version(["sg_inq", "--version"]),
        },
    }


def action_disks(args: argparse.Namespace) -> dict[str, Any]:
    code, paths = http_get(f"{args.sim}/paths?initiator={args.initiator}")
    count = paths.get("total_paths", 2)
    return {
        "ok": True,
        "disks": [
            {
                "name": "mpatha",
                "type": "mpath",
                "size_gb": 8,
                "model": "LabBench iSCSI LUN",
                "path_count": count,
                "paths": [p["portal"] for p in paths.get("paths", [])],
                "mountpoint": args.mount_root or "/mnt/labbench",
            }
        ],
        "multipath": {
            "path_grouping_policy": "failover",
            "no_path_retry": 12,
            "failback": "immediate",
        },
    }


def action_discover(args: argparse.Namespace) -> dict[str, Any]:
    code, data = http_get(f"{args.sim}/targets")
    return {"ok": True, "targets": data.get("targets", [])}


def action_login(args: argparse.Namespace) -> dict[str, Any]:
    code, targets = http_get(f"{args.sim}/targets")
    tgt = (targets.get("targets") or [{}])[0]
    portals = [args.portal] if args.portal else [p for p in _target_portals(args)]
    results = []
    for portal in portals:
        results.append(
            http_post(
                f"{args.sim}/login",
                {"initiator": args.initiator, "iqn": tgt.get("iqn", ""), "portal": portal},
            )
        )
    return {"ok": True, "logged_in": [r.get("portal") for r in results], "results": results}


def _target_portals(args: argparse.Namespace) -> list[str]:
    code, portals = http_get(f"{args.sim}/portals")
    return [p["portal"] for p in portals.get("portals", [])]


def action_sessions(args: argparse.Namespace) -> dict[str, Any]:
    code, data = http_get(f"{args.sim}/sessions?initiator={args.initiator}")
    return {"ok": True, **data}


def action_paths(args: argparse.Namespace) -> dict[str, Any]:
    code, data = http_get(f"{args.sim}/paths?initiator={args.initiator}")
    return {"ok": True, **data}


def action_probe(args: argparse.Namespace) -> dict[str, Any]:
    start = time.perf_counter()
    code, data = http_get(f"{args.sim}/io/probe?initiator={args.initiator}")
    latency_ms = (time.perf_counter() - start) * 1000.0
    data["http_status"] = code
    data["ok"] = code == 200
    data["latency_ms"] = round(latency_ms, 3)
    return data


def action_fail(args: argparse.Namespace) -> dict[str, Any]:
    return {"ok": True, **http_post(f"{args.sim}/path/down", {"portal": args.portal, "initiator": args.initiator})}


def action_restore(args: argparse.Namespace) -> dict[str, Any]:
    return {"ok": True, **http_post(f"{args.sim}/path/up", {"portal": args.portal})}


def action_logout(args: argparse.Namespace) -> dict[str, Any]:
    return {"ok": True, **http_post(f"{args.sim}/logout", {"initiator": args.initiator})}


def action_mount(args: argparse.Namespace) -> dict[str, Any]:
    mountpoint = args.path or os.path.join(args.mount_root or "/mnt/labbench", f"lun{args.lun}")
    os.makedirs(mountpoint, exist_ok=True)
    return {
        "ok": True,
        "mounted": mountpoint,
        "device": args.device or f"/dev/mapper/mpath{chr(ord('a') + args.lun)}",
    }


def action_unmount(args: argparse.Namespace) -> dict[str, Any]:
    mountpoint = args.path or os.path.join(args.mount_root or "/mnt/labbench", f"lun{args.lun}")
    keep = os.environ.get("LABBENCH_KEEP_MOUNT") == "1"
    if not keep and os.path.isdir(mountpoint):
        shutil.rmtree(mountpoint, ignore_errors=True)
    return {"ok": True, "unmounted": mountpoint}


def action_write(args: argparse.Namespace) -> dict[str, Any]:
    directory = args.path or os.path.join(args.mount_root or "/mnt/labbench", f"lun{args.lun}")
    os.makedirs(directory, exist_ok=True)
    target = os.path.join(directory, "labbench_io.bin")
    size_mb = args.size_mb
    chunk = b"\xa5" * (1024 * 1024)
    digest = hashlib.md5()
    start = time.perf_counter()
    with open(target, "wb") as fh:
        for _ in range(size_mb):
            fh.write(chunk)
            digest.update(chunk)
        fh.flush()
        os.fsync(fh.fileno())
    seconds = time.perf_counter() - start
    stat = os.stat(target)
    # Read back and verify.
    verify = hashlib.md5()
    with open(target, "rb") as fh:
        while True:
            block = fh.read(4 * 1024 * 1024)
            if not block:
                break
            verify.update(block)
    return {
        "ok": verify.hexdigest() == digest.hexdigest(),
        "bytes": stat.st_size,
        "size_mb": size_mb,
        "seconds": round(seconds, 4),
        "mbps": round(size_mb / seconds, 2) if seconds > 0 else None,
        "checksum": digest.hexdigest(),
        "verify_checksum": verify.hexdigest(),
        "path": target,
    }


ACTIONS = {
    "info": action_info,
    "disks": action_disks,
    "discover": action_discover,
    "login": action_login,
    "sessions": action_sessions,
    "paths": action_paths,
    "probe": action_probe,
    "fail": action_fail,
    "restore": action_restore,
    "logout": action_logout,
    "mount": action_mount,
    "unmount": action_unmount,
    "write": action_write,
}


def main() -> None:
    parser = argparse.ArgumentParser(description="LabBench Linux storage agent")
    parser.add_argument("action", choices=sorted(ACTIONS))
    parser.add_argument("--sim", default=os.environ.get("LABBENCH_SIMULATOR", "http://storage-target:8080"))
    parser.add_argument("--initiator", default=os.environ.get("LABBENCH_INITIATOR", ""))
    parser.add_argument("--portal", default=None)
    parser.add_argument("--lun", type=int, default=0)
    parser.add_argument("--path", default=None)
    parser.add_argument("--device", default=None)
    parser.add_argument("--size-mb", type=int, default=64)
    parser.add_argument("--mount-root", default="/mnt/labbench")
    args = parser.parse_args()
    try:
        result = ACTIONS[args.action](args)
    except Exception as exc:  # surface as JSON so the driver can classify it
        result = {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
    print(json.dumps(result, default=str))


if __name__ == "__main__":
    main()
