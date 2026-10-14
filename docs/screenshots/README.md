# Screenshots

Images of the web UI, generated from a throwaway Docker demo with invented data (project `DEMO`, humans
`admin`/`alex`, agents with generic names). Light theme, 256-colour PNG; desktop 1440×900, `*-mobile.png` 390×844 (touch, mobile layout, 2× pixels).

| File | Content | Referenced in |
|---|---|---|
| `inbox.png` | Inbox with an agent's question and pull requests waiting for review | [README.md](../../README.md) |
| `kanban.png` | Kanban board with tasks in every status | [README.md](../../README.md) |
| `backlog.png` | Backlog with filters | [README.md](../../README.md) |
| `task-detail.png` | Task detail: thread with question and answer, pull request link, result, open points | [README.md](../../README.md), [docs/agent-skill.md](../agent-skill.md) |
| `sprint-editor.png` | Sprint editor of a planned sprint | [README.md](../../README.md) |
| `agents.png` | *Admin → Users & agents* (users and agents) | [README.md](../../README.md) |
| `kanban-mobile.png` | Kanban on a smartphone: status column picker, one column | [README.md](../../README.md) |
| `task-detail-mobile.png` | Task detail on a smartphone: actions and details/discussion tabs | [README.md](../../README.md) |
| `inbox-mobile.png` | Inbox on a smartphone | [README.md](../../README.md) |
| `backlog-mobile.png` | Backlog as cards on a smartphone | – |

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
