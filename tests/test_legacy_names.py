"""Names from before TaskBrigade 0.8 (taskmeister, TM_*) are still read during the transition, with a note, while
everything new uses taskbrigade/TBR_* (renames of agent machines: tests/test_rename.py)."""
import os
import subprocess
import sys
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient
from typer.testing import CliRunner

from app import config as app_config
from app import container
from app.main import app
from cli import admin_config as cfgfile
from cli import client as client_mod
from cli import i18n, legacy, tm

ROOT = Path(__file__).resolve().parent.parent
runner = CliRunner()


@pytest.fixture
def clean_env(monkeypatch):
    for name in ("URL", "TOKEN", "CONFIG", "LANGUAGE"):
        monkeypatch.delenv(f"TBR_{name}", raising=False)
        monkeypatch.delenv(f"TM_{name}", raising=False)
    monkeypatch.delenv("TBR_NO_RENAME_NOTICE", raising=False)
    monkeypatch.setattr(legacy, "_noted", set())
    monkeypatch.setattr(sys, "argv", ["tbr", "list"])


# --- Agent configuration and variables (cli) ----------------------------------------------------------------------

def test_old_configuration_file_is_read_with_a_note_once(tmp_path, clean_env, capsys):
    (tmp_path / ".taskmeister.toml").write_text('url = "https://old.example/"\ntoken = "tm_old"\n')
    sub = tmp_path / "KET" / ".worktrees" / "KET-0001"
    sub.mkdir(parents=True)
    path = client_mod.find_config(sub)
    assert path == tmp_path / ".taskmeister.toml"
    assert client_mod.load_config(path).token == "tm_old"
    client_mod.load_config(path)
    err = capsys.readouterr().err
    assert err.count("from before TaskBrigade 0.8") == 1 and ".taskbrigade.toml" in err


def test_new_configuration_file_wins_in_the_same_directory(tmp_path, clean_env, capsys):
    (tmp_path / ".taskmeister.toml").write_text('url = "https://old.example"\ntoken = "tm_old"\n')
    (tmp_path / ".taskbrigade.toml").write_text('url = "https://new.example"\ntoken = "tm_new"\n')
    assert client_mod.find_config(tmp_path) == tmp_path / ".taskbrigade.toml"
    assert client_mod.agent_config(tmp_path) == tmp_path / ".taskbrigade.toml"
    assert client_mod.load_config(client_mod.find_config(tmp_path)).token == "tm_new"
    assert capsys.readouterr().err == ""


def test_old_global_configuration_is_still_found(tmp_path, clean_env, monkeypatch):
    monkeypatch.setattr(client_mod, "CONFIG_PATH", tmp_path / "config" / "taskbrigade" / "config.toml")
    legacy_global = tmp_path / "config" / "taskmeister" / "config.toml"
    legacy_global.parent.mkdir(parents=True)
    legacy_global.write_text('url = "https://old.example"\ntoken = "tm_global"\n')
    empty = tmp_path / "empty"
    empty.mkdir()
    assert client_mod.find_config(empty) == legacy_global
    client_mod.CONFIG_PATH.parent.mkdir(parents=True)
    client_mod.CONFIG_PATH.write_text('url = "https://new.example"\ntoken = "tm_new"\n')
    assert client_mod.find_config(empty) == client_mod.CONFIG_PATH


def test_old_variables_are_read_with_a_note_and_new_ones_win(tmp_path, clean_env, monkeypatch, capsys):
    monkeypatch.setenv("TM_URL", "https://old.example")
    monkeypatch.setenv("TM_TOKEN", "tm_old")
    cfg = client_mod.load_config(tmp_path / "missing.toml")
    assert (cfg.url, cfg.token) == ("https://old.example", "tm_old")
    err = capsys.readouterr().err
    assert "TM_URL is now called TBR_URL" in err and "TM_TOKEN is now called TBR_TOKEN" in err
    monkeypatch.setenv("TBR_URL", "https://new.example")
    assert client_mod.load_config(tmp_path / "missing.toml").url == "https://new.example"
    monkeypatch.setenv("TM_CONFIG", str(tmp_path / "x.toml"))
    assert client_mod.find_config(tmp_path) == tmp_path / "x.toml"


def test_no_note_for_machine_interfaces(tmp_path, clean_env, monkeypatch, capsys):
    """git reads stdout of the credential helper and shows its stderr on every fetch."""
    monkeypatch.setattr(sys, "argv", ["tbr", "git-credential", "get"])
    monkeypatch.setenv("TM_URL", "https://old.example")
    assert legacy.getenv("URL") == "https://old.example"
    assert capsys.readouterr().err == ""


def test_old_language_variable(clean_env, monkeypatch):
    monkeypatch.setattr(i18n, "_configured", None)
    monkeypatch.setenv("TM_LANGUAGE", "de")
    assert i18n.language() == "de"
    monkeypatch.setenv("TBR_LANGUAGE", "en")
    assert i18n.language() == "en"


def test_join_respects_and_retires_an_old_configuration(tmp_path, respx_mock):
    (tmp_path / ".taskmeister.toml").write_text('url = "x"\n')
    r = runner.invoke(tm.app, ["join", "https://tm.example", "tmj_abc", "--dir", str(tmp_path)])
    assert r.exit_code == 1 and ".taskmeister.toml already exists" in r.stderr and not respx_mock.calls
    respx_mock.post("https://tm.example/api/v1/join").mock(return_value=httpx.Response(200, json={
        "token": "tm_new", "actor": {"name": "codex", "agent_kind": "codex"}}))
    respx_mock.get("https://tm.example/api/v1/agents/me").mock(return_value=httpx.Response(200, json={
        "name": "codex", "agent_kind": "codex", "role": "worker"}))
    respx_mock.get("https://tm.example/api/v1/agents/me/git-credentials").mock(return_value=httpx.Response(404, json={
        "error": {"code": "not_found", "message": "none"}}))
    r = runner.invoke(tm.app, ["join", "https://tm.example", "tmj_abc", "--dir", str(tmp_path), "--force"])
    assert 'token = "tm_new"' in (tmp_path / ".taskbrigade.toml").read_text(), r.output
    assert not (tmp_path / ".taskmeister.toml").exists()
    assert (tmp_path / ".taskmeister.toml.bak").read_text() == 'url = "x"\n'


# --- Version headers ------------------------------------------------------------------------------------------------

def test_server_sends_the_new_and_the_old_header_names():
    with TestClient(app, client=("127.0.0.1", 50000)) as c:
        r = c.get("/healthz")
    assert r.headers["X-TaskBrigade-Version"] == r.headers["X-Taskmeister-Version"]


@pytest.mark.parametrize("prefix", ["X-TaskBrigade", "X-Taskmeister"])
def test_client_reads_both_header_names(prefix):
    r = httpx.Response(200, headers={f"{prefix}-Version": "0.8.0", f"{prefix}-Build": "abc"})
    assert client_mod.server_headers(r) == ("0.8.0", "abc")


# --- Server settings -----------------------------------------------------------------------------------------------

def test_settings_read_old_variables_below_the_new_names(monkeypatch, tmp_path, caplog):
    monkeypatch.setattr(app_config, "_noted", set())
    monkeypatch.delenv("TBR_DB_NAME", raising=False)
    monkeypatch.setenv("TM_DB_NAME", "taskmeister")
    monkeypatch.setenv("TM_DB_PORT", "3306")
    s = app_config.Settings(_env_file=None)
    assert (s.tbr_db_name, s.tbr_db_port) == ("taskmeister", 3306)
    assert {r.old for r in caplog.records if r.msg == "legacy_name"} == {"TM_DB_NAME", "TM_DB_PORT"}
    monkeypatch.setenv("TBR_DB_NAME", "taskbrigade")
    assert app_config.Settings(_env_file=None).tbr_db_name == "taskbrigade"


def test_settings_read_old_names_from_the_env_file(monkeypatch, tmp_path):
    for name in ("TBR_DB_HOST", "TM_DB_HOST", "TBR_DB_USER", "TM_DB_USER"):
        monkeypatch.delenv(name, raising=False)
    env = tmp_path / "x.env"
    env.write_text("TM_DB_HOST=db.example.internal\nTM_DB_USER=old\nTBR_DB_USER=new\n")
    s = app_config.Settings(_env_file=str(env))
    assert (s.tbr_db_host, s.tbr_db_user) == ("db.example.internal", "new")


def test_unmigrated_native_installation_keeps_its_state_directory(monkeypatch, tmp_path):
    monkeypatch.delenv("TBR_STATE_DIR", raising=False)
    monkeypatch.delenv("TM_STATE_DIR", raising=False)
    old = tmp_path / "taskmeister"
    monkeypatch.setattr(app_config, "LEGACY_STATE_DIR", old)
    default = app_config.Settings.model_fields["tbr_state_dir"].default
    assert app_config.Settings(_env_file=None).tbr_state_dir == default  # nothing old there
    old.mkdir()
    if default.exists():
        pytest.skip(f"{default} exists on this machine")
    assert app_config.Settings(_env_file=None).tbr_state_dir == old
    assert app_config.Settings(_env_file=None, tbr_state_dir=tmp_path / "x").tbr_state_dir == tmp_path / "x"


def test_variables_outside_settings_fall_back_to_old_names():
    assert app_config.getenv("BIND_HOST", "d", {"TM_BIND_HOST": "0.0.0.0"}) == "0.0.0.0"
    assert app_config.getenv("BIND_HOST", "d", {"TM_BIND_HOST": "a", "TBR_BIND_HOST": "b"}) == "b"
    assert app_config.getenv("BIND_HOST", "d", {}) == "d"
    assert container.uvicorn_argv({"TM_PORT": "9000"})[5] == "9000"


# --- tbr-admin config ----------------------------------------------------------------------------------------------

def test_admin_config_accepts_old_keys_and_drops_old_lines(tmp_path, monkeypatch):
    path = tmp_path / "taskmeister.env"
    path.write_text("TM_BIND_HOST=0.0.0.0\nTM_BACKUP_KEEP_DAYS=7\n")
    assert cfgfile.resolve_key("tm_bind_host").name == "TBR_BIND_HOST"
    assert cfgfile.read_values(path)["TBR_BACKUP_KEEP_DAYS"] == "7"  # the old line counts, as for the server
    assert cfgfile.change(path, cfgfile.resolve_key("TBR_BIND_HOST"), "127.0.0.1")
    assert path.read_text() == "TBR_BIND_HOST=127.0.0.1\nTM_BACKUP_KEEP_DAYS=7\n"
    assert cfgfile.change(path, cfgfile.resolve_key("TBR_BACKUP_KEEP_DAYS"), None)  # unset: the old one goes too
    assert path.read_text() == "TBR_BIND_HOST=127.0.0.1\n"


def test_admin_config_finds_the_env_file_of_an_unmigrated_installation(tmp_path, monkeypatch):
    for name in ("TBR_ENV_FILE", "TM_ENV_FILE", "TBR_DB_PASSWORD", "TM_DB_PASSWORD"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(cfgfile, "in_container", lambda: False)
    monkeypatch.setattr(cfgfile, "_systemd_env_file", lambda service=cfgfile.SERVICE: None)
    monkeypatch.setattr(cfgfile, "DEFAULT_ENV_FILE", tmp_path / "taskbrigade.env")
    monkeypatch.setattr(cfgfile, "LEGACY_ENV_FILE", tmp_path / "taskmeister.env")
    assert cfgfile.env_file() == tmp_path / "taskbrigade.env"
    (tmp_path / "taskmeister.env").write_text("")
    assert cfgfile.env_file() == tmp_path / "taskmeister.env"
    monkeypatch.setenv("TM_ENV_FILE", str(tmp_path / "own.env"))
    assert cfgfile.env_file() == tmp_path / "own.env"


# --- Deploy helpers: env file renames (native and Docker) ----------------------------------------------------------

ENV_BEFORE = ("SECRET_KEY=x\nTM_BIND_HOST=0.0.0.0\n# TM_DB_HOST=db.example.internal\n"
              "TM_BACKUP_DIR=/var/backups/taskmeister\nTM_DB_NAME=a\nTBR_DB_NAME=b\nexport TM_PORT=1\n")
ENV_AFTER = ("SECRET_KEY=x\nTBR_BIND_HOST=0.0.0.0\n# TBR_DB_HOST=db.example.internal\n"
             "TBR_BACKUP_DIR=/var/backups/taskbrigade\n"
             "# replaced by TBR_DB_NAME (name before TaskBrigade 0.8): TM_DB_NAME=a\nTBR_DB_NAME=b\n"
             "export TBR_PORT=1\n")


def _bash(script: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["bash", "-c", script], cwd=ROOT, capture_output=True, text=True,
                          env={**os.environ, "TBR_PY": sys.executable})


def test_native_env_file_rename_is_idempotent_and_backed_up(tmp_path):
    env = tmp_path / "taskbrigade.env"
    env.write_text(ENV_BEFORE)
    env.chmod(0o640)
    call = f"source deploy/prereq.sh; tm_env_rename_legacy {env} /var/backups/taskmeister=/var/backups/taskbrigade"
    r = _bash(f"{call}; {call}")
    assert r.returncode == 0, r.stderr
    assert env.read_text() == ENV_AFTER.replace("/var/backups/taskbrigade\n", "/var/backups/taskbrigade\n")
    assert env.stat().st_mode & 0o777 == 0o640
    backups = list(tmp_path.glob("taskbrigade.env.bak-*"))
    assert len(backups) == 1 and backups[0].read_text() == ENV_BEFORE


def test_docker_env_rename_without_python(tmp_path):
    env = tmp_path / ".env"
    env.write_text(ENV_BEFORE.replace("TM_BACKUP_DIR=/var/backups/taskmeister", "TM_BACKUP_DIR=/srv/b"))
    env.chmod(0o600)
    r = _bash(f"source deploy/docker/common.sh; tm_env_migrate {env}; tm_env_migrate {env}; "
              f"tm_env_default {env} TBR_DB_NAME taskmeister; tm_env_default {env} TBR_DB_USER taskmeister")
    assert r.returncode == 0, r.stderr
    expected = ENV_AFTER.replace("TBR_BACKUP_DIR=/var/backups/taskbrigade", "TBR_BACKUP_DIR=/srv/b")
    assert env.read_text() == expected + "TBR_DB_USER=taskmeister\n"
    assert env.stat().st_mode & 0o777 == 0o600


def test_update_scripts_run_the_migration_first():
    native = (ROOT / "deploy/update.sh").read_text()
    assert native.index("migrate-names.sh") < native.index("git -c safe.directory='*' -C \"$APP\" pull")
    assert 'exec env TBR_BASE="$new_base" "$new_base/app/deploy/update.sh"' in native
    docker = (ROOT / "deploy/docker/update.sh").read_text()
    assert docker.index("migrate-names.sh") < docker.index("dc pull")
    install = (ROOT / "deploy/install.sh").read_text()
    assert "Do not install a second one next to it" in install
    for script in ("deploy/migrate-names.sh", "deploy/docker/migrate-names.sh"):
        path = ROOT / script
        assert os.access(path, os.X_OK)
        assert subprocess.run(["bash", "-n", str(path)], capture_output=True).returncode == 0
    # The way back keeps the old Docker volumes: they are copied, never removed by the script.
    assert "docker volume rm" not in "\n".join(
        line for line in (ROOT / "deploy/docker/migrate-names.sh").read_text().splitlines() if not line.startswith("echo"))
