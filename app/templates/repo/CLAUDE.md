# Hinweise für Claude Code ({{ project.name }})

- Lies zuerst `{{ project.brain_path }}`.
- Arbeite nur auf deinem Task-Branch (`task/<TASK-NR>-…`). Nie direkt auf `{{ project.default_branch }}` pushen.
- Neue Erkenntnisse nicht direkt in `{{ project.brain_path }}`, sondern in `brain-changes/<TASK-NR>.md`
  (Format: `brain-changes/README.md`) – TaskBrigade übernimmt sie beim Abnehmen.
- Gibt es eine `CHANGELOG.md`: Einträge nicht direkt dort, sondern in `changelog.d/<TASK-NR>.md` (nur Stichpunkte).
- Rückfragen: `tbr ask <TASK-NR> "…"`. Fertig: `tbr submit <TASK-NR> --branch … --sha … --summary "…"`.
