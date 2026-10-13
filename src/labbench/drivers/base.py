"""OS-agnostic host driver abstraction.

Every method is expressed in storage-test terms (targets, sessions, paths,
mount, write/verify) so the same workflow -- and the same pytest suite -- runs
against Linux (SSH) and Windows (WinRM) hosts.
"""

from __future__ import annotations

import time
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any


class DriverError(RuntimeError):
    """Raised when a driver operation fails."""


@dataclass
class CommandResult:
    stdout: str
    stderr: str
    rc: int

    @property
    def ok(self) -> bool:
        return self.rc == 0


class HostDriver(ABC):
    """Abstract base class for host drivers."""

    os_family: str = "unknown"

    def __init__(self, host: Any, lab: Any, mode: str = "simulated") -> None:
        self.host = host
        self.lab = lab
        self.mode = mode

    @property
    def name(self) -> str:
        return self.host.name

    @property
    def initiator(self) -> str:
        return self.host.tags.get("initiator", f"iqn.2026-01.labbench:{self.host.name}")

    @property
    def simulator_url(self) -> str:
        """URL the *host* (inside the lab network) should use for the simulator."""

        host_url = self.host.tags.get("simulator_url")
        if host_url:
            return host_url
        internal = self.lab.meta.get("simulator_url_internal")
        if internal:
            return internal
        return self.lab.simulator_url

    # -- low level -------------------------------------------------------- #
    @abstractmethod
    def run(self, cmd: str) -> CommandResult:
        """Run a command/script on the host and return its output."""

    # -- inventory -------------------------------------------------------- #
    @abstractmethod
    def os_info(self) -> dict[str, Any]:
        ...

    @abstractmethod
    def disk_topology(self) -> dict[str, Any]:
        ...

    # -- iSCSI / multipath ----------------------------------------------- #
    @abstractmethod
    def discover_targets(self, portal: str | None = None) -> dict[str, Any]:
        ...

    @abstractmethod
    def login(self, target: str, portals: list[str] | None = None) -> dict[str, Any]:
        ...

    @abstractmethod
    def sessions(self) -> dict[str, Any]:
        ...

    @abstractmethod
    def paths(self, target: str | None = None) -> dict[str, Any]:
        ...

    @abstractmethod
    def mount(self, lun: int, path: str) -> dict[str, Any]:
        ...

    @abstractmethod
    def write_verify(self, path: str, size_mb: int) -> dict[str, Any]:
        ...

    @abstractmethod
    def unmount(self, path: str) -> dict[str, Any]:
        ...

    @abstractmethod
    def logout(self, target: str) -> dict[str, Any]:
        ...

    # -- failover --------------------------------------------------------- #
    @abstractmethod
    def fail_path(self, portal: str) -> dict[str, Any]:
        ...

    @abstractmethod
    def restore_path(self, portal: str) -> dict[str, Any]:
        ...

    @abstractmethod
    def io_probe(self) -> dict[str, Any]:
        ...

    def close(self) -> None:  # pragma: no cover - default no-op
        ...

    def utc_epoch(self) -> int:
        """Return the host's current UTC time as a Unix epoch (for time sync)."""

        if self.os_family == "windows":
            res = self.run("[DateTimeOffset]::UtcNow.ToUnixTimeSeconds()")
        else:
            res = self.run("date +%s")
        text = (res.stdout or "").strip().splitlines()
        for line in reversed(text):
            line = line.strip()
            if line.lstrip("-").isdigit():
                return int(line)
        raise DriverError(f"{self.name}: could not read host time: {res.stderr!r}")

    # -- shared helpers --------------------------------------------------- #
    def active_paths(self, target: str | None = None) -> int:
        return int(self.paths(target).get("active_paths", 0))

    def wait_for_active_paths(
        self, expected: int, timeout_s: float = 15.0, poll_s: float = 0.02
    ) -> float:
        """Wait until ``expected`` paths are active; return elapsed seconds.

        Raises :class:`DriverError` on timeout.
        """

        start = time.monotonic()
        deadline = start + timeout_s
        while time.monotonic() < deadline:
            data = self.paths()
            states = [p.get("state") for p in data.get("paths", [])]
            if len(states) >= expected and all(s == "active" for s in states):
                return time.monotonic() - start
            time.sleep(poll_s)
        raise DriverError(
            f"{self.name}: only {self.active_paths()} path(s) active after {timeout_s}s "
            f"(states={[p.get('state') for p in self.paths().get('paths', [])]})"
        )
