"""Base configuration from the environment and .env.

The DB connection settings here are defaults; `<state_dir>/db.json`, managed
through the UI, takes precedence (see app/dbconfig.py).

Names from before TaskBrigade 0.8 are still read during the transition, each noted once in the log: `TM_<X>` for the
`TBR_<X>` settings (the new name wins, `_LegacyEnvSource`) and for the variables read outside Settings (`getenv`),
and `/var/lib/taskmeister` as state directory while `/var/lib/taskbrigade` does not exist yet (a native installation
that deploy/update.sh has not migrated).
"""
import logging
import os
from functools import lru_cache
from ipaddress import ip_address, ip_network
from pathlib import Path

from pydantic import Field, field_validator, model_validator
from pydantic_settings import BaseSettings, PydanticBaseSettingsSource, SettingsConfigDict

ENV_PREFIX = "TBR_"
LEGACY_ENV_PREFIX = "TM_"  # Before TaskBrigade 0.8.
LEGACY_STATE_DIR = Path("/var/lib/taskmeister")
log = logging.getLogger("taskbrigade.config")
_noted: set[str] = set()


def note_legacy(old: str, new: str) -> None:
    if old not in _noted:
        _noted.add(old)
        log.warning("legacy_name", extra={"old": old, "new": new, "hint": "rename it; the old name is read only "
                                          "during the transition (docs/operations.md)"})


def getenv(name: str, default: str | None = None, environ=None) -> str | None:
    """`TBR_<name>` from the environment, else the old `TM_<name>` (noted once); empty counts as unset."""
    environ = os.environ if environ is None else environ
    if environ.get(ENV_PREFIX + name):
        return environ[ENV_PREFIX + name]
    if environ.get(LEGACY_ENV_PREFIX + name):
        note_legacy(LEGACY_ENV_PREFIX + name, ENV_PREFIX + name)
        return environ[LEGACY_ENV_PREFIX + name]
    return default


class _LegacyEnvSource(PydanticBaseSettingsSource):
    """`TM_<X>` for the `tbr_<x>` fields, from the environment and the env file, below the new names."""

    def __init__(self, settings_cls, *sources):
        super().__init__(settings_cls)
        self.sources = sources

    def get_field_value(self, field, field_name):  # Required by the base class; __call__ does the work.
        return None, field_name, False

    def __call__(self) -> dict:
        values = {}
        for name in self.settings_cls.model_fields:
            if not name.startswith("tbr_"):
                continue
            old = LEGACY_ENV_PREFIX + name.removeprefix("tbr_").upper()
            for source in self.sources:
                value = (getattr(source, "env_vars", None) or {}).get(old.lower())
                if value:
                    values[name] = value
                    note_legacy(old, name.upper())
                    break
        return values


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    @classmethod
    def settings_customise_sources(cls, settings_cls, init_settings, env_settings, dotenv_settings,
                                   file_secret_settings):
        return (init_settings, env_settings, dotenv_settings, _LegacyEnvSource(settings_cls, env_settings,
                                                                               dotenv_settings), file_secret_settings)

    # Storage for master.key and db.json.
    tbr_state_dir: Path = Path("/var/lib/taskbrigade")
    # Where the server keeps/builds the tbr wheel it delivers (empty = <tbr_state_dir>/dist; <source>/dist is
    # searched too, see app/services/distribution.py).
    tbr_dist_dir: str = ""

    # DB connection defaults (the password never comes from .env).
    tbr_db_host: str = "localhost"
    tbr_db_port: int = 3307
    tbr_db_name: str = "taskbrigade"
    tbr_db_user: str = "taskadmin"
    # Container mode: the password from the environment makes TBR_DB_* authoritative and db.json is ignored
    # (no setup mode). The native installation leaves it empty and keeps the password in db.json.
    tbr_db_password: str = Field(default="", repr=False)
    # Container start only: how long to wait for the database before giving up.
    tbr_db_wait_seconds: int = 120
    # Development/tests only: complete URL that overrides db.json.
    database_url: str | None = None

    secret_key: str = Field(default="", repr=False)
    bootstrap_admin_password_hash: str = Field(default="", repr=False)
    bootstrap_admin_actor: str = "admin"
    # Container start only: initial UI password for BOOTSTRAP_ADMIN_ACTOR, applied once while that actor
    # has no password yet (app/container.py).
    tbr_initial_admin_password: str = Field(default="", repr=False)
    # Migration 0005 only: owner of existing projects (empty = each project's creator).
    default_project_owner: str = ""
    ui_allowed_logins: str = ""
    # Networks allowed direct HTTP access to the UI (e.g. "192.168.1.0/24"). Empty = localhost only
    # (via tailscale serve). The API remains blocked on these networks; never expose tokens over LAN HTTP.
    lan_ui_networks: str = ""
    # Peers treated like loopback (e.g. the Docker bridge gateway that forwards a published port).
    # Empty = loopback only. They may use the API, send Tailscale-User-Login and fetch git credentials.
    trusted_peer_networks: str = ""
    # Networks allowed direct HTTP access to the UI and the API with bearer tokens (e.g. agents in the LAN
    # without Tailscale). Empty = none. Tokens then travel in cleartext unless a TLS proxy sits in
    # front; these peers are not trusted for Tailscale-User-Login or git credentials.
    api_networks: str = ""
    # May be disabled for tests only.
    enforce_client_networks: bool = True
    tz_display: str = "Europe/Berlin"
    # Default UI language (en|de); admins can override it under Admin → Settings, users per browser
    ui_language: str = "en"

    gitea_url: str = "http://gitea.example.internal:3001"
    # Address of the same Gitea as agents and browsers see it (clone URLs, git credentials, PR links).
    # Empty = GITEA_URL. Docker Compose: the app reaches http://gitea:3000, agents http://localhost:3000.
    gitea_public_url: str = ""
    gitea_token: str = Field(default="", repr=False)
    gitea_default_owner: str = ""

    # Default for the bot token setting (Admin → Settings overrides it). The chat ID is no longer used: every
    # human links their own chat in the profile; the field stays so existing env files keep loading.
    notify_telegram_bot_token: str = Field(default="", repr=False)
    notify_telegram_chat_id: str = ""

    scheduler_enabled: bool = True

    @model_validator(mode="after")
    def _legacy_state_dir(self):
        """Native installation from before 0.8 that deploy/update.sh has not migrated yet: keep its state."""
        if "tbr_state_dir" not in self.model_fields_set and not self.tbr_state_dir.exists() \
                and LEGACY_STATE_DIR.is_dir():
            note_legacy(str(LEGACY_STATE_DIR), str(self.tbr_state_dir))
            self.tbr_state_dir = LEGACY_STATE_DIR
        return self

    @field_validator("lan_ui_networks", "trusted_peer_networks", "api_networks")
    @classmethod
    def _valid_networks(cls, value: str) -> str:
        """Fail at startup on a typo instead of answering every request with 500."""
        for net in parse_networks(value):
            if net.prefixlen == 0:
                raise ValueError(f"{net} would open the server to every address")
        return value

    @property
    def lan_networks(self) -> list:
        return parse_networks(self.lan_ui_networks)

    @property
    def trusted_networks(self) -> list:
        return parse_networks(self.trusted_peer_networks)

    @property
    def api_client_networks(self) -> list:
        return parse_networks(self.api_networks)

    def is_trusted_peer(self, host: str) -> bool:
        """Loopback or a TRUSTED_PEER_NETWORKS address (the direct socket peer, never a forwarded header)."""
        addr = _peer_address(host)
        return addr is not None and (addr.is_loopback or any(addr in net for net in self.trusted_networks))

    def is_api_peer(self, host: str) -> bool:
        """An API_NETWORKS address: UI and API with tokens, but no loopback trust."""
        addr = _peer_address(host)
        return addr is not None and any(addr in net for net in self.api_client_networks)

    @property
    def allowed_logins(self) -> set[str]:
        return {x.strip().lower() for x in self.ui_allowed_logins.split(",") if x.strip()}


def parse_networks(value: str) -> list:
    """Comma-separated CIDRs (host bits allowed, e.g. "192.168.1.0/24, 10.0.0.5")."""
    return [ip_network(x.strip(), strict=False) for x in value.split(",") if x.strip()]


def _peer_address(host: str):
    try:
        return ip_address(host)
    except ValueError:
        return None


def _env_file() -> str | None:
    """Use .env in the working directory only if readable.

    `tbr-admin` may run as a system user in an unreadable directory; otherwise startup raises PermissionError.
    """
    path = getenv("ENV_FILE", ".env")
    try:
        return path if os.path.isfile(path) and os.access(path, os.R_OK) else None
    except OSError:
        return None


@lru_cache
def get_settings() -> Settings:
    return Settings(_env_file=_env_file())
