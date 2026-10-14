"""Review jobs on the agent side: `tbr prepare` (REVIEW.md, read-only checkout), runner review sessions and the
MCP tool `review_task` (real Git, fake API)."""
import time

import pytest
from typer.testing import CliRunner

from cli import runner, tm
from cli import prepare as prep
from cli.client import ApiError
from cli.mcp_server import TaskBrigadeMCP
from tests.test_runner import RunnerClient, write_config
from tests.test_tm_prepare import Resp, git, origin, task  # noqa: F401 (Fixture)


def review_task(clone_url: str, **kw) -> dict:
    """KET-0002 waits for hermes' review; its branch exists in the origin fixture."""
    return {**task("KET-0002", "task/KET-0002-alt", clone_url), "status": "for_review", "assigned_to": "codex",
            "reviewer": "hermes", "review_claimed_at": "2026-10-12T08:00:00", "job": "review",
            "result_summary": "Alte Datei ergänzt", "open_points": ["Doku fehlt"], "pr_url": "http://g/pulls/2",
            "result_commit_sha": "a" * 40, **kw}


class ReviewClient(RunnerClient):
    """Fake API for a reviewer agent: the claim hands out the review job; `decided` ends it."""

    def __init__(self, t, decided: str | None = None):
        super().__init__([t], status="for_review")
        self.decided = decided

    def get(self, path):
        if path.startswith("/tasks/"):
            out = {**self.by_nr[path.split("/")[2]]}
            if self.decided:
                out.update(status=self.decided, review_claimed_at=None)
            return out
        if path == "/agents/me/reviews":
            return {"items": [], "total": 0}
        return super().get(path)

    def request(self, method, path, **kw):
        if path.endswith("/review/release"):
            self.calls.append((method, path, kw.get("json")))
            return Resp(200, {"task_nr": "KET-0002"})
        return super().request(method, path, **kw)


def test_prepare_review_job(tmp_path, origin):  # noqa: F811
    root = tmp_path / "work"
    fc = ReviewClient(review_task(origin.as_uri()))
    result = prep.prepare(fc, root)
    assert fc.calls[0] == ("POST", "/agents/me/claim", {"reviews": True})
    wt = root / "KET" / ".worktrees" / "KET-0002-review"
    assert result.review and result.worktree == wt and not (wt / "TASK.md").exists()
    assert git("rev-parse", "HEAD", cwd=wt) == git("rev-parse", "task/KET-0002-alt", cwd=origin)
    assert git("rev-parse", "--abbrev-ref", "HEAD", cwd=wt) == "HEAD"  # Detached: nothing to commit on.
    assert git("status", "--porcelain", cwd=wt) == ""  # REVIEW.md is excluded.
    md = (wt / "REVIEW.md").read_text()
    assert md.startswith("# TaskBrigade review job KET-0002 (already claimed for you)")
    for text in ("Alte Datei ergänzt", "- Doku fehlt", "alt.txt", "http://g/pulls/2", "- Reviewer: hermes",
                 "tbr review KET-0002 approve --comment", "tbr review KET-0002 reject --comment",
                 "tbr review KET-0002 conflict", "tbr review KET-0002 release --reason", "**read-only**"):
        assert text in md, text
    assert "tbr submit" not in md  # The assignee's closing hint is left out.
    # A second run (resumed job) resets the checkout to the pushed branch.
    (wt / "alt.txt").write_text("lokal verändert\n")
    assert prep.prepare(ReviewClient(review_task(origin.as_uri())), root, "KET-0002").review
    assert (wt / "alt.txt").read_text() == "vom ersten Versuch\n"


def test_prepare_review_failure_gives_job_back(tmp_path, origin):  # noqa: F811
    fc = ReviewClient(review_task(origin.as_uri(), branch_name=None))
    with pytest.raises(prep.PrepareError, match="no task branch"):
        prep.prepare(fc, tmp_path / "work")
    method, path, body = fc.calls[-1]
    assert path == "/tasks/KET-0002/review/release" and "no task branch" in body["reason"]


def test_prepare_without_reviews_flag(tmp_path, origin):  # noqa: F811
    fc = ReviewClient(review_task(origin.as_uri()))
    prep.prepare(fc, tmp_path / "work", "KET-0002", reviews=False)
    assert fc.calls[0] == ("POST", "/agents/me/claim", {"task_nr": "KET-0002"})


def test_tm_prepare_prints_review_md(tmp_path, origin, monkeypatch):  # noqa: F811
    monkeypatch.setattr(tm, "_client", lambda: ReviewClient(review_task(origin.as_uri())))
    r = CliRunner().invoke(tm.app, ["prepare", "--work-root", str(tmp_path / "work")])
    assert r.exit_code == 0, r.output
    assert r.stdout.startswith("# TaskBrigade review job KET-0002") and "## How to review" in r.stdout


def test_review_prompt():
    cmd = runner.build_command({"command": ["claude", "{prompt}"]}, {"task_nr": "KET-0002"}, "hermes", review=True)
    assert "REVIEW.md" in cmd[1] and "tbr review KET-0002 approve" in cmd[1] and "TASK.md" not in cmd[1]
    cmd = runner.build_command({"command": ["claude", "{prompt}"], "review_prompt": "R {task_nr}"},
                               {"task_nr": "KET-0002"}, "hermes", review=True)
    assert cmd[1] == "R KET-0002"


def test_review_session_gives_undecided_job_back(tmp_path, origin, capsys):  # noqa: F811
    root = tmp_path / "work"
    write_config(root, ["unused"], mode="foreground")
    fc = ReviewClient(review_task(origin.as_uri()))
    prep.prepare(fc, root)
    assert runner.session(fc, "KET-0002", root, ["true"], review=True) == 0
    method, path, body = fc.calls[-1]
    assert path == "/tasks/KET-0002/review/release" and "without a decision" in body["reason"]
    assert (root / ".locks" / "session-KET-0002-review.lock").exists()


def test_review_session_after_decision(tmp_path, origin, capsys):  # noqa: F811
    root = tmp_path / "work"
    write_config(root, ["unused"], mode="foreground", stop_after_finish_seconds=0.3)
    fc = ReviewClient(review_task(origin.as_uri()))
    prep.prepare(fc, root)
    fc.decided = "done"
    started = time.monotonic()
    rc = runner.session(fc, "KET-0002", root, ["sleep", "600"], poll_s=0.1, check_s=0.1, pane="", review=True)
    assert time.monotonic() - started < 20 and rc != 0
    out = capsys.readouterr().out
    assert "KET-0002 is done – agent stopped" in out
    assert not [c for c in fc.calls if c[1].endswith("/release")]


def test_supervisor_review_status(tmp_path):
    fc = ReviewClient(review_task("x"))
    sup = runner.Supervisor(fc, "KET-0002", tmp_path, {}, ["claude"], None, check_s=60, review=True)
    assert sup.check() is None  # Held review job: keep running.
    fc.by_nr["KET-0002"]["review_claimed_at"] = None
    assert sup._task_status() == "released"


def test_run_once_starts_review_session(tmp_path, origin):  # noqa: F811
    root = tmp_path / "work"
    write_config(root, ["sh", "-c", "echo reviewer"], mode="foreground")
    msg = runner.run_once(ReviewClient(review_task(origin.as_uri())), root)
    assert msg.startswith("KET-0002-review finished") and "log" in msg
    assert next((root / "logs").glob("KET-0002-review-*.log")).read_text()


class StaleClient:
    def get(self, path):
        if path.startswith("/agents/me/tasks"):
            return {"items": []}
        return {"items": [{"task_nr": "KET-0003", "review_claimed": True}]}


def test_stale_review_windows(monkeypatch, tmp_path):
    monkeypatch.setattr(runner, "list_windows", lambda s: [
        {"name": n, "dead": False} for n in ("KET-0002-review", "KET-0003-review", "runner")])
    assert runner.stale_windows(StaleClient(), "s", tmp_path) == ["KET-0002-review"]


# --- MCP -------------------------------------------------------------------------------------------------------------

class McpReviewClient(ReviewClient):
    def get(self, path, params=None):
        if path == "/tasks":
            return {"items": [], "total": 0}
        if path == "/agents/me/reviews":
            return {"items": [{**self.by_nr["KET-0002"], "review_claimed": False}], "total": 1}
        return super().get(path)

    def request(self, method, path, **kw):
        if path.endswith("/review"):
            self.calls.append((method, path, kw.get("json")))
            if not (kw["json"].get("comment") or "").strip() and kw["json"]["decision"] == "reject":
                raise ApiError(422, "missing_field", "Kommentar fehlt")
            return Resp(200, {"task_nr": "KET-0002", "status": "done"})
        return super().request(method, path, **kw)


def test_mcp_review_flow(tmp_path, origin):  # noqa: F811
    fc = McpReviewClient(review_task(origin.as_uri()))
    app = TaskBrigadeMCP(fc, tmp_path / "work")
    listing = app.list_tasks()
    assert "Review jobs (start_task, then review_task):" in listing and "KET-0002" in listing
    out = app.start_task()
    assert out.startswith("# Review job KET-0002 started") and "Working folder (read only):" in out
    assert "## How to review" in out and "KET-0002" in app._heartbeats
    assert "A rejection needs a comment" in app.review_task("KET-0002", "reject")
    assert "approve, reject, conflict or release" in app.review_task("KET-0002", "merge")
    assert app.review_task("KET-0002", "approve", "Tests grün") == "KET-0002: status done."
    assert fc.calls[-1] == ("POST", "/tasks/KET-0002/review", {"decision": "approve", "comment": "Tests grün"})
    assert "KET-0002" not in app._heartbeats
    assert app.review_task("KET-0002", "release", "keine Zeit") == "KET-0002: review job given back"
    assert fc.calls[-1] == ("POST", "/tasks/KET-0002/review/release", {"reason": "keine Zeit"})
