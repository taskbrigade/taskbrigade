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

    # Reopen: closed again, the tasks moved to Sprint B come back; only admins/owner. KET-0003 was moved out by
    # hand before the close and stays in Sprint B (it is not a close-time move).
    r = tm_as(e["orch"], "sprint", "reopen", "Sprint A", "-p", "KET")
    assert r.exit_code == 1 and "403" in r.output
    r = tm_as(e["admin"], "sprint", "reopen", "Sprint A", "-p", "KET")
    assert r.exit_code == 0 and "reopened – status is 'released' again" in r.stdout, r.output
    assert "2 task(s) moved back into the sprint" in r.stdout
    assert "KET-0001" in r.stdout and "KET-0002" in r.stdout
    assert task(e, "KET-0001")["sprint_name"] == "Sprint A" and task(e, "KET-0001")["status"] == "in_progress"
    assert task(e, "KET-0003")["sprint_name"] == "Sprint B"
    assert "Sprint A [released]" in tm_as(e["orch"], "sprint", "list", "-p", "KET").stdout
    r = tm_as(e["admin"], "sprint", "reopen", "Sprint A", "-p", "KET", "--no-restore-tasks")
    assert r.exit_code == 1  # Already released again.


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
    assert "403" in orch.reopen_sprint("KET", "Sprint M")
    out = admin.reopen_sprint("KET", "Sprint M")
    # KET-0003 was moved out by hand before the close and stays out; only the two detached at close return.
    assert "reopened – status is 'released' again" in out and "2 task(s) moved back into the sprint" in out
    assert task(e, "KET-0001")["sprint_name"] == "Sprint M"
    assert task(e, "KET-0003")["sprint"] is None
    assert "Sprint M [released]" in orch.list_sprints("KET")


def test_sprint_update_and_reopen_without_restore(senv, tm_as, mcp_as):  # noqa: F811
    e = senv
    assert tm_as(e["orch"], "sprint", "create", "-p", "KET", "--name", "Sprint U", "--goal", "Old goal",
                 "--start", "2026-10-12", "--end", "2026-10-23").exit_code == 0
    assert tm_as(e["orch"], "sprint", "create", "-p", "KET", "--name", "Other").exit_code == 0
    r = tm_as(e["orch"], "sprint", "update", "Sprint U", "-p", "KET")
    assert r.exit_code == 2 and "--name" in r.output

    r = tm_as(e["orch"], "sprint", "update", "Sprint U", "-p", "KET", "--name", "Sprint U2")
    assert r.exit_code == 0 and "Sprint U2 [planned] 2026-10-12 – 2026-10-23" in r.stdout, r.output
    assert "Goal: Old goal" in r.stdout
    r = tm_as(e["orch"], "sprint", "update", "Sprint U2", "-p", "KET", "--goal", "New goal")
    assert r.exit_code == 0 and "Goal: New goal" in r.stdout and "Sprint U2 [planned]" in r.stdout
    r = tm_as(e["orch"], "sprint", "update", "Sprint U2", "-p", "KET", "--goal", "")
    assert r.exit_code == 0 and "Goal: –" in r.stdout and "2026-10-12 – 2026-10-23" in r.stdout
    r = tm_as(e["orch"], "sprint", "update", "sprint u2", "-p", "KET", "--end", "none")
    assert r.exit_code == 0 and "Sprint U2 [planned] 2026-10-12 – ?" in r.stdout, r.output
    r = tm_as(e["orch"], "sprint", "update", "Sprint U2", "-p", "KET", "--end", "2026-10-01")
    assert r.exit_code == 1 and "invalid_dates" in r.output and "Traceback" not in r.output
    r = tm_as(e["orch"], "sprint", "update", "Sprint U2", "-p", "KET", "--name", "Other")
    assert r.exit_code == 1 and "sprint_exists" in r.output
    r = tm_as(e["orch"], "sprint", "update", "Nope", "-p", "KET", "--name", "X")
    assert r.exit_code == 2 and "has no sprint 'Nope'" in r.output
    r = tm_as(e["claude"], "sprint", "update", "Sprint U2", "-p", "KET", "--goal", "nope")
    assert r.exit_code == 1 and "403" in r.output and "No permission" in r.output
    r = tm_as(e["orch"], "sprint", "update", "Sprint U2", "-p", "KET", "--start", "12.11.")
    assert r.exit_code == 2 and "YYYY-MM-DD" in r.output

    assert tm_as(e["admin"], "sprint", "release", "Sprint U2", "-p", "KET").exit_code == 0
    r = tm_as(e["orch"], "sprint", "update", "Sprint U2", "-p", "KET", "--start", "2026-11-01", "--json")
    assert r.exit_code == 0 and '"status": "released"' in r.stdout and "2026-11-01" in r.stdout, r.output
    assert '"goal": null' in r.stdout  # Cleared earlier; this update did not touch the goal.

    orch, admin, worker = mcp_as("orch"), mcp_as("admin"), mcp_as("claude")
    out = orch.update_sprint("KET", "Sprint U2", goal="Via MCP")
    assert "Goal: Via MCP" in out and "Sprint U2 [released]" in out
    listing = e["c"].get("/api/v1/projects/KET/sprints", headers=e["admin"]).json()
    sid = next(s["id"] for s in listing if s["name"] == "Sprint U2")
    out = orch.update_sprint("KET", str(sid), name="Sprint U3")
    assert "Sprint U3 [released]" in out and "Goal: Via MCP" in out
    assert "sprint_exists" in orch.update_sprint("KET", "Sprint U3", name="Other")
    assert "invalid_dates" in orch.update_sprint("KET", "Sprint U3", end_date="2020-01-01")
    assert "has no sprint 'Missing'" in orch.update_sprint("KET", "Missing", name="Z")
    assert "403" in worker.update_sprint("KET", "Sprint U3", name="Nope")
    out = orch.update_sprint("KET", "Sprint U3", start_date="none")
    assert "Sprint U3 [released] –" in out  # Both dates are empty; the status did not change.
    assert "invalid_sprint_status" in admin.reopen_sprint("KET", "Sprint U3")

    # Reopen without putting the moved-out tasks back, via the CLI flag and the MCP argument.
    created = tm_as(e["orch"], "create", "--project", "KET", "--title", "Stay out", "--assign", "claude-code",
                    "--sprint", "Sprint U3", *DEF)
    assert created.exit_code == 0, created.output
    nr = created.stdout.strip()
    assert tm_as(e["admin"], "sprint", "close", "Sprint U3", "-p", "KET", "--detach").exit_code == 0
    r = tm_as(e["admin"], "sprint", "reopen", "Sprint U3", "-p", "KET", "--no-restore")
    assert r.exit_code == 0 and "reopened – status is 'released' again" in r.stdout, r.output
    assert "moved back" not in r.stdout and task(e, nr)["sprint"] is None
    # Still released: a second task, closed out again, stays out when restore_tasks is false.
    created = tm_as(e["orch"], "create", "--project", "KET", "--title", "Stay out too", "--assign", "claude-code",
                    "--sprint", "Sprint U3", *DEF)
    assert created.exit_code == 0, created.output
    nr2 = created.stdout.strip()
    assert tm_as(e["admin"], "sprint", "close", "Sprint U3", "-p", "KET", "--detach").exit_code == 0
    out = admin.reopen_sprint("KET", "Sprint U3", restore_tasks=False)
    assert "reopened – status is 'released' again" in out and "moved back" not in out
    assert task(e, nr2)["sprint"] is None
    assert "403" in orch.reopen_sprint("KET", "Sprint U3")  # Already released, and an orchestrator may not reopen.
