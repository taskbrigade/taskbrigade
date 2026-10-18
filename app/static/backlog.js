/* Delegated events survive HTMX filter/sort/pagination swaps. Selection is limited to the visible page. */
(() => {
  const boxes = () => [...document.querySelectorAll('[data-task-select]')];
  const selected = () => boxes().filter(b => b.checked);
  function sync() {
    const all = boxes(), count = selected().length;
    const bar = document.getElementById('backlog-bulk');
    if (!bar) return;
    bar.hidden = !count;
    const label = bar.querySelector('[data-selection-count]');
    label.textContent = label.dataset.label.replace('{count}', count);
    all.forEach(b => b.closest('tr').setAttribute('aria-selected', String(b.checked)));
    const toggle = document.querySelector('[data-select-all]');
    if (toggle) { toggle.checked = !!all.length && count === all.length; toggle.indeterminate = count > 0 && count < all.length; }
  }
  document.addEventListener('change', e => {
    if (e.target.matches('[data-select-all]')) boxes().forEach(b => { b.checked = e.target.checked; });
    if (e.target.matches('[data-select-all], [data-task-select]')) sync();
  });
  document.addEventListener('click', e => {
    if (e.target.closest('[data-clear-selection]')) { boxes().forEach(b => { b.checked = false; }); sync(); }
    if (e.target.closest('[data-bulk-cancel]')) document.getElementById('backlog-dialog').close();
    const action = e.target.closest('[data-bulk-action]');
    if (!action) return;
    const dialog = document.getElementById('backlog-dialog');
    dialog.dataset.action = action.dataset.bulkAction;
    dialog.querySelectorAll('[data-bulk-field]').forEach(field => { field.hidden = field.dataset.bulkField !== action.dataset.bulkAction; });
    dialog.querySelector('#bulk-title').textContent = action.textContent;
    dialog.querySelector('#bulk-error').textContent = '';
    dialog.showModal();
    dialog.querySelector('[data-bulk-field]:not([hidden]) select').focus();
  });
  document.addEventListener('submit', async e => {
    if (e.target.id !== 'backlog-bulk-form') return;
    e.preventDefault();
    const form = e.target, dialog = form.closest('dialog'), action = dialog.dataset.action;
    const tasks = selected().map(b => ({task_nr: b.value, row_version: Number(b.dataset.version)}));
    const submit = form.querySelector('[type=submit]');
    submit.disabled = true;
    try {
      const csrf = JSON.parse(document.body.getAttribute('hx-headers'))['X-CSRF-Token'];
      const response = await fetch('/backlog/bulk', {method: 'POST', headers: {'Content-Type': 'application/json', 'X-CSRF-Token': csrf},
        body: JSON.stringify({tasks, action, value: form.elements[action].value, comment: form.elements.comment.value})});
      if (!response.ok) throw new Error(tmT('Error %(status)s', {status: response.status}));
      const data = await response.json();
      const results = document.getElementById('bulk-results');
      results.replaceChildren();
      const list = document.createElement('ul');
      data.results.forEach(result => {
        const line = document.createElement('li');
        line.textContent = `${result.task_nr}: ${result.ok ? tmT('Saved') : result.error.message}`;
        list.append(line);
      });
      results.append(list);
      dialog.close();
      await htmx.ajax('GET', location.pathname + location.search, {target: '#backlog-content', swap: 'innerHTML'});
    } catch (error) { form.querySelector('#bulk-error').textContent = error.message; }
    finally { submit.disabled = false; }
  });
  document.addEventListener('htmx:afterSwap', sync);
  sync();
})();
