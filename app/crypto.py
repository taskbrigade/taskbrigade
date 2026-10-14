"""Fernet master key for the DB password, Gitea tokens, and secret settings."""
from app.i18n import _
import os
from pathlib import Path

from cryptography.fernet import Fernet, InvalidToken

KEY_FILE = "master.key"


class MasterKeyMissing(RuntimeError):
    pass


def key_path(state_dir: Path) -> Path:
    return state_dir / KEY_FILE


def create_master_key(state_dir: Path) -> bool:
    """Create master.key if absent; return True if newly created."""
    path = key_path(state_dir)
    if path.exists():
        return False
    state_dir.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "wb") as f:
        f.write(Fernet.generate_key())
    return True


def load_fernet(state_dir: Path) -> Fernet:
    path = key_path(state_dir)
    if not path.exists():
        raise MasterKeyMissing(_("%(path)s is missing – run install.sh or `tbr-admin init-key`", path=path))
    return Fernet(path.read_bytes().strip())


def encrypt(fernet: Fernet, plaintext: str) -> str:
    return fernet.encrypt(plaintext.encode()).decode()


def decrypt(fernet: Fernet, token: str) -> str:
    try:
        return fernet.decrypt(token.encode()).decode()
    except InvalidToken as e:
        raise ValueError(_("Decryption failed (wrong master.key?)")) from e
