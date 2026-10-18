// Prevent lost input in forms with data-guard (edit task, new task, sprint editor, workflow editor, settings,
// project).
// - Leaving the page (quick capture, links, other forms) with unsaved input:
//   ask to save and continue, then ask to discard and continue or stay.
// - Publishing (data-publish) with unsaved input: save and publish in one step.
// - "Cancel" (data-discard): confirm, then reload the stored values.
// - Closing or reloading the tab: use the browser's standard warning.
// - [data-dirty-indicator] inside a guarded form is shown while it has unsaved input.
(function () {
  const guarded = [...document.querySelectorAll("form[data-guard]")];
  if (!guarded.length) return;
  const csrf = JSON.parse(document.body.getAttribute("hx-headers") || "{}")["X-CSRF-Token"];
  const snapshot = (f) => {
    const fields = [...new FormData(f)].filter(([k, v]) => k !== "csrf_token" && typeof v === "string");
    const files = [...f.querySelectorAll("input[type=file]")].map((i) => i.files.length).join(",");
    return new URLSearchParams(fields).toString() + "|" + files;
  };
  guarded.forEach((f) => { f.dataset.initial = snapshot(f); });
  const dirtyForms = () => guarded.filter((f) => snapshot(f) !== f.dataset.initial);
  let leaving = false;

  async function save(f) {
    // Save via fetch so the originally requested action can continue afterward.
    const r = await fetch(f.action, { method: "POST", body: new FormData(f), headers: { "X-CSRF-Token": csrf } });
    const url = new URL(r.url);
    const ok = r.ok && !url.searchParams.get("err") && !(f.dataset.guard === "new" && url.pathname === "/tasks/new");
    if (!ok) {
      alert(tmT("Saving failed – please check the message on the page. The action is cancelled."));
      if (r.status >= 400) { document.open(); document.write(await r.text()); document.close(); }
      return false;
    }
    f.dataset.initial = snapshot(f);
    return true;
  }

  async function guard(proceed) {
    const dirty = dirtyForms();
    if (!dirty.length) return proceed();
    const kind = dirty[0].dataset.guard;
    const what = kind === "new" ? tmT("The new task has not been created yet.") :
      kind === "sprint" ? tmT("The changes to this sprint are not saved yet.") :
      kind === "workflow" ? tmT("The changes to this workflow are not saved yet.") :
      kind === "settings" ? tmT("The changes to the settings are not saved yet.") :
      kind === "project" ? tmT("The changes to this project are not saved yet.") :
      tmT("The changes to this task are not saved yet.");
    if (confirm(what + "\n\n" + tmT("Save now?\n(OK = save and continue, Cancel = do not save)"))) {
      for (const f of dirty) { if (!(await save(f))) return; }
      leaving = true;
      return proceed();
    }
    if (confirm(tmT("Discard the input and continue?\n(Cancel = stay on this page)"))) {
      leaving = true;
      return proceed();
    }
  }

  // Publishing: save unsaved edits first, server-side in one step (then_publish).
  document.querySelectorAll("form[data-publish]").forEach((pf) => {
    pf.addEventListener("submit", (e) => {
      const edit = guarded.find((f) => f.dataset.guard === "edit");
      if (!edit || snapshot(edit) === edit.dataset.initial) { leaving = true; return; }
      e.preventDefault();
      if (!confirm(tmT("The changes to this task are not saved yet.") + "\n\n" + tmT("Save and then release?"))) return;
      const flag = document.createElement("input");
      flag.type = "hidden"; flag.name = "then_publish"; flag.value = "1";
      edit.appendChild(flag);
      leaving = true;
      edit.submit();
    });
  });

  const showDirty = () => guarded.forEach((f) => {
    const dirty = snapshot(f) !== f.dataset.initial;
    f.querySelectorAll("[data-dirty-indicator]").forEach((el) => { el.hidden = !dirty; });
  });
  ["input", "change"].forEach((type) => document.addEventListener(type, showDirty));

  // Submitting a guarded form is an intentional save.
  guarded.forEach((f) => f.addEventListener("submit", () => { leaving = true; }));

  // Other forms (quick capture, comment, actions, sign out, etc.).
  document.addEventListener("submit", (e) => {
    const f = e.target;
    if (leaving || e.defaultPrevented || guarded.includes(f) || f.hasAttribute("data-publish")) return;
    if (!dirtyForms().length) return;
    e.preventDefault();
    guard(() => { leaving = true; HTMLFormElement.prototype.submit.call(f); });
  }, false);

  // "Cancel" (data-discard): reload the stored values after one confirmation.
  document.addEventListener("click", (e) => {
    const a = e.target.closest("a[data-discard]");
    if (!a) return;
    e.preventDefault();
    // data-discard="confirm": the page shows input that was not saved (error re-render).
    const unsaved = dirtyForms().length || a.dataset.discard === "confirm";
    if (unsaved && !confirm(tmT("Discard the unsaved changes?"))) return;
    leaving = true;
    window.location.href = a.href;
  });

  // Links to other pages.
  document.addEventListener("click", (e) => {
    const a = e.target.closest("a[href]");
    if (a && a.hasAttribute("data-discard")) return;
    if (!a || leaving || a.target === "_blank" || e.ctrlKey || e.metaKey || e.shiftKey) return;
    const href = a.getAttribute("href");
    if (!href || href.startsWith("#") || href.startsWith("javascript:")) return;
    if (!dirtyForms().length) return;
    e.preventDefault();
    guard(() => { window.location.href = a.href; });
  });

  window.addEventListener("beforeunload", (e) => {
    if (!leaving && dirtyForms().length) { e.preventDefault(); e.returnValue = ""; }
  });
})();
