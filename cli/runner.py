"""`tbr run` / `tbr session`: runner for CLI agents without their own scheduler (Claude Code, Codex, OpenCode).

`tbr run` (via systemd timer): check tmux/agent command → lock → resume an own in-progress task without a running
session, otherwise `tbr prepare` → no task: done, no LLM. Otherwise it starts the agent – visibly in its own tmux
window (default) or in the foreground – wrapped by `tbr session`. A start failure gives the task back.

`tbr session`: holds `.locks/session-<NR>.lock` (flock) while it runs, keeps the lease alive with heartbeats while the agent runs and finishes afterwards
(spec 12.2 step 7): the agent did submit/ask/abort itself → done. Otherwise: new commits →
push + submit (last commit message as summary), no commits → release with the exit code.

Review jobs (reviewer agents): `tbr prepare` checks the task branch out read-only in `<NR>-review` with REVIEW.md; the
agent runs in the tmux window `<NR>-review` (`tbr session --review`, lock `session-<NR>-review.lock`) and decides with
`tbr review`. The session keeps the review lease alive and gives the job back undecided when the agent ends without a
decision.

Configuration in the [runner] section of `.taskbrigade.toml` in the agent's work directory:
    language = "en"               # top level, optional: en | de – language of prompts, TASK.md and messages
    [runner]
    command = ["claude", "--permission-mode", "auto", "{prompt}"]   # {prompt} {task_nr} {agent} {worktree} {repo} {model}
    model_flag = "--model"        # optional; switch for the task's model (default per program: MODEL_FLAGS)
    mode = "tmux"                 # tmux | foreground
    tmux_session = "taskbrigade"
    claude_allow = ["Bash(git:*)", "Bash(tbr:*)"]   # optional; passed as --allowedTools
    max_session_hours = 8
    git = "agent"                 # agent | runner (default for Codex: runner – its sandbox locks .git)
    stop_after_finish_seconds = 120          # stop the agent this long after the task left in_progress; 0 = never
    close_finished_windows_after_hours = 24  # tbr run closes finished tmux windows older than this; 0 = never
    start_check_seconds = 60      # check the agent window for blockers (login, limit, unsent prompt); 0 = off
    stall_minutes = 30            # one thread comment if the agent window does not change this long; 0 = off
    park_idle_seconds = 120       # end an idle session whose task waits for a background run (parked); 0 = never

Model choice: the task's model (concrete model > tier mapping > the agent's default model, resolved by the server
as `model_effective`) is passed with the program's switch from MODEL_FLAGS, or `[runner] model_flag`; a `{model}`
placeholder in the command takes the model instead (without a model, the argument – and a switch right before it –
is left out). Without a model nothing is added and the agent CLI uses its own default.

git = "runner": the agent only edits files and creates `.tm-submit.md` at the end (commit message +
summary); `tbr session` then commits, pushes and submits outside the sandbox.
"""
import fcntl
import json
import os
import re
import shlex
import shutil
import signal
import socket
import subprocess
import sys
import threading
import time
import tomllib
from datetime import UTC, datetime, timedelta
from pathlib import Path

import typer

from cli.client import ApiError, Client, agent_config, split_open_points
from cli.i18n import N_, _
from cli.prepare import COMMAND, LEGACY_COMMAND, REVIEW_SUFFIX, SUBMIT_FILE, PrepareError, _git, prepare, tm_command
from cli.prepare import release_review, review_worktree
from cli.prepare import unresolved_conflicts

# Translated when used (`_()`), so the agent's configured language applies
DEFAULT_PROMPT = N_("You are the TaskBrigade agent {agent}. The current directory contains TASK.md with task "
                    "{task_nr}, which has already been claimed for you. Read TASK.md and work on the task. "
                    "Finish with exactly one closing step: tbr submit, tbr ask or tbr abort (the commands are in "
                    "TASK.md). Run git and tbr commands one at a time – without && chains, heredocs or pipes, commit messages "
                    "with git commit -m – otherwise your permissions do not apply.")
RUNNER_GIT_PROMPT = N_("You are the TaskBrigade agent {agent}. The current directory contains TASK.md with task "
                       "{task_nr}, which has already been claimed for you. Read TASK.md and work on the task. "
                       "No git commands – the runner commits and pushes. Done: create the file .tm-submit.md "
                       "(first line commit message, empty line, summary) and end the run. Otherwise tbr ask "
                       "or tbr abort (the commands are in TASK.md), each on its own without && chains or pipes.")
REVIEW_PROMPT = N_("You are the TaskBrigade agent {agent} and review task {task_nr} as its reviewer. The current "
                   "directory is a read-only checkout of the task branch and contains REVIEW.md with the task, the "
                   "result and the commands. Read REVIEW.md, check the changes against the description and the "
                   "acceptance criteria, and finish with exactly one decision: tbr review {task_nr} approve, reject "
                   "or conflict (the commands are in REVIEW.md). Do not edit, commit or push. Run tbr and git "
                   "commands one at a time – without && chains, heredocs or pipes.")
# Bash(tm:*) stays during the transition: older TASK.md texts and agent habits still use the old name.
DEFAULT_CLAUDE_ALLOW = ["Read", "Edit", "Write", "Glob", "Grep", "Bash(git:*)", "Bash(tbr:*)", "Bash(tm:*)",
                        "Bash(ls:*)", "Bash(cat:*)", "Bash(mkdir:*)"]
CLAUDE_DENY = ["Bash(git push --force:*)", "Bash(git push -f:*)"]


# Switch that selects the model, per agent program (`[runner] model_flag` overrides it). Extend here.
MODEL_FLAGS = {"claude": "--model", "codex": "-m", "opencode": "--model", "gemini": "-m"}
MODEL_PLACEHOLDER = "{model}"


class RunnerError(RuntimeError):
    pass


def runner_config(work_root: Path) -> dict:
    path = agent_config(work_root)
    if not path.is_file():
        raise RunnerError(_("%(path)s is missing (url, token and [runner] command)", path=path))
    cfg = tomllib.loads(path.read_text()).get("runner") or {}
    if not cfg.get("command"):
        raise RunnerError(_("[runner] command is missing in %(path)s", path=path))
    return cfg


def _parts(cfg: dict) -> list[str]:
    command = cfg["command"]
    return shlex.split(command) if isinstance(command, str) else list(command)


def git_by_runner(cfg: dict) -> bool:
    """Codex's sandbox blocks Git metadata (.git, including worktree data in the main clone), so
    the agent cannot commit there. The runner handles Git after the agent's run."""
    mode = cfg.get("git")
    if mode in ("agent", "runner"):
        return mode == "runner"
    if mode is not None:
        raise RunnerError(_('[runner] git = "%(mode)s" – allowed are "agent" and "runner"', mode=mode))
    return Path(_parts(cfg)[0]).name == "codex"


def task_model(task: dict) -> str | None:
    """The model the server resolved for the task and its agent (None: the agent CLI's own default). Older
    servers send no `model_effective`."""
    return (task.get("model_effective") or {}).get("model") or None


def model_flag(cfg: dict) -> str | None:
    """Switch for the model: `[runner] model_flag` (empty = none), otherwise MODEL_FLAGS by agent program."""
    if "model_flag" in cfg:
        return str(cfg["model_flag"] or "").strip() or None
    return MODEL_FLAGS.get(Path(agent_binary(cfg)).name)


def with_model(parts: list[str], cfg: dict, model: str | None) -> tuple[list[str], str | None, str]:
    """Add the model to the command: `{model}` placeholder, else the switch right after the agent program.
    Returns the command, the applied model (None if none was passed) and a short note for the task history."""
    if any(MODEL_PLACEHOLDER in p for p in parts):
        if model:
            return [p.replace(MODEL_PLACEHOLDER, model) for p in parts], model, MODEL_PLACEHOLDER
        out: list[str] = []
        for p in parts:
            if MODEL_PLACEHOLDER in p:
                if p == MODEL_PLACEHOLDER and out and out[-1].startswith("-"):
                    out.pop()  # The switch belongs to the missing value.
                continue
            out.append(p)
        return out, None, ""
    if not model:
        return parts, None, ""
    flag = model_flag(cfg)
    if not flag:
        return parts, None, _("%(model)s not applied: no model switch known for %(program)s – set [runner] "
                              "model_flag", model=model, program=Path(agent_binary(cfg)).name)
    args = [flag + model] if flag.endswith("=") else [flag, model]
    pos = parts.index(agent_binary(cfg)) + 1 if agent_binary(cfg) in parts else 1
    return [*parts[:pos], *args, *parts[pos:]], model, " ".join(args)


def build_command(cfg: dict, task: dict, agent: str, worktree: Path | None = None,
                  model: str | None = None, review: bool = False) -> list[str]:
    """Placeholders: {prompt} {task_nr} {agent} {worktree} {repo} {model}; {repo} is the main clone with .git.
    `model` is added with the program's switch (`with_model`). `review`: prompt for a review job
    (`[runner] review_prompt` overrides it)."""
    parts = with_model(_parts(cfg), cfg, model)[0]
    if review:
        prompt = (cfg.get("review_prompt") or _(REVIEW_PROMPT)).format(agent=agent, task_nr=task["task_nr"])
    else:
        default = RUNNER_GIT_PROMPT if git_by_runner(cfg) else DEFAULT_PROMPT
        prompt = (cfg.get("prompt") or _(default)).format(agent=agent, task_nr=task["task_nr"])
    values = {"{prompt}": prompt, "{task_nr}": task["task_nr"], "{agent}": agent,
              "{worktree}": str(worktree or ""), "{repo}": str(worktree.parent.parent if worktree else "")}
    out = []
    for p in parts:
        for key, value in values.items():
            p = p.replace(key, value)
        out.append(p)
    return out


def claude_cli_rules(allow: list[str]) -> list[str]:
    """TaskBrigade CLI rules: an own `claude_allow` written for `tm` also gets `Bash(tbr:*)` (TASK.md names tbr),
    and both short forms get their absolute path – TASK.md may use it, and `Bash(tbr:*)` covers only the short form."""
    short = [f"Bash({n}:*)" for n in (COMMAND, LEGACY_COMMAND)]
    if not any(rule in allow for rule in short):
        return allow
    extra = [] if short[0] in allow else [short[0]]
    for name in (COMMAND, LEGACY_COMMAND):
        found = tm_command() if name == COMMAND else shutil.which(name)
        if found and Path(found).name == name:
            extra.append(f"Bash({found}:*)")
    return [*allow, *(rule for rule in dict.fromkeys(extra) if rule not in allow)]


def with_claude_permissions(command: list[str], allow: list[str]) -> list[str]:
    """Pass permissions as startup arguments instead of .claude/settings.local.json: project-local
    permissions trigger another Claude Code trust prompt even with a trusted parent, stalling unattended
    startup. Put arguments immediately after `claude` so the prompt remains positional."""
    allow = claude_cli_rules(allow)
    # Default to auto mode so Claude Code approves safe actions without prompting for each command.
    # acceptEdits covers only file changes.
    mode = [] if any(a == "--permission-mode" or a.startswith("--permission-mode=") for a in command) \
        else ["--permission-mode", "auto"]
    # Use `--opt=value`: these options accept multiple values, so without `=` a prompt could be read as a tool.
    return [command[0], f"--allowedTools={','.join(allow)}", f"--disallowedTools={','.join(CLAUDE_DENY)}", *mode,
            *command[1:]]


def _codex_config() -> Path:
    return Path(os.environ.get("CODEX_HOME") or Path.home() / ".codex") / "config.toml"


def codex_trust(worktree: Path, trusted: bool) -> None:
    """Codex prompts for every untrusted Git root; each task worktree is separate. It does not inherit
    parent trust and a -c override does not apply, so add a worktree entry before startup and remove it
    after the session."""
    path = _codex_config()
    text = path.read_text() if path.exists() else ""
    # Marker "# tm-runner" before TaskBrigade 0.8: blocks written then are removed as well.
    block = re.compile(r'\n*# (?:tbr|tm)-runner\n\[projects\."' + re.escape(str(worktree)) + r'"\]\ntrust_level = "trusted"\n')
    text = block.sub("\n", text)
    if trusted:
        text = text.rstrip("\n") + f'\n\n# tbr-runner\n[projects."{worktree}"]\ntrust_level = "trusted"\n'
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text.lstrip("\n") if text.strip() else "")


def _log_path(work_root: Path, task_nr: str) -> Path:
    logs = work_root / "logs"
    logs.mkdir(exist_ok=True)
    return logs / f"{task_nr}-{datetime.now(UTC).strftime('%Y%m%d-%H%M%S')}.log"


WAITING = ("draft", "defined", "changes_requested")


def skipped_tasks(client: Client) -> list[str]:
    """List assigned but unclaimed tasks with reasons, so an empty run is explained.
    This is diagnostic only; failures must never disrupt the runner."""
    try:
        me = client.get("/agents/me")
        items = client.get("/agents/me/tasks?status=" + ",".join((*WAITING, "in_progress", "for_review"))
                           ).get("items") or []
        running = [t["task_nr"] for t in items if t.get("status") == "in_progress"]
        in_review = {t["task_nr"]: t.get("project") for t in items if t.get("status") == "for_review"}
        out = []
        for t in items:
            if t.get("status") not in WAITING:
                continue
            reason = _skip_reason(client, t, running, me.get("max_parallel_tasks") or 2, in_review)
            out.append(_("%(nr)s skipped: %(reason)s", nr=t["task_nr"], reason=reason))
        return out
    except Exception:  # noqa: BLE001
        return []


def _skip_reason(client: Client, t: dict, running: list[str], max_parallel: int,
                 in_review: dict[str, str] | None = None) -> str:
    if t["status"] == "draft":
        return _("draft – only claimed after it is published")
    if len(running) >= max_parallel:
        return _("max_parallel_tasks=%(max)s reached (in progress: %(running)s)", max=max_parallel,
                 running=", ".join(running))
    full = client.get(f"/tasks/{t['task_nr']}")
    project = full.get("project_info") or {}
    if project.get("status", "active") != "active":
        return _("project %(key)s is not active ('%(status)s')", key=project.get("key"), status=project.get("status"))
    open_deps = [f"{d['task_nr']} ({d['status']})" for d in full.get("dependencies") or [] if d["status"] != "done"]
    if open_deps:
        return _("open dependencies: %(deps)s", deps=", ".join(open_deps))
    waiting = [nr for nr, key in (in_review or {}).items() if key == project.get("key") and nr != t["task_nr"]]
    if project.get("serialize_reviews") and waiting:
        return _("waits until %(waiting)s has been reviewed (project %(key)s: one review at a time)",
                 waiting=", ".join(waiting), key=project.get("key"))
    return _("reason unknown (possibly locked by a parallel run right now)")


def log_runner(work_root: Path, lines: list[str]) -> None:
    """Append lines to `logs/runner.log` as well as the journal."""
    logs = work_root / "logs"
    logs.mkdir(exist_ok=True)
    now = datetime.now(UTC).isoformat(timespec="seconds")
    with (logs / "runner.log").open("a") as f:
        f.write("".join(f"{now} {line}\n" for line in lines))


# Install hints for agent commands; tmux gets brew (macOS) or apt (Linux).
AGENT_INSTALL = {"claude": "npm install -g @anthropic-ai/claude-code", "codex": "npm install -g @openai/codex",
                 "opencode": "npm install -g opencode-ai"}


def _install_hint(tool: str) -> str:
    if tool == "tmux":
        return "brew install tmux" if sys.platform == "darwin" else "sudo apt install tmux"
    return AGENT_INSTALL.get(tool, "")


def agent_binary(cfg: dict) -> str:
    """The agent program itself: `env VAR=value opencode …` → `opencode`."""
    parts = _parts(cfg)
    if Path(parts[0]).name != "env":
        return parts[0]
    rest = [p for p in parts[1:] if "=" not in p and not p.startswith("-")]
    return rest[0] if rest else parts[0]


def missing_prerequisites(cfg: dict) -> list[str]:
    """tmux (mode = "tmux"), the command and the agent program it starts that are not found in PATH."""
    tools = (["tmux"] if cfg.get("mode", "tmux") == "tmux" else []) + [_parts(cfg)[0]]
    if agent_binary(cfg) not in tools:
        tools.append(agent_binary(cfg))
    return [t for t in tools if not shutil.which(t)]


def missing_hint(tool: str) -> str:
    hint = _install_hint(Path(tool).name)
    return (_("%(tool)s not found in PATH – install it: %(hint)s", tool=tool, hint=hint) if hint
            else _("%(tool)s not found in PATH – install it", tool=tool))


def check_prerequisites(cfg: dict) -> None:
    """Before claiming: tmux (mode = "tmux") and the agent command must be found, otherwise the task would
    be claimed and then hang in progress without anyone working on it."""
    missing = missing_prerequisites(cfg)
    if not missing:
        return
    lines = [_("Prerequisites missing – no task claimed:")]
    lines += ["  " + missing_hint(tool) for tool in missing]
    lines.append("  " + _("PATH: %(path)s – the systemd unit or launchd job has its own PATH; alternatively put the "
                          "absolute path into [runner] command", path=os.environ.get("PATH", "")))
    raise RunnerError("\n".join(lines))


def _session_lock_path(work_root: Path, task_nr: str) -> Path:
    locks = work_root / ".locks"
    locks.mkdir(exist_ok=True)
    return locks / f"session-{task_nr}.lock"


def acquire_session_lock(work_root: Path, task_nr: str):
    """Exclusive lock for a task's `tbr session`; None if another session holds it."""
    f = _session_lock_path(work_root, task_nr).open("a+")
    try:
        fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        f.close()
        return None
    f.seek(0)
    f.truncate()
    f.write(f"pid={os.getpid()} start={datetime.now(UTC).isoformat(timespec='seconds')}\n")
    f.flush()
    return f


def release_session_lock(f) -> None:
    fcntl.flock(f, fcntl.LOCK_UN)
    f.close()


def session_running(work_root: Path, task_nr: str, tmux_session: str | None = None) -> bool:
    """A `tbr session` holds the task lock, or (tmux mode) a live tmux window carries the task's name."""
    f = acquire_session_lock(work_root, task_nr)
    if f is None:
        return True
    release_session_lock(f)
    return bool(tmux_session) and task_nr in _live_windows(tmux_session)


def _setting(cfg: dict, key: str, default: float) -> float:
    """Numeric [runner] setting; an invalid value falls back to the default instead of stopping the runner."""
    try:
        return max(0.0, float(cfg.get(key, default)))
    except (TypeError, ValueError):
        return float(default)


def _live_windows(session: str) -> set[str]:
    r = _tmux("list-windows", "-t", session, "-F", "#{window_name} #{pane_dead}")
    if r.returncode != 0:
        return set()
    return {name for name, _sep, dead in (line.rpartition(" ") for line in r.stdout.splitlines()) if dead == "0"}


def resumable_task(client: Client, work_root: Path, tmux_session: str | None,
                   parked: frozenset[str] = frozenset()) -> str | None:
    """An own in-progress task or held review job without a running session (e.g. the session crashed or the
    machine rebooted). `parked` tasks wait for a background run and are not resumed."""
    items = client.get("/agents/me/tasks?status=in_progress").get("items") or []
    for t in items:
        if (t.get("status", "in_progress") == "in_progress" and t["task_nr"] not in parked
                and not session_running(work_root, t["task_nr"], tmux_session)):
            return t["task_nr"]
    for t in own_review_jobs(client):
        if not session_running(work_root, t["task_nr"] + REVIEW_SUFFIX, tmux_session):
            return t["task_nr"]
    return None


def watch_file_silent(waiting: dict, silent_s: float) -> bool:
    """The watched log of a waiting task is missing or has not changed for `silent_s` (0 = never silent)."""
    path = waiting.get("watch_file")
    if not path or silent_s <= 0:
        return False
    try:
        return time.time() - Path(path).expanduser().stat().st_mtime >= silent_s
    except OSError:
        return True


def keep_parked(client: Client, work_root: Path, tmux_session: str | None, silent_s: float) -> list[str]:
    """Own in-progress tasks without a session that wait for a background run (the agent reported
    `heartbeat --waiting` and ended its session): extend their lease with a plain heartbeat (it leaves the waiting
    state alone) instead of starting a new session every pass. A task is resumed again once the waiting state ends
    (expired, `--done-waiting`) or its watched log stays silent for `silent_s`. Returns the parked task numbers;
    never raises (older servers send no `waiting`)."""
    parked = []
    try:
        items = client.get("/agents/me/tasks?status=in_progress").get("items") or []
    except ApiError:
        return []
    for t in items:
        waiting = t.get("waiting")
        if (t.get("status", "in_progress") != "in_progress" or not waiting
                or session_running(work_root, t["task_nr"], tmux_session) or watch_file_silent(waiting, silent_s)):
            continue
        try:
            client.request("POST", f"/tasks/{t['task_nr']}/heartbeat")
        except ApiError:
            continue
        parked.append(t["task_nr"])
    return parked


def own_review_jobs(client: Client) -> list[dict]:
    """Review jobs this agent holds (claimed, undecided); none for older servers or other roles."""
    try:
        items = client.get("/agents/me/reviews").get("items") or []
    except ApiError:
        return []
    return [t for t in items if t.get("review_claimed")]


def give_back(client: Client, task_nr: str, reason: str) -> str:
    """Release the task with a reason after a failed start; never raises."""
    try:
        client.request("POST", f"/tasks/{task_nr}/release", json={"reason": reason[:2000], "signal": "blocked"})
    except ApiError as e:
        return _("%(nr)s could not be given back (%(error)s)", nr=task_nr, error=e)
    return _("%(nr)s given back", nr=task_nr)


def abort_task(client: Client, task_nr: str, reason: str) -> str:
    """Hand the task back to a human ('aborted', reason in the thread) – for task-specific start failures such as an
    unavailable model, where releasing would only make the runner claim it again; never raises."""
    try:
        client.request("POST", f"/tasks/{task_nr}/abort", json={"reason": reason[:2000]})
    except ApiError as e:
        return _("%(nr)s could not be given back (%(error)s)", nr=task_nr, error=e)
    return _("%(nr)s handed back to a human (aborted)", nr=task_nr)


def run_once(client: Client, work_root: Path) -> str:
    """Run one runner pass and return a short status for the journal."""
    cfg = runner_config(work_root)
    check_prerequisites(cfg)
    # The file persists; a run is active only while a process holds the flock lock.
    # PID and start time of the latest/current pass are informational.
    lock_file = (work_root / ".runner.lock").open("a+")
    try:
        fcntl.flock(lock_file, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        return _("Runner already running – skipped")
    lock_file.seek(0)
    lock_file.truncate()
    lock_file.write(f"pid={os.getpid()} start={datetime.now(UTC).isoformat(timespec='seconds')}\n")
    lock_file.flush()
    try:
        tmux_session = (cfg.get("tmux_session") or "taskbrigade") if cfg.get("mode", "tmux") == "tmux" else None
        if tmux_session:
            cleanup_windows(work_root, tmux_session, _setting(cfg, "close_finished_windows_after_hours", 24))
        block = read_block(work_root)
        if block:
            msg = _("Agent blocked (%(reason)s) – no task claimed; run tbr doctor once the cause is fixed",
                    reason=block.get("reason") or "?")
            log_runner(work_root, [msg])
            return msg
        if tmux_session:
            stop_returned_windows(client, work_root, tmux_session)
        parked = keep_parked(client, work_root, tmux_session, _setting(cfg, "stall_minutes", 30) * 60)
        notes = [_("%(nr)s waits for a background run – stays in progress, lease extended", nr=nr) for nr in parked]
        if notes:
            log_runner(work_root, notes)
        resume = resumable_task(client, work_root, tmux_session, frozenset(parked))
        result = prepare(client, work_root, resume, git_by_runner=git_by_runner(cfg))
        if result is None:
            skipped = skipped_tasks(client)
            if skipped:
                log_runner(work_root, skipped)
            return " – ".join([_("No task"), *(["; ".join(notes + skipped)] if notes or skipped else [])])
        task = result.task
        try:
            msg = _start(client, cfg, task, result.worktree, work_root, tmux_session, review=result.review)
        except Exception as e:  # noqa: BLE001 – any start failure must give the task back, not leave it hanging.
            reason = _("The runner could not start the agent: %(error)s", error=e)
            log_runner(work_root, [f"{task['task_nr']}: {reason}"])
            back = release_review(client, task["task_nr"], reason) if result.review else give_back(
                client, task["task_nr"], reason)
            raise RunnerError(f"{task['task_nr']}: {reason} – {back}") from e
        return (_("%(nr)s resumed (own task in progress without a running session)", nr=task["task_nr"]) + " – "
                + msg) if resume else msg
    finally:
        fcntl.flock(lock_file, fcntl.LOCK_UN)
        lock_file.close()


def report_model(client: Client, cfg: dict, task: dict) -> str | None:
    """Record the model the agent starts with in the task history; returns the applied model. Never raises (older
    servers do not know the endpoint)."""
    applied, note = with_model(_parts(cfg), cfg, task_model(task))[1:]
    source = (task.get("model_effective") or {}).get("source")
    if applied and source:
        note = f"{note} ({source})"
    try:
        client.request("POST", f"/tasks/{task['task_nr']}/model-used", json={"model": applied, "note": note or None})
    except Exception:  # noqa: BLE001 – a history note must never stop the start.
        pass
    return applied


def _start(client: Client, cfg: dict, task: dict, worktree: Path, work_root: Path, tmux_session: str | None,
           review: bool = False) -> str:
    agent = client.get("/agents/me")["name"]
    # A review job has no model-used note (the history belongs to the assignee's work); its model comes from the
    # claim (resolved for the reviewer).
    model = with_model(_parts(cfg), cfg, task_model(task))[1] if review else report_model(client, cfg, task)
    command = build_command(cfg, task, agent, worktree, model, review=review)
    if Path(command[0]).name == "claude":
        command = with_claude_permissions(command, cfg.get("claude_allow") or DEFAULT_CLAUDE_ALLOW)
    if Path(command[0]).name == "codex":
        codex_trust(worktree, True)
    name = task["task_nr"] + (REVIEW_SUFFIX if review else "")
    session_cmd = [tm_command(), "session", task["task_nr"], "--work-root", str(work_root),
                   *(["--review"] if review else []), "--", *command]
    log = _log_path(work_root, name)
    if tmux_session:
        trust = _launch_tmux(tmux_session, name, worktree, session_cmd, log, work_root)
        return (_("%(nr)s started in a tmux window (tmux attach -t %(session)s)", nr=name,
                  session=tmux_session) + (f" – {trust}" if trust else ""))
    with log.open("w") as f:
        rc = subprocess.call(session_cmd, cwd=worktree, stdout=f, stderr=subprocess.STDOUT)
    return _("%(nr)s finished (exit %(rc)s), log %(log)s", nr=name, rc=rc, log=log)


def _tmux(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["tmux", *args], capture_output=True, text=True)


CLAUDE_TRUST_OPTION = "Yes, I trust this folder"
CLAUDE_READY = "Claude Code v"


def auto_trust_claude(target: str, timeout_s: float = 30.0, poll_s: float = 0.5) -> str:
    """Claude Code requests trust for each new Git repo/worktree; parent trust does not apply. The
    runner selects "Yes, I trust this folder" only if the folder brings no permissions of its own
    (the "pre-approves … permissions" variant, e.g. from committed .claude/settings.json). A human
    must decide that variant in the window."""
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        pane = _tmux("capture-pane", "-p", "-t", target).stdout
        if CLAUDE_TRUST_OPTION in pane:
            if "pre-approves" in pane:
                return _("not confirmed: the folder brings its own permissions – please check in the window yourself")
            # The dialog may appear before accepting keys; a lost Down would select "No, exit".
            # Press Enter only when the cursor visibly points to "Yes".
            for _attempt in range(5):
                _tmux("send-keys", "-t", target, "Down")
                time.sleep(poll_s)
                pane = _tmux("capture-pane", "-p", "-t", target).stdout
                if re.search(r"❯\s*(\d+\.\s*)?" + re.escape(CLAUDE_TRUST_OPTION), pane):
                    _tmux("send-keys", "-t", target, "Enter")
                    return _("trust dialog confirmed")
            return _("trust dialog not confirmed (selection not on \"Yes\") – please confirm in the window")
        if CLAUDE_READY in pane:
            return _("no trust dialog")
        time.sleep(poll_s)
    return _("trust dialog not detected – confirm in the window if needed")


def check_started(work_root: Path, task_nr: str, target: str, log: Path, timeout_s: float = 3.0,
                  poll_s: float = 0.2) -> None:
    """Fail if the window's `tbr session` ends right away (e.g. `tbr` missing in the tmux server's PATH);
    a session holding its task lock has started. A slower start is fine: the live window counts as running."""
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        f = acquire_session_lock(work_root, task_nr)
        if f is None:
            return
        release_session_lock(f)
        if _tmux("display", "-p", "-t", target, "#{pane_dead}").stdout.strip() == "1":
            time.sleep(poll_s)  # Let pipe-pane write the last output.
            output = (log.read_text(errors="replace") if log.exists() else "").strip() \
                or _tmux("capture-pane", "-p", "-t", target).stdout.strip()
            raise RunnerError(_("tbr session ended right after the start in the tmux window: %(output)s",
                                output=" ".join(output.split())[-500:] or _("no output")))
        time.sleep(poll_s)


def _launch_tmux(session: str, name: str, worktree: Path, cmd: list[str], log: Path, work_root: Path) -> str:
    if _tmux("has-session", "-t", session).returncode != 0:
        r = _tmux("new-session", "-d", "-s", session, "-n", "runner", "-c", str(worktree.parents[2]))
        if r.returncode != 0:
            raise RunnerError(_("tmux session '%(session)s' could not be created: %(error)s", session=session,
                                 error=r.stderr.strip()))
    # An earlier window of the task – finished (remain-on-exit) or still running, e.g. within
    # stop_after_finish_seconds after a submission that came straight back – is stopped and closed first:
    # never two sessions on one task.
    stop_task_windows(work_root, session, name)
    held = acquire_session_lock(work_root, name)
    if held is None:
        raise RunnerError(_("A session for %(nr)s is already running", nr=name))
    release_session_lock(held)
    # Open the window with a placeholder, set it up, then start the command: an early failure lands in the log.
    # Every later call addresses the window by its id – a name can be ambiguous.
    r = _tmux("new-window", "-d", "-P", "-F", "#{window_id}", "-t", f"{session}:", "-n", name, "-c", str(worktree),
              "cat")
    target = r.stdout.strip()
    if r.returncode != 0 or not target:
        raise RunnerError(_("tmux window could not be opened: %(error)s", error=r.stderr.strip()))
    try:
        _tmux("set-option", "-t", target, "remain-on-exit", "on")  # Keep the final view until the window is closed.
        _tmux("set-option", "-w", "-t", target, OWNER_OPTION, str(work_root.resolve()))
        _tmux("pipe-pane", "-t", target, "-o", f"cat >> {shlex.quote(str(log))}")
        r = _tmux("respawn-pane", "-k", "-t", target, "-c", str(worktree), shlex.join(cmd))
        if r.returncode != 0:
            raise RunnerError(_("tmux window could not be opened: %(error)s", error=r.stderr.strip()))
        check_started(work_root, name, target, log)
    except Exception:
        # The task goes back; a leftover window (placeholder or dead pane) would count as a running session.
        close_window(work_root, {"id": target, "name": name}, log)
        raise
    agent_bin = Path(cmd[cmd.index("--") + 1]).name if "--" in cmd else ""
    return auto_trust_claude(target) if agent_bin == "claude" else ""


# --- tmux window housekeeping ---------------------------------------------------------------------------------------

# Window option with the work root of the agent that opened the window (several agents can share a session).
OWNER_OPTION = "@taskbrigade_work_root"
TASK_WINDOW = re.compile(r"^[A-Z][A-Z0-9]*-\d+$")
REVIEW_WINDOW = re.compile(r"^([A-Z][A-Z0-9]*-\d+)" + re.escape(REVIEW_SUFFIX) + "$")


def _capture(target: str, history: bool = False) -> str | None:
    """Visible content of a pane (with history: everything tmux still holds); None if the pane is gone."""
    r = _tmux("capture-pane", "-p", "-t", target, *(["-S", "-"] if history else []))
    return r.stdout if r.returncode == 0 else None


def _latest_log(work_root: Path, name: str) -> Path | None:
    logs = sorted((work_root / "logs").glob(f"{name}-*.log"), key=lambda p: p.stat().st_mtime)
    return logs[-1] if logs else None


def list_windows(session: str) -> list[dict]:
    """Windows of the tmux session: id, name, dead (bool), pane_dead_time (epoch seconds, 0 if unknown), owner (the
    work root `_launch_tmux` tagged it with, "" for untagged windows) and path (the pane's current directory)."""
    r = _tmux("list-windows", "-t", session, "-F", "#{window_id}\t#{window_name}\t#{pane_dead}\t#{pane_dead_time}"
              f"\t#{{{OWNER_OPTION}}}\t#{{pane_current_path}}")
    if r.returncode != 0:
        return []
    out = []
    for line in r.stdout.splitlines():
        wid, name, dead, dead_time, owner, path = (line.split("\t") + [""] * 6)[:6]
        out.append({"id": wid, "name": name, "dead": dead == "1",
                    "dead_time": int(dead_time) if dead_time.isdigit() else 0, "owner": owner, "path": path})
    return out


def own_window(window: dict, work_root: Path) -> bool:
    """Whether the window belongs to the agent of `work_root` – several agents may share one tmux session. Tagged
    windows by their owner option; untagged ones (started by an older tbr) by the pane's directory, which lies in the
    agent's work root; without either (no such tmux fields) the window counts as own, as before."""
    root = work_root.resolve()
    if window.get("owner"):
        return Path(window["owner"]).resolve() == root
    if window.get("path"):
        path = Path(window["path"]).resolve()
        return path == root or root in path.parents
    return True


def own_windows(session: str, work_root: Path) -> list[dict]:
    return [w for w in list_windows(session) if own_window(w, work_root)]


def _dead_age_s(work_root: Path, window: dict, now: float) -> float | None:
    """Seconds since the pane ended: tmux's pane_dead_time, otherwise (older tmux) the run log's last write."""
    if window["dead_time"]:
        return now - window["dead_time"]
    log = _latest_log(work_root, window["name"])
    return now - log.stat().st_mtime if log else None


def close_window(work_root: Path, window: dict, log: Path | None = None) -> None:
    """Append the window's content to its run log (default: the task's latest), then close it."""
    content = _capture(window["id"], history=True)
    log = log or _latest_log(work_root, window["name"])
    if log is None:
        (work_root / "logs").mkdir(exist_ok=True)
        log = work_root / "logs" / f"{window['name']}-{datetime.now(UTC).strftime('%Y%m%d-%H%M%S')}.log"
    stamp = datetime.now(UTC).isoformat(timespec="seconds")
    with log.open("a") as f:
        f.write(f"\n--- tmux window {window['name']} closed {stamp} ---\n{(content or '').rstrip()}\n")
    _tmux("kill-window", "-t", window["id"])


# Like `_stop_agent`: Ctrl+C, then more forcefully, each with a grace period in seconds.
STOP_SIGNALS = ((signal.SIGINT, 15), (signal.SIGTERM, 10), (signal.SIGKILL, 5))


def _group_alive(pgid: int) -> bool:
    try:
        os.killpg(pgid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def stop_window(work_root: Path, window: dict, poll_s: float = 0.2) -> None:
    """Stop a live window's processes (the old `tbr session` and its agent share the pane's process group) with
    `STOP_SIGNALS` – the session ends without its closing step, so it cannot release a task that was just claimed
    again – then close the window with its content appended to the run log."""
    if not window["dead"]:
        pid = _tmux("display", "-p", "-t", window["id"], "#{pane_pid}").stdout.strip()
        try:
            pgid = os.getpgid(int(pid)) if pid.isdigit() else None
        except ProcessLookupError:
            pgid = None
        if pgid is not None and pgid != os.getpgrp():
            for sig, wait_s in STOP_SIGNALS:
                try:
                    os.killpg(pgid, sig)
                except ProcessLookupError:
                    break
                deadline = time.monotonic() + wait_s
                while _group_alive(pgid) and time.monotonic() < deadline:
                    time.sleep(poll_s)
                if not _group_alive(pgid):
                    break
    close_window(work_root, window)


def stop_task_windows(work_root: Path, session: str, name: str) -> list[str]:
    """Stop and close every window named `name` (dead or alive); returns the closed window ids."""
    closed = []
    for w in list_windows(session):
        if w["name"] == name:
            stop_window(work_root, w)
            closed.append(w["id"])
    if closed:
        try:
            log_runner(work_root, [_("%(nr)s: earlier tmux window stopped and closed before the start (content "
                                     "appended to its run log)", nr=name)])
        except OSError:
            pass
    return closed


def stop_returned_windows(client: Client, work_root: Path, session: str) -> list[str]:
    """Before claiming: live windows of own tasks that came back from review (changes_requested) are stopped
    right away instead of after stop_after_finish_seconds – otherwise the old session would see its task in
    progress again after the claim, keep running and could hand it back when its agent ends. Never raises."""
    try:
        live = {w["name"] for w in list_windows(session) if not w["dead"] and TASK_WINDOW.match(w["name"])}
        if not live:
            return []
        returned = {t["task_nr"] for t in client.get("/agents/me/tasks?status=changes_requested").get("items") or []
                    if t.get("status", "changes_requested") == "changes_requested"}
        names = sorted(live & returned)
        for name in names:
            stop_task_windows(work_root, session, name)
        return names
    except Exception as e:  # noqa: BLE001 – the start check in _launch_tmux is the fallback.
        try:
            log_runner(work_root, [_("Stopping earlier tmux windows failed: %(error)s", error=e)])
        except OSError:
            pass
        return []


def cleanup_windows(work_root: Path, session: str, max_age_h: float, now: float | None = None,
                    stale: set[str] | None = None, close_unknown_age: bool = False) -> list[str]:
    """Close finished windows (pane dead) older than `max_age_h` hours (0 = never), plus the live windows named in
    `stale`. Never the start window `runner`. Never raises: failures only go to logs/runner.log."""
    if max_age_h <= 0 and not stale:
        return []
    now = time.time() if now is None else now
    closed, lines = [], []
    try:
        for w in own_windows(session, work_root):  # Never another agent's window in a shared session.
            if w["name"] == "runner":
                continue
            if w["dead"]:
                age = _dead_age_s(work_root, w, now) if max_age_h > 0 else None
                if (age is None and not close_unknown_age) or (age is not None and age < max_age_h * 3600):
                    continue
            elif w["name"] not in (stale or set()):
                continue
            try:
                close_window(work_root, w)
                closed.append(w["name"])
                lines.append(_("tmux window %(name)s closed (content appended to its run log)", name=w["name"]))
            except Exception as e:  # noqa: BLE001
                lines.append(_("tmux window %(name)s could not be closed: %(error)s", name=w["name"], error=e))
    except Exception as e:  # noqa: BLE001 – housekeeping must never disturb the runner.
        lines.append(_("Cleaning up tmux windows failed: %(error)s", error=e))
    if lines:
        try:
            log_runner(work_root, lines)
        except OSError:
            pass
    return closed


def stale_windows(client: Client, session: str, work_root: Path) -> list[str]:
    """Live task windows of this agent whose task is no longer in progress for it (agent left running), and review
    windows whose review job it no longer holds. Windows of other agents in a shared session are never stale here:
    their tasks are not in this agent's list."""
    windows = [w["name"] for w in own_windows(session, work_root) if not w["dead"]]
    live = [name for name in windows if TASK_WINDOW.match(name)]
    reviews = [name for name in windows if REVIEW_WINDOW.match(name)]
    if not live and not reviews:
        return []
    running = {t["task_nr"] for t in client.get("/agents/me/tasks?status=in_progress").get("items") or []
               if t.get("status", "in_progress") == "in_progress"} if live else set()
    held = {t["task_nr"] + REVIEW_SUFFIX for t in own_review_jobs(client)} if reviews else set()
    return [name for name in live if name not in running] + [name for name in reviews if name not in held]


# --- blocker detection in the agent window --------------------------------------------------------------------------

BLOCK_FILE = ".agent-blocked"
# Known blockers on the agent's screen, matched case-insensitively per agent program ("*" = every agent). Extend here.
BLOCKERS = {
    "login": {"*": ["login expired", "please run /login", "not logged in", "invalid api key"],
              "codex": ["sign in with chatgpt", "run codex login"],
              "opencode": ["opencode auth login"]},
    "limit": {"*": ["usage limit", "rate limit", "rate-limit"],
              "claude": ["you've hit your limit", "limit reached"]},
    # The task's model is unknown or not part of the agent's plan: the task goes back to a human (abort), the agent
    # stays ready for other tasks.
    "model": {"*": ["model not found", "model not available", "model_not_found", "unknown model", "invalid model",
                    "model is not supported", "does not have access to model", "not available on your plan",
                    "not included in your plan"],
              "claude": ["issue with the selected model", "may not exist or you may not have access"],
              "codex": ["model is not supported when using codex", "does not exist or you do not have access"],
              "opencode": ["providermodelnotfounderror"]},
}
LOGIN_COMMANDS = {"claude": "claude /login", "codex": "codex login", "opencode": "opencode auth login"}
# The agent is busy (its status line) or has answered: then nothing on the screen counts as a blocker.
BUSY_MARKERS = ["esc to interrupt", "esc interrupt"]
ANSWER_MARKERS = ["⏺", "• ", "✻", "✶"]
BOX_CHARS = "│┃╭╮╰╯─━▌>›❯ \t"


def _lines(pane: str) -> list[str]:
    return [line.strip(BOX_CHARS) for line in pane.splitlines() if line.strip(BOX_CHARS)]


def _norm(text: str) -> str:
    return " ".join(text.split()).lower()


def classify_screen(pane: str, agent: str, prompt: str | None) -> tuple[str, str] | None:
    """('login'|'limit'|'model', matching line) or ('unsent', '') for an idle agent window, otherwise None.
    Only the bottom of the screen counts: older output of a working agent may mention 'rate limit' too."""
    lines = _lines(pane)[-20:]
    bottom = [_norm(line) for line in lines]
    if any(m in line for line in bottom[-10:] for m in BUSY_MARKERS):
        return None
    # Blocker messages come last; the agent's own answers above them may quote anything.
    answers = [i for i, line in enumerate(lines) if line.startswith(tuple(m.strip() for m in ANSWER_MARKERS))]
    start = answers[-1] + 1 if answers else 0
    for kind, patterns in BLOCKERS.items():
        needles = patterns.get("*", []) + patterns.get(agent, [])
        for line, original in zip(bottom[start:], lines[start:]):
            if any(n in line for n in needles):
                return kind, original[:200]
    head = _norm(prompt or "")[:30]
    if len(head) < 15:
        return None
    hits = [i for i, line in enumerate(bottom[-15:]) if head in line]
    if hits and not any(m in line for line in bottom[-15:][hits[-1] + 1:] for m in ANSWER_MARKERS):
        return "unsent", ""
    return None


def read_block(work_root: Path) -> dict | None:
    path = work_root / BLOCK_FILE
    if not path.is_file():
        return None
    try:
        return json.loads(path.read_text()) or {"reason": "?"}
    except (OSError, ValueError):
        return {"reason": path.read_text(errors="replace").strip()[:500] or "?"}


def write_block(work_root: Path, kind: str, reason: str, task_nr: str) -> None:
    (work_root / BLOCK_FILE).write_text(json.dumps({
        "kind": kind, "reason": reason, "task_nr": task_nr,
        "at": datetime.now(UTC).isoformat(timespec="seconds")}, ensure_ascii=False) + "\n")


def clear_block(work_root: Path) -> None:
    (work_root / BLOCK_FILE).unlink(missing_ok=True)


def _prompt_of(command: list[str]) -> str | None:
    """The start prompt is the longest argument with spaces (placeholder {prompt})."""
    texts = [a for a in command[1:] if " " in a and len(a) >= 40]
    return max(texts, key=len) if texts else None


def _model_of(command: list[str]) -> str | None:
    """The model passed on the command line (`--model x`, `-m x`, `--model=x`)."""
    for i, arg in enumerate(command[1:], 1):
        if arg in ("--model", "-m") and i + 1 < len(command):
            return command[i + 1]
        if arg.startswith("--model="):
            return arg.split("=", 1)[1]
    return None


def _agent_name(command: list[str]) -> str:
    if Path(command[0]).name != "env":
        return Path(command[0]).name
    rest = [p for p in command[1:] if "=" not in p and not p.startswith("-")]
    return Path(rest[0]).name if rest else "env"


# --- tbr session --------------------------------------------------------------------------------

class Heartbeat(threading.Thread):
    def __init__(self, client: Client, task_nr: str, interval_s: float, max_s: float):
        super().__init__(daemon=True)
        self.client, self.task_nr, self.interval_s, self.max_s = client, task_nr, interval_s, max_s
        self.stop_event = threading.Event()

    def run(self) -> None:
        started = time.monotonic()
        waiting = False  # Conscious waiting for a background run keeps the lease alive past the maximum.
        while not self.stop_event.wait(self.interval_s):
            if time.monotonic() - started > self.max_s and not waiting:
                typer.echo("\n[tm] " + _("Maximum run time reached – no more heartbeats for %(nr)s", nr=self.task_nr),
                           err=True)
                return
            try:
                waiting = bool(self.client.request("POST", f"/tasks/{self.task_nr}/heartbeat").json().get("waiting"))
            except ApiError as e:
                if e.status in (403, 404, 409):  # Task ended (submit/ask/abort) or was reassigned.
                    return

    def stop(self) -> None:
        self.stop_event.set()


def read_submit_file(worktree: Path) -> tuple[str, str] | None:
    """Read and remove `.tm-submit.md`; return (commit message, summary) or None."""
    path = worktree / SUBMIT_FILE
    if not path.is_file():
        return None
    text = path.read_text(errors="replace").strip()
    path.unlink(missing_ok=True)
    first, _sep, rest = text.partition("\n")
    message = first.strip().lstrip("#").strip()[:200]
    return message, rest.strip() or message


def finish(client: Client, task_nr: str, worktree: Path, exit_code: int) -> str:
    task = client.get(f"/tasks/{task_nr}")
    me = client.get("/agents/me")["name"]
    if task["status"] != "in_progress" or task.get("assigned_to") != me:
        return _("%(nr)s is '%(status)s' – the agent finished it itself", nr=task_nr, status=task["status"])
    if task.get("waiting"):  # Parked: the agent waits for a background run; the next passes keep the lease.
        return _("%(nr)s waits for a background run (%(reason)s) – stays in progress; the runner resumes it when the "
                 "waiting ends", nr=task_nr, reason=task["waiting"].get("reason") or "?")
    base = task["project_info"].get("default_branch") or "main"
    branch = task["branch_name"]
    markers = unresolved_conflicts(worktree)
    if markers:
        reason = _("Agent finished (exit %(rc)s), but conflict markers are still in %(files)s – not committed; "
                   "the next run has to resolve the merge conflict", rc=exit_code, files=", ".join(markers))
        client.request("POST", f"/tasks/{task_nr}/release", json={"reason": reason})
        return _("%(nr)s: given back (%(reason)s)", nr=task_nr, reason=reason)
    declared = read_submit_file(worktree)  # Completion by an agent without Git (git = "runner").
    if declared and _git("status", "--porcelain", cwd=worktree):
        _git("add", "-A", cwd=worktree)
        message = declared[0] or _("%(nr)s: result from %(agent)s", nr=task_nr, agent=me)
        _git("commit", "--quiet", "-m", message, cwd=worktree)
    _git("fetch", "--quiet", "origin", cwd=worktree)
    ahead = int(_git("rev-list", "--count", f"origin/{base}..HEAD", cwd=worktree) or 0)
    dirty = [line for line in _git("status", "--porcelain", cwd=worktree).splitlines() if line.strip()]
    if ahead:
        _git("push", "--quiet", "-u", "origin", f"HEAD:{branch}", cwd=worktree)
        sha = _git("rev-parse", "HEAD", cwd=worktree)
        points: list[str] = []
        if declared:
            text, points = split_open_points(declared[1])
            summary = (text or declared[0]) + "\n\n---\n" + _("Commit, push and submission by the runner (agent "
                                                                 "without Git access).")
        else:
            summary = _git("log", "-1", "--format=%B", cwd=worktree).strip() + "\n\n---\n" + _(
                "Submitted automatically by the runner: agent finished (exit %(rc)s) without tbr submit.", rc=exit_code
            ) + (_(" Note: %(num)s uncommitted change(s) in the worktree.", num=len(dirty)) if dirty else "")
        client.request("POST", f"/tasks/{task_nr}/submit",
                       json={"branch_name": branch, "commit_sha": sha, "summary": summary, "open_points": points})
        return _("%(nr)s: %(num)s commit(s) pushed and submitted", nr=task_nr, num=ahead)
    if declared:
        reason = _("Agent reported \"done\" (exit %(rc)s), but there are no changes compared to %(base)s",
                   rc=exit_code, base=base)
        client.request("POST", f"/tasks/{task_nr}/release", json={"reason": reason})
        return _("%(nr)s: given back (%(reason)s)", nr=task_nr, reason=reason)
    reason = _("Agent finished (exit %(rc)s) without a commit and without a closing step", rc=exit_code) + (
        _("; %(num)s uncommitted change(s) are in the worktree %(path)s", num=len(dirty), path=worktree)
        if dirty else "")
    client.request("POST", f"/tasks/{task_nr}/release", json={"reason": reason})
    return _("%(nr)s: given back (%(reason)s)", nr=task_nr, reason=reason)


def submit_file_ready(worktree: Path, settle_s: float) -> bool:
    """The completion file exists and has not changed for `settle_s` (fully written)."""
    try:
        return time.time() - (worktree / SUBMIT_FILE).stat().st_mtime >= settle_s
    except FileNotFoundError:
        return False


def _stop_agent(proc: subprocess.Popen) -> int:
    """Stop the agent after submission: first like Ctrl+C, then more forcefully; keep the tmux window open."""
    for sig, wait_s in ((signal.SIGINT, 15), (signal.SIGTERM, 10), (signal.SIGKILL, 5)):
        proc.send_signal(sig)
        try:
            return proc.wait(timeout=wait_s)
        except subprocess.TimeoutExpired:
            continue
    return proc.wait()


class Supervisor:
    """Watches a running agent for `tbr session`; `check()` returns a message when the session must stop the agent
    without a closing step of its own:

    - the task left in_progress (submit/ask/abort/release, or reassigned): stop after `stop_after_finish_seconds`;
    - start check (`start_check_seconds` after the start, tmux only): login expired, usage limit or the start prompt
      still unsent → Enter (at most twice), otherwise stop, release with the reason and block the agent;
    - stall (`stall_minutes`, tmux only): the window has not changed → one comment in the thread, agent keeps running;
    - parked (`park_idle_seconds`, tmux only): the agent reported waiting for a background run and its window has not
      changed since → stop the agent; the task stays in progress and `tbr run` keeps it (`keep_parked`).
    """

    ENTER_ATTEMPTS = 2

    def __init__(self, client: Client, task_nr: str, work_root: Path, cfg: dict, command: list[str],
                 pane: str | None, check_s: float, clock=time.monotonic, review: bool = False):
        self.client, self.task_nr, self.work_root, self.pane, self.clock = client, task_nr, work_root, pane, clock
        # Review job: the session runs while the agent holds the review (task stays for_review); otherwise while the
        # task is in progress.
        self.review, self.active = review, ("for_review" if review else "in_progress")
        self.check_s = check_s
        self.stop_after = _setting(cfg, "stop_after_finish_seconds", 120)
        self.start_check = _setting(cfg, "start_check_seconds", 60)
        self.stall_s = _setting(cfg, "stall_minutes", 30) * 60
        self.park_idle = _setting(cfg, "park_idle_seconds", 120)
        self.agent, self.prompt, self.command = _agent_name(command), _prompt_of(command), command
        self.me: str | None = None
        self.started = self.next_check = self.last_change = self.pane_change = clock()
        self.left_at: float | None = None
        self.left_status = ""
        self.start_checked = self.stall_reported = False
        self.enter_sent = 0
        self.last_pane: str | None = None
        self.waiting: dict | None = None  # Conscious waiting reported by the agent (valid, from the server).
        self.file_sig: tuple | None = None
        self.file_change = self.clock()
        self.file_reported = False

    def due(self) -> bool:
        return self.clock() >= self.next_check

    def check(self) -> tuple[str, str | None] | None:
        """None = keep running; (message, kind) = stop the agent; kind is the block kind or None."""
        now = self.clock()
        self.next_check = now + self.check_s
        status = self._task_status()
        if status is not None and status != self.active:
            if self.stop_after <= 0:
                return None
            if self.left_at is None:
                self.left_at, self.left_status = now, status
                typer.echo("\n[tm] " + _("%(nr)s is %(status)s – stopping the agent in %(sec)s s", nr=self.task_nr,
                                         status=status, sec=int(self.stop_after)))
            if now - self.left_at >= self.stop_after:
                return _("%(nr)s is %(status)s – agent stopped", nr=self.task_nr, status=self.left_status), None
            self.next_check = min(self.next_check, self.left_at + self.stop_after)
            return None
        self.left_at = None
        if not self.pane:
            return None
        pane = _capture(self.pane)
        if pane is None:
            return None
        if pane != self.last_pane:
            self.last_pane, self.last_change, self.pane_change, self.stall_reported = pane, now, now, False
        if not self.start_checked and self.start_check > 0 and now - self.started >= self.start_check:
            result = self._start_check(pane)
            if result:
                return result
        if self.waiting and self.park_idle > 0 and now - self.pane_change >= self.park_idle:
            # The agent reported waiting and then went idle (an interactive CLI does not exit by itself): end the
            # session; finish() is skipped, the task stays in progress and the next passes keep its lease.
            return _("%(nr)s waits for a background run and the agent is idle – session ended, the task stays in "
                     "progress (parked)", nr=self.task_nr), None
        if self.waiting:
            # The agent waits for a background run: that is no stall. Waiting counts as activity, so normal
            # detection starts only `stall_minutes` after it ended; a watched file that stays unchanged is reported.
            self.last_change, self.stall_reported = now, False
            self._check_watch_file(now)
            return None
        self.file_sig = None
        if self.stall_s > 0 and not self.stall_reported and now - self.last_change >= self.stall_s:
            self._report_stall(pane)
        return None

    def _check_watch_file(self, now: float) -> None:
        path = (self.waiting or {}).get("watch_file")
        if not path:
            return
        try:
            st = Path(path).expanduser().stat()
            sig: tuple | None = (st.st_mtime_ns, st.st_size)
        except OSError:
            sig = None
        if self.file_sig != (sig, path):
            self.file_sig, self.file_change, self.file_reported = (sig, path), now, False
        if self.stall_s > 0 and not self.file_reported and now - self.file_change >= self.stall_s:
            self._report_file_stall(path, sig is None)

    def _task_status(self) -> str | None:
        """Task status, 'reassigned' if another actor holds it now, None if the API is not reachable."""
        try:
            if self.me is None:
                self.me = self.client.get("/agents/me")["name"]
            task = self.client.get(f"/tasks/{self.task_nr}")
        except Exception:  # noqa: BLE001 – API or network errors must not end the session.
            return None
        if self.review:  # Decided (done/changes_requested), given back or taken over by someone else.
            held = task.get("reviewer") == self.me and task.get("review_claimed_at")
            return task.get("status") if task.get("status") != "for_review" or held else "released"
        if task.get("status") == "in_progress" and task.get("assigned_to") not in (None, self.me):
            return "reassigned"
        self.waiting = task.get("waiting") or None
        return task.get("status")

    def _start_check(self, pane: str) -> tuple[str, str] | None:
        found = classify_screen(pane, self.agent, self.prompt)
        if found and found[0] == "unsent" and self.enter_sent < self.ENTER_ATTEMPTS:
            self.enter_sent += 1
            _tmux("send-keys", "-t", self.pane, "Enter")
            typer.echo("\n[tm] " + _("Start prompt still in the input field – Enter sent (attempt %(n)s of %(max)s)",
                                     n=self.enter_sent, max=self.ENTER_ATTEMPTS))
            self.next_check = self.clock() + min(self.check_s, 10.0)
            return None
        self.start_checked = True
        if not found:
            return None
        kind, line = found
        host = socket.gethostname()
        me = self.me or self.agent
        if kind == "model":
            reason = _("%(agent)s: the model %(model)s is not available (%(line)s) – choose another model tier or model "
                       "for the task, or fix the agent's model settings in TaskBrigade", agent=me,
                       model=_model_of(self.command) or "?", line=line)
        elif kind == "login":
            reason = _("%(agent)s: login expired or not logged in – run `%(cmd)s` on %(host)s, then tbr doctor",
                       agent=me, cmd=LOGIN_COMMANDS.get(self.agent, self.agent), host=host)
        elif kind == "limit":
            reason = _("%(agent)s: usage or rate limit reached (%(line)s) – run tbr doctor on %(host)s once it has "
                       "reset", agent=me, line=line, host=host)
        else:
            reason = _("%(agent)s: the start prompt stayed unsent in the input field – check the agent window on "
                       "%(host)s, then run tbr doctor", agent=me, host=host)
        return reason, kind

    def exit_check(self) -> str | None:
        """The agent ended before the start check ran (e.g. the CLI refused the model and quit): a model error on
        the screen gives the reason; other early exits are handled by `finish` as before."""
        if self.start_checked or not self.pane:
            return None
        pane = _capture(self.pane)
        found = classify_screen(pane or "", self.agent, None)
        if not found or found[0] != "model":
            return None
        result = self._start_check(pane or "")
        return result[0] if result else None

    def _report_stall(self, pane: str) -> None:
        self.stall_reported = True
        since = datetime.now(UTC) - timedelta(seconds=self.clock() - self.last_change)
        tail = "\n".join(pane.rstrip().splitlines()[-15:])
        body = _("No activity in the agent window since %(since)s UTC (%(min)s min) – please check. Last lines:",
                 since=since.strftime("%Y-%m-%d %H:%M"), min=int(self.stall_s // 60)) + f"\n\n```\n{tail}\n```"
        try:
            self.client.request("POST", f"/tasks/{self.task_nr}/comments",
                                json={"type": "comment", "body": body, "signal": "stalled"})
            typer.echo("\n[tm] " + _("No activity for %(min)s min – note written to the thread", min=int(self.stall_s // 60)))
        except Exception as e:  # noqa: BLE001
            typer.echo("\n[tm] " + _("Stall note could not be written: %(error)s", error=e), err=True)

    def _report_file_stall(self, path: str, missing: bool) -> None:
        self.file_reported = True
        since = datetime.now(UTC) - timedelta(seconds=self.clock() - self.file_change)
        lines: list[str] = []
        try:
            with open(Path(path).expanduser(), "rb") as f:
                f.seek(0, os.SEEK_END)
                f.seek(max(0, f.tell() - 8192))
                lines = f.read().decode(errors="replace").rstrip().splitlines()[-15:]
        except OSError:
            pass
        body = _("Background run is no longer writing: %(path)s (no change since %(since)s UTC, %(min)s min) – please "
                 "check. Last lines:", path=path, since=since.strftime("%Y-%m-%d %H:%M"),
                 min=int(self.stall_s // 60)) + "\n\n```\n" + ("\n".join(lines) if lines else (
                     _("(file not found)") if missing else _("(file is empty)"))) + "\n```"
        try:
            self.client.request("POST", f"/tasks/{self.task_nr}/comments",
                                json={"type": "comment", "body": body, "signal": "stalled"})
            typer.echo("\n[tm] " + _("Background run silent for %(min)s min – note written to the thread",
                                     min=int(self.stall_s // 60)))
        except Exception as e:  # noqa: BLE001
            typer.echo("\n[tm] " + _("Stall note could not be written: %(error)s", error=e), err=True)


def session(client: Client, task_nr: str, work_root: Path, command: list[str],
            poll_s: float = 5.0, settle_s: float = 10.0, check_s: float = 60.0, pane: str | None = None,
            review: bool = False) -> int:
    # Taken first, so `tbr run` sees a running session right away and never starts a second run.
    name = task_nr + (REVIEW_SUFFIX if review else "")
    lock = acquire_session_lock(work_root, name)
    if lock is None:
        raise RunnerError(_("A session for %(nr)s is already running", nr=name))
    try:
        task = client.get(f"/tasks/{task_nr}")
        worktree = (review_worktree(work_root, task) if review
                    else work_root / task["project_info"]["key"] / ".worktrees" / task_nr)
        if not worktree.is_dir():
            raise RunnerError(_("Worktree %(path)s is missing – run tbr prepare first", path=worktree))
        cfg = runner_config(work_root)
        # Inside the tmux window `tbr session` finds its own pane for the start check and stall detection.
        if pane is None:
            pane = os.environ.get("TMUX_PANE") if cfg.get("mode", "tmux") == "tmux" else None
        sup = Supervisor(client, task_nr, work_root, cfg, command, pane, check_s, review=review)
        return _session(client, task_nr, worktree, cfg, command, poll_s, settle_s, sup)
    finally:
        release_session_lock(lock)


def finish_review(client: Client, task_nr: str, exit_code: int) -> str:
    """After a review session: a review job still held (no decision) goes back to the reviewer's queue."""
    task = client.get(f"/tasks/{task_nr}")
    me = client.get("/agents/me")["name"]
    if task["status"] != "for_review" or task.get("reviewer") != me or not task.get("review_claimed_at"):
        return _("%(nr)s is '%(status)s' – the reviewer decided itself", nr=task_nr, status=task["status"])
    reason = _("Reviewer finished (exit %(rc)s) without a decision", rc=exit_code)
    return release_review(client, task_nr, reason)


def _session(client: Client, task_nr: str, worktree: Path, cfg: dict, command: list[str], poll_s: float,
             settle_s: float, sup: Supervisor) -> int:
    # Interactive agents (Codex TUI) do not exit after completing work. For git = "runner",
    # watch for .tm-submit.md during the run and submit immediately when it appears (not for review jobs).
    watch = git_by_runner(cfg) and not sup.review
    lease_min = client.get("/agents/me").get("lease_minutes") or 120
    hb = Heartbeat(client, task_nr, interval_s=max(60.0, lease_min * 60 / 3),
                   max_s=float(cfg.get("max_session_hours", 8)) * 3600)
    hb.start()
    typer.echo("[tm] " + _("%(nr)s: starting %(cmd)s … (heartbeat every %(min)s min)", nr=task_nr,
                            cmd=shlex.join(command[:2]), min=int(hb.interval_s // 60)))
    done = None
    try:
        try:
            proc = subprocess.Popen(command, cwd=worktree)
        except OSError as e:
            reason = _("The agent command %(cmd)s could not be started: %(error)s", cmd=command[0], error=e)
            typer.echo("\n[tm] " + reason + " – " + give_back(client, task_nr, reason), err=True)
            return 127
        while True:
            try:
                rc = proc.wait(timeout=poll_s)
                break
            except subprocess.TimeoutExpired:
                pass
            if watch and submit_file_ready(worktree, settle_s):
                try:
                    done = finish(client, task_nr, worktree, 0)
                except (PrepareError, ApiError, RunnerError) as e:
                    typer.echo("\n[tm] " + _("Submitting failed (%(error)s) – retrying after the agent ends", error=e))
                    watch = False
                    continue
                typer.echo("\n[tm] " + _("%(result)s – stopping %(agent)s", result=done, agent=Path(command[0]).name))
                rc = _stop_agent(proc)
                break
            if not sup.due():
                continue
            stop = sup.check()
            if stop is None:
                continue
            done, kind = stop
            typer.echo("\n[tm] " + done)
            rc = _stop_agent(proc)
            if kind and sup.review:  # Review job: back to the queue with the reason; blockers also block new claims.
                if kind != "model":
                    write_block(sup.work_root, kind, done, task_nr)
                typer.echo("[tm] " + release_review(client, task_nr, done))
            elif kind == "model":  # Task-specific: a human picks another model; the agent takes other tasks.
                typer.echo("[tm] " + abort_task(client, task_nr, done))
            elif kind:  # Blocker: give the task back with the reason (also lands in the thread) and block new claims.
                write_block(sup.work_root, kind, done, task_nr)
                typer.echo("[tm] " + give_back(client, task_nr, done) + " – " +
                           _("no new tasks until tbr doctor reports the agent ready"))
            break
    finally:
        hb.stop()
        if Path(command[0]).name == "codex":
            codex_trust(worktree, False)
    if done is None:
        model_error = sup.exit_check()
        if model_error:
            back = release_review(client, task_nr, model_error) if sup.review else abort_task(client, task_nr,
                                                                                                model_error)
            typer.echo("\n[tm] " + model_error + " – " + back)
            return rc
        result = finish_review(client, task_nr, rc) if sup.review else finish(client, task_nr, worktree, rc)
        typer.echo(f"\n[tm] {result}")
    return rc
