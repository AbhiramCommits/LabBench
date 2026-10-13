"""Shared fixtures for the cross-OS LabBench validation suite.

One parametrization runs every case against every host in the topology, using
the driver abstraction, so the same test body exercises Linux (SSH) and
Windows (WinRM).
"""

from __future__ import annotations

import os

import pytest

from labbench import paths
from labbench.drivers import driver_for
from labbench.thresholds import load_thresholds
from labbench.topology import load_lab


def pytest_addoption(parser: pytest.Parser) -> None:
    parser.addoption("--lab", action="store", default=None, help="Lab topology YAML.")
    parser.addoption("--hosts", action="store", default=None, help="Comma-separated host filter.")


def _resolve_lab(config: pytest.Config):
    lab_path = config.getoption("--lab") or os.environ.get("LABBENCH_LAB") or str(
        paths.default_lab_path()
    )
    lab = load_lab(lab_path)
    host_filter = config.getoption("--hosts") or os.environ.get("LABBENCH_HOSTS")
    if host_filter:
        names = {h.strip() for h in host_filter.split(",") if h.strip()}
        lab.hosts = [h for h in lab.hosts if h.name in names]
    return lab


def pytest_generate_tests(metafunc: pytest.Metafunc) -> None:
    if "host" in metafunc.fixturenames:
        lab = _resolve_lab(metafunc.config)
        metafunc.parametrize("host", lab.hosts, ids=[h.name for h in lab.hosts])


@pytest.fixture(scope="session")
def lab(request: pytest.FixtureRequest):
    return _resolve_lab(request.config)


@pytest.fixture(scope="session")
def thresholds():
    return load_thresholds()


@pytest.fixture
def driver(host, lab):
    drv = driver_for(host, lab)
    yield drv
    drv.close()
