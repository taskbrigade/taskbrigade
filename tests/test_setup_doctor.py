"""`tbr setup` (runner section, prerequisites, systemd/launchd timer), `tbr join --setup` and `tbr doctor`."""
import plistlib
import shutil
import stat
import subprocess
import tomllib

import httpx
import pytest
from typer.testing import CliRunner

from app.web.admin_agents import join_commands
from cli import doctor as doctor_mod
from cli import runner as runner_mod
from cli import setup as setup_mod
from cli import tm

runner = CliRunner()
SERVER = "https://tm.example"
API = f"{SERVER}/api/v1"
GITEA = "http://gitea.example.internal:3001"
ME = {"name": "claude-code", "agent_kind": "claude_code", "runtime": "cloud", "role": "worker", "is_active": True}


@pytest.fixture
def tools():
    return {"tbr", "tm", "git", "tmux", "claude", "codex", "opencode", "env"}


@pytest.fixture
def home(tmp_path, monkeypatch, tools):
    """Isolated HOME/XDG and fake programs: tbr, tm, git, tmux and the agent CLIs are 'installed' in /opt/bin."""
    h = tmp_path / "home"
    h.mkdir()
    monkeypatch.setenv("HOME", str(h))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(h / ".config"))
    monkeypatch.delenv("CODEX_HOME", raising=False)
    monkeypatch.delenv("TBR_URL", raising=False)
    monkeypatch.delenv("TBR_TOKEN", raising=False)
    monkeypatch.setattr(shutil, "which", lambda name, *a, **k: f"/opt/bin/{name}" if name in tools else None)
    monkeypatch.setattr(setup_mod, "_run", lambda cmd: subprocess.CompletedProcess(cmd, 0, "", ""))
    return h


def work_dir(home, name="claude-code", extra=""):
    root = home / "taskbrigade-work" / name
    root.mkdir(parents=True)
    (root / ".taskbrigade.toml").write_text(f'url = "{SERVER}"\ntoken = "tm_secret"\n{extra}')
    return root


# --- tbr setup -------------------------------------------------------------------------------------------------------

def test_replace_runner_keeps_other_tables():
    text = ('url = "x"\ntoken = "t"\n\n[runner]\ncommand = ["codex",\n  "-c", "x"]\nmode = "tmux"\n\n'
            '[other]\nkey = 1\n')
    out = setup_mod.replace_runner(text, setup_mod.RUNNER_SECTIONS["claude_code"])
    data = tomllib.loads(out)
    assert data["runner"]["command"][0] == "claude" and data["other"] == {"key": 1} and data["token"] == "t"


def test_setup_claude_code_without_timer(home):
    root = work_dir(home, extra='\n[runner]\ncommand = ["my-agent"]\n')
    r = runner.invoke(tm.app, ["setup", "claude-code", "--work-root", str(root), "--no-timer"])
    assert r.exit_code == 0, r.output
    data = tomllib.loads((root / ".taskbrigade.toml").read_text())
    assert data["token"] == "tm_secret" and data["runner"]["command"][0] == "claude"
    assert data["runner"]["mode"] == "tmux"
    backup = root / ".taskbrigade.toml.bak"
    assert "my-agent" in backup.read_text() and stat.S_IMODE(backup.stat().st_mode) == 0o600
    assert stat.S_IMODE((root / ".taskbrigade.toml").stat().st_mode) == 0o600
    assert "not installed (--no-timer)" in r.stdout and f"run --work-root {root}" in r.stdout
    assert "start claude once interactively" in r.stdout  # no ~/.claude.json yet
    assert "tm_secret" not in r.output
    assert not (home / ".config" / "systemd").exists()


def test_setup_reports_missing_tmux(home, tools):
    tools.discard("tmux")
    root = work_dir(home)
    r = runner.invoke(tm.app, ["setup", "claude-code", "--work-root", str(root), "--no-timer"])
    assert r.exit_code == 1
    assert "[error]" in r.stdout and "tmux" in r.stdout and "install tmux" in r.stdout


def test_setup_requires_joined_directory_and_known_kind(home, tmp_path):
    r = runner.invoke(tm.app, ["setup", "claude-code", "--work-root", str(tmp_path / "nothing"), "--no-timer"])
    assert r.exit_code == 1 and "tbr join" in r.stderr
    root = work_dir(home)
    r = runner.invoke(tm.app, ["setup", "aider", "--work-root", str(root), "--no-timer"])
    assert r.exit_code == 1 and "claude-code, codex, opencode" in r.stderr


def test_setup_codex_disables_update_dialog(home):
    (home / ".codex").mkdir()
    (home / ".codex" / "config.toml").write_text('model = "x"\n\n[projects."/a"]\ntrust_level = "trusted"\n')
    root = work_dir(home, "codex")
    r = runner.invoke(tm.app, ["setup", "codex", "--work-root", str(root), "--no-timer"])
    assert r.exit_code == 0, r.output
    data = tomllib.loads((home / ".codex" / "config.toml").read_text())
    assert data["check_for_update_on_startup"] is False and data["model"] == "x" and "/a" in data["projects"]
    assert tomllib.loads((root / ".taskbrigade.toml").read_text())["runner"]["git"] == "runner"


def test_setup_opencode_writes_own_config(home, tools):
    root = work_dir(home, "opencode")
    r = runner.invoke(tm.app, ["setup", "opencode", "--work-root", str(root), "--no-timer"])
    assert r.exit_code == 0, r.output
    cmd = tomllib.loads((root / ".taskbrigade.toml").read_text())["runner"]["command"]
    assert cmd[:2] == ["env", f"OPENCODE_CONFIG={root / 'opencode.json'}"] and cmd[2] == "opencode"
    assert '"tbr *": "allow"' in (root / "opencode.json").read_text()
    tools.discard("opencode")  # `env` is found, the agent behind it is not.
    r = runner.invoke(tm.app, ["setup", "opencode", "--work-root", str(root), "--no-timer"])
    assert r.exit_code == 1 and "opencode-ai" in r.stdout


def test_setup_installs_systemd_timer(home, monkeypatch):
    calls = []

    def fake_run(cmd):
        calls.append(cmd)
        return subprocess.CompletedProcess(cmd, 0, "no\n" if cmd[0] == "loginctl" else "", "")

    monkeypatch.setattr(setup_mod, "_run", fake_run)
    monkeypatch.setattr(setup_mod.sys, "platform", "linux")
    monkeypatch.setenv("USER", "agent")
    root = work_dir(home)
    checks = setup_mod.setup(root, "claude-code", agent_name="claude-code")
    units = home / ".config" / "systemd" / "user"
    service = (units / "tbr-runner-claude-code.service").read_text()
    timer = (units / "tbr-runner-claude-code.timer").read_text()
    assert f'ExecStart="/opt/bin/tbr" run --work-root "{root}"' in service
    assert "KillMode=process" in service and "/opt/bin:" in service
    assert "OnUnitActiveSec=10min" in timer and "OnActiveSec=1min" in timer
    assert ["systemctl", "--user", "enable", "--now", "tbr-runner-claude-code.timer"] in calls
    linger = [c for c in checks if c.label == "Linger"][0]
    assert linger.status == "warn" and "sudo loginctl enable-linger agent" in linger.hint
    assert not any(c.status == "error" for c in checks)


def test_setup_systemd_failure_suggests_no_timer(home, monkeypatch):
    monkeypatch.setattr(setup_mod, "_run", lambda cmd: subprocess.CompletedProcess(cmd, 1, "", "Failed to connect to bus"))
    monkeypatch.setattr(setup_mod.sys, "platform", "linux")
    checks = setup_mod.setup(work_dir(home), "claude-code")
    timer = [c for c in checks if c.label == "Timer"][0]
    assert timer.status == "error" and "Failed to connect to bus" in timer.detail and "--no-timer" in timer.hint


def test_setup_installs_launchd_agent(home, monkeypatch):
    calls = []
    monkeypatch.setattr(setup_mod, "_run", lambda cmd: calls.append(cmd) or subprocess.CompletedProcess(cmd, 0, "", ""))
    monkeypatch.setattr(setup_mod.sys, "platform", "darwin")
    root = work_dir(home)
    checks = setup_mod.setup(root, "claude-code", agent_name="claude-code")
    plist = home / "Library" / "LaunchAgents" / "com.taskbrigade.runner.claude-code.plist"
    data = plistlib.loads(plist.read_bytes())
    assert data["ProgramArguments"] == ["/opt/bin/tbr", "run", "--work-root", str(root)]
    assert data["StartInterval"] == 600 and data["AbandonProcessGroup"] is True
    assert "/opt/homebrew/bin" in data["EnvironmentVariables"]["PATH"]
    assert calls[-1][:2] == ["launchctl", "bootstrap"] and calls[-1][3] == str(plist)
    assert [c for c in checks if c.label == "Timer"][0].status == "ok"


# --- tbr join --setup ------------------------------------------------------------------------------------------------

def test_join_with_setup(home, respx_mock, monkeypatch):
    monkeypatch.setattr(setup_mod.sys, "platform", "linux")
    respx_mock.post(f"{API}/join").mock(return_value=httpx.Response(200, json={"token": "tm_new", "actor": ME}))
    respx_mock.get(f"{API}/agents/me").mock(return_value=httpx.Response(200, json=ME))
    respx_mock.get(f"{API}/agents/me/git-credentials").mock(return_value=httpx.Response(200, json={
        "gitea_url": GITEA, "username": "claude-code", "token": "gt"}))
    respx_mock.get(f"{GITEA}/api/v1/user").mock(return_value=httpx.Response(200, json={"login": "claude-code"}))
    work = home / "taskbrigade-work" / "claude-code"
    r = runner.invoke(tm.app, ["join", SERVER, "tmj_x", "--dir", str(work), "--setup", "claude-code"])
    assert r.exit_code == 0, r.output
    assert "Joined as claude-code" in r.stdout and "tbr-runner-claude-code.timer active" in r.stdout
    assert "next timer pass" in r.stdout and "tm_new" not in r.output
    assert (home / ".config" / "systemd" / "user" / "tbr-runner-claude-code.timer").is_file()
    assert tomllib.loads((work / ".taskbrigade.toml").read_text())["runner"]["command"][0] == "claude"


def test_join_setup_typo_keeps_code_unused(home, respx_mock):
    r = runner.invoke(tm.app, ["join", SERVER, "tmj_x", "--dir", str(home / "w"), "--setup", "claude"])
    assert r.exit_code == 1 and "claude-code" in r.stderr
    assert not respx_mock.calls


def test_ui_join_command_includes_setup_for_cli_agents():
    assert join_commands(SERVER, "cc", "tmj_x", "claude_code")["join"].endswith("--dir ~/taskbrigade-work/cc "
                                                                                "--setup claude-code")
    assert "--setup" not in join_commands(SERVER, "h", "tmj_x", "other")["join"]


# --- tbr doctor ------------------------------------------------------------------------------------------------------

@pytest.fixture
def healthy(home, respx_mock, monkeypatch):
    """A machine where every check passes: server, token, Git, runner programs, timer, tasks."""
    monkeypatch.setattr(doctor_mod, "local_version", lambda: "0.5.0")
    monkeypatch.setattr(doctor_mod, "timer_check", lambda name, root, me: [setup_mod.Check("ok", "Timer", "active")])
    # No real tmux: the developer machine's own `taskbrigade` session must not show up in the checks.
    monkeypatch.setattr(runner_mod, "_tmux", lambda *a: subprocess.CompletedProcess(a, 1, "", "no server running"))
    respx_mock.get(f"{SERVER}/healthz").mock(return_value=httpx.Response(200, json={"version": "0.5.0", "db": "ok"}))
    respx_mock.get(f"{API}/agents/me").mock(return_value=httpx.Response(200, json=ME))
    respx_mock.get(f"{API}/agents/me/git-credentials").mock(return_value=httpx.Response(200, json={
        "gitea_url": GITEA, "username": "claude-code", "token": "gt"}))
    respx_mock.get(f"{GITEA}/api/v1/user").mock(return_value=httpx.Response(200, json={"login": "claude-code"}))
    respx_mock.get(f"{API}/agents/me/sftp").mock(return_value=httpx.Response(404, json={
        "error": {"code": "sftp_disabled", "message": "SFTP storage is not enabled"}}))
    respx_mock.get(url__startswith=f"{API}/agents/me/tasks").mock(return_value=httpx.Response(200, json={
        "items": [{"task_nr": "KET-0001", "status": "defined"}], "total": 1}))
    root = work_dir(home, extra=setup_mod.RUNNER_SECTIONS["claude_code"])
    return root


def _doctor(root):
    return runner.invoke(tm.app, ["doctor", "--work-root", str(root)])


def test_doctor_all_ok(healthy):
    r = _doctor(healthy)
    assert r.exit_code == 0, r.output
    assert "[error]" not in r.stdout and "0 error(s), 0 warning(s)" in r.stdout
    assert "claude-code (claude_code, cloud, role worker)" in r.stdout and "defined 1" in r.stdout
    assert "tm_secret" not in r.output and "gt" not in r.stdout.split()


def test_doctor_detects_missing_tmux(healthy, tools):
    tools.discard("tmux")
    r = _doctor(healthy)
    assert r.exit_code == 1 and "tmux: not found in PATH" in r.stdout and "install tmux" in r.stdout


def test_doctor_detects_wrong_token(healthy, respx_mock):
    respx_mock.get(f"{API}/agents/me").mock(return_value=httpx.Response(401, json={
        "error": {"code": "unauthorized", "message": "invalid token"}}))
    r = _doctor(healthy)
    assert r.exit_code == 1 and "Token: rejected by the server" in r.stdout and "--force" in r.stdout


def test_doctor_warns_about_outdated_tm(healthy, monkeypatch):
    monkeypatch.setattr(doctor_mod, "local_version", lambda: "0.4.0")
    r = _doctor(healthy)
    assert r.exit_code == 0 and "[warning]" in r.stdout and "tbr 0.4.0 is older than the server (0.5.0)" in r.stdout
    assert "tbr self-update" in r.stdout


def test_doctor_detects_missing_git_access(healthy, respx_mock):
    respx_mock.get(f"{API}/agents/me/git-credentials").mock(return_value=httpx.Response(404, json={
        "error": {"code": "not_found", "message": "no Gitea credentials"}}))
    r = _doctor(healthy)
    assert r.exit_code == 1 and "Git access: no Gitea credentials" in r.stdout


def test_doctor_detects_rejected_gitea_token(healthy, respx_mock, monkeypatch):
    from cli import join as join_mod

    monkeypatch.setattr(join_mod, "credential_helper_set", lambda: True)
    respx_mock.get(f"{GITEA}/api/v1/user").mock(return_value=httpx.Response(401))
    r = _doctor(healthy)
    assert r.exit_code == 1 and "rejects the token (HTTP 401)" in r.stdout


def test_doctor_unreachable_server(home, respx_mock):
    respx_mock.get(f"{SERVER}/healthz").mock(side_effect=httpx.ConnectError("refused"))
    r = _doctor(work_dir(home))
    assert r.exit_code == 1 and "is not reachable" in r.stdout and "tailscale status" in r.stdout


def test_doctor_without_config(tmp_path, home):
    r = _doctor(tmp_path)
    assert r.exit_code == 1 and "[error]" in r.stdout and "tbr join" in r.stdout


def test_doctor_hanging_tasks_and_missing_runner(healthy, respx_mock):
    respx_mock.get(url__startswith=f"{API}/agents/me/tasks").mock(return_value=httpx.Response(200, json={"items": [
        {"task_nr": "KET-0002", "status": "in_progress", "lease_expires_at": "2020-01-01T00:00:00"},
        {"task_nr": "KET-0003", "status": "in_progress", "lease_expires_at": "2999-01-01T00:00:00"}], "total": 2}))
    r = _doctor(healthy)
    assert r.exit_code == 0, r.output
    assert "KET-0002: in progress, but the lease expired" in r.stdout
    assert "KET-0003: in progress without a running session" in r.stdout
    (healthy / ".taskbrigade.toml").write_text(f'url = "{SERVER}"\ntoken = "tm_secret"\n')
    r = _doctor(healthy)
    assert "no [runner] section" in r.stdout and "tbr setup claude-code" in r.stdout


def test_doctor_timer_check_systemd(monkeypatch, tmp_path):
    results = {"tbr-runner-cc.timer": (3, "inactive\n"), "tbr-runner@cc.timer": (0, "active\n"),
               "tm-runner-cc.timer": (3, "inactive\n"), "tm-runner@cc.timer": (3, "inactive\n")}
    monkeypatch.setattr(doctor_mod.sys, "platform", "linux")
    monkeypatch.setattr(doctor_mod, "_run", lambda cmd: subprocess.CompletedProcess(cmd, *results[cmd[-1]], ""))
    monkeypatch.setattr(doctor_mod, "linger_check", lambda: None)
    assert doctor_mod.timer_check("cc", tmp_path, ME)[0].status == "ok"  # older manual template setup counts
    results["tbr-runner@cc.timer"] = (3, "inactive\n")
    results["tm-runner-cc.timer"] = (0, "active\n")  # name before 0.8 still runs (leftovers() warns about it)
    assert doctor_mod.timer_check("cc", tmp_path, ME)[0].detail == "tm-runner-cc.timer active"
    results["tm-runner-cc.timer"] = (3, "inactive\n")
    check = doctor_mod.timer_check("cc", tmp_path, ME)[0]
    assert check.status == "warn" and "tbr setup claude-code" in check.hint


def test_doctor_agent_login_and_runner_block(healthy, monkeypatch):
    runner_mod.write_block(healthy, "login", "claude-code: login expired", "KET-0001")
    calls = []
    monkeypatch.setattr(setup_mod, "_run", lambda cmd: calls.append(cmd) or subprocess.CompletedProcess(
        cmd, 1, "Not logged in. Run claude /login\n", ""))
    r = _doctor(healthy)
    assert r.exit_code == 1 and "Agent login: claude is not logged in" in r.stdout and "claude /login" in r.stdout
    assert "Runner block: tbr run claims no tasks: claude-code: login expired" in r.stdout
    assert ["/opt/bin/claude", "auth", "status"] in calls or ["claude", "auth", "status"] in calls
    assert runner_mod.read_block(healthy)  # Block stays while the agent is not ready.

    monkeypatch.setattr(setup_mod, "_run", lambda cmd: subprocess.CompletedProcess(cmd, 0, "Logged in", ""))
    r = _doctor(healthy)
    assert r.exit_code == 0, r.output
    assert "Runner block: removed – the agent is ready again" in r.stdout
    assert runner_mod.read_block(healthy) is None


def test_doctor_login_unknown_keeps_block(healthy, monkeypatch):
    monkeypatch.setattr(setup_mod, "_run", lambda cmd: subprocess.CompletedProcess(cmd, 2, "", "unknown command"))
    runner_mod.write_block(healthy, "limit", "usage limit", "KET-0001")
    r = _doctor(healthy)
    assert "Agent login: cannot be checked automatically" in r.stdout and r.exit_code == 1
    assert runner_mod.read_block(healthy)


def test_doctor_reports_old_tmux_windows(healthy, monkeypatch, respx_mock):
    listing = "@1\trunner\t0\t\n@2\tKET-0001\t1\t1700000000\n@3\tKET-0002\t0\t\n@4\tKET-0003\t0\t\n"
    monkeypatch.setattr(runner_mod, "_tmux", lambda *a: subprocess.CompletedProcess(
        a, 0, listing if a[0] == "list-windows" else "", ""))
    respx_mock.get(url__startswith=f"{API}/agents/me/tasks").mock(return_value=httpx.Response(200, json={
        "items": [{"task_nr": "KET-0003", "status": "in_progress", "lease_expires_at": "2999-01-01T00:00:00"}],
        "total": 1}))
    r = _doctor(healthy)
    assert "tmux windows: 3 open in session taskbrigade, 1 finished, 1 with an agent still running" in r.stdout
    assert "(KET-0002)" in r.stdout and "tbr cleanup --work-root" in r.stdout and "--stale" in r.stdout


def test_doctor_ignores_windows_of_other_agents(healthy, monkeypatch, respx_mock, tmp_path):
    """A tmux session shared with codex: codex's live window of a task in progress there is no warning here."""
    other = tmp_path / "codex"
    listing = (f"@1\trunner\t0\t\t\t{other}\n"
               f"@2\tKET-0003\t0\t\t{healthy.resolve()}\t{healthy}/KET/.worktrees/KET-0003\n"
               f"@3\tKET-0040\t0\t\t{other}\t{other}/KET/.worktrees/KET-0040\n"
               f"@4\tKET-0041\t0\t\t\t{other}/KET/.worktrees/KET-0041\n")
    monkeypatch.setattr(runner_mod, "_tmux", lambda *a: subprocess.CompletedProcess(
        a, 0, listing if a[0] == "list-windows" else "", ""))
    respx_mock.get(url__startswith=f"{API}/agents/me/tasks").mock(return_value=httpx.Response(200, json={
        "items": [{"task_nr": "KET-0003", "status": "in_progress", "lease_expires_at": "2999-01-01T00:00:00"}],
        "total": 1}))
    r = _doctor(healthy)
    assert ("tmux windows: 1 open in session taskbrigade, 0 finished, 0 with an agent still running although the "
            "task is no longer in progress (plus 2 of other agents)") in " ".join(r.stdout.split())
    assert "--stale" not in r.stdout and "KET-0040" not in r.stdout
