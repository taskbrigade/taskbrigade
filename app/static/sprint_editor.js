// Sprint editor (sprint_editor.html): task headings on the left, details of the selected entry on the right.
// The list lives in the hidden field `entries` of #sprint-form (JSON, display order); `removed` lists drafts to
// cancel. Detail fields sit outside the form, so form_guard.js sees a change only once the JSON changes.
(function () {
  const root = document.getElementById("sprint-editor");
  const form = document.getElementById("sprint-form");
  if (!root || !form) return;
  const entriesField = form.elements.entries, removedField = form.elements.removed;
  const list = document.getElementById("se-items"), add = document.getElementById("se-add");
  const detail = document.getElementById("se-detail"), fields = document.getElementById("se-fields");
  const labels = JSON.parse(root.dataset.statusLabels || "{}");
  const FIELDS = ["title", "description", "acceptance_criteria", "assigned_to", "prio", "due_date", "model_tier",
                  "model", "reviewer"];
  const input = (f) => detail.querySelector(`[data-field="${f}"], textarea[name="${f}"]`);
  let entries = JSON.parse(entriesField.value || "[]");
  let removed = JSON.parse(removedField.value || "[]");
  let selected = entries.length ? 0 : -1;

  const isDraft = (e) => (e.status || "draft") === "draft";
  const missing = (e) => [["description", "Description"], ["acceptance_criteria", "Acceptance criteria"]]
    .filter(([f]) => !(e[f] || "").trim()).map(([, label]) => tmT(label));

  function save() {
    // Write the model into the form; form_guard.js compares it with the state at page load.
    entriesField.value = JSON.stringify(entries);
    removedField.value = JSON.stringify(removed);
  }

  function itemHtml(e, i) {
    const li = document.createElement("li");
    li.className = "se-item" + (i === selected ? " selected" : "");
    li.tabIndex = 0;
    li.dataset.index = i;
    const handle = document.createElement("span");
    handle.className = "se-handle"; handle.textContent = "⠿"; handle.title = tmT("Drag to reorder");
    const body = document.createElement("span");
    body.className = "se-body";
    const title = document.createElement("span");
    title.className = "se-title"; title.textContent = e.title;
    const state = document.createElement("small");
    if (!isDraft(e)) {
      const badge = document.createElement("span");
      badge.className = "badge st-" + e.status; badge.textContent = labels[e.status] || e.status;
      state.className = "se-ready";
      state.append(badge, " " + (e.task_nr || ""));
    } else {
      const m = missing(e);
      state.className = "se-ready " + (m.length ? "missing" : "ok");
      state.textContent = m.length ? tmT("missing: %(fields)s", { fields: m.join(", ") }) : tmT("ready");
    }
    body.append(title, state);
    li.append(handle, body);
    if (isDraft(e)) {
      const del = document.createElement("button");
      del.type = "button"; del.className = "se-del"; del.textContent = "×";
      del.title = del.ariaLabel = tmT("Remove");
      li.append(del);
    }
    return li;
  }

  function render() {
    list.replaceChildren(...entries.map(itemHtml));
    document.getElementById("se-count").textContent = `(${entries.length})`;
    document.getElementById("se-list-empty").hidden = entries.length > 0;
  }

  // Concrete model: the allow list of the entry's agent (plus the stored value, so nothing is lost silently).
  const modelSelect = input("model");
  let agentModels = {};
  try { agentModels = JSON.parse(modelSelect.dataset.models || "{}"); } catch (err) { agentModels = {}; }
  function modelOptions(e) {
    const agent = agentModels[e.assigned_to || ""] || { allowed: [], applies: true };
    const choices = agent.allowed.slice();
    if (e.model && !choices.includes(e.model)) choices.unshift(e.model);
    while (modelSelect.options.length > 1) modelSelect.remove(1);
    for (const m of choices) modelSelect.add(new Option(m, m));
    document.getElementById("se-model-hint").hidden = !e.assigned_to || agent.applies;
  }

  function show() {
    // Fill the detail panel with the selected entry.
    const e = entries[selected];
    document.getElementById("se-empty").hidden = !!e;
    fields.hidden = !e;
    if (!e) return;
    const readonly = !isDraft(e);
    modelOptions(e);
    for (const f of FIELDS) {
      const el = input(f);
      el.value = e[f] == null ? "" : String(e[f]);
      el.disabled = readonly;
    }
    document.getElementById("se-readonly").hidden = !readonly;
    document.getElementById("se-open").href = e.task_nr ? "/tasks/" + encodeURIComponent(e.task_nr) : "#";
    for (const d of detail.querySelectorAll("details.md-preview")) d.open = false;
  }

  function select(i, focus) {
    selected = Math.max(-1, Math.min(i, entries.length - 1));
    for (const li of list.children) li.classList.toggle("selected", Number(li.dataset.index) === selected);
    show();
    if (focus && list.children[selected]) list.children[selected].focus();
  }

  function addTitles(titles) {
    const clean = titles.map((t) => t.replace(/^\s*(?:[-*•]|\d+[.)])\s+/, "").trim().slice(0, 200)).filter(Boolean);
    if (!clean.length) return;
    for (const title of clean) {
      entries.push({ task_nr: null, row_version: null, status: "draft", title, description: "",
                     acceptance_criteria: "", assigned_to: "", prio: 3, due_date: "", model_tier: "", model: "",
                     reviewer: "" });
    }
    if (selected < 0) selected = entries.length - clean.length;
    save(); render(); show();
  }

  function remove(i) {
    const e = entries[i];
    if (!e || !isDraft(e)) return;
    if (e.task_nr) {
      if (!confirm(tmT("%(task_nr)s is cancelled when you save the sprint. Remove it?", { task_nr: e.task_nr }))) return;
      removed.push(e.task_nr);
    }
    entries.splice(i, 1);
    if (selected >= entries.length || selected > i) selected -= 1;
    if (selected < 0 && entries.length) selected = 0;
    save(); render(); show();
  }

  function move(from, to) {
    if (to < 0 || to >= entries.length || from === to) return;
    const sel = entries[selected];
    entries.splice(to, 0, entries.splice(from, 1)[0]);
    selected = entries.indexOf(sel);
    save(); render();
  }

  // --- Adding: Enter adds one heading; pasting several lines adds one entry per line.
  add.addEventListener("keydown", (ev) => {
    if (ev.key !== "Enter") return;
    ev.preventDefault();
    addTitles([add.value]);
    add.value = "";
  });
  add.addEventListener("paste", (ev) => {
    const text = (ev.clipboardData || window.clipboardData).getData("text");
    if (!/[\r\n]/.test(text.trim())) return;
    ev.preventDefault();
    addTitles((add.value + text).split(/\r?\n/));
    add.value = "";
  });

  // --- List: select, remove, rename (double-click), keyboard (Alt+↑/↓ moves, Delete removes).
  list.addEventListener("click", (ev) => {
    const li = ev.target.closest(".se-item");
    if (!li) return;
    const i = Number(li.dataset.index);
    if (ev.target.closest(".se-del")) remove(i); else select(i);
  });
  list.addEventListener("dblclick", (ev) => {
    const li = ev.target.closest(".se-item");
    const e = li && entries[Number(li.dataset.index)];
    if (!e || !isDraft(e) || ev.target.closest(".se-del")) return;
    const title = prompt(tmT("New title:"), e.title);
    if (title == null || !title.trim()) return;
    e.title = title.trim().slice(0, 200);
    save(); render(); show();
  });
  list.addEventListener("keydown", (ev) => {
    const li = ev.target.closest(".se-item");
    if (!li || ev.target !== li) return;
    const i = Number(li.dataset.index);
    if (ev.altKey && (ev.key === "ArrowUp" || ev.key === "ArrowDown")) {
      ev.preventDefault();
      select(i);
      move(i, i + (ev.key === "ArrowUp" ? -1 : 1));
      select(selected, true);
    } else if (ev.key === "ArrowUp" || ev.key === "ArrowDown") {
      ev.preventDefault();
      select(i + (ev.key === "ArrowUp" ? -1 : 1), true);
    } else if (ev.key === "Enter" || ev.key === " ") {
      ev.preventDefault();
      select(i);
    } else if (ev.key === "Delete") {
      ev.preventDefault();
      remove(i);
      select(selected, true);
    }
  });
  if (window.Sortable) {
    Sortable.create(list, {
      handle: ".se-handle", animation: 150, ghostClass: "se-ghost",
      onEnd: (ev) => { move(ev.oldIndex, ev.newIndex); show(); },
    });
  }

  // --- Details: every change goes straight into the model, so switching entries loses nothing.
  for (const f of FIELDS) {
    input(f).addEventListener("input", (ev) => {
      const e = entries[selected];
      if (!e || !isDraft(e)) return;
      e[f] = f === "prio" ? Number(ev.target.value) : ev.target.value;
      if (f === "assigned_to") {  // A model the new agent does not allow is dropped, as on the server.
        const agent = agentModels[e.assigned_to || ""] || { allowed: [] };
        if (e.model && !agent.allowed.includes(e.model)) e.model = "";
        modelOptions(e);
        modelSelect.value = e.model || "";
      }
      save();
      const li = list.children[selected];
      if (li) li.replaceWith(itemHtml(e, selected));
    });
  }

  // Prio from order: spread the drafts over prio 1–5 in list order.
  document.getElementById("se-prio-order").addEventListener("click", () => {
    const drafts = entries.filter(isDraft), n = drafts.length;
    drafts.forEach((e, i) => { e.prio = n <= 5 ? i + 1 : 1 + Math.floor((i * 5) / n); });
    save(); render(); show();
  });

  // "Save and release" only for projects the user may release (admins and the owner).
  const release = document.getElementById("se-release"), project = form.elements.project;
  if (release && project && project.tagName === "SELECT") {
    const manage = JSON.parse(form.dataset.manage || "[]");
    const sync = () => { release.hidden = !manage.includes(project.value) && !(project.value === "" && manage.length); };
    project.addEventListener("change", sync);
    sync();
  }

  render();
  show();
})();
