# Screenshots

Images of the web UI, generated from a throwaway Docker demo with invented data (project `DEMO`, humans
`admin`/`alex`, agents with generic names). Light theme, 256-colour PNG; desktop 1440×900, `*-mobile.png` 390×844 (touch, mobile layout, 2× pixels).

| File | Content | Referenced in |
|---|---|---|
| `inbox.png` | Inbox with a workflow approval, pull requests waiting for review and a question | [README.md](../../README.md) |
| `backlog.png` | Backlog with filters | [README.md](../../README.md) |
| `kanban.png` | Kanban board with tasks in every status | [README.md](../../README.md) |
| `planning.png` | Planning: a released and a planned sprint with their tasks | [README.md](../../README.md) |
| `projects.png` | Projects list | [README.md](../../README.md) |
| `task-detail.png` | Task detail: result, open points, properties, activity, pull request link | [README.md](../../README.md), [docs/agent-skill.md](../agent-skill.md) |
| `sprint-editor.png` | Sprint editor of a planned sprint | [README.md](../../README.md) |
| `workflows-list.png` | *Workflows → Templates*: two templates, their last runs, the start templates | [README.md](../../README.md), [docs/workflows-ui.md](../workflows-ui.md) |
| `workflows-editor.png` | Workflow editor with the opened last step and its placeholder list | [README.md](../../README.md), [docs/workflows-ui.md](../workflows-ui.md) |
| `workflows-run.png` | Run page: first step merged, approval waiting, notification pending | [README.md](../../README.md), [docs/workflows-ui.md](../workflows-ui.md) |
| `profile-notifications.png` | *Profile*: Telegram and email channels, event × channel matrix | [README.md](../../README.md) |
| `agents.png` | *Admin → Users & agents* (users and agents; token prefixes masked) | [README.md](../../README.md) |
| `inbox-mobile.png` | Inbox on a smartphone with the bottom tab bar | [README.md](../../README.md) |
| `kanban-mobile.png` | Kanban on a smartphone: status column picker, one column | [README.md](../../README.md) |
| `task-detail-mobile.png` | Task detail on a smartphone: actions and details/discussion tabs | [README.md](../../README.md) |
| `backlog-mobile.png` | Backlog as cards on a smartphone | – |
| `workflows-history.png`, `workflows-kanban.png`, `workflows-inbox.png`, `workflows-task.png` | Run history, Kanban with workflow tasks, Inbox group, task page with the run chain | [docs/workflows-ui.md](../workflows-ui.md) |

The last four `workflows-*.png` images (1366 px wide) were taken by hand from a local demo with invented data; they
are not part of `screenshots.py` yet. The demo workflows (`release-notes` with one run, `kpi-report` with a schedule)
are imported from YAML by `screenshot_seed.py`; `release-notes` is the minimal example shown in the README, so a
change of the publish checks that breaks it fails the screenshot run.

## Regenerating

```sh
python3 deploy/release/screenshots.py          # writes docs/screenshots/*.png
python3 deploy/release/screenshots.py --keep   # keep the demo stack afterwards (URL and login are printed)
```

The script starts `docker compose --profile gitea` as its own Compose project `tbr-shots` (ports 18090/13300/12222,
the smoke test's subnet – do not run both at once), fills it with `deploy/release/screenshot_seed.py` and takes the
screenshots with Playwright in `mcr.microsoft.com/playwright/python`. It needs Docker with Compose v2.24+ and
Python 3 on the host, and removes containers and volumes again unless `--keep` is given. Page lists and viewports are
at the top of `screenshots.py`; demo content is in `screenshot_seed.py`. Look at every image before committing it.

## Still missing

These references stay commented out (`<!-- ![…](screenshots/x.png) -->`) until the image exists; they show tools
outside the web UI and are taken by hand:

| File | Content | Referenced in |
|---|---|---|
| `runner-tmux.png` | tmux session `taskbrigade` with an agent working on a task | [docs/cli-agents.md](../cli-agents.md) |
| `desktop-mcp.png` | Claude Desktop or Codex Desktop calling `start_task` | [docs/desktop-apps-mcp.md](../desktop-apps-mcp.md) |
