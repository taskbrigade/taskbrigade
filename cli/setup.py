"""`tbr setup <kind>`: turn a joined work directory into a runner agent – `[runner]` section for the agent kind,
tmux/agent command check, CLI hints (Codex update dialog, AppArmor, Claude Code login) and a timer that calls
`tbr run` every 10 minutes: a systemd user timer on Linux, a launchd agent on macOS. `--no-timer` skips the timer.

Unit names: systemd `tbr-runner-<agent>.service`/`.timer` in ~/.config/systemd/user, launchd
`com.taskbrigade.runner.<agent>` in ~/Library/LaunchAgents. They carry absolute paths of `tbr` and the work directory
and a PATH built from the directories where tm, git, tmux and the agent command were found.
"""
import json
import os
import plistlib
import re
import shutil
import subprocess
import sys
import tomllib
from dataclasses import dataclass
from pathlib import Path

from cli.client import LOCAL_CONFIG
from cli.i18n import _

# Agent kinds `tbr setup` knows → agent_kind in TaskBrigade.
KINDS = {"claude-code": "claude_code", "claude_code": "claude_code", "codex": "codex", "opencode": "opencode"}
CLI_KINDS = ("claude-code", "codex", "opencode")
# Kinds without the runner (cli/setup_apps.py): desktop apps with `tbr mcp`, frameworks with the wake gate.
APP_KINDS = ("claude-desktop", "codex-desktop", "hermes", "openclaw")

# Runner defaults per agent kind (as in deploy/agents/runner/*.toml.example); opencode needs the work directory.
RUNNER_SECTIONS = {
    "claude_code": '\n[runner]\ncommand = ["claude", "--permission-mode", "auto", "{prompt}"]\nmode = "tmux"\n'
                   'tmux_session = "taskbrigade"\n',
    "codex": '\n[runner]\ncommand = ["codex", "--sandbox", "workspace-write", "--ask-for-approval", "never",\n'
             '           "-c", "sandbox_workspace_write.network_access=true", "{prompt}"]\ngit = "runner"\n'
             'mode = "tmux"\ntmux_session = "taskbrigade"\n',
}
TIMER_MINUTES = 10
LAUNCHD_PREFIX = "com.taskbrigade.runner."


@dataclass
class Check:
    """One line of `tbr setup`/`tbr doctor`: status ok | warn | error, a label, details and a concrete hint."""
    status: str
    label: str
    detail: str = ""
    hint: str = ""


def render(checks: list[Check]) -> str:
    tags = {"ok": _("ok"), "warn": _("warning"), "error": _("error")}
    width = max(len(t) for t in tags.values()) + 2
    lines = []
    for c in checks:
        lines.append(f"{'[' + tags[c.status] + ']':<{width}} {c.label}" + (f": {c.detail}" if c.detail else ""))
        lines += [f"{'':<{width}}   → {h}" for h in c.hint.splitlines() if h.strip()]
    return "\n".join(lines) + "\n"


class SetupError(RuntimeError):
    pass


def normalize_kind(kind: str) -> str:
    key = kind.strip().lower()
    if key in APP_KINDS:
        return key
    value = KINDS.get(key)
    if not value:
        raise SetupError(_("Unknown agent kind '%(kind)s' – allowed: %(kinds)s", kind=kind,
                           kinds=", ".join(CLI_KINDS + APP_KINDS)))
    return value


def _toml_str(value: str) -> str:
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


def runner_section(agent_kind: str, work_root: Path) -> str:
    if agent_kind == "opencode":
        cfg = f"OPENCODE_CONFIG={work_root / 'opencode.json'}"
        return (f'\n[runner]\ncommand = ["env", {_toml_str(cfg)}, "opencode", "--prompt", "{{prompt}}"]\n'
                'mode = "tmux"\ntmux_session = "taskbrigade"\n')
    return RUNNER_SECTIONS[agent_kind]


_TABLE = re.compile(r"^\s*\[\[?[^\]]+\]\]?\s*(#.*)?$")


def replace_runner(text: str, section: str) -> str:
    """Drop an existing [runner] table (up to the next table header) and append `section`."""
    out, skipping = [], False
    for line in text.splitlines():
        if _TABLE.match(line):
            skipping = line.strip().split("#")[0].strip() == "[runner]"
        if not skipping:
            out.append(line)
    return "\n".join(out).rstrip("\n") + "\n" + section


def opencode_config(tm: str) -> str:
    bash = {"*": "ask", "git *": "allow", "tbr *": "allow", "tm *": "allow", "ls *": "allow", "cat *": "allow"}
    if tm not in ("tbr", "tm"):
        bash[f"{tm} *"] = "allow"
    return json.dumps({"$schema": "https://opencode.ai/config.json",
                       "permission": {"edit": "allow", "bash": bash}}, indent=2) + "\n"


def write_runner_config(work_root: Path, agent_kind: str) -> list[Check]:
    from cli.join import write_private
    from cli.prepare import tm_command

    path = work_root / LOCAL_CONFIG
    if not path.is_file():
        raise SetupError(_("%(path)s is missing – first run tbr join <server> <code> --dir %(dir)s", path=path,
                           dir=work_root))
    old = path.read_text()
    new = replace_runner(old, runner_section(agent_kind, work_root))
    tomllib.loads(new)  # Never leave a broken file behind.
    checks = []
    if new != old:
        if "[runner]" in old:
            write_private(path.with_name(LOCAL_CONFIG + ".bak"), old)
        write_private(path, new)
        detail = _("%(path)s ([runner] written)", path=path) + (
            _(", previous version in %(name)s", name=LOCAL_CONFIG + ".bak") if "[runner]" in old else "")
    else:
        detail = _("%(path)s ([runner] already up to date)", path=path)
    checks.append(Check("ok", _("Runner configuration"), detail))
    if agent_kind == "opencode":
        oc = work_root / "opencode.json"
        if oc.exists():
            checks.append(Check("ok", "opencode.json", _("%(path)s exists – left unchanged", path=oc)))
        else:
            oc.write_text(opencode_config(tm_command()))
            checks.append(Check("ok", "opencode.json", _("%(path)s written (approvals for git, tbr and tm)", path=oc)))
    return checks


def prerequisite_checks(cfg: dict) -> list[Check]:
    from cli.runner import _parts, agent_binary, missing_hint, missing_prerequisites

    missing = missing_prerequisites(cfg)
    checks = [Check("error", t, _("not found in PATH"), missing_hint(t)) for t in missing]
    if not shutil.which("git"):
        checks.append(Check("error", "git", _("not found in PATH"), _("install git")))
    found = [t for t in dict.fromkeys(
        (["tmux"] if cfg.get("mode", "tmux") == "tmux" else []) + [_parts(cfg)[0], agent_binary(cfg)])
        if t not in missing]
    if found:
        checks.insert(0, Check("ok", _("Programs"), ", ".join(f"{t} ({shutil.which(t)})" for t in found)))
    return checks


def kind_hints(agent_kind: str, fix: bool = True) -> list[Check]:
    """One-time CLI preparation from docs/cli-agents.md: no dialog may stop an unattended start."""
    checks = []
    if agent_kind == "claude_code":
        if not (Path.home() / ".claude.json").exists():
            checks.append(Check("warn", "Claude Code", _("no login/onboarding found (~/.claude.json)"),
                                _("start claude once interactively and log in")))
    if agent_kind == "codex":
        checks.append(codex_update_check(fix))
        userns = Path("/proc/sys/kernel/apparmor_restrict_unprivileged_userns")
        try:
            restricted = userns.read_text().strip() == "1"
        except OSError:
            restricted = False
        if restricted:
            checks.append(Check("warn", _("Codex sandbox"), _("AppArmor restricts unprivileged user namespaces"),
                                _("test: cd /tmp && codex sandbox -- /bin/echo sandbox-ok – if it fails, an admin "
                                  "adds an AppArmor profile for codex (docs/cli-agents.md)")))
    if agent_kind == "opencode":
        checks.append(Check("warn", "OpenCode", _("runtime local or cloud depends on the configured model"),
                            _("a cloud model → an admin sets the agent's runtime to cloud (docs/cli-agents.md)")))
    return checks


def _codex_config() -> Path:
    return Path(os.environ.get("CODEX_HOME") or Path.home() / ".codex") / "config.toml"


def codex_update_check(fix: bool) -> Check:
    """Codex asks about updates at startup unless `check_for_update_on_startup = false` (top-level key)."""
    path = _codex_config()
    text = path.read_text() if path.exists() else ""
    try:
        value = tomllib.loads(text).get("check_for_update_on_startup")
    except tomllib.TOMLDecodeError:
        return Check("warn", _("Codex update dialog"), _("%(path)s is not valid TOML", path=path),
                     _("add check_for_update_on_startup = false at the top"))
    if value is False:
        return Check("ok", _("Codex update dialog"), _("disabled in %(path)s", path=path))
    if not fix or value is not None:
        return Check("warn", _("Codex update dialog"), _("enabled in %(path)s", path=path),
                     _("set check_for_update_on_startup = false at the top of the file"))
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("check_for_update_on_startup = false\n" + ("\n" + text if text else ""))  # Top-level key first.
    return Check("ok", _("Codex update dialog"), _("disabled in %(path)s", path=path))


# --- Timer --------------------------------------------------------------------------------------------------------

def unit_id(name: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]", "-", name) or "agent"


def systemd_dir() -> Path:
    return Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config") / "systemd" / "user"


def launchd_label(name: str) -> str:
    return LAUNCHD_PREFIX + unit_id(name)


def launchd_path(name: str) -> Path:
    return Path.home() / "Library" / "LaunchAgents" / f"{launchd_label(name)}.plist"


def timer_path(cfg: dict) -> str:
    """PATH for the timer job: directories of tm, git, tmux and the agent command, then the usual ones."""
    from cli.prepare import tm_command
    from cli.runner import _parts, agent_binary

    dirs = []
    for tool in (tm_command(), "git", "tmux", _parts(cfg)[0], agent_binary(cfg)):
        found = shutil.which(tool)
        if found:
            dirs.append(str(Path(found).parent))
    home = Path.home()
    extra = [home / ".local" / "bin", home / ".npm-global" / "bin", home / ".opencode" / "bin"]
    if sys.platform == "darwin":
        extra += [Path("/opt/homebrew/bin")]
    dirs += [str(d) for d in extra] + ["/usr/local/bin", "/usr/bin", "/bin"]
    return ":".join(dict.fromkeys(dirs))


def _systemd_quote(value: str) -> str:
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"').replace("%", "%%") + '"'


def systemd_units(name: str, work_root: Path, tm: str, path: str) -> tuple[str, str]:
    service = (f"# Written by tbr setup – one runner pass for {name}\n[Unit]\nDescription=TaskBrigade runner for "
               f"{unit_id(name)}\n\n[Service]\nType=oneshot\nEnvironment={_systemd_quote('PATH=' + path)}\n"
               f"ExecStart={_systemd_quote(tm)} run --work-root {_systemd_quote(str(work_root))}\n"
               "# The agent keeps running in its tmux window: systemd must not stop a tmux server started here.\n"
               "KillMode=process\n")
    timer = (f"# Written by tbr setup – runner pass every {TIMER_MINUTES} minutes (without a task: no LLM call)\n"
             f"[Unit]\nDescription=TaskBrigade runner schedule for {unit_id(name)}\n\n[Timer]\nOnActiveSec=1min\n"
             f"OnBootSec=2min\nOnUnitActiveSec={TIMER_MINUTES}min\nUnit=tbr-runner-{unit_id(name)}.service\n\n"
             "[Install]\nWantedBy=timers.target\n")
    return service, timer


def launchd_plist(name: str, work_root: Path, tm: str, path: str) -> bytes:
    log = str(work_root / "logs" / "launchd.log")
    return plistlib.dumps({
        "Label": launchd_label(name), "ProgramArguments": [tm, "run", "--work-root", str(work_root)],
        "StartInterval": TIMER_MINUTES * 60, "RunAtLoad": True, "WorkingDirectory": str(work_root),
        "EnvironmentVariables": {"PATH": path}, "StandardOutPath": log, "StandardErrorPath": log,
        # Like KillMode=process: the tmux server started by a pass keeps running.
        "AbandonProcessGroup": True})


def _run(cmd: list[str]) -> subprocess.CompletedProcess:
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.TimeoutExpired) as e:
        return subprocess.CompletedProcess(cmd, 127, "", str(e))


def manual_hint(work_root: Path) -> str:
    from cli.prepare import tm_command

    return _("without a timer: run %(cmd)s yourself, or as a cron job every 10 minutes",
             cmd=f"{tm_command()} run --work-root {work_root}")


def install_timer(name: str, work_root: Path, cfg: dict) -> list[Check]:
    from cli.prepare import tm_command

    tm, path = tm_command(), timer_path(cfg)
    if sys.platform == "darwin":
        return _install_launchd(name, work_root, tm, path)
    if sys.platform.startswith("linux"):
        return _install_systemd(name, work_root, tm, path)
    return [Check("warn", _("Timer"), _("no timer support on %(platform)s", platform=sys.platform),
                  manual_hint(work_root))]


def _write_unit(path: Path, content: str) -> None:
    """A changed unit keeps its previous version as <file>.bak (e.g. one that still called tm)."""
    if path.is_file():
        old = path.read_text()
        if old == content:
            return
        path.with_name(path.name + ".bak").write_text(old)
    path.write_text(content)


def _install_systemd(name: str, work_root: Path, tm: str, path: str) -> list[Check]:
    unit = f"tbr-runner-{unit_id(name)}"
    directory = systemd_dir()
    directory.mkdir(parents=True, exist_ok=True)
    service, timer = systemd_units(name, work_root, tm, path)
    _write_unit(directory / f"{unit}.service", service)
    _write_unit(directory / f"{unit}.timer", timer)
    for cmd in (["systemctl", "--user", "daemon-reload"], ["systemctl", "--user", "enable", "--now", f"{unit}.timer"]):
        r = _run(cmd)
        if r.returncode != 0:
            return [Check("error", _("Timer"), _("%(cmd)s failed: %(error)s", cmd=" ".join(cmd),
                                                 error=(r.stderr or r.stdout).strip()[-300:]),
                          _("no systemd user session (e.g. in a container)? Use --no-timer and %(manual)s",
                            manual=manual_hint(work_root)))]
    checks = [Check("ok", _("Timer"), _("%(unit)s.timer active (every %(min)s min; journalctl --user -u %(unit)s)",
                                        unit=unit, min=TIMER_MINUTES))]
    linger = linger_check()
    if linger:
        checks.append(linger)
    return checks


def linger_check() -> Check | None:
    user = os.environ.get("USER") or os.environ.get("LOGNAME") or ""
    r = _run(["loginctl", "show-user", user, "-p", "Linger", "--value"])
    if r.returncode != 0:
        return None
    if r.stdout.strip() == "yes":
        return Check("ok", "Linger", _("timers also run without a login"))
    return Check("warn", "Linger", _("user timers only run while %(user)s is logged in", user=user),
                 _("an admin runs once: sudo loginctl enable-linger %(user)s", user=user))


def _install_launchd(name: str, work_root: Path, tm: str, path: str) -> list[Check]:
    plist = launchd_path(name)
    plist.parent.mkdir(parents=True, exist_ok=True)
    (work_root / "logs").mkdir(exist_ok=True)
    content = launchd_plist(name, work_root, tm, path)
    if plist.is_file() and plist.read_bytes() != content:
        plist.with_name(plist.name + ".bak").write_bytes(plist.read_bytes())
    plist.write_bytes(content)
    domain = f"gui/{os.getuid()}"
    _run(["launchctl", "bootout", f"{domain}/{launchd_label(name)}"])  # Reload a changed job; fails if not loaded.
    r = _run(["launchctl", "bootstrap", domain, str(plist)])
    if r.returncode != 0:
        return [Check("error", _("Timer"), _("%(cmd)s failed: %(error)s", cmd=f"launchctl bootstrap {domain} {plist}",
                                             error=(r.stderr or r.stdout).strip()[-300:]),
                      _("check the plist or use --no-timer and %(manual)s", manual=manual_hint(work_root)))]
    return [Check("ok", _("Timer"), _("launchd job %(label)s loaded (every %(min)s min, log %(log)s)",
                                      label=launchd_label(name), min=TIMER_MINUTES,
                                      log=work_root / "logs" / "launchd.log"))]


# --- tbr setup -----------------------------------------------------------------------------------------------------

def setup(work_root: Path, kind: str, timer: bool = True, agent_name: str | None = None,
          actual_kind: str | None = None, mcp_name: str | None = None, restart: bool = False,
          openclaw_agent: str | None = None) -> list[Check]:
    """Write [runner], check prerequisites, prepare the CLI and install the timer. Returns the checks.
    App kinds (desktop apps, Hermes, OpenClaw) go to cli/setup_apps.py; `timer` then means their cron job."""
    from cli.runner import runner_config

    from cli.migrate import migrate, rename_config
    from cli.prepare import tm_command

    agent_kind = normalize_kind(kind)
    work_root = work_root.expanduser().resolve()
    renamed = rename_config(work_root)  # .taskmeister.toml from before TaskBrigade 0.8
    if not (work_root / LOCAL_CONFIG).is_file():
        raise SetupError(_("%(path)s is missing – first run tbr join <server> <code> --dir %(dir)s",
                           path=work_root / LOCAL_CONFIG, dir=work_root))
    # Installations from before the renames: switch what still calls tm or has an old name first (backups,
    # idempotent).
    # A runner kind installs its timer below (or not, with --no-timer); the old one is only stopped here.
    moved = renamed + migrate(work_root, agent_name or work_root.name, tm_command(),
                              timer_follows=agent_kind not in APP_KINDS)
    if agent_kind in APP_KINDS:
        from cli.setup_apps import setup_app

        checks = moved + setup_app(agent_kind, work_root, agent_name or work_root.name, mcp_name, timer, restart,
                                   openclaw_agent)
        if actual_kind and agent_kind in ("hermes", "openclaw") and actual_kind != agent_kind:
            checks.insert(0, Check("warn", _("Agent kind"), _("the agent is registered as %(actual)s, set up as "
                                                              "%(kind)s", actual=actual_kind, kind=agent_kind),
                                   _("correct it under Admin → Benutzer & Agenten if needed")))
        return checks
    if mcp_name or restart:
        raise SetupError(_("--name and --restart only apply to %(kinds)s", kinds=", ".join(APP_KINDS)))
    if openclaw_agent is not None:
        raise SetupError(_("--openclaw-agent only applies to openclaw and may only contain letters, digits, '-' "
                           "and '_'"))
    checks = moved + write_runner_config(work_root, agent_kind)
    if actual_kind and actual_kind != agent_kind:
        checks.append(Check("warn", _("Agent kind"), _("the agent is registered as %(actual)s, set up as %(kind)s",
                                                       actual=actual_kind, kind=agent_kind),
                            _("correct it under Admin → Benutzer & Agenten if needed")))
    cfg = runner_config(work_root)
    checks += prerequisite_checks(cfg)
    checks += kind_hints(agent_kind)
    if timer:
        checks += install_timer(agent_name or work_root.name, work_root, cfg)
    else:
        checks.append(Check("ok", _("Timer"), _("not installed (--no-timer)"), manual_hint(work_root)))
    return checks
