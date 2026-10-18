#!/usr/bin/env bash
# Isolation and end-to-end test of the Docker data zones (docs/docker.md → Data zones): compose profiles `data` and
# `data-sftp` in a throwaway project `tbr-dztest` (own volumes, networks 172.31.251.0/24 + 172.31.253.0/24, ports
# 18091 and 12223), removed afterwards; TBR_DZTEST_KEEP=1 keeps it. Never run it together with the smoke test or
# the screenshot script (same subnets). Needs Docker Engine >= 26 and the OpenSSH client (sftp, ssh-keygen).
#
# Checks: data-init layout and KEK, the sandbox probes of `tbr-admin runner selftest` through the launcher (network,
# reads outside the job, writes, the job's own view, symlinks, limits), runner jobs end to end with a stub API
# (decrypt → job container → encrypted quarantine, exit code 3, data key unreadable, pre-check), the runner's
# network (API only), the launcher socket (runner only), no job container or log left behind, `runner join` over
# the runner network, and the real chain SFTP upload → import → profile task → runner → stored profile.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
PROJECT=tbr-dztest
APP_PORT=${TBR_DZTEST_APP_PORT:-18091}
SFTP_PORT=${TBR_DZTEST_SFTP_PORT:-12223}
WORK="$(mktemp -d)"
FAILED=0

rand() { python3 -c 'import secrets; print(secrets.token_urlsafe(24))'; }
export TBR_DB_PASSWORD="$(rand)" MARIADB_ROOT_PASSWORD="$(rand)" TBR_INITIAL_ADMIN_PASSWORD="$(rand)" \
       TBR_DATA_ZONES=1 TBR_DATA_SFTP=1 \
       TBR_PUBLISH=127.0.0.1:$APP_PORT TBR_SFTP_PUBLISH=127.0.0.1:$SFTP_PORT \
       TBR_DOCKER_SUBNET=172.31.251.0/24 TBR_DOCKER_GATEWAY=172.31.251.1 TRUSTED_PEER_NETWORKS=172.31.251.1/32 \
       TBR_RUNNER_SUBNET=172.31.253.0/24 \
       TBR_IMAGE=$PROJECT-app:local TBR_RUNNER_IMAGE=$PROJECT-runner:local TBR_SFTP_IMAGE=$PROJECT-sftp:local

# Network and volume names are fixed in compose.yaml; rename them for this project (the launcher gets the jobs
# volume by name, so it is renamed there as well).
cat > "$WORK/compose.dztest.yaml" <<EOF
services:
  runner-launcher:
    environment:
      TBR_JOBS_VOLUME: $PROJECT-jobs
networks:
  taskbrigade:
    name: $PROJECT
volumes:
EOF
for v in db state gitea data data-key runner-state jobs launcher sftp sftp-spool sftp-hostkeys; do
  key="taskbrigade-$v"; [[ "$v" == gitea ]] && key=gitea-data
  printf '  %s:\n    name: %s-%s\n' "$key" "$PROJECT" "$v" >> "$WORK/compose.dztest.yaml"
done

compose() {
  docker compose -p "$PROJECT" --project-directory "$ROOT" -f "$ROOT/compose.yaml" -f "$WORK/compose.dztest.yaml" \
    --profile data --profile data-sftp "$@"
}

cleanup() {
  status=$?
  if [[ "$status" -ne 0 || "$FAILED" -ne 0 ]]; then
    echo "--- container logs" >&2
    compose logs --no-color --tail 40 data-init runner-launcher runner sftp taskbrigade >&2 || true
  fi
  if [[ "${TBR_DZTEST_KEEP:-}" == 1 ]]; then
    echo "Stack $PROJECT kept (compose files in $WORK)." >&2
  else
    compose down -v --remove-orphans >/dev/null 2>&1 || true
    rm -rf "$WORK"
  fi
}
trap cleanup EXIT

check() {  # check LABEL COMMAND... – PASS/FAIL line, failures counted
  local label="$1"
  shift
  if out="$("$@" 2>&1)"; then
    echo "PASS $label"
  else
    echo "FAIL $label"
    echo "$out" | tail -20 | sed 's/^/     /'
    FAILED=$((FAILED + 1))
  fi
}

echo "==> Building and starting $PROJECT (profiles data, data-sftp)"
compose up -d --build --quiet-pull --wait >/dev/null

echo "==> Layout and key"
init_exit="$(docker inspect -f '{{.State.ExitCode}}' "$(compose ps -aq data-init)")"
check "data-init finished" test "$init_exit" == 0
key_mode="$(compose exec -T runner stat -c '%a %u %g' /etc/taskbrigade-data/data.key)"
check "KEK root:tbr-data 0640 (got $key_mode)" test "$key_mode" == "640 0 10004"

echo "==> Sandbox probes through the launcher (tbr-admin runner selftest)"
compose exec -T runner python -m app.runner.selftest --launcher docker --data-dir /srv/taskbrigade-data \
  --data-key-file /etc/taskbrigade-data/data.key --all --timeout 20 | tee "$WORK/selftest.log"
grep -q '^FAIL' "$WORK/selftest.log" && FAILED=$((FAILED + 1))

echo "==> Runner jobs end to end (stub API, real launcher, store and quarantine)"
E2E=(-e TBR_E2E_DATA_DIR=/srv/taskbrigade-data -e TBR_E2E_KEY_FILE=/etc/taskbrigade-data/data.key
     -e TBR_E2E_SPEC=/srv/taskbrigade-data/store/e2e-spec.json -e TBR_E2E_LAUNCHER=docker)
# The app venv has no openpyxl: the workbook comes from the runner's analysis venv (stdlib + openpyxl only).
compose exec -T runner sh -c 'cd /tmp && PYTHONPATH=/app /opt/taskbrigade/runner-venv/bin/python - xlsx' \
  < "$ROOT/deploy/sandbox-test/e2e_job.py" > "$WORK/data.xlsx"
docker cp "$WORK/data.xlsx" "$(compose ps -q taskbrigade):/tmp/e2e-data.xlsx"
compose exec -T -u 0 "${E2E[@]}" -e TBR_E2E_XLSX=/tmp/e2e-data.xlsx taskbrigade python - prepare \
  < "$ROOT/deploy/sandbox-test/e2e_job.py"
compose exec -T "${E2E[@]}" runner python - run < "$ROOT/deploy/sandbox-test/e2e_job.py" | tee "$WORK/e2e.log" \
  || FAILED=$((FAILED + 1))

echo "==> Runner network and control socket"
# shellcheck disable=SC2016
check "runner reaches the API" compose exec -T runner python -c \
  'import urllib.request; urllib.request.urlopen("http://taskbrigade:8090/healthz", timeout=5)'
check "runner reaches no internet, MariaDB or Gitea" compose exec -T runner python -c '
import socket
for addr in (("1.1.1.1", 53), ("mariadb", 3306), ("gitea", 3000)):
    try:
        socket.create_connection(addr, 3)
    except OSError:
        continue
    raise SystemExit(f"reached {addr}")'
check "runner has no Docker socket" compose exec -T runner sh -c '! test -e /var/run/docker.sock'
check "launcher socket refused to other users" compose exec -T -u 10003:10003 runner python -c '
import socket
s = socket.socket(socket.AF_UNIX)
try:
    s.connect("/run/taskbrigade-launcher/launcher.sock")
except OSError:
    raise SystemExit(0)
raise SystemExit("connected")'
check "no job container left" test -z "$(docker ps -aq --filter label=org.taskbrigade.job)"

echo "==> Join over the runner network, profile job from an SFTP delivery"
setup="$(compose exec -T taskbrigade python - setup < "$ROOT/deploy/docker/data-zones-e2e.py" | sed -n 's/^SETUP //p')"
code="$(python3 -c 'import json, sys; print(json.loads(sys.argv[1])["code"])' "$setup")"
check "runner join" compose exec -T runner tbr-admin runner join "$code"
sleep 12
check "runner check" compose exec -T runner tbr-admin runner check

ssh-keygen -q -t ed25519 -N "" -f "$WORK/deliver" -C dztest
docker cp "$WORK/deliver.pub" "$(compose ps -q taskbrigade):/tmp/deliver.pub"
check "sftp-user add (spool)" compose exec -T taskbrigade tbr-admin data sftp-user add dzt-export --project DZT \
  --key /tmp/deliver.pub
for _i in $(seq 1 12); do
  compose exec -T sftp test -s /srv/taskbrigade-sftp/dzt-export/.ssh/authorized_keys 2>/dev/null && break
  sleep 2
done
printf 'month;account;amount\n' > "$WORK/export.csv"
for i in $(seq 1 40); do printf '2026-0%s;%s;%s.5\n' $((1 + i % 3)) $((4000 + i % 4)) $((10 + i)) >> "$WORK/export.csv"; done
SSH_OPTS=(-o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null -o BatchMode=yes -o LogLevel=ERROR
          -i "$WORK/deliver" -P "$SFTP_PORT")
check "SFTP upload into the chroot" sftp "${SSH_OPTS[@]}" -b - dzt-export@127.0.0.1 <<EOF
put $WORK/export.csv upload/export.csv.part
rename upload/export.csv.part upload/export.csv.done
EOF
listing="$(sftp "${SSH_OPTS[@]}" -b - dzt-export@127.0.0.1 <<< 'ls -1a /' 2>/dev/null | grep -v '^sftp>' \
  | sed 's|^/||' | grep -vx '\.\{1,2\}' | sort | tr '\n' ' ')"
check "chroot shows only .ssh and upload (got: $listing)" test "$listing" == ".ssh upload "
check "no shell for the deliverer" bash -c "! ssh -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null \
  -o BatchMode=yes -o LogLevel=ERROR -i $WORK/deliver -p $SFTP_PORT dzt-export@127.0.0.1 id 2>/dev/null | grep -q uid"

profile=""
for _i in $(seq 1 30); do
  compose exec -T taskbrigade python - assign < "$ROOT/deploy/docker/data-zones-e2e.py" >/dev/null
  status="$(compose exec -T taskbrigade python - status < "$ROOT/deploy/docker/data-zones-e2e.py" | sed -n 's/^STATUS //p')"
  profile="$(python3 -c 'import json, sys; d = json.loads(sys.argv[1])["datasets"]; print(d[0]["profile"] if d else "")' "$status")"
  [[ "$profile" == generated ]] && break
  sleep 5
done
echo "     $status"
check "SFTP delivery imported and profiled by the runner" test "$profile" == generated
check "plaintext delivery removed" compose exec -T sftp sh -c '[ -z "$(ls -A /srv/taskbrigade-sftp/dzt-export/upload)" ]'
check "no job container left" test -z "$(docker ps -aq --filter label=org.taskbrigade.job)"

if [[ "$FAILED" -ne 0 ]]; then
  echo "==> $FAILED check group(s) failed"
  exit 1
fi
echo "==> All data-zone checks passed"
