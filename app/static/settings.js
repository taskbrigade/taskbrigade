// Admin → Settings: one group at a time (URL hash, select on phones), secret fields with "Replace", and the save
// bar's unsaved-changes counter. Leaving with unsaved changes is handled by form_guard.js (data-guard="settings").
// Without JavaScript every group is shown and secret inputs are always visible.
(function () {
  const page = document.querySelector("[data-settings]");
  const form = page && page.querySelector("form[data-settings-form]");
  if (!form) return;
  const cards = [...page.querySelectorAll("[data-group]")];
  const groupsOf = (el) => el.dataset.group.split(" ");
  const keys = [...new Set(cards.flatMap(groupsOf))];
  const links = [...page.querySelectorAll("[data-group-link]")];
  const select = page.querySelector("[data-group-select]");

  function show(key) {
    if (!keys.includes(key)) key = keys[0];
    cards.forEach((c) => { c.hidden = !groupsOf(c).includes(key); });
    links.forEach((a) => {
      if (a.dataset.groupLink === key) a.setAttribute("aria-current", "true"); else a.removeAttribute("aria-current");
    });
    if (select) select.value = key;
    form.action = "/admin/settings#" + key;  // Saving returns to the same group.
  }
  window.addEventListener("hashchange", () => show(location.hash.slice(1)));
  if (select) select.addEventListener("change", () => { location.hash = select.value; });
  show(location.hash.slice(1));

  // Unsaved changes: every named control that differs from the value the page was rendered with.
  const status = page.querySelector("[data-savebar-status]");
  const text = page.querySelector("[data-savebar-text]");
  const changed = (el) => {
    if (el.type === "checkbox" || el.type === "radio") return el.checked !== el.defaultChecked;
    if (el.tagName === "SELECT") return [...el.options].some((o) => o.selected !== o.defaultSelected);
    return el.value !== el.defaultValue;
  };
  function update() {
    const n = [...form.elements].filter((el) => el.name && el.name !== "csrf_token" && changed(el)).length;
    text.textContent = n === 0 ? tmT("No unsaved changes") : n === 1 ? tmT("1 unsaved change")
      : tmT("%(n)s unsaved changes", { n: n });
    status.classList.toggle("is-dirty", n > 0);
  }
  form.addEventListener("input", update);
  form.addEventListener("change", update);

  // Secret fields: "Replace" opens an empty input, "Cancel" closes and clears it; "Reset to .env" excludes it.
  const secrets = [...form.querySelectorAll("[data-secret]")].map((box) => {
    const button = box.querySelector("[data-secret-replace]");
    const input = box.querySelector(".ui-secret-new");
    const reset = box.querySelector("input[name^='reset_']");
    const label = button.textContent;
    const toggle = (open) => {
      box.classList.toggle("is-replacing", open);
      button.setAttribute("aria-expanded", String(open));
      button.textContent = open ? tmT("Cancel") : label;
      if (open) {
        if (reset) reset.checked = false;
        input.focus();
      } else {
        input.value = "";
      }
      update();
    };
    button.addEventListener("click", () => toggle(!box.classList.contains("is-replacing")));
    if (reset) reset.addEventListener("change", () => { if (reset.checked) toggle(false); });
    return toggle;
  });

  // "Discard": the reset button restores the rendered values; close open secret inputs afterwards.
  form.addEventListener("reset", () => setTimeout(() => { secrets.forEach((toggle) => toggle(false)); update(); }));
  update();
})();
