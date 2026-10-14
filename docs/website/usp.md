# TaskBrigade — website source copy

Source text for a product page. It is not the page itself. Feature claims below were checked against this
repository (README, `docs/`, `CHANGELOG.md`, `SECURITY.md`) on 2026-10-13. Competitor notes use the linked pages
as fetched the same day; recheck them before publishing, because those products change.

Each feature names the file that documents it. Anything not shipped is marked **planned** or left out.

## 1. One-liner and short description

**One-liner:** Agile project management for a team of AI agents you already run.

TaskBrigade is a self-hosted task manager. Humans plan work, answer questions, and approve results. Agents claim
tasks, do the work on their own Git branch, and hand in a pull request. A task can be code, a document, a research
brief, or any other files an agent can produce.

**Who it is for:** People and small teams who already use several coding agents — Claude Code, Codex, OpenCode,
desktop apps, Hermes, OpenClaw, local models — and want those agents to work a backlog while someone still decides
what is accepted. It fits when the server and the repositories should stay on machines you operate, including
projects that must not be given to a cloud model.

Sources: [README.md](../../README.md) (opening and "Why TaskBrigade"), [docs/getting-started.md](../getting-started.md).

## 2. Top USPs

### Agents work like team members, and someone else accepts the result

An agent claims an assigned task, gets its own Git branch and worktree, and must finish with one explicit step:
submit a pull request, ask a question, abort, or give the task back. Approval — by a human or by a different
reviewer agent — merges the pull request. The assignee never reviews its own work, and agents do not write to the
default branch.

**Problem it solves:** A chat session does not record who owns the work, what was asked two hours later, or which
change was actually accepted.

Sources: [README.md](../../README.md) ("How it works", "How agile maps to agents"),
[docs/architecture.md](../architecture.md) (task states, worktree, review),
[docs/operations.md](../operations.md) ("Reviewers"), [docs/agent-skill.md](../agent-skill.md).

### One workflow for many agents, including local models, on the logins you already have

The same backlog drives Claude Code, Codex CLI, and OpenCode (runner), Hermes and OpenClaw (wake gate), and
Claude Desktop/Cowork and Codex Desktop (MCP). Each agent runs in its own client, signed in with your account.
TaskBrigade does not proxy model APIs or resell access. A project's `local_only` setting is not offered to agents
whose runtime is `cloud`.

**Problem it solves:** A vendor's background agent runs that vendor's product. Mixing a local model for sensitive
repos with a cloud model for everything else means a second, disconnected workflow.

Sources: [README.md](../../README.md) ("Supported agents", "Use the subscriptions you already have"),
[docs/cli-agents.md](../cli-agents.md), [docs/desktop-apps-mcp.md](../desktop-apps-mcp.md),
[docs/agent-frameworks.md](../agent-frameworks.md), [docs/architecture.md](../architecture.md) ("Privacy").

### Unattended runs, with a cheap check when there is nothing to do

A systemd user timer (Linux) or launchd agent (macOS) runs `tbr run` about every 10 minutes. No task means no
model call. A claimed task is leased (default 120 minutes); heartbeats extend the lease, and an expired lease
returns the task. The runner notices a window that stops changing, resumes a task whose session died, and can park
a task that is waiting on a long background job (up to 24 hours) while watching its log.

**Problem it solves:** Unattended agents hang, lose their machine, or spend tokens asking an empty queue if there
is work.

Sources: [README.md](../../README.md) (leases, wake gate), [docs/cli-agents.md](../cli-agents.md) ("How it works"),
[docs/architecture.md](../architecture.md) ("Leases and heartbeats", "Wake gate"),
[CHANGELOG.md](../../CHANGELOG.md) (0.13.0 waiting; 0.13.1 parked waiting).

### Parallel agents share project knowledge without editing the same file

Each repository can carry a `brain.md` (stack, commands, decisions, pitfalls). Agents do not edit it directly.
They add `brain-changes/<TASK-NR>.md`, and the same pattern exists for changelog fragments in `changelog.d/`.
TaskBrigade applies those files in a separate commit when the task is approved, in approval order. A stale edit
blocks the merge and sends the task back.

**Problem it solves:** Several agents updating the same "how this project works" file will conflict on every
merge.

Sources: [docs/architecture.md](../architecture.md) ("Project knowledge"),
[brain-changes/README.md](../../brain-changes/README.md), [AGENTS.md](../../AGENTS.md),
[CHANGELOG.md](../../CHANGELOG.md) (0.9.0 review workflow).

### Self-hosted and open source, with a rule for projects that must stay local

You install the server yourself (systemd, or Docker Compose for amd64 and arm64) and keep the database on your
host. The license is Apache-2.0. Projects are `local_only` or `cloud_ok`. A cloud agent cannot be assigned a
`local_only` project, and an agent that already has such work cannot be switched to `cloud`. The public repository
is [github.com/taskbrigade/taskbrigade](https://github.com/taskbrigade/taskbrigade).

**Problem it solves:** Hosted agent products send repository contents to the vendor. A local model does not help
if the task tracker itself has no way to refuse a cloud agent.

`local_only` is an assignment rule, not a sandbox. See [Honest limits](#6-honest-limits).

Sources: [README.md](../../README.md) (license, deployment, privacy), [LICENSE](../../LICENSE),
[docs/docker.md](../docker.md), [docs/architecture.md](../architecture.md) ("Privacy"),
[SECURITY.md](../../SECURITY.md).

## 3. Other notable features

Grouped for a features section. Each item is one or two sentences.

### Planning

- **Drafts, then defined.** A task is picked up only when it is `defined`, which requires a description and
  acceptance criteria. Drafts are never handed to an agent.
  ([docs/architecture.md](../architecture.md), [docs/getting-started.md](../getting-started.md))
- **Optional sprints.** A planned sprint holds drafts. Releasing it publishes the drafts that are complete. Closing
  it moves open tasks on and does not stop work already running. The sprint editor is one page for headings and
  details. ([README.md](../../README.md), [docs/architecture.md](../architecture.md))
- **Priorities, dependencies, subtasks.** Agents take the highest-priority ready task. Due dates are shown and do
  not change claim order. A task waits while a dependency is open.
  ([docs/architecture.md](../architecture.md) "What an agent is offered")
- **Open points and follow-ups.** On submit, the agent can list what is still open. The task page can turn the
  selected points into a new draft. ([docs/architecture.md](../architecture.md) "Follow-up tasks")
- **Duplicate a task.** Admins, orchestrators, and the project owner can copy a task into a new unassigned draft.
  ([CHANGELOG.md](../../CHANGELOG.md) 0.10.0)
- **Model tier per task, where the runner can apply it.** Tiers are `simple`, `standard`, and `complex`, mapped
  per agent, plus an optional concrete model on that agent's allow list. The runner passes the flag for Claude
  Code, Codex, and OpenCode, and records the model it used. For Hermes, OpenClaw, and the desktop apps the tier is
  shown and not applied — use a second agent pinned to the model you want.
  ([docs/cli-agents.md](../cli-agents.md) "Model per task", [docs/agent-frameworks.md](../agent-frameworks.md)
  "Model tiers")
- **Optimistic locking.** Concurrent edits send `If-Match` and surface a conflict instead of a silent overwrite.
  ([docs/architecture.md](../architecture.md) "Concurrency")

### Roles

- **Humans:** `admin`, or `member` who owns projects. Every project has a human owner.
  ([docs/operations.md](../operations.md) "Several people: owners and roles")
- **Agents:** `worker` (does tasks), `orchestrator` (may also create and assign tasks), `reviewer` (reviews tasks
  it is entered for, and can still do its own work). ([README.md](../../README.md), [docs/operations.md](../operations.md))
- **No self-review.** The reviewer cannot be the assignee. The project owner and admins can always approve, reject,
  or return a conflict, including while a reviewer agent is working.
  ([docs/operations.md](../operations.md) "Reviewers")
- **Agent owners and public agents.** A non-admin can assign their own agents and agents marked public, not another
  person's private agent. ([docs/operations.md](../operations.md), [CHANGELOG.md](../../CHANGELOG.md) migration 0018)
- **Orchestrate from chat.** An OpenClaw agent with the orchestrator role can create tasks and plan sprints from
  Telegram free text, using `tbr` only. It does not approve results; review stays on the TaskBrigade bot buttons or
  the web UI. Desktop apps can create and assign tasks through MCP.
  ([docs/agent-frameworks.md](../agent-frameworks.md), [docs/desktop-apps-mcp.md](../desktop-apps-mcp.md))

### Questions, review, notifications

- **Questions instead of guesses.** `tbr ask` stops the run. The question appears in the inbox. The answer is in
  the thread when the same agent claims the task again. ([README.md](../../README.md), [docs/getting-started.md](../getting-started.md))
- **Inbox.** Questions, reviews, merge conflicts, and aborted tasks, oldest first.
  ([docs/getting-started.md](../getting-started.md))
- **Reviewer agents.** A reviewer agent claims a review job: read-only checkout, `REVIEW.md`, its own lease, then
  `tbr review` approve, reject, or return conflict. Same merge path as a human approval.
  ([docs/operations.md](../operations.md), [docs/cli-agents.md](../cli-agents.md) "Reviewer agents")
- **Telegram, without opening the server to the internet.** Your own bot long-polls. Humans can answer a question
  by reply and approve or reject with buttons. Quiet hours, pause, and "reviews off" exist. `local_only` projects
  send only the task number, the event, and a link — not the question text.
  ([docs/operations.md](../operations.md) "Telegram notifications", [docs/architecture.md](../architecture.md))
- **One review at a time per agent** is an optional project setting. While an agent already has a task in review
  in that project, it is not offered another one — parallel tasks were colliding on shared files such as the
  changelog. ([CHANGELOG.md](../../CHANGELOG.md) 0.9.0, [brain.md](../../brain.md) decision 2026-10-08,
  `projects.serialize_reviews`)

### Git, conflicts, and files that are not a pull request

- **Pull request per task in Gitea.** Submit opens or updates the PR. Approval merges it (default squash). Reject
  sends the task back to `changes_requested` on the same branch. ([docs/architecture.md](../architecture.md))
- **Conflicts are handed back with the file list.** The next claim merges the base branch; the agent resolves the
  markers. ([docs/architecture.md](../architecture.md) "Merge conflicts")
- **One worktree per task**, with `TASK.md` (description, criteria, full thread, commands). Git credentials are
  fetched for that Gitea host only; they are not stored in the repo. ([docs/architecture.md](../architecture.md))
- **SFTP drop**, optional. Agents can upload results to a pinned SFTP server (for example a NAS). The host key is
  checked before the password is sent. Uploads are noted on the task. Agents use it only when the task asks.
  ([docs/operations.md](../operations.md) "SFTP storage")
- **Attachments** go into the repository under `tasks/<TASK-NR>/` (default limit 10 MB).
  ([docs/architecture.md](../architecture.md))

### Keeping agents running

- **tmux window per runner task.** You can attach and intervene. If the agent exits without reporting back, new
  commits are submitted; otherwise the task is given back. ([docs/cli-agents.md](../cli-agents.md))
- **Start check.** Login expired, rate limit, or an unknown model stops that run and says why. A login failure
  blocks further claims until `tbr doctor` sees the agent logged in again. An unknown model aborts that task only.
  ([docs/cli-agents.md](../cli-agents.md))
- **Parallel-task limit** per agent (default 2). ([docs/architecture.md](../architecture.md))
- **Join in one command.** A one-time code (`tbr join`) plus `tbr setup` installs the timer or the MCP entry.
  `tbr doctor` reports what is missing. The server can hand out the `tbr` build the server itself is running.
  ([docs/cli-agents.md](../cli-agents.md), [README.md](../../README.md) "Getting started")
- **Desktop apps are manual.** Claude Desktop/Cowork and Codex Desktop do not poll on a timer. You ask the app to
  start a task. `tbr mcp` does Git and network calls, because those apps often cannot reach Gitea themselves, and
  it keeps the lease alive. ([docs/desktop-apps-mcp.md](../desktop-apps-mcp.md))
- **Wake gate for Hermes and OpenClaw.** Their own scheduler runs `tbr prepare --wake-gate`. `{"wakeAgent": false}`
  means the framework does not start a model. ([docs/agent-frameworks.md](../agent-frameworks.md))

### The board

- **Kanban** with drag and drop, including a waiting column for questions and aborted tasks, and a period filter
  on done (default: three days). A card can show that it is waiting on a background run.
  ([README.md](../../README.md), [docs/getting-started.md](../getting-started.md))
- **Backlog** filters by project, sprint, agent, owner, priority, and status.
  ([docs/getting-started.md](../getting-started.md))
- **English by default, German optional**, switched per browser. ([docs/getting-started.md](../getting-started.md))
- **Not only software tasks.** The same states apply to research, analysis, and documents, as long as the result
  is files. ([README.md](../../README.md))

### Planned, not shipped

- **More native agent integrations** are planned. Other agents can already use the REST API or `tbr`; that is not
  the same as a setup guide and runner profile. ([README.md](../../README.md) "Supported agents")
- The runner's model-flag table mentions Gemini "for a later extension". Gemini is not a supported integration.
  ([docs/cli-agents.md](../cli-agents.md) "Model per task")

## 4. How this differs from other tools

These are neighbouring tools for coordinating coding agents, not a ranking. The "difference" column only restates
what that product's own page says, next to what TaskBrigade documents. It does not claim the other product cannot
grow a feature that its page does not mention.

| Tool | What its pages describe | Difference that matters here | Source |
|---|---|---|---|
| GitHub Copilot cloud agent | Copilot sessions started from GitHub (issues, agents tab, chat), IDEs, the API, Slack, Teams, Jira, Linear, and others. Work continues in the background and can end in a pull request on GitHub. Automations can start sessions on a schedule or on an event. | A cloud agent inside GitHub's workflow. TaskBrigade is a server you run, with Gitea as the forge, and the same board also drives other CLIs and local models. | [Cloud agent concepts](https://docs.github.com/en/copilot/concepts/agents/cloud-agent), [start sessions](https://docs.github.com/en/copilot/how-tos/use-copilot-agents/cloud-agent/start-copilot-sessions), [on GitHub](https://docs.github.com/en/copilot/how-tos/use-copilot-agents/cloud-agent/use-cloud-agent-on-github) |
| Cursor Cloud Agents | Cursor's agent in isolated cloud VMs, many in parallel, without your laptop staying online. They can build and test, and open pull requests. Source control is GitHub, GitLab, Bitbucket, or Azure DevOps, connected to a Cursor account. | Cursor's own cloud agent, tied to a Cursor account. Not a mixed local fleet of Claude Code, Codex CLI, OpenCode, Hermes, and OpenClaw with a `local_only` project rule. | [Cloud Agents](https://cursor.com/docs/cloud-agent) |
| Claude Code cloud sessions | A Claude Code session on Anthropic-managed infrastructure, or an organisation's self-hosted environment, that keeps running after the laptop closes. Setup for pushing branches is documented for GitHub. Included in Pro, Max, Team, and some Enterprise seats. | One vendor, one agent family, sessions rather than a shared backlog with questions, leases, and a reviewer who can be a different agent. | [Claude Code in the cloud](https://code.claude.com/docs/en/claude-code-on-the-web) |
| Claude Code agent teams | Experimental (off unless `CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS=1`). Several Claude Code sessions in one team: a lead assigns work, teammates message each other. Documented limits include session resume, coordination, and shutdown. | In-session coordination of Claude Code only. TaskBrigade keeps the task list, the lease, and the review decision outside any one CLI, and can assign the work to other agents. | [Agent teams](https://code.claude.com/docs/en/agent-teams) |
| Vibe Kanban | Open-source kanban for running local coding agents (Claude Code, Codex, OpenCode, and others listed in its README) in a workspace with a branch. Diff review in the UI, a dev-server preview, and pull requests it describes as reviewed on GitHub. The homepage says the hosted product is sunsetting and the project continues as community-maintained open source. | Closest shape: a board plus several local coding agents and worktrees. TaskBrigade's documented additions are the server-side states (ask, abort, lease expiry), timers that do not call a model when idle, reviewer agents, `local_only`, Gitea, and `brain.md` updates applied on approval. Vibe Kanban's README centres on interactive planning, in-UI review, and preview. | [vibekanban.com](https://www.vibekanban.com/), [README](https://github.com/BloopAI/vibe-kanban) |
| OpenHands Agent Canvas | Self-hosted control center. Runs the OpenHands agent and names Claude Code, Codex, Gemini, Pi, OpenCode, and other ACP agents, on local, Docker, VM, or cloud backends. Automations can talk to Slack, GitHub, and Linear. | Also self-hosted and multi-agent. Its README describes conversations and automations (reports, splitting GitHub issues), not TaskBrigade's claim / ask / submit states, Gitea merge, or `local_only` assignment rule. | [OpenHands README](https://github.com/All-Hands-AI/OpenHands) |

General multi-agent libraries (graphs of model calls inside an app you build) are a different layer. This pass did
not use their docs as sources, so they are not in the table.

Vendor background agents are a good fit when the whole team already lives in that vendor's forge and only that
agent should run. TaskBrigade is the other choice: several agents, a server you operate, and an explicit human
step before merge.

## 5. Suggestions for the website

### Sections

1. **Hero.** One-liner, the two-sentence description, and two actions: "Install with Docker"
   ([docs/docker.md](../docker.md)) and "Native quickstart" ([docs/quickstart.md](../quickstart.md)). Show the
   status line from section 6, not a "stable" badge.
2. **The problem.** The four questions in README "Why": who is working on what, what happens when an agent hangs,
   how you answer a question later, who accepts the result.
3. **A task's life.** `draft → defined → in progress → for review → done`, plus question, changes requested, and
   aborted. One sentence each.
4. **Agents you already run.** The supported-agents table from the README. Say in the same section that desktop
   apps are started by hand.
5. **Review and questions.** Inbox plus task thread. This is the human's daily screen.
6. **Plan a batch.** Sprint editor: nothing starts until the sprint is released.
7. **Unattended.** Ten-minute check, no model call when idle, lease, stall note, wait for a long job.
8. **Local-only and self-hosted.** Apache-2.0, your host, `local_only` as an assignment rule. Link `SECURITY.md`
   rather than summarising it away.
9. **Limits.** Section 6, shortened.
10. **Call to action.** Repository link, license, quickstart, and "read SECURITY.md before you expose it".

### Screenshots that exist

All of these are generated UI shots (light theme, invented demo data). See
[docs/screenshots/README.md](../screenshots/README.md).

| Section | File | What the reader should see |
|---|---|---|
| Hero or "Review and questions" | [inbox.png](../screenshots/inbox.png) | A question from an agent and results waiting for review |
| "A task's life" | [kanban.png](../screenshots/kanban.png) | Drafts, in progress, a question, an aborted task, review, done |
| Planning | [backlog.png](../screenshots/backlog.png) | Filters for actor, project, sprint, priority, status |
| Planning | [sprint-editor.png](../screenshots/sprint-editor.png) | Headings plus the selected draft |
| Review | [task-detail.png](../screenshots/task-detail.png) | Thread, pull request, result, open points |
| Agents | [agents.png](../screenshots/agents.png) | Agents, current task, parallel limit |

### Screenshots that do not exist yet

[docs/screenshots/README.md](../screenshots/README.md) lists these as still to be taken by hand. Do not ship broken
image links.

| Would illustrate | Missing file |
|---|---|
| Unattended runner (tmux session `taskbrigade`) | `docs/screenshots/runner-tmux.png` |
| Desktop app calling `start_task` | `docs/screenshots/desktop-mcp.png` |

A short terminal excerpt (`tbr ask`, `tbr submit`) from the README can stand in until the tmux shot exists.

### Call to action

Primary: get to a first agent task — Docker Compose in [docs/docker.md](../docker.md), or the native
[quickstart](../quickstart.md). Secondary: the source repository and the Apache-2.0 license. Do not offer a hosted
TaskBrigade; this repository does not describe one.

## 6. Honest limits

- **Experimental public version, not a stable release.** CHANGELOG marks 0.13.3 (2026-10-14) as the first public
  version. The README status box still says the project is experimental, that there is no stable release, and that
  the API may change. Security fixes are made on the current development branch; no older release line is
  maintained ([SECURITY.md](../../SECURITY.md)).
- **Read SECURITY.md before anyone else can reach it.** The intended setup is one trusted host, Uvicorn bound to
  `127.0.0.1` with `--no-proxy-headers`, and HTTPS via Tailscale (`tailscale serve`). Do not publish port 8090.
  Any local process that can connect to the listener can forge `Tailscale-User-Login`. Direct LAN UI is HTTP, and
  the session cookie does not set `Secure`. `local_only` does not stop a local agent from sending data out if its
  own tools allow it, and task text is untrusted input (prompt injection). The owner filter in the UI is not a
  confidentiality boundary. The security review is not a penetration test.
- **Gitea is the integrated forge.** Repositories, collaborators, pull requests, and merges go through Gitea
  (one you run, or the optional Compose profile). This is not a GitHub or GitLab app.
- **Desktop agents do not run unattended.** Only the runner timer and the Hermes/OpenClaw wake gate pick work up
  by themselves.
- **Model choice is not universal.** The runner applies a model flag for Claude Code, Codex, and OpenCode. Other
  agents ignore it. Whether unattended use is allowed by a provider's plan is that provider's rule; the README
  tells you to check.
- **Further native integrations are planned**, not shipped (see section 3).
- **A small self-hosted setup is the one described as running daily** (README). There are no published scale
  numbers in the docs.
