"""`tbr-admin config get|set|unset|list`: the service's env file without knowing its path.

Native installation: the file systemd hands to `taskbrigade.service` (`EnvironmentFile=`, normally
`/etc/taskbrigade/taskbrigade.env`); `TBR_ENV_FILE` overrides the lookup. An installation from before TaskBrigade 0.8
that deploy/update.sh has not migrated yet is found under its old names (`taskmeister.service`,
`/etc/taskmeister/taskmeister.env`); its `TM_<X>` keys count as the `TBR_<X>` settings, and setting or removing a
`TBR_<X>` key also removes the old `TM_<X>` line (otherwise it would apply again). `TM_<X>` on the command line is
accepted as `TBR_<X>`. Keys and values are checked against
`app.config.Settings` (plus the few variables only the systemd unit and deploy scripts read), so a typo is refused
instead of being dropped silently by `extra="ignore"` on the next start. Secrets are never printed.

Docker Compose has no such file inside the container: `set`/`unset` refuse with a hint to `.env` on the Docker
host, `get`/`list` show the container's environment.
"""
import difflib
import ipaddress
import os
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path
from zoneinfo import ZoneInfo

from pydantic import ValidationError

from app.config import Settings
from cli.i18n import _

SERVICE = "taskbrigade.service"
DEFAULT_ENV_FILE = Path("/etc/taskbrigade/taskbrigade.env")
LEGACY_SERVICE = "taskmeister.service"  # Before TaskBrigade 0.8.
LEGACY_ENV_FILE = Path("/etc/taskmeister/taskmeister.env")
ENV_PREFIX, LEGACY_ENV_PREFIX = "TBR_", "TM_"
MASK = "********"


class ConfigError(Exception):
    """A refused key or value, or an env file that cannot be read or written; the message is shown as is."""


class _OnlyInit(Settings):
    """Validates the given values only – neither the caller's environment nor a `.env` in the cwd leak in."""

    @classmethod
    def settings_customise_sources(cls, settings_cls, init_settings, **_sources):
        return (init_settings,)


def _ip(value: str) -> None:
    ipaddress.ip_address(value)


def _whole_number(minimum: int):
    def check(value: str) -> None:
        if not value.isdigit() or int(value) < minimum:
            raise ValueError(_("must be a whole number ≥ %(min)s", min=minimum))
    return check


def _absolute_path(value: str) -> None:
    if not value.startswith("/"):
        raise ValueError(_("must be an absolute path"))


def _language(value: str) -> None:
    if value not in ("en", "de"):
        raise ValueError(_("must be en or de"))


def _time_zone(value: str) -> None:
    try:
        ZoneInfo(value)
    except (KeyError, ValueError, OSError) as e:  # ZoneInfoNotFoundError is a KeyError
        raise ValueError(_("unknown time zone")) from e


def _http_url(value: str) -> None:
    if value and not value.startswith(("http://", "https://")):
        raise ValueError(_("must start with http:// or https://"))


def _argon2_hash(value: str) -> None:
    if value and not value.startswith("$argon2"):
        raise ValueError(_("must be the hash printed by `tbr-admin hash-password`, not the password"))


# Read by the systemd unit or the deploy scripts, not by Settings: default and check.
EXTRA_KEYS = {
    "TBR_BIND_HOST": ("127.0.0.1", _ip),
    "TBR_MAX_ATTACHMENT_MB": ("10", _whole_number(1)),
    "TBR_BACKUP_DIR": ("/var/backups/taskbrigade", _absolute_path),
    "TBR_BACKUP_KEEP_DAYS": ("14", _whole_number(0)),
}
# Checks on top of the Settings types.
VALUE_CHECKS = {
    "UI_LANGUAGE": _language,
    "TZ_DISPLAY": _time_zone,
    "GITEA_URL": _http_url,
    "GITEA_PUBLIC_URL": _http_url,
    "BOOTSTRAP_ADMIN_PASSWORD_HASH": _argon2_hash,
}
# Not a repr=False field, but it carries a password.
EXTRA_SECRETS = {"DATABASE_URL"}


def _refused_reason(key: str) -> str | None:
    """Keys the native env file must not carry, with the way to do it instead."""
    if key == "TBR_DB_PASSWORD":
        return _("Store the database password with `tbr-admin set-db-password` or under Admin → Datenbank; in the "
                 "env file it would switch the server into container mode.")
    if key == "DATABASE_URL":
        return _("DATABASE_URL is for development and tests only; it would put the database password into the env "
                 "file.")
    if key == "TBR_INITIAL_ADMIN_PASSWORD":
        return _("TBR_INITIAL_ADMIN_PASSWORD is read by the Docker container only. Use `tbr-admin hash-password` with "
                 "BOOTSTRAP_ADMIN_PASSWORD_HASH, or `tbr-admin reset-admin-password`.")
    return None


@dataclass(frozen=True)
class Key:
    name: str
    default: str
    secret: bool


def known_keys() -> dict[str, Key]:
    keys = {}
    for field, info in Settings.model_fields.items():
        name = field.upper()
        default = info.default
        keys[name] = Key(name, "" if default is None else _render_default(default),
                         info.repr is False or name in EXTRA_SECRETS)
    for name, (default, _check) in EXTRA_KEYS.items():
        keys[name] = Key(name, default, False)
    return keys


def _render_default(value) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value)


def resolve_key(raw: str) -> Key:
    """`gitea_url` → GITEA_URL; an unknown key is refused with the closest known names."""
    keys = known_keys()
    name = raw.strip().upper().replace("-", "_")
    if name.startswith(LEGACY_ENV_PREFIX) and ENV_PREFIX + name.removeprefix(LEGACY_ENV_PREFIX) in keys:
        name = ENV_PREFIX + name.removeprefix(LEGACY_ENV_PREFIX)  # Name before TaskBrigade 0.8.
    if name in keys:
        return keys[name]
    close = difflib.get_close_matches(name, keys, n=3, cutoff=0.6)
    hint = _(" Did you mean: %(keys)s?", keys=", ".join(close)) if close else ""
    raise ConfigError(_("Unknown setting %(key)s.", key=raw) + hint
                      + _(" `tbr-admin config list` shows all settings."))


def validate(key: Key, value: str) -> str:
    """The value as it goes into the file, or ConfigError with the reason."""
    if "\n" in value or "\r" in value or "\0" in value:
        raise ConfigError(_("%(key)s: the value must be a single line", key=key.name))
    try:
        if key.name in EXTRA_KEYS:
            EXTRA_KEYS[key.name][1](value)
        else:
            _OnlyInit(**{key.name.lower(): value})
            if key.name in VALUE_CHECKS:
                VALUE_CHECKS[key.name](value)
    except ValidationError as e:
        reason = e.errors()[0]["msg"].removeprefix("Value error, ")
        raise ConfigError(_("%(key)s: invalid value (%(reason)s)", key=key.name, reason=reason)) from None
    except ValueError as e:
        raise ConfigError(_("%(key)s: invalid value (%(reason)s)", key=key.name, reason=e)) from None
    return value


def shown(key: Key, value: str) -> str:
    return MASK if key.secret and value else value


# --- where the configuration lives ----------------------------------------------------------------------------


def in_container() -> bool:
    from cli.legacy import getenv

    return Path("/.dockerenv").exists() or bool(getenv("DB_PASSWORD"))


def _systemd_env_file(service: str = SERVICE) -> Path | None:
    """The first `EnvironmentFile=` of the installed unit (what the service really reads)."""
    if not shutil.which("systemctl"):
        return None
    try:
        out = subprocess.run(["systemctl", "show", "--property=EnvironmentFiles", "--value", service],
                             capture_output=True, text=True, timeout=10).stdout
    except (OSError, subprocess.SubprocessError):
        return None
    for line in out.splitlines():
        path = line.split(" (", 1)[0].strip().lstrip("-")
        if path.startswith("/"):
            return Path(path)
    return None


def env_file() -> Path | None:
    """The file to edit; None means Docker (no file in the container)."""
    from cli.legacy import getenv

    if getenv("ENV_FILE"):
        return Path(getenv("ENV_FILE"))
    if in_container():
        return None
    found = _systemd_env_file() or _systemd_env_file(LEGACY_SERVICE)
    if found:
        return found
    return LEGACY_ENV_FILE if not DEFAULT_ENV_FILE.exists() and LEGACY_ENV_FILE.exists() else DEFAULT_ENV_FILE


def service_name() -> str:
    """taskbrigade.service, or taskmeister.service on an installation that has not been migrated yet."""
    for name in (SERVICE, LEGACY_SERVICE):
        try:
            state = subprocess.run(["systemctl", "show", "--property=LoadState", "--value", name],
                                   capture_output=True, text=True, timeout=10).stdout.strip()
        except (OSError, subprocess.SubprocessError):
            break
        if state == "loaded":
            return name
    return SERVICE


def docker_hint() -> str:
    return _("TaskBrigade runs in a container here. Change settings in `.env` next to compose.yaml on the Docker "
             "host, then apply them with `docker compose up -d`.")


# --- reading and writing the file -----------------------------------------------------------------------------


def _assignment(line: str) -> tuple[str, str] | None:
    """`KEY=value` (optionally `export KEY=value`) → (KEY, raw value); comments and other lines → None."""
    text = line.strip()
    if not text or text.startswith(("#", ";")) or "=" not in text:
        return None
    if text.startswith("export "):
        text = text[len("export "):].lstrip()
    name, raw = text.split("=", 1)
    return name.strip(), raw.strip()


def _unquote(raw: str) -> str:
    if len(raw) >= 2 and raw[0] == raw[-1] and raw[0] in "'\"":
        return raw[1:-1]
    return raw


def _quote(key: str, value: str) -> str:
    """Raw like install.sh writes it; single quotes only where systemd and dotenv would read it differently."""
    if value == value.strip() and " #" not in value and not value.startswith(("'", '"')):
        return value
    if "'" in value:
        raise ConfigError(_("%(key)s: a value with leading/trailing spaces or ' #' must not contain a single "
                            "quote", key=key))
    return f"'{value}'"


def _read_lines(path: Path) -> list[str]:
    try:
        return path.read_text().splitlines()
    except FileNotFoundError:
        raise ConfigError(_("%(path)s does not exist. Install the server with deploy/install.sh first, or point "
                            "TBR_ENV_FILE at the env file.", path=path)) from None
    except PermissionError:
        raise ConfigError(_("No permission to read %(path)s – run tbr-admin with sudo.", path=path)) from None


def read_values(path: Path) -> dict[str, str]:
    values = {}
    for line in _read_lines(path):
        parsed = _assignment(line)
        if parsed:
            values[parsed[0]] = _unquote(parsed[1])  # the last assignment wins, as in systemd
    return with_legacy(values)


def with_legacy(values: dict[str, str]) -> dict[str, str]:
    """`TM_<X>` from before 0.8 counts as `TBR_<X>` where that is not set (as the server reads it)."""
    out = dict(values)
    for name, value in values.items():
        if name.startswith(LEGACY_ENV_PREFIX):
            out.setdefault(ENV_PREFIX + name.removeprefix(LEGACY_ENV_PREFIX), value)
    return out


def environment_values() -> dict[str, str]:
    keys = known_keys()
    return {k: v for k, v in with_legacy(dict(os.environ)).items() if k in keys}


def updated_lines(lines: list[str], key: str, value: str | None) -> list[str]:
    """Replace the first assignment of `key` (or its commented example `# KEY=…`), drop later duplicates;
    append if neither exists. `value=None` removes the assignments."""
    out, done = [], False
    for line in lines:
        parsed = _assignment(line)
        if parsed and parsed[0] == key:
            if value is not None and not done:
                out.append(f"{key}={_quote(key, value)}")
                done = True
            continue
        out.append(line)
    if value is None or done:
        return out
    for i, line in enumerate(out):
        commented = line.lstrip("#").strip()
        if line.startswith("#") and commented.startswith(f"{key}="):
            out[i] = f"{key}={_quote(key, value)}"
            return out
    return [*out, f"{key}={_quote(key, value)}"]


def write_lines(path: Path, lines: list[str]) -> None:
    """Atomically, keeping owner, group and mode (root:taskbrigade 0640 on a native installation)."""
    try:
        st = path.stat()
        fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.")
    except PermissionError:
        raise ConfigError(_("No permission to change %(path)s – run tbr-admin with sudo.", path=path)) from None
    try:
        with os.fdopen(fd, "w") as f:
            f.write("\n".join(lines) + "\n")
            f.flush()
            os.fsync(f.fileno())
        os.chmod(tmp, st.st_mode & 0o7777)
        if (os.stat(tmp).st_uid, os.stat(tmp).st_gid) != (st.st_uid, st.st_gid):
            os.chown(tmp, st.st_uid, st.st_gid)
        os.replace(tmp, path)
    except PermissionError:
        Path(tmp).unlink(missing_ok=True)
        raise ConfigError(_("No permission to change %(path)s – run tbr-admin with sudo.", path=path)) from None
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


def change(path: Path, key: Key, value: str | None) -> bool:
    """Set (`value`) or remove (`None`) a key; False if the file already says so. Removing is always allowed,
    also for keys that may not be set (an empty value is harmless as well)."""
    if value:
        if reason := _refused_reason(key.name):
            raise ConfigError(reason)
    if value is not None:
        validate(key, value)
    lines = _read_lines(path)
    new = lines
    if key.name.startswith(ENV_PREFIX):  # The old name: changed in its place, and never left to apply again.
        legacy = LEGACY_ENV_PREFIX + key.name.removeprefix(ENV_PREFIX)
        if not any((_assignment(line) or ("",))[0] == key.name for line in new):
            new = [f"{key.name}={_assignment(line)[1]}" if (_assignment(line) or ("",))[0] == legacy else line
                   for line in new]
        new = updated_lines(new, legacy, None)
    new = updated_lines(new, key.name, value)
    if new == lines:
        return False
    write_lines(path, new)
    return True


def restart_service() -> tuple[bool, str]:
    try:
        r = subprocess.run(["systemctl", "restart", service_name()], capture_output=True, text=True, timeout=60)
    except (OSError, subprocess.SubprocessError) as e:
        return False, str(e)
    return r.returncode == 0, (r.stderr or r.stdout).strip()
