<img src="../app/static/brand/logo@2x.png" alt="TaskBrigade" height="36">

# Agent frameworks with a wake gate (Hermes, OpenClaw)

Some agents are not terminal CLIs but long-running agent frameworks with their own scheduler – for example
Hermes with its cron jobs, or OpenClaw with automations. They connect to TaskBrigade through the same pattern as the runner, just with the framework's
scheduler instead of a systemd timer. The files in [`deploy/agents/`](../deploy/agents/) are working examples;
treat them as templates and check the commands against the framework version you run.

## The pattern

1. The framework's scheduler runs a **cheap, LLM-free check** every few minutes:
   `tbr prepare --work-root <dir> --wake-gate --resume-own`.
2. **No task:** `tbr prepare` prints `{"wakeAgent": false}`. The check tells the framework not to start the agent –
   no model call, no tokens.
3. **Task claimed:** `tbr prepare` has already cloned the repository, created a worktree on the task branch and
   written `TASK.md`. It prints the assignment (starting with `# TaskBrigade assignment <TASK-NR>`;
   `# TaskBrigade-Auftrag <TASK-NR>` with `language = "de"`), which becomes the
   agent's prompt.
4. A **skill** installed in the framework tells the agent how to work the task and finish with exactly one of
   `tbr submit`, `tbr ask`, `tbr abort` (or `tbr comment` + `tbr release` when a run ends early).

`--resume-own` continues a task that is already `in_progress` for this agent instead of claiming a new one – for
example when the previous run hit its step limit, or the check timed out while `tbr prepare` kept running in the
background and claimed the task. Use it only when this scheduler is the only thing working on tasks for that agent.

Any other outcome of the check (error, unexpected output) must fail loudly: a broken check must never look like
"nothing to do".

Note that the check already claims the task. If the agent run then fails, the task only falls back to "defined"
when the lease expires (default 120 minutes) – or on the next check, thanks to `--resume-own`.

## Common setup

For each framework agent:

1. **⏸ Admin:** *Admin → Users & agents → New agent* with kind `hermes` or `openclaw` and the
   runtime and model location (`local` or `cloud`) of the model it uses; “local model” needs a confirmation. The page shows a one-time code and the command.
2. As the framework's user on its machine:

   ```bash
   curl -fsSL https://taskbrigade.example.ts.net/install.sh | sh       # only if tbr is missing
   tbr join https://taskbrigade.example.ts.net tmj_… --dir ~/taskbrigade-work/hermes --setup hermes
   tbr doctor --work-root ~/taskbrigade-work/hermes
   ```

   Already joined: `tbr setup hermes --work-root ~/taskbrigade-work/hermes` (or `openclaw`).

`tbr setup hermes|openclaw` installs the skill with your agent's name filled in, writes the check script with the
absolute paths of the work directory and `tbr`, and creates the cron job or automation through the framework's CLI.
It is idempotent: unchanged files are not touched, a changed file is backed up as `<file>.bak` first, and an existing
job (`taskbrigade-<agent>`) is not created twice. If the CLI is missing or does not offer the needed flags, it
prints the exact command instead. `--no-timer` skips the job. Nothing is restarted unless you pass `--restart`
(`hermes gateway restart` / `openclaw gateway restart`). `tbr doctor` checks skill, check script and job.

From before the rename (`tm`): `tbr setup hermes`/`tbr setup openclaw` rewrites the check script or trigger with
`tbr` (backup `<file>.bak`). Until then the installed scripts keep calling `tm`, which still works. The shipped
scripts fall back to `~/.local/bin/tm` on a machine without `tbr`. Details:
[From `tm` to `tbr`](cli-agents.md#from-tm-to-tbr-taskbrigade-05).

From before TaskBrigade 0.8 (names `taskmeister`): `tbr setup hermes|openclaw` (or `tbr doctor --fix`) installs
skill, script and trigger under the new names and renames the existing job `taskmeister-<agent>` to
`taskbrigade-<agent>` with `hermes cron edit … --name … --skill taskbrigade --script taskbrigade-prepare.sh` or
`openclaw automations edit <id> --name … --trigger-script …` – it never creates a second job next to it. When the
CLI does not offer these flags, the old job stays as it is, with a warning and the exact commands, and keeps its old
skill and script, so it keeps working. Once no old job is left, the old skill folders move to
`<work directory>/.backup-taskmeister/` (a folder left in the framework would still be loaded as a skill) and the old
script/trigger to `<file>.bak`. The triggers and skills recognise the assignment header `# TaskBrigade assignment`
(since 0.8) and the old `# Taskmeister assignment`. Details:
[From the names `taskmeister` to `taskbrigade`](cli-agents.md#from-the-names-taskmeister-to-taskbrigade-taskbrigade-08).

The shipped skills are written for one agent each and name it in their text (`hermes`, `openclaw-agent`); `tbr setup`
replaces the name. Manual setup: create the actor and token as for any agent, write
`~/taskbrigade-work/<agent>/.taskbrigade.toml` (mode `600`) with `url` and `token`, without a `[runner]` section,
and install the files below yourself.

## Hermes

| File | `tbr setup hermes` installs to | Purpose |
|---|---|---|
| [`hermes/taskbrigade-prepare.sh`](../deploy/agents/hermes/taskbrigade-prepare.sh) | `~/.hermes/scripts/taskbrigade-prepare.sh` | Pre-run script of the cron job: runs `tbr prepare --wake-gate --resume-own` in the work directory |
| [`hermes/taskbrigade/`](../deploy/agents/hermes/taskbrigade/SKILL.md) | `~/.hermes/skills/productivity/taskbrigade/` | Skill: how to work a task and report back |

`$HERMES_HOME` replaces `~/.hermes`. The cron job (every 10 minutes) uses the script as its pre-run and attaches
the skill:

```bash
hermes cron create "every 10m" "Work on the TaskBrigade assignment from the pre-run script output, following the taskbrigade skill. If the output is empty, do nothing." \
  --script taskbrigade-prepare.sh --skill taskbrigade --name taskbrigade-<agent>
```

The script reads `TBR_WORK_ROOT` and `TBR_BIN`; `tbr setup` sets their defaults to your work directory and
`tbr`. When it prints `{"wakeAgent": false}`, Hermes does not wake the agent; otherwise its output is the prompt.

If the cron job keeps context between runs, the skill tells the agent to copy a previous run's progress report for
the same task into the TaskBrigade thread first – so the state survives even when Hermes's own memory does not.

## OpenClaw

| File | `tbr setup openclaw` installs to | Purpose |
|---|---|---|
| [`openclaw/taskbrigade-trigger.js`](../deploy/agents/openclaw/taskbrigade-trigger.js) | `<work directory>/taskbrigade-trigger.js` | Condition trigger: `fire: false` without a task, `fire: true` with the assignment as message |
| [`openclaw/taskbrigade/`](../deploy/agents/openclaw/taskbrigade/SKILL.md) | `<agent workspace>/skills/taskbrigade/` | Skill: how to work a task and report back |
| [`openclaw/taskbrigade-telegram/`](../deploy/agents/openclaw/taskbrigade-telegram/SKILL.md) | `<agent workspace>/skills/taskbrigade-telegram/` | Skill: orchestrate TaskBrigade on free-text requests in a chat (see below) |

`$OPENCLAW_STATE_DIR` replaces `~/.openclaw`. `tbr setup` sets the work directory (`root`) and the path of `tbr` in
the trigger and registers the automation.

### Several OpenClaw agents on one gateway

One gateway can run several OpenClaw agents (`main` plus e.g. `researcher`), each with its own workspace and its own
TaskBrigade agent. The OpenClaw agent id does not have to match the TaskBrigade name – tell `tbr setup` which one
works the tasks:

```bash
tbr setup openclaw --work-root ~/taskbrigade-work/research-agent --openclaw-agent researcher
```

- **Default for `--openclaw-agent`:** the agent the existing automation `taskbrigade-<agent>` runs for, otherwise
  `main`. A re-run without the option therefore never moves an automation to `main`.
- **Skill:** goes to `<workspace>/skills/taskbrigade/SKILL.md` of that agent. The workspace comes from
  `openclaw agents list --json`, else from `agents.entries.<id>.workspace` in `~/.openclaw/openclaw.json` (plain JSON
  only), else OpenClaw's default: `~/.openclaw/workspace` for `main`, `~/.openclaw/workspace-<id>` for the others. An
  id the configuration does not know is refused. Other agents' skills, including `main`'s, stay untouched.
- **New automation:**

  ```bash
  openclaw automations add "*/10 * * * *" "Work on the TaskBrigade assignment in this message, following the taskbrigade skill." \
    --name taskbrigade-<agent> --agent <openclaw-agent> --session isolated --no-deliver --timeout-seconds 3600 \
    --trigger-script ~/taskbrigade-work/<agent>/taskbrigade-trigger.js
  ```

  `--agent` is required: without it the automation runs for the default agent `main`. `--session isolated` gives
  every run a fresh session, `--no-deliver` keeps the agent's output out of chat channels (the result goes to
  TaskBrigade), `--timeout-seconds 3600` caps one agent run; the task lease (2 h) leaves room for the next check to
  resume it.
- **Existing automation:** OpenClaw embeds the trigger script into the automation, so changing the file in the work
  directory alone changes nothing. If the embedded script differs from the current one (e.g. still calls `tm`), or
  the list does not show it, `tbr setup` runs `openclaw automations edit <job-id> --trigger-script <file>`; with an
  explicit `--openclaw-agent` that differs, it adds `--agent <id>`.
- **`tbr doctor`** looks for the skill in the workspace of the automation's agent and compares the embedded trigger
  with the current one: "embedded in taskbrigade-<agent>" is fine, a differing trigger is a warning with
  `tbr setup openclaw` as the fix. Only when the list does not show the embedded script is the file in the work
  directory checked.

`tbr setup` runs `add`/`edit` only when their `--help` lists the flags it needs; otherwise it prints the exact
command. OpenClaw moves long commands into the background after a short time; the trigger therefore waits up to 25
seconds and throws a clear error if `tbr prepare` is still running (typically the first clone of a large repository).
A task claimed in the meantime is resumed by the next check. OpenClaw does not start an automation while its
previous run is still active.

### OpenClaw as orchestrator in Telegram

An OpenClaw agent that already talks to you in Telegram (e.g. `openclaw-agent` as OpenClaw agent `main`) can also run
TaskBrigade on free-text requests: "create the task 'check data import' in project DEMO and assign it to codex",
"plan a sprint W43 for KET with the tasks …", "mute the notifications for 2 hours". The skill
`taskbrigade-telegram` tells it how: recognise the intent, ask back for a missing project or agent, create a task
without description and acceptance criteria only as a draft (and say so), confirm before creating or changing
anything, then reply with task number and link. It uses only `tbr` commands – no direct API calls – and never
approves or rejects: reviews stay with the buttons of the TaskBrigade bot and the web UI.

Setup:

1. **⏸ Admin:** give the TaskBrigade agent the role `orchestrator` (*Admin → Users & agents*, edit the agent).
   Without it, creating and assigning tasks and planning sprints fail with `403`. Releasing and closing sprints stays
   reserved for admins and the project owner, whatever the role – `openclaw-agent` then tells you to do it in the web UI.
2. Install or refresh the skills for the OpenClaw agent that chats with you:

   ```bash
   tbr setup openclaw --work-root ~/taskbrigade-work/openclaw-agent --openclaw-agent main
   ```

   `tbr setup openclaw` writes both skills (`taskbrigade` and `taskbrigade-telegram`) into that agent's workspace,
   with the agent name and the work directory filled in – every `tbr` command runs in the work directory, where the
   identity lives. Without `--openclaw-agent` the agent of the existing automation is used (see above). Restart the
   gateway (`--restart` or `openclaw gateway restart`) if the new skill does not show up.
3. Tell `openclaw-agent` your TaskBrigade user name once (it asks otherwise); it is the `--user` for `tbr notify`.
4. Muting from the chat: `tbr notify pause 2h --user <you>`, `tbr notify resume --user <you>`, `tbr notify reviews
   off|on --user <you>`, `tbr notify status --user <you>`. Only orchestrators and admins may change someone else's
   settings, and only for a human linked with the TaskBrigade bot; the change is audited and the TaskBrigade bot tells
   you about it. Your own bot commands (`/pause`, `/resume`, `/reviews`, `/status`) do the same without a model run.

The OpenClaw agent runs as its own TaskBrigade agent with its own role, not as you: everything it creates shows `openclaw-agent` as the
creator. Its chat bot and the TaskBrigade bot are separate bots – one bot token can be polled by one program only.

**Costs:** every free-text request is a model run and costs tokens (on a cloud model, money). Use the TaskBrigade
bot's commands and buttons for routine things – mute, status, inbox, approve – and `openclaw-agent` for requests that need
understanding, such as writing a task or planning a sprint.

## Reviewer agents

A Hermes or OpenClaw agent with the role `reviewer` gets review jobs through the same cron job or automation: `tbr
prepare` hands out a task waiting for its review as "# TaskBrigade review job <NR>" with a read-only checkout and
`REVIEW.md`; the skill (section "Review jobs") has it decide with `tbr review <NR> approve|reject|conflict` or give the
job back with `tbr review <NR> release --reason …`. Run `tbr setup hermes|openclaw` once after updating so the skill
and the OpenClaw trigger know the review header. Details on reviewers: [operations.md](operations.md#reviewers-who-approves-a-task).

## Model tiers

Tasks can carry a model tier (`simple`/`standard`/`complex`) or a concrete model, see
[cli-agents.md](cli-agents.md#model-per-task-tiers). Hermes and OpenClaw choose the model per agent or job
themselves, so TaskBrigade **shows** tier and model for them (task page, TASK.md: "ignored by this agent") but does
not apply them. To use a cheaper model for simple work, run a second agent (job) with that model and assign the
simple tasks to it. The same holds for the desktop apps (model chosen in the app).

## Other frameworks

Anything with a scheduler and a way to skip the model call works the same way. You need:

- a check that runs `tbr prepare --wake-gate` in the agent's work directory and maps `{"wakeAgent": false}` to
  "do not start the agent",
- a way to pass the rest of the output to the agent as its prompt,
- shell access for the agent to run `git` and `tbr` in the worktree, and
- a skill or system prompt along the lines of [agent-skill.md](agent-skill.md).

For agents without shell access, the MCP server is the better fit: see [desktop-apps-mcp.md](desktop-apps-mcp.md).
