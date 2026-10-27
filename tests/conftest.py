import os
import tempfile
from pathlib import Path

import pytest

# Before importing the app: isolated state directory, no scheduler or real DB.
_STATE = Path(tempfile.mkdtemp(prefix="tm-test-state-"))
os.environ["TBR_STATE_DIR"] = str(_STATE)
os.environ["SCHEDULER_ENABLED"] = "false"
os.environ["SECRET_KEY"] = "test-secret"
os.environ["ENFORCE_CLIENT_NETWORKS"] = "false"  # TestClient reports "testclient"; another test checks the rule.
# Use German for UI tests; tests/test_i18n.py checks both languages.
os.environ["UI_LANGUAGE"] = "de"
# Agent-facing CLI texts (cli/i18n.py): English default, independent of a local .taskbrigade.toml
os.environ["TBR_LANGUAGE"] = "en"
os.environ.pop("DATABASE_URL", None)

from app.auth import hash_password  # noqa: E402

BOOTSTRAP_PW = "bootstrap-test-pw"
os.environ["BOOTSTRAP_ADMIN_PASSWORD_HASH"] = hash_password(BOOTSTRAP_PW)

from app import crypto  # noqa: E402
from app.config import get_settings  # noqa: E402

get_settings.cache_clear()
crypto.create_master_key(_STATE)


@pytest.fixture
def settings():
    return get_settings()


@pytest.fixture
def state_dir():
    return _STATE


@pytest.fixture(autouse=True)
def _reset_state():
    from app.api.deps import api_limiter
    from app.auth import login_limiter
    from app.db import ProbeResult, db
    from app.i18n import clear_default_cache

    clear_default_cache()
    (_STATE / "db.json").unlink(missing_ok=True)
    login_limiter._fails.clear()
    api_limiter._fails.clear()
    db.dispose()
    db.engine, db._sessionmaker = None, None
    db.last_probe = ProbeResult(False, "test")
    yield


@pytest.fixture
def client():
    from fastapi.testclient import TestClient

    from app.main import app

    with TestClient(app) as c:
        yield c


def login(client, password=BOOTSTRAP_PW):
    r = client.get("/login")
    csrf = r.text.split('name="csrf_token" value="')[1].split('"')[0]
    return client.post("/login", data={"username": "admin", "password": password, "csrf_token": csrf},
                       follow_redirects=False)


def csrf_of(client) -> str:
    r = client.get("/admin/database")
    return r.text.split('"X-CSRF-Token": "')[1].split('"')[0]


TEST_DB_URL = os.environ.get("TBR_TEST_DATABASE_URL") or os.environ.get("TM_TEST_DATABASE_URL")  # TM_: before 0.8


def _is_test_db(url: str) -> bool:
    return url.split("?")[0].rsplit("/", 1)[-1].endswith("_test")


requires_db = pytest.mark.skipif(not TEST_DB_URL, reason="TBR_TEST_DATABASE_URL nicht gesetzt")
if TEST_DB_URL and not _is_test_db(TEST_DB_URL):
    raise RuntimeError("TBR_TEST_DATABASE_URL muss auf eine *_test-Datenbank zeigen – niemals auf taskbrigade!")


# --- DB fixtures for tests against taskbrigade_test ------------------------------------

_schema_ready = False
# Migration 0001 only creates the bootstrap admin; the tests create their agents themselves (name → kind, runtime).
TEST_AGENTS = {"hermes": ("hermes", "local"), "openclaw-agent": ("openclaw", "local"), "codex": ("codex", "cloud"),
               "claude-code": ("claude_code", "cloud"), "opencode": ("opencode", "local")}
SEED = ["admin", *TEST_AGENTS]


def _create_test_agents(conn) -> None:
    from sqlalchemy import text

    for name, (kind, runtime) in TEST_AGENTS.items():
        # Limit 1 (not the default 2) like the reset below, so claim tests see the same limit in any order.
        # Public like every agent after migration 0018, so assignment tests need no owner.
        # Local agents run a local model, so `privacy_enforce_model_location` (default on) keeps the runtime rule.
        conn.execute(text("INSERT INTO actors (name, display_name, actor_type, agent_kind, runtime, model_location, "
                          "role, max_parallel_tasks, is_public) VALUES (:n, :n, 'agent', :k, :r, :r, 'worker', 1, 1)"),
                     {"n": name, "k": kind, "r": runtime})


@pytest.fixture
def ready_db(monkeypatch):
    """Create a fresh schema once per run, then reset data before each test."""
    global _schema_ready
    if not TEST_DB_URL:
        pytest.skip("TBR_TEST_DATABASE_URL nicht gesetzt")
    from sqlalchemy import create_engine, inspect, text

    from app.db import db, upgrade_schema

    engine = create_engine(TEST_DB_URL)
    with engine.begin() as conn:
        conn.execute(text("SET FOREIGN_KEY_CHECKS = 0"))
        if not _schema_ready:
            for table in inspect(conn).get_table_names():
                conn.execute(text(f"DROP TABLE `{table}`"))
        else:
            # Triggers refuse DELETE on data_audit (append-only); TRUNCATE does not fire them.
            conn.execute(text("TRUNCATE TABLE data_audit"))
            conn.execute(text("UPDATE data_audit_head SET last_id = 0, last_hash = REPEAT('0', 64)"))
            for table in ("workflow_trigger_fires", "workflow_script_pins", "workflow_schedule_fires", "workflow_schedules", "workflow_calendar_days", "workflow_calendars", "data_sftp_keys", "data_sftp_accounts", "task_data_refs", "dataset_profiles", "run_results", "datasets", "workflow_run_steps", "workflow_runs", "workflow_triggers", "workflow_events", "workflow_role_bindings", "workflow_params",
                          "workflow_steps", "workflow_template_versions", "workflow_templates",
                          "task_usage",
                          "outgoing_mails", "project_mail_recipients", "telegram_actions", "question_reads", "email_verify_codes", "notifications", "notification_prefs", "telegram_link_codes", "telegram_links",
                          "join_codes", "sprints", "task_attachments", "task_labels", "labels", "task_events",
                          "task_comments", "task_dependencies", "tasks", "project_owners", "project_members", "projects",
                          "app_settings"):
                conn.execute(text(f"DELETE FROM `{table}`"))
            names = ", ".join(f"'{n}'" for n in SEED)
            conn.execute(text(f"DELETE FROM actors WHERE name NOT IN ({names})"))
            conn.execute(text(
                "UPDATE actors SET api_key_hash=NULL, api_key_prefix=NULL, api_key_created_at=NULL, email=NULL, email_verified_at=NULL, "
                "password_hash=NULL, tailscale_login=NULL, gitea_username=NULL, gitea_token_enc=NULL, "
                "gitea_token_last4=NULL, gitea_token_checked_at=NULL, gitea_email=NULL, sftp_username=NULL, "
                "sftp_password_enc=NULL, sftp_password_last4=NULL, sftp_checked_at=NULL, is_active=1, "
                "last_seen_at=NULL, lease_minutes=120, max_parallel_tasks=1, notes=NULL, show_all_projects=0, "
                "show_all_agents=0, owner_id=NULL, data_access=0, is_public = (actor_type = 'agent'), "
                "role = IF(name='admin','admin','worker'), "
                "runtime = IF(name IN ('codex','claude-code'),'cloud','local'), "
                "model_location = IF(name IN ('codex','claude-code'),'cloud','local')"))
        conn.execute(text("SET FOREIGN_KEY_CHECKS = 1"))
    if not _schema_ready:
        upgrade_schema(engine)
        with engine.begin() as conn:
            _create_test_agents(conn)
        _schema_ready = True
    engine.dispose()
    monkeypatch.setattr(get_settings(), "database_url", TEST_DB_URL)  # App lifespan connects to the test DB.
    db.configure(TEST_DB_URL)
    assert db.ready
    yield db


def make_token(name: str, role: str | None = None) -> str:
    """Create an actor's API token directly through the service."""
    from app.db import db
    from app.services import actors

    with db.session() as s:
        actor = actors.get(s, name)
        if role:
            actor.role = role
        token = actors.rotate_token(s, actor)
        s.commit()
    return token


def bearer(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}
