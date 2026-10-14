"""DB connection settings in `<state_dir>/db.json` (0600, Fernet-encrypted password)."""
import json
import os
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import quote_plus

from app import crypto
from app.config import Settings

DB_FILE = "db.json"


@dataclass
class DbConfig:
    host: str
    port: int
    database: str
    user: str
    password: str | None = field(default=None, repr=False)

    def url(self, database: str | None = None) -> str:
        pw = quote_plus(self.password or "")
        return (
            f"mariadb+pymysql://{quote_plus(self.user)}:{pw}@{self.host}:{self.port}/"
            f"{database or self.database}?charset=utf8mb4"
        )

    def public(self) -> dict:
        return {"host": self.host, "port": self.port, "database": self.database,
                "user": self.user, "password_set": bool(self.password)}


def _path(settings: Settings) -> Path:
    return settings.tbr_state_dir / DB_FILE


def from_env(settings: Settings) -> bool:
    """True when TBR_DB_PASSWORD is set: TBR_DB_* define the connection and db.json is ignored (container mode)."""
    return bool(settings.tbr_db_password)


def load(settings: Settings) -> DbConfig:
    """Use the environment (TBR_DB_PASSWORD set), db.json, or the Settings defaults without a password."""
    cfg = DbConfig(settings.tbr_db_host, settings.tbr_db_port, settings.tbr_db_name, settings.tbr_db_user)
    if from_env(settings):
        cfg.password = settings.tbr_db_password
        return cfg
    path = _path(settings)
    if not path.exists():
        return cfg
    data = json.loads(path.read_text())
    cfg.host = data.get("host", cfg.host)
    cfg.port = int(data.get("port", cfg.port))
    cfg.database = data.get("database", cfg.database)
    cfg.user = data.get("user", cfg.user)
    if data.get("password_enc"):
        cfg.password = crypto.decrypt(crypto.load_fernet(settings.tbr_state_dir), data["password_enc"])
    return cfg


def save(settings: Settings, cfg: DbConfig) -> None:
    fernet = crypto.load_fernet(settings.tbr_state_dir)
    data = {"host": cfg.host, "port": cfg.port, "database": cfg.database, "user": cfg.user,
            "password_enc": crypto.encrypt(fernet, cfg.password) if cfg.password else None}
    state_dir = settings.tbr_state_dir
    state_dir.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=state_dir, prefix=".db.json.")
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w") as f:
            json.dump(data, f, indent=2)
        os.replace(tmp, _path(settings))
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise
