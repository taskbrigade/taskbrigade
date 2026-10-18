"""CLI/MCP argument forwarding, errors and branchless approval preparation."""
import json
from types import SimpleNamespace

import pytest
from typer.testing import CliRunner

from app.errors import Unprocessable
from app.services.workflows.placeholders import Context
from app.services.workflows.rendering import resolve_params
from cli import prepare, tm, tm_admin
from cli.client import ApiError
from cli.mcp_server import TaskBrigadeMCP, build_server


class WorkflowClient:
    def __init__(self):
        self.calls = []

    def request(self, method, path, **kwargs):
        self.calls.append((method, path, kwargs))
        if method == "POST" and kwargs.get("json", {}).get("dry_run"):
            data = {"dry_run": True, "workflow": "DEMO/report", "steps": [{"title": "Report"}]}
        elif path == "/workflows" or path.startswith("/workflows/") and method == "GET":
            data = {"workflow": "DEMO/report", "name": "Report", "version": 1, "params": []}
            if path == "/workflows":
                data = [data]
        else:
            data = {"id": 4, "seq": 2, "workflow": "DEMO/report", "status": "running",
                    "current_position": 1, "total_steps": 3, "first_task": "DEMO-0001"}
            if path == "/workflow-runs":
                data = [data]
        return SimpleNamespace(status_code=200, json=lambda: data)


def test_cli_workflow_commands(monkeypatch):
    client = WorkflowClient()
    monkeypatch.setattr(tm, "_client", lambda: client)
    runner = CliRunner()
    result = runner.invoke(tm.app, ["workflow", "start", "demo/report", "--param", "month=2026-09",
        "--param", "text=a=b", "--budget-cents", "0", "--dry-run"])
    assert result.exit_code == 0 and "nothing created" in result.stdout, result.output
    assert client.calls[-1] == ("POST", "/workflows/DEMO/report/runs", {"json": {
        "params": {"month": "2026-09", "text": "a=b"}, "budget_cents": 0, "dry_run": True}})
    for args in (["workflows", "--project", "DEMO"], ["workflow", "show", "DEMO/report", "--version", "2"],
                 ["workflow", "runs", "DEMO/report", "--status", "paused", "--limit", "5"],
                 ["workflow", "run", "4"], ["workflow", "pause", "4", "--reason", "Check"],
                 ["workflow", "resume", "4", "--from-step", "write", "--skip"],
                 ["workflow", "cancel", "4", "--reason", "Stop"]):
        result = runner.invoke(tm.app, [*args, "--json"])
        assert result.exit_code == 0, result.output
        json.loads(result.stdout)
    assert client.calls[-2][2]["json"] == {"reason": None, "from_step": "write", "skip_failed": True}
    before = len(client.calls)
    for args in (["workflow", "start", "bad"], ["workflow", "start", "DEMO/report", "--param", "x"],
                 ["workflow", "start", "DEMO/report", "--param", "x=1", "--param", "x=2"],
                 ["workflow", "start", "DEMO/report", "--budget-cents", "-1"]):
        assert runner.invoke(tm.app, args).exit_code == 2
    assert len(client.calls) == before


def test_mcp_workflow_tools(tmp_path):
    client = WorkflowClient()
    app = TaskBrigadeMCP(client, tmp_path)
    assert "Report" in app.list_workflows("DEMO")
    assert "Report" in app.show_workflow("DEMO/report", 2)
    assert "nothing created" in app.start_workflow_run("DEMO/report", {"month": "2026-09"}, 100, 0, True)
    assert client.calls[-1][2]["json"]["budget_cents"] == 0
    assert "#2" in app.list_workflow_runs("DEMO/report", "DEMO", "paused", 5)
    assert client.calls[-1][2]["params"]["limit"] == 5
    assert "#2" in app.show_workflow_run(4)
    app.pause_workflow_run(4, "Check")
    assert client.calls[-1][2]["json"] == {"reason": "Check"}
    app.resume_workflow_run(4, "write", True)
    assert client.calls[-1][2]["json"] == {"from_step": "write", "skip_failed": True}
    app.cancel_workflow_run(4, "Stop")
    assert client.calls[-1][1] == "/workflow-runs/4/cancel"
    assert "PROJ/key" in app.start_workflow_run("bad", {})
    server = build_server(app)
    assert server is not None  # Full tool inventory/schema is tested through the stdio test.


def test_mcp_denial_is_reported(tmp_path):
    client = WorkflowClient()

    def deny(*args, **kwargs):
        raise ApiError(403, "forbidden", "No access")

    client.request = deny
    assert "Failed" in TaskBrigadeMCP(client, tmp_path).start_workflow_run("DEMO/report", {})


def test_branchless_approval_without_git_credentials(tmp_path, monkeypatch):
    task = {"task_nr": "DEMO-0001", "task_kind": "approval", "status": "for_review", "job": "review",
            "title": "Release report", "prio": 2, "project_info": {"key": "DEMO"}, "assigned_to": None}

    class Client:
        def get(self, path):
            if path == "/agents/me/git-credentials":
                raise ApiError(422, "missing", "No credentials")
            if path == "/agents/me/reviews":
                return {"items": [task]}
            if path == "/agents/me":
                return {"name": "reviewer"}
            return task

        def request(self, method, path, **kwargs):
            assert path == "/agents/me/claim"
            assert kwargs["json"]["task_nr"] == task["task_nr"]
            return SimpleNamespace(status_code=200, json=lambda: task)

    def no_git(*args, **kwargs):
        pytest.fail("Approval preparation must not run git")

    monkeypatch.setattr(prepare, "_git", no_git)
    result = prepare.prepare(Client(), tmp_path)
    assert result.review and (result.worktree / "REVIEW.md").is_file()
    text = (result.worktree / "REVIEW.md").read_text()
    assert "workflow approval" in text and "No checkout or PR is required" in text


@pytest.mark.parametrize("kind,value,expected", [("int", "42", 42), ("decimal", "2.50", "2.50"),
    ("bool", "false", False), ("month", "2026-09", "2026-09"), ("date", "2026-10-16", "2026-10-16")])
def test_typed_parameters(kind, value, expected):
    param = SimpleNamespace(name="value", param_type=kind, required=True, default_expr=None, enum_values=None)
    assert resolve_params(SimpleNamespace(params=[param]), {"value": value}, Context()) == {"value": expected}


@pytest.mark.parametrize("kind,value", [("int", True), ("int", 2.4), ("bool", 1), ("date", "2026-02-31"),
    ("month", "2026-13"), ("decimal", "NaN"), ("enum", "missing"), ("dataset", 1)])
def test_invalid_parameters(kind, value):
    param = SimpleNamespace(name="value", param_type=kind, required=True, default_expr=None, enum_values='["ok"]')
    with pytest.raises(Unprocessable):
        resolve_params(SimpleNamespace(params=[param]), {"value": value}, Context())


def test_scheduler_admin_commands(monkeypatch):
    from contextlib import nullcontext
    from cli import workflow_admin
    from app.services.workflows import tick

    class Session:
        def scalars(self, query):
            return []

    monkeypatch.setattr(workflow_admin, "database", lambda: SimpleNamespace(session=lambda: nullcontext(Session())))
    calls = []
    monkeypatch.setattr(tick, "tick", lambda s, now: calls.append(now) or {"processed": 0, "failed": 0})
    runner = CliRunner()
    for args in (["workflows", "tick"], ["workflows", "tick", "--now", "2026-11-04T07:00"],
                 ["workflows", "schedules", "--next", "5"]):
        result = runner.invoke(tm_admin.app, args)
        assert result.exit_code == 0, result.output
    assert calls[1].isoformat() == "2026-11-04T07:00:00"
    result = runner.invoke(tm_admin.app, ["workflows", "calendar", "check", "DE-BW", "2027-01"])
    assert result.exit_code == 0 and "2027-01-07" in result.output and "2027-01-06" not in result.output
    assert runner.invoke(tm_admin.app, ["workflows", "tick", "--now", "bad"]).exit_code == 2


def test_approval_runner_prompt():
    from cli.runner import build_command

    command = build_command({"command": ["codex", "{prompt}"]},
                            {"task_nr": "DEMO-0001", "task_kind": "approval"}, "reviewer", review=True)
    assert "workflow approval DEMO-0001" in command[-1]
    assert "no Git checkout" in command[-1]
