// Small conveniences: keep the flash visible briefly, then fade it.
document.querySelectorAll('.flash').forEach(f => setTimeout(() => { f.style.transition = 'opacity .6s'; f.style.opacity = '0'; setTimeout(() => f.remove(), 700); }, 6000));
// Remember open <details> per page.
document.querySelectorAll('details[id]').forEach(d => {
  const k = 'open:' + location.pathname + '#' + d.id;
  try { if (localStorage.getItem(k) === '1') d.open = true; } catch (e) {}
  d.addEventListener('toggle', () => { try { localStorage.setItem(k, d.open ? '1' : '0'); } catch (e) {} });
});
