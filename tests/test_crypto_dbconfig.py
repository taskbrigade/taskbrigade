import stat

import pytest
from cryptography.fernet import Fernet

from app import crypto, dbconfig
from app.db import supports_skip_locked


def test_master_key_created_once_with_0600(tmp_path):
    assert crypto.create_master_key(tmp_path) is True
    assert crypto.create_master_key(tmp_path) is False
    assert stat.S_IMODE(crypto.key_path(tmp_path).stat().st_mode) == 0o600


def test_encrypt_roundtrip_and_wrong_key(tmp_path):
    crypto.create_master_key(tmp_path)
    f = crypto.load_fernet(tmp_path)
    token = crypto.encrypt(f, "geheim")
    assert "geheim" not in token
    assert crypto.decrypt(f, token) == "geheim"
    with pytest.raises(ValueError):
        crypto.decrypt(Fernet(Fernet.generate_key()), token)


def test_missing_master_key(tmp_path):
    with pytest.raises(crypto.MasterKeyMissing):
        crypto.load_fernet(tmp_path)


def test_dbconfig_defaults_without_file(settings):
    cfg = dbconfig.load(settings)
    assert (cfg.host, cfg.port, cfg.database, cfg.user) == ("localhost", 3307, "taskbrigade", "taskadmin")
    assert cfg.password is None


def test_dbconfig_roundtrip_encrypted(settings, state_dir):
    dbconfig.save(settings, dbconfig.DbConfig("h", 3307, "d", "u", "p@ss:w/rd"))
    path = state_dir / "db.json"
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert "p@ss" not in path.read_text()
    cfg = dbconfig.load(settings)
    assert cfg.password == "p@ss:w/rd"
    assert "p%40ss%3Aw%2Frd" in cfg.url()  # URL-encoded.
    assert "p@ss" not in repr(cfg)


@pytest.mark.parametrize("version,expected", [
    ("10.5.19-MariaDB", False), ("10.6.12-MariaDB-log", True), ("11.4.2-MariaDB", True),
    ("10.3.39-MariaDB", False), ("8.0.36", True), ("garbage", False),
])
def test_skip_locked_detection(version, expected):
    assert supports_skip_locked(version) is expected
