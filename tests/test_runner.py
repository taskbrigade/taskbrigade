"""Runner (`tbr run` / `tbr session`) with real Git and tmux where available."""
import shlex
import shutil
import subprocess
import sys
import time
import uuid

import pytest

from cli import runner
from cli.client import ApiError
from tests.test_tm_prepare import FakeClient, Resp, git, origin, task  # noqa: F401 (Fixture)


class RunnerClient(FakeClient):
    """FakeClient with task status, /agents/me, and heartbeat behavior."""

    def __init__(self, tasks, status="in_progress", heartbeat_status=200):
        super().__init__(tasks)
        self.status, self.heartbeat_status, self.by_nr = status, heartbeat_status, {t["task_nr"]: t for t in tasks}

    def get(self, path):
        if path == "/agents/me":
            return {"name": "hermes", "lease_minutes": 120}
        if path.startswith("/tasks/"):
            return {**self.by_nr[path.split("/")[2]], "status": self.status, "assigned_to": "hermes"}
        return super().get(path)

    def request(self, method, path, **kw):
        if path.endswith("/heartbeat"):
            self.calls.append((method, path, None))
            if self.heartbeat_status != 200:
                raise ApiError(self.heartbeat_status, "invalid_transition", "nicht in Arbeit")
            return Resp(200, {})
        return super().request(method, path, **kw)


def write_config(root, command, **extra):
    root.mkdir(parents=True, exist_ok=True)
    lines = ['url = "http://x"', 'token = "tm_x"', "[runner]", f"command = {command!r}".replace("'", '"')]
    lines += [f"{k} = {v!r}".replace("'", '"') for k, v in extra.items()]
    (root / ".taskbrigade.toml").write_text("\n".join(lines) + "\n")


def test_build_command_substitutes_prompt():
    cmd = runner.build_command({"command": ["claude", "--permission-mode", "acceptEdits", "{prompt}"]},
                               {"task_nr": "KET-0007"}, "claude-code")
    assert cmd[:3] == ["claude", "--permission-mode", "acceptEdits"]
    assert "KET-0007" in cmd[3] and "claude-code" in cmd[3] and "TASK.md" in cmd[3]
    assert runner.build_command({"command": "codex exec '{prompt}'"}, {"task_nr": "X-1"}, "codex")[0] == "codex"
    from pathlib import Path
    wt = Path("/w/KET/.worktrees/KET-0007")
    cmd = runner.build_command({"command": ["codex", "--add-dir", "{repo}", "-C", "{worktree}"]},
                               {"task_nr": "KET-0007"}, "codex", wt)
    assert cmd == ["codex", "--add-dir", "/w/KET", "-C", str(wt)]


def test_no_task_and_lock(tmp_path):
    root = tmp_path / "work"
    write_config(root, ["true"], mode="foreground")
    assert runner.run_once(RunnerClient([]), root) == "No task"
    assert (root / ".runner.lock").read_text().startswith("pid=")  # PID/start time of the last pass.
    import fcntl
    with (root / ".runner.lock").open("w") as held:
        fcntl.flock(held, fcntl.LOCK_EX)
        assert "already running" in runner.run_once(RunnerClient([]), root)


def test_finish_submits_commits_or_releases(tmp_path, origin):  # noqa: F811
    root = tmp_path / "work"
    t = task("KET-0010", "task/KET-0010-runner", origin.as_uri())
    fc = RunnerClient([t])
    from cli.prepare import prepare
    wt = prepare(fc, root).worktree

    # Without a commit, release with the exit code.
    msg = runner.finish(fc, "KET-0010", wt, 1)
    assert "given back" in msg and fc.calls[-1][1] == "/tasks/KET-0010/release"
    assert "exit 1" in fc.calls[-1][2]["reason"]

    # With a commit, push and submit using the latest commit message as summary.
    (wt / "neu.txt").write_text("x\n")
    git("add", "neu.txt", cwd=wt)
    git("-c", "user.email=a@b", "commit", "--quiet", "-m", "Neue Datei angelegt", cwd=wt)
    msg = runner.finish(fc, "KET-0010", wt, 0)
    assert "submitted" in msg
    method, path, body = fc.calls[-1]
    assert path == "/tasks/KET-0010/submit" and body["branch_name"] == "task/KET-0010-runner"
    assert body["summary"].startswith("Neue Datei angelegt") and "without tbr submit" in body["summary"]
    assert git("ls-remote", "--heads", str(origin), "task/KET-0010-runner")  # Branch exists on the remote.

    # Agent completed the task itself; do nothing.
    done = RunnerClient([t], status="for_review")
    assert "the agent finished it itself" in runner.finish(done, "KET-0010", wt, 0)
    assert done.calls == []


def test_session_runs_agent_then_finishes(tmp_path, origin, capsys):  # noqa: F811
    root = tmp_path / "work"
    write_config(root, ["unused"], mode="foreground")
    t = task("KET-0011", "task/KET-0011-x", origin.as_uri())
    fc = RunnerClient([t])
    from cli.prepare import prepare
    wt = prepare(fc, root).worktree
    agent = ["sh", "-c", "echo x > ergebnis.txt && git add ergebnis.txt && "
                         "git -c user.email=a@b commit -qm 'Ergebnis vom Agenten' && echo agent-fertig"]
    assert runner.session(fc, "KET-0011", root, agent) == 0
    out = capsys.readouterr().out
    assert "1 commit(s) pushed and submitted" in out
    assert fc.calls[-1][1] == "/tasks/KET-0011/submit"
    assert (wt / "ergebnis.txt").exists()


def test_run_once_foreground_spawns_session_with_log(tmp_path, origin):  # noqa: F811
    """run_once starts `tbr session` as a separate process and logs its output."""
    root = tmp_path / "work"
    write_config(root, ["sh", "-c", "echo agent"], mode="foreground")
    msg = runner.run_once(RunnerClient([task("KET-0014", "task/KET-0014-x", origin.as_uri())]), root)
    assert msg.startswith("KET-0014 finished") and "log" in msg
    assert next((root / "logs").glob("KET-0014-*.log")).read_text()  # Output appears in the log.


def test_heartbeat_stops_when_task_left_in_progress():
    fc = RunnerClient([], heartbeat_status=409)
    hb = runner.Heartbeat(fc, "KET-1", interval_s=0.05, max_s=10)
    hb.start()
    hb.join(timeout=2)
    assert not hb.is_alive() and len([c for c in fc.calls if c[1].endswith("heartbeat")]) == 1


def test_claude_permissions_as_arguments():
    cmd = runner.with_claude_permissions(["claude", "--permission-mode", "acceptEdits", "Prompt …"],
                                         runner.DEFAULT_CLAUDE_ALLOW)
    assert cmd[0] == "claude" and cmd[-1] == "Prompt …"  # Prompt remains the final positional argument.
    allow = next(a for a in cmd if a.startswith("--allowedTools=")).split("=", 1)[1].split(",")
    assert "Bash(tm:*)" in allow and "Bash(git:*)" in allow
    assert "Bash(git push --force:*)" in next(a for a in cmd if a.startswith("--disallowedTools="))
    tm_abs = runner.tm_command()
    if tm_abs != "tm":
        assert f"Bash({tm_abs}:*)" in allow  # TASK.md calls tbr by absolute path.
    only_prompt = runner.with_claude_permissions(["claude", "Prompt"], ["Read"])
    assert only_prompt == ["claude", "--allowedTools=Read", f"--disallowedTools={','.join(runner.CLAUDE_DENY)}",
                           "--permission-mode", "auto", "Prompt"]  # Default to auto mode.
    explicit = runner.with_claude_permissions(["claude", "--permission-mode", "acceptEdits", "P"], ["Read"])
    assert explicit.count("--permission-mode") == 1 and "acceptEdits" in explicit  # Preserve explicit mode.


def fake_tm(tmp_path, monkeypatch, body='while [ "$1" != "--" ]; do shift; done; shift; exec "$@"'):
    """`tbr` stand-in for the tmux window: the real `tbr session` would call the API at http://x."""
    script = tmp_path / "fake-tm"
    script.write_text(f"#!/bin/sh\n{body}\n")
    script.chmod(0o755)
    monkeypatch.setattr(runner, "tm_command", lambda: str(script))


@pytest.mark.skipif(not shutil.which("tmux"), reason="tmux nicht installiert")
def test_tmux_window_launch(tmp_path, origin, monkeypatch):  # noqa: F811
    session_name = f"tm-test-{uuid.uuid4().hex[:6]}"
    root = tmp_path / "work"
    write_config(root, ["sh", "-c", "echo im-tmux-fenster; sleep 30"], mode="tmux", tmux_session=session_name)
    fake_tm(tmp_path, monkeypatch)
    fc = RunnerClient([task("KET-0013", "task/KET-0013-t", origin.as_uri())])
    try:
        msg = runner.run_once(fc, root)
        assert "started in a tmux window" in msg
        windows = subprocess.run(["tmux", "list-windows", "-t", session_name, "-F", "#W"],
                                 capture_output=True, text=True).stdout.split()
        assert "KET-0013" in windows
        wt = root / "KET" / ".worktrees" / "KET-0013"
        pane_path = subprocess.run(["tmux", "display", "-p", "-t", f"{session_name}:KET-0013", "#{pane_current_path}"],
                                   capture_output=True, text=True).stdout.strip()
        assert pane_path == str(wt)
        assert runner.session_running(root, "KET-0013", session_name)  # Live window counts as a running session.
        window = next(w for w in runner.list_windows(session_name) if w["name"] == "KET-0013")
        assert window["owner"] == str(root.resolve()) and runner.own_window(window, root)
        assert not runner.own_window(window, tmp_path / "other-agent")
        assert not runner.session_running(root, "KET-0013")
    finally:
        subprocess.run(["tmux", "kill-session", "-t", session_name], capture_output=True)


@pytest.mark.skipif(not shutil.which("tmux"), reason="tmux nicht installiert")
def test_tmux_session_dying_at_start_gives_task_back(tmp_path, origin, monkeypatch):  # noqa: F811
    session_name = f"tm-test-{uuid.uuid4().hex[:6]}"
    root = tmp_path / "work"
    write_config(root, ["sh", "-c", "true"], mode="tmux", tmux_session=session_name)
    fake_tm(tmp_path, monkeypatch, body="echo 'tm: command not found'; exit 127")
    fc = RunnerClient([task("KET-0015", "task/KET-0015-t", origin.as_uri())])
    try:
        with pytest.raises(runner.RunnerError, match="ended right after the start.*command not found"):
            runner.run_once(fc, root)
        method, path, body = fc.calls[-1]
        assert path == "/tasks/KET-0015/release" and "could not start the agent" in body["reason"]
        # The dead window is closed: it would count as a running session and repeat the failure on every pass.
        assert "KET-0015" not in _window_names(session_name)
        assert "command not found" in next((root / "logs").glob("KET-0015-*.log")).read_text()
    finally:
        subprocess.run(["tmux", "kill-session", "-t", session_name], capture_output=True)


def _window_names(session):
    return subprocess.run(["tmux", "list-windows", "-t", session, "-F", "#W"], capture_output=True,
                          text=True).stdout.split()


def _old_window(session, name, command, dead=False):
    """An earlier window of the task as `_launch_tmux` leaves it (remain-on-exit); returns its pane pid."""
    wid = subprocess.run(["tmux", "new-window", "-d", "-P", "-F", "#{window_id}", "-t", f"{session}:", "-n", name,
                          "cat"], capture_output=True, text=True, check=True).stdout.strip()
    subprocess.run(["tmux", "set-option", "-t", wid, "remain-on-exit", "on"], check=True)
    subprocess.run(["tmux", "respawn-pane", "-k", "-t", wid, command], check=True)
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        state = subprocess.run(["tmux", "display", "-p", "-t", wid, "#{pane_dead} #{pane_pid}"], capture_output=True,
                               text=True).stdout.split()
        if state and state[0] == ("1" if dead else "0") and (dead or len(state) > 1):
            return int(state[1]) if len(state) > 1 else None
        time.sleep(0.1)
    raise AssertionError(f"window {name} not ready")


def _alive(pid):
    """Running – a zombie (exited, not yet reaped by the tmux server; slow CI runners) counts as gone."""
    import os
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return _proc_state(pid).split()[0] not in ("Z", "gone")


def _proc_state(pid):
    """`state ppid pgrp session` from /proc – tells a zombie or a process in another group apart on failure."""
    try:
        return " ".join(open(f"/proc/{pid}/stat").read().rsplit(")", 1)[1].split()[:4])
    except OSError:
        return "gone"


@pytest.mark.skipif(not shutil.which("tmux") or not shutil.which("flock"), reason="tmux/flock nicht installiert")
def test_tmux_old_windows_of_the_task_are_stopped_before_the_start(tmp_path, origin, monkeypatch):  # noqa: F811
    """Picked up again within stop_after_finish_seconds: a live window of the old session (holding the session lock
    and ignoring Ctrl+C) plus a dead one carry the task's name. Both are stopped and closed, the new window is
    addressed by its id – no "can't find window", no second session on the task."""
    session_name = f"tm-test-{uuid.uuid4().hex[:6]}"
    root = tmp_path / "work"
    write_config(root, ["sh", "-c", "echo neue-sitzung; sleep 30"], mode="tmux", tmux_session=session_name)
    fake_tm(tmp_path, monkeypatch)
    monkeypatch.setattr(runner, "STOP_SIGNALS", ((runner.signal.SIGINT, 0.5), (runner.signal.SIGTERM, 2),
                                                  (runner.signal.SIGKILL, 2)))
    lock = runner._session_lock_path(root, "KET-0052")
    (root / "logs").mkdir()
    old_log = root / "logs" / "KET-0052-20261010-193250.log"
    old_log.write_text("old run\n")
    subprocess.run(["tmux", "new-session", "-d", "-s", session_name, "-n", "runner"], check=True)
    try:
        _old_window(session_name, "KET-0052", "echo alt-tot", dead=True)
        old_pid = _old_window(session_name, "KET-0052",
                              f"trap '' INT; echo alt-laeuft; exec flock {shlex.quote(str(lock))} sleep 60")
        time.sleep(0.3)
        assert runner.session_running(root, "KET-0052")  # The old session holds the task lock.
        fc = RunnerClient([task("KET-0052", "task/KET-0052-t", origin.as_uri())])
        msg = runner.run_once(fc, root)
        assert "started in a tmux window" in msg
        assert _window_names(session_name).count("KET-0052") == 1
        assert not _alive(old_pid)  # SIGINT ignored → SIGTERM stopped it.
        assert not runner.session_running(root, "KET-0052")  # Lock released; the fake tbr takes none.
        out = subprocess.run(["tmux", "capture-pane", "-p", "-t", f"{session_name}:KET-0052"], capture_output=True,
                             text=True).stdout
        assert "neue-sitzung" in out
        text = old_log.read_text()
        assert "alt-tot" in text and "alt-laeuft" in text  # Old windows' content went to the old run log.
        assert "earlier tmux window stopped and closed" in (root / "logs" / "runner.log").read_text()
        assert not [c for c in fc.calls if c[1].endswith("/release")]
    finally:
        subprocess.run(["tmux", "kill-session", "-t", session_name], capture_output=True)


@pytest.mark.skipif(not shutil.which("tmux"), reason="tmux nicht installiert")
def test_tmux_window_of_returned_task_stopped_before_claim(tmp_path, origin, monkeypatch):  # noqa: F811
    """Task came back from review (changes_requested) while its old agent is still in the grace period: the runner
    stops that window before claiming, so the old session never sees its task in progress again."""
    session_name = f"tm-test-{uuid.uuid4().hex[:6]}"
    root = tmp_path / "work"
    write_config(root, ["sh", "-c", "sleep 30"], mode="tmux", tmux_session=session_name)
    fake_tm(tmp_path, monkeypatch)
    subprocess.run(["tmux", "new-session", "-d", "-s", session_name, "-n", "runner"], check=True)

    class Client(RunnerClient):
        def get(self, path):
            if path == "/agents/me/tasks?status=changes_requested":
                return {"items": [{"task_nr": "KET-0053", "status": "changes_requested"}]}
            return super().get(path)

        def request(self, method, path, **kw):
            if path == "/agents/me/claim":
                self.old_alive_at_claim = _alive(old_pid)
                self.old_state_at_claim = _proc_state(old_pid)
            return super().request(method, path, **kw)

    try:
        old_pid = _old_window(session_name, "KET-0053", "sleep 60")
        _old_window(session_name, "KET-0099", "sleep 60")  # Another task in progress: untouched.
        fc = Client([task("KET-0053", "task/KET-0053-t", origin.as_uri())])
        assert "started in a tmux window" in runner.run_once(fc, root)
        log = root / "logs" / "runner.log"
        assert fc.old_alive_at_claim is False, (
            f"old pid {old_pid} at claim: {fc.old_state_at_claim}; now: {_proc_state(old_pid)}\n"
            + (log.read_text() if log.exists() else "no runner.log"))
        names = _window_names(session_name)
        assert names.count("KET-0053") == 1 and "KET-0099" in names
    finally:
        subprocess.run(["tmux", "kill-session", "-t", session_name], capture_output=True)


class LaunchTmux:
    """Fake tmux for `_launch_tmux`: `new-window` prints a window id, `respawn-pane` can fail."""

    def __init__(self, respawn_rc=0, windows=""):
        self.respawn_rc, self.windows, self.calls = respawn_rc, windows, []

    def __call__(self, *args):
        self.calls.append(args)
        if args[0] == "new-window":
            return subprocess.CompletedProcess(args, 0, "@7\n", "")
        if args[0] == "respawn-pane" and self.respawn_rc:
            return subprocess.CompletedProcess(args, 1, "", "can't find window: KET-0052")
        if args[0] == "list-windows":
            return subprocess.CompletedProcess(args, 0, self.windows, "")
        if args[0] == "capture-pane":
            return subprocess.CompletedProcess(args, 0, "placeholder screen\n", "")
        return subprocess.CompletedProcess(args, 0, "", "")


def test_launch_addresses_window_by_id(tmp_path, monkeypatch):
    root = tmp_path / "work"
    wt = root / "KET" / ".worktrees" / "KET-0052"
    wt.mkdir(parents=True)
    tmux = LaunchTmux()
    monkeypatch.setattr(runner, "_tmux", tmux)
    monkeypatch.setattr(runner, "check_started", lambda *a, **kw: None)
    runner._launch_tmux("taskbrigade", "KET-0052", wt, ["tbr", "session", "KET-0052", "--", "codex"],
                        root / "logs" / "KET-0052-x.log", root)
    new = next(c for c in tmux.calls if c[0] == "new-window")
    assert "-P" in new and "#{window_id}" in new and new[new.index("-n") + 1] == "KET-0052"
    for cmd in ("set-option", "pipe-pane", "respawn-pane"):
        call = next(c for c in tmux.calls if c[0] == cmd)
        assert call[call.index("-t") + 1] == "@7"


def test_launch_failure_closes_placeholder_window(tmp_path, monkeypatch):
    root = tmp_path / "work"
    wt = root / "KET" / ".worktrees" / "KET-0052"
    wt.mkdir(parents=True)
    (root / "logs").mkdir()
    log = root / "logs" / "KET-0052-x.log"
    tmux = LaunchTmux(respawn_rc=1)
    monkeypatch.setattr(runner, "_tmux", tmux)
    with pytest.raises(runner.RunnerError, match="could not be opened"):
        runner._launch_tmux("taskbrigade", "KET-0052", wt, ["tbr", "session", "KET-0052", "--", "codex"], log, root)
    assert [c[2] for c in tmux.calls if c[0] == "kill-window"] == ["@7"]
    assert "placeholder screen" in log.read_text()


def test_launch_refuses_while_old_session_holds_the_lock(tmp_path, monkeypatch):
    root = tmp_path / "work"
    wt = root / "KET" / ".worktrees" / "KET-0052"
    wt.mkdir(parents=True)
    tmux = LaunchTmux()
    monkeypatch.setattr(runner, "_tmux", tmux)
    held = runner.acquire_session_lock(root, "KET-0052")
    try:
        with pytest.raises(runner.RunnerError, match="already running"):
            runner._launch_tmux("taskbrigade", "KET-0052", wt, ["tbr", "session", "KET-0052", "--", "codex"],
                                root / "logs" / "KET-0052-x.log", root)
    finally:
        runner.release_session_lock(held)
    assert not [c for c in tmux.calls if c[0] == "new-window"]


def test_codex_trust_added_and_removed(tmp_path, monkeypatch):
    home = tmp_path / "codexhome"
    home.mkdir()
    (home / "config.toml").write_text('model = "gpt-5"\n\n[projects."/anderes/projekt"]\ntrust_level = "trusted"\n')
    monkeypatch.setenv("CODEX_HOME", str(home))
    wt = tmp_path / "w" / "KET" / ".worktrees" / "KET-0020"
    runner.codex_trust(wt, True)
    runner.codex_trust(wt, True)  # idempotent
    import tomllib
    cfg = tomllib.loads((home / "config.toml").read_text())
    assert cfg["projects"][str(wt)]["trust_level"] == "trusted"
    assert cfg["projects"]["/anderes/projekt"]["trust_level"] == "trusted" and cfg["model"] == "gpt-5"
    assert (home / "config.toml").read_text().count(str(wt)) == 1
    runner.codex_trust(wt, False)
    cfg = tomllib.loads((home / "config.toml").read_text())
    assert str(wt) not in cfg["projects"] and cfg["projects"]["/anderes/projekt"] and cfg["model"] == "gpt-5"


FAKE_CLAUDE_TRUST = '''\
import os, sys, tty
pre, swallow, out = sys.argv[1] == "1", int(sys.argv[2]), sys.argv[3]
fd = sys.stdin.fileno()
tty.setraw(fd)
def draw(sel):
    sys.stdout.write("\\x1b[2J\\x1b[H" + ("This folder pre-approves 3 tool permissions\\r\\n" if pre else "")
                     + ("  " if sel else "❯ ") + "No, exit\\r\\n" + ("❯ " if sel else "  ")
                     + "Yes, I trust this folder\\r\\n")
    sys.stdout.flush()
sel = 0
draw(sel)
while True:
    k = os.read(fd, 8)
    if k.startswith(b"\\x1b[B"):
        if swallow:
            swallow -= 1  # Dialog does not accept keys yet.
            continue
        sel = 1
        draw(sel)
    elif k in (b"\\r", b"\\n"):
        open(out, "w").write("yes" if sel else "no")
        break
os.system("sleep 5")
'''


@pytest.mark.skipif(not shutil.which("tmux"), reason="tmux nicht installiert")
@pytest.mark.parametrize("pre,swallow,expected,answer", [
    (False, 0, "trust dialog confirmed", "yes"),
    (False, 1, "trust dialog confirmed", "yes"),  # First Down is lost; do not confirm "No, exit".
    (True, 0, "not confirmed", None),
])
def test_auto_trust_claude(tmp_path, pre, swallow, expected, answer):
    """Fake Claude in real tmux: a cursor menu writes the confirmed option to a file."""
    fake = tmp_path / "fake_claude.py"
    out = tmp_path / "answer"
    fake.write_text(FAKE_CLAUDE_TRUST)
    cmd = shlex.join([sys.executable, str(fake), "1" if pre else "0", str(swallow), str(out)])
    session = f"tm-trust-{uuid.uuid4().hex[:6]}"
    subprocess.run(["tmux", "new-session", "-d", "-s", session, "-x", "120", "-y", "20", cmd], check=True)
    try:
        result = runner.auto_trust_claude(f"{session}:0", timeout_s=5, poll_s=0.2)
        assert expected in result
        time.sleep(0.5)
        assert (out.read_text() if out.exists() else None) == answer
    finally:
        subprocess.run(["tmux", "kill-session", "-t", session], capture_output=True)


@pytest.mark.skipif(not shutil.which("tmux"), reason="tmux nicht installiert")
def test_auto_trust_claude_ready_without_dialog(tmp_path):
    fake = tmp_path / "fake-claude.sh"
    fake.write_text("#!/bin/sh\nprintf 'Claude Code vTEST\\n'\nsleep 5\n")
    fake.chmod(0o755)
    session = f"tm-trust-{uuid.uuid4().hex[:6]}"
    subprocess.run(["tmux", "new-session", "-d", "-s", session, "-x", "120", "-y", "20", str(fake)], check=True)
    try:
        assert "no trust dialog" in runner.auto_trust_claude(f"{session}:0", timeout_s=5, poll_s=0.2)
    finally:
        subprocess.run(["tmux", "kill-session", "-t", session], capture_output=True)


@pytest.mark.skipif(not shutil.which("tmux"), reason="tmux nicht installiert")
def test_auto_trust_claude_without_dialog(tmp_path):
    """Trusted folder: Claude starts directly and the runner must not send keys."""
    session = f"tm-trust-{uuid.uuid4().hex[:6]}"
    subprocess.run(["tmux", "new-session", "-d", "-s", session, "-x", "120", "-y", "20",
                    "printf 'Claude Code vTEST\\n'; sleep 5"], check=True)
    try:
        assert runner.auto_trust_claude(f"{session}:0", timeout_s=5, poll_s=0.2) == "no trust dialog"
    finally:
        subprocess.run(["tmux", "kill-session", "-t", session], capture_output=True)


def test_git_by_runner_mode(tmp_path, origin):  # noqa: F811
    """Codex sandbox blocks .git: agent creates files and .tm-submit.md; runner commits and submits."""
    assert runner.git_by_runner({"command": ["codex", "{prompt}"]})
    assert not runner.git_by_runner({"command": ["codex"], "git": "agent"})
    assert runner.git_by_runner({"command": ["claude"], "git": "runner"})
    assert not runner.git_by_runner({"command": ["claude"]})
    with pytest.raises(runner.RunnerError):
        runner.git_by_runner({"command": ["claude"], "git": "beides"})
    cmd = runner.build_command({"command": ["codex", "{prompt}"]}, {"task_nr": "KET-0012"}, "codex")
    assert ".tm-submit.md" in cmd[1] and "No git commands" in cmd[1]

    root = tmp_path / "work"
    t = task("KET-0012", "task/KET-0012-codex", origin.as_uri())
    fc = RunnerClient([t])
    from cli.prepare import SUBMIT_FILE, prepare
    wt = prepare(fc, root, git_by_runner=True).worktree
    md = (wt / "TASK.md").read_text()
    assert "No git commands" in md and SUBMIT_FILE in md and "git push" not in md

    # Reported as finished with no changes: release.
    (wt / SUBMIT_FILE).write_text("Nichts\n\nnichts zu tun")
    assert "no changes" in runner.finish(fc, "KET-0012", wt, 0)

    # Result and completion file: runner commits without the file, pushes, and submits with summary.
    (wt / "recherche.md").write_text("# Painpoints\n")
    (wt / SUBMIT_FILE).write_text("Recherche Painpoints angelegt\n\nTop 20 mit Bewertung.\n\n## Open points\n"
                                  "- Quellen prüfen\n- Top 50 ergänzen\n")
    msg = runner.finish(fc, "KET-0012", wt, 0)
    assert "1 commit(s) pushed and submitted" in msg
    _, path, body = fc.calls[-1]
    assert path == "/tasks/KET-0012/submit" and body["summary"].startswith("Top 20 mit Bewertung.")
    assert body["open_points"] == ["Quellen prüfen", "Top 50 ergänzen"] and "Quellen" not in body["summary"]
    assert "by the runner" in body["summary"]
    assert git("log", "-1", "--format=%s", cwd=wt) == "Recherche Painpoints angelegt"
    assert git("show", "--name-only", "--format=", "HEAD", cwd=wt).split() == ["recherche.md"]
    assert not (wt / SUBMIT_FILE).exists()
    assert git("ls-remote", "--heads", str(origin), "task/KET-0012-codex")


def test_session_submits_when_submit_file_appears(tmp_path, origin, capsys):  # noqa: F811
    """Codex TUI stays open after work: runner submits on a complete .tm-submit.md and stops it."""
    root = tmp_path / "work"
    write_config(root, ["unused"], mode="foreground", git="runner")
    t = task("KET-0013", "task/KET-0013-x", origin.as_uri())
    fc = RunnerClient([t])
    from cli.prepare import prepare
    prepare(fc, root, git_by_runner=True)
    # Interactive agent writes result and completion file, then waits indefinitely for input.
    agent = ["sh", "-c", "echo x > ergebnis.txt; printf 'Ergebnis\\n\\nFertig.' > .tm-submit.md; exec sleep 600"]
    started = time.monotonic()
    rc = runner.session(fc, "KET-0013", root, agent, poll_s=0.2, settle_s=0.5)
    assert time.monotonic() - started < 30 and rc != 0  # Stopped instead of waiting 600 s.
    out = capsys.readouterr().out
    assert "1 commit(s) pushed and submitted – stopping sh" in out
    assert out.count("submitted") == 1  # No second submission after stopping.
    assert [c[1] for c in fc.calls].count("/tasks/KET-0013/submit") == 1


def test_finish_refuses_unresolved_conflict_markers(tmp_path, origin):  # noqa: F811
    root = tmp_path / "work"
    t = task("KET-0014", "task/KET-0014-k", origin.as_uri())
    fc = RunnerClient([t])
    from cli.prepare import SUBMIT_FILE, prepare
    wt = prepare(fc, root, git_by_runner=True).worktree
    (wt / "README.md").write_text("<<<<<<< HEAD\nalt\n=======\nneu\n>>>>>>> origin/main\n")
    (wt / SUBMIT_FILE).write_text("Fertig\n\nfertig")
    msg = runner.finish(fc, "KET-0014", wt, 0)
    assert "conflict markers" in msg and fc.calls[-1][1] == "/tasks/KET-0014/release"
    assert (wt / SUBMIT_FILE).exists()  # Completion file remains for the next run.
    assert git("log", "-1", "--format=%s", cwd=wt) != "Fertig"


class SkipClient:
    """Fake API: assigned tasks that claim does not deliver (204)."""

    def __init__(self, items, details=None, max_parallel=1):
        self.items, self.details, self.max_parallel = items, details or {}, max_parallel

    def get(self, path):
        if path == "/agents/me":
            return {"name": "codex", "max_parallel_tasks": self.max_parallel}
        if path == "/agents/me/reviews":
            return {"items": [], "total": 0}
        if path == "/agents/me/tasks?status=in_progress":
            items = [t for t in self.items if t["status"] == "in_progress"]
            return {"items": items, "total": len(items)}
        if path.startswith("/agents/me/tasks"):
            assert "status=draft,defined,changes_requested,in_progress" in path
            return {"items": self.items, "total": len(self.items)}
        if path == "/agents/me/git-credentials":
            return {"username": "codex", "token": "x", "email": None, "gitea_url": "http://g"}
        return self.details[path.split("/")[2]]

    def request(self, method, path, **kw):
        assert path == "/agents/me/claim"
        return Resp(204)


def test_cancelled_dependency_is_not_open():
    task = {"task_nr": "TRB-0004", "status": "defined"}
    deps = [{"task_nr": "TRB-0001", "status": "cancelled", "title": "x", "resolved": True},
            {"task_nr": "TRB-0002", "status": "done", "title": "x", "resolved": True}]
    client = SkipClient([task], {"TRB-0004": {"project_info": {"key": "TRB", "status": "active"}, "dependencies": deps}})
    assert "dependencies" not in runner._skip_reason(client, task, [], 2)
    deps[0].pop("resolved")  # an older server only sends the status
    assert "dependencies" not in runner._skip_reason(client, task, [], 2)
    deps.append({"task_nr": "TRB-0003", "status": "for_review", "title": "x", "resolved": False})
    assert "open dependencies: TRB-0003 (for_review)" in runner._skip_reason(client, task, [], 2)


def test_runner_logs_skipped_tasks(tmp_path):
    root = tmp_path / "work"
    write_config(root, ["true"], mode="foreground")
    items = [{"task_nr": "TRB-0001", "status": "draft", "due_date": None},
             {"task_nr": "TRB-0002", "status": "defined", "due_date": "2026-10-31"},
             {"task_nr": "TRB-0003", "status": "defined", "due_date": None}]
    details = {"TRB-0002": {"project_info": {"key": "TRB", "status": "paused"}, "dependencies": []},
               "TRB-0003": {"project_info": {"key": "TRB", "status": "active"},
                            "dependencies": [{"task_nr": "TRB-0001", "status": "draft", "title": "x"}]}}
    msg = runner.run_once(SkipClient(items, details), root)
    assert msg.startswith("No task – TRB-0001 skipped: draft")
    log = (root / "logs" / "runner.log").read_text()
    assert "TRB-0001 skipped: draft – only claimed after it is published" in log
    assert "due" not in log.lower()
    assert "TRB-0002 skipped: project TRB is not active ('paused')\n" in log
    assert "TRB-0003 skipped: open dependencies: TRB-0001 (draft)" in log

    running = [{"task_nr": "TRB-0009", "status": "in_progress"}, {"task_nr": "TRB-0002", "status": "defined"}]
    lock = runner.acquire_session_lock(root, "TRB-0009")  # Its session is running, so it is not resumed.
    try:
        assert "max_parallel_tasks=1 reached (in progress: TRB-0009)" in runner.run_once(SkipClient(running), root)
    finally:
        runner.release_session_lock(lock)


def test_skipped_tasks_never_breaks_runner():
    class Broken:
        def get(self, path):
            raise ApiError(500, "error", "kaputt")
    assert runner.skipped_tasks(Broken()) == []


def test_runner_explains_wait_for_review(tmp_path):
    root = tmp_path / "work"
    write_config(root, ["true"], mode="foreground")
    items = [{"task_nr": "TRB-0004", "status": "for_review", "project": "TRB"},
             {"task_nr": "TRB-0005", "status": "defined", "project": "TRB"}]
    details = {"TRB-0005": {"project_info": {"key": "TRB", "status": "active", "serialize_reviews": True},
                            "dependencies": []}}
    msg = runner.run_once(SkipClient(items, details), root)
    assert "TRB-0005 skipped: waits until TRB-0004 has been reviewed (project TRB: one review at a time)" in msg
    details["TRB-0005"]["project_info"]["serialize_reviews"] = False
    assert "reason unknown" in runner.run_once(SkipClient(items, details), root)


# --- prerequisites, start failures, resuming own tasks --------------------------------------------------------------

def test_missing_tmux_or_agent_stops_before_claim(tmp_path, monkeypatch, origin):  # noqa: F811
    root = tmp_path / "work"
    empty = tmp_path / "empty-bin"
    empty.mkdir()
    monkeypatch.setenv("PATH", str(empty))
    write_config(root, ["claude", "{prompt}"], mode="tmux")
    fc = RunnerClient([task("KET-0016", "task/KET-0016-x", origin.as_uri())])
    monkeypatch.setattr(runner.sys, "platform", "darwin")
    with pytest.raises(runner.RunnerError) as e:
        runner.run_once(fc, root)
    msg = str(e.value)
    assert "no task claimed" in msg and "brew install tmux" in msg
    assert "claude not found in PATH" in msg and "npm install -g @anthropic-ai/claude-code" in msg
    assert fc.calls == []  # No claim.

    monkeypatch.setattr(runner.sys, "platform", "linux")
    write_config(root, ["/opt/nowhere/my-agent"], mode="foreground")  # Foreground: no tmux needed.
    with pytest.raises(runner.RunnerError) as e:
        runner.run_once(fc, root)
    assert "tmux" not in str(e.value).split("PATH:")[0] and "/opt/nowhere/my-agent not found" in str(e.value)
    write_config(root, ["claude"], mode="tmux")
    with pytest.raises(runner.RunnerError, match="sudo apt install tmux"):
        runner.run_once(fc, root)
    assert fc.calls == []


def test_tm_run_cli_exit_code_without_traceback(tmp_path, monkeypatch):
    from typer.testing import CliRunner

    from cli import tm
    root = tmp_path / "work"
    write_config(root, ["no-such-agent-tm-test"], mode="foreground")
    monkeypatch.setattr(tm, "Client", lambda cfg: RunnerClient([]))
    r = CliRunner().invoke(tm.app, ["run", "--work-root", str(root)])
    assert r.exit_code == 1 and "no-such-agent-tm-test not found in PATH" in r.output
    assert r.exception is None or isinstance(r.exception, SystemExit)


def test_start_failure_after_claim_gives_task_back(tmp_path, monkeypatch, origin):  # noqa: F811
    """The Mac mini case: tmux vanishes between check and start → release with reason, RunnerError (no traceback)."""
    root = tmp_path / "work"
    write_config(root, ["sh", "-c", "true"], mode="tmux")
    monkeypatch.setattr(runner.shutil, "which", lambda name: f"/usr/bin/{name}")

    def no_tmux(*args):
        raise FileNotFoundError(2, "No such file or directory", "tmux")
    monkeypatch.setattr(runner, "_tmux", no_tmux)
    fc = RunnerClient([task("KET-0017", "task/KET-0017-x", origin.as_uri())])
    with pytest.raises(runner.RunnerError, match="KET-0017: The runner could not start the agent.*given back"):
        runner.run_once(fc, root)
    method, path, body = fc.calls[-1]
    assert path == "/tasks/KET-0017/release" and "No such file or directory" in body["reason"]
    assert "could not start the agent" in (root / "logs" / "runner.log").read_text()


def test_session_agent_not_found_gives_task_back(tmp_path, origin, capsys):  # noqa: F811
    root = tmp_path / "work"
    write_config(root, ["unused"], mode="foreground")
    t = task("KET-0018", "task/KET-0018-x", origin.as_uri())
    fc = RunnerClient([t])
    from cli.prepare import prepare
    prepare(fc, root)
    assert runner.session(fc, "KET-0018", root, ["no-such-agent-tm-test", "x"]) == 127
    assert "could not be started" in capsys.readouterr().err
    method, path, body = fc.calls[-1]
    assert path == "/tasks/KET-0018/release" and "no-such-agent-tm-test" in body["reason"]
    assert runner.acquire_session_lock(root, "KET-0018") is not None  # Lock is released again.


def test_session_holds_lock_and_refuses_second_run(tmp_path, origin):  # noqa: F811
    root = tmp_path / "work"
    write_config(root, ["unused"], mode="foreground")
    fc = RunnerClient([task("KET-0019", "task/KET-0019-x", origin.as_uri())])
    from cli.prepare import prepare
    prepare(fc, root)
    assert not runner.session_running(root, "KET-0019")
    held = runner.acquire_session_lock(root, "KET-0019")
    try:
        assert runner.session_running(root, "KET-0019")
        with pytest.raises(runner.RunnerError, match="already running"):
            runner.session(fc, "KET-0019", root, ["true"])
    finally:
        runner.release_session_lock(held)
    # During the run the session holds the lock: the agent's own attempt fails (exit 1); afterwards it is free.
    agent = [sys.executable, "-c", "import fcntl,sys; f=open(sys.argv[1],'a'); "
             "fcntl.flock(f, fcntl.LOCK_EX|fcntl.LOCK_NB)", str(root / ".locks" / "session-KET-0019.lock")]
    assert runner.session(fc, "KET-0019", root, agent) == 1
    assert not runner.session_running(root, "KET-0019")


class ResumeClient(RunnerClient):
    """An own in-progress task (e.g. the machine rebooted during a run); claim would deliver nothing."""

    def __init__(self, own):
        super().__init__([own])
        self.tasks = []

    def get(self, path):
        if path == "/agents/me/tasks?status=in_progress":
            return {"items": [{"task_nr": nr, "status": "in_progress"} for nr in self.by_nr]}
        if path.startswith("/agents/me/tasks"):
            return {"items": []}
        return super().get(path)


def test_run_resumes_own_task_without_session(tmp_path, origin):  # noqa: F811
    root = tmp_path / "work"
    write_config(root, ["sh", "-c", "echo agent"], mode="foreground")
    fc = ResumeClient(task("KET-0020", "task/KET-0020-x", origin.as_uri()))
    msg = runner.run_once(fc, root)
    assert msg.startswith("KET-0020 resumed (own task in progress without a running session)")
    assert "KET-0020 finished" in msg
    assert ("POST", "/agents/me/claim", None) not in fc.calls
    assert ("POST", "/tasks/KET-0020/heartbeat", None) in fc.calls  # Lease refreshed like prepare --resume-own.
    assert (root / "KET" / ".worktrees" / "KET-0020" / "TASK.md").exists()


def test_run_does_not_start_second_run_while_session_is_active(tmp_path, origin):  # noqa: F811
    root = tmp_path / "work"
    write_config(root, ["sh", "-c", "echo agent"], mode="foreground")
    fc = ResumeClient(task("KET-0021", "task/KET-0021-x", origin.as_uri()))
    held = runner.acquire_session_lock(root, "KET-0021")  # `tbr session` of the first run.
    try:
        assert runner.run_once(fc, root) == "No task"
    finally:
        runner.release_session_lock(held)
    assert [c[1] for c in fc.calls] == ["/agents/me/claim"]  # Only a regular claim, no resume, no new session.
    assert not list(root.glob("logs/KET-0021-*.log"))



class WaitingClient(ResumeClient):
    """An own in-progress task whose agent reported waiting for a background run and ended its session."""

    def __init__(self, own, waiting):
        super().__init__(own)
        self.waiting = waiting

    def get(self, path):
        if path == "/agents/me/tasks?status=in_progress":
            return {"items": [{"task_nr": nr, "status": "in_progress", "waiting": self.waiting} for nr in self.by_nr]}
        if path.startswith("/tasks/"):
            return {**super().get(path), "waiting": self.waiting}
        return super().get(path)


def test_finish_parks_a_waiting_task(tmp_path, origin):  # noqa: F811
    root = tmp_path / "work"
    t = task("KET-0022", "task/KET-0022-x", origin.as_uri())
    from cli.prepare import prepare
    wt = prepare(RunnerClient([t]), root).worktree
    fc = WaitingClient(t, {"reason": "model test", "until": "2026-10-13T12:00:00", "watch_file": None})
    msg = runner.finish(fc, "KET-0022", wt, 0)
    assert "waits for a background run (model test)" in msg
    assert fc.calls == []  # No release, no submit: the task stays in progress.


def test_run_keeps_a_parked_task_and_resumes_it_when_its_log_is_silent(tmp_path, origin):  # noqa: F811
    root = tmp_path / "work"
    write_config(root, ["sh", "-c", "echo agent"], mode="foreground", stall_minutes=30)
    log = tmp_path / "run.log"
    log.write_text("step 1\n")
    fc = WaitingClient(task("KET-0023", "task/KET-0023-x", origin.as_uri()),
                       {"reason": "model test", "until": "2026-10-13T12:00:00", "watch_file": str(log)})
    msg = runner.run_once(fc, root)
    assert msg.startswith("No task") and "KET-0023 waits for a background run" in msg
    assert ("POST", "/tasks/KET-0023/heartbeat", None) in fc.calls  # Lease kept alive …
    assert not list(root.glob("logs/KET-0023-*.log"))  # … but no new session.

    import os
    old = time.time() - 31 * 60
    os.utime(log, (old, old))  # The background run stopped writing: a new session has to look.
    fc.calls.clear()
    msg = runner.run_once(fc, root)
    assert msg.startswith("KET-0023 resumed")


def test_run_resumes_when_waiting_has_ended(tmp_path, origin):  # noqa: F811
    root = tmp_path / "work"
    write_config(root, ["sh", "-c", "echo agent"], mode="foreground")
    fc = WaitingClient(task("KET-0024", "task/KET-0024-x", origin.as_uri()), None)  # Expired: the server sends None.
    assert runner.run_once(fc, root).startswith("KET-0024 resumed")


def test_watch_file_silent(tmp_path):
    log = tmp_path / "run.log"
    assert not runner.watch_file_silent({"watch_file": None}, 60)
    assert runner.watch_file_silent({"watch_file": str(log)}, 60)  # Missing file: let a session look.
    log.write_text("x")
    assert not runner.watch_file_silent({"watch_file": str(log)}, 60)
    assert not runner.watch_file_silent({"watch_file": str(log)}, 0)

# --- stop after the task ended, start check, stall, window cleanup --------------------------------------------------

class FakeTmux:
    """Stand-in for `runner._tmux`: window listing and pane content (a string or a callable), records calls."""

    def __init__(self, windows="", pane=""):
        self.windows, self.pane, self.calls = windows, pane, []

    def __call__(self, *args):
        self.calls.append(args)
        out = ""
        if args[0] == "list-windows":
            out = self.windows
        elif args[0] == "capture-pane":
            out = self.pane() if callable(self.pane) else self.pane
        return subprocess.CompletedProcess(args, 0, out, "")

    def sent(self):
        return [c for c in self.calls if c[0] == "send-keys"]


class Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


PROMPT = ("You are the TaskBrigade agent hermes. The current directory contains TASK.md with task KET-0030, "
          "which has already been claimed for you.")
LOGIN_SCREEN = f"""╭───────────────────────────────╮
│ ✻ Welcome to Claude Code!     │
╰───────────────────────────────╯
 Login expired · Please run /login
╭──────────────────────────────────────────────────────────────╮
│ > {PROMPT[:60]}
│   {PROMPT[60:]}
╰──────────────────────────────────────────────────────────────╯
"""
UNSENT_SCREEN = f"""╭───────────────────────────────╮
│ ✻ Welcome to Claude Code!     │
╰───────────────────────────────╯
╭──────────────────────────────────────────────────────────────╮
│ > {PROMPT[:60]}
│   {PROMPT[60:]}
╰──────────────────────────────────────────────────────────────╯
  ? for shortcuts
"""
WORKING_SCREEN = f"""> {PROMPT[:60]}
  {PROMPT[60:]}

⏺ Reading TASK.md …
  Read 77 lines (ctrl+r to expand)

⏺ The task mentions a rate limit for the API.

✶ Thinking… (12s · esc to interrupt)
╭──────────────────────────────────────────────────────────────╮
│ >
╰──────────────────────────────────────────────────────────────╯
"""


@pytest.mark.parametrize("screen,agent,expected", [
    (LOGIN_SCREEN, "claude", "login"),
    ("Not logged in · Run /login\n> ", "claude", "login"),
    ("  Sign in with ChatGPT\n  Provide your own API key\n", "codex", "login"),
    ("⎿ Claude usage limit reached. Your limit will reset at 5pm\n> ", "claude", "limit"),
    ("■ You've hit your usage limit. Try again later.\n› ", "codex", "limit"),
    (UNSENT_SCREEN, "claude", "unsent"),
    (WORKING_SCREEN, "claude", None),  # Busy and answered: 'rate limit' in the output is no blocker.
    (WORKING_SCREEN.replace("✶ Thinking… (12s · esc to interrupt)", ""), "claude", None),  # Answered, now idle.
    ("", "claude", None),
])
def test_classify_screen(screen, agent, expected):
    found = runner.classify_screen(screen, agent, PROMPT)
    assert (found[0] if found else None) == expected


def _supervisor(fc, tmp_path, pane="%1", clock=None, **cfg):
    return runner.Supervisor(fc, "KET-0030", tmp_path, cfg, ["claude", "--x", PROMPT], pane, check_s=60,
                             clock=clock or Clock())


def test_supervisor_start_check_login_blocks(tmp_path, monkeypatch):
    tmux = FakeTmux(pane=LOGIN_SCREEN)
    monkeypatch.setattr(runner, "_tmux", tmux)
    clock = Clock()
    sup = _supervisor(RunnerClient([task("KET-0030", "b", "x")]), tmp_path, clock=clock)
    assert sup.check() is None  # Too early for the start check.
    clock.t += 60
    reason, kind = sup.check()
    assert kind == "login" and "hermes: login expired or not logged in – run `claude /login` on" in reason
    assert tmux.sent() == []


def test_supervisor_sends_enter_for_unsent_prompt(tmp_path, monkeypatch):
    screens = [UNSENT_SCREEN, WORKING_SCREEN]
    tmux = FakeTmux(pane=lambda: screens[0])
    monkeypatch.setattr(runner, "_tmux", tmux)
    clock = Clock()
    sup = _supervisor(RunnerClient([task("KET-0030", "b", "x")]), tmp_path, clock=clock, start_check_seconds=30)
    clock.t += 30
    assert sup.check() is None and tmux.sent() == [("send-keys", "-t", "%1", "Enter")]
    assert sup.due() is False
    clock.t += 10  # Re-checked soon after the Enter.
    assert sup.due()
    screens.pop(0)
    assert sup.check() is None and len(tmux.sent()) == 1 and sup.start_checked


def test_supervisor_unsent_after_two_enters_blocks(tmp_path, monkeypatch):
    tmux = FakeTmux(pane=UNSENT_SCREEN)
    monkeypatch.setattr(runner, "_tmux", tmux)
    clock = Clock()
    sup = _supervisor(RunnerClient([task("KET-0030", "b", "x")]), tmp_path, clock=clock)
    results = []
    for _step in range(4):
        clock.t += 60
        results.append(sup.check())
    assert len(tmux.sent()) == 2  # At most two attempts.
    assert results[:2] == [None, None] and results[2][1] == "unsent" and "start prompt stayed unsent" in results[2][0]


def test_supervisor_start_check_off_and_foreground(tmp_path, monkeypatch):
    tmux = FakeTmux(pane=LOGIN_SCREEN)
    monkeypatch.setattr(runner, "_tmux", tmux)
    clock = Clock()
    fc = RunnerClient([task("KET-0030", "b", "x")])
    off = _supervisor(fc, tmp_path, clock=clock, start_check_seconds=0)
    no_pane = _supervisor(fc, tmp_path, pane=None, clock=clock)
    clock.t += 120
    assert off.check() is None and no_pane.check() is None


def test_supervisor_stall_comments_once_without_stopping(tmp_path, monkeypatch):
    screen = ["⏺ Waiting for permission to run npm test\n Do you want to proceed?\n ❯ 1. Yes\n"]
    monkeypatch.setattr(runner, "_tmux", FakeTmux(pane=lambda: screen[0]))
    clock = Clock()
    fc = RunnerClient([task("KET-0030", "b", "x")])
    sup = _supervisor(fc, tmp_path, clock=clock, stall_minutes=30)
    comments = lambda: [c for c in fc.calls if c[1] == "/tasks/KET-0030/comments"]  # noqa: E731
    for _step in range(29):
        clock.t += 60
        assert sup.check() is None
    assert comments() == []
    for _step in range(5):
        clock.t += 60
        assert sup.check() is None  # Never stops the agent.
    assert len(comments()) == 1
    body = comments()[0][2]["body"]
    assert "No activity in the agent window since" in body and "(30 min)" in body and "npm test" in body
    screen[0] = "something new"  # Activity again, then a new stall → a new note.
    clock.t += 60
    sup.check()
    clock.t += 31 * 60
    sup.check()
    assert len(comments()) == 2


def test_supervisor_no_stall_note_after_task_ended(tmp_path, monkeypatch):
    monkeypatch.setattr(runner, "_tmux", FakeTmux(pane="idle"))
    clock = Clock()
    fc = RunnerClient([task("KET-0030", "b", "x")], status="for_review")
    sup = _supervisor(fc, tmp_path, clock=clock, stall_minutes=1, stop_after_finish_seconds=0)
    clock.t += 3600
    assert sup.check() is None and not [c for c in fc.calls if c[1].endswith("/comments")]


class StatusClient(RunnerClient):
    def __init__(self, tasks, status="in_progress", assigned_to="hermes"):
        super().__init__(tasks, status)
        self.assigned_to = assigned_to

    def get(self, path):
        out = super().get(path)
        return {**out, "assigned_to": self.assigned_to} if path.startswith("/tasks/") else out


@pytest.mark.parametrize("status,assigned_to,shown", [
    ("for_review", "hermes", "for_review"),   # tbr submit
    ("questions", "hermes", "questions"),     # tbr ask
    ("aborted", "hermes", "aborted"),         # tbr abort
    ("defined", "hermes", "defined"),         # tbr release
    ("in_progress", "codex", "reassigned"),   # reassigned to another agent
])
def test_session_stops_agent_after_task_left_in_progress(tmp_path, origin, capsys, status, assigned_to,  # noqa: F811
                                                         shown):
    root = tmp_path / "work"
    write_config(root, ["unused"], mode="foreground", stop_after_finish_seconds=0.3)
    fc = StatusClient([task("KET-0031", "task/KET-0031-x", origin.as_uri())], status, assigned_to)
    from cli.prepare import prepare
    prepare(fc, root)
    started = time.monotonic()
    # Claude Code stays open after tbr submit and waits for input.
    rc = runner.session(fc, "KET-0031", root, ["sleep", "600"], poll_s=0.1, check_s=0.1, pane="")
    assert time.monotonic() - started < 20 and rc != 0
    out = capsys.readouterr().out
    assert f"KET-0031 is {shown} – stopping the agent in 0 s" in out
    assert f"KET-0031 is {shown} – agent stopped" in out
    assert "the agent finished it itself" not in out  # No second closing step through finish.
    assert not [c for c in fc.calls if c[1].endswith(("/submit", "/release"))]


def test_session_stop_after_finish_zero_keeps_agent(tmp_path, origin, capsys):  # noqa: F811
    root = tmp_path / "work"
    write_config(root, ["unused"], mode="foreground", stop_after_finish_seconds=0)
    fc = RunnerClient([task("KET-0032", "task/KET-0032-x", origin.as_uri())], status="for_review")
    from cli.prepare import prepare
    prepare(fc, root)
    assert runner.session(fc, "KET-0032", root, ["sleep", "1"], poll_s=0.1, check_s=0.1, pane="") == 0
    out = capsys.readouterr().out
    assert "agent stopped" not in out and "the agent finished it itself" in out


def test_session_blocker_releases_and_blocks_runner(tmp_path, origin, capsys, monkeypatch):  # noqa: F811
    root = tmp_path / "work"
    write_config(root, ["true"], mode="tmux", start_check_seconds=0.2)
    t = task("KET-0033", "task/KET-0033-x", origin.as_uri())
    fc = RunnerClient([t])
    from cli.prepare import prepare
    prepare(fc, root)
    monkeypatch.setattr(runner, "_tmux", FakeTmux(pane="Not logged in · Please run /login\n> "))
    # The agent command must exist: a shell script named claude that waits like the real one.
    fake = tmp_path / "bin" / "claude"
    fake.parent.mkdir()
    fake.write_text("#!/bin/sh\nexec sleep 600\n")
    fake.chmod(0o755)
    started = time.monotonic()
    rc = runner.session(fc, "KET-0033", root, [str(fake), PROMPT], poll_s=0.1, check_s=0.1, pane="%5")
    assert time.monotonic() - started < 30 and rc != 0
    releases = [c for c in fc.calls if c[1] == "/tasks/KET-0033/release"]
    assert len(releases) == 1 and "hermes: login expired" in releases[0][2]["reason"]
    assert "claude /login" in releases[0][2]["reason"]
    assert not [c for c in fc.calls if c[1].endswith("/submit")]
    block = runner.read_block(root)
    assert block["kind"] == "login" and block["task_nr"] == "KET-0033"
    assert "no new tasks until tbr doctor reports the agent ready" in capsys.readouterr().out
    # The next runner pass claims nothing while the block is set.
    fc2 = RunnerClient([task("KET-0034", "task/KET-0034-x", origin.as_uri())])
    msg = runner.run_once(fc2, root)
    assert msg.startswith("Agent blocked (hermes: login expired") and fc2.calls == []
    runner.clear_block(root)
    write_config(root, ["true"], mode="foreground")
    assert "KET-0034 finished" in runner.run_once(RunnerClient([task("KET-0034", "task/KET-0034-x",
                                                                     origin.as_uri())]), root)


WINDOWS = ("@1\trunner\t1\t1000\n"           # start window: never
           "@2\tKET-0040\t1\t1000\n"         # finished long ago → close
           "@3\tKET-0041\t1\t{recent}\n"     # finished recently → keep
           "@4\tKET-0042\t0\t\n"             # agent running → keep
           "@5\tKET-0043\t1\t\n")            # finished, tmux without pane_dead_time → age from the run log


def test_cleanup_closes_only_old_finished_windows(tmp_path, monkeypatch):
    root = tmp_path / "work"
    (root / "logs").mkdir(parents=True)
    now = 1000 + 30 * 3600
    tmux = FakeTmux(windows=WINDOWS.format(recent=now - 3600), pane="final screen\nPane is dead\n")
    monkeypatch.setattr(runner, "_tmux", tmux)
    log40 = root / "logs" / "KET-0040-20261009-100000.log"
    log40.write_text("agent output\n")
    log43 = root / "logs" / "KET-0043-20261009-100000.log"
    log43.write_text("x\n")
    import os
    os.utime(log43, (now - 25 * 3600, now - 25 * 3600))
    closed = runner.cleanup_windows(root, "taskbrigade", 24, now=now)
    assert closed == ["KET-0040", "KET-0043"]
    assert [c[2] for c in tmux.calls if c[0] == "kill-window"] == ["@2", "@5"]
    text = log40.read_text()
    assert text.startswith("agent output\n") and "tmux window KET-0040 closed" in text and "final screen" in text
    captures = [c for c in tmux.calls if c[0] == "capture-pane"]
    assert all("-S" in c and "-" in c for c in captures)  # Whole history, before kill-window.
    assert tmux.calls.index(captures[0]) < tmux.calls.index(next(c for c in tmux.calls if c[0] == "kill-window"))
    assert "tmux window KET-0040 closed" in (root / "logs" / "runner.log").read_text()
    # 0 = never
    tmux.calls.clear()
    assert runner.cleanup_windows(root, "taskbrigade", 0, now=now) == [] and tmux.calls == []


def test_cleanup_never_disturbs_runner(tmp_path, monkeypatch):
    root = tmp_path / "work"
    root.mkdir()

    def broken(*args):
        raise FileNotFoundError(2, "No such file or directory", "tmux")
    monkeypatch.setattr(runner, "_tmux", broken)
    assert runner.cleanup_windows(root, "taskbrigade", 24) == []
    assert "Cleaning up tmux windows failed" in (root / "logs" / "runner.log").read_text()
    write_config(root, ["true"], mode="tmux")
    monkeypatch.setattr(runner.shutil, "which", lambda name: f"/usr/bin/{name}")
    assert runner.run_once(RunnerClient([]), root) == "No task"  # The pass goes on.


def test_tm_cleanup_command(tmp_path, monkeypatch):
    from typer.testing import CliRunner

    from cli import tm
    root = tmp_path / "work"
    write_config(root, ["claude"], mode="tmux")
    tmux = FakeTmux(windows=WINDOWS.format(recent=int(time.time()) - 60))
    monkeypatch.setattr(runner, "_tmux", tmux)

    class Client(RunnerClient):
        def get(self, path):
            if path == "/agents/me/tasks?status=in_progress":
                return {"items": []}
            return super().get(path)
    monkeypatch.setattr(tm, "Client", lambda cfg: Client([]))
    r = CliRunner().invoke(tm.app, ["cleanup", "--work-root", str(root)])
    # Without a minimum age even a finished window of unknown age (no pane_dead_time, no log) is closed.
    assert r.exit_code == 0 and "3 window(s) closed: KET-0040, KET-0041, KET-0043" in r.output
    r = CliRunner().invoke(tm.app, ["cleanup", "--work-root", str(root), "--stale", "--min-age-hours", "1000000"])
    assert "1 window(s) closed: KET-0042" in r.output  # Agent left running although its task ended.


def test_cleanup_in_session_shared_by_two_agents(tmp_path, monkeypatch):
    """claude-code and codex share one tmux session: cleanup (also --stale) closes only the own agent's windows."""
    from typer.testing import CliRunner

    from cli import tm
    mine, other = tmp_path / "claude-code", tmp_path / "codex"
    write_config(mine, ["claude"], mode="tmux")
    other.mkdir()
    windows = (f"@1\trunner\t0\t\t\t{other}\n"
               f"@2\tKET-0060\t0\t\t{mine}\t{mine}/KET/.worktrees/KET-0060\n"         # own, task ended → stale
               f"@3\tKET-0061\t0\t\t{other}\t{other}/KET/.worktrees/KET-0061\n"       # codex's, in progress there
               f"@4\tKET-0062\t1\t1000\t{other}\t{other}/KET/.worktrees/KET-0062\n"   # codex's, finished
               f"@5\tKET-0063\t0\t\t\t{other}/KET/.worktrees/KET-0063\n"              # codex's, untagged (older tbr)
               f"@6\tKET-0064\t1\t1000\t\t{mine}/KET/.worktrees/KET-0064\n")          # own, untagged, finished
    tmux = FakeTmux(windows=windows)
    monkeypatch.setattr(runner, "_tmux", tmux)

    class Client(RunnerClient):
        def get(self, path):
            if path == "/agents/me/tasks?status=in_progress":
                return {"items": []}  # None of claude-code's tasks is in progress; codex's are not in its list.
            return super().get(path)
    monkeypatch.setattr(tm, "Client", lambda cfg: Client([]))
    assert runner.stale_windows(Client([]), "taskbrigade", mine) == ["KET-0060"]
    r = CliRunner().invoke(tm.app, ["cleanup", "--work-root", str(mine), "--stale"])
    assert r.exit_code == 0 and "2 window(s) closed: KET-0060, KET-0064" in r.output
    assert [c[2] for c in tmux.calls if c[0] == "kill-window"] == ["@2", "@6"]
    # The runner's own pass leaves codex's finished window alone, too.
    tmux.calls.clear()
    assert runner.cleanup_windows(mine, "taskbrigade", 1, now=1000 + 2 * 3600) == ["KET-0064"]


def test_doctor_reports_a_parked_task_as_ok(tmp_path):
    from cli.doctor import task_checks

    class C:
        def get(self, path):
            return {"items": [
                {"task_nr": "KET-0025", "status": "in_progress", "lease_expires_at": None,
                 "waiting": {"reason": "model test", "until": "2026-10-13T12:00:00", "watch_file": None}},
                {"task_nr": "KET-0026", "status": "in_progress", "lease_expires_at": None, "waiting": None}]}

    checks = {c.label: c for c in task_checks(C(), tmp_path, {"mode": "foreground"})}
    assert checks["KET-0025"].status == "ok" and "model test" in checks["KET-0025"].detail
    assert checks["KET-0026"].status == "warn"
