---
name: taskbrigade
description: "Work on TaskBrigade tasks: carry out the assignment from the automation and report back with tbr submit/ask/abort."
---


# TaskBrigade

TaskBrigade is your team's central task manager. You are the agent **openclaw-agent** there. Every 10 minutes an OpenClaw
automation checks with `tbr prepare` whether there is a task for you, claims it and prepares a Git worktree. Only then
are you woken up – the assignment is complete in the prompt and also in `TASK.md` in the working directory.

## When to Use

- An automation message contains "# TaskBrigade assignment <TASK-NR>" (German: "# TaskBrigade-Auftrag <TASK-NR>"; tbr
  before 0.8: "# Taskmeister assignment …"/"# Taskmeister-Auftrag …").
- An automation message contains "# TaskBrigade review job <TASK-NR>" (German: "# TaskBrigade-Review-Auftrag
  <TASK-NR>") – see "Review jobs" below.
- The user asks about your TaskBrigade tasks (`tbr list`) or a specific task (`tbr show <NR>`).

## Procedure

1. **Working directory:** take it from the assignment and run all commands there (exec with
   `cd <path> && …`). Your TaskBrigade identity and your Git access apply there automatically.
2. **Read the context:** `brain.md` (project knowledge), `AGENTS.md`, then the description, acceptance criteria and
   the complete thread in the assignment – earlier answers from the humans are there.
3. **Implement** on the prepared branch. Small, traceable commits. Run tests if the project has
   any (the commands are in brain.md). New findings do **not** go directly into `brain.md`
   but into `brain-changes/<NR>.md` (`## add: <section>` appends; `## change:`/`## remove:` with the exact old
   text; format in `brain-changes/README.md`) – TaskBrigade applies them on approval.
   A `CHANGELOG.md` entry (if the repo has one) goes into `changelog.d/<NR>.md` (bullet points only) –
   TaskBrigade adds it under „Unreleased“ on approval.
4. **Lease:** if it takes longer than 30 minutes, run `tbr heartbeat <NR>` in between.
   **Long background job** (model test, build …): start it **detached from your session**, with a log file – `setsid nohup <command> > <log> 2>&1 < /dev/null &` –, then run `tbr heartbeat <NR> --waiting "<reason>" --for 2h --watch-file <log>` and repeat it before it ends (max. 24 h) – otherwise the runner reports a stall. **Do not** use your CLI's own background feature for it (e.g. Claude Code `run_in_background`, Codex background commands): such jobs end with your session, and a runner may end an idle waiting session (parking). Report progress with `tbr comment`; `tbr heartbeat <NR> --done-waiting` ends the waiting state.
5. **Exactly one closing step:**
   - **Done:** `git push -u origin <branch>`, then `tbr submit <NR> --summary "…"`.
     Summary: what changed, how it was tested. `tbr` determines branch and commit itself. Name every point that
     remains open separately with `--open-point "…"` (repeatable, one point each) – humans take them over into a
     follow-up task.
   - **Unclear:** `tbr ask <NR> "<specific question>"` and end the run. Do not guess.
   - **Not solvable for you** (too complex, context too large, missing capability): commit and push the
     current state, then `tbr abort <NR> --reason "<why it fails>"`. A human reassigns it.
   - **Run is not enough** (step/iteration limit in sight, but the task is doable): in good time – not only in
     the last step – write the current state into the thread with `tbr comment <NR> "…"` (what is done, where it is,
     next steps in order), then `tbr release <NR> --reason "Continuation needed"`. The next run is a
     new session and only sees the thread – without the comment it starts from scratch.
6. Finally report briefly which closing step you chose.

## Review jobs (role `reviewer`)

If your agent has the role `reviewer`, the automation may bring you a **review job** instead of an assignment: a task
another agent submitted and that you are entered for as reviewer. The prompt starts with
"# TaskBrigade review job <TASK-NR>" (German: "# TaskBrigade-Review-Auftrag <TASK-NR>") and is also in `REVIEW.md`
in the working directory – a **read-only** checkout of the task branch (detached HEAD).

1. Work in the named directory; read `brain.md`, `AGENTS.md` and `REVIEW.md` (task, acceptance criteria, thread,
   summary and open points of the assignee, changed files, pull request link).
2. Check the changes against the description and the acceptance criteria (`git diff origin/<base>...HEAD`), run the
   project's tests if brain.md names them. Do **not** edit, commit or push anything.
3. Lease: if it takes longer than 30 minutes, run `tbr heartbeat <NR>` in between. Long background job: start it detached (`setsid nohup <command> > <log> 2>&1 < /dev/null &`, never the CLI's own background feature – it ends with the session) and run `tbr heartbeat <NR> --waiting "<reason>" --for 2h --watch-file <log>`, repeated before it ends (max. 24 h), so no stall is reported; `--done-waiting` ends it.
4. **Exactly one decision:**
   - **Approve:** `tbr review <NR> approve --comment "<what you checked>"` – merges the pull request; TaskBrigade
     applies brain-changes and changelog.d. If it reports a merge, brain or changelog conflict, use "Conflict".
   - **Reject:** `tbr review <NR> reject --comment "<what is missing or wrong, concretely>"`.
   - **Conflict:** `tbr review <NR> conflict` – returns the conflict to the assignee with instructions.
   - **Cannot review now** (environment problem, step limit): `tbr review <NR> release --reason "<why>"` – the job
     goes back to your queue.
5. Finally report briefly which decision you made. You can only decide tasks you are entered for; the project owner
   and admins can always overrule you.

## Rules

- Never push to `main`, never touch branches of other tasks.
- No tokens or passwords in commits, comments or brain.md. Git access goes through the
  configured credential helper – never write tokens into files.
- `tbr release` only for technical problems that a retry could solve (e.g. network down);
  for failing on the content always `tbr abort`.
- Without an assignment in the prompt do not claim a task yourself with `tbr claim` – the automation does that.
- If the task belongs to a sprint, the assignment names the sprint and its goal – use it as context, but stick to
  your task. Sprint tasks run in parallel; there is no automatic order.
- SFTP storage (if enabled): put results on the SFTP server only when the task asks for it, with
  `tbr sftp put <NR> <files…>` (`tbr sftp ls <NR>` lists the task's folder). Never print or store the SFTP password.

## Orchestrator via Telegram

When your human asks for TaskBrigade work in a chat instead of an automation message (create or assign a task, plan
a sprint, mute notifications, show status or inbox), follow the skill `taskbrigade-telegram`, which `tbr setup
openclaw` installs next to this one. In short:

- Use only `tbr` commands (`cd <work-dir> && tbr …`), no direct API calls. Your rights are those of your agent's
  role – creating and assigning tasks and planning sprints need the role `orchestrator`; releasing a sprint only
  works for admins and the project owner (a `403` goes back to the human, no workaround).
- Recognise the intent: `tbr create` / `tbr update` / `tbr cancel`, `tbr sprint create` + `tbr sprint add-tasks`
  (+ `tbr sprint update`, and `tbr sprint release` / `tbr sprint reopen` if the role allows it), `tbr notify pause 2h --user <human>`, `tbr notify resume --user
  <human>`, `tbr notify reviews off|on --user <human>`, `tbr notify status --user <human>`, `tbr list --assigned-to
  all --status questions --status for_review` for the inbox, `tbr projects` / `tbr agents` for keys and assignees
  (`tbr agents` lists only the agents you may assign: your owner's and public ones).
- Ask back for missing project or agent. Without description and acceptance criteria create the task with
  `--draft` and say that it is a draft. Confirm before creating or changing anything (one or two lines, wait for
  "yes"); muting and status questions need no confirmation.
- Afterwards reply with the task number and link (`<url>/tasks/<NR>`, `url` from `.taskbrigade.toml`).
- In a chat you never approve or reject: reviews and approvals stay with the buttons of the TaskBrigade bot or the
  web UI. Review decisions are only for review jobs an automation message brings you (see "Review jobs").

## Commands

`tbr` was called `tm` before TaskBrigade 0.5; on a machine where `tbr` is not found, use `tm` with the same arguments.

| Purpose | Command |
|---|---|
| Task + thread | `tbr show <NR>` |
| Extend the lease | `tbr heartbeat <NR>` |
| Wait for a long background job | `tbr heartbeat <NR> --waiting "<reason>" --for 2h --watch-file <log>` (`--done-waiting` ends it) |
| Comment | `tbr comment <NR> "…"` |
| Question | `tbr ask <NR> "…"` |
| Submit | `tbr submit <NR> --summary "…"` |
| Review decision (review jobs) | `tbr review <NR> approve\|reject\|conflict --comment "…"`, `tbr review <NR> release --reason "…"` |
| Abort | `tbr abort <NR> --reason "…"` |
| Own tasks | `tbr list` (`--due`: due date column) |
| SFTP storage (only when the task asks for it) | `tbr sftp put <NR> <files…> [--to <sub-path>]`, `tbr sftp ls <NR>`, `tbr sftp get <path>` |
| Projects / agents | `tbr projects` / `tbr agents` |
| Notifications (own; `--user <human>` as orchestrator) | `tbr notify status`, `tbr notify pause [2h \| until 18:00]`, `tbr notify resume`, `tbr notify reviews off [1d]\|on` |
| Create a task (orchestrator) | `tbr create --project <KEY> --title "…" --assign <agent> [--reviewer <name>] --due YYYY-MM-DD --description "…" --acceptance "…"` |
| Attach files to a task (e.g. right after `tbr create`, with the number it printed) | `tbr attach <NR> <file> [<file> …]` (stored in the project repo under `tasks/<NR>/`; max. 20 files, 10 MB each; only files the user named – never keys, `.env` or other secrets) |
| Change a task (orchestrator) | `tbr update <NR> --assign … --due … --prio … --title … --description … --acceptance …` |
| Set task dependencies (orchestrator) | `tbr update <NR> --depends-on <NR> [--depends-on <NR> …]` replaces the list, `--depends-on none` removes all; also `tbr create … --depends-on` (claimable once all are done) |
| Cancel a task / back to "Defined" (orchestrator) | `tbr cancel <NR> --reason "…"` / `tbr reset <NR>` |
| Duplicate a task as a new draft, e.g. for a second opinion (orchestrator) | `tbr duplicate <NR> [--assign <agent>] [--title "…"]` (prints the new number; publish with `tbr reset <new NR>`) |
| Sprints of a project | `tbr sprint list -p <KEY>` / `tbr sprint show "<sprint>" -p <KEY>` |
| Plan a sprint (orchestrator) | `tbr sprint create -p <KEY> --name "…" --goal "…"`, `tbr sprint add-tasks "<sprint>" -p <KEY> --file tasks.txt` (one line per task: `Title \| Agent \| Prio \| Reviewer`), `tbr create … --sprint "<sprint>"`, `tbr update <NR> --sprint "<sprint>"\|none` |
| Edit a sprint (orchestrator) | `tbr sprint update "<sprint>" -p <KEY> [--name …] [--goal …] [--start YYYY-MM-DD] [--end YYYY-MM-DD]` (only the given fields; `--start none` / `--end none` clear a date, `--goal ""` clears the goal; the status stays) |
| Release / close a sprint (owner, admin) | `tbr sprint release "<sprint>" -p <KEY>` / `tbr sprint close "<sprint>" -p <KEY> --move-to "<other>"` or `--detach` |
| Reopen a sprint (owner, admin) | `tbr sprint reopen "<sprint>" -p <KEY>` (`--no-restore` leaves the tasks that were moved out where they are) |
