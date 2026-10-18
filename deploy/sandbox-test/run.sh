#!/usr/bin/env bash
# Sandbox isolation test in a throwaway systemd container (docs/operations.md → Data-zone runner).
#
#   deploy/sandbox-test/run.sh            # build, test, remove the container
#   TBR_SANDBOX_KEEP=1 deploy/sandbox-test/run.sh   # keep the container (docker exec -it tbr-sandbox-test bash)
#
# Needs Docker and a cgroup v2 host. The container is privileged (systemd as PID 1 needs it to create the job
# namespaces); it holds no real data. Steps: users/folders/units/polkit like `TBR_DATA_ZONES=1 install.sh`, small
# limits through the drop-in, the probe jobs of `app.runner.selftest`, a network control outside the sandbox,
# polkit scope of tbr-runner, end-to-end runner jobs (deploy/sandbox-test/e2e_job.py) and the journal check.
# Exit code 0 only if every check passes.
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
SRC="$(cd "$HERE/../.." && pwd)"
NAME=tbr-sandbox-test
IMAGE=tbr-sandbox-test:local
failed=0

cleanup() { [[ "${TBR_SANDBOX_KEEP:-0}" == 1 ]] || docker rm -f "$NAME" >/dev/null 2>&1 || true; }
trap cleanup EXIT
pass() { echo "PASS $*"; }
fail() { echo "FAIL $*"; failed=1; }
in_box() { docker exec -w /src "$NAME" "$@"; }
as_runner() { docker exec -w /src "$NAME" sudo -u tbr-runner "$@"; }

echo "==> image"
docker build -q -t "$IMAGE" "$HERE" >/dev/null
docker rm -f "$NAME" >/dev/null 2>&1 || true
docker run -d --name "$NAME" --privileged --cgroupns=private --tmpfs /run --tmpfs /run/lock \
  -v "$SRC:/src:ro" "$IMAGE" >/dev/null
for _i in $(seq 1 30); do
  state="$(docker exec "$NAME" systemctl is-system-running 2>/dev/null || true)"
  [[ $state == running || $state == degraded ]] && break
  sleep 1
done
echo "    systemd: $state, $(docker exec "$NAME" systemctl --version | head -1)"

echo "==> install (users, folders, key, units, polkit; limits 512M / 100% CPU / 15 s)"
docker exec -e LIMITS='[Service]\nMemoryMax=512M\nMemorySwapMax=0\nCPUQuota=100%\nTimeoutStartSec=15\n' \
  "$NAME" bash /src/deploy/sandbox-test/setup-in-container.sh

echo "==> probe jobs in the sandbox"
as_runner python3 -m app.runner.selftest --all --timeout 40 || failed=1

echo "==> control: the same network probe outside the sandbox must get through"
if as_runner python3 -c "import socket; socket.create_connection(('1.1.1.1', 53), timeout=5)" 2>/dev/null; then
  pass "network reachable outside the sandbox (the network probe is not vacuous)"
else
  echo "SKIP network control: the container itself has no outbound network"
fi

echo "==> polkit: tbr-runner may only start/stop taskbrigade-job@ units"
allowed_stop() { as_runner systemctl --no-ask-password stop taskbrigade-job@polkit-check.service; }
if allowed_stop >/dev/null 2>&1; then pass "stop taskbrigade-job@… allowed"; else fail "stop taskbrigade-job@… refused"; fi
for cmd in "restart polkit.service" "stop systemd-journald.service" "start getty@tty1.service" \
           "enable taskbrigade-job@x.service" "set-property taskbrigade-job@x.service MemoryMax=infinity" \
           "daemon-reload" "kill taskbrigade-runner.service"; do
  # shellcheck disable=SC2086
  if as_runner systemctl --no-ask-password $cmd >/dev/null 2>&1; then fail "tbr-runner may: systemctl $cmd"
  else pass "tbr-runner refused: systemctl $cmd"; fi
done
if docker exec "$NAME" sudo -u tbr-job systemctl --no-ask-password start taskbrigade-job@other.service \
     >/dev/null 2>&1; then fail "tbr-job may start sandbox jobs"; else pass "tbr-job refused: start taskbrigade-job@…"; fi

echo "==> end-to-end runner jobs (stub API)"
in_box env PYTHONPATH=/src python3 deploy/sandbox-test/e2e_job.py prepare
as_runner env PYTHONPATH=/src python3 deploy/sandbox-test/e2e_job.py run || failed=1

echo "==> the same jobs from inside taskbrigade-runner.service (its own hardening, polkit over D-Bus)"
docker exec "$NAME" bash -c 'echo "url = \"http://stub\"" > /etc/taskbrigade/runner.toml
  install -d /etc/systemd/system/taskbrigade-runner.service.d
  printf "[Service]\nType=oneshot\nRestart=no\nExecStart=\nExecStart=/usr/bin/env PYTHONPATH=/src /usr/bin/python3 /src/deploy/sandbox-test/e2e_job.py run\nWorkingDirectory=/src\n" \
    > /etc/systemd/system/taskbrigade-runner.service.d/90-e2e.conf
  systemctl daemon-reload'
if docker exec "$NAME" systemctl start taskbrigade-runner.service; then
  pass "e2e jobs inside taskbrigade-runner.service"
else
  fail "e2e jobs inside taskbrigade-runner.service"
fi
docker exec "$NAME" journalctl -u taskbrigade-runner.service --no-pager -o cat | grep -e '^PASS' -e '^FAIL' || true

echo "==> journal: no job output"
if docker exec "$NAME" journalctl --no-pager -o cat | grep -q -e SELFTEST-STDOUT-MARKER -e "aggregated 60 rows"; then
  fail "job stdout found in the journal"
else
  pass "job stdout stays out of the journal"
fi

[[ $failed == 0 ]] && echo "==> all checks passed" || echo "==> FAILED"
exit "$failed"
