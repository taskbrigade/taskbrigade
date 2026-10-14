# brain-changes – project knowledge without merge conflicts

`brain.md` is not edited directly. When a task produces new findings, add `brain-changes/<TASK-NR>.md` (one file per
task). TaskBrigade applies it to `brain.md` when the task is approved – in approval order – and deletes the file.

```markdown
## add: Known pitfalls
- New entry. Appended to the end of the section; a missing section is created.

## change: Known pitfalls
### old
- Existing entry – exactly as it appears in brain.md.
### new
- New wording.

## remove: Glossary
- Entry to remove – exactly as it appears in brain.md.
```

- The section is the `## …` heading in `brain.md`; its beginning is enough ("Important decisions" matches
  "Important decisions (ADR short form …)").
- `add` never conflicts. If the old text of a `change`/`remove` is no longer in `brain.md` when the task is approved
  (because another task changed it first), the pull request is not merged; the task goes back to its agent, who
  rebases the change onto the current text.
