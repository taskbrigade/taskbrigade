/* Native modal dialogs supply focus trapping, Escape and focus restoration. */
document.addEventListener('click', (event) => {
  const opener = event.target.closest('[data-wf-open]');
  if (opener) {
    const current = opener.closest('dialog');
    if (current) current.close();
    const dialog = document.getElementById(opener.dataset.wfOpen);
    dialog._wfOpener = current ? current._wfOpener : opener;
    dialog.showModal();
    if (dialog.classList.contains('wf-action-menu') && window.innerWidth >= 640) {
      const rect = opener.getBoundingClientRect();
      dialog.style.left = Math.max(8, rect.right - dialog.offsetWidth) + 'px';
      dialog.style.top = Math.max(8, Math.min(rect.bottom + 4, window.innerHeight - dialog.offsetHeight - 8)) + 'px';
    }
  }
  const closer = event.target.closest('[data-wf-close]');
  if (closer) closer.closest('dialog').close();
  if (event.target.matches('dialog[open]')) {
    const rect = event.target.getBoundingClientRect();
    if (event.clientX < rect.left || event.clientX > rect.right || event.clientY < rect.top || event.clientY > rect.bottom) event.target.close();
  }
});
document.querySelectorAll('.wf-row-actions dialog').forEach((dialog) => {
  dialog.addEventListener('close', () => {
    if (!document.querySelector('.wf-row-actions dialog[open]')) dialog._wfOpener?.focus();
  });
});
