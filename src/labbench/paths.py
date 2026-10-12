"""Repository path helpers.

LabBench is installed in editable mode, so at runtime the package may live in
``<repo>/src/labbench`` while the user's current directory can be anywhere.
These helpers locate the repository root (the directory holding
``pyproject.toml``) and the well-known subdirectories used across the tool.
"""

from __future__ import annotations

import os
from pathlib import Path


def repo_root() -> Path:
    """Return the repository root directory.

    Resolution order:
    1. ``LABBENCH_ROOT`` environment variable, if set.
    2. Walk upward from the current working directory looking for ``pyproject.toml``.
    3. Walk upward from this source file (works for editable installs).
    """

    env = os.environ.get("LABBENCH_ROOT")
    if env:
        return Path(env).expanduser().resolve()

    for start in (Path.cwd(), Path(__file__).resolve()):
        for candidate in [start, *start.parents]:
            if (candidate / "pyproject.toml").is_file():
                return candidate

    # Last resort: three levels up from src/labbench/paths.py.
    return Path(__file__).resolve().parents[2]


def _sub(name: str) -> Path:
    return repo_root() / name


def labs_dir() -> Path:
    return _sub("labs")


def artifacts_dir() -> Path:
    d = _sub("artifacts")
    d.mkdir(parents=True, exist_ok=True)
    return d


def logs_dir() -> Path:
    d = _sub("logs")
    d.mkdir(parents=True, exist_ok=True)
    return d


def ansible_dir() -> Path:
    return _sub("ansible")


def docker_dir() -> Path:
    return _sub("docker")


def scripts_dir() -> Path:
    return _sub("scripts")


def schemas_dir() -> Path:
    return _sub("schemas")


def config_dir() -> Path:
    return _sub("config")


def default_lab_path() -> Path:
    return labs_dir() / "dev-lab.yml"


def timestamp() -> str:
    """Return a filesystem-safe UTC timestamp for artifact filenames."""

    import datetime as _dt

    return _dt.datetime.now(_dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
