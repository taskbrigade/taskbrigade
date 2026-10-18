"""Workflow schema foundation against real MariaDB; execution logic belongs to WF 2."""
import pytest
from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
from alembic.script import ScriptDirectory
from sqlalchemy import delete, inspect, select, text
from sqlalchemy.exc import IntegrityError

from app.db import alembic_config
from app.models import (
    TASK_KINDS, Actor, Base, Project, Task, WorkflowParam, WorkflowRoleBinding,
    WorkflowRun, WorkflowRunStep, WorkflowStep, WorkflowTemplate, WorkflowTemplateVersion,
)
from tests.conftest import requires_db
from tests.test_db_integration import empty_test_db  # noqa: F401

WORKFLOW_TABLES = {
    "workflow_templates", "workflow_template_versions", "workflow_steps", "workflow_params",
    "workflow_role_bindings", "workflow_runs", "workflow_run_steps",
}

SCHEDULE_TABLES = {"workflow_schedules", "workflow_schedule_fires", "workflow_calendars", "workflow_calendar_days"}
EVENT_TABLES = {"workflow_triggers", "workflow_events", "workflow_trigger_fires"}


def test_single_linear_migration_head():
    script = ScriptDirectory.from_config(alembic_config())
    assert len(script.get_heads()) == 1
    revisions = list(script.walk_revisions())
    assert all(not isinstance(r.down_revision, tuple) for r in revisions)


@pytest.fixture
def graph(ready_db):
    with ready_db.session() as s:
        owner = s.scalar(select(Actor).where(Actor.name == "admin"))
        project = Project(project_key="DEMO", name="Demo", owner_id=owner.id, created_by=owner.id)
        s.add(project)
        s.flush()
        template = WorkflowTemplate(project=project, workflow_key="content", name="Content",
                                    owner_id=owner.id, created_by=owner.id)
        s.add(template)
        s.flush()
        version = WorkflowTemplateVersion(template=template, version=1, state="published", created_by=owner.id)
        s.add(version)
        s.flush()
        template.current_version = version
        step = WorkflowStep(version=version, position=1, step_key="write", step_type="agent",
                            title_tpl="Write {{params.topic}}", agent_role="writer")
        param = WorkflowParam(version=version, name="topic", param_type="string", label="Topic")
        binding = WorkflowRoleBinding(project=project, role="writer", actor=owner)
        run = WorkflowRun(template=template, version=version, project=project, run_seq=1,
                          trigger_kind="manual", params_json='{"topic":"Workflows"}', status="running")
        s.add_all([step, param, binding, run])
        s.flush()
        run_step = WorkflowRunStep(run=run, step=step, position=1, status="active")
        s.add(run_step)
        s.flush()
        task = Task(project_id=project.id, task_nr="DEMO-0001", title="Write Workflows",
                    created_by=owner.id, workflow_run_step=run_step)
        s.add(task)
        s.flush()
        run_step.task = task
        s.commit()
        yield s, template, version, step, param, binding, run, run_step, task


@pytest.mark.db
def test_workflow_relations_and_defaults(graph):
    s, template, version, step, param, binding, run, run_step, task = graph
    s.expire_all()
    assert template.current_version is version
    assert template.versions == [version]
    assert version.template is template and version.steps == [step] and version.params == [param]
    assert template.status == "active" and template.default_budget_tokens is None
    assert template.owner is binding.actor and template.project is binding.project
    assert step.assignee is None and step.agent_role == "writer" and step.data_class is None
    assert step.review_mode == "manual" and step.max_retries == 0 and step.retry_backoff_s == 300
    assert not step.optional and param.required and binding.rank == 1
    assert run.template is template and run.version is version and run.project is template.project
    assert run.used_tokens == 0 and run.used_cents == 0 and run.started_by is None
    assert run.steps == [run_step] and run_step.run is run and run_step.step is step
    assert run_step.attempt == 1 and run_step.task is task and task.workflow_run_step is run_step
    assert task.task_kind == "work" and task.merge_commit_sha is None and task.sprint_id is None
    assert template.created_at and template.updated_at and version.created_at and run.created_at
    assert WORKFLOW_TABLES <= Base.metadata.tables.keys()


@pytest.mark.db
def test_migrated_workflow_schema_matches_models(ready_db):
    def include_object(obj, name, type_, reflected, compare_to):
        return type_ != "table" or name in WORKFLOW_TABLES | SCHEDULE_TABLES | EVENT_TABLES | {"tasks", "workflow_script_pins"}

    with ready_db.engine.connect() as conn:
        context = MigrationContext.configure(conn, opts={"include_object": include_object, "compare_type": True})
        assert compare_metadata(context, Base.metadata) == []


@pytest.mark.db
def test_runs_keep_their_version_when_current_changes(graph):
    s, template, version, step, param, binding, run, run_step, task = graph
    new_version = WorkflowTemplateVersion(template=template, version=2, created_by=template.created_by)
    s.add(new_version)
    s.flush()
    template.current_version = new_version
    s.commit()
    s.expire_all()
    assert template.current_version.version == 2
    assert run.version is version and run.version.version == 1
    assert run_step.step.version is version
    # Retirement keeps history; deleting a pinned version or instantiated step must fail.
    version.state = "retired"
    s.commit()
    for obj in (version, step, template, task):
        with pytest.raises(IntegrityError):
            with s.begin_nested():
                s.delete(obj)
                s.flush()
    assert s.get(WorkflowRun, run.id).version_id == version.id


@pytest.mark.db
def test_unused_version_cascades_steps_and_params(graph):
    s, template, version, step, param, binding, run, run_step, task = graph
    draft = WorkflowTemplateVersion(template_id=template.id, version=2, created_by=template.created_by)
    s.add(draft)
    s.flush()
    spare = WorkflowStep(version=draft, position=1, step_key="notify", step_type="notify", title_tpl="Done")
    spare_param = WorkflowParam(version=draft, name="topic", param_type="text", label="Topic")
    s.add_all([spare, spare_param])
    s.commit()
    step_id, param_id = spare.id, spare_param.id
    s.execute(delete(WorkflowTemplateVersion).where(WorkflowTemplateVersion.id == draft.id))
    s.commit()
    s.expunge_all()
    assert s.get(WorkflowStep, step_id) is None and s.get(WorkflowParam, param_id) is None
    assert s.get(WorkflowTemplateVersion, version.id) is not None


@pytest.mark.db
@pytest.mark.parametrize("duplicate", ["template", "version", "step_key", "position", "param", "binding", "run", "attempt"])
def test_workflow_unique_constraints(graph, duplicate):
    s, template, version, step, param, binding, run, run_step, task = graph
    objects = {
        "template": WorkflowTemplate(project_id=template.project_id, workflow_key=template.workflow_key,
                                     name="Duplicate", owner_id=template.owner_id, created_by=template.created_by),
        "version": WorkflowTemplateVersion(template_id=template.id, version=1, created_by=template.created_by),
        "step_key": WorkflowStep(version_id=version.id, step_key="write", position=2,
                                 step_type="agent", title_tpl="Duplicate key"),
        "position": WorkflowStep(version_id=version.id, step_key="second", position=1,
                                 step_type="notify", title_tpl="Duplicate position"),
        "param": WorkflowParam(version_id=version.id, name="topic", param_type="text", label="Duplicate"),
        "binding": WorkflowRoleBinding(project_id=binding.project_id, role=binding.role, actor_id=binding.actor_id),
        "run": WorkflowRun(template_id=template.id, version_id=version.id, project_id=template.project_id,
                           run_seq=1, trigger_kind="api", params_json="{}", status="queued"),
        "attempt": WorkflowRunStep(run_id=run.id, step_id=step.id, position=1, attempt=1, status="pending"),
    }
    with pytest.raises(IntegrityError):
        with s.begin_nested():
            s.add(objects[duplicate])
            s.flush()


@pytest.mark.db
def test_retry_attempt_and_actor_foreign_key(graph):
    s, template, version, step, param, binding, run, run_step, task = graph
    retry = WorkflowRunStep(run=run, step=step, position=1, attempt=2, status="pending")
    s.add(retry)
    s.commit()
    assert [row.attempt for row in run.steps] == [1, 2]
    with pytest.raises(IntegrityError):
        with s.begin_nested():
            s.execute(delete(Actor).where(Actor.id == binding.actor_id))
    with pytest.raises(IntegrityError):
        with s.begin_nested():
            s.add(WorkflowRoleBinding(project_id=template.project_id, role="missing", actor_id=0))
            s.flush()


@pytest.mark.db
@requires_db
def test_workflow_migration_roundtrip_preserves_existing_tasks(empty_test_db):
    cfg = alembic_config()
    script = ScriptDirectory.from_config(cfg)
    revision = next(r for r in script.walk_revisions() if r.module.__file__.endswith("_workflows.py"))
    with empty_test_db.begin() as conn:
        cfg.attributes["connection"] = conn
        command.upgrade(cfg, revision.down_revision)
        conn.execute(text("INSERT INTO projects (project_key, name, created_by, owner_id) VALUES ('DEMO', 'Demo', 1, 1)"))
        conn.execute(text("INSERT INTO tasks (task_nr, project_id, title, status, created_by, result_commit_sha) "
                          "VALUES ('DEMO-0001', LAST_INSERT_ID(), 'Existing', 'done', 1, :sha)"), {"sha": "a" * 40})
        query = text("SELECT task_nr, title, status, result_commit_sha FROM tasks")
        before = conn.execute(query).all()
        command.upgrade(cfg, revision.revision)
        assert conn.execute(query).all() == before
        assert conn.execute(text("SELECT task_kind, workflow_run_step_id, merge_commit_sha FROM tasks")).one() == (
            "work", None, None)
        assert WORKFLOW_TABLES <= set(inspect(conn).get_table_names())
        for kind in TASK_KINDS:
            conn.execute(text("UPDATE tasks SET task_kind=:kind"), {"kind": kind})
            assert conn.execute(text("SELECT task_kind FROM tasks")).scalar_one() == kind
        # A schema downgrade must remove the cyclic FKs even with a current version and a linked task.
        conn.execute(text("INSERT INTO workflow_templates (project_id, workflow_key, name, owner_id, created_by) "
                          "VALUES (1, 'content', 'Content', 1, 1)"))
        conn.execute(text("INSERT INTO workflow_template_versions (template_id, version, created_by) VALUES (1, 1, 1)"))
        conn.execute(text("UPDATE workflow_templates SET current_version_id=1"))
        conn.execute(text("INSERT INTO workflow_steps (version_id, position, step_key, step_type, title_tpl) "
                          "VALUES (1, 1, 'write', 'agent', 'Write')"))
        conn.execute(text("INSERT INTO workflow_runs (template_id, version_id, project_id, run_seq, trigger_kind, "
                          "params_json, status) VALUES (1, 1, 1, 1, 'manual', '{}', 'running')"))
        conn.execute(text("INSERT INTO workflow_run_steps (run_id, step_id, position, status, task_id) "
                          "VALUES (1, 1, 1, 'active', 1)"))
        conn.execute(text("UPDATE tasks SET workflow_run_step_id=1"))
        command.downgrade(cfg, revision.down_revision)
        assert not WORKFLOW_TABLES.intersection(inspect(conn).get_table_names())
        assert not {"task_kind", "workflow_run_step_id", "merge_commit_sha"}.intersection(
            c["name"] for c in inspect(conn).get_columns("tasks"))
        assert conn.execute(query).all() == before
        command.upgrade(cfg, "head")
        assert conn.execute(query).all() == before


@pytest.mark.db
def test_scheduler_migration_roundtrip(graph, ready_db):
    """The scheduler FK cycle is reversible, and old manual runs remain valid after upgrade."""
    from app.db import upgrade_schema

    cfg = alembic_config()
    query = text("SELECT id, status, params_json FROM workflow_runs ORDER BY id")
    with ready_db.engine.begin() as connection:
        before = connection.execute(query).all()
        assert before
        cfg.attributes["connection"] = connection
        command.downgrade(cfg, "0025")
        names = set(inspect(connection).get_table_names())
        assert "workflow_schedules" not in names and "workflow_runs" in names
        assert connection.execute(query).all() == before
        command.upgrade(cfg, "head")
        assert connection.execute(query).all() == before
    upgrade_schema(ready_db.engine)
    assert "workflow_schedules" in inspect(ready_db.engine).get_table_names()
