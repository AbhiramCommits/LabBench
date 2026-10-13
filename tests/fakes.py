"""A deterministic in-memory HostDriver used by unit tests.

Models a two-path multipath device with configurable failover/restore delays
and optional checksum corruption. Timing is real (monotonic), so the failover
workflow measures genuine elapsed times.
"""

from __future__ import annotations

import time
from typing import Any

from labbench.drivers.base import CommandResult, HostDriver


class FakeDriver(HostDriver):
    def __init__(
        self,
        host: Any,
        lab: Any = None,
        mode: str = "simulated",
        failover_delay: float = 0.15,
        restore_delay: float = 0.08,
        start_paths: int = 2,
        corrupt: bool = False,
    ) -> None:
        super().__init__(host, lab, mode)
        self.failover_delay = failover_delay
        self.restore_delay = restore_delay
        self.corrupt = corrupt
        self.state_a = "active" if start_paths >= 2 else "failed"
        self.state_b = "active" if start_paths >= 1 else "failed"
        self.down_at: float | None = None
        self.io_resume_at: float | None = None
        self.restore_at: float | None = None
        self.checksum = "baseline-checksum-0000"
        self._write_calls = 0
        self.closed = False

    # -- low level -------------------------------------------------------- #
    def run(self, cmd: str) -> CommandResult:
        return CommandResult("", "", 0)

    def _refresh(self) -> None:
        if self.state_a == "recovering" and self.restore_at and time.monotonic() >= self.restore_at:
            self.state_a = "active"
            self.restore_at = None

    # -- inventory -------------------------------------------------------- #
    def os_info(self) -> dict[str, Any]:
        return {
            "ok": True,
            "os_family": self.os_family,
            "os_version": "FakeOS 1.0",
            "kernel": "6.0",
            "cpu_count": 2,
            "mem_mb": 1024,
            "mode": self.mode,
        }

    def disk_topology(self) -> dict[str, Any]:
        return {"ok": True, "disks": [{"name": "mpatha", "path_count": self.active_paths()}]}

    # -- iSCSI / multipath ----------------------------------------------- #
    def discover_targets(self, portal: str | None = None) -> dict[str, Any]:
        return {
            "ok": True,
            "targets": [{"iqn": "iqn.2026-01.labbench:fake", "portals": ["pA", "pB"]}],
        }

    def login(self, target: str, portals: list[str] | None = None) -> dict[str, Any]:
        return {"ok": True, "logged_in": portals or ["pA", "pB"]}

    def sessions(self) -> dict[str, Any]:
        return {"ok": True, "sessions": [{"portal": "pA"}, {"portal": "pB"}]}

    def paths(self, target: str | None = None) -> dict[str, Any]:
        self._refresh()
        states = [("pA", self.state_a), ("pB", self.state_b)]
        active = sum(1 for _, s in states if s == "active")
        return {
            "ok": True,
            "paths": [{"portal": p, "state": s} for p, s in states],
            "active_paths": active,
            "total_paths": len(states),
        }

    def mount(self, lun: int, path: str) -> dict[str, Any]:
        return {"ok": True, "mounted": path, "device": f"/dev/mapper/mpath{chr(ord('a') + lun)}"}

    def write_verify(self, path: str, size_mb: int) -> dict[str, Any]:
        self._write_calls += 1
        checksum = (
            "corrupted-checksum-9999"
            if (self.corrupt and self._write_calls >= 2)
            else self.checksum
        )
        return {
            "ok": checksum == self.checksum,
            "bytes": size_mb * 1024 * 1024,
            "size_mb": size_mb,
            "seconds": 0.05,
            "mbps": 1280.0,
            "checksum": checksum,
            "verify_checksum": checksum,
            "path": path,
        }

    def unmount(self, path: str) -> dict[str, Any]:
        return {"ok": True, "unmounted": path}

    def logout(self, target: str) -> dict[str, Any]:
        return {"ok": True}

    # -- failover --------------------------------------------------------- #
    def fail_path(self, portal: str) -> dict[str, Any]:
        self.state_a = "failed"
        self.down_at = time.monotonic()
        self.io_resume_at = self.down_at + self.failover_delay
        self.restore_at = None
        return {"ok": True, "portal": portal, "state": "down"}

    def restore_path(self, portal: str) -> dict[str, Any]:
        self.state_a = "recovering"
        self.restore_at = time.monotonic() + self.restore_delay
        return {"ok": True, "portal": portal, "state": "recovering"}

    def io_probe(self) -> dict[str, Any]:
        self._refresh()
        if self.state_a == "failed" and self.state_b == "failed":
            return {"ok": False, "reason": "no-path"}
        if (
            self.state_a == "failed"
            and self.io_resume_at is not None
            and time.monotonic() < self.io_resume_at
        ):
            return {"ok": False, "reason": "failover-in-progress"}
        return {"ok": True, "active_paths": self.paths()["active_paths"]}

    def close(self) -> None:
        self.closed = True
