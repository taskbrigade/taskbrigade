#!/usr/bin/env bash
# One-time migration of a native installation from the names before TaskBrigade 0.8 (taskmeister) to taskbrigade.
# deploy/update.sh runs it when it finds taskmeister.service or /etc/taskmeister; on its own:
#   sudo [TBR_BASE=/opt/taskbrigade] deploy/migrate-names.sh
# Nothing happens on an installation that is already migrated. Steps and the way back: docs/operations.md
# ("Migrating an installation from before TaskBrigade 0.8").
#
#   1. Backup: database dump (deploy/backup.sh of the installed checkout) and a tarball of /etc/taskmeister,
#      /var/lib/taskmeister (master.key, db.json) and the old systemd units, both in the backup directory.
#   2. Stop and disable taskmeister.service and taskmeister-backup.timer.
#   3. Rename the system user and group taskmeister → taskbrigade (same UID/GID, so file ownership stays).
#   4. Move /etc/taskmeister → /etc/taskbrigade (taskmeister.env → taskbrigade.env), /var/lib, /var/log, /var/backups
#      and the install prefix /opt/taskmeister → /opt/taskbrigade (a custom prefix stays where it is, unless
#      TBR_BASE names another one). The virtual environment is rebuilt by update.sh when the prefix moved.
#   5. Env file: TM_<X>= → TBR_<X>= (an existing TBR_<X> wins), old default paths → new ones.
#   6. Remove the old units; update.sh installs and starts the new ones.
# The database is not renamed: db.json in the state directory keeps naming it (default before 0.8: taskmeister).
set -euo pipefail
[[ $EUID -eq 0 ]] || { echo "error: run this script as root (sudo)." >&2; exit 1; }

DEPLOY_DIR="$(cd "$(dirname "$0")" && pwd)"
# shellcheck source=prereq.sh
source "$DEPLOY_DIR/prereq.sh"

UNITS=/etc/systemd/system
OLD=taskmeister
NEW=taskbrigade

if [[ ! -f $UNITS/$OLD.service && ! -d /etc/$OLD ]]; then
  echo "Nothing to migrate: no $OLD.service and no /etc/$OLD."
  exit 0
fi
[[ ! -e /etc/$NEW/$NEW.env ]] || tm_die "/etc/$NEW/$NEW.env exists next to /etc/$OLD – an installation with the new
names is already there. Compare both by hand and remove the one you do not use (docs/operations.md)."

old_app="$(sed -n 's/^WorkingDirectory=//p' "$UNITS/$OLD.service" 2>/dev/null | head -n 1)"
old_base="${TM_BASE:-$(dirname "${old_app:-/opt/$OLD/app}")}"
if [[ -n "${TBR_BASE:-}" ]]; then
  new_base="$TBR_BASE"
elif [[ $old_base == "/opt/$OLD" ]]; then
  new_base="/opt/$NEW"
else
  new_base="$old_base"  # A custom prefix stays.
fi
[[ -d $old_base/app/.git ]] || tm_die "No checkout at $old_base/app (set TM_BASE to the old prefix)."
[[ $new_base == "$old_base" || ! -e $new_base ]] || tm_die "$new_base already exists – move it away or set TBR_BASE."

PY="$(tm_require_python312 "${PY:-}")"
TBR_PY="$PY"
export TBR_PY
stamp="$(date -u +%Y%m%d-%H%M%S)"
backups="/var/backups/$OLD"
[[ -d $backups ]] || install -d -m 0750 "$backups"

echo "==> Migration 1/6: backup (database dump and configuration) in $backups"
env_file="/etc/$OLD/$OLD.env"
if [[ "${TBR_MIGRATE_SKIP_DB_BACKUP:-0}" == 1 ]]; then
  echo "    database dump skipped (TBR_MIGRATE_SKIP_DB_BACKUP=1)"
elif [[ -f $env_file ]] && id "$OLD" &>/dev/null; then
  (cd "$old_base" && sudo -u "$OLD" -H bash -c "set -a; . '$env_file'; set +a; cd '$old_base/app' \
    && TM_BACKUP_DIR='$backups' TBR_BACKUP_DIR='$backups' deploy/backup.sh") \
    || tm_die "The database backup failed – nothing was changed. Fix it, or take a dump yourself and re-run with
TBR_MIGRATE_SKIP_DB_BACKUP=1 (e.g. an installation still in setup mode without a database password)."
fi
saved=()
for path in "etc/$OLD" "var/lib/$OLD" "${UNITS#/}/$OLD.service" "${UNITS#/}/$OLD-backup.service" \
            "${UNITS#/}/$OLD-backup.timer"; do
  [[ -e /$path ]] && saved+=("$path")
done
tarball="$backups/$OLD-migration-$stamp.tar.gz"
(umask 077 && tar -czf "$tarball" -C / "${saved[@]}")
echo "    $tarball"

echo "==> Migration 2/6: stop $OLD.service"
systemctl stop "$OLD-backup.timer" "$OLD.service" 2>/dev/null || true
systemctl disable "$OLD.service" "$OLD-backup.timer" 2>/dev/null || true

echo "==> Migration 3/6: system user $OLD → $NEW"
if id "$OLD" &>/dev/null && ! id "$NEW" &>/dev/null; then
  usermod -l "$NEW" "$OLD"
  if getent group "$OLD" >/dev/null && ! getent group "$NEW" >/dev/null; then
    groupmod -n "$NEW" "$OLD"
  fi
  usermod -d "$new_base" "$NEW"
elif ! id "$NEW" &>/dev/null; then
  useradd --system --home "$new_base" --shell /usr/sbin/nologin "$NEW"
fi

echo "==> Migration 4/6: directories"
move() {
  local src="$1" dst="$2"
  [[ -e $src ]] || return 0
  [[ ! -e $dst ]] || tm_die "$dst already exists – $src was not moved. Resolve it by hand, then re-run."
  mv "$src" "$dst"
  echo "    $src → $dst"
}
move "/etc/$OLD" "/etc/$NEW"
move "/etc/$NEW/$OLD.env" "/etc/$NEW/$NEW.env"
move "/var/lib/$OLD" "/var/lib/$NEW"
move "/var/log/$OLD" "/var/log/$NEW"
move "/var/backups/$OLD" "/var/backups/$NEW"
if [[ $new_base != "$old_base" ]]; then
  move "$old_base" "$new_base"
  # Scripts in the virtual environment carry the old path; update.sh creates it again.
  rm -rf "$new_base/app/.venv"
fi
install -d -o "$NEW" -g "$NEW" -m 0700 "/var/lib/$NEW"
install -d -o "$NEW" -g "$NEW" -m 0750 "/var/log/$NEW" "/var/backups/$NEW"
chown -R "$NEW:$NEW" "$new_base" "/var/lib/$NEW"
chown root:"$NEW" "/etc/$NEW" "/etc/$NEW/$NEW.env"

echo "==> Migration 5/6: /etc/$NEW/$NEW.env (TM_* → TBR_*)"
tm_env_rename_legacy "/etc/$NEW/$NEW.env" "/var/backups/$OLD=/var/backups/$NEW" "/var/lib/$OLD=/var/lib/$NEW" \
  "$old_base=$new_base"

echo "==> Migration 6/6: remove the old systemd units"
rm -f "$UNITS/$OLD.service" "$UNITS/$OLD-backup.service" "$UNITS/$OLD-backup.timer"
systemctl daemon-reload

echo "Migration done (the database keeps its name). Backup: /var/backups/$NEW/$(basename "$tarball")"
echo "Way back: docs/operations.md, \"Migrating an installation from before TaskBrigade 0.8\"."
echo "NEW_BASE=$new_base"
