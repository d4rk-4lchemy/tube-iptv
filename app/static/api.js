export async function api(path, options = {}) {
  const response = await fetch(`/api/${path}`, { ...options, headers: { 'Content-Type': 'application/json', ...options.headers } });
  if (!response.ok) {
    let message = `HTTP error ${response.status}`;
    try { const data = await response.json(); message = typeof data.detail === 'string' ? data.detail : message; } catch {}
    throw new Error(message);
  }
  return response.status === 204 ? null : response.json();
}
export const $ = id => document.getElementById(id);
export const escape = value => String(value ?? '').replace(/[&<>"']/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
let timer;
export function toast(message, error = false) {
  $('toast').textContent = message;
  $('toast').classList.toggle('error', error);
  $('toast').hidden = false;
  clearTimeout(timer);
  timer = setTimeout(() => { $('toast').hidden = true; }, error ? 9000 : 3500);
}
export async function action(fn) { try { await fn(); } catch (error) { toast(error.message, true); } }
