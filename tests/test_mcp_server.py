"""`tm mcp`: tool logic with real Git and a real MCP protocol test over stdio."""
import asyncio
import shutil
import sys

import pytest

from cli.client import ApiError
from cli.mcp_server import INSTRUCTIONS, TaskBrigadeMCP
from tests.test_tm_prepare import FakeClient, Resp, git, origin, task  # noqa: F401 (Fixture)


class McpClient(FakeClient):
    """Fake API: one task becomes in_progress on claim; records submit and ask."""

    def __init__(self, t, status="defined"):
        super().__init__([t])
        self.t = {**t, "status": status, "assigned_to": "claude-desktop"}

    def get(self, path, params=None):
        if path == "/agents/me":
            return {"name": "claude-desktop", "lease_minutes": 120}
        if path == "/agents/me/git-credentials":
            return super().get(path)
        if path == "/tasks":
            return {"items": [self.t], "total": 1}
        return dict(self.t)

    def request(self, method, path, **kw):
        self.calls.append((method, path, kw.get("json")))
        if path == "/agents/me/claim":
            if self.t["status"] not in ("defined", "changes_requested"):
                return Resp(204)
            self.t["status"] = "in_progress"
            return Resp(200, dict(self.t))
        if path.endswith("/submit"):
            self.t.update(status="for_review", pr_url="http://gitea/pulls/1")
        if path.endswith("/ask"):
            self.t["status"] = "questions"
        if path.endswith("/heartbeat") and self.t["status"] != "in_progress":
            raise ApiError(409, "invalid_transition", "nicht in Arbeit")
        return Resp(200, dict(self.t))


def test_list_start_changes_submit(tmp_path, origin):  # noqa: F811
    fc = McpClient(task("KET-0050", "task/KET-0050-mac", origin.as_uri()))
    app = TaskBrigadeMCP(fc, tmp_path / "work")
    assert "Ready to claim" in app.list_tasks() and "KET-0050" in app.list_tasks()

    out = app.start_task("ket-0050")
    wt = tmp_path / "work" / "KET" / ".worktrees" / "KET-0050"
    assert f"Working folder: {wt}" in out and "KET/.worktrees/KET-0050" in out and "# KET-0050" in out
    assert ("POST", "/agents/me/claim", {"task_nr": "KET-0050", "reviews": True}) in fc.calls
    assert "KET-0050" in app._heartbeats  # Lease renews automatically.

    (wt / "mac.md").write_text("# Vom Mac\n")  # The app edits only files.
    changes = app.show_changes("KET-0050")
    assert "mac.md" in changes

    assert "summary and commit_message are required" in app.submit_task("KET-0050", "", "x")
    result = app.submit_task("KET-0050", "Datei angelegt, geprüft", "mac.md anlegen")
    assert "submitted" in result and "http://gitea/pulls/1" in result
    method, path, body = fc.calls[-1]
    assert path == "/tasks/KET-0050/submit" and body["branch_name"] == "task/KET-0050-mac"
    assert body["commit_sha"] == git("rev-parse", "HEAD", cwd=wt)
    assert git("log", "-1", "--format=%s %an", cwd=wt) == "mac.md anlegen hermes"  # Identity from prepare.
    assert git("ls-remote", "--heads", str(origin), "task/KET-0050-mac")  # Pushed.
    assert "KET-0050" not in app._heartbeats


def test_resume_running_task_without_claim(tmp_path, origin):  # noqa: F811
    t = task("KET-0051", "task/KET-0051-x", origin.as_uri())
    TaskBrigadeMCP(McpClient(t), tmp_path / "work").start_task("KET-0051")
    fc = McpClient(t, status="in_progress")  # New chat: task already runs for this agent.
    out = TaskBrigadeMCP(fc, tmp_path / "work").start_task("KET-0051")
    assert "started" in out
    assert not any(p == "/agents/me/claim" for _, p, _ in fc.calls)


def test_submit_without_changes_and_ask(tmp_path, origin):  # noqa: F811
    fc = McpClient(task("KET-0052", "task/KET-0052-x", origin.as_uri()))
    app = TaskBrigadeMCP(fc, tmp_path / "work")
    app.start_task("KET-0052")
    assert "nothing to submit" in app.submit_task("KET-0052", "s", "m")
    assert "question asked" in app.ask("KET-0052", "Welche Farbe?")
    assert "KET-0052" not in app._heartbeats


def test_start_without_task(tmp_path):
    fc = McpClient(task("KET-0053", "task/KET-0053-x", "unused"), status="for_review")
    assert "No task ready to claim" in TaskBrigadeMCP(fc, tmp_path / "work").start_task()


@pytest.mark.skipif(not shutil.which("git"), reason="git fehlt")
def test_mcp_protocol_over_stdio(tmp_path):
    """Real MCP handshake: client starts `tm mcp` as a separate process (like desktop apps)."""
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client

    root = tmp_path / "work"
    root.mkdir()
    (root / ".taskbrigade.toml").write_text('url = "http://127.0.0.1:9"\ntoken = "tm_x"\n')
    params = StdioServerParameters(command=sys.executable, args=["-m", "cli.tm", "mcp", "--work-root", str(root)])

    async def run():
        async with stdio_client(params) as (read, write):
            async with ClientSession(read, write) as session:
                init = await session.initialize()
                tools = await session.list_tools()
                return init, {t.name: t for t in tools.tools}

    init, tools = asyncio.run(asyncio.wait_for(run(), timeout=30))
    assert init.server_info.name == "taskbrigade" and "submit_task" in (init.instructions or "")
    assert set(tools) == {"list_tasks", "start_task", "show_task", "show_changes", "submit_task", "ask",
                          "abort_task", "release_task", "comment", "list_projects", "list_agents", "create_task",
                          "update_task", "cancel_task", "reset_task", "duplicate_task", "list_sprints", "show_sprint", "create_sprint",
                          "add_sprint_tasks", "release_sprint", "close_sprint", "notify", "sftp_put", "sftp_ls",
                          "review_task", "report_waiting", "attach_files", "attach_text"}
    assert "attachments" in tools["create_task"].input_schema["properties"]
    assert set(tools["attach_files"].input_schema["required"]) == {"task_nr", "paths"}
    assert set(tools["attach_text"].input_schema["required"]) == {"task_nr", "filename", "content"}
    assert set(tools["duplicate_task"].input_schema["required"]) == {"task_nr"}
    assert "sprint" in tools["create_task"].input_schema["properties"]
    assert "sprint" in tools["update_task"].input_schema["properties"]
    assert set(tools["add_sprint_tasks"].input_schema["required"]) == {"project", "sprint", "lines"}
    assert set(tools["close_sprint"].input_schema["required"]) == {"project", "sprint"}
    assert "due_date" in tools["create_task"].input_schema["properties"]
    assert set(tools["update_task"].input_schema["required"]) == {"task_nr"}
    assert set(tools["cancel_task"].input_schema["required"]) == {"task_nr", "reason"}
    assert set(tools["create_task"].input_schema["required"]) == {"project", "title"}
    assert set(tools["submit_task"].input_schema["required"]) == {"task_nr", "summary", "commit_message"}
    assert INSTRUCTIONS.splitlines()[0] in init.instructions


def test_submit_refuses_conflict_markers(tmp_path, origin):  # noqa: F811
    fc = McpClient(task("KET-0052", "task/KET-0052-mac", origin.as_uri()))
    app = TaskBrigadeMCP(fc, tmp_path / "work")
    app.start_task("KET-0052")
    wt = tmp_path / "work" / "KET" / ".worktrees" / "KET-0052"
    (wt / "README.md").write_text("<<<<<<< HEAD\na\n=======\nb\n>>>>>>> origin/main\n")
    out = app.submit_task("KET-0052", "x", "y")
    assert "conflict markers" in out and "README.md" in out
    assert not any(c[1].endswith("/submit") for c in fc.calls)


class OrchestratorMcpClient:
    def __init__(self, deny=False):
        self.deny, self.calls = deny, []

    def get(self, path, params=None):
        return {"task_nr": "KET-0060", "row_version": 4}

    def request(self, method, path, **kw):
        self.calls.append((method, path, kw))
        if self.deny:
            raise ApiError(403, "forbidden", "Nur Admins und Orchestratoren dürfen Tasks bearbeiten")
        body = kw.get("json") or {}
        if path == "/tasks":
            return Resp(201, {"task_nr": "KET-0061", "status": "defined", **body})
        status = {"cancelled": "cancelled", "defined": "defined"}.get(body.get("to"), "defined")
        return Resp(200, {"task_nr": "KET-0060", "status": status, "prio": 3, "assigned_to": None, **body})


def test_orchestrator_tools():
    fc = OrchestratorMcpClient()
    app = TaskBrigadeMCP(fc, None)
    out = app.create_task("ket", "Neu", "D", "A", "codex", due_date="2026-10-31")
    assert fc.calls[-1][2]["json"]["due_date"] == "2026-10-31" and "Due date" not in out
    assert "Due date" not in app.create_task("ket", "Neu", "D", "A", "codex")
    assert "invalid" in app.create_task("ket", "Neu", due_date="morgen")

    out = app.update_task("ket-0060", assigned_to="openclaw-agent", due_date="2026-11-01", prio=1)
    method, path, kw = fc.calls[-1]
    assert (method, path, kw["headers"]) == ("PATCH", "/tasks/KET-0060", {"If-Match": "4"})
    assert kw["json"] == {"prio": 1, "assigned_to": "openclaw-agent", "due_date": "2026-11-01"}
    assert "due 2026-11-01" in out
    assert "Nothing to change" in app.update_task("KET-0060")

    assert "cancelled" in app.cancel_task("KET-0060", "doppelt")
    assert fc.calls[-1][2]["json"] == {"to": "cancelled", "comment": "doppelt"}
    assert "required" in app.cancel_task("KET-0060", " ")
    assert "status defined" in app.reset_task("KET-0060")

    worker = TaskBrigadeMCP(OrchestratorMcpClient(deny=True), None)
    for out in (worker.update_task("KET-0060", prio=2), worker.cancel_task("KET-0060", "x"),
                worker.reset_task("KET-0060")):
        assert "403" in out and "No permission" in out
