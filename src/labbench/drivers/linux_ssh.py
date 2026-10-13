"""Linux host driver over SSH (paramiko).

In ``mode: simulated`` every operation is delegated to the on-host
``sim_agent.py`` (which speaks the LabBench simulator API). In ``mode: real``
the driver uses the native tooling: ``iscsiadm``, ``multipath``, ``lsblk``,
``mkfs``, ``mount`` and ``dd``.
"""

from __future__ import annotations

import json
import re
from typing import Any

from .base import CommandResult, DriverError, HostDriver

AGENT = "/opt/labbench/sim_agent.py"


class LinuxSSHDriver(HostDriver):
    os_family = "linux"

    def __init__(self, host: Any, lab: Any, mode: str = "simulated") -> None:
        super().__init__(host, lab, mode)
        self._client_obj = None

    # -- connection ------------------------------------------------------- #
    def _client(self):
        if self._client_obj is not None:
            return self._client_obj
        import paramiko

        client = paramiko.SSHClient()
        client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        client.connect(
            hostname=self.host.address,
            port=self.host.port,
            username=self.host.user,
            password=self.host.tags.get("password"),
            timeout=15,
            banner_timeout=30,
            auth_timeout=30,
            look_for_keys=False,
            allow_agent=False,
        )
        self._client_obj = client
        return client

    def close(self) -> None:
        if self._client_obj is not None:
            self._client_obj.close()
            self._client_obj = None

    def run(self, cmd: str) -> CommandResult:
        client = self._client()
        stdin, stdout, stderr = client.exec_command(cmd, timeout=300)
        out = stdout.read().decode("utf-8", errors="replace")
        err = stderr.read().decode("utf-8", errors="replace")
        rc = stdout.channel.recv_exit_status()
        return CommandResult(out, err, rc)

    # -- agent helper ----------------------------------------------------- #
    def _agent(self, action: str, **params: Any) -> dict[str, Any]:
        parts = [
            f"python3 {AGENT} {action}",
            f"--sim {self.simulator_url}",
            f"--initiator {self.initiator}",
        ]
        for key, value in params.items():
            if value is None:
                continue
            parts.append(f"--{key.replace('_', '-')} {value}")
        res = self.run(" ".join(parts))
        text = res.stdout.strip()
        if not text:
            raise DriverError(f"{self.name}: agent {action} produced no output: {res.stderr.strip()}")
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
        res = self.run(". /etc/os-release; echo \"$PRETTY_NAME\"; uname -r; nproc")
        lines = res.stdout.strip().splitlines()
        return {
            "ok": res.ok,
            "os_family": "linux",
            "os_version": lines[0] if lines else "unknown",
            "kernel": lines[1] if len(lines) > 1 else "unknown",
            "cpu_count": int(lines[2]) if len(lines) > 2 and lines[2].isdigit() else 0,
        }

    def disk_topology(self) -> dict[str, Any]:
        if self.mode == "simulated":
            return self._agent("disks")
        res = self.run("lsblk -J -o NAME,SIZE,TYPE,MODEL,MOUNTPOINT")
        try:
            return {"ok": res.ok, "raw": json.loads(res.stdout or "{}")}
        except json.JSONDecodeError:
            return {"ok": False, "raw": res.stdout, "error": res.stderr}

    # -- iSCSI / multipath ----------------------------------------------- #
    def discover_targets(self, portal: str | None = None) -> dict[str, Any]:
        if self.mode == "simulated":
            return self._agent("discover")
        if not portal:
            raise DriverError(f"{self.name}: portal required for real discovery")
        res = self.run(f"iscsiadm -m discovery -t sendtargets -p {portal}")
        return {"ok": res.ok, "stdout": res.stdout, "stderr": res.stderr}

    def login(self, target: str, portals: list[str] | None = None) -> dict[str, Any]:
        if self.mode == "simulated":
            if portals and len(portals) == 1:
                return self._agent("login", portal=portals[0])
            return self._agent("login")
        results = []
        for portal in portals or []:
            res = self.run(f"iscsiadm -m node -T {target} -p {portal} --login")
            results.append({"portal": portal, "rc": res.rc, "stderr": res.stderr})
        return {"ok": all(r["rc"] == 0 for r in results), "results": results}

    def sessions(self) -> dict[str, Any]:
        if self.mode == "simulated":
            return self._agent("sessions")
        res = self.run("iscsiadm -m session")
        return {"ok": res.ok, "stdout": res.stdout}

    def paths(self, target: str | None = None) -> dict[str, Any]:
        if self.mode == "simulated":
            return self._agent("paths")
        res = self.run("multipath -ll")
        active = len(re.findall(r"\\_ \\d+:\\d+:\\d+:\\d+", res.stdout)) or len(
            re.findall(r"running", res.stdout)
        )
        return {"ok": res.ok, "stdout": res.stdout, "active_paths": active, "total_paths": active}

    def mount(self, lun: int, path: str) -> dict[str, Any]:
        if self.mode == "simulated":
            return self._agent("mount", lun=lun, path=path)
        device = f"/dev/mapper/mpath{chr(ord('a') + lun)}"
        res = self.run(
            f"mkdir -p {path} && (blkid {device} || mkfs.ext4 -F {device}) && mount {device} {path}"
        )
        return {"ok": res.ok, "mounted": path, "device": device, "stderr": res.stderr}

    def write_verify(self, path: str, size_mb: int) -> dict[str, Any]:
        if self.mode == "simulated":
            return self._agent("write", path=path, size_mb=size_mb)
        res = self.run(
            f"cd {path} && dd if=/dev/zero of=labbench_io.bin bs=1M count={size_mb} "
            f"conv=fdatasync && md5sum labbench_io.bin"
        )
        checksum = res.stdout.strip().split()[0] if res.stdout.strip() else None
        return {"ok": res.ok, "checksum": checksum, "size_mb": size_mb}

    def unmount(self, path: str) -> dict[str, Any]:
        if self.mode == "simulated":
            return self._agent("unmount", path=path)
        res = self.run(f"umount {path} || true")
        return {"ok": res.ok, "unmounted": path}

    def logout(self, target: str) -> dict[str, Any]:
        if self.mode == "simulated":
            return self._agent("logout")
        res = self.run(f"iscsiadm -m node -T {target} --logout")
        return {"ok": res.ok, "stderr": res.stderr}

    # -- failover --------------------------------------------------------- #
    def fail_path(self, portal: str) -> dict[str, Any]:
        if self.mode == "simulated":
            return self._agent("fail", portal=portal)
        # Real: take the interface carrying the portal offline.
        addr = portal.split(":")[0]
        res = self.run(
            f"dev=$(ip route get {addr} | awk '{{print $5}}'); ip link set $dev down; echo $dev"
        )
        return {"ok": res.ok, "portal": portal, "device": res.stdout.strip()}

    def restore_path(self, portal: str) -> dict[str, Any]:
        if self.mode == "simulated":
            return self._agent("restore", portal=portal)
        addr = portal.split(":")[0]
        res = self.run(f"dev=$(ip route get {addr} | awk '{{print $5}}'); ip link set $dev up; echo $dev")
        return {"ok": res.ok, "portal": portal, "device": res.stdout.strip()}

    def io_probe(self) -> dict[str, Any]:
        if self.mode == "simulated":
            return self._agent("probe")
        res = self.run("dd if=/dev/mapper/mpatha of=/dev/null bs=4k count=1 iflag=direct")
        return {"ok": res.ok, "stderr": res.stderr}
