#!/usr/bin/env bash
# Restore an archive from deploy/docker/backup.sh into this Compose installation (fresh or existing).
#   deploy/docker/restore.sh [--yes] ARCHIVE.tar
# Replaces the database, the state volume (master.key, secret.key) and, if the archive contains it, the Gitea
# volume. .env is not part of the backup: TBR_DB_PASSWORD and MARIADB_ROOT_PASSWORD come from the local .env.
# TaskBrigade migrates an older schema on its next start.
set -euo pipefail

# shellcheck source=common.sh
source "$(dirname "$0")/common.sh"

yes=0
if [[ "${1:-}" == "--yes" ]]; then
  yes=1
  shift
fi
[[ $# -eq 1 ]] || tm_die "usage: deploy/docker/restore.sh [--yes] ARCHIVE.tar"
archive="$1"
[[ -f "$archive" ]] || tm_die "$archive not found."
tm_require_compose

work="$(mktemp -d)"
trap 'rm -rf "$work"' EXIT
umask 077
tar xf "$archive" -C "$work"
for f in manifest.txt db.sql.gz state.tar.gz; do
  [[ -f "$work/$f" ]] || tm_die "$archive is not a TaskBrigade Docker backup ($f missing)."
done
gzip -t "$work/db.sql.gz" "$work/state.tar.gz" || tm_die "$archive is damaged."
with_gitea=0
[[ -f "$work/gitea.tar.gz" ]] && with_gitea=1

echo "Backup:"
sed 's/^/  /' "$work/manifest.txt"
echo "This replaces the TaskBrigade database and the state volume$([[ $with_gitea == 1 ]] && echo ", and the Gitea data") in $TBR_COMPOSE_DIR."
if [[ "$yes" != 1 ]]; then
  read -r -p "Type 'restore' to continue: " answer
  [[ "$answer" == "restore" ]] || tm_die "aborted."
fi

profile=()
[[ "$with_gitea" == 1 ]] && profile=(--profile gitea)

# On a fresh machine this creates the volumes (with Compose labels), the network and the images first.
dc ${profile[@]+"${profile[@]}"} create --no-recreate
echo "Stopping taskbrigade$([[ $with_gitea == 1 ]] && echo " and gitea") ..."
dc ${profile[@]+"${profile[@]}"} stop taskbrigade $([[ $with_gitea == 1 ]] && echo gitea) >/dev/null 2>&1 || true
dc up -d --wait mariadb

echo "Restoring the database ..."
# Recreate the database empty; the MariaDB user and its grants (from .env) stay as they are.
gunzip -c "$work/db.sql.gz" | dc exec -T mariadb sh -c 'export MYSQL_PWD="$MARIADB_ROOT_PASSWORD"
  mariadb -uroot -e "DROP DATABASE IF EXISTS \`$MARIADB_DATABASE\`;
    CREATE DATABASE \`$MARIADB_DATABASE\` CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;" &&
  exec mariadb -uroot --default-character-set=utf8mb4 "$MARIADB_DATABASE"'

echo "Restoring the state volume ($TBR_STATE_VOLUME) ..."
tm_volume_run "$TBR_STATE_VOLUME" /var/lib/taskbrigade "$(tm_service_image taskbrigade)" 10001 \
  'find /var/lib/taskbrigade -mindepth 1 -delete && tar xzf - -C /var/lib/taskbrigade' < "$work/state.tar.gz"

if [[ "$with_gitea" == 1 ]]; then
  echo "Restoring the Gitea volume ($TBR_GITEA_VOLUME) ..."
  tm_volume_run "$TBR_GITEA_VOLUME" /data "$(tm_service_image gitea)" 0 \
    'find /data -mindepth 1 -delete && tar xzpf - -C /data' < "$work/gitea.tar.gz"
fi

echo "Starting ..."
dc ${profile[@]+"${profile[@]}"} up -d --wait --no-build
echo "Restored from $archive. Sign in with the passwords of the backed-up installation."
