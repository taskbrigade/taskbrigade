"""`tbr mcp`: local MCP server (stdio) for desktop apps (Claude Cowork, Codex Desktop) – manual operation.

Git and network run exclusively here on the machine, not in the app: Cowork works in a Linux VM with a network
allowlist, Codex in a sandbox – neither reaches Gitea/TaskBrigade itself. The app only edits files in the working
folder; `submit_task` commits, pushes and submits.

Invocation (in the app's MCP configuration):  tbr mcp --work-root ~/taskbrigade-work/<agent>
"""
import threading
import time
from pathlib import Path

from mcp.server.mcpserver import MCPServer

from cli.client import BATCH_FORMAT, ApiError, Client, close_report, explain, is_no_sprint, parse_batch, parse_due
from cli.client import parse_depends_on
from cli.client import NOTIFY_ACTIONS, notify_report, notify_request, parallel_limit_note, patch_task, release_report
from cli.client import reopen_report
from cli.client import duplicate_task, model_line, model_value, parse_tier, resolve_sprint, sprint_line, transition
from cli.client import agent_scope, heartbeat_task, review_task, waiting_report
from cli.client import data_class_line, parse_data_class
from cli.client import email_report, emails_report, recipients_report, send_email
from cli import attachments as att, sftp
from cli.i18n import N_, _
from cli.prepare import PrepareError, _git, prepare, unresolved_conflicts
from cli.workflow_mcp import WorkflowTools, register as register_workflows

# Translated in build_server (`_()`), so the language from the agent's .taskbrigade.toml applies
DATA_CLASS_HELP = N_("data_class: cloud_ok, local_only or sealed (\"inherit\" = the project's privacy, the "
                     "default). A class looser than the project's is reserved for the owner and admins; once work "
                     "has started it can only be made stricter.")
INSTRUCTIONS = N_("""\
TaskBrigade is your team's central task manager. With these tools you claim tasks and report results.

Flow:
1. "Check whether there is a task for me" → list_tasks. "Work on task XYZ" → start_task(task_nr="XYZ");
   without a number start_task claims the next task.
2. start_task returns the task (description, acceptance criteria, complete thread) and the working folder.
   Read brain.md and AGENTS.md there first. Only edit files in this working folder.
   New project findings do not go directly into brain.md but into brain-changes/<TASK-NR>.md (## add: <section>,
   ## change:/## remove: with the exact old text; format in brain-changes/README.md).
   A CHANGELOG.md entry (if the repository has one) goes into changelog.d/<TASK-NR>.md (bullet points only).
3. Do NOT run git commands and do not call TaskBrigade yourself – submit_task takes care of commit, push and pull
   request. show_changes shows your changes so far.
4. Finish with exactly one step:
   - done → submit_task(task_nr, summary, commit_message, open_points, outputs): summary = what changed, how it was
     checked; open_points = list of points that remain open (one short, self-contained sentence each; leave it
     out if nothing is left). Humans take them over into a follow-up task, so do not hide them in the summary.
     outputs = declared workflow outputs (object; omit when the task lists none). Required outputs are also listed in TASK.md.
   - unclear → ask(task_nr, question) and stop; the answer arrives in the thread with the next start_task
   - not solvable for you → abort_task(task_nr, reason); a human reassigns the task
Never push anything to main or write secrets into files. The server extends the lease automatically.

Creating new tasks (when the user asks for it, e.g. "Create a task for codex …"):
- list_projects and list_agents show project keys and possible assignees.
- create_task(project, title, description, acceptance_criteria, assigned_to, prio, draft, due_date).
  "Defined" (ready to claim) needs a description AND acceptance criteria; otherwise draft=True (draft, a human
  publishes it). due_date (YYYY-MM-DD) is optional and for information only – it plays no role in claiming.
  Write the description and criteria so concretely that the agent can work without asking back. If something
  essential is missing (project, goal, form of the result), ask first. Without assigned_to nobody claims the task.
- Assign projects with privacy "local_only" only to agents with a local model (model location local, runtime local).
- Attachments: create_task(…, attachments=[paths]) or attach_files(task_nr, paths) attach files from this machine –
  only from the allowed folders (work directory, ~/Downloads, ~/Documents, ~/Desktop unless configured otherwise),
  never hidden files or keys. A file you cannot reach by path (e.g. dropped into the chat) can be attached as text
  with attach_text(task_nr, filename, content), at most 1 MB.
- Existing tasks (role orchestrator/admin only): update_task changes due date, assignee, prio, title,
  description, criteria; cancel_task(task_nr, reason) cancels a task; reset_task sets it back to "Defined"
  (permissions as in the web UI). Move wrongly assigned tasks instead of copying them.
- duplicate_task(task_nr, assign, title) copies a task into a new draft of the same project (e.g. for a second
  opinion from another agent/model); without assign the draft stays unassigned.
- reviewer (create_task/update_task): who reviews the result – a human or an agent with role "reviewer", never the
  assignee; empty = the project's default reviewer, "none" = the project owner and admins.
- depends_on (create_task/update_task): task numbers (list or comma-separated string) that must be done first –
  the agent can claim the task only once all dependencies are done. Set real dependencies instead of writing
  "depends on X" into the description. update_task replaces the list ("none" removes all, omitted = unchanged);
  show_task lists the dependencies with their status. Errors (unknown number, self-dependency, no permission) are
  reported as the server's message.

Reviews (agents with role "reviewer"): a task waiting for your review is a review job – list_tasks shows it under
"Review jobs", start_task claims it and returns REVIEW.md with a read-only checkout. Check the changes against the
description and acceptance criteria, do not edit anything, and finish with review_task(task_nr, decision, comment):
approve (merges), reject (comment required), conflict (returns a merge/brain/changelog conflict) or release (give the
job back undecided). You can only decide tasks you are entered for as reviewer.

Sprints (optional level project → sprint → task; sprint = id or name):
- list_sprints(project) and show_sprint(project, sprint) show sprints, goal and tasks.
- create_sprint, update_sprint (name, goal, start_date, end_date; only the given fields; "none" clears a date and
  an empty goal clears it; the status stays unchanged), add_sprint_tasks (several tasks at once, one line each:
  "Title | Agent | Prio | Reviewer") and the sprint parameter of create_task/update_task ("none" detaches) – admins,
  orchestrators and the project owner.
- Tasks in a sprint that is not released yet are drafts. release_sprint publishes every draft with description and
  acceptance criteria and lists the rest; close_sprint moves open tasks (move_to) or detaches them (detach=True).
  reopen_sprint reopens a closed sprint (status back to released); with restore_tasks (default True) the tasks that
  were moved out when closing return, their status unchanged; tasks moved elsewhere later or finished are skipped.
  release_sprint, close_sprint and reopen_sprint only for admins and the project owner – otherwise report the
  refusal to the user. Released tasks are worked on in parallel; order only through prio and dependencies.

SFTP storage (if the admin enabled it): put results on the team's SFTP server only when the task asks for it –
sftp_put(task_nr, files) uploads files from the working folder into the task's folder, sftp_ls lists folders.

Results by email (if the project has recipients): only when the task asks for it – send_email(task_nr, to, subject,
body, attachments) files a request, the server checks the project's recipient list (list_mail_recipients) and sends
it after a human approved it; list_emails(task_nr) shows the status. Attachments are references the server reads
itself (attachment:<id>, branch:<path>, merge:<path>) – push files before you reference them.
result:<id> attaches a released output-gate aggregate. Local-only mail accepts only these attachments and always
needs separate human mail approval; sealed mail is refused.

Notifications: notify(action="status"|"pause"|"resume"|"reviews_off"|"reviews_on", duration, user) mutes or unmutes
Telegram messages ("mute notifications for 2 hours" → notify("pause", "2h", user=<human>)). For someone else only
with role orchestrator/admin; reviews and approvals stay with the humans (TaskBrigade bot buttons or web UI) unless
you are the task's entered reviewer.
""")


class TaskBrigadeMCP(WorkflowTools):
    def __init__(self, client: Client, work_root: Path, max_session_hours: float = 8.0,
                 attach_policy: att.AttachPolicy | None = None):
        self.client, self.work_root = client, work_root
        self.max_s = max_session_hours * 3600
        self.attach_policy = attach_policy or att.policy(work_root)
        self._heartbeats: dict[str, threading.Event] = {}

    # --- Helpers ------------------------------------------------------------------------------

    def _task(self, task_nr: str) -> dict:
        return self.client.get(f"/tasks/{task_nr.upper()}")

    def _worktree(self, task: dict) -> Path:
        return self.work_root / task["project_info"]["key"] / ".worktrees" / task["task_nr"]

    def _start_heartbeat(self, task_nr: str, lease_minutes: int) -> None:
        if task_nr in self._heartbeats:
            return
        stop = threading.Event()
        self._heartbeats[task_nr] = stop

        def beat() -> None:
            started = time.monotonic()
            waiting = False  # Conscious waiting for a background run keeps the lease alive past the maximum.
            while not stop.wait(max(60.0, lease_minutes * 60 / 3)) and (time.monotonic() - started < self.max_s
                                                                          or waiting):
                try:
                    waiting = bool(self.client.request("POST", f"/tasks/{task_nr}/heartbeat").json().get("waiting"))
                except ApiError as e:
                    if e.status in (403, 404, 409):
                        break
            self._heartbeats.pop(task_nr, None)

        threading.Thread(target=beat, daemon=True).start()

    def _stop_heartbeat(self, task_nr: str) -> None:
        stop = self._heartbeats.pop(task_nr, None)
        if stop:
            stop.set()

    @staticmethod
    def _fmt(t: dict) -> str:
        return f"{t['task_nr']} [{t['status']}] P{t['prio']} – {t['title']}"

    # --- Tools -------------------------------------------------------------------------------

    def list_tasks(self) -> str:
        items = self.client.get("/tasks", params={"assigned_to": "me", "limit": 200})["items"]
        groups = {_("In progress (continue with start_task)"): ["in_progress"],
                  _("Ready to claim (start_task)"): ["defined", "changes_requested"],
                  _("Waiting for humans"): ["questions", "for_review", "aborted"]}
        lines = []
        for label, statuses in groups.items():
            found = [self._fmt(t) for t in items if t["status"] in statuses]
            if found:
                lines += [f"{label}:", *[f"  - {x}" for x in found]]
        try:  # Reviewer agents: tasks waiting for their review (older servers: none).
            jobs = self.client.get("/agents/me/reviews").get("items") or []
        except ApiError:
            jobs = []
        if jobs:
            lines += [_("Review jobs (start_task, then review_task):"),
                      *[f"  - {self._fmt(t)}" + (" " + _("(claimed)") if t.get("review_claimed") else "")
                        for t in jobs]]
        text = "\n".join(lines) or _("No tasks for you.")
        check = getattr(self.client, "version_warning", None)  # Set by the first API response (cli/client.py).
        warning = check() if check else None
        return f"{warning}\n\n{text}" if warning else text

    def start_task(self, task_nr: str | None = None) -> str:
        try:
            result = prepare(self.client, self.work_root, task_nr.upper() if task_nr else None)
        except (PrepareError, ApiError) as e:
            return _("Could not start the task: %(error)s", error=e)
        if result is None:
            return parallel_limit_note(self.client) or _("No task ready to claim for you (list_tasks shows what is "
                                                          "waiting).")
        task, wt = result.task, result.worktree
        self._start_heartbeat(task["task_nr"], self.client.get("/agents/me").get("lease_minutes") or 120)
        relative = wt.relative_to(self.work_root)
        if result.review:
            return (_("# Review job %(nr)s started\n\n"
                      "Working folder (read only): %(path)s\n"
                      "(relative to the shared folder \"%(shared)s\": %(relative)s)\n\n"
                      "Do not edit anything. Finish with review_task(task_nr, decision, comment): approve, reject, "
                      "conflict or release.\n\n", nr=task["task_nr"], path=wt, shared=self.work_root.name,
                      relative=relative) + (wt / "REVIEW.md").read_text())
        from cli.tm import task_markdown

        return (_("# Task %(nr)s started\n\n"
                  "Working folder: %(path)s\n"
                  "(relative to the shared folder \"%(shared)s\": %(relative)s)\n\n"
                  "Only edit files in this folder, without git. Finish with submit_task, ask or abort_task.\n\n",
                  nr=task["task_nr"], path=wt, shared=self.work_root.name, relative=relative)
                + task_markdown(task) + result.note)

    def review_task(self, task_nr: str, decision: str, comment: str | None = None) -> str:
        try:
            t = review_task(self.client, task_nr, decision, comment)
        except ValueError as e:
            return _("Failed: %(error)s", error=e)
        except ApiError as e:
            return _("Failed: %(error)s", error=explain(e, "review_task"))
        self._stop_heartbeat(t["task_nr"])
        if decision.strip().lower() == "release":
            return _("%(nr)s: review job given back", nr=t["task_nr"])
        return _("%(nr)s: status %(status)s.", nr=t["task_nr"], status=t["status"])

    def show_task(self, task_nr: str) -> str:
        from cli.tm import task_markdown

        return task_markdown(self._task(task_nr))

    def show_changes(self, task_nr: str) -> str:
        task = self._task(task_nr)
        wt = self._worktree(task)
        if not wt.is_dir():
            return _("No working folder for %(nr)s – run start_task first.", nr=task["task_nr"])
        base = task["project_info"].get("default_branch") or "main"
        status = _git("status", "--short", cwd=wt) or _("(no uncommitted changes)")
        commits = _git("log", "--oneline", f"origin/{base}..HEAD", cwd=wt) or _("(none)")
        diff = _git("diff", "HEAD", cwd=wt)
        if len(diff) > 12000:
            diff = diff[:12000] + "\n" + _("… (truncated)")
        return _("Uncommitted changes:\n%(status)s\n\nCommits on the task branch:\n%(commits)s\n\nDiff:\n%(diff)s",
                 status=status, commits=commits, diff=diff or _("(empty)"))

    def submit_task(self, task_nr: str, summary: str, commit_message: str,
                    open_points: list[str] | None = None, outputs: dict | None = None,
                    usage: dict | None = None) -> str:
        task = self._task(task_nr)
        wt = self._worktree(task)
        if not wt.is_dir():
            return _("No working folder for %(nr)s – run start_task first.", nr=task["task_nr"])
        if not summary.strip() or not commit_message.strip():
            return _("summary and commit_message are required.")
        base = task["project_info"].get("default_branch") or "main"
        branch = task["branch_name"]
        markers = unresolved_conflicts(wt)
        if markers:
            return _("Not submitted: conflict markers (<<<<<<< / >>>>>>>) are still in %(files)s. Resolve them first – "
                     "keep both changes –, then call submit_task again.", files=", ".join(markers))
        _git("add", "-A", cwd=wt)
        if _git("status", "--porcelain", cwd=wt):
            _git("commit", "--quiet", "-m", commit_message.strip(), cwd=wt)
        if not int(_git("rev-list", "--count", f"origin/{base}..HEAD", cwd=wt) or 0):
            return _("No changes compared to %(base)s – nothing to submit.", base=base)
        _git("push", "--quiet", "-u", "origin", f"HEAD:{branch}", cwd=wt)
        sha = _git("rev-parse", "HEAD", cwd=wt)
        payload = {"branch_name": branch, "commit_sha": sha, "summary": summary.strip(),
                   "open_points": [p for p in open_points or [] if p.strip()]}
        if outputs is not None:
            payload["outputs"] = outputs
        if usage is not None:
            payload["usage"] = usage
        try:
            r = self.client.request("POST", f"/tasks/{task['task_nr']}/submit", json=payload)
        except ApiError as e:
            return _("Push ok, submitting failed: %(error)s", error=e)
        self._stop_heartbeat(task["task_nr"])
        t = r.json()
        return _("%(nr)s submitted – status %(status)s, pull request: %(url)s", nr=t["task_nr"], status=t["status"],
                 url=t.get("pr_url"))

    def _act(self, task_nr: str, action: str, body: dict, done: str) -> str:
        try:
            t = self.client.request("POST", f"/tasks/{task_nr.upper()}/{action}", json=body).json()
        except ApiError as e:
            return _("Failed: %(error)s", error=e)
        if action in ("ask", "abort", "release"):
            self._stop_heartbeat(t["task_nr"])
        return _("%(nr)s: %(done)s (status %(status)s).", nr=t["task_nr"], done=done, status=t["status"])

    def ask(self, task_nr: str, question: str) -> str:
        return self._act(task_nr, "ask", {"body": question}, _("question asked – stop here"))

    def abort_task(self, task_nr: str, reason: str) -> str:
        return self._act(task_nr, "abort", {"reason": reason}, _("aborted, waiting for reassignment"))

    def release_task(self, task_nr: str, reason: str) -> str:
        return self._act(task_nr, "release", {"reason": reason}, _("given back"))

    def list_projects(self) -> str:
        items = [p for p in self.client.get("/projects") if p.get("status", "active") == "active"]
        if not items:
            return _("No active projects.")
        return (_("Active projects (key – name, owner, privacy). Only create tasks in projects the user wants them "
                  "in – the owner is responsible there:") + "\n" + "\n".join(
                    f"  - {p['key']} – {p['name']} (Owner {p.get('owner') or '–'}, {p.get('privacy')})" for p in items))

    def list_agents(self) -> str:
        actors = self.client.get("/actors")
        agents = [a for a in actors if a["actor_type"] == "agent"]
        humans = [a for a in actors if a["actor_type"] == "human"]
        lines = [_("Agents you may assign (name – display name, runtime, model location, public or owner; local_only "
                   "projects only to agents with a local model):")]
        lines += [f"  - {a['name']} – {a['display_name']}, {a['runtime']}, {_('model')} {a.get('model_location', '?')}"
                  f"{agent_scope(a)}" for a in agents]
        if humans:
            lines += [_("Humans:"), *[f"  - {a['name']} – {a['display_name']}" for a in humans]]
        return "\n".join(lines)

    def create_task(self, project: str, title: str, description: str = "", acceptance_criteria: str = "",
                    assigned_to: str | None = None, prio: int = 3, draft: bool = False,
                    due_date: str | None = None, sprint: str | None = None, tier: str | None = None,
                    model: str | None = None, reviewer: str | None = None,
                    attachments: list[str] | None = None, data_class: str | None = None,
                    depends_on: list[str] | str | None = None) -> str:
        try:
            deps = parse_depends_on(depends_on)
            due = parse_due(due_date)
            model_tier = parse_tier(tier)
            cls = parse_data_class(data_class)
            sprint_id = (resolve_sprint(self.client, project, str(sprint))
                         if sprint is not None and str(sprint).strip() and not is_no_sprint(str(sprint)) else None)
        except (ValueError, ApiError) as e:
            return _("Task not created: %(error)s", error=e)
        body = {"project": project.strip().upper(), "title": title.strip(), "prio": prio, "due_date": due,
                "description": description.strip() or None,
                "acceptance_criteria": acceptance_criteria.strip() or None,
                "assigned_to": (assigned_to or "").strip() or None,
                "status": "draft" if draft else "defined", "sprint": sprint_id, "model_tier": model_tier,
                "model": model_value(model), **({"reviewer": reviewer.strip()} if (reviewer or "").strip() else {}),
                **({"data_class": cls} if cls else {}), **({"depends_on": deps} if deps else {})}
        try:
            t = self.client.request("POST", "/tasks", json=body).json()
        except ApiError as e:
            hint = ""
            if e.status == 403 and body["assigned_to"]:
                hint = "\n" + _("Note: only actors with role \"orchestrator\" or \"admin\" may assign other agents – "
                                 "an admin can change the role under Admin → Users & agents.")
            return _("Task not created: %(error)s", error=e) + hint
        who = t.get("assigned_to") or _("nobody")
        state = (_("draft until sprint '%(sprint)s' is released", sprint=t.get("sprint_name"))
                 if t["status"] == "draft" and t.get("sprint_status") == "planned"
                 else _("draft – a human publishes it") if t["status"] == "draft"
                 else _("defined, ready to claim") if t.get("assigned_to")
                 else _("defined, but not assigned to anyone – will not be claimed"))
        out = _("%(nr)s created: \"%(title)s\" – assigned to %(who)s, %(state)s.", nr=t["task_nr"], title=t["title"],
                who=who, state=state)
        if t.get("reviewer"):
            out += "\n" + _("Reviewer: %(name)s", name=t["reviewer"])
        out += ("\n" + model_line(t).removeprefix("- ")) if model_line(t) else ""
        out += ("\n" + data_class_line(t).removeprefix("- ")) if t.get("data_class") else ""
        out += ("\n" + self._dependencies_line(t)) if t.get("dependencies") else ""
        if attachments and any(str(p).strip() for p in attachments):
            out += "\n" + self._attach_report(t["task_nr"], attachments, created=True)
        return out

    @staticmethod
    def _dependencies_line(t: dict) -> str:
        return _("Depends on: %(deps)s – the agent can claim the task once all are done.",
                 deps=", ".join(f"{d['task_nr']} ({d['status']})" for d in t["dependencies"]))

    # --- Attachments ---------------------------------------------------------------------------

    def _attach_report(self, task_nr: str, paths: list[str], created: bool = False) -> str:
        checked = att.check_paths([str(p) for p in paths if str(p).strip()], self.attach_policy)
        uploaded, failed = [], ""
        if checked.files:
            try:
                uploaded = att.upload_paths(self.client, task_nr, checked.files)
            except (ApiError, OSError) as e:
                failed = str(e)
        lines = []
        if uploaded:
            lines += [_("Attached to %(nr)s:", nr=task_nr.upper()),
                      *[f"  {line}" for line in att.result_lines(uploaded)]]
        if failed:
            lines += [_("Upload failed, nothing attached: %(error)s", error=failed)]
        if checked.errors:
            lines += [_("Refused (not attached):"), *[f"  - {e}" for e in checked.errors]]
        if (failed or checked.errors) and created:
            lines += [_("The task %(nr)s exists anyway – attach the files later with attach_files or attach_text.",
                        nr=task_nr.upper())]
        return "\n".join(lines) or _("No files given.")

    def attach_files(self, task_nr: str, paths: list[str]) -> str:
        if not paths or not any(str(p).strip() for p in paths):
            return _("No files given.")
        return self._attach_report(task_nr, paths)

    def attach_text(self, task_nr: str, filename: str, content: str) -> str:
        try:
            name = att.text_filename(filename)
        except att.AttachError as e:
            return _("Not attached: %(error)s", error=e)
        data = (content or "").encode("utf-8")
        if not data:
            return _("Not attached: %(error)s", error=_("content is empty"))
        if len(data) > att.MAX_TEXT_BYTES:
            return _("Not attached: %(error)s", error=_("text larger than 1 MB – put the file into an allowed folder "
                                                         "and use attach_files"))
        try:
            uploaded = att.upload(self.client, task_nr, [(name, data)])
        except ApiError as e:
            return _("Not attached: %(error)s", error=e)
        return "\n".join([_("Attached to %(nr)s:", nr=task_nr.upper()),
                          *[f"  {line}" for line in att.result_lines(uploaded)]])

    def update_task(self, task_nr: str, title: str | None = None, description: str | None = None,
                    acceptance_criteria: str | None = None, assigned_to: str | None = None, prio: int | None = None,
                    due_date: str | None = None, sprint: str | None = None, tier: str | None = None,
                    model: str | None = None, reviewer: str | None = None, data_class: str | None = None,
                    depends_on: list[str] | str | None = None) -> str:
        changes = {k: v for k, v in (("title", title), ("description", description),
                                     ("acceptance_criteria", acceptance_criteria), ("prio", prio)) if v is not None}
        if depends_on is not None:
            try:
                changes["depends_on"] = parse_depends_on(depends_on)
            except ValueError as e:
                return _("Not changed: %(error)s", error=e)
        if tier is not None:
            try:
                changes["model_tier"] = parse_tier(tier)
            except ValueError as e:
                return _("Not changed: %(error)s", error=e)
        if model is not None:
            changes["model"] = model_value(model)
        try:
            if parse_data_class(data_class):
                changes["data_class"] = parse_data_class(data_class)
        except ValueError as e:
            return _("Not changed: %(error)s", error=e)
        if assigned_to is not None:
            changes["assigned_to"] = None if assigned_to.strip().lower() in ("none", "-", "") else assigned_to.strip()
        if reviewer is not None:
            changes["reviewer"] = None if reviewer.strip().lower() in ("none", "-", "") else reviewer.strip()
        if due_date is not None:
            try:
                changes["due_date"] = parse_due(due_date)
            except ValueError as e:
                return _("Not changed: %(error)s", error=e)
        if sprint is not None:
            try:
                changes["sprint"] = None if is_no_sprint(str(sprint)) or not str(sprint).strip() else resolve_sprint(
                    self.client, None if str(sprint).strip().isdigit() else self._task(task_nr)["project"], str(sprint))
            except (ValueError, ApiError) as e:
                return _("Not changed: %(error)s", error=e)
        if not changes:
            return _("Nothing to change – give at least one field.")
        try:
            t = patch_task(self.client, task_nr.upper(), changes)
        except ApiError as e:
            return _("Not changed: %(error)s", error=explain(e, "update_task"))
        return _("%(nr)s changed (%(fields)s): assigned to %(who)s, P%(prio)s, due %(due)s, sprint %(sprint)s, "
                 "status %(status)s.", nr=t["task_nr"], fields=", ".join(changes),
                 who=t.get("assigned_to") or _("nobody"), prio=t["prio"], due=t.get("due_date") or "–",
                 sprint=t.get("sprint_name") or "–", status=t["status"]) + (
            "\n" + _("Reviewer: %(name)s", name=t["reviewer"]) if "reviewer" in changes and t.get("reviewer") else "") + (
            "\n" + model_line(t).removeprefix("- ") if model_line(t) else "") + (
            "\n" + data_class_line(t).removeprefix("- ") if "data_class" in changes and data_class_line(t) else "") + (
            "\n" + (self._dependencies_line(t) if t.get("dependencies") else _("Dependencies: none."))
            if "depends_on" in changes else "")

    def _transition(self, task_nr: str, to: str, reason: str | None, tool: str) -> str:
        try:
            t = transition(self.client, task_nr.upper(), to, reason)
        except ApiError as e:
            return _("Failed: %(error)s", error=explain(e, tool))
        self._stop_heartbeat(t["task_nr"])
        return _("%(nr)s: status %(status)s.", nr=t["task_nr"], status=t["status"])

    def duplicate_task(self, task_nr: str, assign: str | None = None, title: str | None = None) -> str:
        body = {k: v.strip() for k, v in (("assign", assign), ("title", title)) if v and v.strip()}
        try:
            t = duplicate_task(self.client, task_nr, body)
        except ApiError as e:
            return _("Failed: %(error)s", error=explain(e, "duplicate_task"))
        return _("%(nr)s created as a draft from %(origin)s: \"%(title)s\", assigned to %(who)s. Publish it with "
                 "reset_task once it is ready.", nr=t["task_nr"], origin=t.get("duplicate_of") or task_nr.upper(),
                 title=t["title"], who=t.get("assigned_to") or _("nobody"))

    def cancel_task(self, task_nr: str, reason: str) -> str:
        if not reason.strip():
            return _("reason is required (goes into the thread).")
        return self._transition(task_nr, "cancelled", reason, "cancel_task")

    def reset_task(self, task_nr: str, reason: str | None = None) -> str:
        return self._transition(task_nr, "defined", reason, "reset_task")

    # --- Sprints -------------------------------------------------------------------------------

    def _sprint_path(self, project: str, sprint: str) -> str:
        return f"/projects/{project.strip().upper()}/sprints/{resolve_sprint(self.client, project, str(sprint))}"

    def list_sprints(self, project: str, status: str | None = None) -> str:
        params = {"status": status} if status and status.strip() else None
        try:
            items = self.client.get(f"/projects/{project.strip().upper()}/sprints", params=params)
        except ApiError as e:
            return _("Failed: %(error)s", error=e)
        if not items:
            return _("Project %(project)s has no sprints.", project=project.strip().upper())
        lines = [_("Sprints of %(project)s (id name [status] dates · progress):", project=project.strip().upper())]
        for sp in items:
            lines.append(f"  - {sprint_line(sp)}")
            if sp.get("goal"):
                lines.append("    " + _("Goal: %(goal)s", goal=sp["goal"].strip().replace("\n", " ")))
        return "\n".join(lines)

    def show_sprint(self, project: str, sprint: str) -> str:
        try:
            sp = self.client.get(self._sprint_path(project, sprint))
        except (ValueError, ApiError) as e:
            return _("Failed: %(error)s", error=e)
        lines = [sprint_line(sp), _("Goal: %(goal)s", goal=sp.get("goal") or "–"), ""]
        lines += [f"  - {self._fmt(t)} ({t.get('assigned_to') or _('nobody')})" for t in sp["tasks"]]
        return "\n".join(lines).rstrip()

    def create_sprint(self, project: str, name: str, goal: str = "", start_date: str | None = None,
                      end_date: str | None = None) -> str:
        try:
            start, end = parse_due(start_date), parse_due(end_date)
        except ValueError as e:
            return _("Sprint not created: %(error)s", error=e)
        body = {"name": name.strip(), "goal": goal.strip() or None, "start_date": start, "end_date": end}
        try:
            sp = self.client.request("POST", f"/projects/{project.strip().upper()}/sprints", json=body).json()
        except ApiError as e:
            return _("Sprint not created: %(error)s", error=explain(e, "create_sprint"))
        return _("Sprint '%(name)s' created (id %(id)s, %(status)s). Tasks in it stay drafts until release_sprint.",
                 name=sp["name"], id=sp["id"], status=sp["status"])

    def update_sprint(self, project: str, sprint: str, name: str | None = None, goal: str | None = None,
                      start_date: str | None = None, end_date: str | None = None) -> str:
        """PATCH name, goal and dates. Omitted arguments stay unchanged; 'none' clears a date; '' clears the goal."""
        body: dict = {}
        if name is not None:
            body["name"] = name
        if goal is not None:
            body["goal"] = goal
        try:
            if start_date is not None:
                body["start_date"] = parse_due(start_date)
            if end_date is not None:
                body["end_date"] = parse_due(end_date)
        except ValueError as e:
            return _("Sprint not updated: %(error)s", error=e)
        if not body:
            return _("Nothing to change – give name, goal, start_date or end_date.")
        try:
            path = self._sprint_path(project, sprint)
            self.client.request("PATCH", path, json=body)
            sp = self.client.get(path)
        except (ValueError, ApiError) as e:
            return _("Failed: %(error)s", error=explain(e, "update_sprint") if isinstance(e, ApiError) else e)
        lines = [sprint_line(sp), _("Goal: %(goal)s", goal=sp.get("goal") or "–"), ""]
        lines += [f"  - {self._fmt(t)} ({t.get('assigned_to') or _('nobody')})" for t in sp["tasks"]]
        return "\n".join(lines).rstrip()

    def add_sprint_tasks(self, project: str, sprint: str, lines: str) -> str:
        entries, errors = parse_batch(lines)
        if errors or not entries:
            return _("Nothing created – fix the lines first (format: %(format)s):", format=BATCH_FORMAT) + "\n" + \
                "\n".join(errors or [_("No tasks given")])
        try:
            r = self.client.request("POST", f"{self._sprint_path(project, sprint)}/tasks",
                                    json={"tasks": entries}).json()
        except (ValueError, ApiError) as e:
            return _("Nothing created: %(error)s", error=explain(e, "add_sprint_tasks") if isinstance(e, ApiError)
                     else e)
        return (_("%(num)s draft(s) created in sprint '%(sprint)s' – complete each one with update_task (description, "
                  "acceptance_criteria), then release_sprint:", num=len(r["created"]), sprint=r["sprint"]["name"])
                + "\n" + "\n".join(f"  - {self._fmt(t)}" for t in r["created"]))

    def release_sprint(self, project: str, sprint: str) -> str:
        try:
            r = self.client.request("POST", f"{self._sprint_path(project, sprint)}/release").json()
        except (ValueError, ApiError) as e:
            return _("Failed: %(error)s", error=explain(e, "release_sprint") if isinstance(e, ApiError) else e)
        return release_report(r)

    def close_sprint(self, project: str, sprint: str, move_to: str | None = None, detach: bool = False) -> str:
        if move_to and detach:
            return _("Give either move_to or detach, not both.")
        try:
            body = {"move_to": resolve_sprint(self.client, project, str(move_to))} if move_to else {"detach": detach}
            r = self.client.request("POST", f"{self._sprint_path(project, sprint)}/close", json=body).json()
        except (ValueError, ApiError) as e:
            return _("Failed: %(error)s", error=explain(e, "close_sprint") if isinstance(e, ApiError) else e)
        return close_report(r, move_to)

    def reopen_sprint(self, project: str, sprint: str, restore_tasks: bool = True) -> str:
        try:
            r = self.client.request("POST", f"{self._sprint_path(project, sprint)}/reopen",
                                    json={"restore_tasks": restore_tasks}).json()
        except (ValueError, ApiError) as e:
            return _("Failed: %(error)s", error=explain(e, "reopen_sprint") if isinstance(e, ApiError) else e)
        return reopen_report(r)

    def report_waiting(self, task_nr: str, reason: str | None = None, duration: str | None = None,
                       watch_file: str | None = None, done: bool = False, usage: dict | None = None) -> str:
        try:
            return waiting_report(heartbeat_task(self.client, task_nr, reason, duration, watch_file, done, usage))
        except ValueError as e:
            return _("Failed: %(error)s", error=e)
        except ApiError as e:
            return _("Failed: %(error)s", error=explain(e, "report_waiting"))

    def comment(self, task_nr: str, text: str) -> str:
        try:
            self.client.request("POST", f"/tasks/{task_nr.upper()}/comments", json={"type": "comment", "body": text})
        except ApiError as e:
            return _("Failed: %(error)s", error=e)
        return _("Comment saved.")

    def notify(self, action: str = "status", duration: str | None = None, user: str | None = None) -> str:
        action = action.strip().lower()
        if action not in NOTIFY_ACTIONS:
            return _("Unknown action '%(value)s' – expected %(actions)s.", value=action,
                     actions=", ".join(NOTIFY_ACTIONS))
        try:
            return notify_report(notify_request(self.client, action, duration, user))
        except ApiError as e:
            return _("Failed: %(error)s", error=e)

    def sftp_put(self, task_nr: str, files: list[str], to: str | None = None) -> str:
        """Upload from the task's working folder – runs here on the machine (the Cowork VM cannot reach the NAS)."""
        try:
            task = self._task(task_nr)
            folder = self._worktree(task).resolve()
            paths = []
            for name in files:
                path = (folder / name).resolve()
                if not path.is_relative_to(folder) or not path.exists():
                    return _("%(name)s is not a file in the working folder %(folder)s", name=name, folder=folder)
                paths.append(path)
            done = sftp.upload_for_task(self.client, task["task_nr"], paths, to)
        except (ApiError, sftp.SftpError) as e:
            return _("Failed: %(error)s", error=e)
        return "\n".join([_("Uploaded and noted in %(nr)s:", nr=task["task_nr"]),
                          *[f"  {line}" for line in sftp.upload_lines(done)]])

    def send_email(self, task_nr: str, to: list[str], subject: str, body: str, cc: list[str] | None = None,
                   attachments: list[str] | None = None, on_approve: bool = False) -> str:
        try:
            return email_report(send_email(self.client, task_nr, to, subject, body, cc, attachments, on_approve))
        except ApiError as e:
            return _("Failed: %(error)s", error=e)

    def list_emails(self, task_nr: str) -> str:
        try:
            return emails_report(self.client.get(f"/tasks/{task_nr.strip().upper()}/emails"))
        except ApiError as e:
            return _("Failed: %(error)s", error=e)

    def list_mail_recipients(self, project: str) -> str:
        try:
            return recipients_report(self.client.get(f"/projects/{project.strip().upper()}/mail-recipients"))
        except ApiError as e:
            return _("Failed: %(error)s", error=e)

    def sftp_ls(self, where: str | None = None) -> str:
        try:
            path = sftp.remote_path(self.client, where)
            entries = sftp.ls(sftp.target(self.client), path)
        except (ApiError, sftp.SftpError) as e:
            return _("Failed: %(error)s", error=e)
        return "\n".join([f"{path}:", *[f"  {line}" for line in sftp.listing_lines(entries) or [_("(empty)")]]])


def build_server(app: TaskBrigadeMCP) -> MCPServer:
    server = MCPServer("taskbrigade", instructions=_(INSTRUCTIONS))
    register_workflows(server, app)

    @server.tool(description=_("Shows your TaskBrigade tasks: in progress, ready to claim and waiting. Use this for "
                               "\"Check whether there is a task for me\"."))
    def list_tasks() -> str:
        return app.list_tasks()

    @server.tool(description=_("Claims a task and prepares the working folder (for \"Work on task XYZ\" with "
                               "task_nr, otherwise the next one). If the task is already in progress for you, it is "
                               "resumed. Returns the task, thread and working folder."))
    def start_task(task_nr: str | None = None) -> str:
        return app.start_task(task_nr)

    @server.tool(description=_("Review decision for a task you are entered for as reviewer (or as admin/project "
                               "owner): decision approve (merges the pull request), reject (comment required: what "
                               "is missing or wrong), conflict (returns a merge/brain/changelog conflict to the "
                               "assignee) or release (gives your review job back undecided; comment = reason)."))
    def review_task(task_nr: str, decision: str, comment: str | None = None) -> str:
        return app.review_task(task_nr, decision, comment)

    @server.tool(description=_("Shows a task with description, acceptance criteria and the complete thread."))
    def show_task(task_nr: str) -> str:
        return app.show_task(task_nr)

    @server.tool(description=_("Shows your changes so far in the working folder (status, commits, diff) for a "
                               "self-check."))
    def show_changes(task_nr: str) -> str:
        return app.show_changes(task_nr)

    @server.tool(description=_("Submits the result: commits all changes in the working folder with commit_message, "
                               "pushes the task branch and creates the pull request. summary: what changed, how it "
                               "was checked. open_points (optional): list of points left open, one per entry – they "
                               "are stored at the task and offered for a follow-up task. outputs (optional): declared "
                               "workflow outputs, the same object as tbr submit --output/--output-json. usage "
                               "(optional): token/cost report for workflow budgets, with input_tokens, output_tokens, "
                               "cost_cents and model."))
    def submit_task(task_nr: str, summary: str, commit_message: str, open_points: list[str] | None = None,
                    outputs: dict | None = None, usage: dict | None = None) -> str:
        return app.submit_task(task_nr, summary, commit_message, open_points, outputs, usage)

    @server.tool(description=_("Asks the humans a question (state \"Questions\"). Stop afterwards – the answer "
                               "arrives in the thread."))
    def ask(task_nr: str, question: str) -> str:
        return app.ask(task_nr, question)

    @server.tool(description=_("Aborts because you cannot solve the task; a human reassigns it."))
    def abort_task(task_nr: str, reason: str) -> str:
        return app.abort_task(task_nr, reason)

    @server.tool(description=_("Gives the task back without a result (e.g. a technical problem); it can be claimed "
                               "again."))
    def release_task(task_nr: str, reason: str) -> str:
        return app.release_task(task_nr, reason)

    @server.tool(description=_("Lists the active TaskBrigade projects (key, name, privacy) – for create_task."))
    def list_projects() -> str:
        return app.list_projects()

    @server.tool(description=_("Lists agents and humans a task can be assigned to (with runtime cloud/local)."))
    def list_agents() -> str:
        return app.list_agents()

    @server.tool(description=_("Creates a new task in TaskBrigade when the user asks for it. project: project key "
                               "(list_projects). assigned_to: agent or human (list_agents), empty = nobody. "
                               "prio 1 (high) … 5. Without draft the task is \"Defined\" right away and claimed by "
                               "the agent – description and acceptance_criteria are required for that (Markdown, "
                               "concrete). draft=True creates a draft. due_date: due date YYYY-MM-DD (optional, for "
                               "information only – does not affect claiming). sprint: sprint id or name (list_sprints); in a "
                               "sprint that is not released yet the task is always a draft. tier: model tier simple, "
                               "standard or complex (\"with a simple model\" → simple); model: concrete model from the "
                               "agent's allow list (only when asked for explicitly). Without both the agent uses its "
                               "default model. reviewer: a human or an agent with role reviewer (not the assignee); "
                               "empty = the project's default reviewer, \"none\" = the owner and admins. "
                               "depends_on: list of task numbers (or comma-separated string) this task depends on – "
                               "the agent can claim it only once all of them are done. "
                               "attachments: optional list of file paths on this machine, attached after creating "
                               "(only from the allowed folders: work directory, ~/Downloads, ~/Documents, ~/Desktop "
                               "unless configured otherwise; no hidden files or keys). If attaching fails the task "
                               "still exists and the reply names the error.") + " " + _(DATA_CLASS_HELP))
    def create_task(project: str, title: str, description: str = "", acceptance_criteria: str = "",
                    assigned_to: str | None = None, prio: int = 3, draft: bool = False,
                    due_date: str | None = None, sprint: str | None = None, tier: str | None = None,
                    model: str | None = None, reviewer: str | None = None,
                    attachments: list[str] | None = None, data_class: str | None = None,
                    depends_on: list[str] | str | None = None) -> str:
        return app.create_task(project, title, description, acceptance_criteria, assigned_to, prio, draft, due_date,
                               sprint, tier, model, reviewer, attachments, data_class, depends_on)

    @server.tool(description=_("Attaches files from this machine to a task (stored in the project repo under "
                               "tasks/<task>/). paths: absolute file paths (relative ones count from the work "
                               "directory). Only files in the allowed folders – work directory, ~/Downloads, "
                               "~/Documents, ~/Desktop unless configured otherwise – are read; hidden files and "
                               "folders, keys and secrets, folders, files over the size limit and more than 20 files "
                               "are refused with the reason. Allowed for the task's creator, assignee, the project "
                               "owner, orchestrators and admins."))
    def attach_files(task_nr: str, paths: list[str]) -> str:
        return app.attach_files(task_nr, paths)

    @server.tool(description=_("Attaches a small text file (UTF-8, e.g. Markdown or CSV, at most 1 MB) to a task – for "
                               "content this server cannot read by path, e.g. a file dropped into the chat. filename: "
                               "name of the attachment (path parts are removed)."))
    def attach_text(task_nr: str, filename: str, content: str) -> str:
        return app.attach_text(task_nr, filename, content)

    @server.tool(description=_("Changes an existing task (role orchestrator/admin only). Only the given fields are "
                               "changed. assigned_to: new assignee (\"none\" removes the assignment); due_date "
                               "YYYY-MM-DD (\"none\" removes it); sprint: sprint id or name (\"none\" detaches the "
                               "task); tier: model tier simple, standard or complex (\"none\" removes it); model: "
                               "concrete model from the agent's allow list (\"none\" removes it); reviewer: human or "
                               "reviewer agent (\"none\" removes it – the owner and admins review); depends_on: list of "
                               "task numbers (or comma-separated string) that REPLACES the dependencies (\"none\" "
                               "removes all; omitted = unchanged) – the agent can claim the task only once all of "
                               "them are done.") + " "
                 + _(DATA_CLASS_HELP))
    def update_task(task_nr: str, title: str | None = None, description: str | None = None,
                    acceptance_criteria: str | None = None, assigned_to: str | None = None, prio: int | None = None,
                    due_date: str | None = None, sprint: str | None = None, tier: str | None = None,
                    model: str | None = None, reviewer: str | None = None, data_class: str | None = None,
                    depends_on: list[str] | str | None = None) -> str:
        return app.update_task(task_nr, title, description, acceptance_criteria, assigned_to, prio, due_date, sprint,
                               tier, model, reviewer, data_class, depends_on)

    @server.tool(description=_("Lists the sprints of a project (id, name, status, dates, progress, goal). status: "
                               "optional filter planned, released or closed (comma-separated)."))
    def list_sprints(project: str, status: str | None = None) -> str:
        return app.list_sprints(project, status)

    @server.tool(description=_("Shows a sprint (id or name) with goal and its tasks."))
    def show_sprint(project: str, sprint: str) -> str:
        return app.show_sprint(project, sprint)

    @server.tool(description=_("Creates a sprint in a project (admins, orchestrators, project owner). Tasks in it stay "
                               "drafts until release_sprint. start_date/end_date YYYY-MM-DD (optional)."))
    def create_sprint(project: str, name: str, goal: str = "", start_date: str | None = None,
                      end_date: str | None = None) -> str:
        return app.create_sprint(project, name, goal, start_date, end_date)

    @server.tool(description=_("Creates several tasks in a sprint at once as drafts. lines: one task per line, "
                               "\"Title | Agent | Prio | Reviewer\" (agent, prio and reviewer optional). All or "
                               "nothing. Complete them "
                               "afterwards with update_task (description, acceptance_criteria)."))
    def add_sprint_tasks(project: str, sprint: str, lines: str) -> str:
        return app.add_sprint_tasks(project, sprint, lines)

    @server.tool(description=_("Releases a sprint (admins and the project owner only): drafts with description and "
                               "acceptance criteria become \"Defined\", the others stay drafts and are listed with "
                               "the missing fields. Can be called again after completing them."))
    def release_sprint(project: str, sprint: str) -> str:
        return app.release_sprint(project, sprint)

    @server.tool(description=_("Closes a released sprint (admins and the project owner only). Open tasks move to "
                               "move_to (sprint id or name) or are detached with detach=True; running tasks keep "
                               "working, finished ones stay in the sprint."))
    def close_sprint(project: str, sprint: str, move_to: str | None = None, detach: bool = False) -> str:
        return app.close_sprint(project, sprint, move_to, detach)

    @server.tool(description=_("Edits a sprint's name, goal or dates (admins, orchestrators, project owner). Only the "
                               "given fields change; the status stays as it is. start_date/end_date YYYY-MM-DD, "
                               "\"none\" clears a date; an empty goal clears it. sprint is an id or a name."))
    def update_sprint(project: str, sprint: str, name: str | None = None, goal: str | None = None,
                      start_date: str | None = None, end_date: str | None = None) -> str:
        return app.update_sprint(project, sprint, name, goal, start_date, end_date)

    @server.tool(description=_("Reopens a closed sprint (admins and the project owner only). Status goes back to "
                               "released. restore_tasks (default True) moves the tasks that were moved out when it "
                               "was closed back into it; tasks moved elsewhere later or finished are skipped and "
                               "listed."))
    def reopen_sprint(project: str, sprint: str, restore_tasks: bool = True) -> str:
        return app.reopen_sprint(project, sprint, restore_tasks)

    @server.tool(description=_("Duplicates a task into a new draft of the same project (admins, orchestrators, project "
                               "owner): description, criteria, prio, due date, model, dependencies, parent and an open "
                               "sprint are copied, thread/review/branch are not. assign: optional actor for the draft "
                               "(default: nobody); title: optional, default \"(duplicate) <title>\"."))
    def duplicate_task(task_nr: str, assign: str | None = None, title: str | None = None) -> str:
        return app.duplicate_task(task_nr, assign, title)

    @server.tool(description=_("Cancels a task for good (state \"Cancelled\", role orchestrator/admin only). "
                               "reason goes into the thread."))
    def cancel_task(task_nr: str, reason: str) -> str:
        return app.cancel_task(task_nr, reason)

    @server.tool(description=_("Sets a task back to \"Defined\" (e.g. to publish a draft). Permissions as in the "
                               "web UI."))
    def reset_task(task_nr: str, reason: str | None = None) -> str:
        return app.reset_task(task_nr, reason)

    @server.tool(description=_("Report conscious waiting for a long background run (log file written by the run): "
                               "no stall report meanwhile. reason: what you wait for; duration e.g. 2h (default 2h, "
                               "max. 24h) – call again before it ends to extend; watch_file: the run's log (a change "
                               "counts as activity, no change for 30 min is reported). done=true ends the waiting "
                               "state. Also extends the lease."))
    def report_waiting(task_nr: str, reason: str | None = None, duration: str | None = None,
                       watch_file: str | None = None, done: bool = False, usage: dict | None = None) -> str:
        return app.report_waiting(task_nr, reason, duration, watch_file, done, usage)

    @server.tool(description=_("Writes a comment into the task's thread (e.g. current state)."))
    def comment(task_nr: str, text: str) -> str:
        return app.comment(task_nr, text)

    @server.tool(description=_("Telegram notifications: action status | pause | resume | reviews_off | reviews_on. "
                               "duration for pause/reviews_off: 30m, 2h, 1d, 1w or a clock time like 18:00 (empty = "
                               "until switched back). user: another human linked with Telegram (orchestrators and "
                               "admins only; the person is told in Telegram); empty = yourself (humans only)."))
    def notify(action: str = "status", duration: str | None = None, user: str | None = None) -> str:
        return app.notify(action, duration, user)

    @server.tool(description=_("Uploads files or folders from the task's working folder to the team's SFTP storage "
                               "(task folder on the server; to: optional sub-path inside it). Only when the task asks "
                               "for it. Every upload is noted in the task with size and SHA-256."))
    def sftp_put(task_nr: str, files: list[str], to: str | None = None) -> str:
        return app.sftp_put(task_nr, files, to)

    @server.tool(description=_("Lists a folder of the SFTP storage: a task number (its folder), a path inside the base "
                               "folder, or empty (the base folder)."))
    def sftp_ls(where: str | None = None) -> str:
        return app.sftp_ls(where)

    @server.tool(description=_("Sends a work result by email through the server – only when the task asks for it. "
                               "to/cc: addresses on the project's recipient list (list_mail_recipients); body: "
                               "Markdown; attachments: attachment:<id> (task attachment), branch:<path> (file on "
                               "the task branch, pushed), merge:<path> (merge commit). Usually a human approves "
                               "before it is sent; on_approve=true sends it when the task is approved. You never "
                               "get SMTP credentials. result:<id> attaches a released output-gate aggregate. "
                               "Local-only mail always needs separate human mail approval; sealed mail is refused."))
    def send_email(task_nr: str, to: list[str], subject: str, body: str, cc: list[str] | None = None,
                   attachments: list[str] | None = None, on_approve: bool = False) -> str:
        return app.send_email(task_nr, to, subject, body, cc, attachments, on_approve)

    @server.tool(description=_("Lists your email requests of a task with status (waiting for approval, queued, sent, "
                               "failed, rejected), Message-ID and SMTP error."))
    def list_emails(task_nr: str) -> str:
        return app.list_emails(task_nr)

    @server.tool(description=_("Lists the addresses a project's results may be sent to and whether they need "
                               "approval."))
    def list_mail_recipients(project: str) -> str:
        return app.list_mail_recipients(project)

    return server
