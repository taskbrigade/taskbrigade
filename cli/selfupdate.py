"""`tbr self-update`: install the server's version as the uv tool `taskbrigade` and retire the old tool `taskmeister`.

Order matters, no agent may lose its command in between: `uv tool install --force` first takes over `tm`/`tm-admin`
from the old tool (and adds `tbr`), then only the old tool's environment is removed. `uv tool uninstall taskmeister`
would also delete the `tm` links that now belong to `taskbrigade`. The same steps are in the server's `install.sh`
(app/services/distribution.py).
"""
import shutil
import subprocess
from pathlib import Path

from cli.client import DISTRIBUTION, LEGACY_DISTRIBUTION

COMMANDS = ("tbr", "tbr-admin", "tm", "tm-admin")


def _uv_dir(uv: str, *args: str) -> Path | None:
    try:
        r = subprocess.run([uv, "tool", "dir", *args], capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.SubprocessError):
        return None
    out = (r.stdout or "").strip()
    return Path(out) if r.returncode == 0 and out else None


def legacy_tool(uv: str) -> Path | None:
    """Environment of the uv tool `taskmeister` (before the rename), if it is still there."""
    tools = _uv_dir(uv)
    path = tools / LEGACY_DISTRIBUTION if tools else None
    return path if path and path.is_dir() else None


def legacy_links(uv: str, legacy: Path) -> list[str]:
    """Commands in uv's bin directory that still run from the old environment."""
    bin_dir = _uv_dir(uv, "--bin")
    if bin_dir is None:
        return list(COMMANDS)  # Unknown: better keep the old environment.
    used = []
    for name in COMMANDS:
        link = bin_dir / name
        if link.is_symlink() and Path(link.readlink()).is_relative_to(legacy):
            used.append(name)
    return used


def remove_legacy_tool(uv: str) -> tuple[bool, str]:
    """(removed, detail). Leaves the old environment alone while one of its commands is still linked."""
    legacy = legacy_tool(uv)
    if legacy is None:
        return False, ""
    used = legacy_links(uv, legacy)
    if used:
        return False, ", ".join(used)
    shutil.rmtree(legacy)
    return True, str(legacy)


def install(uv: str, url: str) -> int:
    # Every build of a version has the same wheel URL, and uv would reuse its cached copy of an earlier build:
    # --reinstall-package refreshes the cache entry for the package (checked with uv 0.11.21).
    return subprocess.run([uv, "tool", "install", "--force", "--reinstall-package", DISTRIBUTION, "--python", "3.12",
                           url]).returncode
