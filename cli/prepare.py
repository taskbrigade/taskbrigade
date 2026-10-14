"""`tbr prepare`: claim a task and prepare the workspace – the LLM-free part of the runner (spec 12.2).

For agents with their own scheduler (Hermes cron, OpenClaw …): the script runs cheaply on a schedule; only when a
task was claimed does the agent get a ready assignment (worktree, TASK.md, instructions).

Layout under WORK_ROOT (which also holds `.taskbrigade.toml` with the agent's identity):
    <KEY>/                          clone of the project repo
    <KEY>/.worktrees/<NR>/          worktree on the task branch, containing TASK.md (ignored via info/exclude)
    <KEY>/.worktrees/<NR>-review/   review job of a reviewer agent: detached checkout of the task branch with
                                    REVIEW.md (read only – the decision goes through `tbr review`)
"""
import os
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

from cli.client import ApiError, Client
from cli.i18n import _


# Completion file for agents whose sandbox disallows Git (git = "runner", e.g. Codex): first line
# is the commit message, the rest is the summary. The runner commits, pushes, and submits outside the sandbox.
SUBMIT_FILE = ".tm-submit.md"
REVIEW_FILE = "REVIEW.md"
REVIEW_SUFFIX = "-review"  # Review worktree and tmux window: <NR>-review


class PrepareError(RuntimeError):
    pass


@dataclass
class Prepared:
    task: dict
    worktree: Path
    note: str = ""  # Note before instructions, e.g. a merge conflict with the base.
    review: bool = False  # Review job of a reviewer agent: REVIEW.md in a read-only checkout, no TASK.md.


def _git(*args: str, cwd: Path | None = None) -> str:
    env = {**os.environ, "GIT_TERMINAL_PROMPT": "0"}
    r = subprocess.run(["git", *args], cwd=cwd, env=env, capture_output=True, text=True)
    if r.returncode != 0:
        raise PrepareError(_("git %(args)s… failed: %(error)s", args=" ".join(args[:3]), error=r.stderr.strip()[-500:]))
    return r.stdout.strip()


COMMAND = "tbr"
LEGACY_COMMAND = "tm"  # Old name, still installed during the transition (cli/legacy.py).


def tm_command() -> str:
    """Absolute path to `tbr` so credential helpers and instructions work without PATH. Looks next to the running
    program too (uv's tool bin directory is not always on PATH, e.g. in a systemd unit); the old `tm` only if no
    `tbr` exists."""
    argv0 = Path(sys.argv[0])
    for name in (COMMAND, LEGACY_COMMAND):
        found = shutil.which(name)
        if found:
            return found
        sibling = argv0.with_name(name)
        if argv0.name in (COMMAND, LEGACY_COMMAND) and sibling.is_file() and os.access(sibling, os.X_OK):
            return str(sibling)
    return COMMAND


def _add_exclude(repo: Path, *patterns: str) -> None:
    exclude = repo / ".git" / "info" / "exclude"
    exclude.parent.mkdir(parents=True, exist_ok=True)
    existing = exclude.read_text().splitlines() if exclude.exists() else []
    missing = [p for p in patterns if p not in existing]
    if missing:
        with exclude.open("a") as f:
            f.write("".join(f"{p}\n" for p in missing))


def _set_helper(repo: Path, helper: str) -> None:
    """Use only the tbr helper: an empty entry resets the helper list. Otherwise Git could ask
    global helpers (e.g. `store` with another agent's login) and then save this agent's token
    in plain text to ~/.git-credentials after a push."""
    current = subprocess.run(["git", "config", "--local", "--get-all", "credential.helper"], cwd=repo,
                             capture_output=True, text=True).stdout.splitlines()
    if current == ["", helper]:
        return
    subprocess.run(["git", "config", "--local", "--unset-all", "credential.helper"], cwd=repo, capture_output=True)
    _git("config", "--local", "--add", "credential.helper", "", cwd=repo)
    _git("config", "--local", "--add", "credential.helper", helper, cwd=repo)


def _ensure_clone(repo: Path, clone_url: str, helper: str, creds: dict) -> None:
    if not (repo / ".git").exists():
        repo.parent.mkdir(parents=True, exist_ok=True)
        # Empty entry first discards global helpers (e.g. `store` with unrelated logins).
        # Use the workspace as cwd so the helper finds the agent's .taskbrigade.toml there,
        # regardless of where `tbr run` or `tbr prepare` was invoked.
        _git("-c", "credential.helper=", "-c", f"credential.helper={helper}", "clone", "--quiet", clone_url,
             str(repo), cwd=repo.parent)
    else:
        _set_helper(repo, helper)
        _git("fetch", "--quiet", "--prune", "origin", cwd=repo)
    # Set the agent's Git identity locally in this repo, not globally.
    _set_helper(repo, helper)
    _git("config", "user.name", creds["username"], cwd=repo)
    if creds.get("email"):
        _git("config", "user.email", creds["email"], cwd=repo)
    _add_exclude(repo, ".worktrees/", "TASK.md", SUBMIT_FILE, REVIEW_FILE)


def _ensure_worktree(repo: Path, worktree: Path, branch: str, base: str) -> list[str]:
    """Prepare the task branch worktree; return files with merge conflicts, or an empty list."""
    remote_branch = bool(_git("ls-remote", "--heads", "origin", branch, cwd=repo))
    if remote_branch:
        _git("fetch", "--quiet", "origin", f"{branch}:refs/remotes/origin/{branch}", cwd=repo)
    if worktree.exists() and _merge_in_progress(worktree):  # Conflict from the previous run remains unresolved.
        return _conflicted(worktree) or unresolved_conflicts(worktree)
    if worktree.exists():
        _git("checkout", "--quiet", branch, cwd=worktree)
        if remote_branch:  # E.g. after changes_requested, accept remote updates without discarding local work.
            _git("merge", "--quiet", "--ff-only", f"origin/{branch}", cwd=worktree)
        # Always bring a reused worktree up to the fetched base – also without a remote branch (e.g. the first run
        # only asked a question): otherwise it stays on the base of its first run.
        return merge_base_branch(worktree, base)
    start = f"origin/{branch}" if remote_branch else f"origin/{base}"
    _git("worktree", "prune", cwd=repo)
    _git("worktree", "add", "--quiet", "-B", branch, str(worktree), start, cwd=repo)
    return merge_base_branch(worktree, base) if remote_branch else []


def merge_base_branch(worktree: Path, base: str) -> list[str]:
    """Merge the current base (e.g. parallel completed tasks) into the task branch so the PR stays mergeable.
    Conflicts remain as file markers for the agent to resolve; the next commit (or the runner/MCP server)
    finishes the merge. Requires a clean worktree and never touches work in progress."""
    if _merge_in_progress(worktree):  # A conflict from an earlier run is still open.
        return _conflicted(worktree)
    if _git("status", "--porcelain", cwd=worktree):
        return []
    if _is_ancestor(worktree, f"origin/{base}", "HEAD"):
        return []
    if _is_ancestor(worktree, "HEAD", f"origin/{base}"):  # No own commits: just move up to the base.
        _git("merge", "--quiet", "--ff-only", f"origin/{base}", cwd=worktree)
        return []
    env = {**os.environ, "GIT_TERMINAL_PROMPT": "0", "GIT_MERGE_AUTOEDIT": "no"}
    r = subprocess.run(["git", "merge", "--no-edit", "--quiet", f"origin/{base}"], cwd=worktree, env=env,
                       capture_output=True, text=True)
    if r.returncode == 0:
        return []
    files = _conflicted(worktree)
    if not files:  # Another error, not a conflict: abort the merge and continue without it.
        subprocess.run(["git", "merge", "--abort"], cwd=worktree, capture_output=True)
    return files


def _is_ancestor(cwd: Path, older: str, newer: str) -> bool:
    return subprocess.run(["git", "merge-base", "--is-ancestor", older, newer], cwd=cwd,
                          capture_output=True).returncode == 0


def commits_behind(worktree: Path, base: str) -> int:
    """Number of commits on `origin/<base>` that the task branch does not contain yet (0 if unknown)."""
    r = subprocess.run(["git", "rev-list", "--count", f"HEAD..origin/{base}"], cwd=worktree, capture_output=True,
                       text=True)
    return int(r.stdout.strip()) if r.returncode == 0 and r.stdout.strip().isdigit() else 0


def _sync_local_base(repo: Path, base: str) -> None:
    """Move the clone's local `<base>` along with `origin/<base>` (fast-forward only). Worktrees start from
    `origin/<base>`, but a stale local branch confuses anyone looking into the clone. Best effort: a diverged,
    dirty or missing branch is left alone."""
    def run(*args: str) -> subprocess.CompletedProcess:
        return subprocess.run(["git", *args], cwd=repo, capture_output=True, text=True)

    local, remote = f"refs/heads/{base}", f"refs/remotes/origin/{base}"
    if run("rev-parse", "-q", "--verify", local).returncode or run("rev-parse", "-q", "--verify", remote).returncode:
        return
    if not _is_ancestor(repo, local, remote):
        return
    if run("symbolic-ref", "-q", "HEAD").stdout.strip() == local:
        if not run("status", "--porcelain", "--untracked-files=no").stdout.strip():
            run("merge", "--quiet", "--ff-only", remote)
    else:
        run("update-ref", local, remote)


def stale_base_note(base: str, behind: int) -> str:
    return _("""
## ⚠ Your task branch is behind `origin/%(base)s`

Your working directory has uncommitted changes, so the current `%(base)s` was **not** merged in (%(count)s commits \
missing). Work done on `%(base)s` since your last run is not visible here. If the task depends on it: commit your \
changes, then `git merge origin/%(base)s` (with git yourself) – or finish your changes and mention the outdated base \
in your summary.
""", base=base, count=behind)


def unresolved_conflicts(worktree: Path) -> list[str]:
    """Find changed files with unresolved conflict markers; they must not be committed."""
    changed = set(_conflicted(worktree))
    out = subprocess.run(["git", "diff", "--name-only", "HEAD"], cwd=worktree, capture_output=True, text=True).stdout
    changed |= {line for line in out.splitlines() if line.strip()}
    bad = []
    for name in sorted(changed):
        path = worktree / name
        try:
            text = path.read_text(errors="replace")
        except (FileNotFoundError, IsADirectoryError):
            continue
        if re.search(r"^<{7} |^>{7} ", text, re.M):
            bad.append(name)
    return bad


def _merge_in_progress(worktree: Path) -> bool:
    return subprocess.run(["git", "rev-parse", "-q", "--verify", "MERGE_HEAD"], cwd=worktree,
                          capture_output=True).returncode == 0


def _conflicted(worktree: Path) -> list[str]:
    out = subprocess.run(["git", "diff", "--name-only", "--diff-filter=U"], cwd=worktree, capture_output=True,
                         text=True).stdout
    return [line for line in out.splitlines() if line.strip()]


def conflict_note(base: str, files: list[str]) -> str:
    return _("""
## ⚠ Merge conflict with `%(base)s` – resolve it first

The current state of `%(base)s` was merged into your task branch. These files contain conflict markers (`<<<<<<<`, \
`=======`, `>>>>>>>`): %(files)s.
Resolve the markers and keep **both** changes (for brain.md: keep all entries; record future findings in \
brain-changes/<TASK-NR>.md instead of brain.md). If you work with git yourself: afterwards `git add <file>` for each \
file and `git commit -m "Merge %(base)s into the task branch"`, then push and submit as usual. Without git (runner \
with submit file, submit_task): just finish as usual – the commit completes the merge.
""", base=base, files=", ".join(f"`{f}`" for f in files))


def tm_display(tm_abs: str) -> tuple[str, str]:
    """Use the short name (`tbr`) in TASK.md when it is on PATH so agent allow rules like `tbr *` or `Bash(tbr:*)`
    apply. Return the absolute path as a fallback hint in the second value."""
    name = Path(tm_abs).name
    found = shutil.which(name) if name in (COMMAND, LEGACY_COMMAND) else None
    if found and Path(found).resolve() == Path(tm_abs).resolve():
        return name, _(" (if `%(name)s` is not found: `%(path)s`)", name=name, path=tm_abs)
    return tm_abs, ""


def instructions(task: dict, worktree: Path, tm: str, git_by_runner: bool = False) -> str:
    tm, tm_hint = tm_display(tm)
    if git_by_runner:
        return runner_git_instructions(task, worktree, tm, tm_hint)
    pi = task["project_info"]
    return _("""
## How to work on this task

- Working directory: `%(worktree)s` (branch `%(branch)s`). **Run all commands there.**
- **Run commands one at a time:** `git` and `%(tm)s` each as a separate call – no `&&` chains, no
  heredocs, no pipes. Commit messages with `git commit -m "…"`. Only then do the permissions of your
  unattended run apply; otherwise you get stuck at a confirmation prompt.
- TaskBrigade CLI: `%(tm)s`%(tm_hint)s.
- First read `%(brain)s` and `AGENTS.md` in the working directory.
- Never push to `%(base)s`.
- New findings do **not** go directly into brain.md but into `brain-changes/%(nr)s.md`
  (`## add: <section>` appends; `## change:`/`## remove:` with the exact old text; format in
  `brain-changes/README.md`). TaskBrigade applies them on approval.
- A `CHANGELOG.md` entry (if the repository has one) does **not** go directly into the file but into
  `changelog.d/%(nr)s.md` (Markdown bullet points); TaskBrigade adds it under „Unreleased“ on approval.
- If it takes longer than 30 minutes: run `%(tm)s heartbeat %(nr)s` in between.
- Long background job (nohup, model test …): write a log file, then run `%(tm)s heartbeat %(nr)s --waiting "<reason>" --for 2h --watch-file <log>` and repeat it before it ends (max. 24 h) – otherwise the runner reports a stall. Report progress with `%(tm)s comment`; `--done-waiting` ends the waiting state.

End your run with **exactly one** of these steps:
1. **Done:** commit, `git push -u origin %(branch)s`, then
   `%(tm)s submit %(nr)s --summary "<what changed, how it was tested>" --open-point "<point>"` – one
   `--open-point` per point that remains open (leave it out if nothing is left); humans take these over into a
   follow-up task, so name them separately instead of only in the summary.
2. **Unclear:** `%(tm)s ask %(nr)s "<specific question>"` – then stop; the answer arrives with the next run.
3. **Not solvable for you** (too complex, missing capability): commit and push the current state, then
   `%(tm)s abort %(nr)s --reason "<why it fails>"` – a human reassigns the task.
4. **Run is not enough** (step/time limit in sight, but the work is doable): **first** write the current state into
   the thread – `%(tm)s comment %(nr)s "<what is done, where it is, next steps>"` –, then
   `%(tm)s release %(nr)s --reason "Continuation needed"`. The next run is a new session and only knows the thread;
   without this comment it starts from scratch.
5. **Waiting for a long background job** (it keeps running after your run): report it with
   `%(tm)s heartbeat %(nr)s --waiting "<reason>" --for 2h --watch-file <log>`, write the current state with
   `%(tm)s comment %(nr)s "…"`, then end the run **without** `release`. The task stays in progress; the runner keeps
   it and starts a new run when the waiting ends (`--for` expired) or the log stops changing. Do not wait inside the
   run with sleep loops.
""", worktree=worktree, branch=task["branch_name"], tm=tm, tm_hint=tm_hint, nr=task["task_nr"],
             brain=pi.get("brain_path") or "brain.md", base=pi.get("default_branch") or "main")


def runner_git_instructions(task: dict, worktree: Path, tm: str, tm_hint: str) -> str:
    return _("""
## How to work on this task

- Working directory: `%(worktree)s`. **Run all commands there.**
- **No git commands** (no commit, no push, no new clone): your sandbox does not allow writing to Git data.
  The runner commits, pushes and submits after your run. You only edit files here.
- TaskBrigade CLI: `%(tm)s`%(tm_hint)s – run commands one at a time, without `&&` chains, heredocs or pipes.
- First read `%(brain)s` and `AGENTS.md` in the working directory.
  New findings do **not** go directly into brain.md but into `brain-changes/%(nr)s.md`
  (`## add: <section>` appends; `## change:`/`## remove:` with the exact old text; format in
  `brain-changes/README.md`). TaskBrigade applies them on approval.
- A `CHANGELOG.md` entry (if the repository has one) does **not** go directly into the file but into
  `changelog.d/%(nr)s.md` (Markdown bullet points); TaskBrigade adds it under „Unreleased“ on approval.
- If it takes longer than 30 minutes: run `%(tm)s heartbeat %(nr)s` in between.
- Long background job (nohup, model test …): write a log file, then run `%(tm)s heartbeat %(nr)s --waiting "<reason>" --for 2h --watch-file <log>` and repeat it before it ends (max. 24 h) – otherwise the runner reports a stall. Report progress with `%(tm)s comment`; `--done-waiting` ends the waiting state.

End your run with **exactly one** of these steps:
1. **Done:** create the file `%(submit_file)s` in the working directory – first line the commit message, then an
   empty line, then the summary (what changed, how it was checked). Points that remain open go at the end under
   a heading `## Open points`, one bullet each – humans take them over into a follow-up task. Then end the run.
   The runner commits all changes, pushes and submits (pull request). Do **not** call `%(tm)s submit`.
2. **Unclear:** `%(tm)s ask %(nr)s "<specific question>"` – then stop; the answer arrives with the next run.
3. **Not solvable for you** (too complex, missing capability): `%(tm)s abort %(nr)s --reason "<why it fails>"` –
   a human reassigns the task.
4. **Run is not enough** (step/time limit in sight, but the work is doable): **first** write the current state into
   the thread – `%(tm)s comment %(nr)s "<what is done, where it is, next steps>"` –, then
   `%(tm)s release %(nr)s --reason "Continuation needed"`. Your files stay in the working directory for the next run.
5. **Waiting for a long background job** (it keeps running after your run): report it with
   `%(tm)s heartbeat %(nr)s --waiting "<reason>" --for 2h --watch-file <log>`, write the current state with
   `%(tm)s comment %(nr)s "…"`, then end the run **without** `release`. The task stays in progress; the runner keeps
   it and starts a new run when the waiting ends (`--for` expired) or the log stops changing. Do not wait inside the
   run with sleep loops.
""", worktree=worktree, tm=tm, tm_hint=tm_hint, nr=task["task_nr"], submit_file=SUBMIT_FILE,
             brain=task["project_info"].get("brain_path") or "brain.md")


def sftp_note(task: dict, tm: str) -> str:
    """TASK.md section when the server has SFTP storage enabled and this agent has a login."""
    tm, _hint = tm_display(tm)
    return _("""
## SFTP storage

The team's SFTP storage is available. Put results there **only when the task asks for it**:
`%(tm)s sftp put %(nr)s <files…>` (into the task's folder; `--to <sub-path>` for a sub-folder),
`%(tm)s sftp ls %(nr)s` lists it. Every upload is noted in the task with size and SHA-256. Never print or store the
SFTP password – `%(tm)s sftp` fetches it itself. If the SFTP server is not reachable from where you run, say so in
your summary instead of retrying.
""", tm=tm, nr=task["task_nr"])


def sftp_available(client: Client) -> bool:
    """True if SFTP storage is enabled and this agent has a login (older servers: False)."""
    try:
        client.get("/agents/me/sftp")
    except ApiError:
        return False
    return True


def own_running_task(client: Client) -> str | None:
    """Return an in-progress own task (e.g. after a run hit its step limit) or a held review job, else None."""
    items = client.get("/agents/me/tasks", params={"status": "in_progress"})["items"]
    if items:
        return items[0]["task_nr"]
    try:  # Reviewer agents: a review job claimed by an earlier run (older servers: none).
        jobs = [t for t in client.get("/agents/me/reviews").get("items") or [] if t.get("review_claimed")]
    except ApiError:
        return None
    return jobs[0]["task_nr"] if jobs else None


def prepare(client: Client, work_root: Path, task_nr: str | None = None,
            git_by_runner: bool = False, reviews: bool = True) -> Prepared | None:
    """None means no task. With `task_nr`, claim exactly that task or restore its workspace if already
    in progress for this agent (e.g. a new desktop chat). Raise PrepareError on failure; a newly
    claimed task is returned in that case. `reviews`: a reviewer agent may get a review job instead
    (`Prepared.review`, REVIEW.md in `<NR>-review`); a named task waiting for its review is always one."""
    try:
        creds = client.get("/agents/me/git-credentials")
    except ApiError as e:
        raise PrepareError(_("No Git credentials for this agent (%(error)s) – add them in TaskBrigade under "
                             "Admin → Users → Gitea", error=e)) from e
    resumed = False
    if task_nr:
        current = client.get(f"/tasks/{task_nr}")
        me = client.get("/agents/me")["name"]
        resumed = current["status"] == "in_progress" and current.get("assigned_to") == me
    if resumed:
        task = current
        try:  # Refresh the lease as the next run resumes work now.
            client.request("POST", f"/tasks/{task['task_nr']}/heartbeat")
        except ApiError:
            pass
    else:
        body = {**({"task_nr": task_nr} if task_nr else {}), **({"reviews": True} if reviews else {})}
        r = client.request("POST", "/agents/me/claim", json=body or None)
        if r.status_code == 204:
            return None
        task = r.json()
    if task.get("job") == "review":
        return prepare_review(client, work_root, task, creds)
    try:
        pi = task["project_info"]
        if not pi.get("clone_url"):
            raise PrepareError(_("Project %(key)s has no Gitea repository", key=pi.get("key")))
        helper = f"!{tm_command()} git-credential"
        repo = work_root / pi["key"]
        worktree = repo / ".worktrees" / task["task_nr"]
        _ensure_clone(repo, pi["clone_url"], helper, creds)
        base = pi.get("default_branch") or "main"
        _sync_local_base(repo, base)
        conflicts = _ensure_worktree(repo, worktree, task["branch_name"], base)
        from cli.tm import task_markdown

        if conflicts:
            note = conflict_note(base, conflicts)
        else:
            behind = commits_behind(worktree, base)
            note = stale_base_note(base, behind) if behind else ""
        extra = sftp_note(task, tm_command()) if sftp_available(client) else ""
        (worktree / "TASK.md").write_text(task_markdown(task) + note + instructions(task, worktree, tm_command(),
                                                                                           git_by_runner) + extra)
        return Prepared(task, worktree, note)
    except (PrepareError, OSError, KeyError) as e:
        if resumed:  # Do not return a task that was already in progress.
            raise PrepareError(str(e)) from e
        try:
            client.request("POST", f"/tasks/{task['task_nr']}/release",
                           json={"reason": _("Could not prepare the workspace: %(error)s", error=e)[:2000]})
        except ApiError:
            pass
        raise PrepareError(str(e)) from e


# --- Review jobs (reviewer agents) ----------------------------------------------------------------------------------

def review_worktree(work_root: Path, task: dict) -> Path:
    return work_root / task["project_info"]["key"] / ".worktrees" / (task["task_nr"] + REVIEW_SUFFIX)


def _review_checkout(repo: Path, worktree: Path, branch: str) -> None:
    """Detached checkout of the task branch as pushed (`origin/<branch>`); a reused one is reset to it."""
    _git("fetch", "--quiet", "origin", f"+{branch}:refs/remotes/origin/{branch}", cwd=repo)
    if worktree.exists():
        _git("checkout", "--quiet", "--force", "--detach", f"origin/{branch}", cwd=worktree)
        return
    _git("worktree", "prune", cwd=repo)
    _git("worktree", "add", "--quiet", "--detach", str(worktree), f"origin/{branch}", cwd=repo)


def diff_stat(worktree: Path, base: str, max_lines: int = 60) -> str:
    """`git diff --stat` of the task branch against the base (merge base), shortened; empty if unknown."""
    r = subprocess.run(["git", "diff", "--stat", f"origin/{base}...HEAD"], cwd=worktree, capture_output=True,
                       text=True)
    lines = r.stdout.rstrip().splitlines() if r.returncode == 0 else []
    if len(lines) > max_lines:
        lines = lines[:max_lines - 1] + ["…", lines[-1]]
    return "\n".join(lines)


def review_markdown(task: dict, worktree: Path, tm: str, stat: str) -> str:
    """REVIEW.md: the task, the result to review and the reviewer's commands."""
    from cli.tm import task_markdown

    tm, tm_hint = tm_display(tm)
    pi = task["project_info"]
    nr, base, branch = task["task_nr"], pi.get("default_branch") or "main", task.get("branch_name") or "?"
    points = "".join(f"- {p}\n" for p in task.get("open_points") or []) or "–\n"
    head = _("# TaskBrigade review job %(nr)s (already claimed for you)", nr=nr) + "\n\n"
    return head + task_markdown(task, footer=False) + _("""
## Result to review

- Assignee: %(assignee)s · branch `%(branch)s` (commit `%(sha)s`)
- Pull request: %(pr)s

### Summary of the assignee

%(summary)s

### Open points named by the assignee

%(points)s
### Changes compared to `%(base)s`

```
%(stat)s
```

Full diff: `git diff origin/%(base)s...HEAD` in the working directory.

## How to review

- Working directory: `%(worktree)s` – a **read-only** checkout of the task branch (detached HEAD). Do not edit,
  commit or push here; your decision goes through `%(tm)s review`.
- TaskBrigade CLI: `%(tm)s`%(tm_hint)s – run commands one at a time, without `&&` chains, heredocs or pipes.
- Read `%(brain)s` and `AGENTS.md`, then check the changes against the description and the acceptance criteria.
  Run the project's tests if `%(brain)s` names them and your environment allows it.
- `brain-changes/%(nr)s.md` and `changelog.d/%(nr)s.md` are applied by TaskBrigade on approval – check that their
  format is valid (`brain-changes/README.md`, bullet points only).
- If it takes longer than 30 minutes: run `%(tm)s heartbeat %(nr)s` in between.

End your run with **exactly one** decision:
1. **Approve:** `%(tm)s review %(nr)s approve --comment "<what you checked>"` – TaskBrigade merges the pull request
   and applies brain-changes and changelog.d. If it reports a merge, brain or changelog conflict, use 3.
2. **Reject:** `%(tm)s review %(nr)s reject --comment "<what is missing or wrong, concretely>"` – the task goes back
   to the assignee with your comment.
3. **Conflict:** `%(tm)s review %(nr)s conflict [--comment "<note>"]` – returns a merge/brain/changelog conflict to the
   assignee with instructions.
4. **Cannot review now** (environment problem, step limit): `%(tm)s review %(nr)s release --reason "<why>"` – the
   review job goes back to your queue.
The project owner and admins can still decide themselves; you never approve your own work.
""", assignee=task.get("assigned_to") or "–", branch=branch, sha=(task.get("result_commit_sha") or "?")[:10],
        pr=task.get("pr_url") or "–", summary=task.get("result_summary") or "–", points=points, base=base,
        stat=stat or _("(no changes found)"), worktree=worktree, tm=tm, tm_hint=tm_hint, nr=nr,
        brain=pi.get("brain_path") or "brain.md")


def release_review(client: Client, task_nr: str, reason: str) -> str:
    """Give a review job back undecided; never raises."""
    try:
        client.request("POST", f"/tasks/{task_nr}/review/release", json={"reason": reason[:2000]})
    except ApiError as e:
        return _("%(nr)s could not be given back (%(error)s)", nr=task_nr, error=e)
    return _("%(nr)s: review job given back", nr=task_nr)


def prepare_review(client: Client, work_root: Path, task: dict, creds: dict) -> Prepared:
    """Review job: clone, detached checkout of the task branch in `<NR>-review`, REVIEW.md. On failure the job is
    given back and PrepareError raised."""
    try:
        pi = task["project_info"]
        if not pi.get("clone_url"):
            raise PrepareError(_("Project %(key)s has no Gitea repository", key=pi.get("key")))
        if not task.get("branch_name"):
            raise PrepareError(_("%(nr)s has no task branch to review", nr=task["task_nr"]))
        repo = work_root / pi["key"]
        worktree = review_worktree(work_root, task)
        _ensure_clone(repo, pi["clone_url"], f"!{tm_command()} git-credential", creds)
        base = pi.get("default_branch") or "main"
        _sync_local_base(repo, base)
        _review_checkout(repo, worktree, task["branch_name"])
        (worktree / REVIEW_FILE).write_text(review_markdown(task, worktree, tm_command(),
                                                            diff_stat(worktree, base)))
        return Prepared(task, worktree, review=True)
    except (PrepareError, OSError, KeyError) as e:
        release_review(client, task["task_nr"], _("Could not prepare the workspace: %(error)s", error=e))
        raise PrepareError(str(e)) from e
