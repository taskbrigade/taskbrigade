"""Rename tm → tbr (package taskmeister → taskbrigade): old names keep working, the server bridges old self-updates,
`tbr setup` switches existing machines over (backups, idempotent) and `tbr doctor` reports what still calls tm."""
import base64
import csv
import hashlib
import io
import json
import os
import shutil
import subprocess
import sys
import tomllib
import zipfile
from pathlib import Path

import pytest
from typer.testing import CliRunner

from app.services import distribution
from cli import legacy, migrate, prepare, runner as runner_mod, selfupdate, tm
from cli import setup as setup_mod
from cli import setup_apps
from tests.test_distribution import LEGACY_WHEEL, VERSION, WHEEL, dist, get  # noqa: F401
from tests.test_setup_doctor import home, tools, work_dir  # noqa: F401

runner = CliRunner()
ROOT = Path(__file__).resolve().parent.parent


# --- Package and entry points -------------------------------------------------------------------------------------

def test_package_is_taskbrigade_with_old_and_new_commands():
    project = tomllib.loads((ROOT / "pyproject.toml").read_text())["project"]
    assert project["name"] == "taskbrigade"
    assert project["scripts"] == {"tbr": "cli.tm:app", "tbr-admin": "cli.tm_admin:app", "tm": "cli.legacy:tm",
                                  "tm-admin": "cli.legacy:tm_admin"}
    assert '\nname = "taskbrigade"\n' in (ROOT / "uv.lock").read_text()


@pytest.mark.parametrize("argv,env,expected", [
    (["tm", "list"], {}, True),
    (["tm", "--help"], {}, True),
    (["tm", "submit", "K-1", "--summary", "mcp"], {}, True),
    (["tm", "list"], {"TBR_NO_RENAME_NOTICE": "1"}, False),
    (["tm", "git-credential", "get"], {}, False),  # stdout is read by git, stderr shown on every fetch
    (["tm", "mcp", "--work-root", "/w"], {}, False),  # an app may show stderr as a server error
    (["tm", "prepare", "--work-root", "/w", "--wake-gate"], {}, False),  # old Hermes/OpenClaw scripts
    (["tm", "prepare", "--task", "K-1"], {}, True),
])
def test_rename_notice_only_for_humans_and_agents(argv, env, expected):
    assert legacy.wants_notice(argv, env) is expected


def test_old_command_prints_the_notice_once_and_behaves_the_same(monkeypatch, capsys):
    monkeypatch.delenv("TBR_NO_RENAME_NOTICE", raising=False)
    monkeypatch.setattr(sys, "argv", ["tm", "version"])
    with pytest.raises(SystemExit) as e:
        legacy.tm()
    out = capsys.readouterr()
    assert e.value.code == 0 and out.out.startswith("tbr ")  # same output as tbr version
    assert out.err.count("tm is now called tbr") == 1 and "TBR_NO_RENAME_NOTICE=1" in out.err
    monkeypatch.setenv("TBR_NO_RENAME_NOTICE", "1")
    with pytest.raises(SystemExit):
        legacy.tm()
    assert capsys.readouterr().err == ""


def test_old_admin_command_prints_its_notice(monkeypatch, capsys):
    monkeypatch.delenv("TBR_NO_RENAME_NOTICE", raising=False)
    monkeypatch.setattr(sys, "argv", ["tm-admin", "--help"])
    with pytest.raises(SystemExit):
        legacy.tm_admin()
    assert "tm-admin is now called tbr-admin" in capsys.readouterr().err


# --- Command lookup, TASK.md, Claude Code rules -------------------------------------------------------------------

def fake_which(monkeypatch, found: dict[str, str]):
    monkeypatch.setattr(shutil, "which", lambda name, *a, **k: found.get(name))


def test_tm_command_prefers_tbr_and_falls_back_to_tm(monkeypatch, tmp_path):
    fake_which(monkeypatch, {"tbr": "/opt/bin/tbr", "tm": "/opt/bin/tm"})
    assert prepare.tm_command() == "/opt/bin/tbr"
    fake_which(monkeypatch, {"tm": "/opt/bin/tm"})
    monkeypatch.setattr(sys, "argv", ["/nowhere/tm"])
    assert prepare.tm_command() == "/opt/bin/tm"
    # Not on PATH (systemd unit): tbr next to the running tm.
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    for name in ("tm", "tbr"):
        (bin_dir / name).write_text("#!/bin/sh\n")
        (bin_dir / name).chmod(0o755)
    fake_which(monkeypatch, {})
    monkeypatch.setattr(sys, "argv", [str(bin_dir / "tm")])
    assert prepare.tm_command() == str(bin_dir / "tbr")


def test_task_md_names_tbr(monkeypatch):
    fake_which(monkeypatch, {"tbr": "/opt/bin/tbr", "tm": "/opt/bin/tm"})
    assert prepare.tm_display("/opt/bin/tbr") == ("tbr", " (if `tbr` is not found: `/opt/bin/tbr`)")
    assert prepare.tm_display("/elsewhere/tbr") == ("/elsewhere/tbr", "")


def test_claude_rules_allow_tbr_and_tm_with_absolute_paths(monkeypatch):
    fake_which(monkeypatch, {"tbr": "/opt/bin/tbr", "tm": "/opt/bin/tm"})
    allow = runner_mod.claude_cli_rules(runner_mod.DEFAULT_CLAUDE_ALLOW)
    for rule in ("Bash(tbr:*)", "Bash(tm:*)", "Bash(/opt/bin/tbr:*)", "Bash(/opt/bin/tm:*)"):
        assert allow.count(rule) == 1
    # An own claude_allow from before the rename also gets tbr – TASK.md names tbr now.
    assert runner_mod.claude_cli_rules(["Read", "Bash(tm:*)"]) == [
        "Read", "Bash(tm:*)", "Bash(tbr:*)", "Bash(/opt/bin/tbr:*)", "Bash(/opt/bin/tm:*)"]
    assert runner_mod.claude_cli_rules(["Read"]) == ["Read"]  # no TaskBrigade CLI wanted
    cmd = runner_mod.with_claude_permissions(["claude", "go"], runner_mod.DEFAULT_CLAUDE_ALLOW)
    assert "Bash(tbr:*)" in cmd[1] and "Bash(tm:*)" in cmd[1]


def test_opencode_config_allows_both_names():
    bash = json.loads(setup_mod.opencode_config("/opt/bin/tbr"))["permission"]["bash"]
    assert bash["tbr *"] == bash["tm *"] == bash["/opt/bin/tbr *"] == "allow"


def test_agent_templates_recognise_both_commands_and_headers():
    trigger = (ROOT / "deploy/agents/openclaw/taskbrigade-trigger.js").read_text()
    assert 'const tbr = "$HOME/.local/bin/tbr";' in trigger and 'TBR="$HOME/.local/bin/tm"' in trigger
    # New header, and the one of tbr/tm before 0.8 (a machine whose CLI is not updated yet); review jobs of reviewer
    # agents (English and German header).
    assert "/^# (TaskBrigade|Taskmeister)(-Auftrag| assignment| review job|-Review-Auftrag) /m" in trigger
    script = (ROOT / "deploy/agents/hermes/taskbrigade-prepare.sh").read_text()
    assert 'TBR="${TBR_BIN:-$HOME/.local/bin/tbr}"' in script and 'TBR="$HOME/.local/bin/tm"' in script
    skill = (ROOT / "deploy/agents/hermes/taskbrigade/SKILL.md").read_text()
    assert "# TaskBrigade assignment" in skill and "# Taskmeister assignment" in skill


def test_hermes_script_falls_back_to_tm_on_an_old_machine(tmp_path):
    if not shutil.which("bash"):
        pytest.skip("no bash")
    bin_dir = tmp_path / ".local" / "bin"
    bin_dir.mkdir(parents=True)
    (bin_dir / "tm").write_text('#!/bin/sh\necho "old tm $*"\n')
    (bin_dir / "tm").chmod(0o755)
    work = tmp_path / "work"
    work.mkdir()
    env = {"HOME": str(tmp_path), "PATH": "/usr/bin:/bin", "TBR_WORK_ROOT": str(work)}
    script = ROOT / "deploy/agents/hermes/taskbrigade-prepare.sh"
    r = subprocess.run(["bash", str(script)], env=env, capture_output=True, text=True)
    assert r.returncode == 0 and r.stdout.startswith("old tm prepare --work-root"), r.stderr
    (bin_dir / "tbr").write_text('#!/bin/sh\necho "tbr $*"\n')
    (bin_dir / "tbr").chmod(0o755)
    assert subprocess.run(["bash", str(script)], env=env, capture_output=True, text=True).stdout.startswith("tbr ")


# --- Bridge wheel for `tm self-update` of old installations -------------------------------------------------------

def toy_wheel(path: Path, name: str, version: str, scripts: dict[str, str]) -> Path:
    """A minimal valid wheel without dependencies (module `toypkg`)."""
    info = f"{name}-{version}.dist-info"
    files = {
        "toypkg/__init__.py": "def main():\n    print('toy " + version + "')\n",
        f"{info}/METADATA": f"Metadata-Version: 2.1\nName: {name}\nVersion: {version}\n",
        f"{info}/WHEEL": "Wheel-Version: 1.0\nGenerator: test\nRoot-Is-Purelib: true\nTag: py3-none-any\n",
        f"{info}/entry_points.txt": "[console_scripts]\n" + "".join(f"{k} = {v}\n" for k, v in scripts.items()),
    }
    rows = []
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as z:
        for fname, text in files.items():
            data = text.encode()
            z.writestr(fname, data)
            digest = base64.urlsafe_b64encode(hashlib.sha256(data).digest()).rstrip(b"=").decode()
            rows.append((fname, f"sha256={digest}", str(len(data))))
        rows.append((f"{info}/RECORD", "", ""))
        buf = io.StringIO()
        csv.writer(buf, lineterminator="\n").writerows(rows)
        z.writestr(f"{info}/RECORD", buf.getvalue())
    return path


ALL_SCRIPTS = {n: "toypkg:main" for n in ("tbr", "tbr-admin", "tm", "tm-admin")}


def test_bridge_wheel_renames_the_distribution_only(tmp_path):
    src = toy_wheel(tmp_path / f"taskbrigade-{VERSION}-py3-none-any.whl", "taskbrigade", VERSION, ALL_SCRIPTS)
    out = tmp_path / LEGACY_WHEEL
    distribution.bridge_wheel(src, out, "taskmeister")
    with zipfile.ZipFile(out) as z:
        names = set(z.namelist())
        assert f"taskmeister-{VERSION}.dist-info/METADATA" in names and "toypkg/__init__.py" in names
        assert not any(n.startswith("taskbrigade-") for n in names)
        assert "Name: taskmeister\n" in z.read(f"taskmeister-{VERSION}.dist-info/METADATA").decode()
        assert "tbr = toypkg:main" in z.read(f"taskmeister-{VERSION}.dist-info/entry_points.txt").decode()
        for path, digest, size in csv.reader(io.StringIO(z.read(f"taskmeister-{VERSION}.dist-info/RECORD").decode())):
            if digest:
                data = z.read(path)
                assert digest == "sha256=" + base64.urlsafe_b64encode(hashlib.sha256(data).digest()).rstrip(
                    b"=").decode() and int(size) == len(data)


def test_server_serves_the_bridge_under_the_old_name(dist):  # noqa: F811
    toy_wheel(dist / WHEEL, "taskbrigade", VERSION, ALL_SCRIPTS)
    r = get(f"/download/{LEGACY_WHEEL}")
    assert r.status_code == 200
    with zipfile.ZipFile(io.BytesIO(r.content)) as z:
        assert f"taskmeister-{VERSION}.dist-info/METADATA" in z.namelist()
    assert (dist / LEGACY_WHEEL).is_file()  # built once, next to the real wheel
    assert get(f"/download/{WHEEL}").content == (dist / WHEEL).read_bytes()
    assert get("/download/taskmeister-0.0.1-py3-none-any.whl").status_code == 404


@pytest.fixture
def real_uv(tmp_path, monkeypatch):
    """The real uv with its own tool directories – checks uv's behaviour the switch-over relies on."""
    uv = shutil.which("uv")
    if not uv:
        pytest.skip("no uv")
    for var, sub in (("UV_TOOL_DIR", "tools"), ("UV_TOOL_BIN_DIR", "bin")):
        monkeypatch.setenv(var, str(tmp_path / sub))
    monkeypatch.setenv("UV_OFFLINE", "1")
    monkeypatch.setenv("UV_PYTHON_DOWNLOADS", "never")
    monkeypatch.setenv("UV_NO_CONFIG", "1")
    return uv


def uv_tools(uv) -> str:
    return subprocess.run([uv, "tool", "list"], capture_output=True, text=True).stdout


def test_old_installation_switches_to_taskbrigade_without_losing_tm(real_uv, tmp_path):
    old = toy_wheel(tmp_path / "taskmeister-0.4.0-py3-none-any.whl", "taskmeister", "0.4.0",
                    {"tm": "toypkg:main", "tm-admin": "toypkg:main"})
    if subprocess.run([real_uv, "tool", "install", "--python", "3.12", str(old)], capture_output=True).returncode:
        pytest.skip("uv cannot install a tool here (no Python 3.12?)")
    bin_dir = tmp_path / "bin"
    new = toy_wheel(tmp_path / f"taskbrigade-{VERSION}-py3-none-any.whl", "taskbrigade", VERSION, ALL_SCRIPTS)
    # 1. The old `tm self-update` installs the bridge: still tool taskmeister, but tbr is there now.
    bridge = tmp_path / LEGACY_WHEEL
    distribution.bridge_wheel(new, bridge, "taskmeister")
    assert subprocess.run([real_uv, "tool", "install", "--force", "--python", "3.12", str(bridge)],
                          capture_output=True).returncode == 0
    assert f"taskmeister v{VERSION}" in uv_tools(real_uv) and (bin_dir / "tbr").exists()
    # 2. `tbr self-update`: install --force takes over the links, then only the old environment goes.
    assert selfupdate.install(real_uv, str(new)) == 0
    assert all((bin_dir / n).exists() for n in ALL_SCRIPTS)  # nothing missing between the two steps
    assert selfupdate.remove_legacy_tool(real_uv)[0] is True
    assert uv_tools(real_uv).split("\n")[0] == f"taskbrigade v{VERSION}" and "taskmeister" not in uv_tools(real_uv)
    for name in ALL_SCRIPTS:
        assert subprocess.run([bin_dir / name], capture_output=True, text=True).stdout.strip() == f"toy {VERSION}"
    assert selfupdate.legacy_tool(real_uv) is None


def test_uv_uninstall_would_remove_the_taken_over_links(real_uv, tmp_path):
    """Why the old environment is deleted instead of `uv tool uninstall taskmeister` (documented uv behaviour)."""
    old = toy_wheel(tmp_path / "taskmeister-0.4.0-py3-none-any.whl", "taskmeister", "0.4.0", {"tm": "toypkg:main"})
    if subprocess.run([real_uv, "tool", "install", "--python", "3.12", str(old)], capture_output=True).returncode:
        pytest.skip("uv cannot install a tool here (no Python 3.12?)")
    new = toy_wheel(tmp_path / f"taskbrigade-{VERSION}-py3-none-any.whl", "taskbrigade", VERSION, ALL_SCRIPTS)
    assert selfupdate.install(real_uv, str(new)) == 0
    subprocess.run([real_uv, "tool", "uninstall", "taskmeister"], capture_output=True)
    assert not (tmp_path / "bin" / "tm").exists()




# --- tbr setup/doctor switch an existing machine (tm → tbr, taskmeister → taskbrigade) ----------------------------

def legacy_machine(home: Path, root: Path) -> dict[str, Path]:  # noqa: F811
    """Everything a setup from before both renames left behind for the agent claude-code."""
    (root / ".taskbrigade.toml").replace(root / ".taskmeister.toml")
    repo = root / "KET"
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    for value in ("", "!/opt/bin/tm git-credential"):
        subprocess.run(["git", "-C", str(repo), "config", "--local", "--add", "credential.helper", value], check=True)
    units = setup_mod.systemd_dir()
    (units / "timers.target.wants").mkdir(parents=True)
    (units / "tm-runner-claude-code.service").write_text(
        f'[Service]\nType=oneshot\nExecStart="/opt/bin/tm" run --work-root "{root}"\nKillMode=process\n')
    (units / "tm-runner-claude-code.timer").write_text("[Timer]\nUnit=tm-runner-claude-code.service\n")
    (units / "timers.target.wants" / "tm-runner-claude-code.timer").symlink_to(units / "tm-runner-claude-code.timer")
    (units / "tm-runner@.service").write_text("[Service]\nExecStart=%h/.local/bin/tm run --work-root "
                                              "%h/taskmeister-work/%i\n")
    claude = setup_apps.claude_desktop_config()
    claude.parent.mkdir(parents=True)
    claude.write_text(json.dumps({"mcpServers": {"work": {"command": "/opt/bin/tm",
                                                          "args": ["mcp", "--work-root", str(root)]},
                                                 "taskmeister": {"command": "/opt/bin/tbr",
                                                                 "args": ["mcp", "--work-root", str(root)]},
                                                 "other": {"command": "npx", "args": ["x"]}}}))
    (root / "opencode.json").write_text(json.dumps({"permission": {"bash": {"*": "ask", "tm *": "allow"}}}))
    hermes = home / ".hermes" / "scripts" / "taskmeister-prepare.sh"
    hermes.parent.mkdir(parents=True)
    hermes.write_text(f'WORK_ROOT="${{TASKMEISTER_WORK_ROOT:-{root}}}"\nTM="${{TM_BIN:-/opt/bin/tm}}"\n')
    (root / "taskmeister-trigger.js").write_text(f'command: `cd "{root}" && /opt/bin/tm prepare --work-root '
                                                 f'"{root}" --wake-gate`,\n')
    return {"repo": repo, "units": units, "claude": claude, "hermes": hermes}


def helpers(repo: Path) -> list[str]:
    return subprocess.run(["git", "-C", str(repo), "config", "--local", "--get-all", "credential.helper"],
                          capture_output=True, text=True).stdout.splitlines()


@pytest.fixture
def old_machine(home, monkeypatch):  # noqa: F811
    monkeypatch.delenv("HERMES_HOME", raising=False)
    monkeypatch.delenv("OPENCLAW_STATE_DIR", raising=False)
    monkeypatch.setattr(setup_apps.sys, "platform", "linux")
    monkeypatch.setattr(setup_mod.sys, "platform", "linux")
    root = work_dir(home, extra='\n[runner]\ncommand = ["claude", "{prompt}"]\nclaude_allow = ["Bash(tm:*)"]\n'
                                'prompt = "Do {task_nr}, then tm submit"\n')
    return root, legacy_machine(home, root)


def warnings(checks) -> list[str]:
    return [c.detail for c in checks if c.status == "warn"]


def test_doctor_lists_every_place_that_still_uses_an_old_name(old_machine, home):
    root, _files = old_machine
    checks = migrate.leftovers(root, "claude-code", "tbr setup claude-code")
    found = "\n".join(warnings(checks))
    for text in (".taskmeister.toml still has the old name", "credential helper calls tm in KET",
                 "runner timer under the old name:", "tm-runner-claude-code.service",
                 "Claude Desktop: MCP server 'work' calls tm", "Claude Desktop: MCP server still named 'taskmeister'",
                 "taskmeister-prepare.sh calls tm", "taskmeister-trigger.js calls tm",
                 "opencode.json allows tm but not tbr", "[runner] claude_allow, prompt in .taskmeister.toml still names tm"):
        assert text in found
    assert "tm-runner@" not in found  # the shared template is not this agent's timer
    assert all("tbr setup claude-code or tbr doctor --fix" in c.hint for c in checks if c.label == migrate.NAME_LABEL)


def test_doctor_reports_global_helper_and_old_package(home, monkeypatch):  # noqa: F811
    root = work_dir(home)
    subprocess.run(["git", "config", "--global", "credential.helper", "!tm git-credential"], check=True)
    monkeypatch.setattr(migrate, "installed_distribution", lambda: ("taskmeister", "0.5.0"))
    checks = migrate.leftovers(root, "claude-code", "tbr setup claude-code")
    assert "global git credential helper calls tm" in warnings(checks)
    assert any("--replace-all credential.helper" in c.hint for c in checks)
    assert any(c.hint == "replace it with: tbr self-update" for c in checks)


def test_setup_switches_the_machine_with_backups(old_machine, home, monkeypatch):
    root, files = old_machine
    calls = []
    monkeypatch.setattr(setup_mod, "_run", lambda cmd: calls.append(cmd) or subprocess.CompletedProcess(cmd, 0, "", ""))
    r = runner.invoke(tm.app, ["setup", "claude-code", "--work-root", str(root)])
    assert r.exit_code == 0, r.output
    # Configuration: new name, same content and mode, old file kept as backup.
    assert "renamed to .taskbrigade.toml" in r.stdout
    assert 'token = "tm_secret"' in (root / ".taskbrigade.toml").read_text()
    assert (root / ".taskbrigade.toml").stat().st_mode & 0o777 == 0o600
    assert not (root / ".taskmeister.toml").exists()
    # Repo helper → tbr.
    assert "credential helper switched to tbr in KET" in r.stdout
    assert helpers(files["repo"]) == ["", "!/opt/bin/tbr git-credential"]
    assert "/opt/bin/tm git-credential" in (files["repo"] / ".git" / "config.bak").read_text()
    # Old timer stopped and kept as *.bak, the new one installed under the new name.
    units = files["units"]
    assert "old runner timer stopped: tm-runner-claude-code.service, tm-runner-claude-code.timer" in r.stdout
    assert ["systemctl", "--user", "disable", "--now", "tm-runner-claude-code.timer"] in calls
    assert not (units / "tm-runner-claude-code.service").exists()
    assert (units / "tm-runner-claude-code.service.bak").is_file() and (units / "tm-runner-claude-code.timer.bak").is_file()
    assert 'ExecStart="/opt/bin/tbr" run' in (units / "tbr-runner-claude-code.service").read_text()
    assert ["systemctl", "--user", "enable", "--now", "tbr-runner-claude-code.timer"] in calls
    assert (units / "tm-runner@.service").read_text().startswith("[Service]\nExecStart=%h/.local/bin/tm")  # shared
    # MCP entries: 'taskmeister' renamed, 'work' calls tbr, others untouched; backup of the app config.
    servers = json.loads(files["claude"].read_text())["mcpServers"]
    assert set(servers) == {"work", "taskbrigade", "other"}
    assert servers["work"]["command"] == servers["taskbrigade"]["command"] == "/opt/bin/tbr"
    assert servers["other"] == {"command": "npx", "args": ["x"]}
    assert setup_apps.backup_path(files["claude"]).is_file()
    assert json.loads((root / "opencode.json").read_text())["permission"]["bash"]["tbr *"] == "allow"
    # What remains are the framework scripts of other kinds.
    found = warnings(migrate.leftovers(root, "claude-code", "tbr setup claude-code"))
    assert all("taskmeister-prepare.sh" in w or "taskmeister-trigger.js" in w for w in found), found
    # Idempotent: a second run switches nothing and leaves the backups alone.
    backup = (units / "tm-runner-claude-code.service.bak").read_text()
    assert migrate.migrate(root, "claude-code", "/opt/bin/tbr", timer_follows=True) == []
    assert (units / "tm-runner-claude-code.service.bak").read_text() == backup


def test_doctor_renames_the_configuration_and_fix_switches_the_rest(old_machine, home, monkeypatch):
    from cli import doctor as doctor_mod

    root, files = old_machine
    checks, path, cfg = doctor_mod.config_check(root)
    assert path == root / ".taskbrigade.toml" and cfg.token == "tm_secret"
    assert any("renamed to .taskbrigade.toml" in c.detail for c in checks)
    assert (root / ".taskmeister.toml.bak").is_file()
    assert doctor_mod.config_check(root)[0][1:] == []  # nothing left to rename
    fixed = migrate.migrate(root, "claude-code", "/opt/bin/tbr", kind="claude_code")
    assert (files["units"] / "tbr-runner-claude-code.timer").is_file()  # installed by the migration itself
    assert any("old runner timer stopped" in c.detail for c in fixed)
    assert "taskbrigade" in json.loads(files["claude"].read_text())["mcpServers"]


def test_both_configuration_names_present_the_new_one_wins(home):  # noqa: F811
    root = work_dir(home)
    (root / ".taskmeister.toml").write_text('url = "https://old.example"\ntoken = "old"\n')
    assert migrate.rename_config(root)[0].detail.endswith("kept as .taskmeister.toml.bak")
    assert 'token = "tm_secret"' in (root / ".taskbrigade.toml").read_text()
    assert (root / ".taskmeister.toml.bak").read_text().endswith('token = "old"\n')


def test_codex_mcp_entry_is_renamed_in_place(home):  # noqa: F811
    root = work_dir(home)
    config = setup_apps.codex_config()
    config.parent.mkdir(parents=True)
    config.write_text(f'model = "x"\n\n[mcp_servers.taskmeister]\ncommand = "/opt/bin/tbr"\n'
                      f'args = ["mcp", "--work-root", "{root}"]\n\n[mcp_servers.other]\ncommand = "npx"\n')
    checks = migrate.migrate_mcp(root, "/opt/bin/tbr")
    assert [c.label for c in checks] == [migrate.NAME_LABEL] and "renamed to 'taskbrigade'" in checks[0].detail
    data = tomllib.loads(config.read_text())
    assert set(data["mcp_servers"]) == {"taskbrigade", "other"} and data["model"] == "x"
    assert data["mcp_servers"]["taskbrigade"]["args"] == ["mcp", "--work-root", str(root)]
    assert migrate.migrate_mcp(root, "/opt/bin/tbr") == []


def fake_cli(monkeypatch, outputs: dict[tuple[str, ...], str]):
    """setup_mod._run answering by command prefix; records every call."""
    calls = []

    def run(cmd):
        calls.append(cmd)
        out = next((v for k, v in outputs.items() if tuple(cmd[:len(k)]) == k), "")
        return subprocess.CompletedProcess(cmd, 0, out, "")
    monkeypatch.setattr(setup_mod, "_run", run)
    return calls


def test_setup_of_frameworks_renames_scripts_and_retires_the_old_ones(old_machine, home, tools, monkeypatch):
    root, files = old_machine
    tools.update({"hermes", "openclaw"})
    fake_cli(monkeypatch, {("openclaw", "automations", "list"): "[]", ("openclaw", "agents", "list"): "[]"})
    skill = home / ".hermes" / "skills" / "productivity" / "taskmeister" / "SKILL.md"
    skill.parent.mkdir(parents=True)
    skill.write_text("old skill")
    for kind in ("hermes", "openclaw"):
        r = runner.invoke(tm.app, ["setup", kind, "--work-root", str(root), "--no-timer"])
        assert r.exit_code == 0, r.output
    new_script = home / ".hermes" / "scripts" / "taskbrigade-prepare.sh"
    assert 'TBR="${TBR_BIN:-/opt/bin/tbr}"' in new_script.read_text() and f"{root}" in new_script.read_text()
    assert (home / ".hermes" / "skills" / "productivity" / "taskbrigade" / "SKILL.md").is_file()
    # No old job left (the job list does not name it) → the old files go: script → *.bak, skill out of Hermes.
    assert not files["hermes"].exists() and setup_apps.backup_path(files["hermes"]).is_file()
    assert not skill.parent.exists()
    assert (root / ".backup-taskmeister" / "hermes-taskmeister" / "SKILL.md").read_text() == "old skill"
    assert 'const tbr = "/opt/bin/tbr";' in (root / "taskbrigade-trigger.js").read_text()
    assert not (root / "taskmeister-trigger.js").exists() and (root / "taskmeister-trigger.js.bak").is_file()
    found = warnings(migrate.leftovers(root, "claude-code", "tbr setup claude-code"))
    assert not any("calls tm" in w for w in found), found


def test_hermes_job_is_renamed_not_doubled(home, tools, monkeypatch):  # noqa: F811
    monkeypatch.delenv("HERMES_HOME", raising=False)
    root = work_dir(home, "hermes")
    tools.add("hermes")
    calls = fake_cli(monkeypatch, {("hermes", "cron", "list"): "taskmeister-hermes  every 10m\n",
                                   ("hermes", "cron", "edit", "--help"): "--name --skill --script --prompt"})
    old_skill = home / ".hermes" / "skills" / "productivity" / "taskmeister"
    old_skill.mkdir(parents=True)
    assert any("cron job taskmeister-hermes" in d for d in setup_apps.framework_leftovers("hermes", root, "hermes"))
    checks = setup_apps.setup_framework("hermes", root, "/opt/bin/tbr", "hermes", schedule=True, restart=False)
    assert ["hermes", "cron", "edit", "taskmeister-hermes", "--name", "taskbrigade-hermes", "--skill", "taskbrigade",
            "--script", "taskbrigade-prepare.sh"] in calls
    assert not any(c[:3] == ["hermes", "cron", "create"] for c in calls)
    assert any("taskmeister-hermes renamed to taskbrigade-hermes" in c.detail for c in checks)
    # The job list still shows the old name in this fake → the old skill stays until the next run sees it gone.
    assert old_skill.is_dir()


def test_hermes_job_kept_with_a_hint_when_the_cli_cannot_rename(home, tools, monkeypatch):  # noqa: F811
    monkeypatch.delenv("HERMES_HOME", raising=False)
    root = work_dir(home, "hermes")
    tools.add("hermes")
    calls = fake_cli(monkeypatch, {("hermes", "cron", "list"): "taskmeister-hermes\n"})
    old_skill = home / ".hermes" / "skills" / "productivity" / "taskmeister"
    old_skill.mkdir(parents=True)
    checks = setup_apps.setup_framework("hermes", root, "/opt/bin/tbr", "hermes", schedule=True, restart=False)
    job = next(c for c in checks if c.label == "Cron job")
    assert job.status == "warn" and "old job taskmeister-hermes kept" in job.detail and "hermes cron edit" in job.hint
    assert not any(c[:3] in (["hermes", "cron", "create"], ["hermes", "cron", "edit"]) and "--help" not in c
                   for c in calls)
    assert old_skill.is_dir()  # the old job still needs it


def test_openclaw_automation_is_renamed(home, tools, monkeypatch):  # noqa: F811
    monkeypatch.delenv("OPENCLAW_STATE_DIR", raising=False)
    root = work_dir(home, "openclaw-agent")
    tools.add("openclaw")
    job = [{"id": "j1", "name": "taskmeister-openclaw-agent", "agentId": "main"}]
    calls = fake_cli(monkeypatch, {("openclaw", "automations", "list"): json.dumps(job),
                                   ("openclaw", "agents", "list"): "[]",
                                   ("openclaw", "automations", "edit", "--help"): "--name --trigger-script --agent"})
    checks = setup_apps.setup_openclaw(root, "/opt/bin/tbr", "openclaw-agent", schedule=True, restart=False,
                                       openclaw_agent=None)
    edit = next(c for c in calls if c[:3] == ["openclaw", "automations", "edit"] and "--help" not in c)
    assert edit[3:6] == ["j1", "--name", "taskbrigade-openclaw-agent"] and "--trigger-script" in edit
    assert any("name taskmeister-openclaw-agent → taskbrigade-openclaw-agent" in c.detail for c in checks)
    assert not any(c[:3] == ["openclaw", "automations", "add"] for c in calls)


def test_nothing_points_at_tbr_without_tbr(old_machine):
    root, files = old_machine
    migrate.migrate(root, "claude-code", "/opt/bin/tm")
    assert helpers(files["repo"]) == ["", "!/opt/bin/tm git-credential"]
    assert json.loads(files["claude"].read_text())["mcpServers"]["work"]["command"] == "/opt/bin/tm"


def test_clean_machine_reports_ok(home):  # noqa: F811
    root = work_dir(home)
    checks = migrate.leftovers(root, "claude-code", "tbr setup claude-code")
    assert [(c.status, c.detail) for c in checks] == [("ok", "nothing on this machine uses an old name any more")]


def test_prepare_switches_a_repo_helper_on_its_own(tmp_path, monkeypatch):
    """Even without tbr setup: the next tbr prepare/run sets the repo's helper to tbr (existing behaviour)."""
    repo = tmp_path / "KET"
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    for value in ("", "!/opt/bin/tm git-credential"):
        subprocess.run(["git", "-C", str(repo), "config", "--local", "--add", "credential.helper", value], check=True)
    prepare._set_helper(repo, "!/opt/bin/tbr git-credential")
    assert helpers(repo) == ["", "!/opt/bin/tbr git-credential"]
    assert os.path.isdir(repo / ".git")
