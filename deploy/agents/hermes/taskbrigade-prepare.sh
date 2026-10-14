#!/usr/bin/env bash
# Hermes cron pre-run (→ ~/.hermes/scripts/): claims a task with `tbr prepare`.
# No task → {"wakeAgent": false} → Hermes starts no LLM. Otherwise: the assignment (or, for a reviewer agent, a review
# job with REVIEW.md) on stdout → prompt.
set -euo pipefail
WORK_ROOT="${TBR_WORK_ROOT:-$HOME/taskbrigade-work/hermes}"
TBR="${TBR_BIN:-$HOME/.local/bin/tbr}"
# A machine still on the old package (before TaskBrigade 0.5) only has `tm` – same commands, same output.
[[ -x "$TBR" || -n "${TBR_BIN:-}" ]] || TBR="$HOME/.local/bin/tm"
cd "$WORK_ROOT"   # holds .taskbrigade.toml with hermes' identity
# --resume-own: if the last run ended at the step limit, the task stays in progress – resume it here
# (safe, because only this cron job works on tasks for hermes)
exec "$TBR" prepare --work-root "$WORK_ROOT" --wake-gate --resume-own
