# brain-changes – Projektwissen ohne Merge-Konflikte

`{{ project.brain_path }}` wird nicht direkt bearbeitet. Wer bei einem Task neue Erkenntnisse hat, legt
`brain-changes/<TASK-NR>.md` an (eine Datei pro Task). TaskBrigade wendet sie beim Abnehmen auf
`{{ project.brain_path }}` an – in der Reihenfolge, in der Tasks abgenommen werden – und löscht die Datei danach.

```markdown
## add: Bekannte Stolperfallen
- Neuer Eintrag. Wird ans Ende des Abschnitts gehängt; gibt es den Abschnitt nicht, wird er angelegt.

## change: Bekannte Stolperfallen
### old
- Bisheriger Eintrag – exakt so, wie er in brain.md steht.
### new
- Neue Fassung.

## remove: Glossar
- Zu entfernender Eintrag – exakt so, wie er in brain.md steht.
```

- Der Abschnitt ist die `## …`-Überschrift in `{{ project.brain_path }}`; der Anfang genügt
  („Wichtige Entscheidungen“ passt auf „Wichtige Entscheidungen (ADR-Kurzform …)“).
- `add` führt nie zu Konflikten. Steht der alte Text eines `change`/`remove` beim Abnehmen nicht mehr so in
  `{{ project.brain_path }}` (weil ein anderer Task ihn vorher geändert hat), wird nicht gemergt: Der Task geht
  zurück, und die Änderung wird auf den neuen Stand bezogen.
