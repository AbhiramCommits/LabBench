"""Docker lab lifecycle (placeholder).

The real implementation lands in the "dockerized mixed-OS lab" step. Until
then this module exposes a topology-only status so ``labbench lab status`` is
useful without Docker.
"""

from __future__ import annotations

from typing import Any

from .topology import Lab


def status(lab: Lab) -> dict[str, Any]:  # pragma: no cover - replaced in step 2
    return {
        "lab": lab.name,
        "mode": "unknown",
        "state": "not-created",
        "services": [
            {"name": h.name, "state": "not-created", "health": "-", "detail": ""}
            for h in lab.hosts
        ],
    }


def up(lab: Lab, timeout: int = 180, build: bool = True) -> dict[str, Any]:
    raise NotImplementedError("lab up lands in the dockerized-lab step")


def down(lab: Lab, volumes: bool = True, prune_images: bool = False) -> dict[str, Any]:
    raise NotImplementedError("lab down lands in the dockerized-lab step")
