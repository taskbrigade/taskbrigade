#!/usr/bin/env bash
# Shared prerequisite checks for deploy/install.sh, deploy/update.sh, and deploy/backup.sh.
# Source this file. Running it directly does nothing except explain that.
if [[ "${BASH_SOURCE[0]}" == "$0" ]]; then
  echo "Source deploy/prereq.sh from another script; do not execute it." >&2
  exit 1
fi

tm_die() {
  echo "error: $*" >&2
  exit 1
}

# $1 = installed version, $2 = minimum (major.minor or major.minor.patch). Equal counts as new enough.
tm_version_ge() {
  local have="$1" need="$2"
  local IFS=.
  local -a h=() n=()
  local i hv nv
  read -r -a h <<<"$have"
  read -r -a n <<<"$need"
  for i in 0 1 2; do
    hv="${h[$i]:-0}"
    nv="${n[$i]:-0}"
    hv="${hv%%[^0-9]*}"
    nv="${nv%%[^0-9]*}"
    hv="${hv:-0}"
    nv="${nv:-0}"
    if ((10#$hv > 10#$nv)); then
      return 0
    fi
    if ((10#$hv < 10#$nv)); then
      return 1
    fi
  done
  return 0
}

# Prints major.minor.patch, or returns 1 when the text is not a MariaDB client banner.
tm_mariadb_version_string() {
  local out="$1"
  [[ "$out" == *MariaDB* ]] || return 1
  if [[ "$out" =~ Distrib[[:space:]]([0-9]+\.[0-9]+\.[0-9]+) ]]; then
    printf '%s\n' "${BASH_REMATCH[1]}"
    return 0
  fi
  if [[ "$out" =~ from[[:space:]]([0-9]+\.[0-9]+\.[0-9]+) ]]; then
    printf '%s\n' "${BASH_REMATCH[1]}"
    return 0
  fi
  return 1
}

tm_require_python312() {
  local py="${1:-}"
  local ver
  if [[ -z "$py" ]]; then
    if [[ -x /usr/bin/python3.12 ]]; then
      py=/usr/bin/python3.12
    else
      py="$(command -v python3.12 2>/dev/null || true)"
    fi
  fi
  if [[ -z "$py" || ! -x "$py" ]]; then
    tm_die "Python 3.12 is required (this project does not run on 3.11 or 3.13).
On Ubuntu 24.04: apt-get install -y python3.12
Then re-run. You do not need to edit this script."
  fi
  ver="$("$py" -c 'import sys; print("%d.%d" % sys.version_info[:2])')"
  if [[ "$ver" != "3.12" ]]; then
    tm_die "$py is Python $ver, but TaskBrigade requires 3.12 (pyproject.toml: requires-python >=3.12,<3.13).
Install the python3.12 package and pass PY=/usr/bin/python3.12."
  fi
  echo "Python $("$py" -V 2>&1) OK ($py)" >&2
  printf '%s\n' "$py"
}

tm_require_uv() {
  local uv="${1:-}"
  local ver
  if [[ -z "$uv" && -x /usr/local/bin/uv ]]; then
    uv=/usr/local/bin/uv
  fi
  if [[ -z "$uv" ]]; then
    uv="$(command -v uv 2>/dev/null || true)"
  fi
  if [[ -z "$uv" || ! -x "$uv" ]]; then
    tm_die "uv was not found or is not executable.
Install it (the script does not need to be edited) and pass the binary:
  curl -LsSf https://astral.sh/uv/install.sh | sh
  sudo UV=\"\$HOME/.local/bin/uv\" deploy/install.sh
install.sh downloads uv to /usr/local/bin when it is missing, unless TBR_INSTALL_UV=0.
https://docs.astral.sh/uv/getting-started/installation/"
  fi
  ver="$("$uv" --version 2>/dev/null || true)"
  ver="${ver%%$'\n'*}"
  echo "uv OK: $uv${ver:+ ($ver)}" >&2
  printf '%s\n' "$uv"
}

tm_require_git() {
  command -v git >/dev/null 2>&1 || tm_die "git is required.
On Ubuntu 24.04: apt-get install -y git"
}

tm_require_systemd() {
  command -v systemctl >/dev/null 2>&1 || tm_die "systemd is required (systemctl was not found).
install.sh and update.sh target a systemd host such as Ubuntu 24.04. There is no Docker setup."
}

tm_require_mariadb_dump() {
  local bin="mariadb-dump" out ver min="10.5"
  if ! command -v "$bin" >/dev/null 2>&1; then
    tm_die "mariadb-dump was not found (MariaDB client >= $min).
On Ubuntu 24.04: apt-get install -y mariadb-client
MySQL's dump client is not a substitute."
  fi
  out="$("$bin" --version 2>/dev/null || true)"
  ver="$(tm_mariadb_version_string "$out" || true)"
  if [[ -z "$ver" ]]; then
    tm_die "mariadb-dump did not report a MariaDB version.
Output was: ${out:-<empty>}
Install the MariaDB client, not MySQL: apt-get install -y mariadb-client"
  fi
  if ! tm_version_ge "$ver" "$min"; then
    tm_die "mariadb-dump $ver is older than MariaDB $min. Install MariaDB >= $min and re-run."
  fi
  echo "mariadb-dump $ver OK (>= $min)" >&2
}

tm_require_mariadb_client() {
  local min="10.5" bin out ver
  bin="$(command -v mariadb 2>/dev/null || command -v mysql 2>/dev/null || true)"
  if [[ -z "$bin" ]]; then
    tm_die "MariaDB client >= $min was not found.
On Ubuntu 24.04: apt-get install -y mariadb-client
The server TaskBrigade connects to must be MariaDB >= $min as well. MySQL is not supported
(claim locking assumes MariaDB 10.5). A remote server is fine; this check is the local client."
  fi
  out="$("$bin" --version 2>/dev/null || true)"
  ver="$(tm_mariadb_version_string "$out" || true)"
  if [[ -z "$ver" ]]; then
    tm_die "$bin is not a MariaDB client (MySQL is not supported).
Output was: ${out:-<empty>}
On Ubuntu 24.04: apt-get install -y mariadb-client"
  fi
  if ! tm_version_ge "$ver" "$min"; then
    tm_die "MariaDB client $ver from $bin is older than $min.
Install MariaDB >= $min. Ubuntu 24.04's mariadb-client package is new enough."
  fi
  echo "MariaDB client $ver OK (>= $min) via $bin" >&2
  echo "The server at TBR_DB_HOST must also be MariaDB >= $min. Confirm it after the UI database setup." >&2
  tm_require_mariadb_dump
}

# Empty or example hosts are not probed. A concrete URL must answer GET /api/v1/version.
# mode: warn (default) | required | report
#   warn/required: placeholder warns; required then exits. A concrete URL that fails always exits.
#   report: never exits (used by update.sh so a down Gitea does not block an upgrade).
tm_gitea_placeholder() {
  local url="${1:-}"
  [[ -z "$url" ]] && return 0
  case "$url" in
    *example.internal*|*example.com*|*example.org*) return 0 ;;
  esac
  return 1
}

tm_check_gitea() {
  local url="${1:-}" mode="${2:-warn}"
  local tmp code body msg
  if tm_gitea_placeholder "$url"; then
    echo "Gitea: not configured yet (GITEA_URL is empty or still an example host)." >&2
    echo "      Agents cannot open pull requests until a real Gitea answers GET /api/v1/version." >&2
    echo "      Pass GITEA_URL=http://<host>:<port> to install.sh, or set it later in Admin → Settings." >&2
    if [[ "$mode" == "required" ]]; then
      tm_die "Gitea is required. Set GITEA_URL to a reachable server and re-run."
    fi
    return 0
  fi
  if ! command -v curl >/dev/null 2>&1; then
    msg="curl is required to probe Gitea at $url. On Ubuntu 24.04: apt-get install -y curl"
    if [[ "$mode" == "report" ]]; then
      echo "warning: $msg" >&2
      return 0
    fi
    tm_die "$msg"
  fi
  tmp="$(mktemp)"
  code="$(curl -sS -o "$tmp" -w '%{http_code}' --max-time 8 "$url/api/v1/version" || true)"
  body="$(head -c 180 "$tmp" 2>/dev/null || true)"
  rm -f "$tmp"
  if [[ "$code" != "200" ]]; then
    msg="Gitea check failed: $url/api/v1/version returned HTTP ${code:-none}."
    if [[ "$mode" == "report" ]]; then
      echo "warning: $msg Agents cannot open pull requests until Gitea answers." >&2
      return 0
    fi
    tm_die "$msg
Install Gitea or fix GITEA_URL (https://docs.gitea.com/installation/install-from-binary).
To install TaskBrigade before Gitea exists, leave GITEA_URL on the example host.
install.sh will warn and continue; set the real URL in Admin → Settings afterwards."
  fi
  echo "Gitea OK: $url ($body)" >&2
}

# $1 = 1 when TAILSCALE_SERVE was requested, otherwise 0. Tailscale itself is optional.
tm_check_tailscale() {
  local serve="${1:-0}"
  if [[ "$serve" == "1" ]]; then
    if ! command -v tailscale >/dev/null 2>&1; then
      tm_die "TAILSCALE_SERVE=1 but the tailscale command was not found.
Install it, bring the node up, then re-run (https://tailscale.com/download/linux):
  curl -fsSL https://tailscale.com/install.sh | sh
  tailscale up
Tailscale is optional. Omit TAILSCALE_SERVE to keep the UI on http://127.0.0.1:8090."
    fi
    if ! tailscale status >/dev/null 2>&1; then
      tm_die "tailscale is installed but 'tailscale status' failed (not logged in, or the daemon is down).
Run 'tailscale up', or omit TAILSCALE_SERVE and configure serve later."
    fi
    echo "Tailscale OK; serve will proxy https/443 to 127.0.0.1:8090." >&2
    return 0
  fi
  if command -v tailscale >/dev/null 2>&1; then
    echo "Tailscale: installed. Serve was not requested (set TAILSCALE_SERVE=1 to enable it)." >&2
  else
    echo "Tailscale: optional, and not installed. The UI stays on http://127.0.0.1:8090." >&2
    echo "           Later: https://tailscale.com/download/linux" >&2
    echo "           then: tailscale serve --bg --https=443 http://127.0.0.1:8090" >&2
  fi
}

# Set or replace KEY=value in a systemd EnvironmentFile. No-op when value is empty.
# Uses TBR_PY (a Python 3.12 interpreter). Values are written raw; no shell quoting.
tm_env_set() {
  local file="$1" key="$2" value="${3:-}"
  [[ -n "$value" ]] || return 0
  [[ -n "${TBR_PY:-}" && -x "$TBR_PY" ]] || tm_die "tm_env_set needs TBR_PY to point at Python 3.12."
  [[ -f "$file" ]] || tm_die "env file not found: $file"
  "$TBR_PY" -c '
import pathlib, sys
path, key, value = sys.argv[1:]
if "\n" in value or "\r" in value:
    raise SystemExit("env values must be a single line")
file = pathlib.Path(path)
lines = file.read_text().splitlines()
prefix = key + "="
found = False
out = []
for line in lines:
    if line.startswith(prefix):
        out.append(f"{key}={value}")
        found = True
    else:
        out.append(line)
if not found:
    out.append(f"{key}={value}")
file.write_text("\n".join(out) + "\n")
' "$file" "$key" "$value"
}

tm_env_get() {
  local file="$1" key="$2"
  [[ -n "${TBR_PY:-}" && -x "$TBR_PY" ]] || tm_die "tm_env_get needs TBR_PY to point at Python 3.12."
  [[ -f "$file" ]] || return 0
  "$TBR_PY" -c '
import pathlib, sys
path, key = sys.argv[1:]
prefix = key + "="
for line in pathlib.Path(path).read_text().splitlines():
    if line.startswith(prefix):
        print(line[len(prefix):], end="")
        break
' "$file" "$key"
}

# Rename TM_<X>= to TBR_<X>= in an env file (names before TaskBrigade 0.8; also commented examples). An existing
# TBR_<X> wins, the old line is then commented out. Further arguments OLD=NEW replace old default paths at the start
# of a value. Keeps owner and mode; a changed file is backed up as <file>.bak-<timestamp> first.
tm_env_rename_legacy() {
  local file="$1"
  shift
  [[ -n "${TBR_PY:-}" && -x "$TBR_PY" ]] || tm_die "tm_env_rename_legacy needs TBR_PY to point at Python 3.12."
  [[ -f "$file" ]] || return 0
  "$TBR_PY" -c '
import os, pathlib, re, shutil, sys, time
path, pairs = pathlib.Path(sys.argv[1]), [p.split("=", 1) for p in sys.argv[2:]]
lines = path.read_text().splitlines()
assign = re.compile(r"^(\s*#\s*|\s*(?:export\s+)?)TM_([A-Z0-9_]+)=(.*)$")
present = {m.group(1) for m in (re.match(r"^\s*(?:export\s+)?(TBR_[A-Z0-9_]+)=", l) for l in lines) if m}
out = []
for line in lines:
    m = assign.match(line)
    if not m:
        out.append(line)
        continue
    lead, name, value = m.groups()
    for old, new in pairs:
        if value == old or value.startswith(old + "/"):
            value = new + value[len(old):]
    if "#" not in lead and "TBR_" + name in present:
        out.append(f"# replaced by TBR_{name} (name before TaskBrigade 0.8): {line.strip()}")
    else:
        out.append(f"{lead}TBR_{name}={value}")
if out != lines:
    st = path.stat()
    backup = path.with_name(f"{path.name}.bak-{time.strftime("%Y%m%d-%H%M%S")}")
    shutil.copy2(path, backup)
    path.write_text("\n".join(out) + "\n")
    os.chown(path, st.st_uid, st.st_gid)
    os.chmod(path, st.st_mode & 0o7777)
    print(f"    {path}: TM_* renamed to TBR_* (previous version {backup.name})")
' "$file" "$@"
}
