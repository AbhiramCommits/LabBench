# LabBench Runbook

Operational troubleshooting for the LabBench Docker lab. Every command assumes
you are at the repository root with the virtualenv installed (`make install`).

## 1. A host is unreachable

Symptom: `labbench lab up` never becomes healthy, or `labbench attach` reports
`DriverError: ... produced no output` / connection timeouts.

1. Check per-service health and mode:
   ```bash
   labbench lab status
   docker compose -f docker/compose.yml -p labbench ps
   ```
2. SSH (Linux) directly:
   ```bash
   ssh -o StrictHostKeyChecking=no -p 2201 root@127.0.0.1   # password: labbench
   ```
3. WinRM (Windows stub) health:
   ```bash
   curl -s http://127.0.0.1:55985/health
   curl -s http://127.0.0.1:55985/commands   # recent command executions
   ```
4. If a container exited, recreate it:
   ```bash
   labbench lab down && labbench lab up
   ```
5. The emulated Windows nodes can take 10–30 s to start. `labbench lab up`
   waits for readiness; if it times out, raise `--timeout`.

## 2. WinRM authentication / connection errors

Symptom: pywinrm raises `WinRMTransportError` or the ansible Windows play is
unreachable.

* The stub only supports `basic` / `plaintext` transport over HTTP (no
  NTLM/Kerberos/encryption). Ensure the inventory host vars are:
  `ansible_winrm_transport=basic`, `ansible_winrm_scheme=http`,
  `ansible_winrm_message_encryption=never`,
  `ansible_winrm_server_cert_validation=ignore`.
* Confirm the endpoint is `/wsman`:
  ```bash
  curl -s -o /dev/null -w '%{http_code}\n' http://127.0.0.1:55985/wsman
  ```
  A bare `GET` returns 404 (expected); `POST` is the WSMan protocol path.
* Verify credentials in `labs/dev-lab.yml` (`tags.password`, user
  `Administrator`).
* Run a single host verbosely:
  ```bash
  cd ansible
  ../.venv/bin/ansible-playbook playbooks/site.yml -i inventory/labbench_inventory.py \
      --limit win-host-1 -vvv
  ```

## 3. Target login / discovery fails

Symptom: `labbench attach` shows `paths=0` or `active_paths` below 2.

1. Check the simulator mode and portals:
   ```bash
   curl -s http://127.0.0.1:8080/mode
   curl -s http://127.0.0.1:8080/portals
   curl -s http://127.0.0.1:8080/targets
   ```
2. Sessions for a host's initiator:
   ```bash
   curl -s 'http://127.0.0.1:8080/sessions?initiator=iqn.2026-01.labbench:linux-host-1'
   ```
   Empty means the login did not reach the API. Re-run `labbench attach`.
3. On a Linux host, confirm the agent can reach the simulator **by service
   name** (not `127.0.0.1`, which is the container itself):
   ```bash
   ssh -p 2201 root@127.0.0.1 \
     'python3 /opt/labbench/sim_agent.py discover --sim http://storage-target:8080 --initiator iqn.2026-01.labbench:linux-host-1'
   ```
4. A path left down from an aborted run will hold `active_paths` at 1. Restore
   all portals and reset state:
   ```bash
   for p in 172.28.0.10:3260 172.29.0.10:3260; do
     curl -s -X POST http://127.0.0.1:8080/path/up -d "{\"portal\":\"$p\"}"; echo
   done
   curl -s -X POST http://127.0.0.1:8080/reset; echo
   ```

## 4. Stale multipath device

Symptom: validation fails on `target_attach`/`io_integrity` because a previous
mount or session is stale.

1. List current path state for the host:
   ```bash
   curl -s 'http://127.0.0.1:8080/paths?initiator=iqn.2026-01.labbench:linux-host-1'
   ```
2. Log the host out and back in (clears sessions, keeps the target):
   ```bash
   labbench attach --host linux-host-1
   ```
3. On real hardware the equivalent is:
   ```bash
   iscsiadm -m node -u; iscsiadm -m node -l; multipath -F; multipath -r
   ```
4. In the simulated lab, clear all sessions with
   `curl -s -X POST http://127.0.0.1:8080/reset`.

## 5. Stuck container network / address pool exhausted

Symptom: `docker compose up` fails with `Pool overlaps` or a service cannot get
its static IP.

1. Remove leftover networks from an aborted run:
   ```bash
   labbench lab down
   docker network ls --filter label=com.docker.compose.project=labbench
   docker network rm labbench_path-a labbench_path-b 2>/dev/null || true
   ```
2. If another program uses `172.28.0.0/24` or `172.29.0.0/24`, change the
   `networks` subnets in `docker/compose.yml` **and** the `ipv4_address`
   entries and the portal addresses in `labs/dev-lab.yml` together.
3. Nuclear option — remove everything the project created:
   ```bash
   docker compose -f docker/compose.yml -p labbench down -v --remove-orphans
   docker network prune -f
   ```

## 6. Provisioning not idempotent (`changed > 0` on the second run)

1. Re-run and inspect which host changed:
   ```bash
   labbench provision
   python -c "import json,glob; d=json.load(open(sorted(glob.glob('artifacts/provision_*.json'))[-1])); print([(h['host'],h['changed']) for h in d['hosts']])"
   ```
2. Common cause: a config file whose content is regenerated every run (e.g.
   non-deterministic ordering). The Windows roles use ordered JSON and trimmed
   comparisons for exactly this reason (`ansible/roles/*/tasks/*.yml`).
3. If a container was rebuilt, the first run legitimately changes again; run
   `labbench provision` a second time — it must report `changed=0`.

## 7. Collecting a clean diagnostic bundle

```bash
labbench lab status > diagnostics.txt
docker compose -f docker/compose.yml -p labbench ps >> diagnostics.txt
curl -s http://127.0.0.1:8080/health >> diagnostics.txt
curl -s http://127.0.0.1:8080/events >> diagnostics.txt
tail -200 logs/labbench.log >> diagnostics.txt
```
