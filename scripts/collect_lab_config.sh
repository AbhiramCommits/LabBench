#!/usr/bin/env bash
# LabBench Linux lab-configuration collector.
#
# Emits a single JSON object describing the host OS, disk topology, multipath
# configuration, iSCSI initiator and driver versions. The same schema is
# produced by scripts/Collect-LabConfig.ps1 on Windows.
set -euo pipefail

SIM="${LABBENCH_SIMULATOR:-http://storage-target:8080}"
INITIATOR="${LABBENCH_INITIATOR:-}"
AGENT="${LABBENCH_AGENT:-/opt/labbench/sim_agent.py}"

hostname_v="$(hostname)"
os_version_v="$(grep -m1 '^PRETTY_NAME=' /etc/os-release 2>/dev/null | cut -d= -f2- | tr -d '"')"
os_version_v="${os_version_v:-unknown}"
kernel_v="$(uname -r)"
cpu_v="$(nproc 2>/dev/null || echo 0)"
mem_v="$(awk '/MemTotal/ {printf "%d", $2/1024}' /proc/meminfo 2>/dev/null || echo 0)"
timeout_v="null"
for f in /sys/block/sda/device/timeout /sys/block/sda/queue/io_timeout; do
  if [ -r "$f" ]; then timeout_v="$(cat "$f")"; break; fi
done

paths_json='{"active_paths":0,"total_paths":0,"paths":[]}'
disks_json='{"disks":[],"multipath":{}}'
if [ -f "$AGENT" ]; then
  paths_json="$(python3 "$AGENT" paths --sim "$SIM" --initiator "$INITIATOR" 2>/dev/null || echo "$paths_json")"
  disks_json="$(python3 "$AGENT" disks --sim "$SIM" --initiator "$INITIATOR" 2>/dev/null || echo "$disks_json")"
fi

iscsiadm_v="$(iscsiadm --version 2>/dev/null | head -1 || echo not-installed)"
multipathd_v="$(multipathd -v 2>/dev/null | head -1 || echo not-installed)"
sg_v="$(sg_inq --version 2>/dev/null | head -1 || echo not-installed)"

HOSTNAME_V="$hostname_v" OS_VERSION_V="$os_version_v" KERNEL_V="$kernel_v" \
CPU_V="$cpu_v" MEM_V="$mem_v" INITIATOR_V="$INITIATOR" ISCSIADM_V="$iscsiadm_v" \
MULTIPATHD_V="$multipathd_v" SG_V="$sg_v" PATHS_JSON="$paths_json" \
DISKS_JSON="$disks_json" TIMEOUT_V="$timeout_v" python3 - <<'PY'
import datetime
import json
import os

paths = json.loads(os.environ.get("PATHS_JSON") or "{}")
disks = json.loads(os.environ.get("DISKS_JSON") or "{}")
timeout_raw = os.environ.get("TIMEOUT_V", "null")
disk_timeout = None if timeout_raw in ("null", "") else int(float(timeout_raw))

disk_list = []
for d in disks.get("disks", []):
    disk_list.append(
        {
            "name": d.get("name"),
            "size_gb": d.get("size_gb"),
            "model": d.get("model"),
            "path_count": d.get("path_count", paths.get("total_paths", 0)),
            "paths": d.get("paths", []),
            "mountpoint": d.get("mountpoint"),
        }
    )
mp = disks.get("multipath", {}) or {}
mp.setdefault("path_grouping_policy", "failover")
mp.setdefault("no_path_retry", 12)
mp.setdefault("failback", "immediate")

out = {
    "hostname": os.environ["HOSTNAME_V"],
    "os_family": "linux",
    "os_version": os.environ["OS_VERSION_V"],
    "kernel": os.environ["KERNEL_V"],
    "cpu_count": int(os.environ["CPU_V"] or 0),
    "mem_mb": int(os.environ["MEM_V"] or 0),
    "disks": disk_list,
    "multipath": {
        "enabled": True,
        "path_grouping_policy": mp.get("path_grouping_policy"),
        "no_path_retry": mp.get("no_path_retry"),
        "failback": mp.get("failback"),
    },
    "initiator": os.environ["INITIATOR_V"],
    "disk_timeout_s": disk_timeout,
    "drivers": {
        "iscsiadm": os.environ["ISCSIADM_V"],
        "multipathd": os.environ["MULTIPATHD_V"],
        "sg3_utils": os.environ["SG_V"],
    },
    "simulated": True,
    "timestamp": datetime.datetime.now(datetime.timezone.utc).isoformat(),
}
print(json.dumps(out))
PY
