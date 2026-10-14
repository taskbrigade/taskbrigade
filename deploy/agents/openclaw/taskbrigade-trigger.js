// OpenClaw condition trigger (openclaw automations add --trigger-script …).
// Claims a task with `tbr prepare`. No task → fire:false (no agent run, no tokens).
// Task claimed → fire:true, the prepared assignment becomes the message to the agent. A reviewer agent may get a
// review job instead ("# TaskBrigade review job <NR>", REVIEW.md) – same handling, the skill tells them apart.
// Every other outcome throws: a broken check must never look like "nothing to do".
// --resume-own: a task of your own "in progress" is resumed on the next check instead of hanging until the lease ends
// (2 h) – e.g. if the first clone of a large repo took longer than 25 s (the trigger gave up, tbr prepare kept
// running in the background and claimed the task) or the agent run failed at the model.
// OpenClaw does not start an automation while its previous run is still active (already-running).
const root = "$HOME/taskbrigade-work/openclaw-agent";
// The CLI (tbr). A machine still on the old package (before TaskBrigade 0.5) only has `tm` – same commands and output.
const tbr = "$HOME/.local/bin/tbr";
// exec moves commands to the background after yieldMs (default 10 s); a trigger evaluation may take 30 s.
// The first clone of a project can take longer – then say so clearly instead of "no output".
const res = await exec({
  command: `cd "${root}" && TBR="${tbr}" && { [ -x "$TBR" ] || TBR="$HOME/.local/bin/tm"; } && ` +
    `"$TBR" prepare --work-root "${root}" --wake-gate --resume-own`,
  yieldMs: 25000,
});
if (res?.exitCode === undefined || res?.exitCode === null) {
  throw new Error(`tbr prepare ran longer than 25 s and keeps running in the background (status ${res?.status ?? "?"}). ` +
    "A task claimed meanwhile is resumed on the next check (--resume-own).");
}
const out = String(res?.aggregated ?? res?.stdout ?? "").trim();
const lines = out.split("\n").map((l) => l.trim()).filter(Boolean);
const last = lines[lines.length - 1] ?? "";
if (last === '{"wakeAgent": false}') {
  json({ fire: false });
} else if (/^# (TaskBrigade|Taskmeister)(-Auftrag| assignment| review job|-Review-Auftrag) /m.test(out)) {
  // -Auftrag/-Review-Auftrag: language = "de"; Taskmeister: tbr < 0.8
  json({ fire: true, message: out });
} else {
  throw new Error(`tbr prepare failed (exit ${res?.exitCode ?? "?"}): ${out.slice(-1500)}`);
}
