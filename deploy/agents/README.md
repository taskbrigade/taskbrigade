# Agent integrations

All agents use the same pattern: a cheap, LLM-free check on a schedule calls `tbr prepare`.
Only if it claimed a task (worktree + TASK.md prepared) does the LLM start with the assignment.
The agent's identity lives in `~/taskbrigade-work/<agent>/.taskbrigade.toml` (0600).

| Agent | Mechanism | Files |
|---|---|---|
| Hermes (`hermes`) | Hermes cron, `--script` with wake gate (`{"wakeAgent": false}`) | `hermes/taskbrigade-prepare.sh` → `~/.hermes/scripts/`, `hermes/taskbrigade/` → `~/.hermes/skills/productivity/` |
| OpenClaw (`openclaw-agent`) | OpenClaw automation with `--trigger-script` (`fire: false/true`) | `openclaw/taskbrigade-trigger.js`, `openclaw/taskbrigade/` and `openclaw/taskbrigade-telegram/` (orchestration from a chat) → `~/.openclaw/workspace/skills/` |
| Claude Code, Codex, OpenCode | systemd user timer → `tbr run` (agent interactive in a tmux window, `tbr session` with heartbeat and closing step) | `runner/` – guide: `docs/cli-agents.md`; Hermes/OpenClaw: `docs/agent-frameworks.md` |
| Claude Cowork / desktop apps | local MCP server `tbr mcp` | `cowork/taskbrigade/` (skill), `cowork/taskbrigade-skill.zip` – guide: `docs/desktop-apps-mcp.md` |

Setup per agent: create it in TaskBrigade (*Admin → Neuer Agent*, runtime local/cloud to match the model), then on
the agent machine `tbr join <server> <code> --dir ~/taskbrigade-work/<agent> --setup <kind>` with the kind
`claude-code`, `codex`, `opencode`, `hermes`, `openclaw`, `claude-desktop` or `codex-desktop`. `tbr setup <kind>`
installs the files above with the agent's name and paths filled in (backups as `<file>.bak`) and creates the timer,
cron job or automation; `tbr doctor` checks the result. `tbr` ships copies of the Hermes/OpenClaw files and the
Cowork skill zip (`cli/agent_files` in the wheel), so edit them here, not there.

Language: prompts, `TASK.md`, MCP texts and `tbr` messages are English. `language = "de"` in `.taskbrigade.toml`
(or the environment variable `TBR_LANGUAGE=de`) switches them to German. The skills here are English only.

Note: the check already claims the task. If the agent run fails afterwards, the task only falls back to
"Defined" when the lease expires (default 120 min).
