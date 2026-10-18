"""`tbr` – CLI for agents and runners (spec 12.1).

Configuration: TBR_URL/TBR_TOKEN or ~/.config/taskbrigade/config.toml; language: TBR_LANGUAGE or `language` there.
Exit codes: 0 ok · 1 API error · 2 configuration missing · 3 no task (claim).
"""
import json
import subprocess
import sys
from pathlib import Path
from urllib.parse import urlparse

import typer

from cli.client import ApiError, Client, ConfigError, agent_config, explain, load_config, parse_due, patch_task
from cli.client import BATCH_FORMAT, close_report, is_no_sprint, parse_batch, release_report, reopen_report
from cli.client import resolve_sprint
from cli.client import heartbeat_task, parse_usage, sprint_line, version_warning, waiting_report
from cli.client import transition as do_transition
from cli.client import agent_scope, duplicate_task, model_line, model_value, parse_tier, review_task
from cli.client import data_class_line, data_refs_lines, parse_data_class, parse_depends_on
from cli import sftp as sftp_lib
from cli.i18n import _
from cli.outputs import parse_output_args, required_outputs_lines, submitted_outputs_lines
from cli.workflows import register as register_workflows

app = typer.Typer(help=_("TaskBrigade CLI for agents"), no_args_is_help=True)
register_workflows(app)
EXIT_NO_TASK = 3

JsonOpt = typer.Option(False, "--json", help=_("Output as JSON instead of Markdown"))


def _client() -> Client:
    try:
        return Client(load_config())
    except ConfigError as e:
        typer.echo(_("Configuration missing: %(error)s", error=e), err=True)
        raise typer.Exit(2) from e


def _fail(e: ApiError) -> None:
    typer.echo(_("Error: %(error)s", error=e), err=True)
    raise typer.Exit(1)


def _call(method: str, path: str, **kw):
    try:
        r = _client().request(method, path, **kw)
    except ApiError as e:
        _fail(e)
    return None if r.status_code == 204 else r.json()


def _dump(data) -> None:
    typer.echo(json.dumps(data, indent=2, ensure_ascii=False, default=str))


def last_status_change(t: dict) -> str | None:
    """Last status change from history, e.g. defined → in_progress (2026-10-07 09:51 UTC, claude-code)."""
    events = [e for e in t.get("events") or [] if e.get("type") in ("status_change", "lease_expired", "created")]
    if not events:
        return None
    e = events[-1]
    when = str(e.get("created_at") or "")[:16].replace("T", " ")
    who = e.get("actor") or _("system")
    what = f"{e['from']} → {e['to']}" if e.get("from") else _("created as %(status)s", status=e.get("to"))
    if e.get("type") == "lease_expired":
        what += _(" (lease expired)")
    return f"{what} ({when} UTC, {who})"


def task_markdown(t: dict, footer: bool = True) -> str:
    """Render a task and its thread as Markdown for the agent (`footer`: the closing hint for the assignee)."""
    pi = t.get("project_info") or {}
    nr, base = t["task_nr"], pi.get("default_branch") or "main"
    lines = [f"# {nr} – {t['title']}", "",
             _("- Status: %(status)s · Prio: %(prio)s · Assigned to: %(assigned)s", status=t["status"], prio=t["prio"],
               assigned=t.get("assigned_to") or "–"),
             _("- Project: %(key)s (%(name)s), privacy %(privacy)s", key=pi.get("key", t.get("project")),
               name=pi.get("name", ""), privacy=pi.get("privacy", "?")),
             _("- Due: %(due)s", due=t.get("due_date") or "–")]
    if data_class_line(t):
        lines.append(data_class_line(t))
    if t.get("reviewer"):
        lines.append(_("- Reviewer: %(name)s", name=t["reviewer"]))
    if t.get("sprint_name"):
        lines.append(_("- Sprint: %(name)s (%(status)s)", name=t["sprint_name"], status=t.get("sprint_status") or "?"))
    if model_line(t):
        lines.append(model_line(t))
    change = last_status_change(t)
    if change:
        lines.append(_("- Last status change: %(change)s", change=change))
    if pi.get("clone_url"):
        lines.append(_("- Repo: %(url)s (base branch `%(base)s`)", url=pi["clone_url"],
                       base=pi.get("default_branch", "main")))
    if t.get("branch_name"):
        lines.append(f"- Branch: `{t['branch_name']}`")
    if t.get("pr_url"):
        lines.append(_("- Pull request: %(url)s", url=t["pr_url"]))
    if t.get("lease_expires_at"):
        lines.append(_("- Lease until: %(until)s UTC (`tbr heartbeat %(nr)s` extends it)", until=t["lease_expires_at"],
                       nr=nr))
    if t.get("detail_task_git_url"):
        lines.append(_("- Detailed spec: %(url)s", url=t["detail_task_git_url"]))
    if t.get("sprint_name"):
        lines += ["", _("## Sprint %(name)s", name=t["sprint_name"]), "",
                  _("This task is part of the sprint; its tasks are worked on in parallel. Sprint goal:"), "",
                  t.get("sprint_goal") or "–"]
    lines += ["", _("## Description"), "", t.get("description") or "–", "",
              _("## Acceptance criteria"), "", t.get("acceptance_criteria") or "–"]
    lines += required_outputs_lines(t)
    lines += submitted_outputs_lines(t)
    lines += data_refs_lines(t)
    if t.get("attachments"):
        lines += ["", _("## Attachments"), "",
                  _("Stored in the repo (branch `%(base)s`); in the working directory under:", base=base), ""]
        lines += [_("- `%(path)s` (%(size)s bytes, from %(who)s)", path=a["path"], size=a["size_bytes"],
                    who=a.get("uploaded_by") or "?") for a in t["attachments"]]
        lines += ["", _("Missing on an older task branch: `git checkout origin/%(base)s -- tasks/%(nr)s/`",
                        base=base, nr=nr)]
    if t.get("follow_up_of"):
        lines += ["", _("## Follow-up"), "", _("This task follows up on %(nr)s (`tbr show %(nr)s`).",
                                                 nr=t["follow_up_of"])]
    if t.get("open_points"):
        lines += ["", _("## Open points"), ""] + [f"- {p}" for p in t["open_points"]]
    if t.get("dependencies"):
        lines += ["", _("## Dependencies"), ""] + [f"- {d['task_nr']} ({d['status']}): {d['title']}"
                                                   for d in t["dependencies"]]
    if t.get("comments"):
        lines += ["", "## Thread", ""]
        for c in t["comments"]:
            lines += [_("### %(type)s from %(author)s (%(at)s)", type=c["type"], author=c["author"],
                        at=c["created_at"]), "", c["body"], ""]
    if pi.get("brain_path") and footer:
        lines += ["", "---", _("First read `%(brain)s` in the repo. Question: `tbr ask %(nr)s \"…\"`. "
                               "Done: `tbr submit %(nr)s --branch <branch> --sha <sha> --summary \"…\"` (plus "
                               "`--open-point \"…\"` per open point).",
                               brain=pi["brain_path"], nr=nr)]
    return "\n".join(lines).rstrip() + "\n"


def _out(task: dict, as_json: bool) -> None:
    if as_json:
        _dump(task)
    else:
        typer.echo(task_markdown(task), nl=False)


@app.command(help=_("Print the version of tbr and – if reachable – of the TaskBrigade server."))
def version():
    from cli.build import label
    from cli.client import local_build, local_version

    local, build = local_version(), local_build()
    typer.echo(f"tbr {label(local, build)}" if local else _("tbr (version unknown)"))
    try:
        import httpx

        cfg = load_config()
        info = httpx.get(f"{cfg.url}/healthz", timeout=5).json()
        server, server_build = info.get("version", "?"), info.get("build")
        typer.echo(f"Server {label(server, server_build)} ({cfg.url})")
    except Exception:  # noqa: BLE001 – version must work without a server or config.
        return
    warning = version_warning(server, server_build=server_build, local_build_id=build)
    if warning:
        typer.echo(warning, err=True)


def find_uv() -> str | None:
    import shutil

    found = shutil.which("uv")
    if found:
        return found
    fallback = Path.home() / ".local" / "bin" / "uv"  # Default of the uv installer (install.sh).
    return str(fallback) if fallback.is_file() else None


@app.command("self-update", help=_("Install the tbr version of the server that the identity in the current folder "
                                   "points to (uv tool). Also replaces an installation from before the rename "
                                   "(package taskmeister); tm keeps working."))
def self_update(force: bool = typer.Option(False, "--force", help=_("Reinstall even if the version matches"))):
    import httpx

    from cli import build, selfupdate
    from cli.client import DISTRIBUTION, installed_distribution, other_build, server_headers, wheel_name

    try:
        cfg = load_config()
    except ConfigError as e:
        typer.echo(_("Configuration missing: %(error)s", error=e), err=True)
        raise typer.Exit(2) from e
    try:
        r = httpx.get(f"{cfg.url}/healthz", timeout=10)
        server, server_build = server_headers(r)
        server = server or r.json().get("version")
        server_build = server_build or r.json().get("build")
    except (httpx.HTTPError, ValueError) as e:
        typer.echo(_("TaskBrigade is not reachable: %(error)s", error=e), err=True)
        raise typer.Exit(1) from e
    if not server or not server[0].isdigit():
        typer.echo(_("The server at %(url)s does not report its version", url=cfg.url), err=True)
        raise typer.Exit(1)
    package, local = installed_distribution() or (None, None)
    local_build = build.installed(package) if package else ""
    rebuilt = other_build(local, server, local_build, server_build)  # Same version, another build.
    uv = find_uv()
    legacy = selfupdate.legacy_tool(uv) if uv else None
    if local == server and not rebuilt and package == DISTRIBUTION and legacy is None and not force:
        typer.echo(_("tbr %(version)s is up to date (server %(url)s).", version=build.label(local, local_build),
                     url=cfg.url))
        return
    if not uv:
        typer.echo(_("uv not found – install tbr with: curl -fsSL %(url)s/install.sh | sh", url=cfg.url), err=True)
        raise typer.Exit(2)
    install = local != server or rebuilt or package != DISTRIBUTION or force
    if install:
        typer.echo(_("Installing tbr %(server)s (installed: %(local)s) from %(url)s …",
                     server=build.label(server, server_build), local=build.label(local, local_build) if local else "?",
                     url=cfg.url))
        rc = selfupdate.install(uv, f"{cfg.url}/download/{wheel_name(server)}")
        if rc != 0:
            typer.echo(_("Update failed (uv exit code %(rc)s).", rc=rc), err=True)
            raise typer.Exit(1)
    # Texts first: removing the old tool may remove the environment this process runs from.
    done = _("tbr %(version)s installed.", version=build.label(server, server_build))
    removed_text = _("Old package taskmeister removed – tm keeps working as an alias of tbr.")
    kept_text = _("The old package taskmeister still provides %(commands)s – left in place (uv tool list).")
    if legacy is not None:
        removed, detail = selfupdate.remove_legacy_tool(uv)
        typer.echo(removed_text if removed else kept_text % {"commands": detail}, err=not removed)
    if install:
        typer.echo(done)


@app.command(help=_("Set up this machine for an agent with a one-time join code from the web UI (Admin → Neuer "
                    "Agent): fetch a fresh API token, write .taskbrigade.toml and check whoami and Git access."))
def join(server: str = typer.Argument(..., help=_("TaskBrigade address, e.g. https://taskbrigade.example.ts.net")),
         code: str = typer.Argument(..., help=_("One-time join code (tmj_…)")),
         directory: Path = typer.Option(Path("."), "--dir", help=_("Agent's work directory (created if missing)")),
         force: bool = typer.Option(False, "--force", help=_("Overwrite an existing .taskbrigade.toml")),
         language: str = typer.Option(None, "--language", help=_("Language of agent-facing texts (en, de)")),
         setup_kind: str = typer.Option(None, "--setup", help=_("Then run tbr setup for this agent kind "
                                                                 "(claude-code, codex, opencode, claude-desktop, "
                                                                 "codex-desktop, hermes, openclaw)")),
         no_timer: bool = typer.Option(False, "--no-timer", help=_("With --setup: do not install a timer"))):
    from cli.join import JoinError
    from cli.join import join as do_join

    try:
        result = do_join(server, code, directory, force=force, language=language, setup_kind=setup_kind)
    except JoinError as e:
        typer.echo(f"tbr join: {e}", err=True)
        raise typer.Exit(1) from e
    me = result.actor
    typer.echo(_("Joined as %(name)s (%(kind)s, %(runtime)s, role %(role)s).", name=me.get("name"),
                 kind=me.get("agent_kind") or "-", runtime=me.get("runtime"), role=me.get("role")))
    typer.echo(_("Configuration: %(path)s (mode 600)", path=result.config_path))
    typer.echo({"ok": _("Git access: ok"), "missing": _("Git access: no Gitea credentials yet – an admin links them "
                                                       "under Admin → Benutzer & Agenten"),
                "failed": _("Git access: failed")}[result.git])
    for note in result.notes:
        typer.echo(f"  {note}")
    if setup_kind is None:
        typer.echo(_("Next: cd %(dir)s && tbr list", dir=result.config_path.parent))
        if result.git == "failed":
            raise typer.Exit(1)
        return
    typer.echo("")
    ok = _run_setup(result.config_path.parent, setup_kind, not no_timer, me)
    if result.git == "failed" or not ok:
        raise typer.Exit(1)


def _run_setup(work_root: Path, kind: str, timer: bool, me: dict | None, mcp_name: str | None = None,
               restart: bool = False, openclaw_agent: str | None = None) -> bool:
    """Run tbr setup, print the checks; False if one failed."""
    from cli.setup import APP_KINDS, SetupError, render
    from cli.setup import setup as do_setup

    try:
        checks = do_setup(work_root, kind, timer=timer, agent_name=(me or {}).get("name"),
                          actual_kind=(me or {}).get("agent_kind"), mcp_name=mcp_name, restart=restart,
                          openclaw_agent=openclaw_agent)
    except SetupError as e:
        typer.echo(f"tbr setup: {e}", err=True)
        return False
    typer.echo(render(checks), nl=False)
    failed = any(c.status == "error" for c in checks)
    if kind.strip().lower() in APP_KINDS:
        if not failed:
            typer.echo(_("Ready. Check everything with tbr doctor."))
        return not failed
    if not failed:
        typer.echo(_("Ready: the agent picks up tasks at the next timer pass. Check everything with tbr doctor; "
                     "watch with tmux attach -t taskbrigade.") if timer else
                   _("Ready: start a runner pass with tbr run --work-root %(dir)s", dir=work_root.expanduser().resolve()))
    return not failed


@app.command(help=_("Set up the agent on this machine. Runner agents (claude-code, codex, opencode): [runner] for "
                    "the agent kind, check tmux and agent command, install the timer (Linux: systemd user timer, "
                    "macOS: launchd). claude-desktop/codex-desktop: add the MCP server to the app configuration. "
                    "hermes/openclaw: install skill and check script, create the cron job/automation. Changed files "
                    "are backed up as <file>.bak."))
def setup(kind: str = typer.Argument(..., help="claude-code | codex | opencode | claude-desktop | codex-desktop | "
                                               "hermes | openclaw"),
          work_root: Path = typer.Option(None, "--work-root", help=_("Agent's work directory with .taskbrigade.toml "
                                                                     "(default: current)")),
          no_timer: bool = typer.Option(False, "--no-timer", help=_("Do not install a timer, cron job or automation "
                                                                    "(manual operation)")),
          name: str = typer.Option(None, "--name", help=_("Desktop apps: name of the MCP server entry (default "
                                                          "taskbrigade), e.g. for a second server")),
          restart: bool = typer.Option(False, "--restart", help=_("Restart the app or the framework's gateway "
                                                                  "afterwards (never without this flag)")),
          openclaw_agent: str = typer.Option(None, "--openclaw-agent", help=_(
              "OpenClaw: id of the OpenClaw agent that works the tasks (its workspace gets the skill, the automation "
              "runs for it; default: the agent of the existing automation, else main)"))):
    root = (work_root or Path.cwd()).expanduser().resolve()
    me = None
    try:
        me = Client(load_config(agent_config(root))).get("/agents/me")
    except (ApiError, ConfigError):
        pass  # tbr setup reports a missing configuration itself; tbr doctor checks the token.
    if not _run_setup(root, kind, not no_timer, me, name, restart, openclaw_agent):
        raise typer.Exit(1)


@app.command(help=_("Check this agent machine: connection, token and role, version, Git access, tmux/agent command, "
                    "timer, open or hanging tasks. Exit code 1 if a check fails."))
def doctor(work_root: Path = typer.Option(None, "--work-root", help=_("Agent's work directory (default: the "
                                                                      ".taskbrigade.toml tbr finds from here)")),
           fix: bool = typer.Option(False, "--fix", help=_("Switch what still uses a name from before TaskBrigade "
                                                           "0.8 or still calls tm (runner timer, MCP entries, "
                                                           "Hermes/OpenClaw job …; backups as *.bak)"))):
    from cli.doctor import doctor as do_doctor
    from cli.setup import render

    checks = do_doctor(work_root.expanduser().resolve() if work_root else None, fix=fix)
    typer.echo(render(checks), nl=False)
    errors = sum(c.status == "error" for c in checks)
    warnings = sum(c.status == "warn" for c in checks)
    typer.echo(_("%(errors)s error(s), %(warnings)s warning(s)", errors=errors, warnings=warnings))
    if errors:
        raise typer.Exit(1)


@app.command(help=_("Show your own actor (checks token and connectivity)."))
def whoami():
    _dump(_call("GET", "/agents/me"))


def _no_task(client=None) -> None:
    from cli.client import parallel_limit_note

    typer.echo(parallel_limit_note(client or _client()) or _("No task available."), err=True)


@app.command(help=_("Claim the next task – for reviewer agents also review jobs (marked as such). Exit 3 if there is "
                    "none."))
def claim(task: str = typer.Option(None, "--task", help=_("Claim a specific task")),
          reviews: bool = typer.Option(True, "--reviews/--no-reviews",
                                       help=_("Also claim review jobs (reviewer agents)")),
          as_json: bool = JsonOpt):
    body = {**({"task_nr": task} if task else {}), **({"reviews": True} if reviews else {})}
    result = _call("POST", "/agents/me/claim", json=body or None)
    if result is None:
        _no_task()
        raise typer.Exit(EXIT_NO_TASK)
    if result.get("job") == "review" and not as_json:
        typer.echo(_("Review job %(nr)s (claimed for you) – decide with `tbr review %(nr)s approve|reject|conflict`; "
                     "`tbr prepare --task %(nr)s` prepares a read-only checkout with REVIEW.md.",
                     nr=result["task_nr"]) + "\n")
        typer.echo(task_markdown(result, footer=False), nl=False)
        return
    _out(result, as_json)


@app.command(help=_("Claim a task, prepare repo/worktree/TASK.md and print the assignment for the agent.\n\n"
                    "For cron jobs: the LLM only runs afterwards if there is a task."))
def prepare(work_root: Path = typer.Option(None, help=_("Agent's work directory (default: current)")),
            wake_gate: bool = typer.Option(False, "--wake-gate",
                                           help=_('Print {"wakeAgent": false} without a task (Hermes cron) instead '
                                                  'of exit 3')),
            task: str = typer.Option(None, "--task", help=_("Claim or resume exactly this task")),
            resume_own: bool = typer.Option(False, "--resume-own",
                                            help=_("First resume your own task in progress (only if no other run can "
                                                   "work on it in parallel, e.g. Hermes cron)"))):
    from cli.prepare import PrepareError, instructions
    from cli.prepare import prepare as do_prepare
    from cli.prepare import tm_command

    root = (work_root or Path.cwd()).resolve()
    try:
        client = _client()
        resume = task.upper() if task else None
        if resume_own and not resume:
            from cli.prepare import own_running_task

            resume = own_running_task(client)
        result = do_prepare(client, root, resume)
    except (PrepareError, ApiError) as e:
        typer.echo(f"tbr prepare: {e}", err=True)
        raise typer.Exit(1) from e
    if result is None:
        if wake_gate:
            typer.echo('{"wakeAgent": false}')
            return
        _no_task(client)
        raise typer.Exit(EXIT_NO_TASK)
    t = result.task
    if result.review:  # REVIEW.md starts with its own header ("# TaskBrigade review job …").
        typer.echo((result.worktree / "REVIEW.md").read_text(), nl=False)
        return
    typer.echo(_("# TaskBrigade assignment %(nr)s (already claimed for you)", nr=t["task_nr"]) + "\n")
    typer.echo(task_markdown(t) + result.note + instructions(t, result.worktree, tm_command()), nl=False)


@app.command(help=_("Runner pass (for systemd timers): claim a task and start the agent – without a task nothing "
                    "happens."))
def run(work_root: Path = typer.Option(None, help=_("Agent's work directory with .taskbrigade.toml"))):
    from cli.prepare import PrepareError
    from cli.runner import RunnerError, run_once

    root = (work_root or Path.cwd()).resolve()
    try:
        typer.echo(run_once(Client(load_config(agent_config(root))), root))
    except (RunnerError, PrepareError, ApiError, ConfigError) as e:
        typer.echo(f"tbr run: {e}", err=True)
        raise typer.Exit(1) from e


@app.command(help=_("Close finished tmux windows of the runner (their content is appended to the run log first)."))
def cleanup(work_root: Path = typer.Option(None, help=_("Agent's work directory with .taskbrigade.toml")),
            min_age_hours: float = typer.Option(0.0, "--min-age-hours",
                                                help=_("Only windows that finished at least this many hours ago "
                                                       "(default 0: all finished windows)")),
            stale: bool = typer.Option(False, "--stale",
                                       help=_("Also close windows whose agent still runs although its task is no "
                                              "longer in progress"))):
    from cli.runner import RunnerError, cleanup_windows, runner_config, stale_windows

    root = (work_root or Path.cwd()).resolve()
    try:
        cfg = runner_config(root)
        if cfg.get("mode", "tmux") != "tmux":
            typer.echo(_("The runner does not use tmux (mode = \"%(mode)s\") – nothing to clean up",
                         mode=cfg.get("mode")))
            return
        session_name = cfg.get("tmux_session") or "taskbrigade"
        extra = set(stale_windows(Client(load_config(agent_config(root))), session_name, root)) if stale else set()
    except (RunnerError, ApiError, ConfigError) as e:
        typer.echo(f"tbr cleanup: {e}", err=True)
        raise typer.Exit(1) from e
    # 0 means "never" in [runner]; here it means "no minimum age", so pass a tiny positive value.
    closed = cleanup_windows(root, session_name, max(min_age_hours, 1e-9), stale=extra,
                             close_unknown_age=min_age_hours <= 0)
    typer.echo(_("%(num)s window(s) closed: %(names)s", num=len(closed), names=", ".join(closed)) if closed
               else _("No window to close"))


@app.command(context_settings={"allow_extra_args": True, "ignore_unknown_options": True},
             help=_("Run the agent command (after --): heartbeat during the run, then finish (submit/release)."))
def session(ctx: typer.Context, task_nr: str,
            work_root: Path = typer.Option(..., help=_("Agent's work directory")),
            review: bool = typer.Option(False, "--review", help=_("Review job (reviewer agents): runs while the "
                                                                  "review is held, gives it back undecided at the "
                                                                  "end"))):
    from cli.runner import RunnerError
    from cli.runner import session as do_session

    command = [a for a in ctx.args if a != "--"] if ctx.args and ctx.args[0] == "--" else list(ctx.args)
    if not command:
        typer.echo(_("Give the agent command after --"), err=True)
        raise typer.Exit(2)
    root = work_root.resolve()
    try:
        rc = do_session(Client(load_config(agent_config(root))), task_nr, root, command, review=review)
    except (RunnerError, ApiError, ConfigError) as e:
        typer.echo(f"tbr session: {e}", err=True)
        raise typer.Exit(1) from e
    raise typer.Exit(rc)


@app.command(help=_("Local MCP server (stdio) for desktop apps such as Claude Cowork or Codex Desktop."))
def mcp(work_root: Path = typer.Option(..., help=_("Agent's work directory with .taskbrigade.toml"))):
    import logging

    from cli.mcp_server import TaskBrigadeMCP, build_server

    logging.getLogger("httpx").setLevel(logging.WARNING)  # Avoid every API request in the app's MCP log.
    root = work_root.expanduser().resolve()
    try:
        client = Client(load_config(agent_config(root)))
    except ConfigError as e:
        typer.echo(f"tbr mcp: {e}", err=True)
        raise typer.Exit(2) from e
    build_server(TaskBrigadeMCP(client, root)).run("stdio")


@app.command(help=_("Show a task including its thread."))
def show(task_nr: str, as_json: bool = JsonOpt):
    _out(_call("GET", f"/tasks/{task_nr}"), as_json)


@app.command(help=_("Extend the lease. For a long background run: --waiting \"reason\" [--for 2h (max. 24h)] "
                    "[--watch-file LOG] – no stall report while waiting; --done-waiting ends it."))
def heartbeat(task_nr: str,
              waiting: str = typer.Option(None, "--waiting", help=_("Waiting for a background run: reason")),
              duration: str = typer.Option(None, "--for", help=_("How long to wait, e.g. 90m or 2h (default 2h, max. 24h); "
                                                                 "call again before it ends to extend")),
              watch_file: str = typer.Option(None, "--watch-file", help=_("Log file of the run: a change counts as "
                                                                           "activity, no change for the stall limit is "
                                                                           "reported")),
              done_waiting: bool = typer.Option(False, "--done-waiting", help=_("End the waiting state")),
              usage: str = typer.Option(None, "--usage",
                                        help=_("Token/cost usage: input=123,output=456,cost_cents=12,model=foo"))):
    try:
        r = heartbeat_task(_client(), task_nr, waiting, duration, watch_file, done_waiting,
                           parse_usage(usage))
    except ValueError as e:
        typer.echo(str(e), err=True)
        raise typer.Exit(2) from e
    except ApiError as e:
        _fail(e)
    typer.echo(waiting_report(r))
    if r.get("budget_exceeded"):
        typer.echo(_("The workflow budget is exhausted – finish the current step and stop."))


@app.command(help=_("Review decision on a task in for_review: approve (merge), reject (comment required), conflict "
                    "(return a merge/brain/changelog conflict to the assignee) or release (reviewer agents: give the "
                    "review job back undecided). Admins, the project owner and the task's reviewer."))
def review(task_nr: str,
           decision: str = typer.Argument(..., help="approve | reject | conflict | release"),
           comment: str = typer.Option(None, "--comment", help=_("Review comment (required for reject); '-' reads "
                                                                 "from stdin")),
           reason: str = typer.Option(None, "--reason", help=_("Reason for release")),
           as_json: bool = JsonOpt):
    comment = sys.stdin.read() if comment == "-" else comment
    released = decision.strip().lower() == "release"
    try:
        r = _orchestrate("tbr review", lambda c: review_task(c, task_nr, decision,
                                                             (reason or comment) if released else comment))
    except ValueError as e:
        typer.echo(str(e), err=True)
        raise typer.Exit(2) from e
    if as_json:
        _dump(r)
    elif released:
        typer.echo(_("%(nr)s: review job given back", nr=r["task_nr"]))
    else:
        typer.echo(f"{r['task_nr']}: {r['status']}" + (f" – PR: {r['pr_url']}" if r.get("pr_url") else ""))


@app.command(help=_("Ask a question → state questions (the session should end afterwards)."))
def ask(task_nr: str, body: str = typer.Argument(..., help=_("Question (Markdown); '-' reads from stdin"))):
    body = sys.stdin.read() if body == "-" else body
    r = _call("POST", f"/tasks/{task_nr}/ask", json={"body": body})
    typer.echo(_("%(nr)s: %(status)s – the question is in the inbox.", nr=r["task_nr"], status=r["status"]))


@app.command(help=_("Write a comment on the task."))
def comment(task_nr: str, body: str = typer.Argument(..., help=_("Comment; '-' reads from stdin"))):
    body = sys.stdin.read() if body == "-" else body
    _call("POST", f"/tasks/{task_nr}/comments", json={"type": "comment", "body": body})
    typer.echo(_("Commented."))


def _git(*args: str) -> str:
    return subprocess.run(["git", *args], check=True, capture_output=True, text=True).stdout.strip()


@app.command(help=_("Submit the result → for_review, a pull request is created. The branch must be pushed."))
def submit(task_nr: str,
           branch: str = typer.Option(None, help=_("Task branch (default: current Git branch)")),
           sha: str = typer.Option(None, help=_("Commit SHA (default: HEAD)")),
           summary: str = typer.Option(..., help=_("Summary for review/PR (Markdown)")),
           open_point: list[str] = typer.Option(None, "--open-point",
                                                help=_("Open point for a follow-up task (repeatable; one point "
                                                       "per option)")),
           output: list[str] = typer.Option(None, "--output",
                                             help=_("Workflow output NAME=VALUE (repeatable; @file reads the value "
                                                    "from a file)")),
           output_json: Path = typer.Option(None, "--output-json",
                                             exists=True, dir_okay=False, readable=True,
                                             help=_("JSON object of workflow outputs; --output overrides the same name")),
           usage: str = typer.Option(None, "--usage",
                                     help=_("Token/cost usage: input=123,output=456,cost_cents=12,model=foo"))):
    try:
        branch = branch or _git("rev-parse", "--abbrev-ref", "HEAD")
        sha = sha or _git("rev-parse", "HEAD")
        outputs = parse_output_args(output, output_json)
        parsed_usage = parse_usage(usage)
    except (subprocess.CalledProcessError, FileNotFoundError) as e:
        typer.echo(_("Give --branch/--sha (no Git repository in the current directory)"), err=True)
        raise typer.Exit(2) from e
    except ValueError as e:
        typer.echo(str(e), err=True)
        raise typer.Exit(2) from e
    body = {"branch_name": branch, "commit_sha": sha, "summary": summary,
            "open_points": [p for p in open_point or [] if p.strip()]}
    if outputs is not None:
        body["outputs"] = outputs
    if parsed_usage is not None:
        body["usage"] = parsed_usage
    r = _call("POST", f"/tasks/{task_nr}/submit", json=body)
    typer.echo(f"{r['task_nr']}: {r['status']} – PR: {r.get('pr_url') or '–'}")


@app.command(help=_("Give the task back without a result → defined."))
def release(task_nr: str, reason: str = typer.Option(..., help=_("Why the task is given back"))):
    r = _call("POST", f"/tasks/{task_nr}/release", json={"reason": reason})
    typer.echo(f"{r['task_nr']}: {r['status']}")


@app.command(help=_("Abort the task: beyond your capabilities → a human reassigns it (no new claim)."))
def abort(task_nr: str, reason: str = typer.Option(..., help=_("Why you cannot solve the task"))):
    r = _call("POST", f"/tasks/{task_nr}/abort", json={"reason": reason})
    typer.echo(_("%(nr)s: %(status)s – waiting for reassignment.", nr=r["task_nr"], status=r["status"]))


@app.command(help=_("Attach files to a task (stored in the project repo under tasks/<task>/)."))
def attach(task_nr: str, files: list[Path] = typer.Argument(..., exists=True, dir_okay=False, readable=True,
                                                          help=_("One or more files"))):
    from cli.attachments import result_lines, upload_paths

    try:
        result = upload_paths(_client(), task_nr, files)
    except ApiError as e:
        _fail(e)
    for line in result_lines(result):
        typer.echo(line)


@app.command("list", help=_("List tasks (default: your own)."))
def list_tasks(status: list[str] = typer.Option(None, help=_("Can be given several times")),
               project: str = typer.Option(None), assigned_to: str = typer.Option("me", help="me | none | <name> | all"),
               due: bool = typer.Option(False, "--due", help=_("Show the due date column")),
               owner: str = typer.Option(None, "--owner", help=_("Only projects of this owner (me | <name>)")),
               sprint: str = typer.Option(None, "--sprint", help=_("Only tasks of this sprint (id, or name with "
                                                                   "--project); 'none' = tasks without a sprint")),
               reviewer: str = typer.Option(None, "--reviewer", help=_("Only tasks with this reviewer (me | none | "
                                                                       "agents | <name>)")),
               as_json: bool = JsonOpt):
    params = {"limit": 200}
    if reviewer:
        params["reviewer"] = reviewer
    if sprint:
        params["sprint"] = "none" if is_no_sprint(sprint) else str(_sprint_id(project, sprint))
    if owner:
        params["owner"] = owner
    if status:
        params["status"] = status
    if project:
        params["project"] = project
    if assigned_to != "all":
        params["assigned_to"] = assigned_to
    data = _call("GET", "/tasks", params=params)
    # Own list without filters: a reviewer agent also sees its review jobs (marked "review").
    own = assigned_to == "me" and not (status or project or owner or sprint or reviewer)
    jobs = _review_jobs() if own else []
    if as_json:
        _dump({**data, "review_jobs": jobs} if own else data)
        return
    for t in data["items"]:
        due_col = f"{t.get('due_date') or '-':<10} " if due else ""
        typer.echo(f"{t['task_nr']:<12} {t['status']:<18} P{t['prio']} {t.get('assigned_to') or '-':<12} "
                   f"{due_col}{t['title']}")
    for t in jobs:
        due_col = f"{t.get('due_date') or '-':<10} " if due else ""
        state = _("review (claimed)") if t.get("review_claimed") else _("review")
        typer.echo(f"{t['task_nr']:<12} {state:<18} P{t['prio']} {t.get('assigned_to') or '-':<12} "
                   f"{due_col}{t['title']}")
    typer.echo(_("(%(total)s task(s))", total=data["total"]) + (
        _(" + %(num)s review job(s)", num=len(jobs)) if jobs else ""), err=True)


def _review_jobs() -> list[dict]:
    """Review jobs of the calling agent; none for humans, other roles and older servers."""
    try:
        return _client().get("/agents/me/reviews").get("items") or []
    except ApiError:
        return []


@app.command(help=_("List the active projects: key, privacy, owner, name."))
def projects(as_json: bool = JsonOpt):
    items = [p for p in _call("GET", "/projects") if p.get("status", "active") == "active"]
    if as_json:
        _dump(items)
        return
    for p in items:
        typer.echo(f"{p['key']:<8} {p.get('privacy') or '-':<10} {p.get('owner') or '-':<12} {p['name']}")


@app.command(help=_("List the agents you may assign (own, your owner's and public ones; name, runtime local/cloud, "
                     "owner) and the humans tasks can be assigned to."))
def agents(as_json: bool = JsonOpt,
           show_all: bool = typer.Option(False, "--all", help=_("Admins: all agents, not only own and public ones"))):
    actors = _call("GET", "/actors", params={"all": "true"} if show_all else None)
    if as_json:
        _dump(actors)
        return
    for a in sorted(actors, key=lambda a: (a["actor_type"] != "agent", a["name"])):
        kind = a["runtime"] if a["actor_type"] == "agent" else _("human")
        typer.echo(f"{a['name']:<16} {kind:<6} {a['display_name']}{agent_scope(a)}")


@app.command(help=_("Create a task (for orchestrators)."))
def create(project: str = typer.Option(...), title: str = typer.Option(...),
           assign: str = typer.Option(None, help=_("Actor")), prio: int = typer.Option(3, min=1, max=5),
           description: str = typer.Option(None), acceptance: str = typer.Option(None, help=_("Acceptance criteria")),
           parent: str = typer.Option(None, help=_("Parent task")),
           depends_on: list[str] = typer.Option(None, "--depends-on", help=_("Task this one depends on (repeatable); "
                                                                             "claimable once all are done")),
           draft: bool = typer.Option(False, "--draft", help=_("Create as draft (claimable only after publishing)")),
           due: str = typer.Option(None, "--due", help=_("Due date YYYY-MM-DD")),
           sprint: str = typer.Option(None, "--sprint", help=_("Sprint (id or name); in a sprint that is not "
                                                               "released yet the task becomes a draft")),
           tier: str = typer.Option(None, "--tier", help=_("Model tier: simple, standard or complex – the agent maps "
                                                           "it to a model")),
           model: str = typer.Option(None, "--model", help=_("Concrete model; must be on the agent's allow list")),
           reviewer: str = typer.Option(None, "--reviewer", help=_("Reviewer: a human or an agent with role reviewer "
                                                                   "(default: the project's default reviewer; 'none' "
                                                                   "= the owner and admins)")),
           data_class: str = typer.Option(None, "--data-class", help=_("Data class cloud_ok, local_only or sealed "
                                                                       "(default: the project's privacy)")),
           as_json: bool = JsonOpt):
    try:
        deps = parse_depends_on(depends_on)
    except ValueError as e:
        typer.echo(str(e), err=True)
        raise typer.Exit(2) from e
    body = {"data_class": _data_class(data_class),"project": project, "title": title, "prio": prio, "assigned_to": assign, "description": description,
            "reviewer": reviewer.strip() if reviewer else None,
            "acceptance_criteria": acceptance, "parent_task_nr": parent, "depends_on": deps,
            "status": "draft" if draft else "defined", "due_date": _due(due), "model_tier": _tier(tier),
            "model": model_value(model),
            "sprint": _sprint_id(project, sprint) if sprint and not is_no_sprint(sprint) else None}
    r = _call("POST", "/tasks", json={k: v for k, v in body.items() if v is not None})
    if as_json:
        _dump(r)
    else:
        typer.echo(r["task_nr"])
    for warning in create_warnings(r):
        typer.echo(_("Warning: %(text)s", text=warning), err=True)


def create_warnings(t: dict) -> list[str]:
    """Explain why a newly created task has not yet been claimed."""
    nr, out = t["task_nr"], []
    if t.get("status") == "draft" and t.get("sprint_status") == "planned":
        out.append(_("%(nr)s is a draft because sprint '%(sprint)s' is not released yet – the sprint release publishes "
                     "it (tbr sprint release).", nr=nr, sprint=t.get("sprint_name")))
    elif t.get("status") == "draft":
        out.append(_("%(nr)s is a draft and is only claimed after it is published.", nr=nr))
    if not t.get("assigned_to"):
        out.append(_("%(nr)s is not assigned to anyone and will not be claimed – `tbr update %(nr)s --assign <agent>`.",
                     nr=nr))
    return out


def _sprint_id(project: str | None, value: str) -> int:
    """Sprint id from id or name (exit 2 if unknown)."""
    try:
        return resolve_sprint(_client(), project, value)
    except ApiError as e:
        _fail(e)
    except ValueError as e:
        typer.echo(str(e), err=True)
        raise typer.Exit(2) from e


def _tier(value: str | None) -> str | None:
    try:
        return parse_tier(value)
    except ValueError as e:
        typer.echo(str(e), err=True)
        raise typer.Exit(2) from e


def _data_class(value: str | None) -> str | None:
    try:
        return parse_data_class(value)
    except ValueError as e:
        typer.echo(str(e), err=True)
        raise typer.Exit(2) from e


def _due(value: str | None) -> str | None:
    try:
        return parse_due(value)
    except ValueError as e:
        typer.echo(str(e), err=True)
        raise typer.Exit(2) from e


def _orchestrate(action: str, fn):
    """Orchestrator call with a clear error for permissions, version conflicts, or status."""
    try:
        return fn(_client())
    except ApiError as e:
        typer.echo(_("Error: %(error)s", error=explain(e, action)), err=True)
        raise typer.Exit(1) from e


@app.command(help=_("Change a task (for orchestrators): due date, assignee, reviewer, prio, title, description, "
                    "criteria."))
def update(task_nr: str,
           due: str = typer.Option(None, "--due", help=_("Due date YYYY-MM-DD; 'none' removes it")),
           assign: str = typer.Option(None, "--assign", help=_("Actor; 'none' removes the assignment")),
           prio: int = typer.Option(None, "--prio", min=1, max=5),
           title: str = typer.Option(None, "--title"),
           description: str = typer.Option(None, "--description", help=_("Description; '-' reads from stdin")),
           acceptance: str = typer.Option(None, "--acceptance", help=_("Acceptance criteria; '-' reads from stdin")),
           sprint: str = typer.Option(None, "--sprint", help=_("Sprint (id or name); 'none' detaches the task")),
           tier: str = typer.Option(None, "--tier", help=_("Model tier: simple, standard or complex; 'none' removes "
                                                           "it")),
           model: str = typer.Option(None, "--model", help=_("Concrete model from the agent's allow list; 'none' "
                                                             "removes it")),
           reviewer: str = typer.Option(None, "--reviewer", help=_("Reviewer (human or reviewer agent); 'none' "
                                                                   "removes it – the owner and admins review")),
           data_class: str = typer.Option(None, "--data-class", help=_("Data class cloud_ok, local_only or sealed; "
                                                                       "'inherit' takes the project's privacy")),
           depends_on: list[str] = typer.Option(None, "--depends-on",
                                                help=_("Task this one depends on (repeatable); replaces the list, "
                                                       "'none' removes all – claimable once all are done")),
           version: int = typer.Option(None, "--version",
                                       help=_("Expected row_version (If-Match); default: the one just read")),
           as_json: bool = JsonOpt):
    changes = {}
    if depends_on is not None:
        try:
            changes["depends_on"] = parse_depends_on(depends_on)
        except ValueError as e:
            typer.echo(str(e), err=True)
            raise typer.Exit(2) from e
    if due is not None:
        changes["due_date"] = _due(due)
    if assign is not None:
        changes["assigned_to"] = None if assign.strip().lower() in ("none", "-", "") else assign.strip()
    if prio is not None:
        changes["prio"] = prio
    if title is not None:
        changes["title"] = title
    if description is not None:
        changes["description"] = sys.stdin.read() if description == "-" else description
    if acceptance is not None:
        changes["acceptance_criteria"] = sys.stdin.read() if acceptance == "-" else acceptance
    if tier is not None:
        changes["model_tier"] = _tier(tier)
    if model is not None:
        changes["model"] = model_value(model)
    if reviewer is not None:
        changes["reviewer"] = None if reviewer.strip().lower() in ("none", "-", "") else reviewer.strip()
    if data_class is not None and _data_class(data_class):
        changes["data_class"] = _data_class(data_class)
    if sprint is not None:
        if is_no_sprint(sprint):
            changes["sprint"] = None
        else:
            project = None if sprint.strip().isdigit() else _orchestrate(
                "tbr update", lambda c: c.get(f"/tasks/{task_nr.upper()}")["project"])
            changes["sprint"] = _sprint_id(project, sprint)
    if not changes:
        typer.echo(_("Nothing to change – give at least one option (tbr update --help)."), err=True)
        raise typer.Exit(2)
    r = _orchestrate("tbr update", lambda c: patch_task(c, task_nr.upper(), changes, version))
    if as_json:
        _dump(r)
        return
    shown = {**r, "sprint": r.get("sprint_name")}
    shown["depends_on"] = ", ".join(d["task_nr"] for d in r.get("dependencies") or [])
    fields = [k if k in ("description", "acceptance_criteria") else f"{k}={shown.get(k) or '–'}" for k in changes]
    typer.echo(_("%(nr)s changed (version %(version)s): %(fields)s", nr=r["task_nr"], version=r["row_version"],
                 fields=", ".join(fields)))
    if "sprint" in changes and r.get("status") == "draft" and r.get("sprint_status") == "planned":
        typer.echo(_("Note: %(nr)s is a draft until sprint '%(sprint)s' is released.", nr=r["task_nr"],
                     sprint=r.get("sprint_name")), err=True)


@app.command(help=_("Cancel a task → cancelled (admins and orchestrators, even if they are not the assignee)."))
def cancel(task_nr: str, reason: str = typer.Option(..., "--reason", help=_("Reason (goes into the thread)"))):
    r = _orchestrate("tbr cancel", lambda c: do_transition(c, task_nr.upper(), "cancelled", reason))
    typer.echo(f"{r['task_nr']}: {r['status']}")


@app.command(help=_("Set a task back to defined (permissions as in the web UI, e.g. publish a draft; answering a "
                    "question and reopening aborted tasks only by admins)."))
def reset(task_nr: str,
          reason: str = typer.Option(None, "--reason", help=_("Reason or answer (goes into the thread)"))):
    r = _orchestrate("tbr reset", lambda c: do_transition(c, task_nr.upper(), "defined", reason))
    typer.echo(f"{r['task_nr']}: {r['status']}")


@app.command(help=_("Duplicate a task into a new draft of the same project (admins, orchestrators and the project "
                    "owner); prints the new task number."))
def duplicate(task_nr: str,
              assign: str = typer.Option(None, "--assign", help=_("Assign the draft to this actor (default: nobody)")),
              title: str = typer.Option(None, "--title", help=_("Title (default: '(duplicate) ' + the original "
                                                                "title)")),
              as_json: bool = JsonOpt):
    body = {k: v for k, v in (("assign", assign), ("title", title)) if v is not None and v.strip()}
    r = _orchestrate("tbr duplicate", lambda c: duplicate_task(c, task_nr, body))
    if as_json:
        _dump(r)
    else:
        typer.echo(r["task_nr"])
    for warning in create_warnings(r):
        typer.echo(_("Warning: %(text)s", text=warning), err=True)


# --- Sprints ----------------------------------------------------------------------------------

sprint_app = typer.Typer(help=_("Sprints of a project: list, show, create, update, release, close, reopen, "
                                "batch creation. Sprints are addressed by id or name."), no_args_is_help=True)
app.add_typer(sprint_app, name="sprint")
ProjectOpt = typer.Option(..., "--project", "-p", help=_("Project key"))
SprintArg = typer.Argument(..., help=_("Sprint id or name"))


def _sprint_path(project: str, sprint: str) -> str:
    return f"/projects/{project.strip().upper()}/sprints/{_sprint_id(project, sprint)}"


def _iso_date(value: str | None, option: str) -> str | None:
    from datetime import date

    if not value:
        return None
    try:
        return date.fromisoformat(value.strip()).isoformat()
    except ValueError as e:
        typer.echo(_("%(option)s '%(value)s' is invalid – format YYYY-MM-DD", option=option, value=value), err=True)
        raise typer.Exit(2) from e


_UNSET = object()


def _patch_date(value: str | None, option: str):
    """A sprint date for PATCH: omitted stays unset, 'none' or '-' clears it, otherwise YYYY-MM-DD."""
    if value is None:
        return _UNSET
    text = value.strip()
    if text.lower() in ("none", "-"):
        return None
    return _iso_date(text, option)


@sprint_app.command("list", help=_("List the sprints of a project."))
def sprint_list(project: str = ProjectOpt,
                status: list[str] = typer.Option(None, help=_("planned | released | closed; can be given several "
                                                              "times")),
                as_json: bool = JsonOpt):
    data = _call("GET", f"/projects/{project.strip().upper()}/sprints", params={"status": status} if status else None)
    if as_json:
        _dump(data)
        return
    for sp in data:
        typer.echo(sprint_line(sp))
    typer.echo(_("(%(total)s sprint(s))", total=len(data)), err=True)


@sprint_app.command("show", help=_("Show a sprint with goal and tasks."))
def sprint_show(sprint: str = SprintArg, project: str = ProjectOpt, as_json: bool = JsonOpt):
    data = _call("GET", _sprint_path(project, sprint))
    if as_json:
        _dump(data)
        return
    typer.echo(sprint_line(data))
    typer.echo(_("Goal: %(goal)s", goal=data.get("goal") or "–"))
    for t in data["tasks"]:
        typer.echo(f"  {t['task_nr']:<12} {t['status']:<18} P{t['prio']} {t.get('assigned_to') or '-':<12} "
                   f"{t['title']}")


@sprint_app.command("create", help=_("Create a sprint (admins, orchestrators and the project owner). Tasks in it stay "
                                     "drafts until the sprint is released."))
def sprint_create(project: str = ProjectOpt, name: str = typer.Option(..., "--name"),
                  goal: str = typer.Option(None, "--goal", help=_("Sprint goal (Markdown); '-' reads from stdin")),
                  start: str = typer.Option(None, "--start", help=_("Start date YYYY-MM-DD")),
                  end: str = typer.Option(None, "--end", help=_("End date YYYY-MM-DD")),
                  as_json: bool = JsonOpt):
    body = {"name": name, "goal": sys.stdin.read() if goal == "-" else goal,
            "start_date": _iso_date(start, "--start"), "end_date": _iso_date(end, "--end")}
    r = _orchestrate("tbr sprint create", lambda c: c.request(
        "POST", f"/projects/{project.strip().upper()}/sprints",
        json={k: v for k, v in body.items() if v is not None}).json())
    if as_json:
        _dump(r)
        return
    typer.echo(_("Sprint '%(name)s' created (id %(id)s, %(status)s).", name=r["name"], id=r["id"], status=r["status"]))


@sprint_app.command("update", help=_("Edit a sprint's name, goal or dates (admins, orchestrators and the project "
                                     "owner). Only the given fields change; the status stays as it is."))
def sprint_update(sprint: str = SprintArg, project: str = ProjectOpt,
                  name: str = typer.Option(None, "--name", help=_("New name")),
                  goal: str = typer.Option(None, "--goal", help=_("New goal (Markdown); '-' reads from stdin, an "
                                                                  "empty string clears the goal")),
                  start: str = typer.Option(None, "--start", help=_("Start date YYYY-MM-DD; 'none' clears it")),
                  end: str = typer.Option(None, "--end", help=_("End date YYYY-MM-DD; 'none' clears it")),
                  as_json: bool = JsonOpt):
    body: dict = {}
    if name is not None:
        body["name"] = name
    if goal is not None:
        body["goal"] = sys.stdin.read() if goal == "-" else goal
    for option, key, value in (("--start", "start_date", start), ("--end", "end_date", end)):
        parsed = _patch_date(value, option)
        if parsed is not _UNSET:
            body[key] = parsed
    if not body:
        typer.echo(_("Give at least one of --name, --goal, --start or --end."), err=True)
        raise typer.Exit(2)
    path = _sprint_path(project, sprint)
    _orchestrate("tbr sprint update", lambda c: c.request("PATCH", path, json=body).json())
    data = _call("GET", path)  # The patch payload has no tasks; show the sprint the same way as `sprint show`.
    if as_json:
        _dump(data)
        return
    typer.echo(sprint_line(data))
    typer.echo(_("Goal: %(goal)s", goal=data.get("goal") or "–"))
    for t in data["tasks"]:
        typer.echo(f"  {t['task_nr']:<12} {t['status']:<18} P{t['prio']} {t.get('assigned_to') or '-':<12} "
                   f"{t['title']}")


@sprint_app.command("release", help=_("Release a sprint (admins and the project owner): every draft with description "
                                      "and acceptance criteria becomes defined, the others stay drafts and are "
                                      "listed. Run again after completing them."))
def sprint_release(sprint: str = SprintArg, project: str = ProjectOpt, as_json: bool = JsonOpt):
    path = _sprint_path(project, sprint)
    r = _orchestrate("tbr sprint release", lambda c: c.request("POST", f"{path}/release").json())
    if as_json:
        _dump(r)
        return
    typer.echo(release_report(r))


@sprint_app.command("close", help=_("Close a released sprint (admins and the project owner). Open tasks move to "
                                    "another sprint (--move-to) or are detached (--detach); running tasks keep "
                                    "working, finished ones stay in the sprint."))
def sprint_close(sprint: str = SprintArg, project: str = ProjectOpt,
                 move_to: str = typer.Option(None, "--move-to", help=_("Sprint (id or name) for the open tasks")),
                 detach: bool = typer.Option(False, "--detach", help=_("Detach open tasks from the sprint")),
                 as_json: bool = JsonOpt):
    if move_to and detach:
        typer.echo(_("Give either --move-to or --detach, not both."), err=True)
        raise typer.Exit(2)
    path = _sprint_path(project, sprint)
    body = {"move_to": _sprint_id(project, move_to)} if move_to else {"detach": detach}
    r = _orchestrate("tbr sprint close", lambda c: c.request("POST", f"{path}/close", json=body).json())
    if as_json:
        _dump(r)
        return
    typer.echo(close_report(r, move_to))


@sprint_app.command("reopen", help=_("Reopen a closed sprint (admins and the project owner): status goes back to "
                                     "released. With --restore-tasks (default) the tasks that were moved out when "
                                     "it was closed return, their status unchanged; tasks moved elsewhere later or "
                                     "finished are skipped and listed."))
def sprint_reopen(sprint: str = SprintArg, project: str = ProjectOpt,
                  restore_tasks: bool = typer.Option(True, "--restore-tasks/--no-restore-tasks",
                                                     help=_("Move the tasks that were moved out when the sprint was "
                                                            "closed back into it")),
                  no_restore: bool = typer.Option(False, "--no-restore",
                                                  help=_("Do not move those tasks back (same as --no-restore-tasks)")),
                  as_json: bool = JsonOpt):
    if no_restore:
        restore_tasks = False
    path = _sprint_path(project, sprint)
    r = _orchestrate("tbr sprint reopen", lambda c: c.request(
        "POST", f"{path}/reopen", json={"restore_tasks": restore_tasks}).json())
    if as_json:
        _dump(r)
        return
    typer.echo(reopen_report(r))


@sprint_app.command("add-tasks", help=_("Create several tasks in a sprint at once from a file: one task per line, "
                                        "'%(format)s' (agent and prio optional, # starts a comment). They are created "
                                        "as drafts – complete each one with tbr update, then release the sprint.",
                                        format=BATCH_FORMAT))
def sprint_add_tasks(sprint: str = SprintArg, project: str = ProjectOpt,
                     file: Path = typer.Option(..., "--file", "-f", allow_dash=True,
                                               help=_("File with one task per line; '-' reads from stdin")),
                     dry_run: bool = typer.Option(False, "--dry-run", help=_("Only check and show the lines")),
                     as_json: bool = JsonOpt):
    try:
        text = sys.stdin.read() if str(file) == "-" else file.read_text(encoding="utf-8")
    except OSError as e:
        typer.echo(_("Cannot read %(file)s: %(error)s", file=file, error=e), err=True)
        raise typer.Exit(2) from e
    entries, errors = parse_batch(text)
    if errors or not entries:
        for err in errors or [_("No tasks given")]:
            typer.echo(err, err=True)
        typer.echo(_("Nothing created – fix the lines first (format: %(format)s).", format=BATCH_FORMAT), err=True)
        raise typer.Exit(2)
    if dry_run:
        for e in entries:
            typer.echo(f"P{e['prio']} {e['assigned_to'] or '-':<12} {e['title']}")
        typer.echo(_("(%(total)s task(s), nothing created)", total=len(entries)), err=True)
        return
    path = _sprint_path(project, sprint)
    r = _orchestrate("tbr sprint add-tasks", lambda c: c.request("POST", f"{path}/tasks",
                                                                json={"tasks": entries}).json())
    if as_json:
        _dump(r)
        return
    for t in r["created"]:
        typer.echo(f"{t['task_nr']:<12} {t['status']:<8} P{t['prio']} {t.get('assigned_to') or '-':<12} {t['title']}")
    typer.echo(_("%(num)s draft(s) created in sprint '%(sprint)s' – complete each one (tbr update <nr> --description … "
                 "--acceptance …), then tbr sprint release.", num=len(r["created"]), sprint=r["sprint"]["name"]),
               err=True)


notify_app = typer.Typer(help=_("Telegram notifications: status, mute all messages or review requests only. Your own "
                                "settings, or with --user those of a human linked with Telegram (orchestrators and "
                                "admins; the person is told in Telegram)."), no_args_is_help=True)
app.add_typer(notify_app, name="notify")
UserOpt = typer.Option(None, "--user", "-u", help=_("Human whose settings are changed (orchestrators and admins)"))
UntilArg = typer.Argument(None, help=_("Duration or end, e.g. 30m, 2h, 1d, 1w, 18:00, 'until 18:00'; empty = until "
                                       "switched back"))


def _notify(action: str, until: list[str] | None, user: str | None, as_json: bool) -> None:
    from cli.client import notify_report, notify_request

    try:
        data = notify_request(_client(), action, " ".join(until or []), user)
    except ApiError as e:
        _fail(e)
    if as_json:
        _dump(data)
        return
    typer.echo(notify_report(data))


@notify_app.command("status", help=_("Show whether messages and review requests are muted."))
def notify_status(user: str = UserOpt, as_json: bool = JsonOpt):
    _notify("status", None, user, as_json)


@notify_app.command("pause", help=_("Mute all messages, for a duration or until switched back (tbr notify resume)."))
def notify_pause(until: list[str] = UntilArg, user: str = UserOpt, as_json: bool = JsonOpt):
    _notify("pause", until, user, as_json)


@notify_app.command("resume", help=_("Unmute all messages."))
def notify_resume(user: str = UserOpt, as_json: bool = JsonOpt):
    _notify("resume", None, user, as_json)


@notify_app.command("reviews", help=_("Review requests only: off [duration] mutes them, on unmutes them."))
def notify_reviews(switch: str = typer.Argument(..., help="off | on"), until: list[str] = UntilArg,
                   user: str = UserOpt, as_json: bool = JsonOpt):
    value = switch.strip().lower()
    if value not in ("off", "on", "aus", "an"):
        typer.echo(_("Expected off or on, not '%(value)s'.", value=switch), err=True)
        raise typer.Exit(2)
    _notify("reviews_off" if value in ("off", "aus") else "reviews_on", until, user, as_json)


def _read_git_request(stream) -> dict[str, str]:
    attrs = {}
    for line in stream:
        line = line.rstrip("\n")
        if not line:
            break
        key, _sep, value = line.partition("=")
        attrs[key] = value
    return attrs


def _host_matches(request: dict[str, str], gitea_url: str | None) -> bool:
    """Return credentials only for the Gitea host."""
    if not gitea_url:
        return False
    g = urlparse(gitea_url)
    req_host = request.get("host", "")
    return bool(g.scheme and g.netloc and req_host == g.netloc
                and request.get("protocol") == g.scheme)


sftp_app = typer.Typer(help=_("SFTP storage of the TaskBrigade server (only the configured server, only inside its "
                               "base folder): info, put, ls, get."), no_args_is_help=True)
app.add_typer(sftp_app, name="sftp")


def _sftp(fn):
    try:
        return fn()
    except ApiError as e:
        _fail(e)
    except sftp_lib.SftpError as e:
        typer.echo(_("Error: %(error)s", error=e), err=True)
        raise typer.Exit(1) from e


@sftp_app.command("info", help=_("Show host, port, host key, base folder and your SFTP user (never the password)."))
def sftp_info(task_nr: str = typer.Argument(None, help=_("Also show the folder of this task")),
              as_json: bool = JsonOpt):
    data = _sftp(lambda: sftp_lib.info(_client(), task_nr))
    if as_json:
        return _dump(data)
    typer.echo(_("Server: %(host)s:%(port)s (host key %(key)s)", host=data["host"], port=data["port"],
                 key=data.get("host_key") or _("not confirmed yet")))
    typer.echo(_("User: %(user)s", user=data["username"]))
    typer.echo(_("Base folder: %(base)s", base=data["base_dir"]))
    if data.get("task_dir"):
        typer.echo(_("Task folder: %(dir)s", dir=data["task_dir"]))


@sftp_app.command("put", help=_("Upload files or folders into the task's folder on the SFTP server (sub-folders are "
                                "created); every upload is noted in the task with size and SHA-256."))
def sftp_put(task_nr: str, files: list[Path] = typer.Argument(..., exists=True, readable=True),
             to: str = typer.Option(None, "--to", help=_("Sub-path inside the task's folder"))):
    done = _sftp(lambda: sftp_lib.upload_for_task(_client(), task_nr, files, to))
    typer.echo(_("Uploaded and noted in %(nr)s:", nr=task_nr.upper()))
    for line in sftp_lib.upload_lines(done):
        typer.echo(f"  {line}")


@sftp_app.command("ls", help=_("List a folder: a task number (its folder), a path inside the base folder, or nothing "
                               "(the base folder)."))
def sftp_ls(where: str = typer.Argument(None, help=_("Task number or path")), as_json: bool = JsonOpt):
    def run():
        client = _client()
        path = sftp_lib.remote_path(client, where)
        return path, sftp_lib.ls(sftp_lib.target(client), path)

    path, entries = _sftp(run)
    if as_json:
        return _dump(entries)
    typer.echo(f"{path}:")
    for line in sftp_lib.listing_lines(entries) or [_("(empty)")]:
        typer.echo(f"  {line}")


@sftp_app.command("get", help=_("Download a file or folder from inside the base folder."))
def sftp_get(path: str, target: Path = typer.Argument(None, help=_("Local target (default: name in the current "
                                                                    "folder)")),
             force: bool = typer.Option(False, "--force", help=_("Overwrite an existing local file"))):
    def run():
        client = _client()
        remote = sftp_lib.remote_path(client, path)
        name = remote.rstrip("/").rsplit("/", 1)[-1] or "download"
        local = target / name if target is not None and target.is_dir() else target or Path(name)
        if local.exists() and not force:
            raise sftp_lib.SftpError(_("%(path)s exists – use --force to overwrite", path=local))
        sftp_lib.get(sftp_lib.target(client), remote, local)
        return remote, local

    remote, local = _sftp(run)
    typer.echo(_("%(remote)s → %(local)s", remote=remote, local=local))


@app.command("git-credential", help=_('Git credential helper: git config --global credential.helper '
                                     '"!tbr git-credential".'))
def git_credential(operation: str = typer.Argument(..., help="get | store | erase")):
    if operation != "get":
        _read_git_request(sys.stdin)  # store/erase: do not save tokens to files.
        return
    request = _read_git_request(sys.stdin)
    try:
        creds = _client().get("/agents/me/git-credentials")
    except ApiError as e:
        typer.echo(f"tbr git-credential: {e}", err=True)
        raise typer.Exit(1) from e
    if not _host_matches(request, creds.get("gitea_url")):
        return
    sys.stdout.write(f"username={creds['username']}\npassword={creds['token']}\n")


if __name__ == "__main__":
    app()


mail_app = typer.Typer(help=_("Send work results by email through the server: the server checks the project's "
                              "recipient list and data class, a human approves, the server sends. You never get SMTP "
                              "credentials."), no_args_is_help=True)
app.add_typer(mail_app, name="mail")


@mail_app.command("send", help=_("File a send request for a task's result. Attachments are references the server "
                                 "reads itself: attachment:<id> (task attachment), branch:<path> (file on the task "
                                 "branch), merge:<path> (file in the merge commit)."))
def mail_send(task_nr: str,
              to: list[str] = typer.Option(..., "--to", help=_("Recipient (repeatable); only addresses on the "
                                                                 "project's list")),
              subject: str = typer.Option(..., "--subject", "-s", help=_("Subject")),
              body: str = typer.Option(None, "--body", "-b", help=_("Text (Markdown)")),
              body_file: Path = typer.Option(None, "--body-file", exists=True, readable=True,
                                             help=_("Read the text from a file")),
              cc: list[str] = typer.Option(None, "--cc", help=_("Copy recipient (repeatable)")),
              attach: list[str] = typer.Option(None, "--attach", "-a", help=_("Attachment reference (repeatable)")),
              on_approve: bool = typer.Option(False, "--on-approve", help=_("Send only when the task is approved")),
              as_json: bool = JsonOpt):
    from cli.client import email_report, send_email

    text = body_file.read_text(encoding="utf-8") if body_file else body
    if not (text or "").strip():
        typer.echo(_("Give the text with --body or --body-file."), err=True)
        raise typer.Exit(2)
    try:
        data = send_email(_client(), task_nr, to, subject, text, cc, attach, on_approve)
    except ApiError as e:
        _fail(e)
    if as_json:
        return _dump(data)
    typer.echo(email_report(data))


@mail_app.command("list", help=_("Show your email requests of a task with status, Message-ID and SMTP errors."))
def mail_list(task_nr: str, as_json: bool = JsonOpt):
    from cli.client import emails_report

    try:
        data = _client().get(f"/tasks/{task_nr.strip().upper()}/emails")
    except ApiError as e:
        _fail(e)
    if as_json:
        return _dump(data)
    typer.echo(emails_report(data))


@mail_app.command("recipients", help=_("Show the allowed email recipients of a project."))
def mail_recipients(project: str, as_json: bool = JsonOpt):
    from cli.client import recipients_report

    try:
        data = _client().get(f"/projects/{project.strip().upper()}/mail-recipients")
    except ApiError as e:
        _fail(e)
    if as_json:
        return _dump(data)
    typer.echo(recipients_report(data))
