// Task forms (task_new.html `#new-task-form`, task_detail.html `#edit`): "Edit / Preview" tabs of the Markdown
// fields, sprint choices of the selected project (new task), the summary line of the folded "Model & execution"
// section, folded sections that open when a field inside is invalid, and the release check of a draft (what is
// still missing, judged from the values in the editor).
(function () {
  function tabs(field) {
    const area = field.querySelector("textarea"), pane = field.querySelector(".md-preview-pane");
    const buttons = [...field.querySelectorAll("[data-md-tab]")];
    // The preview button also fetches the rendered Markdown (hx-post); the text stays in the textarea.
    const show = (which, focus) => {
      for (const b of buttons) {
        const on = b.dataset.mdTab === which;
        b.setAttribute("aria-selected", on ? "true" : "false");
        b.tabIndex = on ? 0 : -1;
        if (on && focus) b.focus();
      }
      area.hidden = which !== "edit";
      pane.hidden = which !== "preview";
      if (which === "edit" && !focus) area.focus();
    };
    buttons.forEach((b, i) => {
      b.addEventListener("click", () => show(b.dataset.mdTab, false));
      b.addEventListener("keydown", (e) => {  // Arrow keys move between the two tabs (ARIA tabs pattern).
        if (e.key !== "ArrowLeft" && e.key !== "ArrowRight") return;
        e.preventDefault();
        const next = buttons[(i + (e.key === "ArrowRight" ? 1 : buttons.length - 1)) % buttons.length];
        next.click();
        show(next.dataset.mdTab, true);
      });
    });
  }

  function sprints(form) {
    const sprint = document.getElementById("new-task-sprint"), project = form.elements.project;
    if (!sprint || !project) return;
    const sync = () => {
      for (const g of sprint.querySelectorAll("optgroup")) g.hidden = g.disabled = g.dataset.project !== project.value;
      if (sprint.selectedOptions[0] && sprint.selectedOptions[0].parentElement.disabled) sprint.value = "";
    };
    project.addEventListener("change", sync);
    sync();
  }

  function modelSummary(form) {
    const summary = form.querySelector("[data-model-summary]");
    const tier = form.elements.model_tier, model = form.elements.model;
    if (!summary || !tier || !model) return;
    const text = (s) => (s && s.value && s.value !== "none" ? s.selectedOptions[0].textContent.trim() : "");
    const sync = () => {
      summary.textContent = [text(tier), text(model)].filter(Boolean).join(" · ") || summary.dataset.default;
    };
    tier.addEventListener("change", sync);
    model.addEventListener("change", sync);
    if (form.elements.assigned_to) form.elements.assigned_to.addEventListener("change", sync);
  }

  function releaseCheck(form) {
    const box = document.querySelector("[data-release-check]");
    const publish = document.querySelector("form[data-publish]");
    if (!box || !publish) return;
    const button = publish.querySelector("[data-rc-button]");
    const unsaved = publish.querySelector("[data-rc-unsaved]"), saved = publish.querySelector("[data-rc-saved]");
    const filled = (name) => !!(form.elements[name] && form.elements[name].value.trim());
    // Same snapshot as form_guard.js: unsaved edits are saved before releasing (then_publish).
    const snap = () => new URLSearchParams([...new FormData(form)].filter(([k, v]) => k !== "csrf_token" &&
      typeof v === "string")).toString();
    const initial = snap();
    const sync = () => {
      const missing = {assigned_to: !filled("assigned_to"), description: !filled("description"),
                       acceptance_criteria: !filled("acceptance_criteria")};
      for (const li of box.querySelectorAll("[data-missing]")) li.hidden = !missing[li.dataset.missing];
      const blocked = missing.description || missing.acceptance_criteria;
      box.dataset.state = blocked ? "blocked" : (missing.assigned_to ? "unassigned" : "ready");
      button.disabled = blocked;
      if (missing.assigned_to) publish.dataset.unassigned = "1"; else delete publish.dataset.unassigned;
      const dirty = snap() !== initial;
      unsaved.hidden = !dirty;
      saved.hidden = dirty;
    };
    form.addEventListener("input", sync);
    form.addEventListener("change", sync);
    sync();
  }

  function init(form) {
    form.querySelectorAll("[data-md-field]").forEach(tabs);
    sprints(form);
    modelSummary(form);
    // The browser cannot show the message of an invalid field inside a closed <details>: open it first.
    form.addEventListener("invalid", (e) => {
      const fold = e.target.closest("details");
      if (fold) fold.open = true;
      // Narrow screens: the edit form may be folded (read view) or behind the "Discussion" tab.
      const tab = document.getElementById("tab-details"), toggle = document.querySelector(`[aria-controls="${form.id}"]`);
      if (tab && tab.getAttribute("aria-selected") !== "true") tab.click();
      if (toggle && toggle.getAttribute("aria-expanded") !== "true") toggle.click();
    }, true);
  }

  document.addEventListener("DOMContentLoaded", () => {
    const form = document.getElementById("new-task-form") || document.getElementById("edit");
    if (!form) return;
    init(form);
    if (form.id === "edit") releaseCheck(form);
  });
})();
