"""Sprints via `tm sprint …`, `tm create/update/list --sprint` and the MCP sprint tools against the in-process app."""
import pytest

from cli import client as client_mod
from cli.mcp_server import TaskBrigadeMCP
from tests.test_sprints import senv, task  # noqa: F401 (Fixture)
from tests.test_ui_cli import tm_as  # noqa: F401 (Fixture)
from tests.test_workflow import env, gitea  # noqa: F401 (Fixtures)

pytestmark = pytest.mark.db
DEF = ["--description", "Beschreibung", "--acceptance", "- erledigt"]


def test_sprint_flow_via_cli(senv, tm_as, tmp_path):  # noqa: F811
    e = senv
    r = tm_as(e["orch"], "sprint", "create", "-p", "ket", "--name", "Sprint A", "--goal", "Sprints ausliefern",
              "--start", "2026-10-12", "--end", "2026-10-23")
    assert r.exit_code == 0 and "Sprint 'Sprint A' created" in r.stdout and "planned" in r.stdout, r.output
    assert tm_as(e["orch"], "sprint", "create", "-p", "KET", "--name", "x", "--start", "12.10.").exit_code == 2
    r = tm_as(e["claude"], "sprint", "create", "-p", "KET", "--name", "Worker")
    assert r.exit_code == 1 and "403" in r.output
    r = tm_as(e["orch"], "sprint", "list", "-p", "KET")
    assert "Sprint A [planned] 2026-10-12 – 2026-10-23 · 0/0" in r.stdout

    # Task in a planned sprint: always a draft, with an explanation.
    r = tm_as(e["orch"], "create", "--project", "KET", "--title", "Einzeln", "--assign", "claude-code",
              "--sprint", "sprint a", *DEF)
    assert r.exit_code == 0 and r.stdout.strip() == "KET-0001", r.output
    assert "not released yet" in r.output and task(e, "KET-0001")["status"] == "draft"
    r = tm_as(e["orch"], "create", "--project", "KET", "--title", "x", "--sprint", "Gibt es nicht")
    assert r.exit_code == 2 and "has no sprint 'Gibt es nicht'" in r.output

    # Batch from a file (same line format as the UI); invalid lines create nothing.
    bad = tmp_path / "bad.txt"
    bad.write_text("Gut\n | codex\nZu | viele | Felder | hier | jetzt\nX | codex | 9\n")
    r = tm_as(e["orch"], "sprint", "add-tasks", "Sprint A", "-p", "KET", "--file", str(bad))
    assert r.exit_code == 2 and "Line 2: title is missing" in r.output and "Line 3: too many fields" in r.output
    assert "Line 4: prio" in r.output and "KET-0002" not in r.output
    unknown = tmp_path / "unknown.txt"
    unknown.write_text("A | niemand-bekannt\n")
    r = tm_as(e["orch"], "sprint", "add-tasks", "Sprint A", "-p", "KET", "--file", str(unknown))
    assert r.exit_code == 1 and "niemand-bekannt" in r.output  # Server checks agents (API text is German).
    good = tmp_path / "tasks.txt"
    good.write_text("# Sprint A\nBatch eins | claude-code | 2\n\nBatch zwei\n")
    r = tm_as(e["orch"], "sprint", "add-tasks", "Sprint A", "-p", "KET", "--file", str(good), "--dry-run")
    assert r.exit_code == 0 and "P2 claude-code" in r.stdout and "nothing created" in r.output
    r = tm_as(e["orch"], "sprint", "add-tasks", "Sprint A", "-p", "KET", "--file", "-",
              input=good.read_text())
    assert r.exit_code == 0 and "KET-0002" in r.stdout and "KET-0003" in r.stdout, r.output
    assert "2 draft(s) created in sprint 'Sprint A'" in r.output
    assert (task(e, "KET-0002")["prio"], task(e, "KET-0003")["assigned_to"]) == (2, None)
    assert tm_as(e["orch"], "update", "KET-0002", *DEF).exit_code == 0

    # Release: only admins/owner; complete drafts become defined, the rest is listed.
    r = tm_as(e["orch"], "sprint", "release", "Sprint A", "-p", "KET")
    assert r.exit_code == 1 and "403" in r.output and "No permission" in r.output
    r = tm_as(e["admin"], "sprint", "release", "Sprint A", "-p", "KET")
    assert r.exit_code == 0 and "2 task(s) set to defined" in r.stdout, r.output
    assert "KET-0003 Batch zwei (missing: description, acceptance_criteria)" in r.stdout
    assert [task(e, n)["status"] for n in ("KET-0001", "KET-0002", "KET-0003")] == ["defined", "defined", "draft"]

    # Agents see the sprint context in tm show / TASK.md.
    r = tm_as(e["claude"], "show", "KET-0001")
    assert "- Sprint: Sprint A (released)" in r.stdout and "## Sprint Sprint A" in r.stdout
    assert "Sprints ausliefern" in r.stdout

    # Filter and reassignment.
    r = tm_as(e["orch"], "list", "--assigned-to", "all", "--project", "KET", "--sprint", "Sprint A")
    assert all(n in r.stdout for n in ("KET-0001", "KET-0002", "KET-0003"))
    r = tm_as(e["orch"], "list", "--assigned-to", "all", "--sprint", "Sprint A")
    assert r.exit_code == 2 and "give the project too" in r.output
    tm_as(e["orch"], "sprint", "create", "-p", "KET", "--name", "Sprint B")
    r = tm_as(e["orch"], "update", "KET-0003", "--sprint", "none")
    assert r.exit_code == 0 and "sprint=–" in r.stdout and task(e, "KET-0003")["sprint"] is None
    r = tm_as(e["orch"], "list", "--assigned-to", "all", "--sprint", "none")
    assert "KET-0003" in r.stdout and "KET-0001" not in r.stdout
    r = tm_as(e["orch"], "update", "KET-0003", "--sprint", "Sprint B")
    assert r.exit_code == 0 and "sprint=Sprint B" in r.stdout and "draft until sprint 'Sprint B'" in r.output

    # Close: running tasks keep their status, open ones move.
    assert tm_as(e["claude"], "claim", "--task", "KET-0001").exit_code == 0
    r = tm_as(e["admin"], "sprint", "close", "Sprint A", "-p", "KET")
    assert r.exit_code == 1 and "open_tasks" in r.output
    assert tm_as(e["admin"], "sprint", "close", "Sprint A", "-p", "KET", "--detach", "--move-to", "x").exit_code == 2
    r = tm_as(e["admin"], "sprint", "close", "Sprint A", "-p", "KET", "--move-to", "Sprint B")
    assert r.exit_code == 0 and "moved to sprint Sprint B (KET-0001, KET-0002)" in r.stdout, r.output
    assert task(e, "KET-0001")["status"] == "in_progress" and task(e, "KET-0001")["sprint_name"] == "Sprint B"
    r = tm_as(e["orch"], "sprint", "show", "Sprint B", "-p", "KET", "--json")
    assert '"status": "planned"' in r.stdout and '"KET-0003"' in r.stdout
    r = tm_as(e["orch"], "sprint", "list", "-p", "KET", "--status", "closed")
    assert "Sprint A [closed]" in r.stdout and "Sprint B" not in r.stdout


@pytest.fixture
def mcp_as(senv, tm_as, monkeypatch):  # noqa: F811
    """TaskBrigadeMCP with a real Client against the in-process app (tm_as patches httpx.Client)."""
    def make(who):
        token = senv[who]["Authorization"].split()[1]
        return TaskBrigadeMCP(client_mod.Client(client_mod.Config("http://testserver", token)), None)
    return make


def test_sprint_flow_via_mcp(senv, mcp_as):  # noqa: F811
    e = senv
    orch, admin, worker = mcp_as("orch"), mcp_as("admin"), mcp_as("claude")
    assert "has no sprints" in orch.list_sprints("ket")
    out = orch.create_sprint("ket", "Sprint M", "Ziel M", "2026-10-12")
    assert "Sprint 'Sprint M' created" in out and "release_sprint" in out
    assert "403" in worker.create_sprint("KET", "Worker")

    out = orch.create_task("KET", "Per MCP", "D", "A", "claude-code", sprint="Sprint M")
    assert "KET-0001 created" in out and "draft until sprint 'Sprint M' is released" in out
    assert "has no sprint" in orch.create_task("KET", "x", sprint="Nope")

    assert "fix the lines first" in orch.add_sprint_tasks("KET", "Sprint M", "A | codex | 7")
    out = orch.add_sprint_tasks("KET", "sprint m", "Zeile eins | claude-code\nZeile zwei | | 1")
    assert "2 draft(s) created in sprint 'Sprint M'" in out and "KET-0002" in out and "KET-0003" in out
    assert "sprint Sprint M" in orch.update_task("KET-0002", description="D", acceptance_criteria="A")

    out = orch.list_sprints("KET", "planned")
    assert "Sprint M [planned]" in out and "Goal: Ziel M" in out
    assert "KET-0003" in orch.show_sprint("KET", "Sprint M")

    out = orch.release_sprint("KET", "Sprint M")
    assert "403" in out and "No permission" in out
    out = admin.release_sprint("KET", "Sprint M")
    assert "2 task(s) set to defined" in out and "KET-0003 Zeile zwei (missing: description" in out
    assert "- Sprint: Sprint M (released)" in worker.show_task("KET-0001") and "Ziel M" in worker.show_task("KET-0001")

    assert "sprint –" in orch.update_task("KET-0003", sprint="none")
    assert task(e, "KET-0003")["sprint"] is None
    orch.create_sprint("KET", "Sprint N")
    assert "not both" in admin.close_sprint("KET", "Sprint M", move_to="Sprint N", detach=True)
    assert "open_tasks" in admin.close_sprint("KET", "Sprint M")
    assert "403" in orch.close_sprint("KET", "Sprint M", detach=True)
    out = admin.close_sprint("KET", "Sprint M", detach=True)
    assert "detached from the sprint (KET-0001, KET-0002)" in out
    assert task(e, "KET-0001")["sprint"] is None and "Sprint M [closed]" in orch.list_sprints("KET")
