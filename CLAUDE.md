# Notes for Claude Code

- Read `brain.md` first – it holds the stack, test commands, decisions and known pitfalls.
- Work on a feature branch (e.g. `task/<TASK-NR>-…` when the work comes from TaskBrigade); never push to `main`.
- Run `uv run pytest -q` before submitting; DB tests only against a `*_test` database (see `brain.md`).
- Do not edit `brain.md` directly. Record new findings in `brain-changes/<TASK-NR>.md` (`## add: <section>`;
  `## change:`/`## remove:` with the exact old text; format in `brain-changes/README.md`). TaskBrigade applies the
  file to `brain.md` when the task is approved, in approval order.
- Do not edit `CHANGELOG.md` directly either: add `changelog.d/<TASK-NR>.md` (bullet points only); TaskBrigade
  appends it under `## Unreleased` on approval.
- Do not add secrets, real hostnames/IPs, personal names or internal project keys to the repository.
