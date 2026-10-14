#!/usr/bin/env bash
# One-time migration of a Docker Compose installation from the names before TaskBrigade 0.8 (project, volumes and
# network `taskmeister*`) to `taskbrigade*`. deploy/docker/update.sh runs it when the volume taskmeister-db exists and
# taskbrigade-db does not; on its own: deploy/docker/migrate-names.sh. Nothing happens once migrated.
#
#   1. .env: TM_<X>= → TBR_<X>= (backup .env.bak-<timestamp>); the old defaults that differ now are written
#      explicitly – database and user `taskmeister`, Tailscale host name `taskmeister` – so nothing changes for
#      the running data and the tailnet address.
#   2. Database dump of the old stack into the backup directory (default ./backups).
#   3. Stop and remove the old containers and the network `taskmeister` (its subnet is reused).
#   4. Copy every old volume into the new one (taskmeister-db → taskbrigade-db, -state, -gitea, -tailscale-state).
#      The old volumes stay untouched – they are the way back (docs/docker.md) – until you remove them yourself.
set -euo pipefail

# shellcheck source=common.sh
source "$(dirname "$0")/common.sh"

OLD=taskmeister
NEW=taskbrigade
VOLUMES=(db state gitea tailscale-state)
# Key of each volume in compose.yaml/compose.tailscale.yaml: with these labels Compose treats the copy as its own.
# (No associative array: macOS still ships bash 3.2.)
compose_key() {
  case "$1" in
    gitea) echo gitea-data ;;
    tailscale-state) echo tailscale-state ;;
    *) echo "$NEW-$1" ;;
  esac
}

tm_volume_exists "$OLD-db" || { echo "Nothing to migrate: no volume $OLD-db."; exit 0; }
! tm_volume_exists "$NEW-db" || { echo "Nothing to migrate: volume $NEW-db exists already."; exit 0; }
env_file="$TBR_COMPOSE_DIR/.env"
[[ -f $env_file ]] || tm_die "$env_file not found (see docs/docker.md)."
stamp="$(date -u +%Y%m%d-%H%M%S)"

echo "==> Migration 1/4: $env_file (TM_* → TBR_*)"
cp -p "$env_file" "$env_file.bak-$stamp"
tm_env_migrate "$env_file"
# Defaults before 0.8 that differ now: keep them for this installation.
tm_env_default "$env_file" TBR_DB_NAME "$OLD"
tm_env_default "$env_file" TBR_DB_USER "$OLD"
if tm_volume_exists "$OLD-tailscale-state"; then
  tm_env_default "$env_file" TBR_TS_HOSTNAME "$OLD"
fi
echo "    previous version: $env_file.bak-$stamp"

echo "==> Migration 2/4: database dump of the old stack"
db="$(docker ps -q --filter "label=com.docker.compose.project=$OLD" --filter "label=com.docker.compose.service=mariadb")"
backups="${TBR_BACKUP_DIR:-$TBR_COMPOSE_DIR/backups}"
if [[ -n $db ]]; then
  mkdir -p "$backups"
  out="$backups/$OLD-docker-migration-$stamp.sql.gz"
  (umask 077 && docker exec "$db" sh -c 'MYSQL_PWD="$MARIADB_ROOT_PASSWORD" exec mariadb-dump -uroot --single-transaction \
    --routines --triggers --default-character-set=utf8mb4 "$MARIADB_DATABASE"' | gzip > "$out")
  [[ "$(gunzip -c "$out" | tail -n 1)" == "-- Dump completed"* ]] || tm_die "the database dump is incomplete – nothing
else was changed (.env is renamed; the old one is $env_file.bak-$stamp)."
  echo "    $out"
else
  echo "    the old mariadb container is not running – no dump; the old volumes stay as the backup"
fi

echo "==> Migration 3/4: stop the old containers and network"
containers="$(docker ps -aq --filter "label=com.docker.compose.project=$OLD")"
if [[ -n $containers ]]; then
  # shellcheck disable=SC2086 # one container id per word
  docker stop $containers >/dev/null
  # shellcheck disable=SC2086
  docker rm $containers >/dev/null
fi
docker network rm "$OLD" >/dev/null 2>&1 || true

echo "==> Migration 4/4: copy the volumes"
image="$(tm_service_image mariadb)"  # Any image with sh and cp will do; this one is there anyway.
for suffix in "${VOLUMES[@]}"; do
  tm_volume_exists "$OLD-$suffix" || continue
  if tm_volume_exists "$NEW-$suffix"; then
    echo "    $NEW-$suffix exists – left alone"
    continue
  fi
  docker volume create --label "com.docker.compose.project=$NEW" \
    --label "com.docker.compose.volume=$(compose_key "$suffix")" "$NEW-$suffix" >/dev/null
  docker run --rm --network none --user 0 -v "$OLD-$suffix:/from:ro" -v "$NEW-$suffix:/to" --entrypoint sh "$image" \
    -c 'cp -a /from/. /to/'
  echo "    $OLD-$suffix → $NEW-$suffix"
done

echo "Migration done. The old volumes ($OLD-*) are unchanged; remove them once the new stack runs:"
echo "  docker volume rm $(printf "$OLD-%s " "${VOLUMES[@]}")"
