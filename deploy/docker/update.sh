#!/usr/bin/env bash
# Update a Docker Compose installation: backup, pull/build the images, recreate the containers.
# The taskbrigade container migrates the schema (alembic upgrade head) before it serves requests.
#   git pull                     # local build (default): fetch the new source first
#   deploy/docker/update.sh [--no-backup]
# With TBR_IMAGE + TBR_PULL_POLICY=always the new image is pulled from the registry instead of built.
# An installation from before TaskBrigade 0.8 (volumes taskmeister-*) is moved to the new names first
# (deploy/docker/migrate-names.sh: .env keys, database dump, volumes copied, old volumes kept; docs/docker.md).
set -euo pipefail

# shellcheck source=common.sh
source "$(dirname "$0")/common.sh"

backup=1
if [[ "${1:-}" == "--no-backup" ]]; then
  backup=0
  shift
fi
[[ $# -eq 0 ]] || tm_die "usage: deploy/docker/update.sh [--no-backup]"
tm_require_compose

# Installation from before TaskBrigade 0.8 (volumes taskmeister-*): move it to the new names first. The migration
# takes its own database dump and keeps the old volumes, so the regular backup is skipped this once.
if tm_volume_exists taskmeister-db && ! tm_volume_exists taskbrigade-db; then
  "$(dirname "$0")/migrate-names.sh"
  backup=0
fi

if [[ "$backup" == 1 ]]; then
  if tm_service_running mariadb; then
    "$(dirname "$0")/backup.sh"
  else
    tm_die "mariadb is not running, so no backup can be taken. Start it (docker compose up -d) or pass --no-backup."
  fi
fi

policy="$(tm_pull_policy)"
echo "Pulling images (taskbrigade: pull_policy $policy) ..."
dc pull --ignore-buildable
if [[ "$policy" == "build" ]]; then
  dc build --pull taskbrigade
  dc up -d --wait --no-build
else
  dc up -d --wait --pull always
fi
dc ps
echo "Update finished. Old images: docker image prune"
