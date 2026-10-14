#!/usr/bin/env bash
# Idempotent install on a systemd host (run as root). No Docker.
# On a fresh Ubuntu 24.04 you do not edit this script. Install flow:
#   1. Packages that apt can provide (python3.12, git, curl, mariadb-client) are installed if missing.
#   2. uv is downloaded to /usr/local/bin when it is not already on PATH (TBR_INSTALL_UV=0 skips that).
#   3. The script refuses to continue unless Python is 3.12, uv runs, git and systemd exist,
#      and the MariaDB *client* is >= 10.5. The MariaDB *server* may be local or remote;
#      its password is not set here (Admin → Database on first start).
#   4. Gitea is probed only when GITEA_URL is a real URL. The example host warns and continues.
#   5. Tailscale is optional. TAILSCALE_SERVE=1 fails with install instructions if it is missing.
#
#   sudo [UV=/path/to/uv] [PY=/usr/bin/python3.12] \
#        [REPO_URL=<git url or local checkout>] \
#        [TBR_BASE=/opt/taskbrigade] \
#        [TBR_DB_HOST=127.0.0.1] [TBR_DB_PORT=3306] [TBR_DB_NAME=taskbrigade] [TBR_DB_USER=taskadmin] \
#        [GITEA_URL=http://127.0.0.1:3001] \
#        [LAN_UI_NETWORK=192.168.1.0/24] \
#        [UI_ALLOWED_LOGINS=admin@example.org] \
#        [BOOTSTRAP_ADMIN_ACTOR=admin] \
#        [TZ_DISPLAY=Europe/Berlin] \
#        [TAILSCALE_SERVE=1] \
#        [TBR_INSTALL_UV=1] \
#        deploy/install.sh
#
# TBR_BASE is the install prefix (the checkout lives in $TBR_BASE/app).
# LAN_UI_NETWORK also exposes the UI over plain HTTP on that LAN; the API stays blocked.
# UI_ALLOWED_LOGINS lists Tailscale logins that get admin access (bootstrap).
# TAILSCALE_SERVE=1 runs `tailscale serve --https=443`.
#
# Full walkthrough: docs/quickstart.md
set -euo pipefail
[[ $EUID -eq 0 ]] || { echo "error: run this script as root (sudo)." >&2; exit 1; }

DEPLOY_DIR="$(cd "$(dirname "$0")" && pwd)"
# shellcheck source=prereq.sh
source "$DEPLOY_DIR/prereq.sh"

if [[ -f /etc/systemd/system/taskmeister.service || -d /etc/taskmeister ]]; then
  tm_die "This host runs an installation from before TaskBrigade 0.8 (taskmeister.service, /etc/taskmeister).
Do not install a second one next to it: update it with deploy/update.sh, which migrates it to the new names
(docs/operations.md)."
fi

SRC_DIR="$(cd "$DEPLOY_DIR/.." && pwd)"
REPO_URL="${REPO_URL:-$SRC_DIR}"
LAN_UI_NETWORK="${LAN_UI_NETWORK:-}"
UI_ALLOWED_LOGINS="${UI_ALLOWED_LOGINS:-}"
TAILSCALE_SERVE="${TAILSCALE_SERVE:-0}"
PORT=8090

echo "==> 0. Prerequisites"
tm_apt_ensure() {
  local missing=() cmd pkg
  while [[ $# -ge 2 ]]; do
    cmd="$1"
    pkg="$2"
    shift 2
    if ! command -v "$cmd" >/dev/null 2>&1; then
      missing+=("$pkg")
    fi
  done
  if [[ ${#missing[@]} -eq 0 ]]; then
    return 0
  fi
  if ! command -v apt-get >/dev/null 2>&1; then
    tm_die "These commands are missing, and apt-get is not available to install them: ${missing[*]}.
Install Python 3.12, git, curl, and a MariaDB client >= 10.5, then re-run."
  fi
  echo "    installing packages: ${missing[*]}"
  DEBIAN_FRONTEND=noninteractive apt-get update
  DEBIAN_FRONTEND=noninteractive apt-get install -y "${missing[@]}"
}
# A MariaDB client may be named mariadb or only ship mariadb-dump. Don't apt-install over it;
# the version check below still rejects MySQL and anything older than 10.5.
if ! command -v mariadb >/dev/null 2>&1 && ! command -v mariadb-dump >/dev/null 2>&1; then
  tm_apt_ensure python3.12 python3.12 git git curl curl mariadb mariadb-client
else
  tm_apt_ensure python3.12 python3.12 git git curl curl
fi

if [[ -z "${UV:-}" ]] && ! command -v uv >/dev/null 2>&1 && [[ ! -x /usr/local/bin/uv ]]; then
  if [[ "${TBR_INSTALL_UV:-1}" != "1" ]]; then
    tm_die "uv was not found and TBR_INSTALL_UV=0.
Install uv and re-run with UV=/path/to/uv.
https://docs.astral.sh/uv/getting-started/installation/"
  fi
  echo "    uv not found; downloading the official installer to /usr/local/bin"
  uv_inst="$(mktemp)"
  curl -LsSf https://astral.sh/uv/install.sh -o "$uv_inst"
  env UV_INSTALL_DIR=/usr/local/bin UV_NO_MODIFY_PATH=1 sh "$uv_inst"
  rm -f "$uv_inst"
fi

PY="$(tm_require_python312 "${PY:-}")"
UV="$(tm_require_uv "${UV:-}")"
TBR_PY="$PY"
export TBR_PY
tm_require_git
tm_require_systemd
tm_require_mariadb_client

BASE="${TBR_BASE:-/opt/taskbrigade}"
APP=$BASE/app
ETC=/etc/taskbrigade
STATE=/var/lib/taskbrigade
as_tm() { (cd "$BASE" && sudo -u taskbrigade -H env UV_CACHE_DIR="$BASE/.cache/uv" "$@"); }
# Git runs as root (the source checkout may be unreadable for the taskbrigade user); chown follows.
git_root() { git -c safe.directory='*' "$@"; }

echo "==> 1. User and directories ($BASE)"
id taskbrigade &>/dev/null || useradd --system --home "$BASE" --shell /usr/sbin/nologin taskbrigade
install -d -o taskbrigade -g taskbrigade -m 0755 "$BASE"
install -d -o taskbrigade -g taskbrigade -m 0700 "$STATE"
install -d -o taskbrigade -g taskbrigade -m 0750 /var/log/taskbrigade /var/backups/taskbrigade
install -d -o root -g taskbrigade -m 0750 "$ETC"

echo "==> 2. Checkout at $APP (branch main)"
if [[ -d $APP/.git ]]; then
  git_root -C "$APP" pull --ff-only "$REPO_URL" main
else
  git_root clone --branch main "$REPO_URL" "$APP"
fi
chown -R taskbrigade:taskbrigade "$APP"

echo "==> 3. Dependencies (uv sync --frozen) and the tbr wheel for agents (/install.sh)"
as_tm bash -c "cd '$APP' && '$UV' sync --frozen --no-dev --python '$PY'"
as_tm bash -c "cd '$APP' && rm -f dist/taskmeister-*.whl dist/taskbrigade-*.whl && '$UV' build --wheel --out-dir dist --python '$PY'" \
  || echo "    warning: building the tbr wheel failed; the server retries on the first /install.sh request"

echo "==> 4. Configuration, master.key"
if [[ ! -f $ETC/taskbrigade.env ]]; then
  install -o root -g taskbrigade -m 0640 "$APP/.env.example" "$ETC/taskbrigade.env"
  secret="$("$PY" -c 'import secrets; print(secrets.token_urlsafe(48))')"
  tm_env_set "$ETC/taskbrigade.env" SECRET_KEY "$secret"
  echo "    created $ETC/taskbrigade.env (SECRET_KEY filled in)"
else
  echo "    keeping existing $ETC/taskbrigade.env"
fi
tm_env_set "$ETC/taskbrigade.env" GITEA_URL "${GITEA_URL:-}"
tm_env_set "$ETC/taskbrigade.env" TBR_DB_HOST "${TBR_DB_HOST:-}"
tm_env_set "$ETC/taskbrigade.env" TBR_DB_PORT "${TBR_DB_PORT:-}"
tm_env_set "$ETC/taskbrigade.env" TBR_DB_NAME "${TBR_DB_NAME:-}"
tm_env_set "$ETC/taskbrigade.env" TBR_DB_USER "${TBR_DB_USER:-}"
tm_env_set "$ETC/taskbrigade.env" UI_ALLOWED_LOGINS "$UI_ALLOWED_LOGINS"
tm_env_set "$ETC/taskbrigade.env" BOOTSTRAP_ADMIN_ACTOR "${BOOTSTRAP_ADMIN_ACTOR:-}"
tm_env_set "$ETC/taskbrigade.env" TZ_DISPLAY "${TZ_DISPLAY:-}"
if [[ -n $LAN_UI_NETWORK ]]; then
  tm_env_set "$ETC/taskbrigade.env" TBR_BIND_HOST "0.0.0.0"
  tm_env_set "$ETC/taskbrigade.env" LAN_UI_NETWORKS "$LAN_UI_NETWORK"
  if command -v ufw &>/dev/null && ufw status | grep -q "Status: active"; then
    ufw allow from "$LAN_UI_NETWORK" to any port "$PORT" proto tcp comment "TaskBrigade UI (LAN)"
  else
    echo "    note: ufw is not active; the app itself limits UI access to $LAN_UI_NETWORK"
  fi
fi
as_tm env TBR_STATE_DIR="$STATE" "$APP/.venv/bin/tbr-admin" init-key

echo "==> 5. Database and Gitea"
echo "    No database password is written here. After startup: Admin → Database → test, save, upgrade schema."
gitea_url="${GITEA_URL:-$(tm_env_get "$ETC/taskbrigade.env" GITEA_URL)}"
tm_check_gitea "$gitea_url" warn

echo "==> 6. systemd"
# Units ship with /opt/taskbrigade; a different TBR_BASE is substituted here.
for unit in taskbrigade.service taskbrigade-backup.service taskbrigade-backup.timer; do
  sed "s|/opt/taskbrigade|$BASE|g" "$APP/deploy/systemd/$unit" > "/etc/systemd/system/$unit"
  chmod 0644 "/etc/systemd/system/$unit"
done
systemctl daemon-reload
systemctl enable taskbrigade.service taskbrigade-backup.timer
systemctl restart taskbrigade.service
systemctl start taskbrigade-backup.timer

echo "==> 7. Tailscale (optional)"
tm_check_tailscale "$TAILSCALE_SERVE"
if [[ $TAILSCALE_SERVE == 1 ]]; then
  tailscale serve --bg --https=443 "http://127.0.0.1:$PORT"
  tailscale serve status
fi

sleep 2
echo
echo "TaskBrigade: http://127.0.0.1:$PORT"
echo "Logs: journalctl -u taskbrigade -f"
curl -s -o /dev/null -w "  /healthz → HTTP %{http_code} (503 = setup mode, expected before the database is saved)\n" \
  "http://127.0.0.1:$PORT/healthz" || echo "  service is not answering yet; check journalctl -u taskbrigade"
if [[ $TAILSCALE_SERVE != 1 ]]; then
  echo "TLS/tailnet (optional): tailscale serve --bg --https=443 http://127.0.0.1:$PORT"
fi
if [[ -n $LAN_UI_NETWORK ]]; then
  lan_ip="$(hostname -I 2>/dev/null | awk '{print $1}')"
  echo "LAN (UI only, API stays blocked): http://${lan_ip:-<this-host>}:$PORT"
fi
if ! grep -q '^BOOTSTRAP_ADMIN_PASSWORD_HASH=.\+' "$ETC/taskbrigade.env"; then
  echo "Next: set BOOTSTRAP_ADMIN_PASSWORD_HASH. See docs/quickstart.md"
  echo "  sudo -u taskbrigade $APP/.venv/bin/tbr-admin hash-password"
  echo "  sudo $APP/.venv/bin/tbr-admin config set BOOTSTRAP_ADMIN_PASSWORD_HASH --restart   # paste the hash"
fi
echo "Back up the master key separately: $STATE/master.key"
echo "Walkthrough: $APP/docs/quickstart.md"
