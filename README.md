# LabBench

LabBench is a cross-platform storage **test-lab provisioner and validation
harness**. It stands up a simulated mixed Linux/Windows storage lab in Docker,
provisions host-side storage configuration with Ansible (SSH for Linux,
WinRM for Windows), attaches iSCSI targets with multipath, injects a path
failure and measures failover recovery time, then runs **one shared pytest
suite** against every host through an OS-agnostic driver abstraction. Bash and
PowerShell collectors snapshot OS version, disk topology and driver versions
into a machine-readable inventory, and a reusable reporting step renders an
HTML/Markdown report with a failure **triage** summary.

## The problem it solves

Storage validation is normally done by hand: rack hosts, wire two fabrics,
configure initiators, cut a path, and eyeball whether I/O survived. That work
is slow, unrepeatable, and impossible to run in CI. LabBench encodes the whole
loop as code so a mixed-OS storage configuration can be provisioned, exercised
and validated on a laptop or in CI, with real measured failover numbers and a
single pass/fail matrix across operating systems.

## Architecture

```
                            +---------------------------------+
                            |   control node (this repo)      |
                            |   labbench CLI  |  pytest       |
                            +----+----------------+-----------+
                    SSH (2201/2202)  |        |  WinRM (55985/55986)
                                     |        |
              +----------------------+        +---------------------+
              |                                                     |
     +--------v---------+                                 +---------v---------+
     |  linux-host-1/2  |                                 |   win-host-1/2    |
     |  openssh-server  |                                 |   pwsh + WinRM    |
     |  open-iscsi      |                                 |   stub (WSMan)    |
     |  multipath-tools |                                 |   + SimAgent.ps1  |
     +--------+---------+                                 +---------+---------+
              |                                                     |
              |  each host is attached to BOTH storage networks      |
              +----------------------+   +--------------------------+
                                     |   |
        path-a 172.28.0.0/24 --------+   +-------- path-b 172.29.0.0/24
                                     |   |
                            +--------v---v--------+
                            |   storage-target     |
                            |   iscsi_sim.py       |   HTTP mgmt API :8080
                            |   portal A + B       |   (targetcli/tgt if real)
                            +----------------------+

  driver abstraction            pipeline
  +------------------+          provision -> attach -> validate -> collect -> report
  | HostDriver (ABC) |            ansible     iscsi     pytest      bash/ps   jinja2
  |  |- LinuxSSH     |            ssh/winrm   multipath + failover   json      html/md
  |  \_ WindowsWinRM|
  | driver_for(host)|
  +------------------+
```

Every host is reachable on both storage networks (`path-a`, `path-b`) plus the
management network, so a single iSCSI path can be severed independently while
management stays up.

### Real vs simulated iSCSI

`docker/storage-target/iscsi_sim.py` detects at startup whether a kernel LIO
target is usable (`targetcli` + `/sys/kernel/config/target`). It logs exactly
which mode it is in and the management API reports it:

* **`mode: real`** — a real kernel target is configured through `targetcli`
  (`backstores/fileio`, `iscsi/<iqn>/tpg1/luns`, both portals). Host drivers use
  the native tooling (`iscsiadm`, `multipath`, `lsblk`; `Get-IscsiTarget`,
  `Get-Disk`, `Get-MPIOAvailableHW`).
* **`mode: simulated`** — the kernel stack is unavailable (typical inside
  Docker on macOS/CI). The simulator owns target/LUN/session/path state and
  models multipath with **real wall-clock timers**: a path-down pauses I/O for
  the `no_path_retry` window (queueing) then resumes on the survivor, and a
  path-up returns the path to service after the multipath `failback` delay.
  Host drivers delegate to on-host agents (`sim_agent.py`, `SimAgent.ps1`) that
  speak the simulator API.

The simulator is a real component with real timing — recovery numbers in this
README are measured from it, not invented. The mode is logged and recorded in
every artifact (`"mode": "simulated"` below).

## Quickstart

Prerequisites: Linux or macOS with Docker + Docker Compose, Python 3.11+ with
`venv`, GNU Make, and (for the Ansible Windows path) the collections installed
by `make install`.

```bash
make install          # venv + pip install -e '.[dev]' + ansible-galaxy collections
make lab-up           # build & start 5 services, wait for readiness
make e2e              # provision -> attach -> validate -> collect -> report
make lab-down         # tear down (no leftover containers)
```

CLI subcommands (all read `labs/dev-lab.yml` unless `--lab` is given):

```console
$ labbench lab up
lab 'dev-lab' up in 4.1s (mode: simulated)
  storage-target   healthy    api 127.0.0.1:8080 (HTTP 200)
  linux-host-1     healthy    ssh 127.0.0.1:2201 (tcp open)
  linux-host-2     healthy    ssh 127.0.0.1:2202 (tcp open)
  win-host-1       healthy    winrm 127.0.0.1:55985 (HTTP 200)
  win-host-2       healthy    winrm 127.0.0.1:55986 (HTTP 200)

$ labbench lab status
Hosts
name          os_family  connection  address    port   roles
------------  ---------  ----------  ---------  -----  ---------------------
linux-host-1  linux      ssh         127.0.0.1  2201   common_linux,tuning
linux-host-2  linux      ssh         127.0.0.1  2202   common_linux,tuning
win-host-1    windows    winrm       127.0.0.1  55985  common_windows,tuning
win-host-2    windows    winrm       127.0.0.1  55986  common_windows,tuning

$ labbench provision
provision complete in 22.4s: changed=0 ok=31 failed=0 unreachable=0

$ labbench attach
attach: 4/4 host(s) ready (mode: simulated)
  linux-host-1   paths=2 device=/dev/mapper/mpatha checksum=f1c88a14a4e6020ad4121468b7365c4f
  ...

$ labbench validate
host          passed  failed  skipped  total
------------  ------  ------  -------  -----
linux-host-1  12      0       0        12
win-host-1    12      0       0        12
TOTAL: passed=48 failed=0 skipped=0 total=48

$ labbench collect
inventory: artifacts/lab_inventory.json
markdown : artifacts/lab_inventory.md
drift findings: 0

$ labbench report
HTML: artifacts/report.html
MD  : artifacts/report.md
TRIAGE: no failures
```

`labbench validate --only failover` runs just the failover case; `--only`
accepts `os_baseline`, `initiator_config`, `target_attach`, `io_integrity`,
`failover`. `labbench validate --host win-host-1` limits to one host.

## How the Ansible layer works

* `ansible/inventory/labbench_inventory.py` is a dynamic inventory script. It
  reads the same YAML topology and emits three groups — `linux`, `windows`,
  `storage` — plus `_meta.hostvars` (address, port, connection, credentials,
  initiator IQN, WinRM settings). No duplicated host list.
* `ansible/playbooks/site.yml` runs three plays:
  * **Linux (SSH, `paramiko`)**: `roles/common_linux` installs
    `open-iscsi`/`multipath-tools`/`sg3-utils`, templates
    `/etc/multipath.conf` (path grouping, `no_path_retry`, `failback`) and sets
    the initiator name; `roles/tuning` sets the scheduler/queue depth via udev
    and sysctl.
  * **Windows (WinRM via pywinrm)**: because the lab's Windows nodes are WinRM
    stubs backed by `pwsh`, `ansible.windows` modules (which need the real
    Windows API surface) cannot run there. The play drives PowerShell over the
    same pywinrm connection with `raw`, branching on `$IsWindows` so the *same
    role* either touches the real registry/MPIO cmdlets or writes the
    equivalent simulated configuration. `win_host` initiator/MPIO config, disk
    timeout and the collector script are all idempotent.
  * **Storage (local)**: `roles/storage_target` defines the target, LUNs and
    both portals through the management API.
* **Idempotence proof**: `labbench provision` runs `ansible-playbook` and parses
  the `PLAY RECAP` into `artifacts/provision_<ts>.json`. The first run reports
  `changed > 0`; the second reports `changed=0`. `tests/test_provision_idempotent.py`
  asserts this from the parsed JSON (skipped with a clear reason when Docker is
  unavailable).

## Adding a new host OS or validation case

* **New OS**: add the host to `labs/dev-lab.yml` with an `os_family`, then add a
  driver under `src/labbench/drivers/` and register it in
  `driver_for()` ( `src/labbench/drivers/factory.py` ). Add collector support in
  `scripts/` and extend `schemas/lab_config.schema.json`. The validation suite
  parametrizes over `lab.hosts`, so it picks up the new host automatically.
* **New validation case**: drop a `test_*.py` in `tests/validation/` using the
  `host`, `driver`, `lab`, `thresholds` fixtures from
  `tests/validation/conftest.py`, then add its name to `CASE_FILES` in
  `src/labbench/validation.py` so `--only <name>` works.
* **New threshold**: edit `config/thresholds.yml` (loaded by
  `src/labbench/thresholds.py`); the failover workflow and the suite read it.

## RESULTS

All numbers below were produced by this build on the developer machine
(macOS/arm64, Docker Desktop, LinuxKit kernel; Windows nodes run the pwsh
WinRM stub under emulation). Two clean runs (`make clean && make e2e`) were
performed; both are reported. Nothing here is estimated.

### Lab bring-up (all 5 services healthy)

| run | `labbench lab up` | mode |
| --- | --- | --- |
| A | 4.1 s | simulated |
| B | 6.2 s | simulated |

### Provisioning and idempotence (4 hosts + storage target)

| run | duration | ok | changed | failed | unreachable |
| --- | --- | --- | --- | --- | --- |
| A first run | 51.7 s | 33 | 22 | 0 | 0 |
| B first run | 60.6 s | 33 | 22 | 0 | 0 |
| second run (both) | 22.4 s | 31 | **0** | 0 | 0 |

The second run reports `changed=0` for every host — the exact assertion in
`tests/test_provision_idempotent.py`.

### Validation suite

48 cases executed (5 case files × 4 hosts; the failover case is one case per
host). 12 cases per host, all passing.

| host | os_family | passed | failed | skipped | total |
| --- | --- | --- | --- | --- | --- |
| linux-host-1 | linux | 12 | 0 | 0 | 12 |
| linux-host-2 | linux | 12 | 0 | 0 | 12 |
| win-host-1 | windows | 12 | 0 | 0 | 12 |
| win-host-2 | windows | 12 | 0 | 0 | 12 |

**Pass rate: 48/48 = 100%** (Linux 24/24, Windows 24/24).

### Multipath failover recovery times (N=5 iterations per host, ms)

Recovery = time from severing path A until I/O resumes on the surviving path.
Restore = time from restoring path A until both paths are active again.

| host | os_family | min | median | p95 | max | restore median | io_errors | corrupted | pass |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| linux-host-1 | linux | 3900.9 | 4055.68 | 4217.16 | 4217.16 | 1625.47 | 264 | False | True |
| linux-host-2 | linux | 3426.87 | 3868.13 | 4567.31 | 4567.31 | 1574.56 | 280 | False | True |
| win-host-1 | windows | 4163.75 | 5827.15 | 6062.67 | 6062.67 | 4102.62 | 8 | False | True |
| win-host-2 | windows | 3885.67 | 4530.37 | 7283.11 | 7283.11 | 4038.44 | 7 | False | True |

Per-OS summary (across both hosts, N=10 samples each):

| os_family | recovery median | recovery p95 | recovery max | restore median |
| --- | --- | --- | --- | --- |
| linux | ~3.97 s | ~4.57 s | ~4.57 s | ~1.60 s |
| windows | ~5.18 s | ~7.28 s | ~7.28 s | ~4.07 s |

The Linux recovery times track the simulator's 4 s `no_path_retry` window
(plus polling granularity). The Windows p95 is wider because each I/O probe is
a full emulated `pwsh` WinRM round-trip; the guardrails in
`config/thresholds.yml` are widened per-OS for this reason (Linux p95 < 9 s,
Windows p95 < 20 s), and every host still passed with no corruption.

### I/O integrity

64 MB written and read back per host with an MD5 checksum of the fabricated
payload; all four hosts verified:

| host | checksum (first 12) | throughput | read-back match |
| --- | --- | --- | --- |
| linux-host-1 | f1c88a14a4e6 | 206.33 MB/s | yes |
| linux-host-2 | f1c88a14a4e6 | 231.84 MB/s | yes |
| win-host-1 | f1c88a14a4e6 | 223.95 MB/s | yes |
| win-host-2 | f1c88a14a4e6 | 175.89 MB/s | yes |

### Configuration drift

**0 findings.** Every host reports the expected OS family, initiator IQN, two
active paths and enabled multipath (and, for Windows, the provisioned 60 s disk
timeout).

### End-to-end wall clock

| run | `make clean && make e2e` (lab up → report) |
| --- | --- |
| A | 434 s |
| B | 426 s |

### CI job durations

GitHub Actions was not executed from this environment, but these jobs run
exactly the commands below; the durations are the measured local runtimes of
those commands.

| CI job | command | duration |
| --- | --- | --- |
| Lint (ruff, black, ansible-lint, shellcheck) | `make lint` | 6 s |
| Unit tests (py3.11 and py3.12) | `make test` | 4 s |
| Docker end-to-end | `make e2e` | ~426 s |

### Real `artifacts/report.md`

```markdown
# LabBench Validation Report

- Run: 20261013T015055831478Z
- Mode: `simulated`
- Lab: dev-lab
- Validation: passed=48 failed=0 skipped=0 total=48

## Per-host pass/fail matrix

| host | os_family | passed | failed | skipped | total |
| --- | --- | --- | --- | --- | --- |
| linux-host-1 | linux | 12 | 0 | 0 | 12 |
| linux-host-2 | linux | 12 | 0 | 0 | 12 |
| win-host-1 | windows | 12 | 0 | 0 | 12 |
| win-host-2 | windows | 12 | 0 | 0 | 12 |

## Multipath failover recovery times (ms)

| host | os_family | min | median | p95 | max | restore median | io_errors | corrupted | pass |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| linux-host-1 | linux | 3900.9 | 4055.68 | 4217.16 | 4217.16 | 1625.47 | 264 | False | True |
| linux-host-2 | linux | 3426.87 | 3868.13 | 4567.31 | 4567.31 | 1574.56 | 280 | False | True |
| win-host-1 | windows | 4163.75 | 5827.15 | 6062.67 | 6062.67 | 4102.62 | 8 | False | True |
| win-host-2 | windows | 3885.67 | 4530.37 | 7283.11 | 7283.11 | 4038.44 | 7 | False | True |

## Configuration drift

- none

## TRIAGE

No failures. All configurations passed.
```

## Known limitations and what changes on physical E-Series hardware

* **iSCSI mode**: on this machine the kernel target modules are unavailable, so
  the lab validates `mode: simulated`. Against real hardware (and a Linux host
  kernel with `iscsi_target_mod`/`dm_multipath`), the same code runs
  `mode: real` with `targetcli` and `iscsiadm`/`multipath`; the drivers already
  implement those paths.
* **Windows nodes** are a WinRM/WSMan stub executing PowerShell, not real
  Windows. The driver still uses pywinrm's API surface and real process
  execution, but `ansible.windows` native modules (registry, MPIO cmdlets) only
  run on a real Windows host — the roles branch on `$IsWindows` to do so.
* **Timing granularity**: because each Windows I/O probe is a full emulated
  `pwsh` round-trip, Windows failover p95 is dominated by probe latency rather
  than the fabric. On real hardware (or native `pwsh`) the probes are
  sub-millisecond and recovery tracks the multipath/`no_path_retry` settings
  tightly.
* **E-Series differences**: a physical E-Series lab would add host-side HBA
  queue-depth tuning, ALUA path priorities (`path_prio_alua`), SCSI-3 PR
  reservation checks, SANtricity API–driven volume creation and `rdac`/`alua`
  path grouping. LabBench's driver abstraction and `config/thresholds.yml` are
  where those would be added (`path_grouping_policy`, `failback`, queue depth
  are already parameters); the simulated target would be replaced by the real
  `storage_target` role calling the array's management interface.
* The simulator models target-side controllers as a single path-group; it does
  not model asymmetric logical unit access.

## Repository layout

```
src/labbench/        CLI, topology model, lab lifecycle, drivers, workflows, reporting
docker/              compose.yml, storage-target/iscsi_sim.py, host images + WinRM stub
ansible/             dynamic inventory, playbooks, roles (linux/windows/storage/tuning)
scripts/             collect_lab_config.sh, Collect-LabConfig.ps1
schemas/             lab_config.schema.json
config/              thresholds.yml
tests/               unit tests, tests/validation/ cross-OS suite
labs/                dev-lab.yml topology
Jenkinsfile          declarative pipeline (nightly + JUnit + triage + always-down)
.github/workflows/   ci.yml (lint, unit tests on 3.11/3.12, static analysis, docker e2e)
docs/RUNBOOK.md      troubleshooting guide
```

See `docs/RUNBOOK.md` for common lab failures and `LICENSE` (MIT).
