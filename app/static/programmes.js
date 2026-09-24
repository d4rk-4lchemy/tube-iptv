import { api, $, escape, action, toast } from './api.js';

const names = ['Monday', 'Tuesday', 'Wednesday', 'Thursday', 'Friday', 'Saturday', 'Sunday'];
let channel = null, rows = [], calendar = null, week = null, editing = null, editorChannel = null;
let generation = 0, fetchedAt = 0, dragging = false, loading = false;
const path = () => `channels/${channel}`;
const markup = `
<section id="programmes" class="programmes-section">
  <div class="section-heading"><h2>Programmes <span id="programme-count" class="count">0</span></h2><button id="new-programme" class="button dark">New programme +</button></div>
  <p class="field-hint">Weekly programmes have their own sources. Without programmes, the channel shuffles its general Sources.</p>
  <div id="programme-list"></div>
  <div class="schedule-toolbar"><h3>Weekly schedule</h3><span id="schedule-zone" class="mono"></span><div><button class="button small" id="previous-week" aria-label="Previous week">←</button><button class="button small" id="current-week">This week</button><button class="button small" id="next-week" aria-label="Next week">→</button></div></div>
  <p id="schedule-week" class="mono"></p>
  <p class="field-hint">Drag an emission to change its weekly weekday and time (5-minute steps). Other weekdays stay unchanged. Use Edit for precise times and keyboard access. Clock changes appear as missing or repeated hours.</p>
  <label class="gap-setting" for="gap-mode">Between programmes <select id="gap-mode"><option value="black">Black screen with silence</option><option value="sources">Random videos from Sources</option></select></label>
  <div id="schedule-grid" class="schedule-scroll" aria-label="Weekly programme schedule"></div>
</section>`;
$('sources').insertAdjacentHTML('beforebegin', markup);
document.querySelector('.rail nav a[href="#sources"]').insertAdjacentHTML('beforebegin', '<a class="nav-item" href="#programmes"><span>▦</span> Programmes</a>');
document.body.insertAdjacentHTML('beforeend', `
<dialog id="programme-dialog" class="programme-dialog">
  <form id="programme-form"><h2 id="programme-editor-title">New programme</h2>
    <label for="programme-name">Programme name</label><input id="programme-name" required maxlength="120">
    <label for="programme-duration">Duration in local minutes</label><input id="programme-duration" type="number" min="1" max="1440" step="1" required value="60">
    <p class="field-hint">Films may finish up to 5 minutes late. If none fits, the last film is cut at the planned end. Clock changes preserve local start and end times.</p>
    <h3>Weekly times</h3><div id="programme-rules"></div><button type="button" class="button small" id="add-programme-rule">Add time +</button>
    <p class="field-hint">Changes apply after the current emission. Removing a programme stops it immediately.</p>
    <div class="dialog-actions"><button type="button" class="button" id="close-programme">Close</button><button id="save-programme" class="button dark">Save programme</button></div>
  </form>
  <section class="programme-sources"><h3>Programme sources</h3><p class="field-hint">These links are used only by this programme. Save a new programme before adding sources.</p>
    <form id="programme-source-form"><label for="programme-urls">Video, playlist or channel URLs (one per line)</label><textarea id="programme-urls" rows="3" required></textarea><button id="add-programme-source" class="button">Add sources +</button></form><div id="programme-source-list"></div>
  </section>
</dialog>`);

function dateShift(value, days) {
  const d = new Date(`${value}T12:00:00Z`);
  d.setUTCDate(d.getUTCDate() + days);
  return d.toISOString().slice(0, 10);
}
function localTime(timestamp) {
  return new Intl.DateTimeFormat('en-GB', { timeZone: calendar.timezone, hour: '2-digit', minute: '2-digit', hourCycle: 'h23' }).format(timestamp * 1000);
}
function fullTime(timestamp) {
  return new Intl.DateTimeFormat('en-GB', { timeZone: calendar.timezone, weekday: 'short', hour: '2-digit', minute: '2-digit', timeZoneName: 'shortOffset', hourCycle: 'h23' }).format(timestamp * 1000);
}
function renderList() {
  $('programme-count').textContent = rows.length;
  $('programme-list').innerHTML = rows.length ? rows.map(p => `<article class="programme-row"><div><h3>${escape(p.name)}</h3><p class="field-hint">${p.duration_minutes} min · ${p.sources.length} sources</p><p class="mono">${p.rules.map(r => `${r.weekdays.map(d => names[d].slice(0, 3)).join(', ')} ${escape(r.time)}`).join('<br>')}</p></div><div class="programme-actions"><button class="button small" data-edit="${escape(p.id)}">Edit</button><button class="button small" data-remove="${escape(p.id)}">Remove</button></div></article>`).join('') : '<p class="empty">No weekly programmes. Add one to build a weekly schedule.</p>';
}
function renderGrid() {
  if (!calendar || dragging) return;
  $('schedule-zone').textContent = calendar.timezone;
  $('schedule-week').textContent = `${calendar.week} — ${dateShift(calendar.week, 6)}`;
  const heights = calendar.days.map(day => (day.ends_at - day.starts_at) / 1800 * 28);
  $('schedule-grid').innerHTML = `<div class="schedule-columns">${calendar.days.map((day, index) => {
    const cells = calendar.occurrences.filter(o => o.starts_at < day.ends_at && o.ends_at > day.starts_at);
    return `<section class="schedule-day"><h4>${names[day.weekday].slice(0, 3)} <span>${day.date.slice(5)}</span><small>${(day.ends_at - day.starts_at) / 3600} hours</small></h4><div class="schedule-track" data-day="${index}" style="height:${heights[index]}px">
      ${day.ticks.map(t => `<div class="schedule-tick" style="top:${(t.timestamp - day.starts_at) / 1800 * 28}px"><span>${escape(t.label)}</span></div>`).join('')}
      ${cells.map(o => {
        const start = Math.max(o.starts_at, day.starts_at), end = Math.min(o.ends_at, day.ends_at);
        const title = `${o.title}: ${fullTime(o.starts_at)} – ${fullTime(o.ends_at)}${o.hard_end && o.programme_id ? ' · Clock-change boundary: no overrun' : ''}`;
        const body = `<strong>${escape(o.title)}</strong><span>${localTime(start)}–${localTime(end)}${o.repeated ? ' ↺' : ''}</span>`;
        const style = `top:${(start - day.starts_at) / 1800 * 28}px;height:${Math.max(12, (end - start) / 1800 * 28)}px`;
        return o.programme_id ? `<button class="schedule-block" style="${style}" title="${escape(title)}" draggable="true" data-occurrence="${escape(o.id)}" aria-label="Edit ${escape(title)}">${body}</button>` : `<div class="schedule-block schedule-gap" style="${style}" title="${escape(title)}">${body}</div>`;
      }).join('')}
    </div></section>`;
  }).join('')}</div>`;
}
async function refresh(force = false) {
  if (!channel || (!force && (loading || Date.now() - fetchedAt < 10000))) return;
  const request = ++generation, base = path(), requestedWeek = week;
  loading = true;
  try {
    const [programmes, schedule] = await Promise.all([api(`${base}/programmes`), api(`${base}/schedule${requestedWeek ? '?week=' + requestedWeek : ''}`)]);
    if (request !== generation) return;
    rows = programmes; calendar = schedule; week = schedule.week; fetchedAt = Date.now();
    renderList(); renderGrid();
    if ($('programme-dialog').open && editing && editorChannel === channel) renderSources();
  } finally { if (request === generation) loading = false; }
}
export function syncProgrammes(data) {
  if (channel !== data.channel.id) {
    channel = data.channel.id; rows = []; calendar = null; week = null; fetchedAt = 0; loading = false;
    generation++; $('programme-dialog').close();
  }
  if (!$('gap-mode').disabled) $('gap-mode').value = data.gap_mode;
  action(() => refresh());
}
function addRule(rule = { weekdays: [0, 1, 2, 3, 4, 5, 6], time: '18:00' }) {
  const field = document.createElement('fieldset');
  field.className = 'programme-rule'; field.dataset.id = rule.id || '';
  field.innerHTML = `<legend>Emission time</legend><div class="rule-time"><label>Start <input type="time" required value="${escape(rule.time)}"></label><div class="weekday-choices">${names.map((name, day) => `<label><input type="checkbox" value="${day}" ${rule.weekdays.includes(day) ? 'checked' : ''}>${name.slice(0, 3)}</label>`).join('')}<button type="button" class="button small" data-all-days>Every day</button></div><button type="button" class="button small" data-remove-rule>Remove time</button></div>`;
  $('programme-rules').append(field);
}
function readRules() {
  return [...$('programme-rules').children].map(field => {
    const weekdays = [...field.querySelectorAll('input[type=checkbox]:checked')].map(input => Number(input.value));
    if (!weekdays.length) throw new Error('Select at least one weekday for every time.');
    return { id: field.dataset.id || null, time: field.querySelector('input[type=time]').value, weekdays };
  });
}
function openEditor(id = null) {
  editing = id; editorChannel = channel;
  const p = rows.find(row => row.id === id);
  $('programme-editor-title').textContent = p ? 'Edit programme' : 'New programme';
  $('programme-name').value = p?.name || '';
  $('programme-duration').value = p?.duration_minutes || 60;
  $('programme-rules').innerHTML = '';
  (p?.rules || [{ weekdays: [0, 1, 2, 3, 4, 5, 6], time: '18:00' }]).forEach(addRule);
  $('programme-urls').value = '';
  renderSources(); $('programme-dialog').showModal();
}
function renderSources() {
  $('add-programme-source').disabled = !editing;
  $('programme-urls').disabled = !editing;
  const p = rows.find(row => row.id === editing);
  $('programme-source-list').innerHTML = (p?.sources || []).map(s => `<article class="programme-source ${s.enabled ? '' : 'disabled'}"><strong>${escape(s.title || s.url)}</strong><a href="${escape(s.url)}" target="_blank" rel="noopener noreferrer">${escape(s.url)}</a><small>${escape(s.state)} · ${s.count} videos${s.enabled ? '' : ' · Disabled'}</small>${s.error ? `<p class="source-error">${escape(s.error)}</p>` : ''}<div><button type="button" class="button small" data-source="${escape(s.id)}" data-source-action="toggle">${s.enabled ? 'Disable' : 'Enable'}</button><button type="button" class="button small" data-source="${escape(s.id)}" data-source-action="refresh" ${s.state === 'pending' ? 'disabled' : ''}>Refresh</button><button type="button" class="button small" data-source="${escape(s.id)}" data-source-action="delete">Remove</button></div></article>`).join('');
}
$('new-programme').onclick = () => openEditor();
$('close-programme').onclick = () => $('programme-dialog').close();
$('add-programme-rule').onclick = () => addRule();
$('programme-rules').onclick = event => {
  if (event.target.closest('[data-remove-rule]')) {
    if ($('programme-rules').children.length === 1) return toast('Keep at least one emission time.', true);
    event.target.closest('fieldset').remove();
  }
  if (event.target.closest('[data-all-days]')) event.target.closest('fieldset').querySelectorAll('input[type=checkbox]').forEach(input => { input.checked = true; });
};
$('programme-form').onsubmit = event => {
  event.preventDefault(); action(async () => {
    const payload = { name: $('programme-name').value, duration_minutes: Number($('programme-duration').value), rules: readRules() };
    $('save-programme').disabled = true;
    try {
      const result = await api(`channels/${editorChannel}/programmes${editing ? '/' + editing : ''}`, { method: editing ? 'PUT' : 'POST', body: JSON.stringify(payload) });
      editing = result.id; $('programme-editor-title').textContent = 'Edit programme';
      $('programme-rules').innerHTML = ''; result.rules.forEach(addRule);
      await refresh(true); toast('Programme saved. Current emissions keep their original schedule.');
    } finally { $('save-programme').disabled = false; }
  });
};
$('programme-source-form').onsubmit = event => {
  event.preventDefault(); action(async () => {
    const urls = [...new Set($('programme-urls').value.split(/\s+/).filter(Boolean))];
    $('add-programme-source').disabled = true;
    try {
      for (const url of urls) await api(`channels/${editorChannel}/programmes/${editing}/sources`, { method: 'POST', body: JSON.stringify({ url }) });
      $('programme-urls').value = ''; await refresh(true); toast('Sources added. Reading media.');
    } finally { $('add-programme-source').disabled = false; }
  });
};
$('programme-source-list').onclick = event => {
  const button = event.target.closest('[data-source-action]'); if (!button) return;
  action(async () => {
    const source = rows.find(p => p.id === editing)?.sources.find(s => s.id === button.dataset.source);
    if (!source) return;
    const kind = button.dataset.sourceAction;
    if (kind === 'delete' && !window.confirm('Remove this source from the programme?')) return;
    await api(`sources/${source.id}${kind === 'refresh' ? '/refresh' : ''}`, { method: kind === 'delete' ? 'DELETE' : kind === 'refresh' ? 'POST' : 'PATCH', ...(kind === 'toggle' ? { body: JSON.stringify({ enabled: !source.enabled }) } : {}) });
    await refresh(true);
  });
};
$('programme-list').onclick = event => {
  const edit = event.target.closest('[data-edit]'); if (edit) return openEditor(edit.dataset.edit);
  const remove = event.target.closest('[data-remove]'); if (!remove) return;
  action(async () => {
    if (!window.confirm('Remove this programme and its sources? Its current emission stops immediately.')) return;
    await api(`${path()}/programmes/${remove.dataset.remove}`, { method: 'DELETE' }); await refresh(true);
  });
};
$('previous-week').onclick = () => { if (week) { week = dateShift(week, -7); action(() => refresh(true)); } };
$('next-week').onclick = () => { if (week) { week = dateShift(week, 7); action(() => refresh(true)); } };
$('current-week').onclick = () => { week = null; action(() => refresh(true)); };
$('gap-mode').onchange = () => action(async () => {
  $('gap-mode').disabled = true;
  try { await api(`${path()}/settings`, { method: 'PATCH', body: JSON.stringify({ gap_mode: $('gap-mode').value }) }); }
  finally { $('gap-mode').disabled = false; }
});
$('schedule-grid').onclick = event => {
  const button = event.target.closest('[data-occurrence]');
  const row = calendar?.occurrences.find(o => o.id === button?.dataset.occurrence);
  if (row) openEditor(row.programme_id);
};
$('schedule-grid').ondragstart = event => {
  const button = event.target.closest('[data-occurrence]'); if (!button) return;
  dragging = true; event.dataTransfer.setData('text/plain', button.dataset.occurrence); event.dataTransfer.effectAllowed = 'move';
};
$('schedule-grid').ondragend = () => { dragging = false; };
$('schedule-grid').ondragover = event => { if (event.target.closest('.schedule-track')) { event.preventDefault(); event.dataTransfer.dropEffect = 'move'; } };
$('schedule-grid').ondrop = event => {
  const track = event.target.closest('.schedule-track'); if (!track) return;
  event.preventDefault(); dragging = false;
  const occurrence = calendar.occurrences.find(o => o.id === event.dataTransfer.getData('text/plain'));
  if (!occurrence) return;
  const day = calendar.days[Number(track.dataset.day)];
  const seconds = Math.round((event.clientY - track.getBoundingClientRect().top) / 28 * 1800 / 300) * 300;
  const timestamp = Math.max(day.starts_at, Math.min(day.ends_at - 300, day.starts_at + seconds));
  const programme = rows.find(p => p.id === occurrence.programme_id);
  if (!programme.rules.some(r => r.id === occurrence.rule_id && r.weekdays.includes(occurrence.weekday))) {
    return toast('This emission keeps an earlier schedule. Use Edit to change future times.', true);
  }
  const rules = programme.rules.map(r => ({ ...r, weekdays: r.id === occurrence.rule_id ? r.weekdays.filter(d => d !== occurrence.weekday) : [...r.weekdays] })).filter(r => r.weekdays.length);
  rules.push({ weekdays: [day.weekday], time: localTime(timestamp) });
  action(async () => {
    try {
      await api(`${path()}/programmes/${programme.id}`, { method: 'PUT', body: JSON.stringify({ name: programme.name, duration_minutes: programme.duration_minutes, rules }) });
      toast(`Weekly emission moved to ${names[day.weekday]} ${localTime(timestamp)}.`);
    } finally { await refresh(true); }
  });
};
