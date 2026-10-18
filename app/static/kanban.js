// Kanban: drag and drop and the "Move" menu of each card both call /kanban/move. An invalid drop reverts the card
// (an admin can override after confirmation); the menu offers only the moves the server allows
// (GET /kanban/<nr>/moves) and never an override. If the transition needs a comment (answer or review reason),
// prompt for it. On phones the board snap-scrolls with ~85 vw columns and a sticky row of status tabs jumps to a
// column. "Swimlanes by agent" renders one board per assignee; drag and drop stays within each swimlane.
(function () {
  const scrollBox = document.querySelector(".kanban-scroll");
  const boards = document.querySelectorAll(".kanban.kanban-board");
  if (!scrollBox || !boards.length) return;
  const csrf = JSON.parse(document.body.getAttribute("hx-headers") || "{}")["X-CSRF-Token"];
  const msg = document.getElementById("kanban-msg");
  const tabs = document.querySelector(".kanban-tabs");
  const viewForm = document.querySelector("form.kanban-filters");

  function flash(text, kind) {
    msg.textContent = text;
    msg.className = "banner " + (kind || "error");
  }

  function updateCounts() {
    document.querySelectorAll(".kanban.kanban-board .kanban-col").forEach(col => {
      if (col.dataset.column === "done") return;  // "N of M" is server-rendered.
      const n = col.querySelectorAll(".card").length;
      const c = col.querySelector(".kanban-col-head .count");
      if (c) c.textContent = String(n);
      const tab = tabs && tabs.querySelector('button[data-column="' + col.dataset.column + '"] .count');
      if (tab) tab.textContent = String(n);
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

  // --- Segmented control "Board | Swimlanes by agent": set the view and submit the filter form. ---
  if (viewForm) document.querySelectorAll("[data-kanban-view]").forEach(btn => {
    btn.addEventListener("click", () => {
      const input = viewForm.querySelector('input[name="view"]');
      if (input) input.value = btn.dataset.kanbanView;
      viewForm.submit();
    });
  });

  // --- Status tabs (phones): jump to the matching column. ---
  if (tabs) tabs.addEventListener("click", e => {
    const btn = e.target.closest("button[data-column]");
    if (!btn) return;
    const col = scrollBox.querySelector('.kanban-col[data-column="' + btn.dataset.column + '"]');
    if (col) col.scrollIntoView({behavior: "smooth", inline: "start", block: "nearest"});
  });

  // --- Drag and drop (desktop; on touch screens only after a long press, so swiping still scrolls). Cards stay
  // within their own board: each swimlane gets its own Sortable group. ---
  if (window.Sortable) {
    boards.forEach((grid, i) => {
      const group = grid.classList.contains("kanban-swimlane") ? "kanban-swim-" + i : "kanban";
      grid.querySelectorAll(".kanban-list").forEach(list => {
        new Sortable(list, {
          group: group, animation: 150, draggable: ".card", filter: ".card-move", preventOnFilter: false,
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
    });
  }

  // --- "Move" menu: load the allowed targets when opened. ---
  scrollBox.addEventListener("toggle", async e => {
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

  scrollBox.addEventListener("click", async e => {
    const btn = e.target.closest("[data-move-to]");
    if (!btn) return;
    const card = btn.closest(".card");
    const nr = card.dataset.task, to = btn.dataset.moveTo;
    if (btn.dataset.confirm && !confirm(btn.dataset.confirm)) return;
    btn.disabled = true;
    const html = await post(nr, to, {}, false);
    btn.disabled = false;
    if (html === null) return;  // The card stays where it is; the message says why.
    const target = card.closest(".kanban").querySelector('.kanban-list[data-status="' + to + '"]');
    const tmp = document.createElement("div");
    tmp.innerHTML = html.trim();
    card.remove();
    if (target) target.prepend(tmp.firstElementChild);
    updateCounts();
    flash(nr + " → " + btn.dataset.label, "ok");
    msg.focus();
  });

  // --- Scroll position (kept across visits) and the active status tab. ---
  const key = "tb-kanban-scroll:" + location.search;
  function markCurrent() {
    const left = scrollBox.getBoundingClientRect().left;
    let best = null, bestX = Infinity;
    boards.forEach(grid => grid.querySelectorAll(".kanban-col").forEach(c => {
      const cx = Math.abs(c.getBoundingClientRect().left - left);
      if (cx < bestX) { bestX = cx; best = c; }
    }));
    if (!best) return;
    const col = best.dataset.column;
    document.querySelectorAll(".kanban.kanban-board .kanban-col").forEach(c =>
      c.toggleAttribute("data-current", c.dataset.column === col));
    if (tabs) tabs.querySelectorAll("button[data-column]").forEach(b => {
      if (b.dataset.column === col) b.setAttribute("aria-current", "true");
      else b.removeAttribute("aria-current");
    });
    // Filter changes keep the column in view.
    const colInput = viewForm && viewForm.querySelector('input[name="col"]');
    if (colInput && tabs && tabs.offsetParent) colInput.value = col;
  }
  // Phones: `?col=` opens that column; otherwise the stored scroll position, else the server's current column
  // (the first one, "Draft"). Wider screens only restore the scroll position.
  const phone = !!(tabs && tabs.offsetParent);
  const askedCol = new URLSearchParams(location.search).get("col");
  let x = null;
  try { x = sessionStorage.getItem(key); } catch (e) { /* Storage blocked. */ }
  if (x && !(phone && askedCol)) scrollBox.scrollLeft = +x;
  else if (phone) {
    const cur = scrollBox.querySelector(".kanban-col[data-current]");
    if (cur) scrollBox.scrollLeft += cur.getBoundingClientRect().left - scrollBox.getBoundingClientRect().left;
  }
  scrollBox.addEventListener("scroll", () => {
    markCurrent();
    try { sessionStorage.setItem(key, String(scrollBox.scrollLeft)); } catch (e) { /* Private mode. */ }
  }, {passive: true});
})();
