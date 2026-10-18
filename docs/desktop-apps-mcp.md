<img src="../app/static/brand/logo@2x.png" alt="TaskBrigade" height="36">

# Desktop apps via MCP (Claude Desktop/Cowork, Codex Desktop)

Desktop apps are driven by hand: you tell the app "Check whether there is a TaskBrigade task for you" or "Run
TaskBrigade task DEMO-0003". The app uses the tools of a local MCP server, `tbr mcp`, for that. The examples assume
a Mac; on other systems only the paths of the app configuration differ.

Claude Code (or you) can work through this guide step by step on the desktop machine. Steps marked **⏸ Admin** are
yours – **never paste tokens into a chat**.

## In short

1. **⏸ Admin:** *Admin → Users & agents → New agent* – e.g. `claude-desktop`, kind `other`,
   runtime `cloud`. The page shows a one-time code.
2. On the desktop machine:

   ```bash
   curl -fsSL https://taskbrigade.example.ts.net/install.sh | sh       # only if tbr is missing
   tbr join https://taskbrigade.example.ts.net tmj_… --dir ~/taskbrigade-work/claude-desktop --setup claude-desktop
   tbr doctor --work-root ~/taskbrigade-work/claude-desktop
   ```

   For Codex Desktop the same with `codex-desktop` (work folder `~/taskbrigade-work/codex-desktop`).
3. Restart the app (or add `--restart` on macOS) and, for Claude, upload the skill once (the path is printed).

The admin page does not add `--setup` for desktop apps (they have no agent kind of their own) – type it yourself.
The steps below explain what `tbr setup` does and the manual way.

## How it works

- `tbr mcp` runs **on the desktop machine** (started by the app) and talks to TaskBrigade (over Tailscale, for
  example `https://taskbrigade.example.ts.net`) and to Gitea.
- **Git and networking are done exclusively by the MCP server.** Cowork works in a Linux VM with a network
  allowlist, Codex in a sandbox – neither reaches Gitea or TaskBrigade itself. The app only edits files in the work
  folder; `submit_task` commits, pushes and opens the pull request.
- Tools: `list_tasks`, `start_task`, `show_task`, `show_changes`, `submit_task`, `ask`, `abort_task`,
  `release_task`, `comment` – and for creating and managing tasks `list_projects`, `list_agents`, `create_task`,
  `update_task`, `cancel_task`, `reset_task`, `duplicate_task`, and for sprints `list_sprints`, `show_sprint`, `create_sprint`,
  `update_sprint`, `add_sprint_tasks`, `release_sprint`, `close_sprint`, `reopen_sprint` (plus the `sprint` parameter of `create_task`/`update_task`),
  and for attachments `attach_files`, `attach_text` (plus the `attachments` parameter of `create_task`).
  The server ships the workflow rules as MCP instructions.
- While a task is running, the MCP server extends the lease automatically (up to 8 hours).
- `start_task` with a number that is already in progress for you resumes the task (for example in a new chat).

<!-- Screenshot placeholder – see screenshots/README.md:
![A desktop app calling the start_task tool](screenshots/desktop-mcp.png)
-->

| App | TaskBrigade actor | Gitea user | Work folder |
|---|---|---|---|
| Claude Desktop / Cowork | `claude-desktop` | `claude-desktop` | `~/taskbrigade-work/claude-desktop` |
| Codex Desktop | `codex-desktop` | `codex-desktop` | `~/taskbrigade-work/codex-desktop` |

Both actors (any names you like, one set per machine) are **manual only** – no timer picks up tasks you assign to
them. **⏸ Admin:** create the actors in TaskBrigade (*Admin → Users & agents*), with runtime `cloud` (both apps
send code to a model provider), and link their Gitea users.

## 1. Prerequisites

```bash
tailscale status | grep -i taskbrigade                      # machine in the tailnet?
curl -fsS https://taskbrigade.example.ts.net/healthz      # expected: "db":"ok"
command -v git uv || echo "missing"
```

- `git` on macOS: Xcode Command Line Tools (`xcode-select --install`).
- `uv`: installed by the script in step 2 if missing (or `brew install uv`).

## 2. Install `tbr`

```bash
curl -fsSL https://taskbrigade.example.ts.net/install.sh | sh
~/.local/bin/tbr version
```

The server delivers `tbr` in its own version (no Gitea access needed); the script installs `uv` first if it is
missing. Details: [CLI agents → Install `tbr`](cli-agents.md#1-install-tbr).

Update: `tbr self-update` in the work folder (its `.taskbrigade.toml` names the server). An outdated `tbr` warns, and
`list_tasks` in the desktop app starts with that warning.

From before the rename (`tm`): `tm self-update`, then `tbr self-update`, then run `tbr setup claude-desktop` or
`tbr setup codex-desktop` again – it points the MCP entries of this work folder to `tbr` (backup `<file>.bak`);
restart the app afterwards. `tbr doctor` names entries that still call `tm`. Details:
[From `tm` to `tbr`](cli-agents.md#from-tm-to-tbr-taskbrigade-05).

## 3. Work folders and identities

`tbr join` creates the work folder and its `.taskbrigade.toml` (mode `600`) with the agent's token – without
`[runner]`, desktop apps work manually only. Already joined? Then run only `tbr setup` (step 4/5) in the folder.

Manual way without a join code:

```bash
install -d -m 700 ~/taskbrigade-work/claude-desktop ~/taskbrigade-work/codex-desktop
```

```toml
url = "https://taskbrigade.example.ts.net"
token = "PASTE_API_TOKEN_HERE"
```

**⏸ Admin:** rotate the token in TaskBrigade under *Admin → Users & agents → claude-desktop or codex-desktop*
and enter it yourself (`nano ~/taskbrigade-work/claude-desktop/.taskbrigade.toml`).

Verify:

```bash
cd ~/taskbrigade-work/claude-desktop && ~/.local/bin/tbr whoami     # name=claude-desktop, gitea_username=claude-desktop
cd ~/taskbrigade-work/codex-desktop  && ~/.local/bin/tbr whoami     # name=codex-desktop, gitea_username=codex-desktop
```

## 4. Claude Desktop / Cowork

```bash
tbr setup claude-desktop --work-root ~/taskbrigade-work/claude-desktop
tbr setup claude-desktop --work-root ~/taskbrigade-work/claude-desktop-test --name taskbrigade-test   # second server
```

`tbr setup claude-desktop` adds the MCP server to `claude_desktop_config.json` (macOS:
`~/Library/Application Support/Claude/`, Linux: `~/.config/Claude/`, Windows: `%APPDATA%\Claude\`):

- It merges the JSON: other `mcpServers` entries and settings stay as they are. Before a change it copies the file
  to `claude_desktop_config.json.bak`; if the entry is already correct, nothing is written. A file that is not valid
  JSON is refused and left untouched.
- `--name` sets the entry name (default `taskbrigade`) – one entry per work folder, e.g. a second agent on a test
  server.
- It prints the path of the skill `taskbrigade-skill.zip` (see below) and reminds you to restart Claude Desktop.
  Only `--restart` restarts the app (macOS); without it nothing is restarted.
- The entry contains only the path of `tbr` and the work folder – the token stays in `.taskbrigade.toml`.
- An entry from before TaskBrigade 0.8 named `taskmeister` (for the same work folder) is renamed to `taskbrigade`
  instead of adding a second one; `tbr doctor --fix` does the same. Restart the app afterwards; its tools are then
  called `mcp__taskbrigade__…`. Upload the new skill `taskbrigade-skill.zip` and remove the old skill `taskmeister`.

The resulting entry (absolute paths only):

```json
{
  "mcpServers": {
    "taskbrigade": {
      "command": "/Users/<user>/.local/bin/tbr",
      "args": ["mcp", "--work-root", "/Users/<user>/taskbrigade-work/claude-desktop"]
    }
  }
}
```

Manual way: add it in Claude Desktop under *Settings → Developer → Edit Config*, keep the existing entries under
`mcpServers`. Quit Claude Desktop completely and restart it; under *Settings → Developer*, `taskbrigade` must show
as running.

**Cowork:** when starting a Cowork task, share the folder `~/taskbrigade-work/claude-desktop` – only there can
Cowork edit the task's files. Inside the VM the folder has a different path; `start_task` therefore also gives the
path relative to the shared folder. Claude Desktop passes local MCP servers from `claude_desktop_config.json`
through to Cowork; if `taskbrigade` does not show up in Cowork, test it in a normal chat first.

### Skill for Claude (recommended)

Add [`deploy/agents/cowork/taskbrigade-skill.zip`](../deploy/agents/cowork/taskbrigade-skill.zip) in Claude under
*Settings → Capabilities → Skills → Upload skill* (`tbr setup claude-desktop` prints the path of the copy that comes
with `tbr`). It teaches Claude the workflow for "Check whether there is a TaskBrigade task for you" or "Run task …"
and makes it use the MCP tools. The skill applies to your Claude account, so in Cowork and in normal chats. The skill
and the MCP texts are English; Claude follows them regardless of the chat language (`language = "de"` in
`.taskbrigade.toml` switches the MCP texts to German). **Without the MCP server from step 4 the skill cannot claim
anything** – Cowork cannot reach TaskBrigade and Gitea from its VM.

## 5. Codex Desktop

```bash
tbr setup codex-desktop --work-root ~/taskbrigade-work/codex-desktop
```

It adds the table `[mcp_servers.taskbrigade]` (or `--name`) to `~/.codex/config.toml` (`$CODEX_HOME` if set; the
file applies to the Codex app and CLI). Other tables and keys stay untouched: `tbr setup` replaces only its own table,
checks that the result parses and differs from the old file only there, and keeps the previous file as
`config.toml.bak`. If it cannot change the file safely – for example when `mcp_servers` is written as an inline
table – it changes nothing and prints the section to add yourself:

```toml
[mcp_servers.taskbrigade]
command = "/Users/<user>/.local/bin/tbr"
args = ["mcp", "--work-root", "/Users/<user>/taskbrigade-work/codex-desktop"]
```

Alternatively, enter the same values in the Codex app under *Settings → MCP servers*. Restart Codex (or
`--restart` on macOS). Open the folder `~/taskbrigade-work/codex-desktop` as a project in Codex so that it may edit
the task files. Codex may ask for approval before the first call of a TaskBrigade tool – fine for manual use.

`tbr doctor --work-root <folder>` finds the entries in both apps that point to the folder and checks that their `tbr`
exists; it does not expect a `[runner]` or timer then.

## 6. Use

- "Check whether there is a TaskBrigade task for you" → `list_tasks`
- "Run TaskBrigade task DEMO-0012" → `start_task`, then the app works in the work folder
- At the end the app calls `submit_task` (pull request), `ask` (a question for you) or `abort_task`.
- After you answered: "Continue task DEMO-0012" – the thread with your answer comes along.
- "Create a TaskBrigade task in project DEMO for codex: …" → `create_task`. With a description and acceptance
  criteria the task is "defined" right away and will be claimed; otherwise a draft is created.
  **Requirement:** to assign tasks to other agents, the app's actor (claude-desktop/codex-desktop) needs the role
  `orchestrator` in TaskBrigade (*Admin → Users & agents*). With role `worker` it can only create unassigned
  tasks or tasks for itself.
- "Plan a sprint 'Sprint 1' in DEMO with these tasks: …" → `create_sprint`, `add_sprint_tasks` (one line per task,
  `Title | Agent | Prio | Reviewer`), then `update_task` for each draft. "Release sprint 'Sprint 1'" →
  `release_sprint` – only for admins and the project owner; the app reports which drafts are still missing a description or criteria.
  "Close the sprint and move the open tasks to 'Sprint 2'" → `close_sprint(move_to=…)` or `detach=True`.
  "Change the sprint goal" / "clear the end date" → `update_sprint` (only the given fields; `"none"` clears a date,
  an empty goal clears it; the status stays unchanged). "Reopen the sprint" → `reopen_sprint` (admins and the
  project owner; `restore_tasks=False` leaves the tasks that were moved out where they are).
- Reviews (the app's agent has the role `reviewer` and is entered as a task's reviewer): `list_tasks` lists the task
  under "Review jobs", `start_task` claims it and returns `REVIEW.md` with a read-only work folder; the app decides
  with `review_task(task_nr, "approve"|"reject"|"conflict"|"release", comment)`. `create_task`/`update_task` take a
  `reviewer` (human or reviewer agent, `"none"` removes it).
- Dependencies: `create_task`/`update_task` take `depends_on` (list of task numbers or a comma-separated string). The
  agent can claim the task only once all of them are done. `update_task` replaces the list, `"none"` removes all, and
  `show_task` lists the dependencies with their status.
- SFTP storage (when enabled under *Admin → Settings* and the app's agent has an SFTP login): "Put the report on
  the SFTP" → `sftp_put(task_nr, files)` uploads files from the task's work folder into the task's folder on the
  SFTP server, `sftp_ls` lists it. The transfer runs in `tbr mcp` on the Mac – the Cowork VM does not reach the NAS
  itself. Every upload is noted in the task with size and SHA-256.
- Attachments: "Create a task for codex and attach `~/Downloads/offer.pdf`" → `create_task(…, attachments=[…])`;
  for an existing task `attach_files(task_nr, paths)`. See [Attachments](#attachments).

## Attachments

`tbr mcp` reads the files on the desktop machine and uploads them with the same API call as `tbr attach`
(`POST /api/v1/tasks/{nr}/attachments`); TaskBrigade commits them into the project repository under
`tasks/<NR>/`. The project needs a Gitea repository, and only the task's creator, its assignee, the project owner,
orchestrators and admins may attach. In a `local_only` project the server refuses spreadsheet and archive files
(`.xlsx`, `.xls`, `.xlsm`, `.csv`, `.tsv`, `.ods`, `.zip`) with `422 use_dataset_upload` and points at the coming
data store; `cloud_ok` projects are unchanged. If attaching fails after `create_task`, the task stays and the reply
names the task number and the error – attach again with `attach_files`.

Because a chat model decides which paths to pass (and a prompt injection could try `~/.ssh/id_ed25519`), the MCP
server reads files **only from allowed folders**:

- Default: the agent's work folder (`--work-root`), `~/Downloads`, `~/Documents`, `~/Desktop`.
- Your own list in the work folder's `.taskbrigade.toml` – it **replaces** the defaults (add the work folder again if
  you want it); `~` is expanded, relative paths count from the work folder, folders that do not exist are ignored:

  ```toml
  [mcp]
  attach_roots = ["~/Downloads", "~/Shared/TaskBrigade"]
  max_attachment_mb = 10   # only if the server's TBR_MAX_ATTACHMENT_MB is raised
  ```

Paths are resolved (symlinks included) before the check, so a link pointing out of the folders is refused. Always
refused, each with the reason in the reply: hidden files or folders anywhere below the allowed folder (`.ssh`, `.env`,
`.git`, `.aws` …; task work folders under `<KEY>/.worktrees/` are the one exception), names that look like keys or
secrets (`id_*`, `*.pem`, `*.key`, `*.p12`, `*.pfx`, `*.ppk`, `*.kdbx`, `*.keychain`, `credentials*`, `netrc`,
`*.gpg`), folders, devices/pipes/sockets, empty files, files over 10 MB (`max_attachment_mb`) and more than 20 files
at once. Accepted files of the same call are uploaded anyway.

`attach_text(task_nr, filename, content)` attaches a small UTF-8 text (Markdown, CSV …, at most 1 MB) that the app
passes as content – the filename is reduced to its last part without leading dots.

**Claude Cowork:** Cowork runs in a VM. A file dragged into the chat lives inside the VM, and `tbr mcp` on the Mac
cannot read it. Put the file into a folder that Cowork shares with the Mac *and* that is an allowed folder (for
example `~/Documents/…`), then name the Mac path – or, for text, let Claude use `attach_text`. Codex Desktop works
on Mac folders directly, so its paths work as they are.

## 7. Test

**⏸ Admin:** create one small task each for `claude-desktop` and `codex-desktop` in project `DEMO`, then in the
respective app: "Run TaskBrigade task DEMO-00xx". Expected: a file is created, `submit_task` reports a pull request,
the task is in "Review" in TaskBrigade, and the commit author is `claude-desktop` or `codex-desktop`.

## Notes

- Set the runtime by where the model runs: both Claude Desktop and Codex send code to a cloud provider, so `cloud` –
  otherwise the privacy rule for "local agents only" projects does not protect anything.
- A task's model tier or concrete model is shown but not applied: the model is chosen in the app (see
  [cli-agents.md](cli-agents.md#model-per-task-tiers)).
- The Git identity (author, access) only applies to the repositories under the respective work folder; your global
  Git configuration stays untouched.

## See also

- [Workflow tools](workflows-api.md#mcp) – inspect templates, preview, start and control runs.
- [CLI agents on a second machine](cli-agents.md) – unattended agents with the runner.
- [Agent workflow and API](agent-skill.md)
