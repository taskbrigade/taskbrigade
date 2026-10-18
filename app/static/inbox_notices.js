/* Read actions keep the server's changed IDs, so undo affects exactly the last action. */
(() => {
  const panel = document.querySelector('[data-notices]');
  if (!panel) return;
  const rows = () => [...panel.querySelectorAll('[data-notice-ids]')];
  const boxes = () => [...panel.querySelectorAll('[data-notice-select]')].filter(b => !b.closest('li').hidden);
  const idsOf = row => row.dataset.noticeIds.split(',').filter(Boolean).map(Number);
  const toast = panel.querySelector('.notice-toast');
  const selectMode = panel.querySelector('[data-notice-select-mode]');
  let lastIds = [], timer, busy = false;
  function sync() {
    const all = boxes(), count = all.filter(b => b.checked).length;
    panel.querySelector('[data-notice-count]').textContent = panel.dataset.countLabel.replace('%(count)s', count);
    panel.querySelector('[data-notice-read-selected]').disabled = count === 0;
    const toggle = panel.querySelector('[data-notice-select-all]');
    toggle.checked = all.length > 0 && count === all.length;
    toggle.indeterminate = count > 0 && count < all.length;
  }
  function selection(on) {
    panel.classList.toggle('is-selecting', on);
    panel.querySelector('.notice-selection').hidden = !on;
    selectMode.setAttribute('aria-pressed', String(on));
    panel.querySelectorAll('[data-notice-select]').forEach(b => { b.hidden = !on; b.checked = false; });
    sync();
  }
  async function act(body, undo = false) {
    if (busy) return;
    busy = true;
    panel.setAttribute('aria-busy', 'true');
    panel.querySelector('#inbox-notices-msg').textContent = '';
    try {
      const csrf = JSON.parse(document.body.getAttribute('hx-headers'))['X-CSRF-Token'];
      const response = await fetch('/inbox/notices/' + (undo ? 'unread' : 'read'), {
        method: 'POST', headers: {'Content-Type': 'application/json', 'X-CSRF-Token': csrf}, body: JSON.stringify(body)
      });
      if (!response.ok) throw new Error('notice action failed');
      const data = await response.json(), changed = new Set(data.ids);
      rows().forEach(row => {
        if (idsOf(row).some(id => changed.has(id))) {
          row.hidden = !undo;
          row.classList.remove('swiped');
        }
      });
      clearTimeout(timer);
      selection(false);
      if (undo) {
        lastIds = [];
        toast.hidden = true;
      } else {
        lastIds = data.ids;
        toast.querySelector('span').textContent = panel.dataset.readLabel.replace('%(count)s', data.count) + ' – ';
        toast.hidden = !data.count;
        timer = setTimeout(() => { toast.hidden = true; lastIds = []; }, 6000);
      }
    } catch (error) {
      panel.querySelector('#inbox-notices-msg').textContent = undo
        ? tmT('Could not undo marking notices as read – please reload.')
        : tmT('Could not mark notices as read – please reload.');
    }
    finally { busy = false; panel.removeAttribute('aria-busy'); }
  }
  panel.addEventListener('click', event => {
    const target = event.target.closest('button');
    if (!target) return;
    // Header buttons must not toggle the surrounding details element.
    event.preventDefault();
    panel.querySelector('details.inbox-notices-body').open = true;
    if (target.matches('[data-notice-select-mode]')) selection(!panel.classList.contains('is-selecting'));
    if (target.matches('[data-notice-cancel]')) { selection(false); selectMode.focus(); }
    if (target.matches('[data-notice-read-all]')) act({all: true, sprint: panel.dataset.sprint || null, until: panel.dataset.until, max_id: Number(panel.dataset.maxId)});
    if (target.matches('[data-notice-read]')) act({ids: idsOf(target.closest('li'))});
    if (target.matches('[data-notice-read-selected]')) act({ids: boxes().filter(b => b.checked).flatMap(b => idsOf(b.closest('li')))});
    if (target.matches('[data-notice-undo]') && lastIds.length) act({ids: lastIds}, true);
  });
  panel.addEventListener('change', event => {
    if (event.target.matches('[data-notice-select-all]')) boxes().forEach(b => { b.checked = event.target.checked; });
    sync();
  });
  document.addEventListener('keydown', event => {
    if (event.key === 'Escape' && panel.classList.contains('is-selecting')) { selection(false); selectMode.focus(); }
  });
  // Only a deliberate horizontal swipe reveals the action; vertical scrolling remains native.
  let start;
  panel.addEventListener('touchstart', event => {
    const row = event.target.closest('[data-notice-ids]');
    start = row && !panel.classList.contains('is-selecting') ? {row, x: event.touches[0].clientX, y: event.touches[0].clientY} : null;
  }, {passive: true});
  panel.addEventListener('touchend', event => {
    if (!start) return;
    const dx = event.changedTouches[0].clientX - start.x, dy = event.changedTouches[0].clientY - start.y;
    if (Math.abs(dx) > 45 && Math.abs(dx) > Math.abs(dy) * 1.5) start.row.classList.toggle('swiped', dx < 0);
    start = null;
  }, {passive: true});
  panel.addEventListener('touchcancel', () => { start = null; }, {passive: true});
  sync();
})();
