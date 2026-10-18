"""`tbr setup claude-desktop|codex-desktop|hermes|openclaw` and their `tbr doctor` checks – with example app
configurations: merge keeps foreign entries, a second run changes nothing, a changed file gets a backup."""
import json
import os
import subprocess
import tomllib

import httpx
import pytest
from typer.testing import CliRunner

from cli import doctor as doctor_mod
from cli import setup as setup_mod
from cli import setup_apps
from cli import tm
from tests.test_setup_doctor import API, GITEA, ME, SERVER, home, tools, work_dir  # noqa: F401

runner = CliRunner()

CLAUDE_CONFIG = {"globalShortcut": "Alt+Space",
                 "mcpServers": {"filesystem": {"command": "npx", "args": ["-y", "@modelcontextprotocol/server-fs"]}}}
CODEX_CONFIG = ('check_for_update_on_startup = false\nmodel = "gpt-5"\n\n[projects."/srv/app"]\ntrust_level = '
                '"trusted"\n\n[mcp_servers.docs]\ncommand = "docs-mcp"\nargs = ["--port", "1"]\n\n'
                '[mcp_servers.docs.env]\nTOKEN_FILE = "/x"\n')


@pytest.fixture(autouse=True)
def no_framework_homes(monkeypatch):
    """Never touch a real Hermes/OpenClaw installation of the developer."""
    monkeypatch.delenv("HERMES_HOME", raising=False)
    monkeypatch.delenv("OPENCLAW_STATE_DIR", raising=False)


@pytest.fixture
def linux(monkeypatch):
    monkeypatch.setattr(setup_apps.sys, "platform", "linux")


def claude_path(home):
    return home / ".config" / "Claude" / "claude_desktop_config.json"


def _setup(root, *args):
    return runner.invoke(tm.app, ["setup", *args, "--work-root", str(root)])


# --- Claude Desktop -------------------------------------------------------------------------------------------------

def test_claude_desktop_merges_and_is_idempotent(home, linux):
    path = claude_path(home)
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps(CLAUDE_CONFIG, indent=4))
    root = work_dir(home, "claude-desktop")
    r = _setup(root, "claude-desktop")
    assert r.exit_code == 0, r.output
    data = json.loads(path.read_text())
    assert data["globalShortcut"] == "Alt+Space" and data["mcpServers"]["filesystem"] == CLAUDE_CONFIG["mcpServers"][
        "filesystem"]
    assert data["mcpServers"]["taskbrigade"] == {"command": "/opt/bin/tbr", "args": ["mcp", "--work-root", str(root)]}
    assert json.loads((path.parent / "claude_desktop_config.json.bak").read_text()) == CLAUDE_CONFIG
    assert "taskbrigade-skill.zip" in r.stdout and "Upload skill" in r.stdout
    assert "start it again" in r.stdout and "tm_secret" not in r.output
    # Second run: nothing changes, the backup keeps the original.
    before = path.read_text()
    (path.parent / "claude_desktop_config.json.bak").write_text("original")
    r = _setup(root, "claude-desktop")
    assert r.exit_code == 0 and "already up to date" in r.stdout
    assert path.read_text() == before and (path.parent / "claude_desktop_config.json.bak").read_text() == "original"


def test_claude_desktop_second_instance_with_name(home, linux):
    root = work_dir(home, "claude-desktop")
    test_root = work_dir(home, "claude-desktop-test")
    assert _setup(root, "claude-desktop").exit_code == 0
    r = _setup(test_root, "claude-desktop", "--name", "taskbrigade-test")
    assert r.exit_code == 0, r.output
    servers = json.loads(claude_path(home).read_text())["mcpServers"]
    assert servers["taskbrigade"]["args"][-1] == str(root) and servers["taskbrigade-test"]["args"][-1] == str(test_root)
    assert _setup(root, "claude-desktop", "--name", "bad name").exit_code == 1


def test_claude_desktop_creates_missing_config_and_refuses_broken_json(home, linux):
    root = work_dir(home, "claude-desktop")
    r = _setup(root, "claude-desktop")
    assert r.exit_code == 0 and "is Claude Desktop installed?" in r.stdout
    assert list(json.loads(claude_path(home).read_text())["mcpServers"]) == ["taskbrigade"]
    assert not (claude_path(home).parent / "claude_desktop_config.json.bak").exists()
    claude_path(home).write_text('{"mcpServers": {')
    r = _setup(root, "claude-desktop")
    assert r.exit_code == 1 and "not valid JSON" in r.stderr
    assert claude_path(home).read_text() == '{"mcpServers": {'


def test_claude_desktop_restart_only_with_flag(home, monkeypatch):
    calls = []
    monkeypatch.setattr(setup_mod, "_run", lambda cmd: calls.append(cmd) or subprocess.CompletedProcess(cmd, 0, "", ""))
    monkeypatch.setattr(setup_apps.sys, "platform", "darwin")
    root = work_dir(home, "claude-desktop")
    assert _setup(root, "claude-desktop").exit_code == 0
    assert calls == []
    assert (home / "Library" / "Application Support" / "Claude" / "claude_desktop_config.json").is_file()
    r = _setup(root, "claude-desktop", "--restart")
    assert r.exit_code == 0 and "Claude restarted" in r.stdout
    assert calls[0][0] == "osascript" and 'quit app "Claude"' in calls[0]


def test_join_without_config_and_name_for_frameworks(home, linux, tmp_path):
    r = _setup(tmp_path / "nothing", "claude-desktop")
    assert r.exit_code == 1 and "tbr join" in r.stderr
    r = _setup(work_dir(home, "hermes"), "hermes", "--name", "x")
    assert r.exit_code == 1 and "--name only applies" in r.stderr
    r = _setup(work_dir(home, 'bad"dir'), "openclaw")
    assert r.exit_code == 1 and "cannot quote" in r.stderr
    r = _setup(work_dir(home), "claude-code", "--restart")
    assert r.exit_code == 1 and "only apply to" in r.stderr


# --- Codex Desktop --------------------------------------------------------------------------------------------------

def test_codex_desktop_merges_toml_and_is_idempotent(home):
    path = home / ".codex" / "config.toml"
    path.parent.mkdir()
    path.write_text(CODEX_CONFIG)
    old = tomllib.loads(CODEX_CONFIG)
    root = work_dir(home, "codex-desktop")
    r = _setup(root, "codex-desktop")
    assert r.exit_code == 0, r.output
    data = tomllib.loads(path.read_text())
    assert data["mcp_servers"]["taskbrigade"] == {"command": "/opt/bin/tbr", "args": ["mcp", "--work-root", str(root)]}
    assert data["mcp_servers"]["docs"] == old["mcp_servers"]["docs"] and data["projects"] == old["projects"]
    assert data["model"] == "gpt-5" and (path.parent / "config.toml.bak").read_text() == CODEX_CONFIG
    before = path.read_text()
    r = _setup(root, "codex-desktop")
    assert r.exit_code == 0 and "already up to date" in r.stdout and path.read_text() == before
    # A moved work directory replaces the table (no duplicate) and keeps the rest.
    other = work_dir(home, "codex-desktop-2")
    r = runner.invoke(tm.app, ["setup", "codex-desktop", "--work-root", str(other)])
    assert r.exit_code == 0, r.output
    data = tomllib.loads(path.read_text())
    assert data["mcp_servers"]["taskbrigade"]["args"][-1] == str(other) and path.read_text().count("taskbrigade]") == 1
    assert data["mcp_servers"]["docs"]["env"] == {"TOKEN_FILE": "/x"}


def test_codex_desktop_refuses_unsafe_toml(home):
    path = home / ".codex" / "config.toml"
    path.parent.mkdir()
    inline = 'mcp_servers = { taskbrigade = { command = "old" } }\n'
    path.write_text(inline)
    r = _setup(work_dir(home, "codex-desktop"), "codex-desktop")
    assert r.exit_code == 1 and "cannot be changed safely" in r.stderr and "[mcp_servers.taskbrigade]" in r.stderr
    assert path.read_text() == inline
    path.write_text("model = \n")
    r = _setup(home / "taskbrigade-work" / "codex-desktop", "codex-desktop")
    assert r.exit_code == 1 and "not valid TOML" in r.stderr


def test_replace_toml_table_handles_quoted_and_sub_tables():
    text = ('a = 1\n[mcp_servers."taskbrigade"]\ncommand = "x"\n[mcp_servers.taskbrigade.env]\nK = "v"\n'
            '[mcp_servers.taskbrigade-test]\ncommand = "y"\n')
    out = setup_apps.replace_toml_table(text, ("mcp_servers", "taskbrigade"), '[mcp_servers."taskbrigade"]\n'
                                        'command = "z"\n')
    data = tomllib.loads(out)
    assert data["mcp_servers"]["taskbrigade"] == {"command": "z"} and data["mcp_servers"]["taskbrigade-test"]
    assert data["a"] == 1


# --- Hermes / OpenClaw ----------------------------------------------------------------------------------------------

class FakeCli:
    """Records commands; `jobs` is what the framework's list command prints."""

    def __init__(self, helptext="--script --skill --trigger-script", jobs=""):
        self.calls, self.helptext, self.jobs = [], helptext, jobs

    def created(self, group):
        return [c for c in self.calls if c[1] == group and c[2] in ("create", "add") and "--help" not in c]

    def __call__(self, cmd):
        self.calls.append(cmd)
        if "--help" in cmd:
            return subprocess.CompletedProcess(cmd, 0, self.helptext, "")
        if cmd[1:3] in (["cron", "list"], ["automations", "list"]):
            return subprocess.CompletedProcess(cmd, 0, self.jobs, "")
        if cmd[1:3] in (["cron", "create"], ["automations", "add"]):
            self.jobs += cmd[cmd.index("--name") + 1]
        return subprocess.CompletedProcess(cmd, 0, "", "")


def test_hermes_installs_skill_script_and_cron_job(home, tools, monkeypatch):
    tools.add("hermes")
    cli = FakeCli()
    monkeypatch.setattr(setup_mod, "_run", cli)
    root = work_dir(home, "hermes-2")
    r = _setup(root, "hermes")
    assert r.exit_code == 0, r.output
    skill = (home / ".hermes" / "skills" / "productivity" / "taskbrigade" / "SKILL.md").read_text()
    assert "You are the agent **hermes-2**" in skill and "**hermes** there" not in skill
    script = home / ".hermes" / "scripts" / "taskbrigade-prepare.sh"
    assert f'WORK_ROOT="${{TBR_WORK_ROOT:-{root}}}"' in script.read_text()
    assert 'TBR="${TBR_BIN:-/opt/bin/tbr}"' in script.read_text() and os.access(script, os.X_OK)
    create = cli.created("cron")
    assert len(create) == 1 and create[0][3] == "every 10m" and "--script" in create[0]
    assert create[0][create[0].index("--name") + 1] == "taskbrigade-hermes-2"
    assert not any("gateway" in c for c in cli.calls)  # never restarted without --restart
    # Second run: job exists, files unchanged, no backup.
    r = _setup(root, "hermes")
    assert r.exit_code == 0 and "taskbrigade-hermes-2 already exists" in r.stdout
    assert len(cli.created("cron")) == 1
    assert not script.with_name(script.name + ".bak").exists()
    # A locally edited skill is backed up before it is replaced.
    skill_path = home / ".hermes" / "skills" / "productivity" / "taskbrigade" / "SKILL.md"
    skill_path.write_text("my edit")
    assert _setup(root, "hermes", "--restart").exit_code == 0
    assert skill_path.with_name("SKILL.md.bak").read_text() == "my edit"
    assert ["hermes", "gateway", "restart"] in cli.calls


def test_hermes_without_cli_prints_exact_command(home):
    r = _setup(work_dir(home, "hermes"), "hermes")
    assert r.exit_code == 0, r.output
    assert "hermes not found in PATH" in r.stdout and "hermes cron create 'every 10m'" in r.stdout
    assert "--skill taskbrigade --name taskbrigade-hermes" in r.stdout


def test_hermes_cli_without_flags_is_not_called(home, tools, monkeypatch):
    tools.add("hermes")
    cli = FakeCli(helptext="usage: hermes cron create SCHEDULE PROMPT")
    monkeypatch.setattr(setup_mod, "_run", cli)
    r = _setup(work_dir(home, "hermes"), "hermes")
    assert r.exit_code == 0 and "does not offer --script, --skill" in r.stdout
    assert cli.created("cron") == []


class FakeOpenClaw:
    """An OpenClaw gateway with several agents (main and two with their own workspace); records commands, keeps
    the automations with agent and embedded trigger script like `openclaw automations list --json` shows them."""

    def __init__(self, home, jobs=None, helptext=" ".join(setup_apps.OPENCLAW_ADD_FLAGS)):
        self.home, self.calls, self.helptext = home, [], helptext
        self.agents = [{"id": "main", "workspace": str(home / ".openclaw" / "workspace"), "default": True},
                       {"id": "researcher", "workspace": "~/.openclaw/workspace-researcher"},
                       {"id": "review-agent"}]
        self.jobs = jobs if jobs is not None else []

    def commands(self, verb):
        return [c for c in self.calls if c[1:3] == ["automations", verb] and "--help" not in c]

    def __call__(self, cmd):
        self.calls.append(cmd)
        out = ""
        if "--help" in cmd:
            out = self.helptext
        elif cmd[1:3] == ["agents", "list"]:
            out = json.dumps(self.agents)
        elif cmd[1:3] == ["automations", "list"]:
            out = json.dumps({"jobs": self.jobs})
        elif cmd[1:3] == ["automations", "add"]:
            def opt(flag):
                return cmd[cmd.index(flag) + 1]
            self.jobs.append({"id": f"job-{len(self.jobs) + 1}", "name": opt("--name"), "agentId": opt("--agent"),
                              "trigger": {"script": open(opt("--trigger-script")).read()}})
        elif cmd[1:3] == ["automations", "edit"]:
            job = next(j for j in self.jobs if j["id"] == cmd[3])
            if "--trigger-script" in cmd:
                job["trigger"] = {"script": open(cmd[cmd.index("--trigger-script") + 1]).read()}
            if "--agent" in cmd:
                job["agentId"] = cmd[cmd.index("--agent") + 1]
        return subprocess.CompletedProcess(cmd, 0, out, "")


def openclaw_skill(home, workspace="workspace"):
    return home / ".openclaw" / workspace / "skills" / "taskbrigade" / "SKILL.md"


def test_openclaw_installs_trigger_and_automation(home, tools, monkeypatch):
    tools.add("openclaw")
    cli = FakeOpenClaw(home)
    monkeypatch.setattr(setup_mod, "_run", cli)
    root = work_dir(home, "openclaw-agent-2")
    r = _setup(root, "openclaw")
    assert r.exit_code == 0, r.output
    assert "You are the agent **openclaw-agent-2**" in openclaw_skill(home).read_text()
    chat = (openclaw_skill(home).parent.parent / "taskbrigade-telegram" / "SKILL.md").read_text()
    assert "You are the agent **openclaw-agent-2**" in chat and f"`cd {root} && tbr …`" in chat and "<work-dir>" not in chat
    trigger = (root / "taskbrigade-trigger.js").read_text()
    assert f'const root = "{root}";' in trigger and 'const tbr = "/opt/bin/tbr";' in trigger and '"$TBR" prepare' in trigger
    add = cli.commands("add")
    assert add == [setup_apps.openclaw_command("openclaw-agent-2", root / "taskbrigade-trigger.js", "main")]
    for flag in ("--session isolated", "--no-deliver", "--agent main", "--timeout-seconds 3600"):
        assert flag in " ".join(add[0])
    # Second run: automation with the same trigger → nothing to do.
    r = _setup(root, "openclaw")
    assert r.exit_code == 0 and "taskbrigade-openclaw-agent-2 already up to date (agent main)" in r.stdout
    assert len(cli.commands("add")) == 1 and cli.commands("edit") == []
    r = _setup(root, "openclaw", "--no-timer")
    assert r.exit_code == 0 and "not created (--no-timer)" in r.stdout and "--agent main" in r.stdout


def test_openclaw_skill_per_agent_main_untouched(home, tools, monkeypatch):
    tools.add("openclaw")
    cli = FakeOpenClaw(home)
    monkeypatch.setattr(setup_mod, "_run", cli)
    main_skill = openclaw_skill(home)
    main_skill.parent.mkdir(parents=True)
    main_skill.write_text("You are the agent **openclaw-agent**")
    root = work_dir(home, "research-agent")
    r = _setup(root, "openclaw", "--openclaw-agent", "researcher")
    assert r.exit_code == 0, r.output
    assert "You are the agent **research-agent**" in openclaw_skill(home, "workspace-researcher").read_text()
    assert main_skill.read_text() == "You are the agent **openclaw-agent**" and not setup_apps.backup_path(main_skill).exists()
    add = cli.commands("add")
    assert len(add) == 1 and add[0][add[0].index("--agent") + 1] == "researcher"
    assert add[0][add[0].index("--name") + 1] == "taskbrigade-research-agent"
    # Without a workspace in the configuration: OpenClaw's default workspace-<id>.
    r = _setup(work_dir(home, "reviewer"), "openclaw", "--openclaw-agent", "review-agent")
    assert r.exit_code == 0, r.output
    assert "**reviewer**" in openclaw_skill(home, "workspace-review-agent").read_text()
    # A re-run without the option keeps the agent of the existing automation – it never falls back to main.
    r = _setup(root, "openclaw")
    assert r.exit_code == 0 and "OpenClaw agent researcher" in r.stdout and "(agent researcher)" in r.stdout
    assert main_skill.read_text() == "You are the agent **openclaw-agent**"
    # Unknown agent id: refused, nothing written.
    r = _setup(work_dir(home, "x"), "openclaw", "--openclaw-agent", "typo")
    assert r.exit_code == 1 and "OpenClaw agent 'typo' not found (known: main, researcher, review-agent)" in r.stderr
    assert not (home / ".openclaw" / "workspace-typo").exists()
    r = _setup(work_dir(home, "hermes"), "hermes", "--openclaw-agent", "main")
    assert r.exit_code == 1 and "--openclaw-agent only applies to openclaw" in r.stderr


def test_openclaw_workspace_from_config_file_without_cli(home):
    config = home / ".openclaw" / "openclaw.json"
    config.parent.mkdir(parents=True)
    config.write_text(json.dumps({"agents": {"entries": {"main": {"default": True},
                                                         "ops-agent": {"workspace": "~/claws/super"}}}}))
    root = work_dir(home, "ops-agent")
    r = _setup(root, "openclaw", "--openclaw-agent", "ops-agent")
    assert r.exit_code == 0, r.output
    assert (home / "claws" / "super" / "skills" / "taskbrigade" / "SKILL.md").is_file()
    assert "openclaw not found in PATH" in r.stdout and "--agent ops-agent" in r.stdout


def test_openclaw_updates_an_outdated_trigger(home, tools, monkeypatch):
    tools.add("openclaw")
    root = work_dir(home, "research-agent")
    old = f'command: `cd "{root}" && /opt/bin/tm prepare --work-root "{root}" --wake-gate`,\n'
    cli = FakeOpenClaw(home, jobs=[{"id": "abc", "name": "taskbrigade-research-agent", "agentId": "researcher",
                                    "trigger": {"script": old}}])
    monkeypatch.setattr(setup_mod, "_run", cli)
    r = _setup(root, "openclaw")
    assert r.exit_code == 0, r.output
    assert "taskbrigade-research-agent updated: trigger script" in r.stdout
    assert cli.commands("edit") == [["openclaw", "automations", "edit", "abc", "--trigger-script",
                                     str(root / "taskbrigade-trigger.js")]]
    assert cli.commands("add") == [] and "const tbr = \"/opt/bin/tbr\";" in cli.jobs[0]["trigger"]["script"]
    assert openclaw_skill(home, "workspace-researcher").is_file() and not openclaw_skill(home).exists()
    # An explicit other agent moves the automation as well; an edit CLI without the flags only prints the command.
    r = _setup(root, "openclaw", "--openclaw-agent", "review-agent")
    assert r.exit_code == 0 and "agent researcher → review-agent" in r.stdout and cli.jobs[0]["agentId"] == "review-agent"
    cli.jobs[0]["trigger"] = {"script": old}
    cli.helptext = "usage: openclaw automations edit <id>"
    r = _setup(root, "openclaw")
    assert r.exit_code == 0 and "does not offer --trigger-script" in r.stdout
    assert "update it yourself: openclaw automations edit abc --trigger-script" in r.stdout


def test_join_command_offers_setup_for_frameworks():
    from app.web.admin_agents import join_commands

    assert join_commands(SERVER, "h", "tmj_x", "hermes")["join"].endswith("--setup hermes")
    assert "--setup" not in join_commands(SERVER, "x", "tmj_x", "other")["join"]


# --- tbr doctor ------------------------------------------------------------------------------------------------------

@pytest.fixture
def server(respx_mock, monkeypatch):
    def serve(me):
        monkeypatch.setattr(doctor_mod, "local_version", lambda: "0.5.0")
        respx_mock.get(f"{SERVER}/healthz").mock(return_value=httpx.Response(200, json={"version": "0.5.0", "db": "ok"}))
        respx_mock.get(f"{API}/agents/me").mock(return_value=httpx.Response(200, json=me))
        respx_mock.get(f"{API}/agents/me/git-credentials").mock(return_value=httpx.Response(200, json={
            "gitea_url": GITEA, "username": me["name"], "token": "gt"}))
        respx_mock.get(f"{GITEA}/api/v1/user").mock(return_value=httpx.Response(200, json={"login": me["name"]}))
        respx_mock.get(f"{API}/agents/me/sftp").mock(return_value=httpx.Response(404, json={
            "error": {"code": "sftp_disabled", "message": "SFTP storage is not enabled"}}))
        respx_mock.get(f"{API}/agents/me/email-status").mock(return_value=httpx.Response(404, json={
            "error": {"code": "email_disabled", "message": "Email notifications are not enabled (Admin → Settings → Email)"}}))
        respx_mock.get(f"{API}/agents/me/workflow-tick-status").mock(return_value=httpx.Response(200, json={
            "last": None, "stale": True, "enabled": False}))
        respx_mock.get(url__startswith=f"{API}/agents/me/tasks").mock(return_value=httpx.Response(200, json={
            "items": [], "total": 0}))
    return serve


def test_doctor_desktop_app(home, linux, server, monkeypatch):
    which = setup_apps.shutil.which
    monkeypatch.setattr(setup_apps.shutil, "which", lambda n, *a, **k: n if n.startswith("/opt/bin/") else which(n))
    server({**ME, "name": "claude-desktop", "agent_kind": "other"})
    root = work_dir(home, "claude-desktop")
    r = runner.invoke(tm.app, ["doctor", "--work-root", str(root)])
    assert "no [runner] section" in r.stdout  # not set up yet
    assert _setup(root, "claude-desktop").exit_code == 0
    r = runner.invoke(tm.app, ["doctor", "--work-root", str(root)])
    assert r.exit_code == 0, r.output
    assert "Claude Desktop: MCP server 'taskbrigade' → /opt/bin/tbr" in r.stdout
    assert "[runner]" not in r.stdout and "Timer" not in r.stdout


def test_doctor_hermes(home, tools, monkeypatch, server):
    server({**ME, "name": "hermes", "agent_kind": "hermes", "runtime": "local"})
    tools.add("hermes")
    cli = FakeCli()
    monkeypatch.setattr(setup_mod, "_run", cli)
    root = work_dir(home, "hermes")
    r = runner.invoke(tm.app, ["doctor", "--work-root", str(root)])
    assert r.exit_code == 1 and "Skill:" in r.stdout and "tbr setup hermes" in r.stdout
    assert _setup(root, "hermes").exit_code == 0
    r = runner.invoke(tm.app, ["doctor", "--work-root", str(root)])
    assert r.exit_code == 0, r.output
    assert "Cron job: taskbrigade-hermes" in r.stdout and "[warning]" not in r.stdout


def test_doctor_openclaw_embedded_trigger(home, tools, monkeypatch, server):
    server({**ME, "name": "research-agent", "agent_kind": "openclaw", "runtime": "local"})
    tools.add("openclaw")
    cli = FakeOpenClaw(home)
    monkeypatch.setattr(setup_mod, "_run", cli)
    root = work_dir(home, "research-agent")
    assert _setup(root, "openclaw", "--openclaw-agent", "researcher").exit_code == 0
    # The file in the work directory is only the source – the trigger embedded in the automation counts.
    (root / "taskbrigade-trigger.js").unlink()
    r = runner.invoke(tm.app, ["doctor", "--work-root", str(root)])
    assert r.exit_code == 0, r.output
    assert "Check script: embedded in taskbrigade-research-agent" in r.stdout
    assert "Automation: taskbrigade-research-agent (agent researcher)" in r.stdout
    assert "workspace-researcher" in r.stdout and "[warning]" not in r.stdout
    cli.jobs[0]["trigger"]["script"] = cli.jobs[0]["trigger"]["script"].replace("/opt/bin/tbr", "/opt/bin/tm")
    r = runner.invoke(tm.app, ["doctor", "--work-root", str(root)])
    assert "differs from the current one" in r.stdout and "tbr setup openclaw" in r.stdout
    # Without a visible embedded trigger the file in the work directory is checked as before.
    del cli.jobs[0]["trigger"]
    r = runner.invoke(tm.app, ["doctor", "--work-root", str(root)])
    assert r.exit_code == 1 and "taskbrigade-trigger.js missing" in r.stdout
