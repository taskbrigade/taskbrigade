"""Workflow transport rights, preview purity, publication and engine integration on MariaDB."""
import json

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select

from app.main import app
from app.models import Actor, Notification, Task, WorkflowParam, WorkflowRun, WorkflowRunStep, WorkflowTemplateVersion
from app.services import state_machine as sm
from tests.conftest import make_token
from tests.test_workflow_engine import graph  # noqa: F401

pytestmark = pytest.mark.db


@pytest.fixture
def api(graph):  # noqa: F811
    s, owner, agent, other, template, steps = graph
    other.role = "orchestrator"
    steps[0].title_tpl = "Write {{topic}} for {{run.period}}"
    s.add(WorkflowParam(version_id=template.current_version_id, name="topic", param_type="string",
                        label="Topic", required=True, default_expr="Workflows"))
    s.commit()
    tokens = {"owner": make_token(owner.name), "agent": make_token(agent.name), "orch": make_token(other.name),
              "outsider": make_token("opencode")}
    with TestClient(app, client=("127.0.0.1", 50000)) as client:
        yield client, {k: {"Authorization": "Bearer " + v} for k, v in tokens.items()}, s, template, steps


def test_preview_and_start_have_identical_resolution_without_writes(api):
    client, h, s, template, steps = api
    path = "/api/v1/workflows/DEMO/content/runs"
    before = {m: s.scalar(select(func.count()).select_from(m)) for m in (Task, WorkflowRun, WorkflowRunStep, Notification)}
    response = client.post(path, headers=h["orch"], json={"dry_run": True, "budget_cents": 0})
    assert response.status_code == 200, response.text
    preview = response.json()
    assert preview["params"] == {"topic": "Workflows"}
    assert preview["steps"][0]["title"].startswith("Write Workflows for ")
    assert preview["steps"][0]["assignee"] == "codex"
    assert preview["steps"][1]["placeholder_state"] == "deferred"
    s.commit()
    assert before == {m: s.scalar(select(func.count()).select_from(m)) for m in before}
    started = client.post(path, headers=h["orch"], json={"budget_cents": 0})
    assert started.status_code == 200, started.text
    run = started.json()
    assert run["budget_cents"] == 0 and run["first_task"]
    s.commit()
    task = s.scalar(select(Task).where(Task.task_nr == run["first_task"]))
    assert task.title == preview["steps"][0]["title"]
    detail = client.get(f"/api/v1/workflow-runs/{run['id']}", headers=h["agent"]).json()
    assert detail["params"] == preview["params"] and detail["steps"][0]["task_kind"] == "work"


def test_rights_and_controls(api):
    client, h, s, template, steps = api
    path = "/api/v1/workflows/DEMO/content"
    assert client.get(path, headers=h["outsider"]).status_code == 403
    assert client.get("/api/v1/workflows", headers=h["outsider"]).json() == []
    for who in ("agent", "outsider"):
        for preview in (True, False):
            assert client.post(path + "/runs", headers=h[who], json={"dry_run": preview}).status_code == 403
    started = client.post(path + "/runs", headers=h["orch"], json={}).json()
    run_path = f"/api/v1/workflow-runs/{started['id']}"
    assert client.post(run_path + "/pause", headers=h["agent"], json={}).status_code == 403
    assert client.post(run_path + "/pause", headers=h["orch"], json={"reason": "Check inputs"}).json()["pause_reason"] == "manual"
    assert client.post(run_path + "/resume", headers=h["orch"], json={"skip_failed": True}).status_code == 422
    assert client.post(run_path + "/resume", headers=h["orch"], json={}).json()["status"] == "running"
    assert client.get("/api/v1/workflow-runs?workflow=DEMO/content&status=running", headers=h["orch"]).json()[0]["id"] == started["id"]
    assert client.get("/api/v1/workflow-runs", headers=h["outsider"]).json() == []
    assert client.post(run_path + "/cancel", headers=h["owner"], json={"reason": "Stop"}).json()["status"] == "cancelled"
    s.commit()
    assert s.scalar(select(Task.status).where(Task.task_nr == started["first_task"])) == "cancelled"


def test_bad_params_and_budget_leave_no_runs(api):
    client, h, s, template, steps = api
    path = "/api/v1/workflows/DEMO/content/runs"
    for body in ({"params": {"unknown": "x"}}, {"params": {"topic": 7}}, {"params": {"topic": ""}},
                 {"budget_cents": -1}, {"budget_tokens": 4294967296}):
        for dry_run in (True, False):
            response = client.post(path, headers=h["orch"], json={**body, "dry_run": dry_run})
            assert response.status_code == 422, response.text
    s.commit()
    assert s.scalar(select(func.count()).select_from(WorkflowRun)) == 0


def test_draft_publish_pin_and_version_selection(api):
    client, h, s, template, steps = api
    path = "/api/v1/workflows/DEMO/new"
    body = {"name": "New", "steps": [{"step_key": "write", "step_type": "agent", "agent_role": "writer",
        "title_tpl": "Write {{topic}}", "description_tpl": "Write report", "acceptance_tpl": "Report exists"}],
        "params": [{"name": "topic", "param_type": "string", "label": "Topic", "default_expr": "Report"}]}
    assert client.put(path + "/draft", headers=h["agent"], json=body).status_code == 403
    response = client.put(path + "/draft", headers=h["orch"], json=body)
    assert response.status_code == 200, response.text
    assert client.get(path + "/draft", headers=h["orch"]).json()["state"] == "draft"
    published = client.post(path + "/publish", headers=h["owner"])
    assert published.status_code == 200, published.text
    assert published.json()["version"] == 1
    started = client.post(path + "/runs", headers=h["orch"], json={}).json()
    body["steps"][0]["title_tpl"] = "New {{topic}}"
    assert client.put(path + "/draft", headers=h["owner"], json=body).json()["version"] == 2
    assert client.put(path + "/draft", headers=h["owner"], json=body).json()["version"] == 2
    assert client.post(path + "/publish", headers=h["owner"]).json()["version"] == 2
    assert client.get(path + "?version=1", headers=h["orch"]).json()["steps"][0]["title"] == "Write {{topic}}"
    assert client.get(f"/api/v1/workflow-runs/{started['id']}", headers=h["orch"]).json()["version"] == 1
    assert client.get(path + "?version=99", headers=h["orch"]).status_code == 404
    s.commit()
    assert s.scalar(select(func.count()).select_from(WorkflowTemplateVersion)) == 3


def test_masking_and_later_output_resolution(api):
    client, h, s, template, steps = api
    steps[0].data_class = "local_only"
    steps[0].agent_role = None
    steps[0].assignee_id = s.scalar(select(Actor.id).where(Actor.name == "hermes"))
    # Raise capacity: the orchestrator is also the role's fixed assignee here.
    actor = s.get(Actor, steps[0].assignee_id)
    actor.max_parallel_tasks = 5
    s.commit()
    path = "/api/v1/workflows/DEMO/content"
    assert client.get(path, headers=h["agent"]).json()["steps"][0]["title"] == "***"
    preview = client.post(path + "/runs", headers=h["orch"], json={"dry_run": True}).json()
    assert preview["steps"][0]["title"].startswith("Write")
    run = client.post(path + "/runs", headers=h["owner"], json={}).json()
    s.commit()
    row = s.scalar(select(WorkflowRunStep).join(WorkflowRun).where(WorkflowRun.id == run["id"], WorkflowRunStep.position == 1))
    row.output_json = json.dumps({"output": {"secret": "private result"}})
    s.commit()
    detail = client.get(f"/api/v1/workflow-runs/{run['id']}", headers=h["agent"]).json()
    assert detail["steps"][0]["outputs"] == "***" and detail["params"] == "***"
    assert "private result" not in json.dumps(detail)


def test_output_placeholder_activates_successor(api):
    client, h, s, template, steps = api
    steps[1].title_tpl = "Approve {{steps.write.summary}}"
    s.commit()
    run = client.post("/api/v1/workflows/DEMO/content/runs", headers=h["owner"], json={}).json()
    s.commit()
    task = s.scalar(select(Task).where(Task.task_nr == run["first_task"]))
    task.result_summary = "Finished report"
    owner = s.scalar(select(Actor).where(Actor.name == "admin"))
    sm.transition_to(s, task, "done", owner, override=True)
    s.commit()
    response = client.get(f"/api/v1/workflow-runs/{run['id']}", headers=h["owner"]).json()
    assert response["status"] == "waiting"
    approval = s.scalar(select(Task).where(Task.task_kind == "approval"))
    assert approval.title == "Approve Finished report"


def test_tick_requires_admin_and_loopback(api):
    client, h, s, template, steps = api
    assert client.post("/api/v1/workflows/tick", headers=h["orch"]).status_code == 403
    response = client.post("/api/v1/workflows/tick", headers=h["owner"])
    assert response.status_code == 200 and response.json() == {"skipped": "disabled"}


def test_branchless_approval_claim_prepare_and_decide(api, tmp_path):
    from cli.client import ApiError
    from cli.prepare import prepare

    client, h, s, template, steps = api
    reviewer = s.scalar(select(Actor).where(Actor.name == "hermes"))
    reviewer.role = "reviewer"
    steps[0].step_type, steps[0].review_mode, steps[0].agent_role = "approval", "agent", None
    steps[0].reviewer_id = reviewer.id
    s.commit()
    run = client.post("/api/v1/workflows/DEMO/content/runs", headers=h["owner"], json={}).json()
    assert run["status"] == "waiting", run

    class Bridge:
        def get(self, path):
            response = client.get("/api/v1" + path, headers=h["orch"])
            if response.is_error:
                data = response.json()["error"]
                raise ApiError(response.status_code, data["code"], data["message"])
            return response.json()

        def request(self, method, path, **kwargs):
            return client.request(method, "/api/v1" + path, headers=h["orch"], **kwargs)

    prepared = prepare(Bridge(), tmp_path)
    assert prepared.review and prepared.task["task_kind"] == "approval"
    assert prepared.task["branch_name"] is None and (prepared.worktree / "REVIEW.md").exists()
    response = client.post(f"/api/v1/tasks/{run['first_task']}/review", headers=h["orch"],
                           json={"decision": "approve", "comment": "Checked"})
    assert response.status_code == 200 and response.json()["status"] == "done", response.text


def test_condition_and_parallel_group_roundtrip_and_skip(api):
    client, h, s, template, steps = api
    path = "/api/v1/workflows/DEMO/parallel"
    body = {"name": "Parallel", "params": [{"name": "mode", "param_type": "string", "label": "Mode",
                                            "default_expr": "off"}],
            "steps": [
                {"step_key": "one", "step_type": "agent", "agent_role": "writer", "title_tpl": "One",
                 "description_tpl": "First", "acceptance_tpl": "Done", "parallel_group": "g"},
                {"step_key": "two", "step_type": "agent", "agent_role": "writer", "title_tpl": "Two",
                 "description_tpl": "Second", "acceptance_tpl": "Done", "parallel_group": "g",
                 "condition_tpl": "{{params.mode}}"},
                {"step_key": "done", "step_type": "notify", "title_tpl": "Done"},
            ]}
    assert client.put(path + "/draft", headers=h["orch"], json=body).status_code == 200
    info = client.get(path + "/draft", headers=h["orch"]).json()
    by_key = {s["key"]: s for s in info["steps"]}
    assert by_key["one"]["parallel_group"] == "g" and by_key["two"]["parallel_group"] == "g"
    assert by_key["two"]["condition_tpl"] == "{{params.mode}}"
    assert client.post(path + "/publish", headers=h["owner"]).json()["version"] == 1
    # With mode=off the conditional member is skipped, the sibling still runs.
    run = client.post(path + "/runs", headers=h["orch"], json={}).json()
    s.commit()
    rows = {r.position: r for r in s.scalars(select(WorkflowRunStep).where(WorkflowRunStep.run_id == run["id"])).all()}
    assert rows[2].status == "skipped" and rows[2].error_code == "condition_false"
    assert rows[1].status == "active" and rows[1].task_id
    assert run["status"] == "running"
