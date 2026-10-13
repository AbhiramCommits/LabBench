#!/usr/bin/env bash
# LabBench linux-host entrypoint: bring up sshd (and multipathd if present).
set -euo pipefail

ssh-keygen -A >/dev/null 2>&1 || true
mkdir -p /run/sshd

# Best-effort: start multipathd when the kernel supports it. In containers
# without the dm_multipath module this is a no-op and we run in simulated mode.
if command -v multipathd >/dev/null 2>&1 && [ -d /sys/module/dm_multipath ]; then
  multipathd -B >/dev/null 2>&1 || true
fi

echo "linux-host starting: hostname=$(hostname) sim=${LABBENCH_SIMULATOR:-unset} initiator=${LABBENCH_INITIATOR:-unset}"
exec /usr/sbin/sshd -D -e
