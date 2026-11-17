# GitHub "About" box for taskbrigade/taskbrigade

Values for the About box of <https://github.com/taskbrigade/taskbrigade> (gear icon next to "About"). Only the
account owner can change them; nothing here is applied automatically.

## Description

The current description says "…AI coding agents…", which undersells the project: tasks are not limited to code.

> Agile project management for mixed teams of humans and AI agents – backlog, kanban, sprints, workflows and review. Self-hosted.

(127 characters; GitHub allows 350.)

## Website

<https://taskbrigade.dev>

## Topics

14 of at most 20:

| Topic | Why |
|---|---|
| `ai-agents` | Core subject: work for AI agents. |
| `agent-orchestration` | Assigning, leasing and reviewing tasks across agents and machines. |
| `mcp` | Desktop apps connect through the MCP server `tbr mcp`. |
| `claude-code` | Supported agent (runner). |
| `codex` | Supported agent (runner and desktop app via MCP). |
| `opencode` | Supported agent (runner), common entry point for local models. |
| `kanban` | Kanban board across projects and agents. |
| `project-management` | Backlog, sprints, planning, project owners. |
| `agile` | Agile concepts mapped to agents (see README "How agile maps to agents"). |
| `self-hosted` | Runs on your own server, natively or with Docker Compose. |
| `llm` | Cloud and local language models side by side. |
| `local-llm` | `local_only` projects go only to agents whose model runs on your own hardware. |
| `workflow-automation` | Workflows run task chains by hand, on a schedule or on events. |
| `gitea` | Results are handed in as pull requests in Gitea; the Compose stack can bundle it. |

## Command

Run as the account owner with an authenticated GitHub CLI (`gh auth status`):

```bash
gh repo edit taskbrigade/taskbrigade \
  --description "Agile project management for mixed teams of humans and AI agents – backlog, kanban, sprints, workflows and review. Self-hosted." \
  --homepage "https://taskbrigade.dev" \
  --add-topic ai-agents,agent-orchestration,mcp,claude-code,codex,opencode,kanban,project-management,agile,self-hosted,llm,local-llm,workflow-automation,gitea
```

`--add-topic` keeps existing topics; check them afterwards with
`gh repo view taskbrigade/taskbrigade --json description,homepageUrl,repositoryTopics` and remove outdated ones with
`gh repo edit taskbrigade/taskbrigade --remove-topic <topic>`.
