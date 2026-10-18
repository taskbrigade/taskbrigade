#!/usr/bin/env bash
# As root: check prerequisites → (once) migrate the names → git pull → uv sync, tbr wheel → backup → schema upgrade →
# systemd units → restart.
# No personal paths. Override the checkout or the uv binary; do not edit this script.
#   sudo [UV=/path/to/uv] [PY=/usr/bin/python3.12] [REPO_URL=<source>] [TBR_BASE=/opt/taskbrigade] deploy/update.sh
# A down Gitea is reported and does not block the upgrade. MariaDB client >= 10.5 is required
# because the script takes a backup first.
# An installation from before TaskBrigade 0.8 (taskmeister.service, /opt/taskmeister, /etc/taskmeister …) is first
# moved to the new names by deploy/migrate-names.sh – backup first, data and database name unchanged. Pull the new
# version into the old checkout before (docs/operations.md), then run this script from there.
set -euo pipefail
[[ $EUID -eq 0 ]] || { echo "error: run this script as root (sudo)." >&2; exit 1; }

DEPLOY_DIR="$(cd "$(dirname "$0")" && pwd)"
# shellcheck source=prereq.sh
source "$DEPLOY_DIR/prereq.sh"

if [[ -f /etc/systemd/system/taskmeister.service || -d /etc/taskmeister ]]; then
  echo "==> Installation from before TaskBrigade 0.8: migrating the names first"
  log="$(mktemp)"
  "$DEPLOY_DIR/migrate-names.sh" | tee "$log"
  new_base="$(sed -n 's/^NEW_BASE=//p' "$log")"
  rm -f "$log"
  [[ -n $new_base ]] || tm_die "the migration did not report the new install prefix."
  # This script may have been moved with the checkout: continue with the copy at the new place.
  exec env TBR_BASE="$new_base" "$new_base/app/deploy/update.sh" "$@"
fi

BASE="${TBR_BASE:-/opt/taskbrigade}"
APP=$BASE/app
ETC=/etc/taskbrigade/taskbrigade.env
[[ -d $APP/.git ]] || tm_die "No checkout at $APP. Run deploy/install.sh first (TBR_BASE overrides the prefix)."
[[ -f $ETC ]] || tm_die "Missing $ETC. Run deploy/install.sh first."

echo "==> Prerequisites"
PY="$(tm_require_python312 "${PY:-}")"
UV="$(tm_require_uv "${UV:-}")"
TBR_PY="$PY"
export TBR_PY
tm_require_git
tm_require_systemd
tm_require_mariadb_client
tm_check_gitea "$(tm_env_get "$ETC" GITEA_URL)" report
# Keys written by hand with the old names after the migration (e.g. from an old guide).
tm_env_rename_legacy "$ETC"
# The opt-in data zones must be re-provisioned on every update (idempotent) so a changed layout is applied.
# TBR_DATA_ZONES from the environment wins over a key set earlier with `tbr-admin config set`.
if [[ "${TBR_DATA_ZONES:-}" != "1" ]]; then
  TBR_DATA_ZONES="$(tm_env_get "$ETC" TBR_DATA_ZONES)"
fi
export TBR_DATA_ZONES
DATA_DIR="$(tm_env_get "$ETC" TBR_DATA_DIR)"
DATA_DIR="${DATA_DIR:-$(tbr_data_dir_path)}"
DATA_KEY="$(tm_env_get "$ETC" TBR_DATA_KEY_FILE)"
DATA_KEY="${DATA_KEY:-$(tbr_data_key_path)}"
DATA_GROUP="$(tbr_data_group)"
# The workflow scheduler trigger: native timer (default), app (Docker), or off.
WORKFLOW_TICK="${TBR_WORKFLOW_TICK:-$(tm_env_get "$ETC" TBR_WORKFLOW_TICK)}"
WORKFLOW_TICK="${WORKFLOW_TICK:-timer}"

REPO_URL="${REPO_URL:-$(git -c safe.directory='*' -C "$APP" remote get-url origin)}"
as_tm() { (cd "$BASE" && sudo -u taskbrigade -H env UV_CACHE_DIR="$BASE/.cache/uv" "$@"); }

echo "==> Update $APP from $REPO_URL (branch main)"
git -c safe.directory='*' -C "$APP" pull --ff-only "$REPO_URL" main
chown -R taskbrigade:taskbrigade "$APP"
as_tm bash -c "cd '$APP' && '$UV' sync --frozen --no-dev --python '$PY'"
as_tm bash -c "cd '$APP' && rm -f dist/taskmeister-*.whl dist/taskbrigade-*.whl && '$UV' build --wheel --out-dir dist --python '$PY'" \
  || echo "    warning: building the tbr wheel failed; the server retries on the first /install.sh request"
# Stop the workflow tick around the migration; the tick itself also checks the schema revision.
systemctl stop taskbrigade-workflows.timer 2>/dev/null || true
as_tm bash -c "set -a; . '$ETC'; set +a; cd '$APP' && deploy/backup.sh"
as_tm bash -c "set -a; . '$ETC'; set +a; cd '$APP' && .venv/bin/tbr-admin upgrade"

echo "==> Data zones (TBR_DATA_ZONES=${TBR_DATA_ZONES:-0})"
if tbr_data_zones_enabled; then
  tm_require_data_zone_host
  tm_system_group "$DATA_GROUP"
  tm_system_user tbr-runner
  tm_system_user tbr-job
  tm_add_to_group taskbrigade "$DATA_GROUP"
  tm_add_to_group tbr-runner "$DATA_GROUP"
  tm_add_to_group tbr-runner tbr-job
  tm_ensure_dir "$DATA_DIR" "taskbrigade:$DATA_GROUP" 0750
  tm_ensure_dir "$DATA_DIR/store" "taskbrigade:$DATA_GROUP" 0750
  tm_ensure_dir "$DATA_DIR/incoming" "taskbrigade:$DATA_GROUP" 0750
  tm_ensure_dir "$DATA_DIR/quarantine" "tbr-runner:$DATA_GROUP" 0750
  tm_ensure_dir /var/lib/taskbrigade-runner tbr-runner:tbr-runner 0700
  if [[ ! -f "$DATA_KEY" ]]; then
    echo "    creating the data store key (KEK) at $DATA_KEY"
    tmp_key="$(mktemp)"
    "$PY" -c 'import base64, secrets, sys; open(sys.argv[1], "wb").write(base64.b64encode(secrets.token_bytes(32)) + b"\n")' "$tmp_key"
    install -o root -g "$DATA_GROUP" -m 0640 "$tmp_key" "$DATA_KEY"
    rm -f "$tmp_key"
  fi
  chown root:"$DATA_GROUP" "$DATA_KEY"
  chmod 0640 "$DATA_KEY"
  RUNNER_VENV="${TBR_RUNNER_VENV:-$BASE/runner-venv}"
  if [[ ! -x "$RUNNER_VENV/bin/python" ]]; then
    "$UV" venv --python "$PY" "$RUNNER_VENV"
  fi
  "$UV" pip install --python "$RUNNER_VENV/bin/python" --quiet -r "$APP/deploy/runner-requirements.txt" \
    || echo "    warning: installing the runner packages failed; rerun deploy/update.sh later"
  chown -R root:root "$RUNNER_VENV"
  chmod -R go-w "$RUNNER_VENV" || true
  install -d -o root -g root -m 0755 /etc/polkit-1/rules.d
  install -o root -g root -m 0644 "$APP/deploy/polkit/60-taskbrigade-runner.rules" \
    /etc/polkit-1/rules.d/60-taskbrigade-runner.rules
  # Native SFTP inbox (concept §4): sshd drop-in, group, chroot root and the import path/service units.
  if [[ "${TBR_DATA_SFTP:-}" != "1" ]]; then
    TBR_DATA_SFTP="$(tm_env_get "$ETC" TBR_DATA_SFTP)"
  fi
  export TBR_DATA_SFTP
  if [[ "${TBR_DATA_SFTP:-}" == "1" ]]; then
    tm_install_sftp_inbox "$APP"
  else
    # Switch off: stop and disable the import units, keep the users, the chroot tree and the drop-in file.
    for unit in taskbrigade-data-import.path taskbrigade-data-import.service; do
      if systemctl list-unit-files "$unit" >/dev/null 2>&1 && [[ -f "/etc/systemd/system/$unit" ]]; then
        systemctl disable --now "$unit" 2>/dev/null || true
      fi
    done
  fi
  tm_env_set "$ETC" TBR_DATA_DIR "$DATA_DIR"
  tm_env_set "$ETC" TBR_DATA_KEY_FILE "$DATA_KEY"
  tm_env_set "$ETC" TBR_DATA_ZONES 1
  # Runner service and sandbox template with this host's paths; limits from Admin → Settings (defaults when the
  # database cannot be read).
  limits="$(as_tm bash -c "set -a; . '$ETC'; set +a; '$APP/.venv/bin/tbr-admin' runner limits" 2>/dev/null)" \
    || limits="$("$APP/.venv/bin/tbr-admin" runner limits --defaults)"
  tbr_install_runner_units "$APP" "$BASE" "$DATA_DIR" "$RUNNER_VENV" "$limits"
else
  # Switch off: stop and disable the data-zone units if they are installed; keep users, data and key.
  for unit in taskbrigade-runner.service taskbrigade-job@.service; do
    if systemctl list-unit-files "$unit" >/dev/null 2>&1 && [[ -f "/etc/systemd/system/$unit" ]]; then
      systemctl disable --now "$unit" 2>/dev/null || true
    fi
  done
  echo "    data zones off: units stopped/disabled, data and key unchanged"
fi

echo "==> systemd units"
# As in install.sh: the units ship with /opt/taskbrigade; a different TBR_BASE is substituted.
for unit in taskbrigade.service taskbrigade-backup.service taskbrigade-backup.timer \
           taskbrigade-workflows.service taskbrigade-workflows.timer; do
  sed "s|/opt/taskbrigade|$BASE|g" "$APP/deploy/systemd/$unit" > "/etc/systemd/system/$unit.new"
  chmod 0644 "/etc/systemd/system/$unit.new"
  mv "/etc/systemd/system/$unit.new" "/etc/systemd/system/$unit"
done
if tbr_data_zones_enabled; then
  install -d -o root -g root -m 0755 /etc/systemd/system/taskbrigade.service.d
  cat > /etc/systemd/system/taskbrigade.service.d/50-data-zones.conf <<EOF
[Service]
SupplementaryGroups=$DATA_GROUP
ReadWritePaths=$DATA_DIR
EOF
  chmod 0644 /etc/systemd/system/taskbrigade.service.d/50-data-zones.conf
fi
systemctl daemon-reload
if tbr_data_zones_enabled; then
  systemctl try-restart taskbrigade-runner.service || true  # Only if `tbr-admin runner join` started it.
fi
systemctl enable taskbrigade.service taskbrigade-backup.timer
systemctl start taskbrigade-backup.timer
if [[ "$WORKFLOW_TICK" == "timer" ]]; then
  systemctl enable taskbrigade-workflows.timer
  systemctl start taskbrigade-workflows.timer
else
  systemctl disable --now taskbrigade-workflows.timer 2>/dev/null || true
fi
systemctl restart taskbrigade
systemctl --no-pager --lines=5 status taskbrigade
