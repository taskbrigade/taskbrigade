// Workflow editor (workflow_editor.html): add, remove and reorder steps and parameters, keep the step summaries
// current, and fill each step's "Insert placeholder" list with what is valid there (parameters, run fields and
// outputs of earlier steps). Every structural change fires `wf-changed`, which re-runs the server checks (HTMX).
(function () {
  const form = document.getElementById("wf-form");
  if (!form) return;
  const steps = document.getElementById("wf-steps");
  const params = document.getElementById("wf-param-rows");
  const stepTemplate = document.getElementById("wf-step-template");
  const paramTemplate = document.getElementById("wf-param-template");
  const basics = JSON.parse(form.dataset.placeholders || "{}");
  const changed = () => document.body.dispatchEvent(new CustomEvent("wf-changed", { bubbles: true }));
  const field = (li, name) => li.querySelector(`[name="s_${name}"]`);

  function outputsOf(li) {
    try {
      const value = JSON.parse(field(li, "outputs").value || "null");
      return value && typeof value === "object" && !Array.isArray(value) ? Object.keys(value) : [];
    } catch (err) { return []; }
  }

  function refresh() {
    // Positions, summary labels and placeholder lists follow the current order.
    const names = [...params.querySelectorAll('[name="p_name"]')].map((i) => i.value.trim()).filter(Boolean);
    let earlier = [];
    [...steps.children].forEach((li, i) => {
      const key = field(li, "key").value.trim();
      const type = field(li, "type");
      li.querySelector("[data-pos]").textContent = String(i + 1);
      li.querySelector("[data-key-label]").textContent = key || "…";
      li.querySelector("[data-title-label]").textContent = field(li, "title").value;
      li.querySelector("[data-type-label]").textContent = type.options[type.selectedIndex].text;
      const role = field(li, "role").value.trim();
      li.querySelector("[data-meta-label]").textContent = role ? role : "";
      const list = li.querySelector("[data-insert-list]");
      const items = [...names, ...(basics.run || []), ...earlier];
      list.replaceChildren(...items.map((path) => {
        const b = document.createElement("button");
        b.type = "button"; b.className = "small outline secondary wf-ph";
        b.textContent = `{{${path}}}`; b.dataset.placeholder = `{{${path}}}`;
        b.title = tmT("Insert %(placeholder)s", { placeholder: b.textContent });
        return b;
      }));
      if (!items.length) list.textContent = tmT("No placeholders available yet.");
      if (key) {
        if ((basics.task_types || []).includes(type.value)) {
          earlier = earlier.concat((basics.builtins || []).map((b) => `steps.${key}.${b}`));
        }
        earlier = earlier.concat(outputsOf(li).map((o) => `steps.${key}.output.${o}`));
      }
    });
  }

  function addStep(type) {
    const li = stepTemplate.content.firstElementChild.cloneNode(true);
    field(li, "type").value = type;
    steps.append(li);
    refresh();
    li.querySelector("details").open = true;
    field(li, "key").focus();
    changed();
  }

  function move(li, delta) {
    const sibling = delta < 0 ? li.previousElementSibling : li.nextElementSibling;
    if (!sibling) return;
    if (delta < 0) steps.insertBefore(li, sibling); else steps.insertBefore(sibling, li);
    refresh();
    li.querySelector(`[data-move="${delta}"]`).focus();
    changed();
  }

  // Remember the last text field per step, so "Insert placeholder" knows where to write.
  steps.addEventListener("focusin", (ev) => {
    if (ev.target.matches("[data-insert-target]")) ev.target.closest("[data-step]").lastField = ev.target;
  });
  steps.addEventListener("click", (ev) => {
    const li = ev.target.closest("[data-step]");
    if (!li) return;
    const ph = ev.target.closest("[data-placeholder]");
    if (ph) {
      const target = li.lastField || field(li, "description");
      const start = target.selectionStart ?? target.value.length, end = target.selectionEnd ?? start;
      target.value = target.value.slice(0, start) + ph.dataset.placeholder + target.value.slice(end);
      target.focus();
      target.setSelectionRange(start + ph.dataset.placeholder.length, start + ph.dataset.placeholder.length);
      target.dispatchEvent(new Event("change", { bubbles: true }));
      return;
    }
    if (ev.target.closest("[data-move]")) move(li, Number(ev.target.closest("[data-move]").dataset.move));
    if (ev.target.closest("[data-remove]") && confirm(tmT("Remove this step?"))) {
      li.remove(); refresh(); changed();
    }
  });
  steps.addEventListener("input", (ev) => { if (ev.target.matches("[data-field]")) refresh(); });
  steps.addEventListener("change", refresh);
  params.addEventListener("input", refresh);
  params.addEventListener("click", (ev) => {
    if (ev.target.closest("[data-remove-param]")) { ev.target.closest("[data-param]").remove(); refresh(); changed(); }
  });
  document.getElementById("wf-add-step").addEventListener("click", () => {
    addStep(document.getElementById("wf-new-type").value);
  });
  document.getElementById("wf-add-param").addEventListener("click", () => {
    const row = paramTemplate.content.firstElementChild.cloneNode(true);
    params.append(row);
    row.querySelector('[name="p_name"]').focus();
    refresh(); changed();
  });
  form.addEventListener("submit", (ev) => {
    const button = ev.submitter;
    if (button && button.dataset.confirm && !confirm(button.dataset.confirm)) ev.preventDefault();
  });
  if (window.Sortable) {
    Sortable.create(steps, { handle: ".wf-handle", animation: 150, ghostClass: "se-ghost",
                             onEnd: () => { refresh(); changed(); } });
  }
  refresh();
})();
