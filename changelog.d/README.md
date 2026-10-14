# changelog.d – CHANGELOG entries without merge conflicts

`CHANGELOG.md` is not edited directly. A task that deserves a changelog entry adds `changelog.d/<TASK-NR>.md` with
Markdown bullet points (no `#`/`##` headings). When the task is approved, TaskBrigade appends the fragment to the
`## Unreleased` section of `CHANGELOG.md` (creating the section below the title if needed) and deletes the file.

```markdown
- Docker: `compose.yaml` with app and MariaDB; migrations run on start.
```
