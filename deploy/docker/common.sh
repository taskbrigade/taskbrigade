#!/usr/bin/env bash
# Shared helpers for deploy/docker/backup.sh, restore.sh and update.sh. Source this file.
if [[ "${BASH_SOURCE[0]}" == "$0" ]]; then
  echo "Source deploy/docker/common.sh from another script; do not execute it." >&2
  exit 1
fi

tm_die() {
  echo "error: $*" >&2
  exit 1
}

# Directory with compose.yaml and .env. Default: the repository root; TBR_COMPOSE_DIR overrides it.
TBR_COMPOSE_DIR="${TBR_COMPOSE_DIR:-$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)}"
# Volume names are fixed in compose.yaml.
TBR_STATE_VOLUME=taskbrigade-state
TBR_GITEA_VOLUME=taskbrigade-gitea

# Once the Gitea volume exists, the services of the "gitea" profile are included as well, whether the stack was
# started with --profile gitea or COMPOSE_PROFILES=gitea.
dc() {
  local profile=()
  if tm_volume_exists "$TBR_GITEA_VOLUME"; then profile=(--profile gitea); fi
  docker compose --project-directory "$TBR_COMPOSE_DIR" ${profile[@]+"${profile[@]}"} "$@"
}

tm_require_compose() {
  command -v docker >/dev/null 2>&1 || tm_die "docker was not found. Install Docker Engine or Docker Desktop."
  docker compose version >/dev/null 2>&1 || tm_die "'docker compose' (v2) was not found."
  docker info >/dev/null 2>&1 || tm_die "the Docker daemon is not reachable (is Docker running? permissions?)."
  [[ -f "$TBR_COMPOSE_DIR/compose.yaml" ]] || tm_die "$TBR_COMPOSE_DIR/compose.yaml not found. Set TBR_COMPOSE_DIR."
  [[ -f "$TBR_COMPOSE_DIR/.env" ]] || tm_die "$TBR_COMPOSE_DIR/.env not found (see docs/docker.md)."
}

tm_volume_exists() {
  docker volume inspect "$1" >/dev/null 2>&1
}

# True when the service has a running container in this project.
tm_service_running() {
  [[ -n "$(dc ps --status running -q "$1" 2>/dev/null)" ]]
}

# A string field of a service from the resolved configuration: tm_service_field SERVICE FIELD.
# Plain awk over `config --format json` (two-space indent), because jq is not installed everywhere.
tm_service_field() {
  dc --profile gitea config --format json | awk -F'"' -v svc="$1" -v field="$2" '
    /^  "/ { top = $2 }
    /^    "/ && top == "services" { cur = $2 }
    top == "services" && cur == svc && $0 ~ "^      \"" field "\":" && !n++ { print $4 }'
}

# Image of a service; built or pulled first if it is not present yet (e.g. on a fresh machine).
tm_service_image() {
  local image
  image="$(tm_service_field "$1" image)"
  [[ -n "$image" ]] || tm_die "no image for service $1."
  if ! docker image inspect "$image" >/dev/null 2>&1; then
    dc --profile gitea create --no-recreate "$1" >&2
  fi
  echo "$image"
}

# Run a throwaway container of IMAGE with VOLUME mounted at MOUNT: tm_volume_run VOLUME MOUNT IMAGE USER CMD...
# Not `docker compose run`: that may rebuild the image and mixes progress output into stdout.
tm_volume_run() {
  local volume="$1" mount="$2" image="$3" user="$4"
  shift 4
  docker run --rm -i --network none --user "$user" -v "$volume:$mount" --entrypoint sh "$image" -c "$*"
}

# Effective pull_policy of the taskbrigade service ("build" unless TBR_PULL_POLICY says otherwise).
tm_pull_policy() {
  local policy
  policy="$(tm_service_field taskbrigade pull_policy)"
  echo "${policy:-build}"
}

# Rename TM_<X>= to TBR_<X>= in .env (names before TaskBrigade 0.8; also commented examples). An existing TBR_<X>
# wins, the old line is then commented out. Plain awk: Python is not required on a Docker host.
tm_env_migrate() {
  local file="$1" tmp
  tmp="$(mktemp "$file.XXXXXX")"
  awk '
    NR == FNR { if (match($0, /^[ \t]*(export[ \t]+)?TBR_[A-Z0-9_]+=/)) { s = substr($0, RSTART, RLENGTH);
                  sub(/^[ \t]*(export[ \t]+)?/, "", s); sub(/=$/, "", s); present[s] = 1 } next }
    /^[ \t]*(export[ \t]+)?TM_[A-Z0-9_]+=/ {
      line = $0; name = $0; sub(/^[ \t]*(export[ \t]+)?TM_/, "", name); sub(/=.*/, "", name)
      if (("TBR_" name) in present) { print "# replaced by TBR_" name " (name before TaskBrigade 0.8): " line; next }
      sub(/TM_/, "TBR_", line); print line; next
    }
    /^[ \t]*#[ \t]*TM_[A-Z0-9_]+=/ { line = $0; sub(/TM_/, "TBR_", line); print line; next }
    { print }
  ' "$file" "$file" > "$tmp"
  cat "$tmp" > "$file"  # Keeps owner and mode of .env.
  rm -f "$tmp"
}

# Append KEY=VALUE to .env unless KEY is assigned there already.
tm_env_default() {
  local file="$1" key="$2" value="$3"
  grep -Eq "^[[:space:]]*(export[[:space:]]+)?$key=" "$file" || printf '%s=%s\n' "$key" "$value" >> "$file"
}
