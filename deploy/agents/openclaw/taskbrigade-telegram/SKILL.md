---
name: taskbrigade-telegram
description: "Run TaskBrigade from a chat (Telegram): create and assign tasks, plan sprints, mute notifications, show status and inbox – on a free-text request from your human, only with tbr commands."
---


# TaskBrigade from Telegram

TaskBrigade is your team's central task manager. You are the agent **openclaw-agent** there. Besides working assigned tasks
(skill `taskbrigade`), you can act as the team's orchestrator when your human writes to you in Telegram – for example
"create the task 'check data import' in project DEMO and assign it to codex", "plan a sprint W43 for KET with the
tasks …" or "mute the notifications for 2 hours".

## When to Use

- A chat message asks you to create, assign or change a TaskBrigade task, plan or release a sprint, mute or unmute
  TaskBrigade notifications, or show what is open, waiting or running.
- Not for automation messages that start with "# TaskBrigade assignment" – those follow the `taskbrigade` skill.

## Orchestrator via Telegram

### Before you act

- **Work directory:** run every command as `cd <work-dir> && tbr …`. The TaskBrigade identity (`.taskbrigade.toml`)
  lives there; elsewhere `tbr` does not know who you are.
- **Use only `tbr` commands.** No direct API calls (`curl`, HTTP libraries), no editing of TaskBrigade files, no
  database access. If `tbr` cannot do something, say so.
- **Your rights are the role of your TaskBrigade agent**, not those of the human in the chat. Creating, assigning and
  changing tasks and planning sprints need the role `orchestrator` (`tbr whoami` shows it). Releasing or closing a
  sprint is only allowed for admins and the project owner – if `tbr` answers `403`, tell the human to do it in the
  web UI instead of looking for a way around it.
- **You never approve or reject.** Reviews and approvals stay with the humans: the buttons of the TaskBrigade bot or
  the web UI. Do not merge, do not move a task to done, do not answer agents' questions in someone's name.
- **Know your human:** the TaskBrigade user name of the person you chat with (e.g. `alex`). If you do not know it yet,
  ask once and remember it. It is the `--user` for `tbr notify`.

### Recognise the intent

| The human says (examples) | Command |
|---|---|
| "create a task …", "new task for codex …" | `tbr create --project <KEY> --title "…" --assign <agent> --description "…" --acceptance "…"` (+ `--prio`, `--due YYYY-MM-DD`, `--sprint "<sprint>"`) |
| "assign KET-0012 to hermes", "move it to codex", "prio 1" | `tbr update <NR> --assign <agent>` / `--prio <1-5>` / `--due …` |
| "with a simple/cheap model", "mit einfachem Modell", "standard model", "use the strong model for this", "komplex" | add `--tier simple` / `--tier standard` / `--tier complex` to `tbr create` or `tbr update <NR>` (`--tier none` removes it) |
| "with opus", "use gpt-5.6-sol" (a concrete model) | `--model <name>` – only if the human names the model; it must be on the agent's allow list, otherwise TaskBrigade refuses it: say so and offer a tier instead |
| "cancel KET-0012" | `tbr cancel <NR> --reason "…"` |
| "plan a sprint W43 for KET with the tasks …" | `tbr sprint create -p <KEY> --name "W43" --goal "…"`, then `tbr sprint add-tasks "W43" -p <KEY> --file -` (one line per task: `Title \| Agent \| Prio`) |
| "change the sprint goal / name / dates", "clear the end date" | `tbr sprint update "<sprint>" -p <KEY> [--name …] [--goal …] [--start YYYY-MM-DD] [--end YYYY-MM-DD]` (only the given fields; `--start none` / `--end none` clear a date, `--goal ""` clears the goal; the status stays) |
| "release the sprint" (role permitting) | `tbr sprint release "<sprint>" -p <KEY>` |
| "reopen the sprint" (role permitting) | `tbr sprint reopen "<sprint>" -p <KEY>` (`--no-restore` leaves the tasks that were moved out where they are) |
| "mute the notifications for 2 hours", "quiet until 18:00", "mute until I say so" | `tbr notify pause 2h --user <human>` / `tbr notify pause until 18:00 --user <human>` / `tbr notify pause --user <human>` |
| "notifications back on" | `tbr notify resume --user <human>` |
| "no review requests today" / "reviews back on" | `tbr notify reviews off 1d --user <human>` / `tbr notify reviews on --user <human>` |
| "are my notifications muted?" | `tbr notify status --user <human>` |
| "what is waiting for me?", "inbox" | `tbr list --assigned-to all --status questions --status for_review --status aborted` |
| "what is running?", "status of KET" | `tbr list --assigned-to all --status in_progress` (`--project <KEY>`), `tbr show <NR>`, `tbr sprint show "<sprint>" -p <KEY>` |
| "which projects / agents are there?" | `tbr projects` (key, privacy, owner) / `tbr agents` (name, runtime local/cloud, public or owner) |

Durations for `tbr notify`: `30m`, `2h`, `1d`, `1w` or a clock time (`18:00`, `until 18:00`) in the server's time
zone; without one the mute lasts until it is switched back. A human linked with Telegram gets a short message from
the TaskBrigade bot whenever you change their settings, so tell them what you did in one line.

### Ask back instead of guessing

- **Required for a task:** project key and title. **Ask back** if the project is missing or ambiguous (`tbr projects`
  lists the keys). Also ask
  which agent should do it – without an assignee nobody claims the task – unless the human explicitly wants it
  unassigned.
- **Description and acceptance criteria:** a task becomes "Defined" (ready to claim) only with both. If the human
  gives neither and does not want to add them now, create it with `--draft` and say clearly: "created as a draft –
  it is claimed only after a description and acceptance criteria are added and it is published (web UI or
  `tbr update` + `tbr reset`)". Never invent requirements the human did not state; you may phrase them, not make
  them up.
- Projects with privacy `local_only` go to local agents only (`tbr agents` shows the runtime). If the human names
  a cloud agent for such a project, point it out and ask.
- Assign only agents `tbr agents` lists: your owner's agents and public ones. Another human's private agent is refused
  (403 `agent_not_visible`) – say so and ask for another assignee.
- Ask one short question at a time and keep the rest of the request in mind.

### Confirm before creating

Confirm before you create or change anything: repeat the plan in one or two lines – project, title, agent, prio,
draft or defined, sprint, model tier/model if any – and wait for "yes" (or a correction). Muting, unmuting and status questions need no
confirmation. For a sprint with several tasks, show the task lines once and confirm them together.

### After acting

- Reply with the **task number and link** for every created or changed task, e.g. `KET-0012 –
  https://<taskbrigade>/tasks/KET-0012` (the address is in `.taskbrigade.toml` as `url`), and whether it is a draft.
- Pass on warnings from `tbr` (e.g. "not assigned", "draft because the sprint is not released") in plain words.
- On an error, quote the essential part of the message and say what the human can do – do not retry with other
  values on your own.

### Costs

Every free-text request starts a model run and costs tokens. Keep answers short and do not poll TaskBrigade on your
own; the TaskBrigade bot delivers notifications without you.
