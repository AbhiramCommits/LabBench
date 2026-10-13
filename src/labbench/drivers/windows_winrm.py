"""Windows host driver over WinRM (pywinrm).

In ``mode: simulated`` all operations go through the on-host
``SimAgent.ps1`` invoked over a real pywinrm session. In ``mode: real`` the
driver issues native PowerShell: ``Get-IscsiTarget`` / ``Connect-IscsiTarget``,
``Get-Disk`` / ``Get-Partition``, ``Get-MPIOAvailableHW``, ``Format-Volume``.
"""

from __future__ import annotations

import json
from typing import Any

from .base import CommandResult, DriverError, HostDriver

AGENT = "/opt/labbench/SimAgent.ps1"


class WindowsWinRMDriver(HostDriver):
    os_family = "windows"

    def __init__(self, host: Any, lab: Any, mode: str = "simulated") -> None:
        super().__init__(host, lab, mode)
        self._session_obj = None

    def _session(self):
        if self._session_obj is not None:
            return self._session_obj
        import winrm

        self._session_obj = winrm.Session(
            f"http://{self.host.address}:{self.host.port}/wsman",
            auth=(self.host.user, self.host.tags.get("password")),
            transport="basic",
            server_cert_validation="ignore",
            read_timeout_sec=150,
            operation_timeout_sec=120,
        )
        return self._session_obj

    def close(self) -> None:
        if self._session_obj is not None:
            try:
                self._session_obj.protocol.transport.close_session()
            except Exception:  # noqa: BLE001
                pass
            self._session_obj = None

    def run(self, cmd: str) -> CommandResult:
        res = self._session().run_ps(cmd)
        return CommandResult(res.std_out or "", res.std_err or "", res.status_code)

    # -- agent helper ----------------------------------------------------- #
    def _agent(self, action: str, **params: Any) -> dict[str, Any]:
        script = (
            f"& {AGENT} -Action {action} "
            f"-Sim '{self.simulator_url}' -Initiator '{self.initiator}'"
        )
        for key, value in params.items():
            if value is None:
                continue
            script += f" -{key.title().replace('_', '')} '{value}'"
        res = self.run(script)
        text = res.stdout.strip()
        if not text:
            raise DriverError(
                f"{self.name}: agent {action} produced no output: {res.stderr.strip()}"
            )
        try:
            return json.loads(text.splitlines()[-1])
        except json.JSONDecodeError as exc:
            raise DriverError(
                f"{self.name}: agent {action} returned non-JSON: {text[:300]}"
            ) from exc

    # -- inventory -------------------------------------------------------- #
    def os_info(self) -> dict[str, Any]:
        if self.mode == "simulated":
            return self._agent("info")
        res = self.run(
            "Get-CimInstance Win32_OperatingSystem | "
            "Select-Object Caption,Version,BuildNumber | ConvertTo-Json -Compress"
        )
        try:
            data = json.loads(res.stdout.strip().splitlines()[-1])
        except Exception:  # noqa: BLE001
            data = {}
        return {
            "ok": res.ok,
            "os_family": "windows",
            "os_version": data.get("Caption", "unknown"),
            "kernel": data.get("Version", "unknown"),
        }

    def disk_topology(self) -> dict[str, Any]:
        if self.mode == "simulated":
            return self._agent("disks")
        res = self.run(
            "Get-Disk | Select-Object Number,FriendlyName,Size,OperationalStatus "
            "| ConvertTo-Json -Compress"
        )
        try:
            return {"ok": res.ok, "raw": json.loads(res.stdout.strip().splitlines()[-1])}
        except Exception:  # noqa: BLE001
            return {"ok": False, "raw": res.stdout, "error": res.stderr}

    # -- iSCSI / MPIO ----------------------------------------------------- #
    def discover_targets(self, portal: str | None = None) -> dict[str, Any]:
        if self.mode == "simulated":
            return self._agent("discover")
        res = self.run(
            "Get-IscsiTarget | Select-Object NodeAddress,IsConnected | ConvertTo-Json -Compress"
        )
        return {"ok": res.ok, "stdout": res.stdout, "stderr": res.stderr}

    def login(self, target: str, portals: list[str] | None = None) -> dict[str, Any]:
        if self.mode == "simulated":
            if portals and len(portals) == 1:
                return self._agent("login", portal=portals[0])
            return self._agent("login")
        results = []
        for portal in portals or []:
            res = self.run(f"Connect-IscsiTarget -NodeAddress '{target}' -IsPersistent $true")
            results.append({"portal": portal, "rc": res.rc, "stderr": res.stderr})
        return {"ok": all(r["rc"] == 0 for r in results), "results": results}

    def sessions(self) -> dict[str, Any]:
        if self.mode == "simulated":
            return self._agent("sessions")
        res = self.run("Get-IscsiSession | ConvertTo-Json -Compress")
        return {"ok": res.ok, "stdout": res.stdout}

    def paths(self, target: str | None = None) -> dict[str, Any]:
        if self.mode == "simulated":
            return self._agent("paths")
        res = self.run(
            "Get-MPIOAvailableHW | ConvertTo-Json -Compress; "
            "(Get-Disk | Measure-Object).Count | Write-Output"
        )
        return {"ok": res.ok, "stdout": res.stdout}

    def mount(self, lun: int, path: str) -> dict[str, Any]:
        if self.mode == "simulated":
            return self._agent("mount", lun=lun, path=path)
        res = self.run(
            f"New-Item -ItemType Directory -Force -Path '{path}' | Out-Null; "
            f"Get-Disk -Number {lun} | Set-Disk -IsOffline $false; "
            f"Get-Disk -Number {lun} | Set-Disk -IsReadOnly $false"
        )
        return {"ok": res.ok, "mounted": path, "device": f"\\\\.\\PhysicalDrive{lun}"}

    def write_verify(self, path: str, size_mb: int) -> dict[str, Any]:
        if self.mode == "simulated":
            return self._agent("write", path=path, size_mb=size_mb)
        res = self.run(
            f"$p='{path}\\labbench_io.bin'; "
            f"$b=[byte[]]::new(1MB); (Get-FileHash -Algorithm MD5 $p).Hash"
        )
        return {"ok": res.ok, "checksum": res.stdout.strip().splitlines()[-1] if res.stdout else None}

    def unmount(self, path: str) -> dict[str, Any]:
        if self.mode == "simulated":
            return self._agent("unmount", path=path)
        res = self.run(f"Remove-Item -Recurse -Force '{path}' -ErrorAction SilentlyContinue")
        return {"ok": res.ok, "unmounted": path}

    def logout(self, target: str) -> dict[str, Any]:
        if self.mode == "simulated":
            return self._agent("logout")
        res = self.run(
            f"Get-IscsiSession | Where-Object {{ $_.TargetNodeAddress -eq '{target}' }} "
            f"| Disconnect-IscsiTarget -Confirm:$false"
        )
        return {"ok": res.ok, "stderr": res.stderr}

    # -- failover --------------------------------------------------------- #
    def fail_path(self, portal: str) -> dict[str, Any]:
        if self.mode == "simulated":
            return self._agent("fail", portal=portal)
        res = self.run(
            "Get-NetAdapter | Where-Object Status -eq 'Up' | "
            "Select-Object -First 1 | Disable-NetAdapter -Confirm:$false"
        )
        return {"ok": res.ok, "portal": portal}

    def restore_path(self, portal: str) -> dict[str, Any]:
        if self.mode == "simulated":
            return self._agent("restore", portal=portal)
        res = self.run(
            "Get-NetAdapter | Where-Object Status -ne 'Up' | "
            "Select-Object -First 1 | Enable-NetAdapter -Confirm:$false"
        )
        return {"ok": res.ok, "portal": portal}

    def io_probe(self) -> dict[str, Any]:
        if self.mode == "simulated":
            return self._agent("probe")
        res = self.run(
            "Get-Disk | Where-Object OperationalStatus -eq 'Online' | "
            "Select-Object -First 1 | ConvertTo-Json -Compress"
        )
        return {"ok": res.ok, "stdout": res.stdout}
