// Planning (sprint accordion): expand/collapse each sprint card (state per user in localStorage), drag a task row
// into another sprint (posts /planning/move → sprints.assign; inside a planned sprint the order sets rank and draft
// prio) and the "Move to sprint …" row menu as the touch/keyboard fallback. Sprint actions open <dialog>s (the
// release preview is loaded when opened). Planned and running sprints start expanded, closed ones collapsed; a
// stored choice wins.
(function () {
  const list = document.querySelector(".plan-list");
  const msg = document.getElementById("planning-msg");
  if (!list) return;
  const csrf = JSON.parse(document.body.getAttribute("hx-headers") || "{}")["X-CSRF-Token"];
  const storeKey = "tb-plan-open:" + (list.dataset.project || "");
  const cards = () => [...list.querySelectorAll(".plan-sprint")];

  function flash(text, kind) {
    msg.textContent = text;
    msg.className = "banner " + (kind || "error");
    msg.focus();
  }

  function expanded() {
    try { return JSON.parse(localStorage.getItem(storeKey) || "null"); } catch (e) { return null; }
  }

  function remember(id, open) {
    const state = expanded() || {};
    state[id] = open;
    try { localStorage.setItem(storeKey, JSON.stringify(state)); } catch (e) { /* Private mode. */ }
  }

  function setOpen(card, open, store) {
    const btn = card.querySelector(".plan-toggle");
    const body = document.getElementById(btn.getAttribute("aria-controls"));
    btn.setAttribute("aria-expanded", open ? "true" : "false");
    card.classList.toggle("is-open", open);
    if (body) body.hidden = !open;
    if (store) remember(card.dataset.sprint, open);
  }

  // Default: planned and running sprints open, others closed; the stored state per user wins.
  const saved = expanded();
  cards().forEach(card => {
    const def = card.dataset.status === "planned" || card.dataset.status === "released";
    setOpen(card, saved && card.dataset.sprint in saved ? !!saved[card.dataset.sprint] : def, false);
  });

  list.addEventListener("click", e => {
    const btn = e.target.closest(".plan-toggle");
    if (!btn) return;
    const card = btn.closest(".plan-sprint");
    setOpen(card, btn.getAttribute("aria-expanded") !== "true", true);
    return;
  });

  const EMPTY_HINT = (msg && msg.dataset.emptyHint) || "No tasks yet – drag tasks here from the backlog or add a task";

  function recount() {
    cards().forEach(card => {
      const rows = card.querySelectorAll(".plan-row");
      const done = card.querySelectorAll('.plan-row[data-status="done"]').length;
      const bar = card.querySelector(".plan-bar-fill");
      const label = card.querySelector(".plan-done");
      if (bar) bar.style.width = (rows.length ? Math.floor((100 * done) / rows.length) : 0) + "%";
      if (label) label.textContent = tmT("%(done)d / %(total)d done", {done: done, total: rows.length});
      // Keep the empty hint in sync after a drag: show it in an emptied sprint, drop it in a filled one.
      const ul = card.querySelector(".plan-rows");
      const hint = ul && ul.querySelector(".plan-empty");
      if (ul && !rows.length && !hint) ul.insertAdjacentHTML("beforeend", '<li class="plan-empty">' + EMPTY_HINT + "</li>");
      else if (hint && rows.length) hint.remove();
    });
  }

  // --- Drag and drop of task rows (touch: only after a long press, so swiping still scrolls). ---
  async function move(nr, body) {
    body.set("task_nr", nr);
    const r = await fetch("/planning/move", {method: "POST", body: body,
      headers: {"X-CSRF-Token": csrf, "Content-Type": "application/x-www-form-urlencoded"}});
    if (r.ok) return r.text();
    let data = {message: tmT("Error %(status)s", {status: r.status})};
    try { data = await r.json(); } catch (e) { /* No JSON. */ }
    flash(data.message);
    return null;
  }

  if (window.Sortable) {
    list.querySelectorAll(".plan-rows").forEach(rows => {
      new Sortable(rows, {
        group: {name: "planning", put: rows.dataset.drop === "1"},
        sort: rows.dataset.sortable === "1", animation: 150, draggable: ".plan-row",
        filter: '.plan-row[data-status="done"], .plan-row[data-status="cancelled"]', preventOnFilter: false,
        delay: 300, delayOnTouchOnly: true,
        onEnd: async evt => {
          if (evt.from === evt.to && evt.oldIndex === evt.newIndex) return;
          const body = new URLSearchParams({sprint: evt.to.dataset.sprint});
          if (evt.to.dataset.sortable === "1") {
            body.set("order", Array.from(evt.to.querySelectorAll(".plan-row"), c => c.dataset.task).join(" "));
          }
          const html = await move(evt.item.dataset.task, body);
          if (html === null) {
            evt.from.insertBefore(evt.item, evt.from.children[evt.oldIndex] || null);
          } else {
            evt.item.outerHTML = html;
          }
          recount();
        },
      });
    });
  }

  // --- Row menu "Move to sprint …" (touch and keyboard fallback). ---
  list.addEventListener("click", async e => {
    const btn = e.target.closest("[data-move-sprint]");
    if (!btn) return;
    const row = btn.closest(".plan-row");
    const menu = btn.closest("details");
    btn.disabled = true;
    const html = await move(row.dataset.task, new URLSearchParams({sprint: btn.dataset.moveSprint}));
    btn.disabled = false;
    if (menu) menu.open = false;
    if (html === null) return;
    const target = list.querySelector('.plan-rows[data-sprint="' + btn.dataset.moveSprint + '"]');
    const tmp = document.createElement("div");
    tmp.innerHTML = html.trim();
    const fresh = tmp.firstElementChild;
    row.remove();
    if (target) {
      const empty = target.querySelector(".plan-empty");
      if (empty) empty.remove();
      target.appendChild(fresh);
    }
    recount();
    flash(row.dataset.task + " → " + btn.dataset.label, "ok");
  });

  // A click outside an open row menu closes it.
  document.addEventListener("click", e => {
    list.querySelectorAll("details.plan-move[open]").forEach(d => {
      if (!d.contains(e.target)) d.open = false;
    });
  });

  // --- Dialogs (sprint actions, "+ New sprint", "+ Task"). ---
  document.addEventListener("click", async e => {
    const opener = e.target.closest("[data-open]");
    if (opener) {
      const dlg = document.getElementById(opener.dataset.open);
      if (!dlg) return;
      dlg.showModal();
      const body = dlg.querySelector(".plan-dialog-body");
      if (dlg.dataset.src && body) {
        const r = await fetch(dlg.dataset.src);
        if (r.ok) body.innerHTML = await r.text();
        else body.textContent = tmT("Error %(status)s", {status: r.status});
      }
      return;
    }
    const closer = e.target.closest("dialog [data-close]");
    if (closer) closer.closest("dialog").close();
  });
})();
