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

REPO_URL="${REPO_URL:-$(git -c safe.directory='*' -C "$APP" remote get-url origin)}"
as_tm() { (cd "$BASE" && sudo -u taskbrigade -H env UV_CACHE_DIR="$BASE/.cache/uv" "$@"); }

echo "==> Update $APP from $REPO_URL (branch main)"
git -c safe.directory='*' -C "$APP" pull --ff-only "$REPO_URL" main
chown -R taskbrigade:taskbrigade "$APP"
as_tm bash -c "cd '$APP' && '$UV' sync --frozen --no-dev --python '$PY'"
as_tm bash -c "cd '$APP' && rm -f dist/taskmeister-*.whl dist/taskbrigade-*.whl && '$UV' build --wheel --out-dir dist --python '$PY'" \
  || echo "    warning: building the tbr wheel failed; the server retries on the first /install.sh request"
as_tm bash -c "set -a; . '$ETC'; set +a; cd '$APP' && deploy/backup.sh"
as_tm bash -c "set -a; . '$ETC'; set +a; cd '$APP' && .venv/bin/tbr-admin upgrade"

echo "==> systemd units"
# As in install.sh: the units ship with /opt/taskbrigade; a different TBR_BASE is substituted.
for unit in taskbrigade.service taskbrigade-backup.service taskbrigade-backup.timer; do
  sed "s|/opt/taskbrigade|$BASE|g" "$APP/deploy/systemd/$unit" > "/etc/systemd/system/$unit.new"
  chmod 0644 "/etc/systemd/system/$unit.new"
  mv "/etc/systemd/system/$unit.new" "/etc/systemd/system/$unit"
done
systemctl daemon-reload
systemctl enable taskbrigade.service taskbrigade-backup.timer
systemctl start taskbrigade-backup.timer
systemctl restart taskbrigade
systemctl --no-pager --lines=5 status taskbrigade
