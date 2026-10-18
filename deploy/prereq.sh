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

# --- Data zones (research/konzept-datenzonen.md §9) ------------------------------------------------------
# Opt-in switch: `TBR_DATA_ZONES=1` during install/update, or `tbr-admin config set TBR_DATA_ZONES 1` followed by
# `deploy/update.sh`. Without it every data-zone routine below is a no-op and only the database migrations run.
# Defaults of the paths the scripts create; the app reads TBR_DATA_DIR/TBR_DATA_KEY_FILE itself (app/config.py).
tbr_data_zones_enabled() {
  [[ "${TBR_DATA_ZONES:-}" == "1" ]]
}

tbr_data_dir_path() {
  printf '%s\n' "${TBR_DATA_DIR:-/srv/taskbrigade-data}"
}

tbr_data_key_path() {
  printf '%s\n' "${TBR_DATA_KEY_FILE:-/etc/taskbrigade/data.key}"
}

# True when /run/systemd/system exists (a real systemd host); tests set TBR_FAKE_SYSTEMCTL to skip probing.
tm_has_systemd() {
  [[ -d /run/systemd/system ]]
}

# systemd version of the *running* systemd, e.g. 255. Empty when it cannot be determined.
tm_systemd_version() {
  local ver
  ver="$(systemctl --version 2>/dev/null | head -n1 | awk '{print $2}')" || true
  printf '%s\n' "${ver%%[^0-9]*}"
}

# Create a system user/group with a stable name when it does not exist yet. Idempotent.
# tm_system_user NAME [home] – the shell is always nologin (these are service accounts).
tm_system_user() {
  local name="$1" home="${2:-/nonexistent}"
  if ! getent group "$name" >/dev/null 2>&1; then
    groupadd --system "$name"
  fi
  if ! id "$name" >/dev/null 2>&1; then
    useradd --system --home "$home" --shell /usr/sbin/nologin --gid "$name" "$name"
  fi
}

# Create a system group when it is missing.
tm_system_group() {
  local name="$1"
  getent group "$name" >/dev/null 2>&1 || groupadd --system "$name"
}

# Add a user to a supplementary group (idempotent).
tm_add_to_group() {
  local user="$1" group="$2"
  id -nG "$user" 2>/dev/null | tr ' ' '\n' | grep -qx "$group" || usermod -aG "$group" "$user"
}

# install -d with owner:group and mode, tolerating an existing directory (rights are enforced every run).
tm_ensure_dir() {
  local path="$1" owner="$2" mode="$3"
  install -d -o "${owner%%:*}" -g "${owner##*:}" -m "$mode" "$path"
}

# Group that owns the data store root: tbr-data (native). Overridable for tests.
tbr_data_group() {
  printf '%s\n' "${TBR_DATA_GROUP:-tbr-data}"
}

# --- Prerequisite checks for the opt-in data zones ---------------------------------------------------
# Returns 0 and prints a short report when the host can run the data zones; exits with a concrete message
# otherwise. Only call this while TBR_DATA_ZONES=1.
tm_require_data_zone_host() {
  tm_require_systemd
  local ver
  ver="$(tm_systemd_version)"
  if [[ -n "$ver" ]] && ! tm_version_ge "$ver" "249"; then
    tm_die "TBR_DATA_ZONES=1 needs systemd >= 249 for the sandbox unit (ProtectProc=invisible,
RestrictAddressFamilies in a template). This host runs systemd ${ver:-unknown}.
Update the distribution, or leave TBR_DATA_ZONES unset to keep the data zones off."
  fi
  command -v polkitd >/dev/null 2>&1 || [[ -d /etc/polkit-1 ]] || tm_die "TBR_DATA_ZONES=1 needs policykit (polkit) for the sandbox rule /etc/polkit-1/rules.d/60-taskbrigade-runner.rules.
On Ubuntu 24.04: apt-get install -y polkitd
Or leave TBR_DATA_ZONES unset to keep the data zones off."
  echo "Data zones: systemd ${ver:-unknown}, polkit OK" >&2
}

# Install the data-zone runner units with this host's paths and the sandbox limits drop-in (concept §5/§9).
# tbr_install_runner_units SRC BASE DATA_DIR RUNNER_VENV LIMITS – LIMITS is the drop-in text from
# `tbr-admin runner limits`; empty keeps an existing drop-in (the unit's own values apply without one).
# taskbrigade-runner.service is only (re)started when it is enabled (`tbr-admin runner join` enables it).
tbr_install_runner_units() {
  local src="$1" base="$2" data_dir="$3" venv="$4" limits="$5" unit dropin
  for unit in taskbrigade-runner.service taskbrigade-job@.service; do
    sed -e "s|/opt/taskbrigade|$base|g" -e "s|$base/runner-venv|$venv|g" -e "s|/srv/taskbrigade-data|$data_dir|g" \
      "$src/deploy/systemd/$unit" > "/etc/systemd/system/$unit.new"
    chmod 0644 "/etc/systemd/system/$unit.new"
    mv "/etc/systemd/system/$unit.new" "/etc/systemd/system/$unit"
  done
  dropin=/etc/systemd/system/taskbrigade-job@.service.d
  install -d -o root -g root -m 0755 "$dropin"
  if [[ -n "$limits" ]]; then
    printf '%s\n' "$limits" > "$dropin/50-limits.conf.new"
    chmod 0644 "$dropin/50-limits.conf.new"
    mv "$dropin/50-limits.conf.new" "$dropin/50-limits.conf"
  fi
}

# SFTP inbox (concept §4, phase 2) – only checked when TBR_DATA_SFTP=1 requests it.
tm_require_sftp() {
  command -v sshd >/dev/null 2>&1 || tm_die "TBR_DATA_SFTP=1 but sshd was not found.
On Ubuntu 24.04: apt-get install -y openssh-server
Or leave TBR_DATA_SFTP unset."
  local ver
  ver="$(sshd -V 2>&1 | awk 'NR==1 {for (i = 1; i <= NF; i++) if ($i ~ /^[0-9]+\.[0-9]+/) {print $i; exit}}')"
  if [[ -n "$ver" ]] && ! tm_version_ge "$ver" "8.0"; then
    tm_die "TBR_DATA_SFTP=1 needs OpenSSH >= 8 (this host has ${ver})."
  fi
  echo "SFTP inbox: OpenSSH ${ver:-unknown} OK" >&2
}

# chroot root of the native SFTP inbox (concept §4); overridable for tests.
tbr_sftp_dir_path() {
  printf '%s\n' "${TBR_SFTP_DIR:-/srv/taskbrigade-sftp}"
}

# Provision the native SFTP inbox (concept §4): the sshd drop-in, the group tbr-sftp, the chroot root and the
# systemd path/service units. Every step is idempotent; sshd is reloaded only when `sshd -t` accepts the new
# configuration. Only call this while TBR_DATA_SFTP=1 and TBR_DATA_ZONES=1. $1 = repo checkout (for deploy/ssh).
tm_install_sftp_inbox() {
  local app="${1:-${APP:-}}"
  tm_require_sftp
  local sftp_root
  sftp_root="$(tbr_sftp_dir_path)"
  tm_system_group tbr-sftp
  tm_ensure_dir "$sftp_root" root:root 0755
  # The app user needs group tbr-data (SupplementaryGroups) and read/write on the chroot tree for the import.
  [[ -n "$app" && -f "$app/deploy/ssh/60-taskbrigade-sftp.conf" ]] \
    || tm_die "tm_install_sftp_inbox needs the checkout path (deploy/ssh/60-taskbrigade-sftp.conf)."
  # sshd drop-in: only install and reload when sshd accepts the whole configuration.
  install -d -o root -g root -m 0755 /etc/ssh/sshd_config.d
  local drop_in=/etc/ssh/sshd_config.d/60-taskbrigade-sftp.conf
  local candidate
  candidate="$(mktemp)"
  sed "s|/srv/taskbrigade-sftp|$sftp_root|g" "$app/deploy/ssh/60-taskbrigade-sftp.conf" > "$candidate"
  if [[ -f "$drop_in" ]] && cmp -s "$candidate" "$drop_in"; then
    echo "SFTP inbox: sshd drop-in already installed" >&2
  else
    install -o root -g root -m 0644 "$candidate" "$drop_in"
    if command -v sshd >/dev/null 2>&1 && sshd -t 2>/dev/null; then
      # `systemctl reload ssh` (Ubuntu) and `sshd.service` (other units) both work.
      systemctl reload ssh 2>/dev/null || systemctl reload sshd 2>/dev/null || tm_warn_sftp_reload
      echo "SFTP inbox: sshd drop-in installed and sshd reloaded" >&2
    else
      rm -f "$drop_in"
      tm_die "The sshd drop-in failed 'sshd -t'; it was removed again. Check /etc/ssh/sshd_config.d for conflicts."
    fi
  fi
  rm -f "$candidate"
  # Path/service units for the import (the checkout ships them with /opt/taskbrigade; substitute the prefix).
  local base="${TBR_BASE:-/opt/taskbrigade}"
  local unit
  for unit in taskbrigade-data-import.service taskbrigade-data-import.path; do
    sed "s|/opt/taskbrigade|$base|g; s|/srv/taskbrigade-sftp|$sftp_root|g" \
      "$app/deploy/systemd/$unit" > "/etc/systemd/system/$unit"
    chmod 0644 "/etc/systemd/system/$unit"
  done
  systemctl daemon-reload 2>/dev/null || true
  systemctl enable --now taskbrigade-data-import.path 2>/dev/null || tm_warn_sftp_units
  echo "SFTP inbox ready: root $sftp_root, import unit taskbrigade-data-import.path" >&2
}

# Reloading sshd failed: the drop-in is correct, the running daemon still needs a reload by hand.
tm_warn_sftp_reload() {
  cat >&2 <<'MSG'
SFTP inbox: the sshd drop-in was installed but sshd did not reload. Check the unit name and run:
  sudo sshd -t && sudo systemctl reload ssh    # or: sudo systemctl reload sshd
MSG
}

# Enabling the path unit failed (e.g. a container without systemd): the timer/CLI still works.
tm_warn_sftp_units() {
  echo "SFTP inbox: taskbrigade-data-import.path could not be enabled now; run 'systemctl enable --now taskbrigade-data-import.path' later, or import from a timer with 'tbr-admin data sftp-user import'." >&2
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
