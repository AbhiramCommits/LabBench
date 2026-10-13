"""Driver factory: pick the right driver for a host.

The lab-wide mode is probed once from the storage management API so every host
reports the same ``mode: real`` / ``mode: simulated``.
"""

from __future__ import annotations

import json
import urllib.request
from typing import Any

from ..logging_setup import get_logger, log_event
from .base import HostDriver
from .linux_ssh import LinuxSSHDriver
from .windows_winrm import WindowsWinRMDriver

log = get_logger("drivers")


def detect_mode(lab: Any) -> str:
    """Return the lab-wide iSCSI mode (``real`` or ``simulated``)."""

    try:
        with urllib.request.urlopen(f"{lab.simulator_url}/mode", timeout=3) as resp:
            mode = json.loads(resp.read()).get("mode", "simulated")
    except Exception:  # noqa: BLE001
        # No management API -> assume a real hardware lab.
        mode = "real"
    log_event(log, "detect_mode", mode=mode)
    return mode


def driver_for(host: Any, lab: Any, mode: str | None = None) -> HostDriver:
    mode = mode or detect_mode(lab)
    if host.is_windows:
        return WindowsWinRMDriver(host, lab, mode)
    return LinuxSSHDriver(host, lab, mode)
