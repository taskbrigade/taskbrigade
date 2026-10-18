#!/usr/bin/env bash
# mariadb-dump over the network, gzip, rotate. The password lives only in a temporary mode-0600 options file.
# Requires MariaDB client tools >= 10.5 (mariadb-dump). No host-specific paths.
#   TBR_BACKUP_DIR=/var/backups/taskbrigade TBR_BACKUP_KEEP_DAYS=14 deploy/backup.sh
set -euo pipefail

DEPLOY_DIR="$(cd "$(dirname "$0")" && pwd)"
# shellcheck source=prereq.sh
source "$DEPLOY_DIR/prereq.sh"

# TM_*: names before TaskBrigade 0.8. Without a setting, an installation that is not migrated yet keeps its directory.
BACKUP_DIR="${TBR_BACKUP_DIR:-${TM_BACKUP_DIR:-}}"
if [[ -z $BACKUP_DIR ]]; then
  BACKUP_DIR=/var/backups/taskbrigade
  [[ -d $BACKUP_DIR || ! -d /var/backups/taskmeister ]] || BACKUP_DIR=/var/backups/taskmeister
fi
KEEP_DAYS="${TBR_BACKUP_KEEP_DAYS:-${TM_BACKUP_KEEP_DAYS:-14}}"
APP_DIR="$(cd "$DEPLOY_DIR/.." && pwd)"
[[ "$KEEP_DAYS" =~ ^[0-9]+$ ]] || tm_die "TBR_BACKUP_KEEP_DAYS must be a non-negative integer (got '$KEEP_DAYS')."
# tbr-admin since TaskBrigade 0.5; tm-admin for a checkout whose venv was not synced yet.
ADMIN="$APP_DIR/.venv/bin/tbr-admin"
[[ -x "$ADMIN" ]] || ADMIN="$APP_DIR/.venv/bin/tm-admin"
[[ -x "$ADMIN" ]] || tm_die "$APP_DIR/.venv/bin/tbr-admin is missing. Run deploy/install.sh first."
tm_require_mariadb_dump

err="$(mktemp)"
cnf=""
cleanup() { rm -f ${cnf:+"$cnf"} "$err"; }
trap cleanup EXIT
if ! cnf="$(TBR_NO_RENAME_NOTICE=1 "$ADMIN" print-db-cnf 2>"$err")"; then
  echo "error: backup could not read the database password." >&2
  echo "Save it in the UI (Admin → Database) or with: tbr-admin set-db-password" >&2
  echo "Until then the daily backup timer fails. That is expected on a fresh install." >&2
  cat "$err" >&2
  exit 1
fi
db="$(TBR_NO_RENAME_NOTICE=1 "$ADMIN" db-name)"

mkdir -p "$BACKUP_DIR"
out="$BACKUP_DIR/taskbrigade-$(date -u +%Y%m%d-%H%M%S).sql.gz"
mariadb-dump --defaults-extra-file="$cnf" --single-transaction --routines --triggers "$db" | gzip > "$out.part"
mv "$out.part" "$out"

# Encrypted data store (concept §9): only with TBR_BACKUP_DATA=1. The KEK (TBR_DATA_KEY_FILE) is never part of
# this archive – store it separately, otherwise a leaked backup plus the key would open every dataset.
DATA_ARCHIVE=""
if [[ "${TBR_BACKUP_DATA:-}" == "1" ]]; then
  DATA_DIR="${TBR_DATA_DIR:-/srv/taskbrigade-data}"
  STORE="$DATA_DIR/store"
  if [[ -d "$STORE" ]]; then
    DATA_ARCHIVE="$BACKUP_DIR/taskbrigade-data-$(date -u +%Y%m%d-%H%M%S).tar.gz"
    tar czf "$DATA_ARCHIVE.part" -C "$DATA_DIR" store
    mv "$DATA_ARCHIVE.part" "$DATA_ARCHIVE"
    echo "Backup (data store, ciphertext): $DATA_ARCHIVE"
  else
    echo "warning: TBR_BACKUP_DATA=1 but $STORE does not exist; skipping the data store." >&2
  fi
fi
# taskmeister-*: dumps from before TaskBrigade 0.8 rotate out the same way.
find "$BACKUP_DIR" \( -name 'taskbrigade-*.sql.gz' -o -name 'taskmeister-*.sql.gz' -o -name 'taskbrigade-data-*.tar.gz' \) -mtime +"$KEEP_DAYS" -delete
echo "Backup: $out"
