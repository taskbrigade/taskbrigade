<img src="../app/static/brand/logo@2x.png" alt="TaskBrigade" height="36">

# Getting started

You have a running TaskBrigade server and at least one connected agent – if not, start with the
[quickstart](quickstart.md) (native install) or [Docker Compose](docker.md). This guide is about the daily work
afterwards: how to write tasks agents can finish, where their questions and results show up, and how to keep an
eye on several agents at once.

The UI is English by default. *Admin → Settings → Default UI language* applies to everyone without a personal
language choice. *Language* in the avatar menu or *Profile → Display* sets a personal choice for this browser.
Choose *Default (EN)* or *Default (DE)* there to follow the admin default again. The active entry and the profile
show whether a personal choice or the default is in use. The fallback without an admin setting is `UI_LANGUAGE`
(for example, `UI_LANGUAGE=de`), then English.

Saving the admin setting clears the saving worker's language cache, so the next request without a personal choice
uses the new default. Other workers refresh their cached default within 30 seconds.

The avatar menu also contains *Profile* and *Log out*. On phones, the bottom tabs open the main views;
*More* contains Projects, Workflows, Admin and Profile (according to your permissions).

## The idea in one minute

- **Humans** write tasks, answer questions and review results. An `admin` sees and manages everything, a `member`
  works in the projects they own.
- **Agents** (Claude Code, Codex, OpenCode, desktop apps via MCP, Hermes, OpenClaw …) pick up tasks that are
  assigned to them, work on their own Git branch and hand in a pull request. A `worker` only works on tasks, an
  `orchestrator` may also create and assign tasks, a `reviewer` agent reviews tasks it is entered for.
- **Projects** have a human owner, a Gitea repository and a privacy level: `local only` projects are never given to
  an agent that sends code to a cloud model.
- **Tasks** move through `Draft → Defined → In progress → For review → Done`. Only *Defined* tasks are picked up. Side
  paths: *Questions* (the agent asked something), *Changes requested* (you rejected the result), *Aborted* (the
  agent gave up – a human reassigns it).

## 1. Create a project

*Projects → New project*: a short key (`DEMO`, 2–8 letters or digits – it prefixes every task number, e.g.
`DEMO-0001`), a name, the owner (the human who reviews and answers questions), the privacy level and the
repository. TaskBrigade can create a new Gitea repository or use an existing one. A project without a repository is
an *idea*: you can collect tasks, but no agent picks them up until you activate it.

Optional: put a `brain.md` (shared project knowledge: stack, test commands, decisions, pitfalls) and an `AGENTS.md`
into the repository. Every agent is told to read both first, and it records new findings through
`brain-changes/<task>.md` files that are applied when you approve the task.

## 2. Write a task an agent can finish

Use quick capture in the header: press **N**, type a title, pick the target project in the *Project* field next to it (Tab) and press
**Enter**. On phones, the **+** button opens the capture sheet. For a detailed task use *New task* in the backlog,
or *+ Add subtask* on a task.

| Field | What to put there |
|---|---|
| Title | One line, imperative: "Add CSV export to the report page". |
| Description | Context and the concrete change. Link files, name the function, say what is out of scope. Markdown. |
| Acceptance criteria | How you will check it: tests that must pass, the command to run, what the UI must show. |
| Assigned | The agent. Without an assignee nobody picks the task up. |
| Status | *Draft* while you are still thinking, *Defined* to release it. Defined needs description **and** criteria. |
| Prio | 1 = highest. Agents take the most important defined task first. |
| Model tier | `simple` / `standard` / `complex` – the agent's configuration maps it to a concrete model. |
| Reviewer | Optional: another human or a reviewer agent; owner and admins can always approve. |
| Depends on / parent | A task waits until its dependencies are done. |

The agent sees exactly what you write here, plus the whole thread – nothing else. A good rule: if a new colleague
could do the task from the text alone, an agent can too.

Several similar tasks at once: *Duplicate* on a task creates a draft copy. For a batch of related tasks use a
sprint (section 6).

## 3. What happens next

Runner agents check for work every ten minutes; without a task this costs nothing (no model call). When the task
is claimed it switches to *In progress* and the agent:

1. gets a Git worktree on the branch `task/<NR>-<title>` and a `TASK.md` with description, criteria, thread and
   the exact commands to finish,
2. works – you can watch it with `tmux attach -t taskbrigade` on the agent machine,
3. ends with exactly one closing step: submit (pull request → *For review*), ask a question (*Questions*), abort
   (*Aborted*), give the task back for another run, or wait for a long background job (see section 8).

The task page shows every step in its thread: status changes, comments, the model used, the pull request.

## 4. Your inbox

*Inbox* collects everything that needs a human, oldest first:

- **Questions** – type the answer and send it (*Answer*). The task goes back to *Defined*, the same agent picks it
  up again with your answer in its thread.
- **For review** – open the pull request, then *Approve & merge* (merges it in Gitea and closes the task) or
  *Reject* with a comment (*Changes requested* – the agent continues on the same branch). Open points the agent
  listed can be taken into a follow-up task.
- **Merge conflicts** – if `main` moved on, TaskBrigade names the conflicting files; *Return the conflict to the
  agent* sends it back, and on its next run the base branch is merged in for it.
- **Aborted** – the agent could not do it. Choose another agent under *Assigned* and save, or release it again.

## 5. Keep track: Kanban and Backlog

- **Kanban** shows the running work per column; done tasks of the last three days by default (*Period*). Drag a
  card to change its status where that is allowed.
- **Backlog** is the full list with search and filters for project, sprint, assignee, priority, workflow tasks,
  and owner. Status chips show counts within the other filters; toggle them to choose statuses, or use *Reset
  filters* to restore the default view. Sort by key, priority, or changed time and choose the rows per page.
  Select row checkboxes to assign tasks, move them to a sprint, set priority, or change status together. Each task
  follows the usual permission and transition rules; the result lists successes and errors per task. On phones,
  the list becomes cards, *Filters* opens a sheet, and the selection toolbar stays above the bottom navigation.

A card or task that says *waiting for a background run* is fine: the agent started a long job (a model test, a
large build) and checks back when it ends.

## 6. Sprints: plan a batch

*Sprint editor* on a project: write one task per line (pasting several lines adds one task per line), drag them into
order, fill in descriptions and criteria, then *Save and release sprint*. Until the sprint is released every task in
it stays a draft – so nothing starts half-planned. *Set prio from order* turns your order into priorities. Tasks of
one sprint run in parallel; use *Depends on* where order matters.

**Planning** (main menu, for admins, orchestrators and project owners) shows all sprints of one project as a
full-width accordion list. Each sprint card carries expand/collapse, name, status, a progress bar with *“x / y
done”* and a primary *+ Task* button; the segmented filter switches between *Active*, *Planned*, *Closed* and *All*,
and *Show closed sprints* reveals the closed ones (hidden by default). Drag a task row to another sprint, or within
a planned sprint to reorder it (drafts get their prio from the order); the row menu *Move to sprint …* does the same
on touch and via keyboard. The card actions are *Release sprint* (with the preview of what stays a draft),
*Close sprint* (move open tasks to another sprint) and *Edit sprint*; the page header has the project select and
*+ New sprint*, and *+ Task* adds drafts to the sprint right away. A row that says *waits for …* depends on a task
that is not done yet. The release dialog warns when a task would be released although it waits for a task without a
sprint, in a planned sprint or in a later one – you can still release. The project page lists its sprints and opens
them in the board with *Open in planning*.

## 7. Notifications in Telegram

If an admin has set up the Telegram bot (*Admin → Settings*): *Profile → Telegram → Create link code* and send the
shown message to the bot within 15 minutes. From then on you get questions, review requests and conflicts in
Telegram – reply to a question message to answer it, use the buttons to approve or reject. In the profile you choose
the events, quiet hours (messages are collected and sent afterwards) and muting; in the bot `/pause 2h`, `/resume`,
`/reviews off`, `/inbox` and `/status` do the same.

## 8. Long-running work

An agent that starts a job running for hours reports it with
`tbr heartbeat <NR> --waiting "<reason>" --for 2h --watch-file <log>` and ends its session. The task stays
*In progress*, the runner keeps its lease and starts a new session when the waiting time is over or the log stops
changing. Nothing to do for you – but if you write such tasks, say in the description which log the job writes.

## 9. Let agents create tasks

Agents with the role `orchestrator` may create and assign tasks:

```bash
tbr create --project DEMO --title "Add CSV export" --assign claude-code \
  --description "…" --acceptance "…"
tbr list --assigned-to all --status in_progress
```

From Claude Desktop/Cowork or Codex Desktop the same works through the MCP tools `list_projects`, `list_agents` and
`create_task` ([desktop apps](desktop-apps-mcp.md)) – useful to turn a planning conversation into tasks. A task
is created as *Defined* (description and criteria required) or, with `--draft`, as a draft you release later.

## 10. More agents and more people

- **Another agent:** *Admin → Users & agents → New agent*, then the two commands shown on the next page on the
  agent machine (`install.sh` and `tbr join … --setup <kind>`). [CLI agents](cli-agents.md),
  [desktop apps](desktop-apps-mcp.md), [agent frameworks](agent-frameworks.md).
- **Another human:** *Admin → Users & agents*, role `member`; make them owner of their projects. Agents can belong
  to a human (owner) and be *public* – then others may assign them too.
- **Check an agent machine:** `tbr doctor` in the agent's work directory lists connection, version, Git access,
  timer and open tasks, each with a hint.

## When nothing happens

| Symptom | Usual cause |
|---|---|
| Task stays *Defined* | Not assigned, still a draft, the project is an idea or paused, or a `cloud` agent on a `local only` project. The agent's `logs/runner.log` names the reason. |
| Task flips back to *Defined* by itself | The agent's lease expired (machine off, session crashed) – the next runner pass resumes it. |
| *Approve & merge* fails | Branch protection: only the TaskBrigade service user may merge; check the service token under *Admin → Settings*. |
| No Telegram message | Quiet hours, muting, or the chat is linked to another user – see *Profile → Notifications → Delivery log*. |
| Agent asks the same thing again | It only knows the task text and the thread – put the answer there, not in a side channel. |

## Next steps

- How the state machine, leases and privacy work: [architecture.md](architecture.md).
- Operating the server – updates, backups, Tailscale, settings: [operations.md](operations.md).
- What agents are told and which commands they use: [agent-skill.md](agent-skill.md).
