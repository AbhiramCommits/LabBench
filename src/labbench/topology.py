"""Lab topology model and schema validation.

A lab is described by a small YAML document (see ``labs/dev-lab.yml``). This
module turns that document into typed dataclasses and raises
:class:`LabSchemaError` with actionable messages when the schema is wrong.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

OS_FAMILIES = {"linux", "windows"}

# Which remote connection types are valid for each OS family.
CONNECTIONS_BY_FAMILY: dict[str, set[str]] = {
    "linux": {"ssh", "local"},
    "windows": {"winrm"},
}

DEFAULT_CONNECTION = {"linux": "ssh", "windows": "winrm"}

# Default ports used by the Docker lab topology.
DEFAULT_PORT = {"ssh": 22, "winrm": 5985, "local": 0}


class LabSchemaError(ValueError):
    """Raised when a lab YAML document does not match the expected schema."""


def _require(obj: dict[str, Any], key: str, where: str) -> Any:
    if key not in obj or obj[key] in (None, ""):
        raise LabSchemaError(f"{where}: missing required field '{key}'")
    return obj[key]


def _as_str(value: Any, where: str) -> str:
    if not isinstance(value, str):
        raise LabSchemaError(f"{where}: expected a string, got {type(value).__name__}")
    return value


def _as_int(value: Any, where: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise LabSchemaError(f"{where}: expected an integer, got {type(value).__name__}")
    return value


@dataclass
class Lun:
    """A single LUN exported by a storage target."""

    id: int
    size_gb: int
    backstore: str = "fileio"

    @classmethod
    def from_dict(cls, data: dict[str, Any], where: str) -> "Lun":
        if not isinstance(data, dict):
            raise LabSchemaError(f"{where}: expected a mapping")
        return cls(
            id=_as_int(_require(data, "id", where), f"{where}.id"),
            size_gb=_as_int(_require(data, "size_gb", where), f"{where}.size_gb"),
            backstore=_as_str(data.get("backstore", "fileio"), f"{where}.backstore"),
        )


@dataclass
class Host:
    """A host under test (Linux or Windows)."""

    name: str
    os_family: str
    address: str
    user: str = "root"
    connection: str = "ssh"
    roles: list[str] = field(default_factory=list)
    tags: dict[str, Any] = field(default_factory=dict)

    @property
    def port(self) -> int:
        explicit = self.tags.get("port")
        if explicit is not None:
            return _as_int(explicit, f"hosts[{self.name}].tags.port")
        return DEFAULT_PORT.get(self.connection, 22)

    @property
    def is_linux(self) -> bool:
        return self.os_family == "linux"

    @property
    def is_windows(self) -> bool:
        return self.os_family == "windows"

    @property
    def endpoint(self) -> str:
        return f"{self.address}:{self.port}"

    @classmethod
    def from_dict(cls, data: dict[str, Any], where: str) -> "Host":
        if not isinstance(data, dict):
            raise LabSchemaError(f"{where}: expected a mapping")

        name = _as_str(_require(data, "name", where), f"{where}.name")
        os_family = _as_str(_require(data, "os_family", where), f"{where}.os_family")
        if os_family not in OS_FAMILIES:
            raise LabSchemaError(
                f"{where}: unsupported os_family {os_family!r}; "
                f"expected one of {sorted(OS_FAMILIES)}"
            )
        address = _as_str(_require(data, "address", where), f"{where}.address")
        user = _as_str(data.get("user", "root"), f"{where}.user")

        connection = data.get("connection") or DEFAULT_CONNECTION[os_family]
        connection = _as_str(connection, f"{where}.connection")
        allowed = CONNECTIONS_BY_FAMILY[os_family]
        if connection not in allowed:
            raise LabSchemaError(
                f"{where}: connection {connection!r} is not valid for os_family "
                f"{os_family!r}; expected one of {sorted(allowed)}"
            )

        roles = data.get("roles", []) or []
        if not isinstance(roles, list) or not all(isinstance(r, str) for r in roles):
            raise LabSchemaError(f"{where}.roles: expected a list of strings")

        tags = data.get("tags", {}) or {}
        if not isinstance(tags, dict):
            raise LabSchemaError(f"{where}.tags: expected a mapping")

        return cls(
            name=name,
            os_family=os_family,
            address=address,
            user=user,
            connection=connection,
            roles=list(roles),
            tags=dict(tags),
        )


@dataclass
class Target:
    """An iSCSI storage target with one or more LUNs and portals."""

    name: str
    iqn: str
    luns: list[Lun]
    portals: list[str]

    @property
    def path_count(self) -> int:
        return len(self.portals)

    @classmethod
    def from_dict(cls, data: dict[str, Any], where: str) -> "Target":
        if not isinstance(data, dict):
            raise LabSchemaError(f"{where}: expected a mapping")

        name = _as_str(_require(data, "name", where), f"{where}.name")
        iqn = _as_str(_require(data, "iqn", where), f"{where}.iqn")
        if not iqn.startswith("iqn.") and not iqn.startswith("naa."):
            raise LabSchemaError(
                f"{where}.iqn: {iqn!r} does not look like an iSCSI name (iqn.* / naa.*)"
            )

        raw_portals = _require(data, "portals", where)
        if not isinstance(raw_portals, list) or not raw_portals:
            raise LabSchemaError(f"{where}.portals: expected a non-empty list of host:port")
        portals: list[str] = []
        for i, portal in enumerate(raw_portals):
            portal = _as_str(portal, f"{where}.portals[{i}]")
            if ":" not in portal:
                raise LabSchemaError(
                    f"{where}.portals[{i}]: {portal!r} is missing a port (expected host:port)"
                )
            host, _, port = portal.rpartition(":")
            if not host or not port.isdigit():
                raise LabSchemaError(
                    f"{where}.portals[{i}]: {portal!r} is not a valid host:port portal"
                )
            portals.append(portal)

        raw_luns = data.get("luns")
        if not isinstance(raw_luns, list) or not raw_luns:
            raise LabSchemaError(f"{where}.luns: expected a non-empty list of LUNs")
        luns = [Lun.from_dict(l, f"{where}.luns[{i}]") for i, l in enumerate(raw_luns)]

        ids = [lun.id for lun in luns]
        if len(ids) != len(set(ids)):
            raise LabSchemaError(f"{where}.luns: duplicate LUN ids {ids}")

        return cls(name=name, iqn=iqn, luns=luns, portals=portals)


@dataclass
class Lab:
    """A complete lab: hosts under test plus storage targets."""

    name: str
    hosts: list[Host]
    targets: list[Target]
    raw: dict[str, Any] = field(default_factory=dict)
    meta: dict[str, Any] = field(default_factory=dict)

    @property
    def simulator_url(self) -> str:
        return str(self.meta.get("simulator_url", "http://127.0.0.1:8080")).rstrip("/")

    @property
    def networks(self) -> dict[str, str]:
        nets = self.meta.get("networks", {}) or {}
        return dict(nets)

    def host(self, name: str) -> Host:
        for h in self.hosts:
            if h.name == name:
                return h
        raise KeyError(f"no host named {name!r}")

    def target(self, name: str) -> Target:
        for t in self.targets:
            if t.name == name:
                return t
        raise KeyError(f"no target named {name!r}")

    def hosts_by_family(self, os_family: str) -> list[Host]:
        return [h for h in self.hosts if h.os_family == os_family]

    @classmethod
    def from_dict(cls, data: Any) -> "Lab":
        if not isinstance(data, dict):
            raise LabSchemaError("document root: expected a mapping")

        # Accept an optional top-level "lab" block, or a flat document.
        lab_meta = data.get("lab", {}) or {}
        if not isinstance(lab_meta, dict):
            raise LabSchemaError("lab: expected a mapping")
        name = _as_str(lab_meta.get("name", data.get("name", "lab")), "lab.name")

        raw_hosts = data.get("hosts")
        if not isinstance(raw_hosts, list) or not raw_hosts:
            raise LabSchemaError("hosts: expected a non-empty list of hosts")

        hosts: list[Host] = []
        seen: set[str] = set()
        for i, raw in enumerate(raw_hosts):
            host = Host.from_dict(raw, f"hosts[{i}]")
            if host.name in seen:
                raise LabSchemaError(f"hosts[{i}]: duplicate host name {host.name!r}")
            seen.add(host.name)
            hosts.append(host)

        raw_targets = data.get("targets", []) or []
        if not isinstance(raw_targets, list):
            raise LabSchemaError("targets: expected a list of targets")
        targets: list[Target] = []
        seen_t: set[str] = set()
        for i, raw in enumerate(raw_targets):
            target = Target.from_dict(raw, f"targets[{i}]")
            if target.name in seen_t:
                raise LabSchemaError(f"targets[{i}]: duplicate target name {target.name!r}")
            seen_t.add(target.name)
            targets.append(target)

        return cls(name=name, hosts=hosts, targets=targets, raw=data, meta=dict(lab_meta))


def load_lab_str(text: str) -> Lab:
    try:
        data = yaml.safe_load(text)
    except yaml.YAMLError as exc:  # pragma: no cover - passthrough
        raise LabSchemaError(f"could not parse YAML: {exc}") from exc
    return Lab.from_dict(data)


def load_lab(path: str | Path) -> Lab:
    p = Path(path)
    if not p.is_file():
        raise LabSchemaError(f"lab file not found: {p}")
    return load_lab_str(p.read_text(encoding="utf-8"))
