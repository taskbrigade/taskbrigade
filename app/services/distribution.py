"""Deliver the `tbr` package of the running server version (wheel, install.sh).

The wheel is looked up as `taskbrigade-<version>-py3-none-any.whl` in `TBR_DIST_DIR` (default `<state_dir>/dist`) and
in `<source root>/dist`: the Docker image ships it there, `deploy/install.sh`/`update.sh` build it there. If it is
missing, the server builds it once with `uv build` from its own source tree (native installation) – agent machines
need no Gitea access.

Bridge for installations from before the rename: their `tm self-update` asks for `taskmeister-<version>-…whl`, and uv
refuses a wheel whose metadata names another package. So that name serves the same wheel with the package name
`taskmeister` in its metadata (`bridge_wheel`, written next to the real one). It brings `tbr` along; the next
`tbr self-update` then replaces the old uv tool with `taskbrigade`.

Build identifier (`build_id`, cli/build.py): digest over the package files in the delivered wheel's RECORD. The server
reports it next to the version (`X-TaskBrigade-Build`, `/healthz`), so `tbr self-update` and the version warning notice
a changed build even when the version number was not raised. If both directories hold a wheel of the running version,
the newer file wins – an old lazily built wheel must not shadow the one `deploy/update.sh` just built.
"""
import base64
import csv
import hashlib
import io
import logging
import os
import re
import shutil
import subprocess
import threading
import time
import zipfile
from functools import cache
from pathlib import Path

from app.config import get_settings
from cli import build

log = logging.getLogger(__name__)

SOURCE_ROOT = Path(__file__).resolve().parent.parent.parent
BUILD_RETRY_SECONDS = 300
BUILD_TIMEOUT_SECONDS = 300

DISTRIBUTION = "taskbrigade"
LEGACY_DISTRIBUTION = "taskmeister"  # Package name before TaskBrigade 0.5.

_build_lock = threading.Lock()
_last_failed_build = float("-inf")  # monotonic() counts from boot: 0.0 would mean "failed at boot".


@cache
def app_version() -> str:
    """Version of the installed taskbrigade package ("" when unknown, e.g. a plain source tree)."""
    from importlib.metadata import PackageNotFoundError, version

    try:
        return version(DISTRIBUTION)
    except PackageNotFoundError:
        return ""


def wheel_name(version: str) -> str:
    return f"{DISTRIBUTION}-{version}-py3-none-any.whl"


def legacy_wheel_name(version: str) -> str:
    return f"{LEGACY_DISTRIBUTION}-{version}-py3-none-any.whl"


def build_dir() -> Path:
    s = get_settings()
    return Path(s.tbr_dist_dir) if s.tbr_dist_dir else s.tbr_state_dir / "dist"


def search_dirs() -> list[Path]:
    return [build_dir(), SOURCE_ROOT / "dist"]


def find_wheel(version: str | None = None) -> Path | None:
    version = version or app_version()
    if not version:
        return None
    found = [c for c in (d / wheel_name(version) for d in search_dirs()) if c.is_file()]
    return max(found, key=lambda c: c.stat().st_mtime) if found else None


_build_cache: dict[tuple[str, float, int], str] = {}


def build_id() -> str:
    """Build identifier of the wheel this server delivers ("" while there is none; never builds one)."""
    wheel = find_wheel()
    if wheel is None:
        return ""
    try:
        st = wheel.stat()
    except OSError:
        return ""
    key = (str(wheel), st.st_mtime, st.st_size)
    if key not in _build_cache:
        _build_cache.clear()
        _build_cache[key] = wheel_build_id(wheel)
    return _build_cache[key]


def wheel_build_id(wheel: Path) -> str:
    try:
        with zipfile.ZipFile(wheel) as z:
            record = next((n for n in z.namelist() if n.endswith(".dist-info/RECORD")), None)
            return build.from_record(z.read(record).decode().splitlines()) if record else ""
    except (OSError, zipfile.BadZipFile, UnicodeDecodeError):
        return ""


def version_label() -> str:
    """`0.6.0+<build>` for display; just the version while no wheel exists."""
    return build.label(app_version(), build_id()) if app_version() else ""


def ensure_wheel() -> Path | None:
    """Wheel of the running version; builds it if necessary (at most once per BUILD_RETRY_SECONDS on failure)."""
    global _last_failed_build
    found = find_wheel()
    if found or not app_version():
        return found
    with _build_lock:
        found = find_wheel()
        if found or time.monotonic() - _last_failed_build < BUILD_RETRY_SECONDS:
            return found
        found = _build()
        if found is None:
            _last_failed_build = time.monotonic()
        return found


def _build() -> Path | None:
    uv = shutil.which("uv")
    if not uv or not (SOURCE_ROOT / "pyproject.toml").is_file():
        log.warning("wheel_build_unavailable", extra={"uv": bool(uv), "source": str(SOURCE_ROOT)})
        return None
    out = build_dir()
    try:
        out.mkdir(parents=True, exist_ok=True)
        r = subprocess.run([uv, "build", "--wheel", "--out-dir", str(out), str(SOURCE_ROOT)], capture_output=True,
                           text=True, timeout=BUILD_TIMEOUT_SECONDS)
    except (OSError, subprocess.SubprocessError) as e:
        log.warning("wheel_build_failed", extra={"error": str(e)})
        return None
    if r.returncode != 0:
        log.warning("wheel_build_failed", extra={"error": (r.stderr or r.stdout)[-500:]})
        return None
    found = find_wheel()
    if found is None:  # Source tree and installed metadata disagree (e.g. pyproject bumped without uv sync).
        log.warning("wheel_build_version_mismatch", extra={"version": app_version(), "dir": str(out)})
    return found


def ensure_legacy_wheel() -> Path | None:
    """The running version's wheel under the old package name (see module docstring); built from the real one."""
    wheel = ensure_wheel()
    if wheel is None:
        return None
    target = wheel.with_name(legacy_wheel_name(app_version()))
    if target.is_file() and target.stat().st_mtime >= wheel.stat().st_mtime:
        return target
    for out in (target, build_dir() / target.name):  # The source dist dir may not be writable.
        try:
            out.parent.mkdir(parents=True, exist_ok=True)
            bridge_wheel(wheel, out, LEGACY_DISTRIBUTION)
            return out
        except (OSError, zipfile.BadZipFile, ValueError) as e:
            log.warning("legacy_wheel_failed", extra={"error": str(e), "target": str(out)})
    return None


def _record_hash(data: bytes) -> str:
    return "sha256=" + base64.urlsafe_b64encode(hashlib.sha256(data).digest()).rstrip(b"=").decode()


def bridge_wheel(source: Path, target: Path, name: str) -> None:
    """Copy the wheel `source` to `target` with the distribution name `name`: renamed .dist-info, `Name:` in
    METADATA and a new RECORD. Code and entry points stay the same."""
    with zipfile.ZipFile(source) as zin:
        infos = [i for i in zin.infolist() if not i.is_dir()]
        dist_info = next((i.filename.split("/")[0] for i in infos
                          if re.fullmatch(r"[^/]+\.dist-info/METADATA", i.filename)), None)
        if dist_info is None:
            raise ValueError(f"{source.name} has no .dist-info/METADATA")
        new_info = f"{name}-{dist_info.split('-', 1)[1]}"
        tmp = target.with_name(f".{target.name}.tmp")
        records = []
        with zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED) as zout:
            for info in infos:
                path = info.filename
                if path == f"{dist_info}/RECORD":
                    continue
                data = zin.read(info)
                if path.startswith(dist_info + "/"):
                    path = new_info + path[len(dist_info):]
                    if path == f"{new_info}/METADATA":
                        data = re.sub(rb"^Name: .*$", f"Name: {name}".encode(), data, count=1, flags=re.M)
                out = zipfile.ZipInfo(path, info.date_time)
                out.external_attr, out.compress_type = info.external_attr, zipfile.ZIP_DEFLATED
                zout.writestr(out, data)
                records.append((path, _record_hash(data), str(len(data))))
            records.append((f"{new_info}/RECORD", "", ""))
            buf = io.StringIO()
            csv.writer(buf, lineterminator="\n").writerows(records)
            zout.writestr(zipfile.ZipInfo(f"{new_info}/RECORD", infos[0].date_time), buf.getvalue())
    os.replace(tmp, target)


INSTALL_SCRIPT = r"""#!/bin/sh
# TaskBrigade: install the `tbr` CLI of this server (version @VERSION@) for the current user – no root needed.
#   curl -fsSL @URL@/install.sh | sh
# Installs uv into ~/.local/bin when it is missing, then tbr as a uv tool from the server's wheel.
# Safe to run again: an installed tbr of the same version and build is left alone. TBR_URL=<url> overrides the server
# address.
set -eu

TBR_URL="${TBR_URL:-${TM_URL:-@URL@}}"  # TM_URL: name before TaskBrigade 0.8
TBR_URL="${TBR_URL%/}"
TBR_VERSION="@VERSION@"
TBR_BUILD="@BUILD@"
WHEEL="@WHEEL@"

say() { printf '==> %s\n' "$*"; }
die() { printf 'error: %s\n' "$*" >&2; exit 1; }

case "$(uname -s)" in
  Linux | Darwin) ;;
  *) die "unsupported system $(uname -s) – tbr runs on Linux and macOS" ;;
esac
command -v curl >/dev/null 2>&1 || die "curl is required"
[ "$(id -u)" -ne 0 ] || say "warning: running as root – tbr is installed for root only; run it as the agent user"

USER_PATH="$PATH"
PATH="$HOME/.local/bin:$PATH"
export PATH
UV="$(command -v uv 2>/dev/null || true)"
if [ -z "$UV" ]; then
  say "uv not found – installing it for $(id -un) (official installer, ~/.local/bin)"
  curl -LsSf https://astral.sh/uv/install.sh | sh || die "uv installation failed"
  UV="$HOME/.local/bin/uv"
  [ -x "$UV" ] || UV="$(command -v uv 2>/dev/null || true)"
  [ -n "$UV" ] && [ -x "$UV" ] || die "uv was installed but not found; open a new shell and run this again"
else
  say "uv found: $UV"
fi

BIN="$("$UV" tool dir --bin 2>/dev/null || printf '%s' "$HOME/.local/bin")"
TOOLS="$("$UV" tool dir 2>/dev/null || true)"
TBR_LABEL="$TBR_VERSION${TBR_BUILD:++$TBR_BUILD}"
if "$UV" tool list 2>/dev/null | grep -q "^taskbrigade v$TBR_VERSION\$" \
    && { [ -z "$TBR_BUILD" ] || [ "$("$BIN/tbr" version 2>/dev/null | head -n 1)" = "tbr $TBR_LABEL" ]; }; then
  say "tbr $TBR_LABEL is already installed ($BIN/tbr) – nothing to do"
else
  say "Installing tbr $TBR_LABEL from $TBR_URL"
  # --force: takes over tm/tm-admin from the package taskmeister (before the rename) without removing them first.
  # --reinstall-package: every build of a version has the same wheel URL; uv would reuse a cached earlier build.
  "$UV" tool install --force --reinstall-package taskbrigade --python 3.12 "$TBR_URL/download/$WHEEL" \
    || die "installing tbr failed – is $TBR_URL/download/$WHEEL reachable from here?"
fi

# Before the rename the package was called taskmeister. Its tm/tm-admin now point to taskbrigade, so only its
# environment is removed: `uv tool uninstall taskmeister` would delete those links and leave the machine without tm
# for a moment (runner timers, credential helpers and MCP entries may still call tm).
if [ -n "$TOOLS" ] && [ -d "$TOOLS/taskmeister" ]; then
  IN_USE=""
  for name in tm tm-admin tbr tbr-admin; do
    if [ -L "$BIN/$name" ]; then
      case "$(readlink "$BIN/$name")" in "$TOOLS/taskmeister/"*) IN_USE="$IN_USE $name" ;; esac
    fi
  done
  if [ -n "$IN_USE" ]; then
    say "warning: the old package taskmeister still provides$IN_USE – left in place; run: $UV tool list"
  else
    rm -rf "$TOOLS/taskmeister"
    say "Removed the old package taskmeister – tm keeps working as an alias of tbr"
  fi
fi

"$BIN/tbr" version >/dev/null 2>&1 || die "$BIN/tbr does not start"
case ":$USER_PATH:" in
  *":$BIN:"*)
    OTHER="$(PATH="$USER_PATH"; command -v tbr 2>/dev/null || true)"
    [ "$OTHER" = "$BIN/tbr" ] || say "warning: another tbr comes first on PATH: $OTHER (e.g. the ebook reader tbr from crates.io) – call $BIN/tbr or the old name tm"
    ;;
  *) say "$BIN is not on your PATH yet – run: $UV tool update-shell (then open a new shell)" ;;
esac
say "Done: $("$BIN/tbr" version 2>/dev/null | head -n 1). Next: put the .taskbrigade.toml from the web UI into the"
say "agent's work directory and check it there with: tbr whoami"
"""


def install_script(server_url: str, version: str, build_id: str = "") -> str:
    return (INSTALL_SCRIPT.replace("@URL@", server_url.rstrip("/")).replace("@VERSION@", version)
            .replace("@BUILD@", build_id)
            .replace("@WHEEL@", wheel_name(version)))
