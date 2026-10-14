"""`tm-admin config get|set|unset|list` (cli/admin_config.py): env file lookup, validation, masking, Docker."""
import stat
import subprocess
from pathlib import Path

import pytest
from typer.testing import CliRunner

from cli import admin_config as cfgfile
from cli import tm_admin

ROOT = Path(__file__).resolve().parent.parent
runner = CliRunner()


@pytest.fixture
def restarts():
    return []


@pytest.fixture
def env(tmp_path, monkeypatch, restarts):
    """A native installation whose service reads tmp_path/taskbrigade.env – the caller never names the path."""
    path = tmp_path / "taskbrigade.env"
    path.write_text("# TaskBrigade\nGITEA_URL=http://gitea.example.internal:3001\nGITEA_TOKEN=\n"
                    "# SCHEDULER_ENABLED=true\nMARIADB_ROOT_PASSWORD=\n")
    path.chmod(0o640)
    monkeypatch.delenv("TBR_ENV_FILE", raising=False)
    monkeypatch.delenv("TBR_DB_PASSWORD", raising=False)
    monkeypatch.setattr(cfgfile, "in_container", lambda: False)
    monkeypatch.setattr(cfgfile, "_systemd_env_file", lambda: path)
    monkeypatch.setattr(cfgfile, "restart_service", lambda: restarts.append(1) or (True, ""))
    return path


def admin(*args, input=None):
    return runner.invoke(tm_admin.app, ["config", *args], input=input)


def test_set_writes_the_file_the_service_reads(env):
    r = admin("set", "gitea_url", "http://127.0.0.1:3001", "--no-restart")
    assert r.exit_code == 0, r.output
    assert f"GITEA_URL=http://127.0.0.1:3001 written to {env}" in r.output
    text = env.read_text()
    assert "GITEA_URL=http://127.0.0.1:3001\n" in text and "gitea.example.internal" not in text
    assert stat.S_IMODE(env.stat().st_mode) == 0o640
    assert not list(env.parent.glob(".taskbrigade.env.*"))  # no temp file left behind
    assert admin("get", "GITEA_URL").output.strip() == "http://127.0.0.1:3001"


def test_unknown_key_is_refused_with_suggestion(env):
    before = env.read_text()
    r = admin("set", "GITEA_URLL", "http://x", "--no-restart")
    assert r.exit_code == 1
    assert "Unknown setting GITEA_URLL" in r.output and "GITEA_URL" in r.output
    assert admin("get", "NO_SUCH_SETTING").exit_code == 1
    assert admin("unset", "MARIADB_ROOT_PASSWORD").exit_code == 1  # Docker-only, not a server setting
    assert env.read_text() == before


@pytest.mark.parametrize("key,value", [
    ("SCHEDULER_ENABLED", "maybe"),
    ("TBR_DB_PORT", "abc"),
    ("LAN_UI_NETWORKS", "0.0.0.0/0"),
    ("API_NETWORKS", "10.0.0.0/33"),
    ("TBR_BIND_HOST", "localhost"),
    ("TBR_BACKUP_KEEP_DAYS", "1.5"),
    ("UI_LANGUAGE", "fr"),
    ("TZ_DISPLAY", "Mars/Base"),
    ("GITEA_URL", "gitea.example.internal:3001"),
    ("BOOTSTRAP_ADMIN_PASSWORD_HASH", "plain-password"),
])
def test_invalid_values_are_refused(env, key, value):
    before = env.read_text()
    r = admin("set", key, value, "--no-restart")
    assert r.exit_code == 1 and f"{key}: invalid value" in r.output
    assert env.read_text() == before


def test_keys_that_do_not_belong_in_the_native_file(env):
    r = admin("set", "TBR_DB_PASSWORD", "s3cret-pw", "--no-restart")
    assert r.exit_code == 1 and "set-db-password" in r.output and "s3cret-pw" not in env.read_text()
    assert admin("set", "DATABASE_URL", "mysql://u:p@h/db", "--no-restart").exit_code == 1
    env.write_text(env.read_text() + "TBR_DB_PASSWORD=old\n")
    assert admin("unset", "TBR_DB_PASSWORD", "--no-restart").exit_code == 0  # removing is fine
    assert "TBR_DB_PASSWORD" not in env.read_text()


def test_bool_replaces_commented_example_and_duplicates_go(env):
    env.write_text(env.read_text() + "GITEA_URL=http://later.example.internal\n")
    assert admin("set", "SCHEDULER_ENABLED", "false", "--no-restart").exit_code == 0
    assert admin("set", "GITEA_URL", "http://127.0.0.1:3001", "--no-restart").exit_code == 0
    lines = env.read_text().splitlines()
    assert "SCHEDULER_ENABLED=false" in lines and "# SCHEDULER_ENABLED=true" not in lines
    assert [x for x in lines if x.startswith("GITEA_URL=")] == ["GITEA_URL=http://127.0.0.1:3001"]
    assert admin("set", "TZ_DISPLAY", "UTC", "--no-restart").exit_code == 0
    assert env.read_text().endswith("TZ_DISPLAY=UTC\n")  # not in the file before: appended


def test_unchanged_value_does_not_write_or_restart(env, restarts):
    mtime = env.stat().st_mtime_ns
    r = admin("set", "GITEA_URL", "http://gitea.example.internal:3001", "--restart")
    assert r.exit_code == 0 and "unchanged" in r.output
    assert env.stat().st_mtime_ns == mtime and restarts == []


def test_secrets_are_masked_everywhere(env):
    r = admin("set", "GITEA_TOKEN", "--no-restart", input="tok-123456\n")  # prompted, hidden
    assert r.exit_code == 0, r.output
    assert "GITEA_TOKEN=tok-123456" in env.read_text()
    assert "tok-123456" not in r.output and "GITEA_TOKEN=********" in r.output
    assert admin("get", "GITEA_TOKEN").output.strip() == cfgfile.MASK
    listing = admin("list").output
    assert "tok-123456" not in listing and "GITEA_TOKEN=********" in listing
    assert "SECRET_KEY=  (default)" in listing  # empty secrets stay visibly empty


def test_list_shows_file_values_and_defaults(env):
    out = admin("list").output
    assert f"# {env}" in out
    assert "GITEA_URL=http://gitea.example.internal:3001\n" in out
    assert "TBR_DB_PORT=3307  (default)" in out and "SCHEDULER_ENABLED=true  (default)" in out
    assert "TBR_BIND_HOST=127.0.0.1  (default)" in out
    assert "MARIADB_ROOT_PASSWORD" not in out


def test_restart_offer(env, restarts):
    r = admin("set", "TZ_DISPLAY", "UTC")  # not interactive: only the hint
    assert "sudo systemctl restart taskbrigade.service" in r.output and restarts == []
    r = admin("set", "TZ_DISPLAY", "Europe/Berlin", "--restart")
    assert r.exit_code == 0 and "restarted" in r.output and restarts == [1]


def test_permission_and_missing_file_messages(env, monkeypatch):
    env.unlink()
    r = admin("set", "TZ_DISPLAY", "UTC", "--no-restart")
    assert r.exit_code == 1 and "does not exist" in r.output and "Traceback" not in r.output

    def denied(*_args, **_kw):
        raise PermissionError
    monkeypatch.setattr(cfgfile.Path, "read_text", denied)
    r = admin("list")
    assert r.exit_code == 1 and "sudo" in r.output


def test_docker_points_to_env_file(monkeypatch):
    monkeypatch.delenv("TBR_ENV_FILE", raising=False)
    monkeypatch.setattr(cfgfile, "in_container", lambda: True)
    monkeypatch.setenv("GITEA_URL", "http://gitea:3000")
    monkeypatch.setenv("TBR_DB_PASSWORD", "container-db-pw")
    r = admin("set", "GITEA_URL", "http://x")
    assert r.exit_code == 1 and "`.env` next to compose.yaml" in r.output and "docker compose up -d" in r.output
    out = admin("list").output
    assert "GITEA_URL=http://gitea:3000" in out and "container-db-pw" not in out and "TBR_DB_PASSWORD=****" in out


def test_tm_env_file_overrides_lookup(tmp_path, monkeypatch):
    path = tmp_path / "custom.env"
    path.write_text("")
    monkeypatch.setenv("TBR_ENV_FILE", str(path))
    monkeypatch.setattr(cfgfile, "in_container", lambda: True)  # an explicit file wins even in a container
    assert cfgfile.env_file() == path
    assert admin("set", "UI_LANGUAGE", "de", "--no-restart").exit_code == 0
    assert path.read_text() == "UI_LANGUAGE=de\n"


def test_systemd_unit_names_the_file(tmp_path, monkeypatch):
    monkeypatch.setattr(cfgfile, "DEFAULT_ENV_FILE", tmp_path / "taskbrigade.env")  # never the real /etc
    monkeypatch.setattr(cfgfile, "LEGACY_ENV_FILE", tmp_path / "taskmeister.env")
    monkeypatch.setattr(cfgfile.shutil, "which", lambda _cmd: "/usr/bin/systemctl")
    seen = []

    def fake_run(cmd, **_kw):
        seen.append(cmd)
        return subprocess.CompletedProcess(cmd, 0, "-/srv/tm/taskbrigade.env (ignore_errors=yes)\n", "")
    monkeypatch.setattr(cfgfile.subprocess, "run", fake_run)
    assert cfgfile._systemd_env_file() == Path("/srv/tm/taskbrigade.env")
    assert seen[0][:2] == ["systemctl", "show"] and "taskbrigade.service" in seen[0]
    monkeypatch.setattr(cfgfile.shutil, "which", lambda _cmd: None)
    monkeypatch.delenv("TBR_ENV_FILE", raising=False)
    monkeypatch.setattr(cfgfile, "in_container", lambda: False)
    assert cfgfile.env_file() == cfgfile.DEFAULT_ENV_FILE


def test_unreadable_config_directory_is_not_a_traceback(tmp_path, monkeypatch, restarts):
    """`/etc/taskbrigade` exists but is not readable for this user (e.g. on a production host): no traceback, a hint to use sudo."""
    locked = tmp_path / "locked"  # stands for /etc/taskbrigade
    default, legacy = locked / "taskbrigade.env", tmp_path / "legacy" / "taskmeister.env"
    monkeypatch.setattr(cfgfile, "DEFAULT_ENV_FILE", default)
    monkeypatch.setattr(cfgfile, "LEGACY_ENV_FILE", legacy)
    monkeypatch.setattr(cfgfile, "_systemd_env_file", lambda service=cfgfile.SERVICE: None)
    monkeypatch.setattr(cfgfile, "in_container", lambda: False)
    for name in ("TBR_ENV_FILE", "TM_ENV_FILE"):
        monkeypatch.delenv(name, raising=False)
    real_exists, real_read_text = Path.exists, Path.read_text

    def denied(path):
        return path == locked or locked in path.parents

    def exists(self, *args, **kwargs):
        if denied(self):
            raise PermissionError(13, "Permission denied", str(self))
        return real_exists(self, *args, **kwargs)

    def read_text(self, *args, **kwargs):
        if denied(self):
            raise PermissionError(13, "Permission denied", str(self))
        return real_read_text(self, *args, **kwargs)
    monkeypatch.setattr(Path, "exists", exists)
    monkeypatch.setattr(Path, "read_text", read_text)

    assert cfgfile.env_file() == default
    for args in (["list"], ["set", "TZ_DISPLAY", "UTC", "--no-restart"]):
        r = admin(*args)
        assert r.exit_code == 1 and "sudo" in r.output and "Traceback" not in r.output, r.output
    assert restarts == []


def test_values_round_trip_through_quotes():
    lines = cfgfile.updated_lines([], "UI_ALLOWED_LOGINS", " admin@example.org ")
    assert lines == ["UI_ALLOWED_LOGINS=' admin@example.org '"]
    assert cfgfile._unquote(cfgfile._assignment(lines[0])[1]) == " admin@example.org "
    hash_line = cfgfile.updated_lines([], "BOOTSTRAP_ADMIN_PASSWORD_HASH", "$argon2id$v=19$m=65536,t=3,p=4$abc")
    assert hash_line == ["BOOTSTRAP_ADMIN_PASSWORD_HASH=$argon2id$v=19$m=65536,t=3,p=4$abc"]  # like install.sh
    with pytest.raises(cfgfile.ConfigError):
        cfgfile.validate(cfgfile.resolve_key("GITEA_DEFAULT_OWNER"), "a\nb")


def test_every_server_key_in_env_example_is_known():
    """`.env.example` and the validator must not drift apart (Docker-only keys excluded)."""
    text = (ROOT / ".env.example").read_text().split("# --- Docker Compose", 1)[0]
    keys = set()
    for line in text.splitlines():
        candidate = line.lstrip("# ").split("=", 1)[0]
        if "=" in line and candidate.isupper() and candidate.replace("_", "").isalnum():
            keys.add(candidate)
    unknown = keys - set(cfgfile.known_keys()) - {"TBR_ENV_FILE"}
    assert not unknown


def test_german_messages(env, monkeypatch):
    monkeypatch.setenv("TBR_LANGUAGE", "de")
    r = admin("set", "GITEA_URLL", "x")
    assert "Unbekannte Einstellung GITEA_URLL" in r.output
