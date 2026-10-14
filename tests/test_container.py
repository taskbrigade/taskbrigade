"""Container start (app/container.py): wait for the DB, migrate, initial admin, DB from env, trusted peers."""
import json

import pytest
from fastapi.testclient import TestClient

from app import container, crypto, dbconfig
from app.db import ProbeResult
from tests.conftest import TEST_DB_URL, csrf_of, login, requires_db


class FakeClock:
    def __init__(self):
        self.now = 0.0
        self.sleeps: list[float] = []

    def __call__(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds


def _probes(*results):
    calls = iter(results)
    return lambda _engine: next(calls)


DOWN = ProbeResult(False, "2003: Can't connect to MySQL server")
UP_EMPTY = ProbeResult(True, None, "11.4.3-MariaDB", True, None, "0005")
UP_CURRENT = ProbeResult(True, None, "11.4.3-MariaDB", True, "0005", "0005")


# --- waiting for the database ------------------------------------------------------------

def test_wait_for_db_retries_until_reachable():
    clock = FakeClock()
    result = container.wait_for_db(object(), timeout=30, interval=2, probe_fn=_probes(DOWN, DOWN, UP_EMPTY),
                                   sleep=clock.sleep, clock=clock)
    assert result is UP_EMPTY
    assert clock.sleeps == [2, 2]


def test_wait_for_db_gives_up_after_timeout():
    clock = FakeClock()
    with pytest.raises(container.StartupError, match="after 5 s: 2003"):
        container.wait_for_db(object(), timeout=5, interval=2, probe_fn=lambda _e: DOWN,
                              sleep=clock.sleep, clock=clock)
    assert clock.sleeps == [2, 2, 2]


def test_wait_for_db_returns_immediately_when_up():
    clock = FakeClock()
    container.wait_for_db(object(), timeout=0, probe_fn=_probes(UP_CURRENT), sleep=clock.sleep, clock=clock)
    assert clock.sleeps == []


# --- migrations ----------------------------------------------------------------------------

def test_migrate_runs_upgrade_on_empty_schema(monkeypatch):
    calls = []
    monkeypatch.setattr(container, "upgrade_schema", lambda engine: calls.append(engine) or "0005")
    assert container.migrate("engine", UP_EMPTY) == "0005"
    assert calls == ["engine"]


def test_migrate_skips_current_schema(monkeypatch):
    monkeypatch.setattr(container, "upgrade_schema", lambda engine: pytest.fail("must not migrate"))
    assert container.migrate("engine", UP_CURRENT) == "0005"


def test_prepare_waits_then_migrates_then_seeds(monkeypatch, settings, tmp_path):
    monkeypatch.setattr(settings, "tbr_state_dir", tmp_path)
    monkeypatch.setattr(settings, "tbr_db_password", "pw")
    monkeypatch.setattr(settings, "secret_key", "fixed")
    order = []

    class Engine:
        def dispose(self):
            order.append("dispose")

    monkeypatch.setattr(container, "make_engine", lambda url: order.append(("engine", url)) or Engine())
    monkeypatch.setattr(container, "wait_for_db", lambda engine, timeout: order.append("wait") or UP_EMPTY)
    monkeypatch.setattr(container, "migrate", lambda engine, before: order.append(("migrate", before)))
    monkeypatch.setattr(container, "ensure_initial_admin", lambda engine, s: order.append("admin"))
    container.prepare(settings, {})
    assert order == [("engine", dbconfig.load(settings).url()), "wait", ("migrate", UP_EMPTY), "admin", "dispose"]
    assert crypto.key_path(tmp_path).exists()


def test_prepare_disposes_engine_on_failure(monkeypatch, settings, tmp_path):
    monkeypatch.setattr(settings, "tbr_state_dir", tmp_path)
    monkeypatch.setattr(settings, "tbr_db_password", "pw")
    disposed = []

    class Engine:
        def dispose(self):
            disposed.append(True)

    def fail(engine, timeout):
        raise container.StartupError("down")

    monkeypatch.setattr(container, "make_engine", lambda url: Engine())
    monkeypatch.setattr(container, "wait_for_db", fail)
    with pytest.raises(container.StartupError):
        container.prepare(settings, {})
    assert disposed == [True]


def test_main_exits_without_db_password(monkeypatch, settings, tmp_path):
    monkeypatch.setattr(settings, "tbr_state_dir", tmp_path)
    monkeypatch.setattr(container.os, "execvp", lambda *a: pytest.fail("must not start uvicorn"))
    with pytest.raises(SystemExit) as exc:
        container.main()
    assert exc.value.code == 1


def test_uvicorn_keeps_no_proxy_headers():
    argv = container.uvicorn_argv({"TBR_PORT": "9000"})
    assert argv[:2] == ["uvicorn", "app.main:app"]
    assert "--no-proxy-headers" in argv and argv[argv.index("--port") + 1] == "9000"
    assert argv[argv.index("--host") + 1] == "0.0.0.0" and argv[argv.index("--workers") + 1] == "1"


# --- state: secret key and DB connection from the environment ------------------------------

def test_secret_key_persisted_in_state_dir(monkeypatch, settings, tmp_path):
    monkeypatch.setattr(settings, "tbr_state_dir", tmp_path)
    monkeypatch.setattr(settings, "secret_key", "")
    env1, env2 = {}, {}
    assert container.ensure_secret_key(settings, env1) is True
    assert container.ensure_secret_key(settings, env2) is False
    assert env1["SECRET_KEY"] == env2["SECRET_KEY"] and len(env1["SECRET_KEY"]) > 40
    assert (tmp_path / container.SECRET_FILE).stat().st_mode & 0o777 == 0o600


def test_explicit_secret_key_wins(monkeypatch, settings, tmp_path):
    monkeypatch.setattr(settings, "tbr_state_dir", tmp_path)
    env = {}
    assert container.ensure_secret_key(settings, env) is False
    assert env == {} and not (tmp_path / container.SECRET_FILE).exists()


def test_database_url_requires_password(monkeypatch, settings):
    with pytest.raises(container.StartupError, match="TBR_DB_PASSWORD"):
        container.database_url(settings)
    monkeypatch.setattr(settings, "database_url", "mariadb+pymysql://u:p@h/x_test")
    assert container.database_url(settings) == "mariadb+pymysql://u:p@h/x_test"


def test_env_password_overrides_db_json(monkeypatch, settings, state_dir):
    (state_dir / "db.json").write_text(json.dumps({"host": "db.example.internal", "port": 3307,
                                                   "database": "old", "user": "old", "password_enc": None}))
    assert dbconfig.load(settings).host == "db.example.internal"
    monkeypatch.setattr(settings, "tbr_db_host", "mariadb")
    monkeypatch.setattr(settings, "tbr_db_port", 3306)
    monkeypatch.setattr(settings, "tbr_db_password", "p@ss/word")
    cfg = dbconfig.load(settings)
    assert (cfg.host, cfg.port, cfg.database, cfg.password) == ("mariadb", 3306, "taskbrigade", "p@ss/word")
    assert container.database_url(settings).startswith("mariadb+pymysql://taskadmin:p%40ss%2Fword@mariadb:3306/")


def test_env_mode_leaves_setup_mode(monkeypatch, settings):
    """Without TBR_DB_PASSWORD the app stays in setup mode; with it, it connects without db.json."""
    from app.db import db

    first = db.init_from_settings(settings)
    assert db.engine is None and not first.ok
    monkeypatch.setattr(settings, "tbr_db_password", "pw")
    monkeypatch.setattr(settings, "tbr_db_port", 1)
    result = db.init_from_settings(settings)
    assert db.engine is not None and not result.ok  # It tried to connect instead of waiting for setup.


def test_ui_does_not_save_db_json_in_env_mode(client, monkeypatch, settings, state_dir):
    login(client)
    csrf = csrf_of(client)
    monkeypatch.setattr(settings, "tbr_db_password", "pw")
    form = {"host": "127.0.0.1", "port": "1", "database": "x", "user": "u", "password": "pw"}
    r = client.post("/admin/database/save", data=form, headers={"X-CSRF-Token": csrf})
    assert "TBR_DB_PASSWORD" in r.text
    assert not (state_dir / "db.json").exists()


# --- trusted peers (Docker bridge gateway) ------------------------------------------------

@pytest.fixture
def enforce(monkeypatch, settings):
    monkeypatch.setattr(settings, "enforce_client_networks", True)
    monkeypatch.setattr(settings, "trusted_peer_networks", "172.31.250.1/32")
    return settings


def _get(ip: str, path: str, **headers):
    from app.main import app

    with TestClient(app, client=(ip, 50000)) as c:
        return c.get(path, headers=headers, follow_redirects=False)


def test_trusted_peer_gets_ui_and_api(enforce):
    assert _get("172.31.250.1", "/login").status_code == 200
    assert _get("172.31.250.1", "/api/v1/agents/me").status_code == 503  # Setup mode, but not blocked.
    assert _get("172.31.250.3", "/login").status_code == 403  # Other containers on the network stay blocked.


def test_trusted_peer_may_send_tailscale_login(enforce, monkeypatch):
    monkeypatch.setattr(enforce, "ui_allowed_logins", "admin@test")
    hdr = {"Tailscale-User-Login": "admin@test"}
    assert _get("172.31.250.1", "/admin/database", **hdr).status_code == 200
    monkeypatch.setattr(enforce, "trusted_peer_networks", "")
    assert _get("172.31.250.1", "/admin/database", **hdr).status_code == 403


@pytest.mark.parametrize("host,trusted", [("127.0.0.1", True), ("::1", True), ("172.31.250.1", True),
                                          ("172.31.250.2", False), ("testclient", False), ("", False)])
def test_is_trusted_peer(enforce, host, trusted):
    assert enforce.is_trusted_peer(host) is trusted


# --- initial admin (real MariaDB) ---------------------------------------------------------

@requires_db
def test_initial_admin_password_set_once(ready_db, monkeypatch, settings):
    from sqlalchemy import create_engine, select

    from app.auth import verify_password
    from app.models import Actor

    engine = create_engine(TEST_DB_URL)

    def admin_hash():
        with engine.connect() as conn:
            return conn.execute(select(Actor.password_hash).where(Actor.name == "admin")).scalar()

    with engine.begin() as conn:
        conn.execute(Actor.__table__.update().where(Actor.name == "admin").values(password_hash=None))
    assert container.ensure_initial_admin(engine, settings) is False  # No TBR_INITIAL_ADMIN_PASSWORD.
    monkeypatch.setattr(settings, "tbr_initial_admin_password", "first-password-123")
    assert container.ensure_initial_admin(engine, settings) is True
    assert verify_password(admin_hash(), "first-password-123")
    monkeypatch.setattr(settings, "tbr_initial_admin_password", "second-password-123")
    assert container.ensure_initial_admin(engine, settings) is False  # Never overwrites a set password.
    assert verify_password(admin_hash(), "first-password-123")
    engine.dispose()


@requires_db
def test_initial_admin_password_too_short(ready_db, monkeypatch, settings):
    from sqlalchemy import create_engine

    from app.models import Actor

    engine = create_engine(TEST_DB_URL)
    with engine.begin() as conn:
        conn.execute(Actor.__table__.update().where(Actor.name == "admin").values(password_hash=None))
    monkeypatch.setattr(settings, "tbr_initial_admin_password", "short")
    with pytest.raises(container.StartupError, match="TBR_INITIAL_ADMIN_PASSWORD"):
        container.ensure_initial_admin(engine, settings)
    engine.dispose()


@requires_db
def test_migrate_against_real_db_is_idempotent(ready_db):
    from sqlalchemy import create_engine

    from app.db import probe

    engine = create_engine(TEST_DB_URL)
    before = probe(engine)
    assert before.schema_current
    assert container.migrate(engine, before) == before.head_revision
    engine.dispose()
