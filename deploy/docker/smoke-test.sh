#!/usr/bin/env bash
# Compose smoke test: `docker compose --profile gitea up`, /healthz, then one task through the agent workflow
# (create → tbr prepare → push → tbr submit → pull request in Gitea). Used by CI (.github/workflows/ci.yml).
#
# Runs as its own Compose project with its own ports, network and volumes (override below), so it does not touch
# a TaskBrigade stack on the same host. Removes everything again unless TBR_SMOKE_KEEP=1.
# Requirements: Docker with Compose v2.24+, git, curl, python3, uv (or TBR_SMOKE_TM pointing to `tbr`).
set -euo pipefail

ROOT=$(cd "$(dirname "$0")/../.." && pwd)
PROJECT=${TBR_SMOKE_PROJECT:-tm-smoke}
APP_PORT=${TBR_SMOKE_APP_PORT:-18090}
GITEA_PORT=${TBR_SMOKE_GITEA_PORT:-13300}
WORK=$(mktemp -d)

# Throwaway secrets for this run only; nothing is written to the repository.
rand() { python3 -c 'import secrets; print(secrets.token_urlsafe(24))'; }
export TBR_DB_PASSWORD MARIADB_ROOT_PASSWORD TBR_INITIAL_ADMIN_PASSWORD
TBR_DB_PASSWORD=$(rand)
MARIADB_ROOT_PASSWORD=$(rand)
TBR_INITIAL_ADMIN_PASSWORD=$(rand)
export TBR_PUBLISH=127.0.0.1:$APP_PORT \
       TBR_PUBLIC_URL=http://127.0.0.1:$APP_PORT \
       TBR_GITEA_PUBLISH=127.0.0.1:$GITEA_PORT \
       TBR_GITEA_SSH_PUBLISH=127.0.0.1:${TBR_SMOKE_GITEA_SSH_PORT:-12222} \
       GITEA_PUBLIC_URL=http://localhost:$GITEA_PORT \
       TBR_DOCKER_SUBNET=${TBR_SMOKE_SUBNET:-172.31.251.0/24} \
       TBR_DOCKER_GATEWAY=${TBR_SMOKE_GATEWAY:-172.31.251.1} \
       TRUSTED_PEER_NETWORKS=${TBR_SMOKE_GATEWAY:-172.31.251.1}/32 \
       TBR_RUNNER_SUBNET=${TBR_SMOKE_RUNNER_SUBNET:-172.31.253.0/24}

# Network and volume names are fixed in compose.yaml; rename them for the smoke project.
cat > "$WORK/compose.smoke.yaml" <<EOF
services:
  taskbrigade:
    environment:
      TBR_INITIAL_ADMIN_PASSWORD: \${TBR_INITIAL_ADMIN_PASSWORD}
networks:
  taskbrigade:
    name: $PROJECT
volumes:
  taskbrigade-db:
    name: $PROJECT-db
  taskbrigade-state:
    name: $PROJECT-state
  gitea-data:
    name: $PROJECT-gitea
  taskbrigade-data:
    name: $PROJECT-data
  taskbrigade-data-key:
    name: $PROJECT-data-key
  taskbrigade-sftp:
    name: $PROJECT-sftp
  taskbrigade-sftp-spool:
    name: $PROJECT-sftp-spool
EOF

# Hide API tokens in printed logs.
mask() { sed 's/tm_[A-Za-z0-9_-]\{20,\}/tm_***/g'; }

compose() {
  docker compose -p "$PROJECT" --project-directory "$ROOT" -f "$ROOT/compose.yaml" -f "$WORK/compose.smoke.yaml" \
    --profile gitea "$@"
}

cleanup() {
  status=$?
  if [ "$status" -ne 0 ]; then
    echo "--- smoke test failed (exit $status), container logs:" >&2
    compose logs --no-color --tail 80 | mask >&2 || true
  fi
  if [ "${TBR_SMOKE_KEEP:-0}" = 1 ]; then
    echo "TBR_SMOKE_KEEP=1: stack $PROJECT and $WORK kept" >&2
  else
    compose down -v --remove-orphans >/dev/null 2>&1 || true
    rm -rf "$WORK"
  fi
}
trap cleanup EXIT

step() { printf '\n==> %s\n' "$*"; }
json() { python3 -c "import json, sys; print(json.load(sys.stdin)$1)"; }

if [ -f "$ROOT/.env" ]; then
  echo "Warning: $ROOT/.env is read by the containers (env_file) and may change the result" >&2
fi

step "docker compose --profile gitea up (project $PROJECT)"
compose up -d --build --quiet-pull
# gitea-init is a one-shot container: wait for it, then read its exit code.
compose wait gitea-init >/dev/null || true
init_rc=$(docker inspect -f '{{.State.ExitCode}}' "$(compose ps -aq gitea-init)")
compose logs --no-color --no-log-prefix gitea-init | mask
[ "$init_rc" = 0 ] || { echo "gitea-init exited with $init_rc" >&2; exit 1; }

step "GET /healthz"
curl -fsS "http://127.0.0.1:$APP_PORT/healthz"
echo

# The demo agent's API token is printed once by gitea-init (fresh volumes: always this run).
TOKEN=$(compose logs --no-color --no-log-prefix gitea-init | grep -o 'tm_[A-Za-z0-9_-]\{20,\}' | head -n 1)
[ -n "$TOKEN" ] || { echo "No demo agent token in the gitea-init log" >&2; exit 1; }

AGENT="$WORK/agent"
mkdir -p "$AGENT"
printf 'url = "http://127.0.0.1:%s"\ntoken = "%s"\n' "$APP_PORT" "$TOKEN" > "$AGENT/.taskbrigade.toml"
tm() {
  if [ -n "${TBR_SMOKE_TM:-}" ]; then (cd "$AGENT" && "$TBR_SMOKE_TM" "$@")
  else (cd "$AGENT" && uv run --quiet --project "$ROOT" tbr "$@"); fi
}
export TBR_LANGUAGE=en GIT_AUTHOR_NAME=smoke GIT_AUTHOR_EMAIL=smoke@example.invalid \
       GIT_COMMITTER_NAME=smoke GIT_COMMITTER_EMAIL=smoke@example.invalid

step "Create a task (tm create)"
tm whoami > /dev/null
NR=$(tm create --project DEMO --title "Smoke test" --assign demo-agent \
       --description "Add smoke.txt." --acceptance "- smoke.txt exists" --json | json '["task_nr"]')
echo "created $NR"

step "Claim it (tm prepare --task $NR)"
tm prepare --task "$NR" > "$WORK/prepare.md"
head -n 5 "$WORK/prepare.md"
WT="$AGENT/DEMO/.worktrees/$NR"
BRANCH=$(git -C "$WT" rev-parse --abbrev-ref HEAD)

step "Commit, push and submit ($BRANCH)"
echo "smoke $(date -u +%FT%TZ)" > "$WT/smoke.txt"
git -C "$WT" add smoke.txt
git -C "$WT" commit --quiet -m "$NR smoke test"
git -C "$WT" push --quiet -u origin HEAD
SHA=$(git -C "$WT" rev-parse HEAD)
tm submit "$NR" --branch "$BRANCH" --sha "$SHA" --summary "Smoke test from CI"

step "Check status and pull request"
STATUS=$(tm show "$NR" --json | json '["status"]')
echo "status: $STATUS"
[ "$STATUS" = for_review ] || { echo "expected for_review" >&2; exit 1; }
PULLS=$(curl -fsS -u "giteaadmin:$TBR_INITIAL_ADMIN_PASSWORD" \
  "http://127.0.0.1:$GITEA_PORT/api/v1/repos/taskbrigade/demo/pulls?state=open")
echo "$PULLS" | python3 -c "
import json, sys
pulls = [p for p in json.load(sys.stdin) if p['head']['ref'] == sys.argv[1]]
assert pulls, 'no open pull request for ' + sys.argv[1]
print('pull request:', pulls[0]['html_url'])" "$BRANCH"

step "Smoke test passed"
