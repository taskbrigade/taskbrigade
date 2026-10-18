// Project page (project_detail.html): tabs selected by the URL hash and "Copy" for the clone URL.
// - #settings, #members, #gitea, #recipients, #data show one panel; a hash of an element inside a panel
//   (e.g. #sprints, #mail-recipients) selects that panel. Without JS all panels stay visible.
// - [data-open="<details id>"] links also open that <details> element after switching the tab.
(function () {
  const panels = [...document.querySelectorAll("[data-tab-panel]")];
  if (!panels.length) return;
  const tabs = [...document.querySelectorAll("[data-tab]")];

  function show(hash) {
    const target = hash && document.getElementById(hash.slice(1));
    const panel = (target && target.closest("[data-tab-panel]")) || panels[0];
    panels.forEach((p) => p.classList.toggle("is-active", p === panel));
    tabs.forEach((t) => {
      if (t.dataset.tab === panel.id) t.setAttribute("aria-current", "page");
      else t.removeAttribute("aria-current");
    });
    if (target && target !== panel) target.scrollIntoView({ block: "start" });
  }
  show(location.hash);
  ["hashchange", "popstate"].forEach((type) => window.addEventListener(type, () => show(location.hash)));

  // Tab links switch without jumping to the panel's anchor.
  tabs.forEach((t) => t.addEventListener("click", (e) => {
    e.preventDefault();
    history.pushState(null, "", t.getAttribute("href"));
    show(t.getAttribute("href"));
  }));

  document.addEventListener("click", (e) => {
    const a = e.target.closest("a[data-open]");
    if (!a) return;
    const details = document.getElementById(a.dataset.open);
    if (details) {
      e.preventDefault();
      history.pushState(null, "", a.getAttribute("href"));
      show(a.getAttribute("href"));
      details.open = true;
      const first = details.querySelector("input:not([type=hidden])");
      if (first) first.focus();
    }
  });

  document.addEventListener("click", async (e) => {
    const btn = e.target.closest("button[data-copy]");
    if (!btn) return;
    const label = btn.dataset.label || btn.textContent;
    btn.dataset.label = label;
    try {
      await navigator.clipboard.writeText(btn.dataset.copy);
      btn.textContent = tmT("Copied");
    } catch (err) {
      // No clipboard API (plain HTTP outside localhost): select the text so Ctrl+C works.
      const code = btn.parentElement.querySelector("code");
      if (code) window.getSelection().selectAllChildren(code);
      btn.textContent = tmT("Press Ctrl+C to copy");
    }
    setTimeout(() => { btn.textContent = label; }, 2000);
  });
})();
