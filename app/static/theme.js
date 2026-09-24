(() => {
  const storageKey = 'tube-theme';
  const preference = window.matchMedia('(prefers-color-scheme: dark)');
  let saved;
  try { saved = localStorage.getItem(storageKey); } catch { /* Storage may be unavailable. */ }
  let explicit = saved === 'dark' || saved === 'light';

  function apply(dark) {
    document.documentElement.dataset.theme = dark ? 'dark' : 'light';
    document.querySelector('meta[name="theme-color"]').content = dark ? '#171d19' : '#eeeee6';
    const toggle = document.getElementById('dark-mode');
    if (toggle) toggle.checked = dark;
  }

  apply(explicit ? saved === 'dark' : preference.matches);
  preference.addEventListener('change', event => {
    if (!explicit) apply(event.matches);
  });
  document.addEventListener('DOMContentLoaded', () => {
    const toggle = document.getElementById('dark-mode');
    toggle.checked = document.documentElement.dataset.theme === 'dark';
    toggle.addEventListener('change', () => {
      explicit = true;
      apply(toggle.checked);
      try { localStorage.setItem(storageKey, toggle.checked ? 'dark' : 'light'); } catch { /* Keep the selection for this page. */ }
    });
  });
})();
