"""WF 12: outbox events start each configured trigger once."""
import json

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select

from app.main import app
from app.models import (
    DataSftpAccount, Dataset, DatasetProfile, Task, WorkflowEvent, WorkflowParam, WorkflowRun,
    WorkflowStep, WorkflowTemplate, WorkflowTemplateVersion, WorkflowTrigger, WorkflowTriggerFire,
)
from app.runner.profiler import fingerprint
from app.services import dataset_profiles, settings_store
from app.services.workflows import engine, events, triggers
from app.services.workflows.errors import fail
from app.services.workflows.tick import tick
from tests.conftest import login
from tests.test_workflow_engine import graph, latest  # noqa: F401

pytestmark = pytest.mark.db


def _notify(s, owner, project_id, key):
    template = WorkflowTemplate(project_id=project_id, workflow_key=key, name=key,
                                owner_id=owner.id, created_by=owner.id)
    s.add(template)
    s.flush()
    version = WorkflowTemplateVersion(template_id=template.id, version=1, state="published", created_by=owner.id)
    s.add(version)
    s.flush()
    template.current_version = version
    s.add(WorkflowStep(version_id=version.id, position=1, step_key="inform", step_type="notify",
                       title_tpl="Done", config_json='{"to":["owner"],"text":"ok"}'))
    s.add(WorkflowParam(version_id=version.id, name="month", param_type="string", required=False, label="Month"))
    s.add(WorkflowParam(version_id=version.id, name="datensatz", param_type="dataset", required=False, label="Dataset"))
    s.add(WorkflowParam(version_id=version.id, name="label", param_type="string", required=False, label="Label"))
    s.flush()
    return template


def _runs(s, template):
    return list(s.scalars(select(WorkflowRun).where(WorkflowRun.template_id == template.id).order_by(WorkflowRun.id)))


def test_filename_date_shapes():
    assert events.filename_date("Buchungen_2026-09.xlsx") == "2026-09"
    assert events.filename_date("export_2026_09_15.csv") == "2026-09-15"
    assert events.filename_date("no-date.txt") is None


def test_dataset_and_workflow_end_start_once(graph, monkeypatch):
    s, owner, agent, other, template, steps = graph
    settings_store.update(s, {"workflows_enabled": "true"}, owner.id)
    child = _notify(s, owner, template.project_id, "follow")
    source = _notify(s, owner, template.project_id, "source")
    triggers.save(s, owner, child, {"kind": "dataset_ready", "enabled": True,
        "filter_json": '{"sftp_account":"buchhaltung-export","name_glob":"Buchungen_*.xlsx"}',
        "params_json": '{"month":"{{event.filename_date}}","datensatz":"{{event.dataset}}"}'})
    triggers.save(s, owner, child, {"kind": "workflow_succeeded", "enabled": True,
        "filter_json": '{"workflow":"DEMO/source"}',
        "params_json": '{"label":"{{event.run.params.label}}"}'})
    triggers.save(s, owner, child, {"kind": "workflow_failed", "enabled": True,
        "filter_json": '{"workflow":"content"}', "params_json": '{"label":"failed"}'})
    s.add(DataSftpAccount(username="buchhaltung-export", project_id=template.project_id, owner_id=owner.id))
    ds = Dataset(dataset_key="DS-0199", project_id=template.project_id, name="Buchungen",
                 original_filename="Buchungen_2026-09.xlsx", upload_channel="sftp", uploaded_by=owner.id,
                 status="ready", data_class="local_only")
    s.add(ds)
    s.flush()
    assert events.emit_dataset_ready(s, ds) is not None
    assert events.emit_dataset_ready(s, ds) is None
    s.commit()
    assert tick(s)["processed"] == 1
    started = _runs(s, child)
    assert len(started) == 1 and started[0].trigger_kind == "event" and started[0].started_by is None
    assert json.loads(started[0].params_json) == {"month": "2026-09", "datensatz": ds.id}
    assert tick(s)["processed"] == 1  # the child's own success event matches no trigger
    assert len(_runs(s, child)) == 1
    event = s.scalar(select(WorkflowEvent).where(WorkflowEvent.kind == "dataset_ready"))
    event.processed_at = None
    s.commit()
    tick(s)
    assert len(_runs(s, child)) == 1
    assert s.scalar(select(func.count()).select_from(WorkflowTriggerFire).where(
        WorkflowTriggerFire.outcome == "started")) == 1

    missed = Dataset(dataset_key="DS-0200", project_id=template.project_id, name="Other",
                     original_filename="Other.xlsx", upload_channel="web", uploaded_by=owner.id,
                     status="ready", data_class="local_only")
    s.add(missed)
    s.flush()
    events.emit_dataset_ready(s, missed)
    s.commit()
    before = len(_runs(s, child))
    tick(s)
    assert len(_runs(s, child)) == before

    run = engine.start(s, owner, source, params={"label": "hello"})
    assert run.status == "succeeded"
    s.commit()
    tick(s)
    chained = [row for row in _runs(s, child) if json.loads(row.params_json).get("label") == "hello"]
    assert len(chained) == 1 and chained[0].trigger_kind == "workflow" and chained[0].parent_run_id == run.id
    tick(s)
    assert len([row for row in _runs(s, child) if json.loads(row.params_json).get("label") == "hello"]) == 1

    parent = engine.start(s, owner, template)
    fail(s, parent, latest(s, parent, 1), "placeholder_unresolved")
    assert parent.status == "paused" and parent.pause_reason == "placeholder_unresolved"
    s.commit()
    tick(s)
    failed = [row for row in _runs(s, child) if json.loads(row.params_json).get("label") == "failed"]
    assert len(failed) == 1
    end = s.scalar(select(WorkflowEvent).where(WorkflowEvent.dedupe_key == f"workflow_failed:{parent.id}"))
    end.processed_at = None
    s.commit()
    tick(s)
    assert len([row for row in _runs(s, child) if json.loads(row.params_json).get("label") == "failed"]) == 1

    again = Dataset(dataset_key="DS-0201", project_id=template.project_id, name="Buchungen",
                    original_filename="Buchungen_2026-10.xlsx", upload_channel="sftp", uploaded_by=owner.id,
                    status="ready", data_class="local_only")
    s.add(again)
    s.flush()
    events.emit_dataset_ready(s, again)
    s.commit()
    tick(s)
    assert len(_runs(s, child)) == before + 3  # first dataset, success chain, failure chain, second dataset


def test_profile_release_enqueues_dataset_ready(graph, monkeypatch):
    s, owner, agent, other, template, steps = graph
    task = Task(project_id=template.project_id, task_nr="DEMO-0199", title="Profile", created_by=owner.id)
    ds = Dataset(dataset_key="DS-0301", project_id=template.project_id, name="Ledger",
                 original_filename="Buchungen_2026-08.xlsx", upload_channel="web", uploaded_by=owner.id,
                 status="profiling", data_class="local_only")
    s.add_all([task, ds])
    s.flush()
    schema = {"sheets": [{"name": "DATEV", "columns": [{"name": "A", "type": "string"}]}]}
    s.add(DatasetProfile(dataset_id=ds.id, task_id=task.id, profiler_version="test", sample_csv="A\n",
                         schema_json=json.dumps(schema), schema_fingerprint=fingerprint(schema), status="generated"))
    s.flush()
    monkeypatch.setattr("app.services.data_refs.propose_script", lambda *args, **kwargs: task)
    dataset_profiles.decide(s, ds, owner, "release")
    s.commit()
    row = s.scalar(select(WorkflowEvent).where(WorkflowEvent.dedupe_key == f"dataset_ready:{ds.id}"))
    assert ds.status == "ready" and row is not None and row.processed_at is None
    payload = json.loads(row.payload_json)
    assert payload["dataset"] == ds.id and payload["filename_date"] == "2026-08"
    assert payload["sftp_account"] is None and payload["project"] == "DEMO"


def test_trigger_editor_and_agent_denied(graph):
    s, owner, agent, other, template, steps = graph
    assert triggers.may_edit(owner, template) and not triggers.may_edit(agent, template)
    s.commit()
    path = "/workflows/DEMO/content/triggers"
    with TestClient(app) as client:
        assert login(client).status_code == 303
        client.cookies.set("tm_lang", "en")
        page = client.get(path)
        assert page.status_code == 200 and "Event triggers" in page.text
        csrf = page.text.split('name="csrf_token" value="')[1].split('"')[0]
        values = {"kind": "dataset_ready", "filter_json": '{"name_glob":"OP_*.csv"}', "params_json": "{}",
                  "enabled": "true", "csrf_token": csrf}
        assert client.post(path, data={**values, "csrf_token": ""}).status_code == 403
        saved = client.post(path, data=values, follow_redirects=False)
        assert saved.status_code == 303, saved.text
        s.commit()
        row = s.scalar(select(WorkflowTrigger))
        assert row.kind == "dataset_ready" and row.enabled and "OP_*.csv" in row.filter_json
        values.update(trigger_id=row.id, filter_json="{")
        assert client.post(path, data=values).status_code == 422
        s.commit()
        s.refresh(row)
        assert row.filter_json == '{"name_glob": "OP_*.csv"}' or "OP_*.csv" in row.filter_json
        assert client.get("/workflows/triggers").status_code == 200
        assert "dataset_ready" in client.get("/workflows/triggers").text
