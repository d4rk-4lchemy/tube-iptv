import { api, $, escape, toast, action } from './api.js';
import { startPreview, stopPreview, syncPreview } from './player.js';
let state, sourceSignature = '', deleteId = null;
let selectedChannel = null, channels = [], refreshRequest = 0, editingChannel = null;
const channelPath = path => `channels/${selectedChannel}${path ? '/' + path : ''}`;
const labels = { idle: 'IDLE', buffering: 'BUFFERING', live: 'LIVE', error: 'RETRYING', empty: 'NO SOURCES', ended: 'FINISHED' };
function formatTime(seconds) {
  const value = Math.max(0, Math.floor(seconds));
  return [Math.floor(value / 3600), Math.floor(value / 60) % 60, value % 60].map(n => String(n).padStart(2, '0')).join(':');
}
function render(data) {
  state = data;
  syncPreview(data);
  if (!$('finish-current').disabled) $('finish-current').checked = data.finish_current_on_remove;
  if (!$('channel-resolution').disabled) $('channel-resolution').value = data.resolution;
  if (!$('channel-fps').disabled) $('channel-fps').value = String(data.fps);
  $('connection').textContent = 'Server connected';
  $('channel-name').textContent = data.channel.name;
  const number = String(channels.findIndex(c => c.id === data.channel.id) + 1).padStart(2, '0');
  $('channel-number').textContent = number;
  $('monitor-channel').textContent = `CH ${number} · ${data.resolution === '4k' ? '4K' : data.resolution}`;
  $('screen-number').textContent = number;
  $('channel-count').textContent = String(channels.length).padStart(2, '0');
  $('remove-channel').disabled = channels.length < 2;
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
  $('epg-url').value = data.epg_url;
  if (!$('epg-days').disabled) $('epg-days').value = String(data.epg_days);
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
async function refresh() {
  const request = ++refreshRequest;
  const list = await api('channels');
  if (request !== refreshRequest) return;
  channels = list;
  if (!channels.some(c => c.id === selectedChannel)) {
    stopPreview();
    selectedChannel = channels[0].id;
  }
  const data = await api(channelPath('status'));
  if (request !== refreshRequest) return;
  const options = channels.map(c => `<option value="${escape(c.id)}">${escape(c.name)}</option>`).join('');
  if ($('channel-select').innerHTML !== options) $('channel-select').innerHTML = options;
  $('channel-select').value = selectedChannel;
  render(data);
}
$('channel-select').onchange = () => {
  selectedChannel = $('channel-select').value;
  state = null;
  stopPreview();
  $('source-url').value = '';
  action(refresh);
};
$('new-channel').onclick = () => {
  editingChannel = null;
  $('name-input').value = '';
  $('rename-dialog').showModal();
};
$('remove-channel').onclick = () => {
  if (!state) return;
  $('remove-channel-title').textContent = state.channel.name;
  $('remove-channel-dialog').dataset.channel = state.channel.id;
  $('remove-channel-dialog').showModal();
};
$('cancel-remove-channel').onclick = () => $('remove-channel-dialog').close();
$('remove-channel-form').onsubmit = event => {
  event.preventDefault();
  action(async () => {
    const id = $('remove-channel-dialog').dataset.channel;
    await api(`channels/${id}`, { method: 'DELETE' });
    $('remove-channel-dialog').close();
    if (selectedChannel === id) { stopPreview(); selectedChannel = null; state = null; }
    toast('Channel and its sources removed');
    await refresh();
  });
};
async function poll() {
  try { await refresh(); } catch { $('connection').textContent = 'Server disconnected'; }
  setTimeout(poll, 2000);
}
$('add-source').addEventListener('submit', event => {
  event.preventDefault();
  action(async () => {
    const button = event.target.querySelector('button'); button.disabled = true;
    try { await api(channelPath('sources'), { method: 'POST', body: JSON.stringify({ url: $('source-url').value.trim() }) }); $('source-url').value = ''; toast('Source added. Reading the video list.'); await refresh(); }
    finally { button.disabled = false; }
  });
});
$('source-list').addEventListener('click', event => {
  const button = event.target.closest('button[data-action]'); if (!button) return;
  const source = state?.sources.find(s => s.id === button.closest('[data-id]').dataset.id);
  if (!source) return;
  action(async () => {
    if (button.dataset.action === 'delete') { deleteId = source.id; $('delete-title').textContent = source.title || source.url; $('delete-behaviour').textContent = state.finish_current_on_remove ? 'Removed videos will leave the queue. Only the currently playing video may finish.' : 'Removed videos will stop immediately and leave the queue.'; $('delete-dialog').showModal(); return; }
    if (button.dataset.action === 'toggle') await api(`sources/${source.id}`, { method: 'PATCH', body: JSON.stringify({ enabled: !source.enabled }) });
    if (button.dataset.action === 'refresh') await api(`sources/${source.id}/refresh`, { method: 'POST' });
    await refresh();
  });
});
$('delete-form').addEventListener('submit', event => { event.preventDefault(); action(async () => { await api(`sources/${deleteId}`, { method: 'DELETE' }); $('delete-dialog').close(); toast('Source removed'); await refresh(); }); });
$('cancel-delete').onclick = () => $('delete-dialog').close();
$('rename').onclick = () => { if (!state) return; editingChannel = state.channel.id; $('name-input').value = state.channel.name; $('rename-dialog').showModal(); };
$('cancel-rename').onclick = () => $('rename-dialog').close();
$('rename-form').addEventListener('submit', event => { event.preventDefault(); action(async () => {
  const creating = !editingChannel;
  const result = await api(creating ? 'channels' : `channels/${editingChannel}`, {
    method: creating ? 'POST' : 'PATCH', body: JSON.stringify({ name: $('name-input').value.trim() }) });
  if (creating) { stopPreview(); selectedChannel = result.id; state = null; }
  $('rename-dialog').close(); await refresh();
}); });
async function copyPlaylist() {
  if (!state) return;
  try { await navigator.clipboard.writeText(state.playlist_url); toast('Playlist URL copied'); }
  catch { $('playlist-url').focus(); $('playlist-url').select(); toast('URL selected. Copy it with Ctrl+C or your browser menu.'); }
}
$('copy-playlist').onclick = copyPlaylist; $('copy-url').onclick = copyPlaylist;
$('copy-epg').onclick = async () => {
  if (!state) return;
  try { await navigator.clipboard.writeText(state.epg_url); toast('EPG URL copied'); }
  catch { $('epg-url').focus(); $('epg-url').select(); toast('URL selected. Copy it with Ctrl+C or your browser menu.'); }
};
$('epg-days').onchange = () => {
  const input = $('epg-days');
  const previous = state?.epg_days ?? 2;
  const days = Number(input.value);
  input.disabled = true;
  ++refreshRequest;
  action(async () => {
    try {
      const saved = await api('epg/settings', { method: 'PATCH', body: JSON.stringify({ days }) });
      if (state) state.epg_days = saved.days;
      toast('EPG horizon saved for all channels.');
    } catch (error) { input.value = String(previous); throw error; }
    finally { ++refreshRequest; input.disabled = false; await refresh(); }
  });
};
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
      await api(channelPath('settings'), { method: 'PATCH', body: JSON.stringify({ finish_current_on_remove: selected }) });
      toast(selected ? 'The current video may finish after source removal.' : 'Removed videos will stop immediately.');
    } catch (error) { input.checked = !selected; throw error; }
    finally { input.disabled = false; await refresh(); }
  });
});

$('channel-fps').addEventListener('change', () => {
  const input = $('channel-fps');
  const path = channelPath('settings');
  const fps = input.value === 'original' ? 'original' : Number(input.value);
  input.disabled = true;
  action(async () => {
    try {
      await api(path, { method: 'PATCH', body: JSON.stringify({ fps }) });
      toast('Frame rate saved. Applies from the next video or stream start.');
    } finally { input.disabled = false; await refresh(); }
  });
});

$('channel-resolution').addEventListener('change', () => {
  const input = $('channel-resolution');
  const path = channelPath('settings');
  const resolution = input.value;
  input.disabled = true;
  action(async () => {
    try {
      await api(path, { method: 'PATCH', body: JSON.stringify({ resolution }) });
      toast('Resolution saved. Applies from the next video or stream start.');
    } finally { input.disabled = false; await refresh(); }
  });
});

let gpuDevices = [], gpuDevice = '';
function renderGPUDevices() {
  const encoder = $('gpu-driver').value;
  const devices = gpuDevices.filter(d => d.encoders.includes(encoder));
  $('gpu-device').innerHTML = devices.length ? devices.map(d => `<option value="${escape(d.id)}" ${d.accessible ? '' : 'disabled'}>${escape(d.label)}${d.accessible ? '' : ' (no access)'}</option>`).join('') : '<option value="">No compatible GPU available</option>';
  if (devices.some(d => d.id === gpuDevice && d.accessible)) $('gpu-device').value = gpuDevice;
  else $('gpu-device').value = devices.find(d => d.accessible)?.id || '';
  $('gpu-device').disabled = encoder === 'software';
  $('save-gpu').disabled = encoder !== 'software' && !$('gpu-device').value;
}
async function loadGPU() {
  const data = await api('gpu');
  gpuDevices = data.devices;
  gpuDevice = data.device;
  $('gpu-driver').value = data.encoder;
  renderGPUDevices();
  $('gpu-status').textContent = `Saved: ${data.encoder.toUpperCase()}${data.encoder === 'software' ? '' : ' · ' + data.device}. Applies to all channels from the next video or stream start.`;
}
$('gpu-driver').onchange = renderGPUDevices;
$('gpu-device').onchange = () => { gpuDevice = $('gpu-device').value; };
$('refresh-gpu').onclick = () => action(loadGPU);
$('gpu-settings').onsubmit = event => {
  event.preventDefault();
  const encoder = $('gpu-driver').value, device = $('gpu-device').value;
  action(async () => {
    const controls = [...$('gpu-settings').elements];
    controls.forEach(control => { control.disabled = true; });
    $('gpu-status').textContent = 'Testing encoder and saving…';
    try {
      await api('gpu', { method: 'PATCH', body: JSON.stringify({ encoder, device }) });
      await loadGPU();
      await refresh();
      toast('Encoding settings saved for all channels.');
    } catch (error) {
      $('gpu-status').textContent = error.message;
      throw error;
    } finally {
      controls.forEach(control => { control.disabled = false; });
      renderGPUDevices();
    }
  });
};
action(loadGPU);
