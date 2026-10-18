"""Against real MariaDB (`TBR_TEST_DATABASE_URL` must point to a *_test database)."""
import pytest
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.engine import make_url

from app.db import db
from app.models import Base
from tests import conftest
from tests.conftest import TEST_DB_URL, csrf_of, login, requires_db

pytestmark = [requires_db, pytest.mark.db]


@pytest.fixture
def empty_test_db():
    engine = create_engine(TEST_DB_URL)
    with engine.begin() as conn:
        conn.execute(text("SET FOREIGN_KEY_CHECKS = 0"))
        for table in inspect(conn).get_table_names():
            conn.execute(text(f"DROP TABLE `{table}`"))
        conn.execute(text("SET FOREIGN_KEY_CHECKS = 1"))
    conftest._schema_ready = False  # ready_db must recreate the schema.
    yield engine
    engine.dispose()


def test_acceptance_setup_without_restart(client, empty_test_db):
    """Setup acceptance: fresh install → UI password → schema upgrade → fully functional."""
    url = make_url(TEST_DB_URL)
    assert client.get("/healthz").status_code == 503
    login(client)
    csrf = csrf_of(client)
    form = {"host": url.host, "port": str(url.port or 3306), "database": url.database,
            "user": url.username, "password": url.password}

    r = client.post("/admin/database/test", data=form, headers={"X-CSRF-Token": csrf})
    assert "✅ Verbindung erfolgreich" in r.text and "Speichern" in r.text

    r = client.post("/admin/database/save", data=form, headers={"X-CSRF-Token": csrf})
    assert "Gespeichert" in r.text and "Schema aktualisieren" in r.text
    assert not db.ready  # Connected, but schema is empty.

    r = client.post("/admin/database/upgrade", headers={"X-CSRF-Token": csrf})
    from app.db import alembic_head
    assert f"Schema auf {alembic_head()} aktualisiert" in r.text
    assert db.ready

    assert client.get("/healthz").status_code == 200
    assert client.get("/", follow_redirects=False).status_code == 200
    assert client.get("/api/v1/projects").status_code != 503


def test_schema_options_and_seed(empty_test_db):
    db.configure(TEST_DB_URL)
    from app.db import upgrade_schema

    upgrade_schema(db.engine)
    with empty_test_db.connect() as conn:
        rows = conn.execute(text(
            "SELECT table_name, engine, table_collation FROM information_schema.tables "
            "WHERE table_schema = DATABASE() AND table_name <> 'alembic_version'")).all()
        assert {r.table_name for r in rows} == set(Base.metadata.tables)
        assert all(r.engine == "InnoDB" and r.table_collation == "utf8mb4_unicode_ci" for r in rows)
        actors = conn.execute(text("SELECT name, actor_type, role FROM actors")).all()
        assert [tuple(a) for a in actors] == [("admin", "human", "admin")]  # No sample agents on a fresh install.
        with pytest.raises(Exception, match="chk_tasks_prio"):  # CHECK is enforced (MariaDB ≥ 10.2).
            conn.execute(text("INSERT INTO projects (project_key, name, created_by, owner_id) VALUES ('T', 't', 1, 1)"))
            conn.execute(text("INSERT INTO tasks (task_nr, project_id, title, prio, created_by) "
                              "VALUES ('T-0001', LAST_INSERT_ID(), 'x', 9, 1)"))
        conn.rollback()


def test_existing_actors_survive_upgrade(empty_test_db):
    """An installation migrated with the earlier agent seed keeps those agents unchanged up to head."""
    from alembic import command

    from app.db import alembic_config

    cfg = alembic_config()
    query = text("SELECT name, display_name, actor_type, agent_kind, runtime, role FROM actors ORDER BY name")
    with empty_test_db.begin() as conn:
        cfg.attributes["connection"] = conn
        command.upgrade(cfg, "0001")
        conn.execute(text("INSERT INTO actors (name, display_name, actor_type, agent_kind, runtime, role) VALUES "
                          "('codex', 'Codex', 'agent', 'codex', 'cloud', 'worker'), "
                          "('opencode', 'OpenCode', 'agent', 'opencode', 'local', 'worker')"))
        before = conn.execute(query).all()
        command.upgrade(cfg, "head")
        assert conn.execute(query).all() == before


def test_output_budget_upgrade_counts_historical_releases(empty_test_db):
    from alembic import command

    from app.db import alembic_config

    cfg = alembic_config()
    with empty_test_db.begin() as conn:
        cfg.attributes["connection"] = conn
        command.upgrade(cfg, "0037")
        conn.execute(text("INSERT INTO projects (project_key, name, created_by, owner_id) VALUES ('T', 't', 1, 1)"))
        conn.execute(text("INSERT INTO datasets (dataset_key, project_id, name, uploaded_by) "
                          "VALUES ('DS-0001', 1, 'sample', 1), ('DS-0002', 1, 'other', 1)"))
        conn.execute(text("INSERT INTO tasks (task_nr, project_id, title, created_by) VALUES ('T-0001', 1, 't', 1)"))
        conn.execute(text("INSERT INTO run_results (task_id, dataset_id, job_id, status, released_at) VALUES "
                          "(1, 1, 'released', 'released', NULL), "
                          "(1, 1, 'deleted', 'deleted', '2026-01-01 00:00:00'), "
                          "(1, 1, 'sealed', 'kept_sealed', NULL), "
                          "(1, 1, 'pending', 'pending_approval', NULL)"))
        command.upgrade(cfg, "0038")
        query = text("SELECT released_count, epsilon_spent, gate_policy_json FROM datasets ORDER BY id")
        rows = conn.execute(query).all()
        assert [tuple(r) for r in rows] == [(2, 0, None), (0, 0, None)]
        assert conn.scalar(text("SELECT COUNT(*) FROM run_results WHERE release_policy_json IS NOT NULL")) == 0
        command.downgrade(cfg, "0037")
        assert conn.scalar(text("SELECT COUNT(*) FROM run_results")) == 4
        command.upgrade(cfg, "0038")
        assert conn.execute(query).all() == rows


def test_existing_tasks_survive_sprint_migration(empty_test_db):
    """Migration 0008 on an existing installation: tasks keep their data and get no sprint; downgrade works."""
    from alembic import command

    from app.db import alembic_config

    cfg = alembic_config()
    query = text("SELECT task_nr, status, title FROM tasks ORDER BY task_nr")
    with empty_test_db.begin() as conn:
        cfg.attributes["connection"] = conn
        command.upgrade(cfg, "0007")
        conn.execute(text("INSERT INTO projects (project_key, name, created_by, owner_id) VALUES ('T', 't', 1, 1)"))
        conn.execute(text("INSERT INTO tasks (task_nr, project_id, title, status, created_by) VALUES "
                          "('T-0001', LAST_INSERT_ID(), 'alt', 'in_progress', 1)"))
        before = conn.execute(query).all()
        command.upgrade(cfg, "0008")
        assert conn.execute(query).all() == before
        assert conn.execute(text("SELECT sprint_id FROM tasks")).scalar_one() is None
        command.downgrade(cfg, "0007")
        assert "sprints" not in inspect(conn).get_table_names()
        command.upgrade(cfg, "head")


def test_parallel_default_migration(empty_test_db):
    """Migration 0009: agents on the old default 1 get 2, raised limits and humans stay; downgrade works."""
    from alembic import command

    from app.db import alembic_config

    cfg = alembic_config()
    query = text("SELECT name, max_parallel_tasks FROM actors ORDER BY name")
    with empty_test_db.begin() as conn:
        cfg.attributes["connection"] = conn
        command.upgrade(cfg, "0008")
        conn.execute(text("INSERT INTO actors (name, display_name, actor_type, max_parallel_tasks) VALUES "
                          "('a-one', 'A', 'agent', 1), ('a-four', 'B', 'agent', 4), ('alex', 'Alex', 'human', 1)"))
        command.upgrade(cfg, "0009")
        assert conn.execute(query).all() == [("a-four", 4), ("a-one", 2), ("admin", 1), ("alex", 1)]
        conn.execute(text("INSERT INTO actors (name, display_name, actor_type) VALUES ('fresh', 'F', 'agent')"))
        assert conn.execute(text("SELECT max_parallel_tasks FROM actors WHERE name = 'fresh'")).scalar() == 2
        assert {"follow_up_of_id", "open_points"} <= {c["name"] for c in inspect(conn).get_columns("tasks")}
        command.downgrade(cfg, "0008")
        assert "open_points" not in {c["name"] for c in inspect(conn).get_columns("tasks")}
        command.upgrade(cfg, "head")


def test_engine_rebuild_at_runtime(empty_test_db):
    db.configure(TEST_DB_URL)
    first = db.engine
    db.configure(TEST_DB_URL)
    assert db.engine is not first and db.last_probe.ok
    with db.engine.connect() as conn:
        assert conn.execute(text("SELECT @@session.time_zone")).scalar() == "+00:00"
