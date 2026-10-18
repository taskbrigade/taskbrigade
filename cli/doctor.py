"""`tbr doctor`: check an agent machine – configuration, connection, token/role, version vs. server, Git access,
tmux/agent command, timer (or the desktop app MCP entry, the Hermes/OpenClaw skill and schedule) and own open or
hanging tasks. Every check reports ok/warning/error with a concrete hint;
`tbr doctor` exits with 1 if any check fails. Never prints the token.

Old names (cli/migrate.py): `.taskmeister.toml` is renamed right away (backup `.taskmeister.toml.bak`); everything
else is reported, and `--fix` switches it (runner timer, MCP entries, Hermes/OpenClaw files and job …)."""
import os
import sys
import tomllib
from datetime import UTC, datetime
from pathlib import Path

import httpx

from cli.client import ApiError, Client, ConfigError, agent_config, find_config, is_older, load_config, local_version
from cli.client import LEGACY_LOCAL_CONFIG, server_headers
from cli.i18n import _
from cli import setup as setup_mod
from cli.setup import Check, _run, launchd_label, linger_check, prerequisite_checks, unit_id
from cli.setup_apps import doctor_checks
from cli.migrate import LEGACY_LAUNCHD_PREFIX, LEGACY_UNIT_PREFIX, leftovers, migrate, rename_config

KIND_SETUP = {"claude_code": "claude-code", "codex": "codex", "opencode": "opencode", "hermes": "hermes",
              "openclaw": "openclaw"}


def _setup_cmd(me: dict | None) -> str:
    return f"tbr setup {KIND_SETUP.get((me or {}).get('agent_kind') or '', '<claude-code|codex|opencode|claude-desktop|codex-desktop>')}"


def config_check(work_root: Path | None) -> tuple[list[Check], Path, object]:
    path = agent_config(work_root) if work_root else find_config()
    renamed = rename_config(path.parent) if path.name == LEGACY_LOCAL_CONFIG else []
    if renamed:
        path = agent_config(path.parent)
    check, path, cfg = _load(path)
    return [check, *renamed], path, cfg


def _load(path: Path) -> tuple[Check, Path, object]:
    try:
        cfg = load_config(path)
    except ConfigError as e:
        return Check("error", _("Configuration"), str(e),
                     _("join with a code from the web UI: tbr join <server> <code> --dir <work directory>")), path, None
    except (OSError, tomllib.TOMLDecodeError) as e:
        return Check("error", _("Configuration"), _("%(path)s cannot be read: %(error)s", path=path, error=e)), path, None
    source = path if path.is_file() else _("environment TBR_URL/TBR_TOKEN")
    return Check("ok", _("Configuration"), str(source)), path, cfg


def connection_check(url: str) -> tuple[Check, str | None]:
    try:
        r = httpx.get(f"{url}/healthz", timeout=10)
        body = r.json()
    except httpx.HTTPError as e:
        return Check("error", _("Connection"), _("%(url)s is not reachable: %(error)s", url=url, error=e),
                     _("check the url in the configuration and the network (e.g. tailscale status)")), None
    except ValueError:
        return Check("error", _("Connection"), _("%(url)s does not answer like TaskBrigade (HTTP %(status)s)",
                                                 url=url, status=r.status_code),
                     _("check the url in the configuration")), None
    version = server_headers(r)[0] or body.get("version")
    if r.status_code != 200 or body.get("db") != "ok":
        return Check("warn", _("Connection"), _("%(url)s reachable, but the server reports db=%(db)s",
                                                url=url, db=body.get("db")),
                     _("the TaskBrigade server has a problem – tell an admin")), version
    return Check("ok", _("Connection"), _("%(url)s (server %(version)s)", url=url, version=version or "?")), version


def version_check(server: str | None, local: str | None = None) -> Check:
    local = local if local is not None else local_version()
    if not local:
        return Check("warn", _("tbr version"), _("unknown (not installed as a package)"),
                     _("install tbr with: curl -fsSL <server>/install.sh | sh"))
    if is_older(local, server):
        return Check("warn", _("tbr version"), _("tbr %(local)s is older than the server (%(server)s)", local=local,
                                                server=server), _("update with: tbr self-update"))
    return Check("ok", _("tbr version"), f"tbr {local}" + (f" / server {server}" if server else ""))


def token_check(client: Client) -> tuple[Check, dict | None]:
    try:
        me = client.get("/agents/me")
    except ApiError as e:
        if e.status == 401:
            return Check("error", _("Token"), _("rejected by the server (%(error)s)", error=e),
                         _("an admin creates a new join code (Admin → Benutzer & Agenten → <agent>); then "
                           "tbr join <server> <code> --dir <work directory> --force")), None
        return Check("error", _("Token"), str(e)), None
    detail = _("%(name)s (%(kind)s, %(runtime)s, role %(role)s)", name=me.get("name"), kind=me.get("agent_kind") or "-",
               runtime=me.get("runtime"), role=me.get("role"))
    if not me.get("is_active", True):
        return Check("error", _("Token"), detail + " – " + _("inactive"), _("an admin reactivates the agent")), me
    return Check("ok", _("Token"), detail), me


def git_check(client: Client, runner: bool) -> Check:
    from cli.join import check_git, credential_helper_set

    status, detail = check_git(client)
    if status == "ok":
        return Check("ok", _("Git access"), detail or "")
    if status == "missing":
        return Check("error" if runner else "warn", _("Git access"), _("no Gitea credentials for this agent"),
                     _("an admin links the Gitea user under Admin → Benutzer & Agenten → <agent> → Gitea"))
    hint = _("an admin checks the Gitea token under Admin → Benutzer & Agenten → <agent> → Gitea")
    if not credential_helper_set():
        hint += "\n" + _('outside the runner: git config --global credential.helper "!tbr git-credential"')
    return Check("error", _("Git access"), detail or _("failed"), hint)


def sftp_check(client: Client) -> Check | None:
    """Only when the server has SFTP storage enabled: log in with the agent's own login (no write test)."""
    from cli import sftp

    try:
        data = sftp.info(client)
    except ApiError as e:
        if e.code == "sftp_not_configured":
            return Check("warn", _("SFTP"), _("enabled on the server, but no SFTP login for this agent"),
                         _("an admin adds it under Admin → Benutzer & Agenten → <agent> → SFTP storage"))
        return None  # Disabled, or a server without SFTP storage.
    try:
        detail = sftp.check(sftp.target(client), write=False)
    except ApiError as e:
        hint = (_("fetch credentials over HTTPS (tailscale serve) or from a trusted network")
                if e.code == "insecure_channel" else None)
        return Check("error", _("SFTP"), str(e), hint)
    except sftp.SftpError as e:
        return Check("error", _("SFTP"), str(e),
                     _("check that %(host)s:%(port)s is reachable from this machine; an admin uses “Test access” "
                       "under Admin → Benutzer & Agenten", host=data.get("host"), port=data.get("port")))
    return Check("ok", _("SFTP"), f"{data.get('host')}:{data.get('port')} – {detail}")


def email_check(client: Client) -> Check | None:
    """The server's outgoing email channel: SMTP reachability and login probed by the server (no credentials
    travel to this machine) plus the delivery status from the log. Only shown when the channel is enabled."""
    try:
        d = client.get("/agents/me/email-status")
    except ApiError as e:
        if e.code == "email_disabled":
            return None
        return Check("error", _("Email SMTP"), str(e))
    probe_msg = _("SMTP %(host)s reachable (login ok)", host=d.get("host"))
    last = d.get("last_delivery") or {}
    fails = d.get("failures_in_a_row") or 0
    failed = last.get("last_status") == "failed" and last.get("last_error")
    parts = []
    if last.get("sent_at"):
        parts.append(_("last successful delivery %(at)s", at=last["sent_at"].replace("T", " ")[:16] + " UTC"))
    if failed:
        parts.append(_("last delivery failed: %(error)s", error=last["last_error"]))
    elif fails:
        parts.append(_("%(num)s deliveries failed in a row (retrying)", num=fails))
    if not parts:
        parts.append(_("no delivery yet"))
    status = "warn" if fails else "ok"
    return Check(status, _("Email SMTP"), probe_msg + " – " + "; ".join(parts),
                 _("email is enabled but deliveries keep failing; check the delivery log under Admin → Settings "
                   "and the sender settings (rate limit, spam) or switch the channel off") if fails else "")


def workflow_tick_check(client: Client) -> Check | None:
    """The server's workflow scheduler: warn when it is enabled but the last tick is older than five minutes."""
    try:
        d = client.get("/agents/me/workflow-tick-status")
    except ApiError as e:
        return Check("error", _("Workflow tick"), str(e))
    if not d.get("enabled"):
        return None
    last = d.get("last")
    if d.get("stale"):
        return Check("warn", _("Workflow tick"),
                     _("no tick in the last five minutes (last: %(last)s UTC)", last=last or "–"),
                     _("check the scheduler trigger on the server (TBR_WORKFLOW_TICK=timer or app)"))
    return Check("ok", _("Workflow tick"), _("last tick %(last)s UTC", last=last))


def runner_checks(path: Path, me: dict | None) -> tuple[list[Check], dict | None]:
    from cli.runner import RunnerError, git_by_runner

    try:
        cfg = (tomllib.loads(path.read_text()).get("runner") if path.is_file() else None) or None
    except (OSError, tomllib.TOMLDecodeError):
        cfg = None
    if not cfg or not cfg.get("command"):
        return [Check("warn", _("Runner"), _("no [runner] section – tbr run is not set up"),
                      _("for unattended work: %(cmd)s (in the work directory)", cmd=_setup_cmd(me)))], None
    try:
        git_by_runner(cfg)
    except RunnerError as e:
        return [Check("error", _("Runner"), str(e))], cfg
    checks = [Check("ok", _("Runner"), _("[runner] in %(path)s, mode %(mode)s", path=path,
                                         mode=cfg.get("mode", "tmux")))]
    return checks + prerequisite_checks(cfg), cfg


def timer_check(name: str, work_root: Path, me: dict | None) -> list[Check]:
    hint = _("install it with %(cmd)s, or run tbr run --work-root %(dir)s yourself", cmd=_setup_cmd(me), dir=work_root)
    if sys.platform == "darwin":
        for label in (launchd_label(name), f"{LEGACY_LAUNCHD_PREFIX}{unit_id(name)}"):  # current, before 0.8
            if _run(["launchctl", "print", f"gui/{os.getuid()}/{label}"]).returncode == 0:
                return [Check("ok", _("Timer"), _("launchd job %(label)s loaded", label=label))]
        return [Check("warn", _("Timer"), _("launchd job %(label)s not loaded", label=launchd_label(name)), hint)]
    if not sys.platform.startswith("linux"):
        return [Check("warn", _("Timer"), _("no timer support on %(platform)s", platform=sys.platform))]
    # tbr setup, manual setup with the template; the same under the names before 0.8 (reported by leftovers()).
    units = [f"{prefix}{sep}{unit_id(name)}.timer" for prefix in ("tbr-runner", LEGACY_UNIT_PREFIX) for sep in "-@"]
    for unit in units:
        r = _run(["systemctl", "--user", "is-active", unit])
        if r.returncode == 0 and r.stdout.strip() == "active":
            checks = [Check("ok", _("Timer"), _("%(unit)s active", unit=unit))]
            linger = linger_check()
            return checks + ([linger] if linger else [])
        if r.returncode == 127:
            return [Check("warn", _("Timer"), _("systemctl not available"), hint)]
    return [Check("warn", _("Timer"), _("%(unit)s not active", unit=units[0]), hint)]


def _expired(value) -> bool:
    if not value:
        return False
    try:
        at = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return False
    return (at if at.tzinfo else at.replace(tzinfo=UTC)) < datetime.now(UTC)


def task_checks(client: Client, work_root: Path, cfg: dict | None) -> list[Check]:
    from cli.runner import session_running

    try:
        items = client.get("/agents/me/tasks?status=defined,changes_requested,in_progress,questions").get("items") or []
    except ApiError as e:
        return [Check("error", _("Tasks"), str(e))]
    checks, counts = [], {}
    tmux_session = (cfg.get("tmux_session") or "taskbrigade") if cfg and cfg.get("mode", "tmux") == "tmux" else None
    for t in items:
        counts[t["status"]] = counts.get(t["status"], 0) + 1
        if t["status"] != "in_progress":
            continue
        nr = t["task_nr"]
        if _expired(t.get("lease_expires_at")):
            checks.append(Check("warn", nr, _("in progress, but the lease expired at %(at)s UTC", at=t["lease_expires_at"]),
                                _("the session is probably gone; the task falls back to defined shortly")))
        elif t.get("waiting") and cfg and not session_running(work_root, nr, tmux_session):
            checks.append(Check("ok", nr, _("waits for a background run until %(until)s UTC: %(reason)s",
                                            until=t["waiting"].get("until"), reason=t["waiting"].get("reason")),
                                _("the runner keeps the lease and resumes it when the waiting ends")))
        elif cfg and not session_running(work_root, nr, tmux_session):
            checks.append(Check("warn", nr, _("in progress without a running session on this machine"),
                                _("the next tbr run resumes it; check logs/runner.log if it keeps hanging")))
    summary = ", ".join(f"{status} {n}" for status, n in sorted(counts.items())) or _("none open")
    return [Check("ok", _("Tasks"), summary), *checks]


# Login status without cost (no model call); the arguments follow the agent program.
AUTH_STATUS = {"claude": ["auth", "status"], "codex": ["login", "status"], "opencode": ["auth", "list"]}


def agent_login_checks(cfg: dict, work_root: Path) -> list[Check]:
    """Is the agent CLI logged in? Removes the runner's block (start check) once the agent is ready again."""
    from cli.runner import BLOCKERS, BLOCK_FILE, LOGIN_COMMANDS, agent_binary, clear_block, read_block

    binary = agent_binary(cfg)
    name = Path(binary).name
    login_cmd = LOGIN_COMMANDS.get(name, name)
    args = AUTH_STATUS.get(name)
    status = None
    if args:
        r = setup_mod._run([binary, *args])  # module attribute, so tests patching cli.setup._run cover it
        out = (r.stdout + r.stderr).lower()
        if r.returncode == 0 and not any(n in out for n in BLOCKERS["login"]["*"]):
            status = Check("ok", _("Agent login"), _("%(cmd)s: logged in", cmd=" ".join([name, *args])))
        elif r.returncode != 127 and any(n in out for n in BLOCKERS["login"]["*"] + ["logged out", "no credentials"]):
            status = Check("error", _("Agent login"), _("%(name)s is not logged in", name=name),
                           _("log in on this machine: %(cmd)s", cmd=login_cmd))
    if status is None:
        status = Check("warn", _("Agent login"), _("cannot be checked automatically"),
                       _("start %(name)s in a terminal and check that it does not ask for a login (%(cmd)s)",
                         name=name, cmd=login_cmd))
    checks = [status]
    block = read_block(work_root)
    if block:
        if status.status == "ok":
            clear_block(work_root)
            checks.append(Check("ok", _("Runner block"), _("removed – the agent is ready again (was: %(reason)s)",
                                                           reason=block.get("reason") or "?")))
        else:
            checks.append(Check("error", _("Runner block"), _("tbr run claims no tasks: %(reason)s",
                                                              reason=block.get("reason") or "?"),
                                _("fix the cause, then run tbr doctor again (or delete %(path)s)",
                                  path=work_root / BLOCK_FILE)))
    return checks


def window_checks(client: Client, cfg: dict, work_root: Path) -> list[Check]:
    """Open tmux windows of the runner session: finished ones and agents left running after their task ended."""
    from cli.runner import list_windows, own_window, stale_windows

    if cfg.get("mode", "tmux") != "tmux":
        return []
    session = cfg.get("tmux_session") or "taskbrigade"
    try:
        listed = [w for w in list_windows(session) if w["name"] != "runner"]
        # A session shared with other agents: only this agent's windows are judged, the others are only counted.
        windows = [w for w in listed if own_window(w, work_root)]
        stale = stale_windows(client, session, work_root) if windows else []
    except (ApiError, OSError) as e:
        return [Check("warn", _("tmux windows"), str(e))]
    dead = sum(w["dead"] for w in windows)
    detail = _("%(total)s open in session %(session)s, %(dead)s finished, %(stale)s with an agent still running "
               "although the task is no longer in progress", total=len(windows), session=session, dead=dead,
               stale=len(stale))
    if len(listed) > len(windows):
        detail += _(" (plus %(num)s of other agents)", num=len(listed) - len(windows))
    if stale:
        return [Check("warn", _("tmux windows"), detail + f" ({', '.join(stale)})",
                      _("close them with: tbr cleanup --work-root %(dir)s --stale", dir=work_root))]
    hint = _("finished windows close automatically after close_finished_windows_after_hours; now: tbr cleanup "
             "--work-root %(dir)s", dir=work_root) if dead else ""
    return [Check("ok", _("tmux windows"), detail, hint)]


def doctor(work_root: Path | None = None, fix: bool = False) -> list[Check]:
    checks, path, cfg = config_check(work_root)
    if cfg is None:
        return checks
    root = (work_root or path.parent).resolve()
    conn, server = connection_check(cfg.url)
    checks.append(conn)
    if conn.status == "error":
        return checks
    checks.append(version_check(server))
    client = Client(cfg)
    token, me = token_check(client)
    checks.append(token)
    if me is None:
        return checks
    name = me.get("name") or root.name
    if fix:
        from cli.prepare import tm_command

        checks += migrate(root, name, tm_command(), kind=me.get("agent_kind"))
    checks += leftovers(root, name, _setup_cmd(me), me.get("agent_kind"))
    runner, runner_cfg = runner_checks(path, me)
    apps = None if runner_cfg is not None else doctor_checks(me.get("agent_kind"), root, name)
    checks.append(git_check(client, runner_cfg is not None or apps is not None))
    sftp = sftp_check(client)
    if sftp is not None:
        checks.append(sftp)
    email = email_check(client)
    if email is not None:
        checks.append(email)
    workflow_tick = workflow_tick_check(client)
    if workflow_tick is not None:
        checks.append(workflow_tick)
    if apps is not None:
        checks += apps  # Desktop app or Hermes/OpenClaw: no [runner] and no timer expected.
    else:
        checks += runner
    if runner_cfg is not None:
        checks += agent_login_checks(runner_cfg, root)
        checks += timer_check(name, root, me)
        checks += window_checks(client, runner_cfg, root)
    checks += task_checks(client, root, runner_cfg)
    return checks
