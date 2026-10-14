"""Build identifier of a taskbrigade wheel: a digest over the package files listed in its RECORD.

Version numbers alone miss changes rolled out without a version bump. Server and CLI therefore compare a build
identifier as well. It is computed from `path,sha256` of every package file (no `*.dist-info`/`*.data` entries, no
scripts outside site-packages, no bytecode), so the server gets it from the wheel it delivers (app/services/
distribution.py) and an installed `tbr` from its own RECORD – uv copies those lines unchanged. Timestamps inside the
zip do not matter, the bridge wheel under the old package name has the same identifier, and an editable install
(source checkout) has none.
"""
import json
from collections.abc import Iterable

LENGTH = 10


def _package_file(path: str) -> bool:
    top = path.split("/", 1)[0]
    return not (path.startswith("../") or top.endswith((".dist-info", ".data")) or path.endswith((".pyc", ".pth"))
                or "/__pycache__/" in f"/{path}")


def from_record(lines: Iterable[str]) -> str:
    """Identifier from RECORD lines (`path,hash,size`); "" when no package file with a hash is listed."""
    import hashlib

    entries = sorted({(path, digest) for path, digest in (_split(line) for line in lines)
                      if digest and _package_file(path)})
    if not entries:
        return ""
    return hashlib.sha256("".join(f"{p},{d}\n" for p, d in entries).encode()).hexdigest()[:LENGTH]


def _split(line: str) -> tuple[str, str]:
    path, _sep, rest = line.strip().rpartition(",")  # Size last; a path may contain commas (RECORD is CSV).
    path, _sep, digest = path.rpartition(",")
    return path.strip('"'), digest


def installed(package: str) -> str:
    """Identifier of an installed distribution; "" when unknown or installed editable (source checkout)."""
    from importlib.metadata import PackageNotFoundError, distribution

    try:
        dist = distribution(package)
    except PackageNotFoundError:
        return ""
    try:
        direct = json.loads(dist.read_text("direct_url.json") or "{}")
    except ValueError:
        direct = {}
    if (direct.get("dir_info") or {}).get("editable"):
        return ""
    record = dist.read_text("RECORD") or ""
    if any(line.split(",", 1)[0].endswith(".pth") for line in record.splitlines()):
        return ""  # Editable install without direct_url.json.
    return from_record(record.splitlines())


def label(version: str | None, build: str | None) -> str:
    """`0.6.0+d6f1018a2b` – version and build for display; just the version when the build is unknown."""
    return f"{version}+{build}" if version and build else (version or "?")
