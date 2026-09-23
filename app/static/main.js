import { api, $, escape, toast, action } from './api.js';
import { startPreview, syncPreview } from './player.js';
let state, sourceSignature = '', deleteId = null;
const labels = { idle: 'IDLE', buffering: 'BUFFERING', live: 'LIVE', error: 'RETRYING', empty: 'NO SOURCES', ended: 'FINISHED' };
function formatTime(seconds) {
  const value = Math.max(0, Math.floor(seconds));
  return [Math.floor(value / 3600), Math.floor(value / 60) % 60, value % 60].map(n => String(n).padStart(2, '0')).join(':');
}
function render(data) {
  state = data;
  syncPreview(data);
  if (!$('finish-current').disabled) $('finish-current').checked = data.finish_current_on_remove;
  $('connection').textContent = 'Server connected';
  $('channel-name').textContent = data.channel.name;
  $('state-label').textContent = labels[data.state] || data.state;
  $('state-dot').classList.toggle('live', data.state === 'live');
  $('now-playing').textContent = data.now?.title || 'No programme scheduled';
  const position = data.now ? `${formatTime(data.now.offset)} / ${data.now.estimated ? '~' : ''}${formatTime(data.now.duration)}` : '';
  $('programme-position').textContent = position;
  $('programme-position').title = data.now?.estimated ? 'Estimated slot length: the source has not reported its duration yet.' : 'Current channel time; HLS playback has a short buffer delay.';
  $('media-count').textContent = data.media_count;
  $('viewers').textContent = data.viewers;
  $('buffer').textContent = (data.buffer_bytes / 1024 / 1024).toFixed(1);
  $('source-count').textContent = data.sources.length;
  $('playlist-url').value = data.playlist_url;
  $('download-playlist').href = data.playlist_url;
  $('current-channel').textContent = data.yt_dlp.channel;
  $('current-version').textContent = data.yt_dlp.version;
  $('encoder-label').textContent = `H.264 / AAC · ${data.encoder.toUpperCase()}`;
  $('start-preview').disabled = !data.stream_available;
  $('preview-title').textContent = data.media_count ? 'The clock keeps running.' : 'Start with a source.';
  $('preview-description').textContent = data.media_count ? 'Join the current programme. The clock runs without viewers, too.' : 'Add your first link to create a channel.';
  $('install-version').disabled = data.update.state === 'installing';
  $('install-version').firstChild.textContent = data.update.state === 'installing' ? 'Installing… ' : 'Install version ';
  if (data.update.state === 'installing') $('update-status').textContent = 'Downloading, checking SHA-256 and validating the version…';
  else if (data.update.state === 'error') $('update-status').textContent = `Installation failed. The previous version is still active. ${data.update.error}`;
  else if (data.update.state === 'done') $('update-status').textContent = 'Version installed. New extraction jobs will use it.';
  const signature = JSON.stringify(data.sources);
  if (sourceSignature !== signature) {
    sourceSignature = signature;
    $('source-list').innerHTML = data.sources.length ? data.sources.map((s, index) => `<article class="source-row ${s.enabled ? '' : 'disabled'}" data-id="${escape(s.id)}"><span class="source-index">${String(index + 1).padStart(2, '0')}</span><div><h3 class="source-title">${escape(s.title || 'Reading source…')}</h3><a class="source-url" href="${escape(s.url)}" target="_blank" rel="noopener noreferrer">${escape(s.url)}</a><div class="source-meta"><span class="${s.state}">${{ ready: '● Ready', pending: '◌ Reading', error: '× Source error' }[s.state] || ''}</span><span>${s.count} videos</span>${!s.enabled ? '<span>Disabled</span>' : ''}</div>${s.error ? `<details class="source-error"><summary>Error details</summary>${escape(s.error)}</details>` : ''}</div><div class="source-actions"><button data-action="toggle" title="${s.enabled ? 'Disable' : 'Enable'} source" aria-label="${s.enabled ? 'Disable' : 'Enable'} source" aria-pressed="${Boolean(s.enabled)}">${s.enabled ? '◉' : '○'}</button><button data-action="refresh" aria-label="Refresh source" title="Refresh source" ${s.state === 'pending' ? 'disabled' : ''}>↻</button><button data-action="delete" aria-label="Remove source" title="Remove source">×</button></div></article>`).join('') : '<div class="empty"><span class="empty-icon">↗</span><h3>Every channel starts with a link.</h3><p>Add a video or an entire playlist. Tube takes it from there.</p></div>';
  }
  $('events').innerHTML = data.events.length ? data.events.slice(0, 6).map(e => `<div class="event"><time>${new Date(e.time * 1000).toLocaleTimeString('en-GB', { hour: '2-digit', minute: '2-digit' })}</time><span>${escape(e.text)}</span></div>`).join('') : '<p class="muted">Channel events will appear here.</p>';
  $('error-details').hidden = !data.error;
  $('engine-error').textContent = data.error || '';
}
async function refresh() { render(await api('status')); }
async function poll() {
  try { await refresh(); } catch { $('connection').textContent = 'Server disconnected'; }
  setTimeout(poll, 2000);
}
$('add-source').addEventListener('submit', event => {
  event.preventDefault();
  action(async () => {
    const button = event.target.querySelector('button'); button.disabled = true;
    try { await api('sources', { method: 'POST', body: JSON.stringify({ url: $('source-url').value.trim() }) }); $('source-url').value = ''; toast('Source added. Reading the video list.'); await refresh(); }
    finally { button.disabled = false; }
  });
});
$('source-list').addEventListener('click', event => {
  const button = event.target.closest('button[data-action]'); if (!button) return;
  const source = state.sources.find(s => s.id === button.closest('[data-id]').dataset.id);
  action(async () => {
    if (button.dataset.action === 'delete') { deleteId = source.id; $('delete-title').textContent = source.title || source.url; $('delete-behaviour').textContent = state.finish_current_on_remove ? 'Removed videos will leave the queue. Only the currently playing video may finish.' : 'Removed videos will stop immediately and leave the queue.'; $('delete-dialog').showModal(); return; }
    if (button.dataset.action === 'toggle') await api(`sources/${source.id}`, { method: 'PATCH', body: JSON.stringify({ enabled: !source.enabled }) });
    if (button.dataset.action === 'refresh') await api(`sources/${source.id}/refresh`, { method: 'POST' });
    await refresh();
  });
});
$('delete-form').addEventListener('submit', event => { event.preventDefault(); action(async () => { await api(`sources/${deleteId}`, { method: 'DELETE' }); $('delete-dialog').close(); toast('Source removed'); await refresh(); }); });
$('cancel-delete').onclick = () => $('delete-dialog').close();
$('rename').onclick = () => { if (!state) return; $('name-input').value = state.channel.name; $('rename-dialog').showModal(); };
$('cancel-rename').onclick = () => $('rename-dialog').close();
$('rename-form').addEventListener('submit', event => { event.preventDefault(); action(async () => { await api('channel', { method: 'PATCH', body: JSON.stringify({ name: $('name-input').value.trim() }) }); $('rename-dialog').close(); await refresh(); }); });
async function copyPlaylist() {
  if (!state) return;
  try { await navigator.clipboard.writeText(state.playlist_url); toast('Playlist URL copied'); }
  catch { $('playlist-url').focus(); $('playlist-url').select(); toast('URL selected. Copy it with Ctrl+C or your browser menu.'); }
}
$('copy-playlist').onclick = copyPlaylist; $('copy-url').onclick = copyPlaylist;
$('start-preview').onclick = () => { if (state) startPreview(state.stream_url); };
let releaseRequest = 0;
async function loadVersions() {
  const request = ++releaseRequest;
  const channel = $('release-channel').value;
  $('release-version').innerHTML = '<option value="latest">Latest available</option>';
  try {
    const releases = await api(`versions/${channel}`);
    if (request !== releaseRequest) return;
    $('release-version').innerHTML += releases.map(r => `<option value="${escape(r.version)}">${escape(r.version)}</option>`).join('');
  } catch (error) { if (request === releaseRequest) toast(error.message, true); }
}
$('release-channel').onchange = loadVersions;
$('update-engine').addEventListener('submit', event => {
  event.preventDefault();
  action(async () => { await api('versions', { method: 'POST', body: JSON.stringify({ channel: $('release-channel').value, version: $('release-version').value }) }); await refresh(); });
});
poll(); loadVersions();

$('finish-current').addEventListener('change', () => {
  const input = $('finish-current');
  const selected = input.checked;
  input.disabled = true;
  action(async () => {
    try {
      await api('settings', { method: 'PATCH', body: JSON.stringify({ finish_current_on_remove: selected }) });
      toast(selected ? 'The current video may finish after source removal.' : 'Removed videos will stop immediately.');
    } catch (error) { input.checked = !selected; throw error; }
    finally { input.disabled = false; await refresh(); }
  });
});
