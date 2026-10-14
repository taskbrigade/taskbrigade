// Task forms: the "concrete model" choice offers the allow list of the agent selected in the same form
// (`_model_fields.html`); agents that pick their model themselves get a hint instead.
(function () {
  function setup(select) {
    const form = select.form;
    const assignee = form && form.elements.assigned_to;
    let models = {};
    try { models = JSON.parse(select.dataset.models || "{}"); } catch (e) { models = {}; }
    const hint = select.parentElement.querySelector("[data-model-hint]");
    // On load the stored model stays visible even if not allowed (the server drops it on reassignment).
    const sync = (initial) => {
      const agent = models[assignee ? assignee.value : ""] || {allowed: [], applies: true};
      const current = select.value;
      while (select.options.length > 1) select.remove(1);
      const choices = agent.allowed.slice();
      if (initial === true && current && current !== "none" && !choices.includes(current)) choices.unshift(current);
      for (const m of choices) select.add(new Option(m, m, false, m === current));
      if (hint) hint.hidden = !assignee || !assignee.value || agent.applies;
    };
    if (assignee) assignee.addEventListener("change", () => sync(false));
    sync(true);
  }
  document.addEventListener("DOMContentLoaded", () => document.querySelectorAll("[data-model-select]").forEach(setup));
})();
