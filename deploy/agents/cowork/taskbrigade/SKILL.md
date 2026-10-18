---
name: taskbrigade
description: "Claim and work on TaskBrigade tasks (Claude desktop agent on the Mac, e.g. claude-desktop). Use for: \"Check whether there is a TaskBrigade task for you\", \"Work on task DEMO-0003\", \"Continue task …\", questions about your own TaskBrigade tasks, \"Create a TaskBrigade task for codex …\"."
---

# TaskBrigade (Claude desktop agents on the Mac)

TaskBrigade is your team's central task manager (`https://taskbrigade.example.ts.net`). You work there as an
agent – with its own name per Mac, e.g. **claude-desktop** (the MCP server knows the
identity; `<agent>` below stands for this name) – manually: the user tells you when to check or which task to work on.

## Access – check first

**Route A (preferred): MCP server `taskbrigade`.** If tools such as `list_tasks`, `start_task`, `submit_task`
are available, use only them. The server runs on the Mac and does everything that needs the network
(TaskBrigade, Gitea, commit, push, pull request) – that is not possible from inside Cowork otherwise.

**Route B (fallback): `tbr` on the Mac.** Only if the MCP tools are missing and you can run commands directly **on the
Mac** (not in the Cowork VM). Work directory `~/taskbrigade-work/<agent>` – the identity lives there.
First check `ls ~/taskbrigade-work`, then `cd ~/taskbrigade-work/<agent> && tbr whoami`. If that fails with a connection
error, the command runs in the VM – then stop and tell the user that the MCP server has to be set up
(instructions in `docs/desktop-apps-mcp.md` in the TaskBrigade repo).
`tbr` was called `tm` before TaskBrigade 0.5; on a machine where `tbr` is not found, use `tm` with the same arguments.

If neither route is available: do not improvise (no curl, no searching for tokens), tell the user.

## Flow

1. **Check:** Route A `list_tasks` · Route B `tbr list`
2. **Start a task:** "Work on task XYZ" → Route A `start_task(task_nr="XYZ")` · Route B
   `cd ~/taskbrigade-work/<agent> && tbr prepare --task XYZ` (without `--task`: next claimable task).
   A task that is already in progress for you is resumed. With Route B the instructions in
   `TASK.md` in the working folder apply afterwards (run git and `tbr submit` yourself).
3. **Read the context:** the answer contains the description, acceptance criteria and the **complete thread** – earlier
   questions, the humans' answers, intermediate states. In the working folder read `brain.md` and `AGENTS.md` first.
   In Cowork the working folder is in the shared folder `taskbrigade-work`/`<agent>` under the
   **relative path** that `start_task` gives (the absolute Mac path does not apply in the VM).
4. **Work:** only edit files in the working folder. With Route A **no git commands** – `submit_task` does
   that. Check your own changes with `show_changes` before submitting.
5. **Exactly one closing step:**
   - **Done:** Route A `submit_task(task_nr, summary, commit_message, open_points)` – summary: what changed, how it
     was checked; `open_points`: list of points that remain open, one short sentence each (leave it out if nothing
     is left) – humans take them over into a follow-up task. Then give the user the pull request link.
   - **Unclear:** `ask(task_nr, question)` – a specific question, then stop. The answer arrives in the thread.
   - **Not solvable for you:** `abort_task(task_nr, reason)` – a human reassigns the task.
   - **Interruption** (the conversation ends, the work is not finished): write the current state into the thread with
     `comment(task_nr, text)` (what is done, where it is, next steps). The task stays in progress for you; "Continue
     task …" picks it up there later.

## Review jobs (agents with role `reviewer`)

If you are entered as reviewer of a task, `list_tasks` shows it under "Review jobs" and `start_task` claims it: the
answer contains `REVIEW.md` (task, criteria, thread, the assignee's summary and open points, changed files, pull
request) and a **read-only** working folder with the task branch. Check the changes against the description and the
acceptance criteria – do not edit anything – and decide with exactly one call: `review_task(task_nr, "approve",
comment)` (merges), `review_task(task_nr, "reject", comment)` (comment required: what is missing or wrong),
`review_task(task_nr, "conflict")` (returns a merge/brain/changelog conflict) or `review_task(task_nr, "release",
reason)` (give the job back undecided). Route B: `tbr review <NR> approve|reject|conflict|release`. You can only
decide tasks you are entered for; the project owner and admins can always overrule you.

## Creating new tasks (e.g. "Create a task for codex …")

1. Clarify project and assignee: `list_projects` (keys) and `list_agents` (names, runtime).
   Assign projects with privacy `local_only` only to agents with runtime `local`.
2. Write the description and acceptance criteria (Markdown) so concretely that the agent can work without asking
   back: goal, context, expected result (file/location), checks. If something essential is missing, ask the user first.
3. `create_task(project, title, description, acceptance_criteria, assigned_to, prio, due_date)` – the task is then
   "Defined" and is claimed by the agent. Only an idea or incomplete: `draft=True` (draft, a human publishes it).
   `due_date` (YYYY-MM-DD) only if the user names a deadline – it plays no role in claiming. `reviewer`: a human or
   an agent with role `reviewer` who approves the result (never the assignee); without it the project's default
   reviewer applies, `"none"` leaves the review to the owner and admins.
   Files the user wants attached: `attachments=[…]` with their paths on the Mac (or `attach_files(task_nr, paths)`
   later). Only the work folder, `~/Downloads`, `~/Documents` and `~/Desktop` are allowed (unless configured
   otherwise); hidden files and keys are refused. A file dropped into this chat lives in your VM and cannot be read
   by path – ask the user to put it into a shared allowed folder, or attach text content with
   `attach_text(task_nr, filename, content)` (max. 1 MB). If attaching fails, the task still exists – tell the user.
4. Give the user the task number. Without `assigned_to` nobody claims the task – point that out.
5. If the tool reports that the role is missing: an admin has to give your agent the role `orchestrator` in TaskBrigade.
6. Existing tasks (role `orchestrator`): `update_task(task_nr, …)` changes due date, assignee, prio, title,
   description, criteria (wrongly assigned → move it instead of creating a new one); `cancel_task(task_nr, reason)`
   cancels, `reset_task(task_nr)` sets it back to "Defined" (e.g. publish a draft). `duplicate_task(task_nr, assign,
   title)` copies a task into a new unassigned draft ("(duplicate) <title>"), e.g. for a second opinion from another
   agent. Route B: `tbr update`/`tbr cancel`/`tbr reset`/`tbr duplicate`.
   Dependencies: `create_task(…, depends_on=[…])` / `update_task(task_nr, depends_on=[…])` (list or comma-separated;
   `"none"` clears on update; the agent can claim the task only once all are done; Route B: `--depends-on`).

## Sprints (optional: project → sprint → task)

- `list_sprints(project)` / `show_sprint(project, sprint)` – sprints with status, goal and tasks; `sprint` is the id
  or the name.
- Planning (admins, orchestrators, project owner): `create_sprint(project, name, goal, start_date, end_date)` and
  `update_sprint(project, sprint, name, goal, start_date, end_date)` (only the given fields; `"none"` clears a date,
  an empty goal clears it; the status stays unchanged);
  several tasks at once with `add_sprint_tasks(project, sprint, lines)` – one task per line,
  `Title | Agent | Prio | Reviewer` (agent, prio and reviewer optional), created as drafts; then complete each one with
  `update_task(task_nr, description=…, acceptance_criteria=…)`. `create_task(…, sprint=…)` and
  `update_task(task_nr, sprint=…)` (`"none"` detaches) assign single tasks.
- Tasks in a sprint that is not released yet are always drafts. `release_sprint(project, sprint)` sets every draft
  with description and acceptance criteria to "Defined" and lists the rest with the missing fields – tell the user.
  `close_sprint(project, sprint, move_to=… | detach=True)` moves or detaches open tasks; running tasks keep going.
  `reopen_sprint(project, sprint, restore_tasks=True)` reopens a closed sprint; with `restore_tasks` (default) the
  tasks moved out at close come back, `False` leaves them. release, close and reopen only for admins and the project
  owner – if the tool reports 403, tell the user instead of retrying.
- Released sprint tasks are worked on in parallel; order only through prio and dependencies.
- If your own task belongs to a sprint, `start_task` shows the sprint and its goal – context for your work.
  Route B: `tbr sprint list|show|create|update|add-tasks|release|close|reopen`, `--sprint` on `tbr create`/`tbr update`/`tbr list`.

## Rules

- Never work on or push to `main` directly; results only go through `submit_task` (pull request).
- New project findings do **not** go directly into `brain.md` but into `brain-changes/<TASK-NR>.md` in the working
  folder (`## add: <section>` appends; `## change:`/`## remove:` with the exact old text; format in
  `brain-changes/README.md`). TaskBrigade applies them on approval.
- A `CHANGELOG.md` entry (if the repo has one) goes into `changelog.d/<TASK-NR>.md` (bullet points only);
  TaskBrigade adds it under „Unreleased“ on approval.
- No tokens, passwords or credentials in files, comments or answers.
- A task's attachments are in the working folder under `tasks/<TASK-NR>/`.
- SFTP storage (if enabled): put results on the team's SFTP server only when the task asks for it – MCP
  `sftp_put(task_nr, files)` uploads files from the working folder (the MCP server on the Mac does the transfer; the
  VM cannot reach the server itself), `sftp_ls` lists folders. Route B: `tbr sftp put <NR> <files…>`.
- You do not get projects for "local agents only" – you run via Anthropic (cloud).
