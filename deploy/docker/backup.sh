#!/usr/bin/env bash
# Backup of a Docker Compose installation: database dump, state volume (master.key, secret.key) and, if present,
# the Gitea volume of the "gitea" profile. Writes one archive taskbrigade-docker-<UTC timestamp>.tar.
#   deploy/docker/backup.sh [BACKUP_DIR]       (default: TBR_BACKUP_DIR or ./backups next to compose.yaml)
# TBR_BACKUP_KEEP_DAYS=14 deletes older archives in BACKUP_DIR; unset = keep everything.
# Restore with deploy/docker/restore.sh. The archive contains master.key – store it like a password.
set -euo pipefail

# shellcheck source=common.sh
source "$(dirname "$0")/common.sh"
tm_require_compose

BACKUP_DIR="${1:-${TBR_BACKUP_DIR:-$TBR_COMPOSE_DIR/backups}}"
KEEP_DAYS="${TBR_BACKUP_KEEP_DAYS:-}"
[[ -z "$KEEP_DAYS" || "$KEEP_DAYS" =~ ^[0-9]+$ ]] || tm_die "TBR_BACKUP_KEEP_DAYS must be a non-negative integer."
tm_service_running mariadb || tm_die "the mariadb service is not running. Start it: docker compose up -d mariadb"

work="$(mktemp -d)"
gitea_stopped=0
cleanup() {
  rm -rf "$work"
  if [[ "$gitea_stopped" == 1 ]]; then dc start gitea >/dev/null || true; fi
}
trap cleanup EXIT
umask 077

echo "Dumping the database ..."
# The root password stays in the container environment (MYSQL_PWD), never on a command line.
dc exec -T mariadb sh -c 'MYSQL_PWD="$MARIADB_ROOT_PASSWORD" exec mariadb-dump -uroot --single-transaction \
  --routines --triggers --default-character-set=utf8mb4 "$MARIADB_DATABASE"' | gzip > "$work/db.sql.gz"
# A failed dump still produces a gzip header; an empty or truncated dump has no completion marker.
[[ "$(gunzip -c "$work/db.sql.gz" | tail -n 1)" == "-- Dump completed"* ]] || tm_die "the database dump is incomplete."

echo "Archiving the state volume ($TBR_STATE_VOLUME) ..."
tm_volume_exists "$TBR_STATE_VOLUME" || tm_die "volume $TBR_STATE_VOLUME not found."
tm_volume_run "$TBR_STATE_VOLUME" /var/lib/taskbrigade "$(tm_service_image taskbrigade)" 10001 \
  'tar czf - -C /var/lib/taskbrigade .' > "$work/state.tar.gz"
tar tzf "$work/state.tar.gz" > "$work/state.list"
grep -q 'master.key$' "$work/state.list" || tm_die "master.key is missing in the state volume."

if tm_volume_exists "$TBR_GITEA_VOLUME"; then
  echo "Archiving the Gitea volume ($TBR_GITEA_VOLUME); Gitea is stopped meanwhile ..."
  if tm_service_running gitea; then
    dc stop gitea >/dev/null
    gitea_stopped=1
  fi
  tm_volume_run "$TBR_GITEA_VOLUME" /data "$(tm_service_image gitea)" 0 'tar czf - -C /data .' > "$work/gitea.tar.gz"
  if [[ "$gitea_stopped" == 1 ]]; then
    dc start gitea >/dev/null
    gitea_stopped=0
  fi
fi

{
  echo "created=$(date -u +%Y-%m-%dT%H:%M:%SZ)"
  echo "image=$(tm_service_field taskbrigade image)"
  echo "files=$(cd "$work" && echo *.gz)"
} > "$work/manifest.txt"

mkdir -p "$BACKUP_DIR"
out="$BACKUP_DIR/taskbrigade-docker-$(date -u +%Y%m%d-%H%M%S).tar"
tar cf "$out.part" -C "$work" manifest.txt db.sql.gz state.tar.gz $([[ -f "$work/gitea.tar.gz" ]] && echo gitea.tar.gz)
mv "$out.part" "$out"
if [[ -n "$KEEP_DAYS" ]]; then
  find "$BACKUP_DIR" -maxdepth 1 \( -name 'taskbrigade-docker-*.tar' -o -name 'taskmeister-docker-*.tar' \) \
    -mtime +"$KEEP_DAYS" -delete  # taskmeister-*: archives from before TaskBrigade 0.8
fi
echo "Backup: $out"
