// Kanban: drag and drop and the "Move" menu of each card both call /kanban/move. An invalid drop reverts the card
// (an admin can override after confirmation); the menu offers only the moves the server allows
// (GET /kanban/<nr>/moves) and never an override. If the transition needs a comment (answer or review reason),
// prompt for it. On phones the "Status column" picker shows one column; its choice stays in the URL (`col`).
(function () {
  const board = document.querySelector(".kanban");
  if (!board) return;
  const csrf = JSON.parse(document.body.getAttribute("hx-headers") || "{}")["X-CSRF-Token"];
  const msg = document.getElementById("kanban-msg");
  const picker = document.getElementById("kanban-col-picker");

  function flash(text, kind) {
    msg.textContent = text;
    msg.className = "banner " + (kind || "error");
  }

  function updateCounts() {
    document.querySelectorAll(".kanban-col").forEach(col => {
      const n = col.querySelectorAll(".card").length;
      col.querySelector("h4 small").textContent = "(" + n + ")";
      const opt = picker && picker.querySelector('option[value="' + col.dataset.column + '"]');
      if (opt) opt.textContent = opt.dataset.label + " (" + n + ")";
    });
  }

  // POST the move; follow up on "needs a comment" and (drag and drop only) "admin override".
  // Returns the new card HTML, or null after showing the error.
  async function post(nr, to, extra, allowOverride) {
    const body = new URLSearchParams({task_nr: nr, to: to, ...extra});
    const r = await fetch("/kanban/move", {method: "POST", body: body,
      headers: {"X-CSRF-Token": csrf, "Content-Type": "application/x-www-form-urlencoded"}});
    if (r.ok) return r.text();
    let data = {message: tmT("Error %(status)s", {status: r.status})};
    try { data = await r.json(); } catch (e) { /* No JSON. */ }
    if (r.status === 422 && data.needs_comment) {
      const comment = prompt(data.message + "\n\n" + tmT("Enter text:"));
      if (comment) return post(nr, to, {...extra, comment: comment}, allowOverride);
    } else if (allowOverride && r.status === 409 && data.can_override && !extra.override) {
      if (confirm(data.message + "\n\n" + tmT("Set the status manually anyway (admin override)?"))) {
        return post(nr, to, {...extra, override: "true"}, allowOverride);
      }
    }
    flash(data.message);
    return null;
  }

  // --- Drag and drop (desktop; on touch screens only after a long press, so swiping still scrolls). ---
  if (window.Sortable) {
    document.querySelectorAll(".kanban-list").forEach(list => {
      new Sortable(list, {
        group: "kanban", animation: 150, draggable: ".card", filter: ".card-move", preventOnFilter: false,
        delay: 300, delayOnTouchOnly: true,
        onEnd: async evt => {
          if (evt.from === evt.to) return;
          const html = await post(evt.item.dataset.task, evt.to.dataset.status, {}, true);
          if (html === null) {
            evt.from.insertBefore(evt.item, evt.from.children[evt.oldIndex] || null);
          } else {
            evt.item.outerHTML = html;
            flash(evt.item.dataset.task + " → " + (evt.to.dataset.label || evt.to.dataset.status), "ok");
          }
          updateCounts();
        },
      });
    });
  }

  // --- "Move" menu: load the allowed targets when opened. ---
  board.addEventListener("toggle", async e => {
    const d = e.target;
    if (!d.matches || !d.matches("details.card-move") || !d.open || d.dataset.loaded) return;
    const list = d.querySelector(".card-move-list");
    const nr = d.closest(".card").dataset.task;
    list.textContent = tmT("Loading…");
    const r = await fetch("/kanban/" + encodeURIComponent(nr) + "/moves");
    if (!r.ok) { list.textContent = tmT("Error %(status)s", {status: r.status}); return; }
    list.innerHTML = await r.text();
    d.dataset.loaded = "1";
  }, true);

  board.addEventListener("click", async e => {
    const btn = e.target.closest("[data-move-to]");
    if (!btn) return;
    const card = btn.closest(".card");
    const nr = card.dataset.task, to = btn.dataset.moveTo;
    if (btn.dataset.confirm && !confirm(btn.dataset.confirm)) return;
    btn.disabled = true;
    const html = await post(nr, to, {}, false);
    btn.disabled = false;
    if (html === null) return;  // The card stays where it is; the message says why.
    const target = board.querySelector('.kanban-list[data-status="' + to + '"]');
    const tmp = document.createElement("div");
    tmp.innerHTML = html.trim();
    card.remove();
    if (target) target.prepend(tmp.firstElementChild);
    updateCounts();
    flash(nr + " → " + btn.dataset.label, "ok");
    msg.focus();
  });

  // --- Phone column picker and the board's scroll position (kept across visits to a task and back). ---
  const key = "tb-kanban-scroll:" + location.search;
  function show(col) {
    board.querySelectorAll(".kanban-col").forEach(c => c.toggleAttribute("data-current", c.dataset.column === col));
    const url = new URL(location.href);
    url.searchParams.set("col", col);
    history.replaceState(history.state, "", url);
    const hidden = document.querySelector('.filters input[name="col"]');
    if (hidden) hidden.value = col;
  }
  if (picker) picker.addEventListener("change", () => show(picker.value));
  try {
    const x = sessionStorage.getItem(key);
    if (x) board.scrollLeft = +x;
    board.addEventListener("scroll", () => {
      try { sessionStorage.setItem(key, String(board.scrollLeft)); } catch (e) { /* Private mode. */ }
    }, {passive: true});
  } catch (e) { /* Storage blocked: start at the left. */ }
})();
