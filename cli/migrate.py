"""Switch an agent machine to the current names: find what still uses an old one and change it.

Two renames are covered:
- TaskBrigade 0.5, command `tm` → `tbr`: credential helpers in the agent's repos (the global one is only reported),
  MCP entries of Claude/Codex Desktop that start `tm`, `opencode.json`, the [runner] section (reported).
- TaskBrigade 0.8, technical name taskmeister → taskbrigade: `.taskmeister.toml` → `.taskbrigade.toml`, runner timers
  `tm-runner-<agent>` (systemd, also the older template instance `tm-runner@<agent>.timer`) and
  `com.taskmeister.runner.<agent>` (launchd) → `tbr-runner-<agent>`/`com.taskbrigade.runner.<agent>`, MCP entries
  named `taskmeister` → `taskbrigade`. Hermes/OpenClaw skills, scripts and jobs are renamed by their own setup
  (cli/setup_apps.py), which `migrate()` runs for those kinds.

`tbr setup <kind>` calls `migrate()` before it writes its own files; `tbr doctor` renames the configuration file
(`rename_config`) and reports the rest (`leftovers()`); `tbr doctor --fix` runs `migrate()`. Every changed file is
backed up first (`<file>.bak`); a second run changes nothing. Old names keep working during the transition, so a
machine that is switched halfway still runs – the order only ever points something at `tbr` once `tbr` exists.
"""
import json
import os
import re
import shlex
import shutil
import subprocess
import tomllib
from pathlib import Path

from cli.client import LEGACY_DISTRIBUTION, LEGACY_LOCAL_CONFIG, LOCAL_CONFIG, agent_config, installed_distribution
from cli.i18n import _
from cli import setup as setup_mod
from cli.setup import Check, launchd_path, systemd_dir, unit_id

LEGACY = "tm"
LABEL = "tm → tbr"
NAME_LABEL = "taskmeister → taskbrigade"
LEGACY_NAME = "taskmeister"  # Technical name before TaskBrigade 0.8.
LEGACY_UNIT_PREFIX = "tm-runner"
LEGACY_LAUNCHD_PREFIX = "com.taskmeister.runner."


def is_legacy(command) -> bool:
    """`tm`, `/home/a/.local/bin/tm` or a quoted form of them."""
    value = str(command or "").strip().strip("'\"")
    return bool(value) and Path(value).name == LEGACY


# --- Configuration file -------------------------------------------------------------------------------------------

def rename_config(work_root: Path) -> list[Check]:
    """`.taskmeister.toml` → `.taskbrigade.toml` (mode kept); the old file stays as `.taskmeister.toml.bak`."""
    from cli.setup_apps import backup_path

    old, new = work_root / LEGACY_LOCAL_CONFIG, work_root / LOCAL_CONFIG
    if not old.is_file():
        return []
    backup = backup_path(old)
    if new.is_file():  # The new name wins; the old file was not read any more.
        old.replace(backup)
        return [Check("ok", NAME_LABEL, _("%(old)s was no longer read (%(new)s exists) – kept as %(backup)s",
                                          old=old, new=new.name, backup=backup.name))]
    shutil.copy2(old, new)
    old.replace(backup)
    return [Check("ok", NAME_LABEL, _("%(old)s renamed to %(new)s (previous file kept as %(backup)s)", old=old,
                                      new=new.name, backup=backup.name))]


# --- Git credential helper ----------------------------------------------------------------------------------------

def legacy_helper(value: str) -> bool:
    value = value.strip()
    if not value.startswith("!"):
        return False
    try:
        parts = shlex.split(value[1:])
    except ValueError:
        return False
    return len(parts) >= 2 and is_legacy(parts[0]) and parts[1] == "git-credential"


def _helpers(*scope: str, cwd: Path | None = None) -> list[str]:
    try:
        r = subprocess.run(["git", "config", *scope, "--get-all", "credential.helper"], cwd=cwd, capture_output=True,
                           text=True, timeout=30)
    except (OSError, subprocess.SubprocessError):
        return []
    return r.stdout.splitlines() if r.returncode == 0 else []


def agent_repos(work_root: Path) -> list[Path]:
    """Project clones in the work directory (`tbr prepare` puts them at <work_root>/<KEY>)."""
    try:
        return sorted(d for d in work_root.iterdir() if d.is_dir() and (d / ".git").is_dir())
    except OSError:
        return []


def legacy_repos(work_root: Path) -> list[Path]:
    return [r for r in agent_repos(work_root) if any(legacy_helper(h) for h in _helpers("--local", cwd=r))]


def global_helper_legacy() -> bool:
    return any(legacy_helper(h) for h in _helpers("--global"))


def migrate_repos(work_root: Path, tbr: str) -> list[Check]:
    from cli.prepare import _set_helper
    from cli.setup_apps import backup_path

    repos = legacy_repos(work_root)
    for repo in repos:
        config = repo / ".git" / "config"
        backup_path(config).write_bytes(config.read_bytes())
        _set_helper(repo, f"!{tbr} git-credential")
    if not repos:
        return []
    return [Check("ok", LABEL, _("credential helper switched to tbr in %(repos)s (previous config in .git/config.bak)",
                                 repos=", ".join(r.name for r in repos)))]


# --- Runner timer -------------------------------------------------------------------------------------------------

def legacy_plist(name: str) -> Path:
    return launchd_path(name).with_name(f"{LEGACY_LAUNCHD_PREFIX}{unit_id(name)}.plist")


def legacy_timer_files(name: str) -> list[Path]:
    """Timer files of this agent under the old names (the shared template `tm-runner@.service` is not one of them)."""
    unit = f"{LEGACY_UNIT_PREFIX}-{unit_id(name)}"
    files = [systemd_dir() / f"{unit}.service", systemd_dir() / f"{unit}.timer", legacy_plist(name)]
    return [p for p in files if p.is_file()]


def legacy_template_timer(name: str) -> str | None:
    """`tm-runner@<agent>.timer` while it is enabled (older manual setup with the template unit)."""
    unit = f"{LEGACY_UNIT_PREFIX}@{unit_id(name)}.timer"
    return unit if (systemd_dir() / "timers.target.wants" / unit).exists() else None


def migrate_timers(work_root: Path, name: str, timer_follows: bool) -> list[Check]:
    """Old timer of this agent → stop and disable it, keep its files as `*.bak`, install the timer under the new name
    (unless setup installs it right afterwards anyway: `timer_follows`)."""
    from cli.setup_apps import backup_path

    files, template = legacy_timer_files(name), legacy_template_timer(name)
    if not files and not template:
        return []
    for path in files:
        if path.suffix == ".plist":
            setup_mod._run(["launchctl", "bootout", f"gui/{os.getuid()}/{LEGACY_LAUNCHD_PREFIX}{unit_id(name)}"])
        elif path.suffix == ".timer":
            setup_mod._run(["systemctl", "--user", "disable", "--now", path.name])
    if template:
        setup_mod._run(["systemctl", "--user", "disable", "--now", template])
    for path in files:
        path.replace(backup_path(path))
    if any(p.suffix in (".service", ".timer") for p in files):
        setup_mod._run(["systemctl", "--user", "daemon-reload"])
    old = ", ".join([p.name for p in files] + ([template] if template else []))
    checks = [Check("ok", NAME_LABEL, _("old runner timer stopped: %(old)s (files kept as *.bak)", old=old))]
    if timer_follows:
        return checks
    from cli.runner import RunnerError, runner_config

    try:
        cfg = runner_config(work_root)
    except (RunnerError, OSError, tomllib.TOMLDecodeError) as e:
        return checks + [Check("warn", NAME_LABEL, _("new runner timer not installed: %(error)s", error=e),
                               _("install it with: tbr setup <claude-code|codex|opencode> --work-root %(dir)s",
                                 dir=work_root))]
    return checks + setup_mod.install_timer(name, work_root, cfg)


# --- MCP entries of the desktop apps ------------------------------------------------------------------------------

def legacy_mcp_entries(work_root: Path) -> list[tuple[str, str, dict]]:
    """Desktop MCP entries of this work directory that still call tm or are still named taskmeister."""
    from cli.setup_apps import desktop_entries

    return [(app, name, entry) for app, name, entry in desktop_entries(work_root)
            if is_legacy(entry.get("command")) or name == LEGACY_NAME]


def migrate_mcp(work_root: Path, tbr: str) -> list[Check]:
    """Rename `taskmeister` → `taskbrigade`; point an entry that calls tm at tbr (only once tbr exists)."""
    from cli.setup import SetupError
    from cli.setup_apps import (DEFAULT_MCP_NAME, claude_desktop_config, codex_config, merge_json_config,
                                merge_toml_config)

    checks = []
    for app, name, entry in legacy_mcp_entries(work_root):
        new_name = DEFAULT_MCP_NAME if name == LEGACY_NAME else name
        new = {**entry, "command": tbr} if Path(tbr).name == "tbr" and is_legacy(entry.get("command")) else entry
        if (new_name, new) == (name, entry):
            continue
        replace = name if new_name != name else None
        try:
            if app == "Claude Desktop":
                detail = merge_json_config(claude_desktop_config(), new_name, new, replace=replace)
            else:
                detail = merge_toml_config(codex_config(), new_name, new, replace=replace)
            checks.append(Check("ok", NAME_LABEL if replace else LABEL, f"{app}: {detail}"))
        except SetupError as e:
            checks.append(Check("warn", NAME_LABEL if replace else LABEL, f"{app}: {e}"))
    return checks


# --- Hermes, OpenClaw, OpenCode -----------------------------------------------------------------------------------

def _script_command(path: Path) -> str | None:
    """The CLI a Hermes check script or OpenClaw trigger calls (as written by tm/tbr setup)."""
    try:
        text = path.read_text()
    except OSError:
        return None
    m = (re.search(r'^(?:TM|TBR)="\$\{(?:TM|TBR)_BIN:-(.*)\}"$', text, re.M)
         or re.search(r"^const tbr = (.*);$", text, re.M))
    if m:
        return m.group(1).strip().strip('"')
    m = re.search(r"""(\S+?)['"]? prepare --work-root""", text)  # Trigger written before the rename.
    return m.group(1).strip("'\"") if m else None


def framework_scripts(kind: str, work_root: Path) -> list[Path]:
    """The check script (Hermes) or trigger (OpenClaw), under the current and the old name."""
    from cli.setup_apps import hermes_home

    if kind == "hermes":
        return [hermes_home() / "scripts" / f"{n}-prepare.sh" for n in ("taskbrigade", LEGACY_NAME)]
    return [work_root / f"{n}-trigger.js" for n in ("taskbrigade", LEGACY_NAME)]


def opencode_lacks_tbr(work_root: Path) -> bool:
    path = work_root / "opencode.json"
    try:
        bash = (json.loads(path.read_text()).get("permission") or {}).get("bash") if path.is_file() else None
    except (OSError, ValueError, AttributeError):
        return False
    return isinstance(bash, dict) and "tm *" in bash and "tbr *" not in bash


def migrate_opencode(work_root: Path, tbr: str) -> list[Check]:
    from cli.setup_apps import write_file

    if not opencode_lacks_tbr(work_root):
        return []
    path = work_root / "opencode.json"
    data = json.loads(path.read_text())
    bash = data["permission"]["bash"]
    bash["tbr *"] = bash["tm *"]
    if Path(tbr).name == "tbr" and tbr != "tbr":
        bash.setdefault(f"{tbr} *", bash["tm *"])
    return [Check("ok", LABEL, write_file(path, json.dumps(data, indent=2) + "\n"))]


def runner_mentions(work_root: Path) -> list[str]:
    """Settings of [runner] in the configuration that still name tm: `claude_allow` without tbr, `prompt`."""
    try:
        cfg = tomllib.loads(agent_config(work_root).read_text()).get("runner") or {}
    except (OSError, tomllib.TOMLDecodeError):
        return []
    found = []
    allow = cfg.get("claude_allow") or []
    if "Bash(tm:*)" in allow and "Bash(tbr:*)" not in allow:
        found.append("claude_allow")
    if re.search(r"(?<![\w.-])tm (submit|ask|abort|comment|release|heartbeat)\b", str(cfg.get("prompt") or "")):
        found.append("prompt")
    return found


# --- Entry points -------------------------------------------------------------------------------------------------

def migrate(work_root: Path, agent: str, tbr: str, timer_follows: bool = False, kind: str | None = None) -> list[Check]:
    """Switch everything of this work directory that still uses an old name. `timer_follows`: setup installs the
    runner timer afterwards anyway. `kind` hermes/openclaw: also rename their skills, scripts and job (`tbr setup`
    does that in its own step and passes no kind)."""
    checks = [*rename_config(work_root), *migrate_timers(work_root, agent, timer_follows), *migrate_mcp(work_root, tbr)]
    if Path(tbr).name == "tbr":  # Only point things at tbr when it exists.
        checks += [*migrate_repos(work_root, tbr), *migrate_opencode(work_root, tbr)]
    if kind in ("hermes", "openclaw"):
        from cli.setup_apps import framework_leftovers, setup_framework

        if framework_leftovers(kind, work_root, agent):
            checks += setup_framework(kind, work_root, tbr, agent, schedule=True, restart=False)
    return checks


def leftovers(work_root: Path, agent: str, setup_cmd: str, kind: str | None = None) -> list[Check]:
    """`tbr doctor`: one warning per place that still uses an old name, each with the command that switches it."""
    hint = _("switch with: %(cmd)s or tbr doctor --fix (in the work directory; the old names keep working until "
             "then)", cmd=setup_cmd)
    checks = []
    installed = installed_distribution()
    if installed and installed[0] == LEGACY_DISTRIBUTION:
        checks.append(Check("warn", LABEL, _("installed as the old package %(name)s", name=LEGACY_DISTRIBUTION),
                            _("replace it with: tbr self-update")))
    if (work_root / LEGACY_LOCAL_CONFIG).is_file():
        checks.append(Check("warn", NAME_LABEL, _("%(path)s still has the old name",
                                                  path=work_root / LEGACY_LOCAL_CONFIG), hint))
    repos = legacy_repos(work_root)
    if repos:
        checks.append(Check("warn", LABEL, _("credential helper calls tm in %(repos)s",
                                             repos=", ".join(r.name for r in repos)), hint))
    if global_helper_legacy():
        checks.append(Check("warn", LABEL, _("global git credential helper calls tm"),
                            'git config --global --replace-all credential.helper "!tbr git-credential" '
                            '"tm git-credential$"'))
    timers = [str(p) for p in legacy_timer_files(agent)] + [t for t in [legacy_template_timer(agent)] if t]
    if timers:
        checks.append(Check("warn", NAME_LABEL, _("runner timer under the old name: %(files)s",
                                                  files=", ".join(timers)), hint))
    for app, name, entry in legacy_mcp_entries(work_root):
        if is_legacy(entry.get("command")):
            checks.append(Check("warn", LABEL, _("%(app)s: MCP server '%(name)s' calls tm", app=app, name=name), hint))
        if name == LEGACY_NAME:
            checks.append(Check("warn", NAME_LABEL, _("%(app)s: MCP server still named '%(name)s'", app=app,
                                                      name=name), hint))
    for fw in ("hermes", "openclaw"):
        for script in framework_scripts(fw, work_root):
            if script.is_file() and (fw == "openclaw" or str(work_root) in script.read_text()) \
                    and is_legacy(_script_command(script)):
                checks.append(Check("warn", LABEL, _("%(path)s calls tm", path=script), hint))
    if kind in ("hermes", "openclaw"):
        from cli.setup_apps import framework_leftovers

        checks += [Check("warn", NAME_LABEL, detail, hint) for detail in framework_leftovers(kind, work_root, agent)]
    if opencode_lacks_tbr(work_root):
        checks.append(Check("warn", LABEL, _("opencode.json allows tm but not tbr"), hint))
    mentions = runner_mentions(work_root)
    if mentions:
        checks.append(Check("warn", LABEL, _("[runner] %(keys)s in %(file)s still names tm", keys=", ".join(mentions),
                                             file=agent_config(work_root).name),
                            _("replace tm with tbr there (Bash(tbr:*), tbr submit …)")))
    return checks or [Check("ok", NAME_LABEL, _("nothing on this machine uses an old name any more"))]
