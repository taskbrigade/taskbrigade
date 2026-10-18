// App shell (_app_header.html, header.css): disclosure buttons, tabs and folded panels.
// Layout follows the available width in CSS only, so rotating the phone never touches form values or state.
// - Buttons with data-toggle and aria-controls open/close the element `.collapsible` (`.is-open`): menus, capture,
//   quick capture, filter panels. Buttons sharing data-group close each other. Escape or a click outside a
//   data-group popup closes it and returns the focus to its button.
// - [data-tabs]: tab buttons (role=tab, aria-controls) show one panel at a time; CSS applies it on phones only.
// - details[data-fold-narrow]: folded on page load when the screen is narrow (open on desktop).
// - [data-segmented] (segmented control, templates/_ui.html): a click presses one button (aria-pressed), releases
//   the others, copies its value into a hidden input of the group and fires "ui:segment" on the group.
// - select[data-qc-project] (quick capture): the closed select shows the chosen key (.qc-project-key) and the
//   project name as tooltip; the list keeps "KEY – name".
(function () {
  const NARROW = "(max-width: 599px)";
  const toggles = () => [...document.querySelectorAll("[data-toggle][aria-controls]")];
  const target = (btn) => document.getElementById(btn.getAttribute("aria-controls"));
  const isOpen = (btn) => btn.getAttribute("aria-expanded") === "true";

  function set(btn, open, focus) {
    const el = target(btn);
    if (!el) return;
    btn.setAttribute("aria-expanded", open ? "true" : "false");
    el.classList.toggle("is-open", open);
    if (open && focus) {
      const first = el.querySelector("input:not([type=hidden]), select, textarea, a[href], button");
      if (first) first.focus();
    }
  }

  function closeGroup(except) {
    toggles().forEach((b) => { if (b !== except && b.dataset.group && isOpen(b)) set(b, false); });
  }

  document.addEventListener("click", (e) => {
    const closer = e.target.closest("[data-close]");
    if (closer) {
      const opener = toggles().find((b) => b.getAttribute("aria-controls") === closer.dataset.close);
      if (opener) { set(opener, false); opener.focus(); }
      return;
    }
    const seg = e.target.closest("[data-segmented] > button[aria-pressed]");
    if (seg) {
      const group = seg.parentElement;
      group.querySelectorAll(":scope > button[aria-pressed]").forEach((b) => {
        b.setAttribute("aria-pressed", b === seg ? "true" : "false");
      });
      const input = group.querySelector("input[type=hidden]");
      if (input) input.value = seg.value;
      group.dispatchEvent(new CustomEvent("ui:segment", { bubbles: true, detail: { value: seg.value } }));
    }
    const btn = e.target.closest("[data-toggle][aria-controls]");
    if (btn) {
      const open = !isOpen(btn);
      if (btn.dataset.group) closeGroup(btn);
      set(btn, open, open && btn.dataset.group);
      return;
    }
    // A click outside an open header popup (menu, quick capture) closes it.
    toggles().forEach((b) => {
      if (b.dataset.group && isOpen(b) && !target(b).contains(e.target)) set(b, false);
    });
  });

  document.addEventListener("keydown", (e) => {
    if (e.key.toLowerCase() === "n" && !e.ctrlKey && !e.metaKey && !e.altKey && !e.repeat &&
        !e.target.closest("input, textarea, select, button, [contenteditable]:not([contenteditable=false]), [role=textbox]")) {
      const form = document.getElementById("quick-capture");
      if (!form) return;
      e.preventDefault();
      closeGroup();
      const opener = toggles().find((b) => target(b) === form);
      if (window.matchMedia("(max-width: 639px)").matches && opener) set(opener, true);
      form.querySelector("input[name=title]").focus();
      return;
    }
    if (e.key !== "Escape") return;
    const open = toggles().filter(isOpen);
    if (!open.length) return;
    // Close the popup the focus is in, else the last one opened in the header.
    const btn = open.find((b) => target(b).contains(document.activeElement)) || open[open.length - 1];
    set(btn, false);
    btn.focus();
  });

  function tabs(box) {
    const buttons = [...box.querySelectorAll("[role=tab][aria-controls]")];
    const show = (which, focus) => buttons.forEach((b) => {
      const on = b === which;
      b.setAttribute("aria-selected", on ? "true" : "false");
      b.tabIndex = on ? 0 : -1;
      const panel = document.getElementById(b.getAttribute("aria-controls"));
      if (panel) panel.classList.toggle("tab-hidden", !on);
      if (on && focus) b.focus();
    });
    buttons.forEach((b, i) => {
      b.addEventListener("click", () => show(b, false));
      b.addEventListener("keydown", (e) => {  // Arrow keys move between tabs (ARIA tabs pattern).
        if (e.key !== "ArrowLeft" && e.key !== "ArrowRight") return;
        e.preventDefault();
        show(buttons[(i + (e.key === "ArrowRight" ? 1 : buttons.length - 1)) % buttons.length], true);
      });
    });
    // A link to an element inside a panel (e.g. #edit, #thread) selects that panel.
    const fromHash = () => {
      const el = location.hash.length > 1 && document.getElementById(location.hash.slice(1));
      const b = el && buttons.find((x) => document.getElementById(x.getAttribute("aria-controls")).contains(el));
      if (b) show(b, false);
    };
    fromHash();
    window.addEventListener("hashchange", fromHash);
  }

  function showProject(select) {
    const option = select.selectedOptions[0];
    const key = select.closest(".qc-project-box").querySelector(".qc-project-key");
    if (!option || !key) return;
    key.textContent = option.value;
    select.closest("label").title = option.title;
  }

  document.addEventListener("change", (e) => {
    if (e.target.matches("select[data-qc-project]")) showProject(e.target);
  });
  // Also after the browser restored a chosen project (back button, reload).
  window.addEventListener("pageshow", () => document.querySelectorAll("select[data-qc-project]").forEach(showProject));

  document.addEventListener("DOMContentLoaded", () => {
    document.querySelectorAll("[data-tabs]").forEach(tabs);
    // A link to a folded element (e.g. "#edit" after duplicating) opens it.
    const hashed = location.hash.length > 1 && document.getElementById(location.hash.slice(1));
    const opener = hashed && toggles().find((b) => target(b) === hashed && !b.dataset.group);
    if (opener && !isOpen(opener)) set(opener, true, false);
    if (window.matchMedia(NARROW).matches) {
      document.querySelectorAll("details[data-fold-narrow][open]").forEach((d) => { d.open = false; });
    }
  });
})();
