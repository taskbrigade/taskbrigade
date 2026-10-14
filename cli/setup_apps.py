"""`tbr setup` for agents that are not started by the runner: desktop apps with the MCP server `tbr mcp` and agent
frameworks with their own scheduler and the wake gate `tbr prepare --wake-gate`.

- `claude-desktop`: MCP entry in `claude_desktop_config.json` (JSON merge, other entries kept), skill path, restart.
- `codex-desktop`: `[mcp_servers.<name>]` in `~/.codex/config.toml` (other tables kept, result checked with tomllib).
- `hermes`: skill → `~/.hermes/skills/productivity/taskbrigade/`, pre-run script → `~/.hermes/scripts/`, cron job via
  `hermes cron create`.
- `openclaw`: skills → `<workspace of the OpenClaw agent>/skills/taskbrigade/` and `…/taskbrigade-telegram/`
  (orchestration from a chat; main: `~/.openclaw/workspace`), trigger
  script → work directory, automation via `openclaw automations add --agent <id>`; an existing automation gets the
  current trigger via `openclaw automations edit`.

Every changed file is backed up as `<file>.bak` first; an unchanged file is not touched (idempotent). Nothing is
restarted without `--restart`. Missing CLIs or flags → the exact command as a hint instead.

Names from before TaskBrigade 0.8 (`taskmeister`): an existing job/automation `taskmeister-<agent>` is renamed to
`taskbrigade-<agent>` and pointed at the new skill and script instead of creating a second one; once no old job is
left, the old skills move to `<work dir>/.backup-taskmeister/` (a skill folder left in place would still be loaded)
and the old script/trigger to `<file>.bak`. While the old job cannot be renamed, its files stay so it keeps working.
"""
import json
import os
import re
import shlex
import shutil
import sys
import tomllib
from pathlib import Path

from cli.client import LOCAL_CONFIG
from cli.i18n import _
from cli import setup as setup_mod
from cli.setup import Check, SetupError, _toml_str, unit_id

DESKTOP_KINDS = ("claude-desktop", "codex-desktop")
FRAMEWORK_KINDS = ("hermes", "openclaw")
APP_KINDS = DESKTOP_KINDS + FRAMEWORK_KINDS
DEFAULT_MCP_NAME = "taskbrigade"
SCHEDULE_HERMES = "every 10m"
SCHEDULE_OPENCLAW = "*/10 * * * *"
HERMES_PROMPT = ("Work on the TaskBrigade assignment from the pre-run script output, following the taskbrigade skill. "
                 "If the output is empty, do nothing.")
OPENCLAW_PROMPT = "Work on the TaskBrigade assignment in this message, following the taskbrigade skill."
OPENCLAW_TIMEOUT = 3600  # Seconds per agent run; the task lease (2 h) leaves room for a resume.
OPENCLAW_MAIN = "main"
OPENCLAW_CHAT_SKILL = "taskbrigade-telegram"  # Orchestration on free-text requests from a chat
SKILL = "taskbrigade"
LEGACY_NAME = "taskmeister"  # Skill, script and job names before TaskBrigade 0.8.
LEGACY_BACKUP = ".backup-taskmeister"  # In the work directory: old skill folders after the rename.
_NAME = re.compile(r"^[A-Za-z0-9_-]+$")


# --- Files --------------------------------------------------------------------------------------------------------

def agent_file(rel: str) -> Path:
    """A file from deploy/agents: packaged in the wheel as cli/agent_files, in a source checkout under deploy/."""
    here = Path(__file__).resolve().parent
    for base in (here / "agent_files", here.parent / "deploy" / "agents"):
        if (base / rel).exists():
            return base / rel
    raise SetupError(_("%(file)s is missing from this tbr installation – reinstall tbr", file=rel))


def backup_path(path: Path) -> Path:
    return path.with_name(path.name + ".bak")


def write_file(path: Path, content: str, mode: int | None = None) -> str:
    """Write `content` unless it is already there; back up a differing file first. Returns the detail text."""
    if path.is_file():
        if path.read_text() == content:
            if mode is not None:
                os.chmod(path, mode)
            return _("%(path)s already up to date", path=path)
        shutil.copy2(path, backup_path(path))
        _replace(path, content, mode if mode is not None else path.stat().st_mode & 0o777)
        return _("%(path)s updated, previous version in %(name)s", path=path, name=backup_path(path).name)
    path.parent.mkdir(parents=True, exist_ok=True)
    _replace(path, content, mode if mode is not None else 0o644)
    return _("%(path)s written", path=path)


def _replace(path: Path, content: str, mode: int) -> None:
    """Write atomically: a crash never leaves a half-written app configuration behind."""
    tmp = path.with_name(f".{path.name}.tm-tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, mode)
    with os.fdopen(fd, "w") as f:
        f.write(content)
    os.chmod(tmp, mode)
    os.replace(tmp, path)


def mcp_entry(tm: str, work_root: Path) -> dict:
    return {"command": tm, "args": ["mcp", "--work-root", str(work_root)]}


def _serves(entry, work_root: Path) -> bool:
    args = entry.get("args") if isinstance(entry, dict) else None
    return isinstance(args, list) and "mcp" in args and str(work_root) in [str(a) for a in args]


# --- Claude Desktop -----------------------------------------------------------------------------------------------

def claude_desktop_config() -> Path:
    if sys.platform == "darwin":
        base = Path.home() / "Library" / "Application Support"
    elif sys.platform.startswith("win"):
        base = Path(os.environ.get("APPDATA") or Path.home() / "AppData" / "Roaming")
    else:
        base = Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config")
    return base / "Claude" / "claude_desktop_config.json"


def merge_json_config(path: Path, name: str, entry: dict, replace: str | None = None) -> str:
    """Set mcpServers[name] = entry, keep everything else; `replace` names an entry to drop (a rename). Refuses a
    file that is not a JSON object."""
    data: dict = {}
    if path.is_file():
        text = path.read_text()
        try:
            data = json.loads(text) if text.strip() else {}
        except ValueError as e:
            raise SetupError(_("%(path)s is not valid JSON (%(error)s) – fix it first, nothing was changed",
                               path=path, error=e)) from e
        if not isinstance(data, dict) or not isinstance(data.get("mcpServers", {}), dict):
            raise SetupError(_("%(path)s has an unexpected structure – nothing was changed", path=path))
    if data.get("mcpServers", {}).get(name) == entry and (replace is None or replace not in data.get("mcpServers", {})):
        return _("%(path)s: MCP server '%(name)s' already up to date", path=path, name=name)
    servers = data.setdefault("mcpServers", {})
    if replace is not None:
        servers.pop(replace, None)
    servers[name] = entry
    detail = write_file(path, json.dumps(data, indent=2, ensure_ascii=False) + "\n")
    if replace is not None:
        return _("MCP server '%(old)s' renamed to '%(name)s' → %(detail)s", old=replace, name=name, detail=detail)
    return _("MCP server '%(name)s' → %(detail)s", name=name, detail=detail)


# --- Codex Desktop ------------------------------------------------------------------------------------------------

def codex_config() -> Path:
    return Path(os.environ.get("CODEX_HOME") or Path.home() / ".codex") / "config.toml"


_HEADER = re.compile(r"^\s*\[(\[?)([^\[\]]+)\]\]?\s*(#.*)?$")


def _key_path(header: str) -> tuple[str, ...]:
    """`mcp_servers."a.b"` → ("mcp_servers", "a.b"), via tomllib so quoting rules stay TOML's."""
    try:
        data = tomllib.loads(f"[{header}]")
    except tomllib.TOMLDecodeError:
        return ()
    keys = []
    while isinstance(data, dict) and len(data) == 1:
        key, data = next(iter(data.items()))
        keys.append(key)
    return tuple(keys)


def replace_toml_table(text: str, keys: tuple[str, ...], section: str) -> str:
    """Drop the table `keys` and its sub-tables (up to the next other header) and append `section`."""
    out, skipping = [], False
    for line in text.splitlines():
        m = _HEADER.match(line)
        if m:
            path = _key_path(m.group(2).strip())
            skipping = path[:len(keys)] == keys
        if not skipping:
            out.append(line)
    body = "\n".join(out).rstrip("\n")
    return (body + "\n\n" if body else "") + section


def _without(data: dict, *tables: tuple[str, ...]) -> dict:
    data = json.loads(json.dumps(data, default=str))  # Deep copy, also datetimes.
    for keys in tables:
        node = data
        for key in keys[:-1]:
            node = node.get(key, {}) if isinstance(node, dict) else {}
        if isinstance(node, dict):
            node.pop(keys[-1], None)
        if isinstance(data.get(keys[0]), dict) and not data[keys[0]]:
            data.pop(keys[0])
    return data


def merge_toml_config(path: Path, name: str, entry: dict, replace: str | None = None) -> str:
    """Set [mcp_servers.<name>] in a TOML file; `replace` names a table to drop (a rename). The result must parse and
    differ from the old one only there."""
    text = path.read_text() if path.is_file() else ""
    try:
        old = tomllib.loads(text)
    except tomllib.TOMLDecodeError as e:
        raise SetupError(_("%(path)s is not valid TOML (%(error)s) – fix it first, nothing was changed",
                           path=path, error=e)) from e
    servers = old.get("mcp_servers") or {}
    if servers.get(name) == entry and (replace is None or replace not in servers):
        return _("%(path)s: MCP server '%(name)s' already up to date", path=path, name=name)
    keys = ("mcp_servers", name)
    header = name if _NAME.match(name) else _toml_str(name)
    section = (f"[mcp_servers.{header}]\ncommand = {_toml_str(entry['command'])}\n"
               f"args = [{', '.join(_toml_str(a) for a in entry['args'])}]\n")
    dropped = [keys] + ([("mcp_servers", replace)] if replace is not None else [])
    new_text = text
    if replace is not None:
        new_text = replace_toml_table(new_text, ("mcp_servers", replace), "").rstrip("\n")
        new_text = new_text + "\n" if new_text else ""
    new_text = replace_toml_table(new_text, keys, section)
    try:
        new = tomllib.loads(new_text)
    except tomllib.TOMLDecodeError:
        new = None
    if new is None or new.get("mcp_servers", {}).get(name) != entry \
            or _without(new, *dropped) != _without(old, *dropped) \
            or (replace is not None and replace in new.get("mcp_servers", {})):
        raise SetupError(_("%(path)s cannot be changed safely (e.g. mcp_servers written inline) – add this "
                           "yourself, nothing was changed:\n%(section)s", path=path, section=section.strip()))
    detail = write_file(path, new_text)
    if replace is not None:
        return _("MCP server '%(old)s' renamed to '%(name)s' → %(detail)s", old=replace, name=name, detail=detail)
    return _("MCP server '%(name)s' → %(detail)s", name=name, detail=detail)


# --- Desktop apps -------------------------------------------------------------------------------------------------

APP_NAMES = {"claude-desktop": "Claude", "codex-desktop": "Codex"}


def restart_app(kind: str, restart: bool) -> Check:
    app = APP_NAMES[kind]
    if not restart:
        return Check("warn", _("Restart"), _("%(app)s reads the configuration only at startup", app=app),
                     _("quit %(app)s completely and start it again (or run tbr setup again with --restart)", app=app))
    if sys.platform != "darwin":
        return Check("warn", _("Restart"), _("--restart is only supported on macOS"),
                     _("quit %(app)s completely and start it again", app=app))
    r = setup_mod._run(["osascript", "-e", f'quit app "{app}"', "-e", "delay 2", "-e", f'tell application "{app}" to activate'])
    if r.returncode != 0:
        return Check("warn", _("Restart"), _("%(app)s could not be restarted: %(error)s", app=app,
                                              error=(r.stderr or r.stdout).strip()[-200:]),
                     _("quit %(app)s completely and start it again", app=app))
    return Check("ok", _("Restart"), _("%(app)s restarted", app=app))


def setup_desktop(kind: str, work_root: Path, tm: str, name: str, restart: bool) -> list[Check]:
    entry = mcp_entry(tm, work_root)
    if kind == "claude-desktop":
        path = claude_desktop_config()
        checks = []
        if not path.parent.exists():
            checks.append(Check("warn", "Claude Desktop", _("%(dir)s does not exist – is Claude Desktop installed?",
                                                            dir=path.parent)))
        checks.append(Check("ok", _("MCP configuration"), merge_json_config(path, name, entry)))
        skill = agent_file("cowork/taskbrigade-skill.zip")
        checks.append(Check("ok", _("Skill"), str(skill),
                            _("upload it once in Claude under Settings → Capabilities → Skills → Upload skill")))
        checks.append(restart_app(kind, restart))
        checks.append(Check("ok", _("Next"), _("in Claude: \"Check whether there is a TaskBrigade task for you\""),
                            _("Cowork: share the folder %(dir)s when starting a task", dir=work_root)))
        return checks
    path = codex_config()
    checks = [Check("ok", _("MCP configuration"), merge_toml_config(path, name, entry)), restart_app(kind, restart)]
    checks.append(Check("ok", _("Next"), _("open %(dir)s as a project in Codex and ask: \"Check whether there is a "
                                           "TaskBrigade task for you\"", dir=work_root)))
    return checks


# --- Hermes -------------------------------------------------------------------------------------------------------

def hermes_home() -> Path:
    return Path(os.environ.get("HERMES_HOME") or Path.home() / ".hermes")


def openclaw_home() -> Path:
    return Path(os.environ.get("OPENCLAW_STATE_DIR") or Path.home() / ".openclaw")


def skill_text(kind: str, agent: str, skill: str = "taskbrigade", work_root: Path | None = None) -> str:
    """Shipped skill with the agent's name and – where the skill says `<work-dir>` – the work directory filled in."""
    text = agent_file(f"{kind}/{skill}/SKILL.md").read_text()
    if work_root is not None:
        text = text.replace("<work-dir>", str(work_root))
    return re.sub(r"You are the agent \*\*[^*]+\*\*", f"You are the agent **{agent}**", text, count=1)


def hermes_script(work_root: Path, tm: str) -> str:
    text = agent_file("hermes/taskbrigade-prepare.sh").read_text()
    text = re.sub(r'^WORK_ROOT=.*$', f'WORK_ROOT="${{TBR_WORK_ROOT:-{work_root}}}"', text, count=1, flags=re.M)
    return re.sub(r'^TBR=.*$', f'TBR="${{TBR_BIN:-{tm}}}"', text, count=1, flags=re.M)


def openclaw_trigger(work_root: Path, tm: str) -> str:
    text = agent_file("openclaw/taskbrigade-trigger.js").read_text()
    text = re.sub(r'^const root = .*;$', f"const root = {json.dumps(str(work_root))};", text, count=1, flags=re.M)
    return re.sub(r'^const tbr = .*;$', f"const tbr = {json.dumps(tm)};", text, count=1, flags=re.M)


def job_name(agent: str) -> str:
    return f"{SKILL}-{unit_id(agent)}"


def legacy_job_name(agent: str) -> str:
    return f"{LEGACY_NAME}-{unit_id(agent)}"


def hermes_files(name: str = SKILL) -> tuple[Path, Path]:
    """(skill folder, check script) of Hermes under the current or the old name."""
    home = hermes_home()
    return home / "skills" / "productivity" / name, home / "scripts" / f"{name}-prepare.sh"


def retire(path: Path, work_root: Path, label: str) -> str:
    """Move an old skill folder to <work dir>/.backup-taskmeister/<label> (inside the framework it would still be
    loaded), an old file to <file>.bak. Returns the detail text."""
    if path.is_dir():
        target = work_root / LEGACY_BACKUP / label
        if target.exists():
            shutil.rmtree(target)
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(path), str(target))
    else:
        target = backup_path(path)
        path.replace(target)
    return _("%(old)s moved to %(new)s", old=path, new=target)


def hermes_command(agent: str) -> list[str]:
    return ["hermes", "cron", "create", SCHEDULE_HERMES, HERMES_PROMPT, "--script", f"{SKILL}-prepare.sh",
            "--skill", SKILL, "--name", job_name(agent)]


def hermes_rename_command(agent: str) -> list[str]:
    return ["hermes", "cron", "edit", legacy_job_name(agent), "--name", job_name(agent), "--skill", SKILL,
            "--script", f"{SKILL}-prepare.sh"]


def openclaw_command(agent: str, trigger: Path, openclaw_agent: str = OPENCLAW_MAIN) -> list[str]:
    return ["openclaw", "automations", "add", SCHEDULE_OPENCLAW, OPENCLAW_PROMPT, "--name", job_name(agent),
            "--agent", openclaw_agent, "--session", "isolated", "--no-deliver",
            "--timeout-seconds", str(OPENCLAW_TIMEOUT), "--trigger-script", str(trigger)]


OPENCLAW_ADD_FLAGS = ("--agent", "--session", "--no-deliver", "--timeout-seconds", "--trigger-script")


# --- OpenClaw: agents, workspaces, automations ------------------------------------------------------------------
# The JSON layout of `openclaw … --json` differs between versions; the readers below accept the known variants and
# return None/empty when they find nothing, never guess silently.

def _openclaw_json(cmd: list[str]):
    """Parsed JSON output of an openclaw command; None if the CLI is missing, fails or prints no JSON."""
    if not shutil.which("openclaw"):
        return None
    r = setup_mod._run(cmd)
    if r.returncode != 0:
        return None
    try:
        return json.loads(r.stdout)
    except ValueError:
        return None


def _rows(data, *keys: str) -> list[dict]:
    """A list of objects from `[…]`, `{key: […]}` or `{key: {id: {…}}}`."""
    if isinstance(data, dict):
        data = next((data[k] for k in keys if isinstance(data.get(k), (list, dict))), data)
        if isinstance(data, dict):
            return [{"id": k, **v} for k, v in data.items() if isinstance(v, dict)]
    return [d for d in data if isinstance(d, dict)] if isinstance(data, list) else []


def openclaw_jobs() -> list[dict] | None:
    """The automations as listed by `openclaw automations list --json`; None if that does not work."""
    data = _openclaw_json(["openclaw", "automations", "list", "--json"])
    return None if data is None else _rows(data, "jobs", "automations", "items")


def openclaw_job(jobs: list[dict] | None, agent: str) -> dict | None:
    """The automation of this agent – under the current name, else under the old one (renamed by tbr setup)."""
    for name in (job_name(agent), legacy_job_name(agent)):
        job = next((j for j in jobs or [] if j.get("name") == name), None)
        if job is not None:
            return job
    return None


def job_agent(job: dict | None) -> str | None:
    for key in ("agentId", "effectiveAgentId", "agent"):
        if isinstance((job or {}).get(key), str) and job[key]:
            return job[key]
    return None


def embedded_trigger(job: dict | None) -> str | None:
    """The trigger script stored in the automation (`trigger.script`, `triggerScript`); None if not visible."""
    job = job or {}
    trigger = job.get("trigger")
    for value in ((trigger or {}).get("script") if isinstance(trigger, dict) else trigger, job.get("triggerScript")):
        if isinstance(value, str) and value.strip():
            return value
    return None


def same_script(a: str, b: str) -> bool:
    return a.strip() == b.strip()


def _expand(path: str) -> Path:
    return Path(os.path.expandvars(path)).expanduser()


def _configured_agents() -> list[dict]:
    """Agents with id/workspace from `openclaw agents list --json`, else from openclaw.json (plain JSON only)."""
    rows = _rows(_openclaw_json(["openclaw", "agents", "list", "--json"]), "agents", "entries", "list")
    if rows:
        return rows
    try:
        config = json.loads((openclaw_home() / "openclaw.json").read_text())
    except (OSError, ValueError):
        return []
    agents = config.get("agents") if isinstance(config, dict) else None
    return _rows(agents, "entries", "list") if isinstance(agents, dict) else []


def openclaw_workspace(openclaw_agent: str) -> Path:
    """Workspace of an OpenClaw agent: as configured, else OpenClaw's default (main: workspace, others:
    workspace-<id>). An agent id the configuration does not know is refused – it would land in a foreign place."""
    agents = _configured_agents()
    for row in agents:
        if row.get("id") == openclaw_agent:
            workspace = row.get("workspace") or row.get("workspaceDir")
            if isinstance(workspace, str) and workspace:
                return _expand(workspace)
            break
    else:
        if agents and openclaw_agent != OPENCLAW_MAIN:
            raise SetupError(_("OpenClaw agent '%(agent)s' not found (known: %(known)s) – check --openclaw-agent",
                               agent=openclaw_agent, known=", ".join(str(a.get("id")) for a in agents)))
    name = "workspace" if openclaw_agent == OPENCLAW_MAIN else f"workspace-{openclaw_agent}"
    return openclaw_home() / name


def openclaw_skill(openclaw_agent: str, skill: str = SKILL) -> Path:
    return openclaw_workspace(openclaw_agent) / "skills" / skill / "SKILL.md"


def schedule_openclaw(agent: str, openclaw_agent: str, jobs: list[dict] | None, trigger: Path,
                      script: str) -> Check:
    """Create the automation with --agent, or bring an existing one up to date (trigger script, agent)."""
    label = _("Automation")
    cmd = openclaw_command(agent, trigger, openclaw_agent)
    job = openclaw_job(jobs, agent)
    if job is None:
        if jobs is None:
            return Check("warn", label, _("%(cli)s not found in PATH or not working", cli="openclaw"),
                         _("create it yourself: %(cmd)s", cmd=shlex.join(cmd)))
        return create_job("openclaw", agent, cmd, OPENCLAW_ADD_FLAGS)
    job_id = str(job.get("id") or job.get("jobId") or job.get("name"))
    edit, changes = ["openclaw", "automations", "edit", job_id], []
    if job.get("name") != job_name(agent):
        edit += ["--name", job_name(agent)]
        changes.append(_("name %(old)s → %(new)s", old=job.get("name"), new=job_name(agent)))
    embedded = embedded_trigger(job)
    if embedded is None or not same_script(embedded, script):  # Not visible in the list → refresh to be sure.
        edit += ["--trigger-script", str(trigger)]
        changes.append(_("trigger script"))
    if job_agent(job) not in (None, openclaw_agent):
        edit += ["--agent", openclaw_agent]
        changes.append(_("agent %(old)s → %(new)s", old=job_agent(job), new=openclaw_agent))
    if not changes:
        return Check("ok", label, _("%(name)s already up to date (agent %(agent)s)", name=job_name(agent),
                                    agent=openclaw_agent))
    manual = _("update it yourself: %(cmd)s", cmd=shlex.join(edit))
    flags = tuple(f for f in ("--name", "--trigger-script", "--agent") if f in edit)
    helptext = setup_mod._run(edit[:3] + ["--help"])
    if helptext.returncode != 0 or not all(f in helptext.stdout + helptext.stderr for f in flags):
        return Check("warn", label, _("this %(cli)s version does not offer %(flags)s", cli="openclaw",
                                      flags=", ".join(flags)), manual)
    r = setup_mod._run(edit)
    if r.returncode != 0:
        return Check("error", label, _("%(cmd)s failed: %(error)s", cmd=shlex.join(edit[:3]),
                                       error=(r.stderr or r.stdout).strip()[-300:]), manual)
    return Check("ok", label, _("%(name)s updated: %(changes)s", name=job_name(agent), changes=", ".join(changes)))


def hermes_jobs() -> str | None:
    """Text of the Hermes job list (and jobs.json); None if the CLI is missing or fails. (OpenClaw: openclaw_jobs.)"""
    if not shutil.which("hermes"):
        return None
    r = setup_mod._run(["hermes", "cron", "list"])
    text = r.stdout if r.returncode == 0 else ""
    jobs = hermes_home() / "cron" / "jobs.json"
    text += jobs.read_text() if jobs.is_file() else ""
    if r.returncode != 0 and not text:
        return None
    return text


def _listed(text: str, name: str) -> bool:
    return re.search(rf"(?<![\w-]){re.escape(name)}(?![\w-])", text) is not None


def _job_exists(kind: str, agent: str, name: str | None = None) -> bool | None:
    """True/False from the Hermes job list (default: the current job name); None if the CLI does not work."""
    text = hermes_jobs()
    return None if text is None else _listed(text, name or job_name(agent))


def schedule_job(kind: str, agent: str, cmd: list[str], flags: tuple[str, ...]) -> Check:
    label = _("Cron job") if kind == "hermes" else _("Automation")
    manual = _("create it yourself: %(cmd)s", cmd=shlex.join(cmd))
    exists = _job_exists(kind, agent)
    if exists is None:
        return Check("warn", label, _("%(cli)s not found in PATH or not working", cli=kind), manual)
    old = _job_exists(kind, agent, legacy_job_name(agent))
    if exists and old:
        return Check("warn", label, _("%(name)s exists, but so does the old job %(old)s – remove the old one in "
                                      "Hermes (hermes cron list), it claims tasks too", name=job_name(agent),
                                      old=legacy_job_name(agent)))
    if exists:
        return Check("ok", label, _("%(name)s already exists", name=job_name(agent)))
    if old:
        return rename_hermes_job(agent, cmd)
    return create_job(kind, agent, cmd, flags)


def rename_hermes_job(agent: str, create: list[str]) -> Check:
    """Rename the old job and point it at the new skill and script – never a second job next to it."""
    label = _("Cron job")
    edit = hermes_rename_command(agent)
    manual = _("rename it yourself: %(cmd)s – or remove %(old)s in Hermes and create the new one: %(create)s",
               cmd=shlex.join(edit), old=legacy_job_name(agent), create=shlex.join(create))
    helptext = setup_mod._run(edit[:3] + ["--help"])
    flags = ("--name", "--skill", "--script")
    if helptext.returncode != 0 or not all(f in helptext.stdout + helptext.stderr for f in flags):
        return Check("warn", label, _("old job %(old)s kept: this %(cli)s version does not offer %(flags)s",
                                      old=legacy_job_name(agent), cli="hermes", flags=", ".join(flags)), manual)
    r = setup_mod._run(edit)
    if r.returncode != 0:
        return Check("error", label, _("%(cmd)s failed: %(error)s", cmd=shlex.join(edit[:3]),
                                       error=(r.stderr or r.stdout).strip()[-300:]), manual)
    return Check("ok", label, _("%(old)s renamed to %(name)s", old=legacy_job_name(agent), name=job_name(agent)))


def create_job(kind: str, agent: str, cmd: list[str], flags: tuple[str, ...]) -> Check:
    """Run the create command if the CLI's help lists all `flags`, else print it."""
    label = _("Cron job") if kind == "hermes" else _("Automation")
    manual = _("create it yourself: %(cmd)s", cmd=shlex.join(cmd))
    helptext = setup_mod._run(cmd[:3] + ["--help"])
    if helptext.returncode != 0 or not all(f in helptext.stdout + helptext.stderr for f in flags):
        return Check("warn", label, _("this %(cli)s version does not offer %(flags)s", cli=kind, flags=", ".join(flags)),
                     manual + "\n" + _("details: docs/agent-frameworks.md"))
    r = setup_mod._run(cmd)
    if r.returncode != 0:
        return Check("error", label, _("%(cmd)s failed: %(error)s", cmd=shlex.join(cmd[:3]),
                                       error=(r.stderr or r.stdout).strip()[-300:]), manual)
    return Check("ok", label, _("%(name)s created", name=job_name(agent)))


def restart_gateway(kind: str, restart: bool) -> Check:
    cmd = [kind, "gateway", "restart"]
    if not restart:
        return Check("ok", _("Restart"), _("nothing restarted"),
                     _("if the skill does not show up: %(cmd)s (or tbr setup again with --restart)", cmd=shlex.join(cmd)))
    if not shutil.which(kind):
        return Check("warn", _("Restart"), _("%(cli)s not found in PATH", cli=kind), shlex.join(cmd))
    r = setup_mod._run(cmd)
    if r.returncode != 0:
        return Check("warn", _("Restart"), _("%(cmd)s failed: %(error)s", cmd=shlex.join(cmd),
                                             error=(r.stderr or r.stdout).strip()[-200:]))
    return Check("ok", _("Restart"), _("%(cmd)s done", cmd=shlex.join(cmd)))


def setup_framework(kind: str, work_root: Path, tm: str, agent: str, schedule: bool, restart: bool,
                    openclaw_agent: str | None = None) -> list[Check]:
    for value in (str(work_root), tm):
        if any(c in value for c in '"$`\\'):  # Both end up inside double quotes of a shell command.
            raise SetupError(_("%(path)s contains characters the check script cannot quote – use another path",
                               path=value))
    if kind == "openclaw":
        return setup_openclaw(work_root, tm, agent, schedule, restart, openclaw_agent)
    skill, script = hermes_files()
    checks = [Check("ok", _("Skill"), write_file(skill / "SKILL.md", skill_text(kind, agent))),
              Check("ok", _("Check script"), write_file(script, hermes_script(work_root, tm), 0o755))]
    cmd = hermes_command(agent)
    if schedule:
        checks.append(schedule_job(kind, agent, cmd, ("--script", "--skill")))
    else:
        checks.append(Check("ok", _("Cron job"), _("not created (--no-timer)"),
                            _("create it yourself: %(cmd)s", cmd=shlex.join(cmd))))
    checks += retire_hermes(work_root, agent)
    checks.append(restart_gateway(kind, restart))
    return checks


def _old_hermes_files(work_root: Path) -> list[Path]:
    skill, script = hermes_files(LEGACY_NAME)
    try:
        own_script = script.is_file() and str(work_root) in script.read_text()
    except OSError:
        own_script = False
    return ([skill] if skill.is_dir() else []) + ([script] if own_script else [])


def retire_hermes(work_root: Path, agent: str) -> list[Check]:
    """Old skill and check script, once no old job uses them any more (unknown → they stay)."""
    old = _old_hermes_files(work_root)
    if not old or _job_exists("hermes", agent, legacy_job_name(agent)) is not False:
        return []
    return [Check("ok", _("Old files"), retire(p, work_root, f"hermes-{p.name}")) for p in old]


def setup_openclaw(work_root: Path, tm: str, agent: str, schedule: bool, restart: bool,
                   openclaw_agent: str | None) -> list[Check]:
    jobs = openclaw_jobs()
    # Default: the agent the existing automation runs for – a re-run never moves it to main.
    openclaw_agent = openclaw_agent or job_agent(openclaw_job(jobs, agent)) or OPENCLAW_MAIN
    skill = openclaw_skill(openclaw_agent)
    trigger = work_root / f"{SKILL}-trigger.js"
    script = openclaw_trigger(work_root, tm)
    chat_skill = openclaw_skill(openclaw_agent, OPENCLAW_CHAT_SKILL)
    checks = [Check("ok", _("Skill"), _("OpenClaw agent %(agent)s: %(detail)s", agent=openclaw_agent,
                                        detail=write_file(skill, skill_text("openclaw", agent, work_root=work_root)))),
              Check("ok", _("Skill"), _("OpenClaw agent %(agent)s: %(detail)s", agent=openclaw_agent, detail=write_file(
                  chat_skill, skill_text("openclaw", agent, OPENCLAW_CHAT_SKILL, work_root)))),
              Check("ok", _("Trigger script"), write_file(trigger, script))]
    if schedule:
        checks.append(schedule_openclaw(agent, openclaw_agent, jobs, trigger, script))
        jobs = openclaw_jobs() if checks[-1].status == "ok" else jobs  # A renamed automation is no old one any more.
    else:
        cmd = openclaw_command(agent, trigger, openclaw_agent)
        checks.append(Check("ok", _("Automation"), _("not created (--no-timer)"),
                            _("create it yourself: %(cmd)s", cmd=shlex.join(cmd))))
    checks += retire_openclaw(work_root, agent, openclaw_agent, jobs)
    checks.append(restart_gateway("openclaw", restart))
    return checks


def _old_openclaw_files(work_root: Path, openclaw_agent: str) -> list[Path]:
    try:
        skills = [openclaw_skill(openclaw_agent, n).parent for n in (LEGACY_NAME, f"{LEGACY_NAME}-telegram")]
    except SetupError:
        skills = []
    trigger = work_root / f"{LEGACY_NAME}-trigger.js"
    return [p for p in skills if p.is_dir()] + ([trigger] if trigger.is_file() else [])


def retire_openclaw(work_root: Path, agent: str, openclaw_agent: str, jobs: list[dict] | None) -> list[Check]:
    """Old skills and trigger file, once no automation is still called taskmeister-<agent> (unknown → they stay)."""
    old = _old_openclaw_files(work_root, openclaw_agent)
    if not old or jobs is None or any(j.get("name") == legacy_job_name(agent) for j in jobs):
        return []
    return [Check("ok", _("Old files"), retire(p, work_root, f"openclaw-{openclaw_agent}-{p.name}")) for p in old]


# --- Entry points -------------------------------------------------------------------------------------------------

def setup_app(kind: str, work_root: Path, agent: str, mcp_name: str | None = None, schedule: bool = True,
              restart: bool = False, openclaw_agent: str | None = None) -> list[Check]:
    from cli.prepare import tm_command

    if not (work_root / LOCAL_CONFIG).is_file():
        raise SetupError(_("%(path)s is missing – first run tbr join <server> <code> --dir %(dir)s",
                           path=work_root / LOCAL_CONFIG, dir=work_root))
    name = mcp_name or DEFAULT_MCP_NAME
    if not _NAME.match(name):
        raise SetupError(_("--name may only contain letters, digits, '-' and '_'"))
    if mcp_name and kind not in DESKTOP_KINDS:
        raise SetupError(_("--name only applies to claude-desktop and codex-desktop"))
    if openclaw_agent is not None and (kind != "openclaw" or not _NAME.match(openclaw_agent)):
        raise SetupError(_("--openclaw-agent only applies to openclaw and may only contain letters, digits, '-' "
                           "and '_'"))
    tm = tm_command()
    if kind in DESKTOP_KINDS:
        return setup_desktop(kind, work_root, tm, name, restart)
    return setup_framework(kind, work_root, tm, agent, schedule, restart, openclaw_agent)


def desktop_entries(work_root: Path) -> list[tuple[str, str, dict]]:
    """(app, MCP name, entry) of every desktop MCP entry that serves this work directory."""
    found = []
    path = claude_desktop_config()
    try:
        servers = json.loads(path.read_text()).get("mcpServers") or {} if path.is_file() else {}
    except (ValueError, AttributeError, OSError):
        servers = {}
    found += [("Claude Desktop", n, e) for n, e in servers.items() if _serves(e, work_root)]
    path = codex_config()
    try:
        servers = tomllib.loads(path.read_text()).get("mcp_servers") or {} if path.is_file() else {}
    except (tomllib.TOMLDecodeError, OSError):
        servers = {}
    found += [("Codex", n, e) for n, e in servers.items() if _serves(e, work_root)]
    return found


def doctor_checks(agent_kind: str | None, work_root: Path, agent: str) -> list[Check] | None:
    """Checks for app/framework integrations of this work directory; None if there is none."""
    if agent_kind in FRAMEWORK_KINDS:
        return framework_checks(agent_kind, work_root, agent)
    entries = desktop_entries(work_root)
    if not entries:
        return None
    checks = []
    for app, name, entry in entries:
        command = str(entry.get("command") or "")
        if command and (shutil.which(command) or Path(command).is_file()):
            checks.append(Check("ok", app, _("MCP server '%(name)s' → %(cmd)s", name=name, cmd=command)))
        else:
            checks.append(Check("error", app, _("MCP server '%(name)s': %(cmd)s not found", name=name, cmd=command),
                                _("run tbr setup %(kind)s again", kind="claude-desktop" if app == "Claude Desktop"
                                  else "codex-desktop")))
    return checks


def framework_checks(kind: str, work_root: Path, agent: str) -> list[Check]:
    if kind == "openclaw":
        return openclaw_checks(work_root, agent)
    hint = _("set it up with tbr setup %(kind)s", kind=kind)
    skill, script = hermes_files()
    checks = [skill_check(skill / "SKILL.md", hint), script_check(script, work_root, hint)]
    exists = _job_exists(kind, agent)
    if exists is None:
        checks.append(Check("warn", _("Cron job"), _("%(cli)s not found in PATH or not working", cli=kind)))
    elif exists:
        checks.append(Check("ok", _("Cron job"), job_name(agent)))
    else:
        checks.append(Check("warn", _("Cron job"), _("%(name)s not found", name=job_name(agent)), hint))
    return checks


def skill_check(skill: Path, hint: str) -> Check:
    return Check("ok", _("Skill"), str(skill)) if skill.is_file() \
        else Check("error", _("Skill"), _("%(path)s missing", path=skill), hint)


def script_check(script: Path, work_root: Path, hint: str) -> Check:
    if not script.is_file():
        return Check("error", _("Check script"), _("%(path)s missing", path=script), hint)
    if str(work_root) not in script.read_text():
        return Check("warn", _("Check script"), _("%(path)s points to another work directory", path=script), hint)
    return Check("ok", _("Check script"), str(script))


def openclaw_checks(work_root: Path, agent: str) -> list[Check]:
    """Skill in the workspace of the agent the automation runs for; the trigger embedded in the automation counts,
    the file in the work directory is only its source."""
    from cli.prepare import tm_command

    hint = _("set it up with tbr setup %(kind)s", kind="openclaw")
    jobs = openclaw_jobs()
    job = openclaw_job(jobs, agent)
    openclaw_agent = job_agent(job) or OPENCLAW_MAIN
    try:
        skill = openclaw_skill(openclaw_agent)
    except SetupError as e:
        return [Check("error", _("Skill"), str(e), hint)]
    checks = [skill_check(skill, hint)]
    embedded = embedded_trigger(job)
    if embedded is None:
        checks.append(script_check(work_root / f"{SKILL}-trigger.js", work_root, hint))
    elif same_script(embedded, openclaw_trigger(work_root, tm_command())):
        checks.append(Check("ok", _("Check script"), _("embedded in %(name)s", name=job_name(agent))))
    elif str(work_root) not in embedded:
        checks.append(Check("warn", _("Check script"), _("the trigger in %(name)s points to another work directory",
                                                         name=job_name(agent)), hint))
    else:
        checks.append(Check("warn", _("Check script"), _("the trigger in %(name)s differs from the current one "
                                                         "(e.g. still calls tm)", name=job_name(agent)), hint))
    if jobs is None:
        checks.append(Check("warn", _("Automation"), _("%(cli)s not found in PATH or not working", cli="openclaw")))
    elif job is None:
        checks.append(Check("warn", _("Automation"), _("%(name)s not found", name=job_name(agent)), hint))
    else:
        checks.append(Check("ok", _("Automation"), _("%(name)s (agent %(agent)s)", name=job_name(agent),
                                                     agent=openclaw_agent)))
    return checks


def framework_leftovers(kind: str, work_root: Path, agent: str) -> list[str]:
    """What of a Hermes/OpenClaw integration still has the name from before TaskBrigade 0.8 (for tbr doctor)."""
    if kind == "hermes":
        found = [str(p) for p in _old_hermes_files(work_root)]
        if _job_exists("hermes", agent, legacy_job_name(agent)):
            found.append(_("cron job %(name)s", name=legacy_job_name(agent)))
        return [_("old name: %(what)s", what=w) for w in found]
    jobs = openclaw_jobs()
    job = openclaw_job(jobs, agent)
    found = [str(p) for p in _old_openclaw_files(work_root, job_agent(job) or OPENCLAW_MAIN)]
    if job is not None and job.get("name") == legacy_job_name(agent):
        found.append(_("automation %(name)s", name=legacy_job_name(agent)))
    return [_("old name: %(what)s", what=w) for w in found]
