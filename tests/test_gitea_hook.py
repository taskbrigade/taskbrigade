"""Offline guard tests: no Git commands or production Gitea access."""
import io
import subprocess
import types
from contextlib import contextmanager

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session
from typer.testing import CliRunner

from app.services import gitea_hook as hooks
from app.services.attachments import BLOCKED_EXTENSIONS
from cli.gitea_hook import register

ZERO, OLD, TIP, MIDDLE = "0" * 40, "1" * 40, "2" * 40, "3" * 40


@pytest.fixture
def guard():
    module = types.ModuleType("guard")
    exec(compile(hooks.payload(), "<installed hook>", "exec"), module.__dict__)
    return module


def push(guard, monkeypatch, trees, *, old=ZERO, new=TIP, commits=None, extra=""):
    calls = []

    def git(*args):
        calls.append(args)
        if args[0] == "rev-parse":
            return (TIP + "\n").encode()
        if args[0] == "rev-list":
            return "\n".join(commits or [TIP]).encode()
        assert args[:4] == ("ls-tree", "-r", "-z", "--name-only")
        return b"\0".join(trees[args[-1]]) + b"\0"

    monkeypatch.setattr(guard, "git", git)
    result = guard.check(io.StringIO(f"{old} {new} refs/heads/main\n" + extra))
    return result, calls


@pytest.mark.parametrize("extension", sorted(BLOCKED_EXTENSIONS))
def test_blocked_extensions_share_attachment_policy(guard, monkeypatch, capsys, extension):
    result, _ = push(guard, monkeypatch, {TIP: [f"nested/report.{extension.upper()}".encode()]})
    assert result == 1
    assert "encrypted Data upload" in capsys.readouterr().err


@pytest.mark.parametrize("path", [b"analysis.py", b"report.xlsx.txt", b"csv", b"dir.csv/script.py"])
def test_code_allowed(guard, monkeypatch, path):
    assert push(guard, monkeypatch, {TIP: [path]})[0] == 0


def test_intermediate_commit_cannot_hide_data(guard, monkeypatch):
    result, calls = push(guard, monkeypatch, {TIP: [b"code.py"], MIDDLE: [b"private.csv"]},
                         old=OLD, commits=[TIP, MIDDLE])
    assert result == 1
    assert ("rev-list", TIP, "^" + OLD) in calls


def test_new_branch_checks_full_history_and_tip(guard, monkeypatch):
    result, calls = push(guard, monkeypatch, {TIP: [b"data.zip"]})
    assert result == 1
    assert ("rev-list", TIP) in calls


def test_tags_multiref_and_delete(guard, monkeypatch):
    assert push(guard, monkeypatch, {}, old=OLD, new=ZERO)[1] == []
    result, calls = push(guard, monkeypatch, {TIP: [b"ok.py"]},
                         extra=f"{ZERO} {MIDDLE} refs/tags/v1\n")
    assert result == 0
    assert ("rev-parse", "--verify", MIDDLE + "^{commit}") in calls
    assert sum(c[0] == "ls-tree" for c in calls) == 1  # Shared commits checked once.


def test_unusual_filename_is_not_quoted_or_split_by_git(guard, monkeypatch, capsys):
    assert push(guard, monkeypatch, {TIP: [b"nested/line\n\xff\t.CSV"]})[0] == 1
    message = capsys.readouterr().err
    assert "\\n" in message and "\\xff" in message


@pytest.mark.parametrize("error", [OSError(), subprocess.CalledProcessError(1, "git")])
def test_inspection_errors_fail_closed(guard, monkeypatch, capsys, error):
    def fail(*args):
        raise error
    monkeypatch.setattr(guard, "git", fail)
    monkeypatch.setattr(guard.sys, "stdin", io.StringIO(f"{ZERO} {TIP} refs/heads/main\n"))
    assert guard.main() == 1
    assert "could not inspect" in capsys.readouterr().err


def test_invalid_input_fails_closed(guard, monkeypatch):
    monkeypatch.setattr(guard.sys, "stdin", io.StringIO("--options not-a-sha ref\n"))
    assert guard.main() == 1


def test_git_keeps_quarantine_environment(guard, monkeypatch):
    seen = []
    monkeypatch.setenv("GIT_OBJECT_DIRECTORY", "/tmp/quarantine-example")
    monkeypatch.setattr(guard.subprocess, "check_output", lambda *a, **kw: seen.append((a, kw)) or b"ok")
    assert guard.git("rev-list", TIP) == b"ok"
    assert seen[0][0] == (["git", "rev-list", TIP],)
    assert "env" not in seen[0][1]


@pytest.fixture
def repository(tmp_path):
    repo = tmp_path / "owner" / "repo.git"
    (repo / "objects").mkdir(parents=True)
    (repo / "HEAD").write_text("ref: refs/heads/main\n")
    (repo / "hooks").mkdir()
    dispatcher = repo / "hooks" / "pre-receive"
    dispatcher.write_text("#!/bin/sh\n# Gitea dispatcher placeholder (not run in unit tests)\n")
    dispatcher.chmod(0o755)
    return tmp_path, hooks.hook_path(tmp_path, "Owner", "Repo")


def test_installer_idempotent_dry_run_and_uninstall(repository):
    root, path = repository
    hooks.install(path, dry_run=True)
    assert not path.parent.exists()
    hooks.install(path)
    assert hooks.status(path) == "installed" and path.stat().st_mode & 0o111
    timestamp = path.stat().st_mtime_ns
    hooks.install(path)
    assert path.stat().st_mtime_ns == timestamp
    neighbor = path.parent / "gitea"
    neighbor.write_text("keep me")
    path.chmod(0o644)
    assert hooks.status(path) == "outdated"
    hooks.install(path)
    hooks.uninstall(path, dry_run=True)
    assert hooks.status(path) == "installed"
    hooks.uninstall(path)
    hooks.uninstall(path)
    assert hooks.status(path) == "missing" and neighbor.read_text() == "keep me"


def test_installed_executable_stdin_and_exit_status(repository):
    _, path = repository
    hooks.install(path)
    deletion = subprocess.run([str(path)], input=f"{OLD} {ZERO} refs/heads/main\n", text=True, capture_output=True)
    assert deletion.returncode == 0 and not deletion.stderr
    invalid = subprocess.run([str(path)], input="invalid input\n", text=True, capture_output=True)
    assert invalid.returncode == 1 and "push rejected" in invalid.stderr


def test_missing_dispatcher_is_not_reported_as_protected(repository):
    root, path = repository
    (path.parent.parent / "pre-receive").unlink()
    with pytest.raises(ValueError, match="dispatcher"):
        hooks.hook_path(root, "owner", "repo")


def test_foreign_hook_and_unsafe_paths_refused(repository, tmp_path):
    root, path = repository
    path.parent.mkdir(parents=True)
    path.write_text("#!/bin/sh\nexit 0\n")
    for operation in (hooks.install, hooks.uninstall):
        with pytest.raises(ValueError, match="unmanaged"):
            operation(path)
    for owner, repo in (("..", "repo"), ("owner", "../repo"), ("/owner", "repo"), ("", "repo")):
        with pytest.raises(ValueError):
            hooks.hook_path(root, owner, repo)
    with pytest.raises(ValueError, match="missing"):
        hooks.hook_path(root, "owner", "absent")
    path.unlink()
    path.symlink_to(root / "external")
    with pytest.raises(ValueError, match="symlink"):
        hooks.hook_path(root, "owner", "repo")


@pytest.fixture
def lookup_session():
    # Only the columns used by the real lookup are needed; no MariaDB test DB.
    engine = create_engine("sqlite://")
    with engine.begin() as conn:
        conn.execute(text("CREATE TABLE projects (id INTEGER PRIMARY KEY, gitea_owner TEXT, gitea_repo TEXT, "
                          "privacy TEXT)"))
        conn.execute(text("CREATE TABLE tasks (project_id INTEGER, data_class TEXT)"))
        conn.execute(text("CREATE TABLE datasets (project_id INTEGER)"))
        conn.execute(text("INSERT INTO projects VALUES (1,'o','local','local_only'), (2,'o','cloud','cloud_ok'), "
                          "(3,'o','mixed','cloud_ok'), (4,'o','store','cloud_ok'), (5,NULL,NULL,'local_only')"))
        conn.execute(text("INSERT INTO tasks VALUES (2,NULL), (3,'sealed')"))
        conn.execute(text("INSERT INTO datasets VALUES (4)"))
    with Session(engine) as session:
        yield session
    engine.dispose()


def test_project_lookup(lookup_session):
    # Execute the service's SQL with only its selected columns projected.
    class Projection:
        def scalars(self, query):
            return lookup_session.scalars(query.with_only_columns(hooks.Project.id))
    assert hooks.repositories(Projection()) == [1, 3, 4]
    assert hooks.repositories(Projection(), protected_only=False) == [1, 2, 3, 4]
    lookup_session.execute(text("UPDATE tasks SET data_class='local_only' WHERE project_id=3"))
    assert hooks.repositories(Projection()) == [1, 3, 4]


def test_cli_install_status_uninstall(repository, monkeypatch):
    import typer
    root, path = repository
    @contextmanager
    def session():
        yield object()
    database = types.SimpleNamespace(session=session)
    project = types.SimpleNamespace(gitea_owner="owner", gitea_repo="repo")
    flags = []
    def repositories(session, protected_only=True):
        flags.append(protected_only)
        return [project]
    monkeypatch.setattr(hooks, "repositories", repositories)
    app = typer.Typer()
    register(app, lambda: database)
    runner = CliRunner()
    def invoke(*args):
        return runner.invoke(app, ["gitea-hook", *args, "--repo-root", str(root)])
    assert invoke("status").exit_code == 1
    assert invoke("install", "--dry-run").exit_code == 0
    assert not path.exists()
    assert invoke("install").exit_code == 0
    assert invoke("status").exit_code == 0
    assert invoke("uninstall").exit_code == 0
    assert flags[-1] is False
    assert not path.exists()


def test_payload_command_needs_no_db():
    from cli.tm_admin import app
    result = CliRunner().invoke(app, ["data", "gitea-hook", "payload"])
    assert result.exit_code == 0 and result.stdout == hooks.payload()
