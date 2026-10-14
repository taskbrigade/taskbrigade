"""`tm prepare`: claim and workspace with real Git (local bare repo as Gitea)."""
import json
import subprocess

import pytest
from typer.testing import CliRunner

from cli import client as client_mod
from cli import prepare as prep
from cli import tm
from cli.client import ApiError


def helpers(repo):
    """Full credential.helper entries (the first, empty entry is intentional)."""
    return subprocess.run(["git", "config", "--local", "--get-all", "credential.helper"], cwd=repo,
                          check=True, capture_output=True, text=True).stdout.splitlines()


def git(*args, cwd=None):
    return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True).stdout.strip()


@pytest.fixture
def origin(tmp_path):
    """Bare repo with main (README) and an existing task branch."""
    bare, seed = tmp_path / "origin.git", tmp_path / "seed"
    git("init", "--quiet", "--bare", "-b", "main", str(bare))
    git("clone", "--quiet", str(bare), str(seed))
    for k, v in (("user.name", "seed"), ("user.email", "s@x")):
        git("config", k, v, cwd=seed)
    (seed / "README.md").write_text("hallo\n")
    git("add", ".", cwd=seed); git("commit", "--quiet", "-m", "init", cwd=seed); git("push", "--quiet", "origin", "main", cwd=seed)
    git("checkout", "--quiet", "-b", "task/KET-0002-alt", cwd=seed)
    (seed / "alt.txt").write_text("vom ersten Versuch\n")
    git("add", ".", cwd=seed); git("commit", "--quiet", "-m", "wip", cwd=seed)
    git("push", "--quiet", "origin", "task/KET-0002-alt", cwd=seed)
    return bare


class Resp:
    def __init__(self, status, data=None):
        self.status_code, self._data = status, data

    def json(self):
        return self._data


class FakeClient:
    def __init__(self, tasks, creds=True):
        self.tasks, self.creds, self.calls = list(tasks), creds, []

    def get(self, path):
        if not self.creds:
            raise ApiError(404, "not_found", "keine Credentials")
        return {"username": "hermes", "token": "x", "email": "hermes@example.org", "gitea_url": "http://g"}

    def request(self, method, path, **kw):
        self.calls.append((method, path, kw.get("json")))
        if path == "/agents/me/claim":
            return Resp(200, self.tasks.pop(0)) if self.tasks else Resp(204)
        return Resp(200, {})


def task(nr, branch, clone_url):
    return {"task_nr": nr, "title": "Test", "status": "in_progress", "prio": 2, "assigned_to": "hermes",
            "branch_name": branch, "description": "Mach was", "acceptance_criteria": "- geht", "comments": [],
            "project_info": {"key": "KET", "name": "Ket", "privacy": "local_only", "clone_url": clone_url,
                             "default_branch": "main", "brain_path": "brain.md"}}


def test_no_task_wake_gate(monkeypatch, tmp_path):
    monkeypatch.setattr(tm, "_client", lambda: FakeClient([]))
    r = CliRunner().invoke(tm.app, ["prepare", "--work-root", str(tmp_path), "--wake-gate"])
    assert r.exit_code == 0 and json.loads(r.stdout.strip()) == {"wakeAgent": False}
    r = CliRunner().invoke(tm.app, ["prepare", "--work-root", str(tmp_path)])
    assert r.exit_code == 3


def test_missing_git_credentials_does_not_claim(tmp_path):
    fc = FakeClient([task("KET-0001", "task/KET-0001-x", "unused")], creds=False)
    with pytest.raises(prep.PrepareError, match="No Git credentials"):
        prep.prepare(fc, tmp_path)
    assert fc.calls == []  # Not claimed; task remains available for later.


def test_new_branch_and_existing_branch(tmp_path, origin):
    root = tmp_path / "work"
    fc = FakeClient([task("KET-0001", "task/KET-0001-neu", origin.as_uri()),
                     task("KET-0002", "task/KET-0002-alt", origin.as_uri())])
    p1 = prep.prepare(fc, root)
    wt1 = root / "KET" / ".worktrees" / "KET-0001"
    assert p1.worktree == wt1 and (wt1 / "README.md").exists()
    assert git("rev-parse", "--abbrev-ref", "HEAD", cwd=wt1) == "task/KET-0001-neu"
    task_md = (wt1 / "TASK.md").read_text()
    assert "# KET-0001 – Test" in task_md and "submit KET-0001" in task_md and "abort KET-0001" in task_md
    assert git("status", "--porcelain", cwd=wt1) == ""  # TASK.md is ignored.
    repo = root / "KET"
    assert git("config", "user.name", cwd=repo) == "hermes"
    h = helpers(repo)
    assert len(h) == 2 and h[0] == "" and h[1].endswith("git-credential")
    assert git("status", "--porcelain", cwd=repo) == ""  # .worktrees/ is ignored.

    p2 = prep.prepare(fc, root)  # Continue an existing remote branch (e.g. changes_requested).
    assert (p2.worktree / "alt.txt").read_text() == "vom ersten Versuch\n"


def test_reuse_existing_worktree_after_answer(tmp_path, origin):
    root = tmp_path / "work"
    t = task("KET-0003", "task/KET-0003-frage", origin.as_uri())
    prep.prepare(FakeClient([t]), root)
    wt = root / "KET" / ".worktrees" / "KET-0003"
    (wt / "lokal.txt").write_text("ungepushte Arbeit\n")
    prep.prepare(FakeClient([t]), root)  # Claim again after an answered question.
    assert (wt / "lokal.txt").exists()  # Preserve local work.


def test_git_failure_releases_task(tmp_path):
    fc = FakeClient([task("KET-0004", "task/KET-0004-x", (tmp_path / "gibtsnicht.git").as_uri())])
    with pytest.raises(prep.PrepareError):
        prep.prepare(fc, tmp_path / "work")
    method, path, body = fc.calls[-1]
    assert (method, path) == ("POST", "/tasks/KET-0004/release")
    assert "Could not prepare the workspace" in body["reason"]


def test_local_config_found_from_subdirectory(tmp_path, monkeypatch):
    monkeypatch.delenv("TBR_URL", raising=False)
    monkeypatch.delenv("TBR_TOKEN", raising=False)
    monkeypatch.delenv("TBR_CONFIG", raising=False)
    (tmp_path / ".taskbrigade.toml").write_text('url = "http://127.0.0.1:8090"\ntoken = "tm_hermes"\n')
    sub = tmp_path / "KET" / ".worktrees" / "KET-0001"
    sub.mkdir(parents=True)
    monkeypatch.chdir(sub)
    assert client_mod.load_config() == client_mod.Config("http://127.0.0.1:8090", "tm_hermes")


def test_global_store_helper_is_not_used(tmp_path, origin, monkeypatch):
    """Global `credential.helper=store` must not apply in the agent repo."""
    home = tmp_path / "home"
    home.mkdir()
    (home / ".gitconfig").write_text("[credential]\n\thelper = store\n")
    monkeypatch.setenv("HOME", str(home))
    root = tmp_path / "work"
    prep.prepare(FakeClient([task("KET-0005", "task/KET-0005-x", origin.as_uri())]), root)
    repo = root / "KET"
    # Git resolves the effective helper list; only tm remains after the empty entry.
    out = subprocess.run(["git", "credential", "fill"], cwd=repo, input="protocol=http\nhost=example.invalid\n\n",
                         capture_output=True, text=True, env={**__import__("os").environ, "GIT_TERMINAL_PROMPT": "0",
                                                               "GIT_TRACE": "1"})
    assert "credential-store" not in out.stderr
    prep.prepare(FakeClient([task("KET-0005", "task/KET-0005-x", origin.as_uri())]), root)  # idempotent
    assert len(helpers(repo)) == 2


def test_clone_runs_in_work_root_for_credential_lookup(tmp_path, origin, monkeypatch):
    """The credential helper searches for .taskbrigade.toml from its cwd; clone must run in the workspace,
    even when tm was invoked elsewhere (systemd timer or another cwd)."""
    seen = []
    real_git = prep._git

    def spy(*args, cwd=None):
        if "clone" in args:
            seen.append(cwd)
        return real_git(*args, cwd=cwd)

    monkeypatch.setattr(prep, "_git", spy)
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    monkeypatch.chdir(elsewhere)
    root = tmp_path / "work"
    prep.prepare(FakeClient([task("KET-0030", "task/KET-0030-x", origin.as_uri())]), root)
    assert seen == [root]


def test_task_md_uses_short_tm_and_single_commands(tmp_path, origin, monkeypatch):  # noqa: F811
    """TASK.md uses short `tm` (matching `tm *` allow rules) and asks for separate commands."""
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    tm_bin = fake_bin / "tm"
    tm_bin.write_text("#!/bin/sh\n")
    tm_bin.chmod(0o755)
    monkeypatch.setenv("PATH", f"{fake_bin}:{__import__('os').environ['PATH']}")
    monkeypatch.setattr(prep, "tm_command", lambda: str(tm_bin))
    wt = prep.prepare(FakeClient([task("KET-0040", "task/KET-0040-x", origin.as_uri())]), tmp_path / "work").worktree
    md = (wt / "TASK.md").read_text()
    assert "`tm submit KET-0040" in md and "`tm abort KET-0040" in md
    assert f"if `tm` is not found: `{tm_bin}`" in md
    assert "no `&&` chains" in md and 'git commit -m' in md


def test_resume_own_running_task(tmp_path, origin):  # noqa: F811
    """Hermes hit its step limit with a task in progress; the next run resumes without claiming."""
    t = {**task("KET-0060", "task/KET-0060-x", origin.as_uri()), "status": "in_progress", "assigned_to": "hermes"}

    class Own(FakeClient):
        def get(self, path, params=None):
            if path == "/agents/me/tasks":
                return {"items": [t] if params == {"status": "in_progress"} else []}
            if path == "/agents/me":
                return {"name": "hermes"}
            if path.startswith("/tasks/"):
                return dict(t)
            return super().get(path)

    fc = Own([])
    assert prep.own_running_task(fc) == "KET-0060"
    result = prep.prepare(fc, tmp_path / "work", prep.own_running_task(fc))
    assert result.task["task_nr"] == "KET-0060" and (result.worktree / "TASK.md").exists()
    paths = [p for _, p, _ in fc.calls]
    assert "/agents/me/claim" not in paths and "/tasks/KET-0060/heartbeat" in paths  # Lease refreshed.


# --- Merge conflict with main: prepare merges the base and lists conflicts in TASK.md -----------

def _push_main(tmp_path, origin, name, files):
    """Simulate a parallel merged task by committing directly to main."""
    seed = tmp_path / name
    git("clone", "--quiet", str(origin), str(seed))
    for k, v in (("user.name", "seed"), ("user.email", "s@x")):
        git("config", k, v, cwd=seed)
    for path, content in files.items():
        (seed / path).write_text(content)
    git("add", ".", cwd=seed); git("commit", "--quiet", "-m", name, cwd=seed); git("push", "--quiet", "origin", "main", cwd=seed)


def _task_branch_with(tmp_path, origin, branch, files):
    seed = tmp_path / ("b-" + branch.replace("/", "-"))
    git("clone", "--quiet", str(origin), str(seed))
    for k, v in (("user.name", "agent"), ("user.email", "a@x")):
        git("config", k, v, cwd=seed)
    git("checkout", "--quiet", "-b", branch, cwd=seed)
    for path, content in files.items():
        (seed / path).write_text(content)
    git("add", ".", cwd=seed); git("commit", "--quiet", "-m", "Ergebnis", cwd=seed)
    git("push", "--quiet", "origin", branch, cwd=seed)


def test_prepare_merges_base_and_reports_conflicts(tmp_path, origin, capsys):
    _task_branch_with(tmp_path, origin, "task/KET-0060-x", {"brain.md": "# Brain\n- Eintrag vom Agenten\n",
                                                            "ergebnis.md": "fertig\n"})
    _push_main(tmp_path, origin, "parallel", {"brain.md": "# Brain\n- Eintrag aus parallelem Task\n",
                                              "anderes.md": "x\n"})
    root = tmp_path / "work"
    t = task("KET-0060", "task/KET-0060-x", origin.as_uri())
    p = prep.prepare(FakeClient([t]), root)
    wt = p.worktree
    assert "Merge conflict with `main`" in p.note and "`brain.md`" in p.note
    assert "Merge conflict with `main`" in (wt / "TASK.md").read_text()
    assert "<<<<<<<" in (wt / "brain.md").read_text() and (wt / "anderes.md").exists()
    assert prep.unresolved_conflicts(wt) == ["brain.md"]
    # Reclaimed with conflict still open: no checkout error; note remains.
    assert "`brain.md`" in prep.prepare(FakeClient([t]), root).note
    # Resolved and committed: next run has no note and retains both entries.
    (wt / "brain.md").write_text("# Brain\n- Eintrag aus parallelem Task\n- Eintrag vom Agenten\n")
    git("add", "-A", cwd=wt); git("-c", "user.email=a@x", "commit", "--quiet", "--no-edit", cwd=wt)
    assert prep.unresolved_conflicts(wt) == []
    assert prep.prepare(FakeClient([t]), root).note == ""


def test_prepare_merges_base_without_conflict(tmp_path, origin):
    _task_branch_with(tmp_path, origin, "task/KET-0061-y", {"ergebnis.md": "fertig\n"})
    _push_main(tmp_path, origin, "parallel2", {"anderes.md": "x\n"})
    p = prep.prepare(FakeClient([task("KET-0061", "task/KET-0061-y", origin.as_uri())]), tmp_path / "work")
    assert p.note == "" and (p.worktree / "anderes.md").exists() and (p.worktree / "ergebnis.md").exists()
    assert git("status", "--porcelain", cwd=p.worktree) == ""
    # New task without a remote branch starts on main; no merge needed.
    p2 = prep.prepare(FakeClient([task("KET-0062", "task/KET-0062-neu", origin.as_uri())]), tmp_path / "work")
    assert p2.note == "" and (p2.worktree / "anderes.md").exists()


# --- Reused worktree without a remote branch: prepare brings it up to the current base --------------

def _origin_main(origin):
    return git("rev-parse", "main", cwd=origin)


def test_reused_worktree_without_own_commits_moves_to_new_base(tmp_path, origin):
    """First run only asked a question (nothing pushed); main moved on in between."""
    root = tmp_path / "work"
    t = task("KET-0070", "task/KET-0070-frage", origin.as_uri())
    wt = prep.prepare(FakeClient([t]), root).worktree
    _push_main(tmp_path, origin, "danach", {"neu.md": "aus WEB1-0009\n"})
    p = prep.prepare(FakeClient([t]), root)
    assert p.note == "" and (wt / "neu.md").exists()
    assert git("rev-parse", "HEAD", cwd=wt) == _origin_main(origin)  # Fast-forward, no merge commit.
    assert git("rev-parse", "--abbrev-ref", "HEAD", cwd=wt) == "task/KET-0070-frage"
    # The clone's local main follows origin/main too.
    assert git("rev-parse", "main", cwd=root / "KET") == _origin_main(origin)


def test_reused_worktree_with_local_commit_merges_new_base(tmp_path, origin):
    root = tmp_path / "work"
    t = task("KET-0071", "task/KET-0071-lokal", origin.as_uri())
    wt = prep.prepare(FakeClient([t]), root).worktree
    (wt / "eigen.md").write_text("nicht gepusht\n")
    git("add", ".", cwd=wt); git("-c", "user.email=a@x", "commit", "--quiet", "-m", "eigen", cwd=wt)
    own = git("rev-parse", "HEAD", cwd=wt)
    _push_main(tmp_path, origin, "danach2", {"neu.md": "x\n"})
    p = prep.prepare(FakeClient([t]), root)
    assert p.note == "" and (wt / "neu.md").exists() and (wt / "eigen.md").exists()
    assert subprocess.run(["git", "merge-base", "--is-ancestor", own, "HEAD"], cwd=wt).returncode == 0
    assert git("rev-list", "--count", "HEAD..origin/main", cwd=wt) == "0"
    assert git("status", "--porcelain", cwd=wt) == ""


def test_reused_dirty_worktree_is_untouched_and_noted(tmp_path, origin):
    root = tmp_path / "work"
    t = task("KET-0072", "task/KET-0072-dirty", origin.as_uri())
    wt = prep.prepare(FakeClient([t]), root).worktree
    before = git("rev-parse", "HEAD", cwd=wt)
    (wt / "README.md").write_text("halbfertig\n")
    _push_main(tmp_path, origin, "danach3", {"neu.md": "x\n"})
    p = prep.prepare(FakeClient([t]), root)
    assert git("rev-parse", "HEAD", cwd=wt) == before and not (wt / "neu.md").exists()
    assert (wt / "README.md").read_text() == "halbfertig\n"
    assert "behind `origin/main`" in p.note and "1 commits missing" in p.note
    assert "behind `origin/main`" in (wt / "TASK.md").read_text()


def test_local_base_not_moved_when_diverged(tmp_path, origin):
    root = tmp_path / "work"
    prep.prepare(FakeClient([task("KET-0073", "task/KET-0073-x", origin.as_uri())]), root)
    repo = root / "KET"
    (repo / "lokal.md").write_text("x\n")
    git("add", ".", cwd=repo); git("-c", "user.email=a@x", "commit", "--quiet", "-m", "lokal", cwd=repo)
    mine = git("rev-parse", "main", cwd=repo)
    _push_main(tmp_path, origin, "danach4", {"neu.md": "x\n"})
    prep.prepare(FakeClient([task("KET-0074", "task/KET-0074-x", origin.as_uri())]), root)
    assert git("rev-parse", "main", cwd=repo) == mine  # Diverged local main is left alone.
