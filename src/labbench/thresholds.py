"""Threshold/expectation config loader (config/thresholds.yml)."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

from . import paths

DEFAULTS: dict[str, Any] = {
    "failover": {
        "iterations": 5,
        "no_path_retry_window_ms": 15000,
        "recovery_p95_ms": 9000,
        "restore_p95_ms": 6000,
        "probe_interval_ms": 20,
    },
    "io": {"size_mb": 64, "min_mbps": 5},
    "os": {
        "linux": {"expected_family": "linux", "version_range": ["20.04", "24.04"]},
        "windows": {"expected_family": "windows", "version_range": ["10.0", "10.1"]},
    },
    "attach": {"expected_paths": 2, "lun": 0},
    "drift": {"disk_timeout_s": 60, "path_count": 2, "multipath_required": True},
}


def load_thresholds(path: str | Path | None = None) -> dict[str, Any]:
    p = Path(path) if path else (paths.config_dir() / "thresholds.yml")
    data: dict[str, Any] = {}
    if p.is_file():
        data = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    merged = _deep_merge(DEFAULTS, data)
    return merged


def _deep_merge(base: dict, override: dict) -> dict:
    out = dict(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = _deep_merge(out[key], value)
        else:
            out[key] = value
    return out
