"""Topology loading and schema validation tests (no Docker required)."""

from __future__ import annotations

import textwrap

import pytest

from labbench import paths
from labbench.topology import Lab, LabSchemaError, load_lab, load_lab_str

VALID = """
lab:
  name: unit-lab
hosts:
  - name: l1
    os_family: linux
    address: 10.0.0.1
    connection: ssh
    roles: [common_linux]
    tags: {port: 2201}
  - name: w1
    os_family: windows
    address: 10.0.0.2
    connection: winrm
    roles: [common_windows]
targets:
  - name: t1
    iqn: iqn.2026-01.com.example:target1
    luns:
      - {id: 0, size_gb: 8}
    portals:
      - 10.0.0.10:3260
      - 10.0.0.11:3260
"""


def test_valid_load_from_string() -> None:
    lab = load_lab_str(VALID)
    assert isinstance(lab, Lab)
    assert lab.name == "unit-lab"
    assert [h.name for h in lab.hosts] == ["l1", "w1"]
    assert lab.host("l1").connection == "ssh"
    assert lab.host("l1").port == 2201
    assert lab.host("w1").connection == "winrm"  # default for windows
    assert lab.host("w1").port == 5985
    target = lab.target("t1")
    assert target.path_count == 2
    assert len(target.luns) == 1


def test_valid_load_dev_lab() -> None:
    lab = load_lab(paths.default_lab_path())
    assert lab.name == "dev-lab"
    assert len(lab.hosts_by_family("linux")) == 2
    assert len(lab.hosts_by_family("windows")) == 2
    assert len(lab.targets) == 1
    assert lab.targets[0].path_count == 2


def test_default_connection_for_linux() -> None:
    lab = load_lab_str(textwrap.dedent("""
            hosts:
              - name: l1
                os_family: linux
                address: 10.0.0.1
            targets: []
            """))
    assert lab.host("l1").connection == "ssh"
    assert lab.host("l1").port == 22


def test_duplicate_host_names_rejected() -> None:
    doc = """
    hosts:
      - {name: dup, os_family: linux, address: 10.0.0.1}
      - {name: dup, os_family: linux, address: 10.0.0.2}
    """
    with pytest.raises(LabSchemaError, match="duplicate host name 'dup'"):
        load_lab_str(doc)


def test_missing_portal_rejected() -> None:
    doc = """
    hosts:
      - {name: l1, os_family: linux, address: 10.0.0.1}
    targets:
      - name: t1
        iqn: iqn.2026-01.com.example:t1
        luns:
          - {id: 0, size_gb: 8}
        portals: []
    """
    with pytest.raises(LabSchemaError, match="portals: expected a non-empty list"):
        load_lab_str(doc)


def test_portal_without_port_rejected() -> None:
    doc = """
    hosts:
      - {name: l1, os_family: linux, address: 10.0.0.1}
    targets:
      - name: t1
        iqn: iqn.2026-01.com.example:t1
        luns:
          - {id: 0, size_gb: 8}
        portals:
          - 10.0.0.10
    """
    with pytest.raises(LabSchemaError, match="missing a port"):
        load_lab_str(doc)


def test_bad_os_family_rejected() -> None:
    doc = """
    hosts:
      - {name: h, os_family: plan9, address: 10.0.0.1}
    """
    with pytest.raises(LabSchemaError, match="unsupported os_family 'plan9'"):
        load_lab_str(doc)


def test_bad_connection_for_family_rejected() -> None:
    doc = """
    hosts:
      - {name: h, os_family: linux, address: 10.0.0.1, connection: winrm}
    """
    with pytest.raises(LabSchemaError, match="not valid for os_family 'linux'"):
        load_lab_str(doc)


def test_missing_required_host_field_rejected() -> None:
    doc = """
    hosts:
      - {name: h, os_family: linux}
    """
    with pytest.raises(LabSchemaError, match="missing required field 'address'"):
        load_lab_str(doc)


def test_duplicate_lun_ids_rejected() -> None:
    doc = """
    hosts:
      - {name: h, os_family: linux, address: 10.0.0.1}
    targets:
      - name: t1
        iqn: iqn.2026-01.com.example:t1
        luns:
          - {id: 0, size_gb: 8}
          - {id: 0, size_gb: 16}
        portals: [10.0.0.10:3260]
    """
    with pytest.raises(LabSchemaError, match="duplicate LUN ids"):
        load_lab_str(doc)


def test_missing_file_rejected() -> None:
    with pytest.raises(LabSchemaError, match="lab file not found"):
        load_lab("/nonexistent/does-not-exist.yml")
