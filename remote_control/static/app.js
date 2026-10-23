'use strict';
const $ = s => document.querySelector(s);
const esc = s => String(s ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const TICKS = 1e7;

const COMMANDS = ['MoveUp','MoveDown','MoveLeft','MoveRight','PageUp','PageDown','PreviousLetter','NextLetter','ToggleOsd','ToggleContextMenu','Select','Back','TakeScreenshot','SendKey','SendString','GoHome','GoToSettings','VolumeUp','VolumeDown','Mute','Unmute','ToggleMute','SetVolume','SetAudioStreamIndex','SetSubtitleStreamIndex','ToggleFullscreen','DisplayContent','GoToSearch','DisplayMessage','SetRepeatMode','ChannelUp','ChannelDown','Guide','ToggleStats','PlayMediaSource','PlayTrailers','SetShuffleQueue','PlayState','PlayNext','ToggleOsdMenu','Play','SetMaxStreamingBitrate','SetPlaybackOrder'];
const TEMPLATES = {SetVolume:'{"Volume":"50"}',SetAudioStreamIndex:'{"Index":"1"}',SetSubtitleStreamIndex:'{"Index":"-1"}',SetRepeatMode:'{"RepeatMode":"RepeatNone"}',SetShuffleQueue:'{"ShuffleMode":"Shuffle"}',SetMaxStreamingBitrate:'{"Bitrate":"20000000"}',SendString:'{"String":"hello"}',SendKey:'{"Key":"Enter"}',DisplayContent:'{"ItemId":"","ItemType":"Movie","ItemName":""}',DisplayMessage:'{"Header":"Hi","Text":"Test","TimeoutMs":"3000"}',PlayMediaSource:'{"ItemId":"","MediaSourceId":""}'};
const TRANSPORT = ['Pause','Unpause','PlayPause','Stop','PreviousTrack','NextTrack','Rewind','FastForward'];
const KEY_LABELS = {home:'Home',back:'Back',options:'✱ Options',info:'Info',search:'Search',play_pause:'⏯',stop:'⏹',rewind:'⏪',ff:'⏩',prev:'⏮',next:'⏭',replay:'↺ Replay',
  vol_up:'Vol +',vol_down:'Vol −',mute:'Mute',ch_up:'Ch +',ch_down:'Ch −',backspace:'⌫',enter:'Enter'};
const KEYBOARD = {ArrowUp:'up',ArrowDown:'down',ArrowLeft:'left',ArrowRight:'right',Enter:'ok',Escape:'back',Backspace:'back',Home:'home',' ':'play_pause',m:'mute','+':'vol_up','=':'vol_up','-':'vol_down'};

const S = {
  devices: [], devId: localStorage.getItem('devId') || '', status: {}, apps: {}, appsLoaded: {},
  sessions: [], sessSel: 'auto', logAll: [], logLast: 0, keyboard: localStorage.getItem('kbd') === '1', sig: '',
};
const dev = () => S.devices.find(d => d.id === S.devId);
const has = cap => (dev()?.capabilities || []).includes(cap);

/* ------------------------------------------------------------------ api */
async function api(method, path, body) {
  try {
    const r = await fetch(path, {method, headers: body === undefined ? {} : {'Content-Type': 'application/json'}, body: body === undefined ? undefined : JSON.stringify(body)});
    return await r.json();
  } catch (e) { return {ok: false, status: 0, body: String(e)}; }
}
// run an action from a button: show busy, then flash green/red from the result
async function run(btn, promise) {
  if (btn) btn.classList.add('busy');
  const res = await promise;
  if (btn) {
    btn.classList.remove('busy');
    const cls = res && res.ok ? 'ok' : 'bad';
    btn.classList.add(cls); setTimeout(() => btn.classList.remove(cls), 600);
  }
  fetchLog();
  return res;
}
const devPath = p => `/api/devices/${encodeURIComponent(S.devId)}${p}`;

/* ------------------------------------------------------------------ devices */
async function loadConfig() {
  const c = await api('GET', '/api/config');
  S.devices = c.devices || [];
  if (!dev()) S.devId = S.devices[0]?.id || '';
  const banner = $('#banner');
  banner.hidden = !c.configError;
  banner.textContent = c.configError || '';
  $('#fDevice').innerHTML = '<option value="">all devices</option>' + S.devices.map(d => `<option value="${esc(d.id)}">${esc(d.name)}</option>`).join('');
  renderDevBar(); renderRemote(); selectDevice(S.devId, true);
}
function renderDevBar() {
  $('#devbar').innerHTML = S.devices.map(d => {
    const st = S.status[d.id];
    const cls = !st ? '' : !st.online ? 'offline' : st.power || '';
    const sub = !st ? '…' : !st.online ? 'offline' : [st.power, st.appName].filter(Boolean).join(' · ');
    return `<div class="dev ${d.id === S.devId ? 'sel' : ''}" data-act="selectdev" data-id="${esc(d.id)}"><span class="dot ${cls}"></span><div>${esc(d.name)}<div class="sub">${esc(sub)}</div></div></div>`;
  }).join('') || '<span class="dim small">No devices configured</span>';
}
function selectDevice(id, force) {
  if (id === S.devId && !force) return;
  S.devId = id; localStorage.setItem('devId', id); S.sessSel = 'auto';
  renderDevBar(); renderRemote(); renderSessions(); loadApps();
}
async function pollStatus(d) {
  const st = await api('GET', `/api/devices/${encodeURIComponent(d.id)}/status?poll=1`);
  S.status[d.id] = st;
  renderDevBar();
  if (d.id === S.devId) renderRemoteStatus();
}
const pollAll = () => Promise.all(S.devices.map(pollStatus));

/* ------------------------------------------------------------------ remote */
function renderRemote() {
  const d = dev(), el = $('#remote');
  if (!d) { el.innerHTML = '<div class="dim">No device selected</div>'; return; }
  const k = (key, cls = '') => `<button data-act="key" data-key="${key}" class="${cls}" ${d.keys[key] ? '' : 'disabled'} title="${key}">${KEY_LABELS[key] || key}</button>`;
  const dp = (key, label, cls = '') => `<button data-act="key" data-key="${key}" class="${cls}" ${d.keys[key] ? '' : 'disabled'} title="${key}">${label}</button>`;
  const power = has('power_on') ? `<div class="rgrid r2"><button data-act="power" data-state="on" class="pwr">⏻ Power on</button><button data-act="power" data-state="off" class="pwr">⏻ Power off</button></div>` : '';
  const more = d.extraKeys.length ? `<details class="adv"><summary>More keys</summary><div class="rgrid r2">${d.extraKeys.map(x => `<button data-act="rawkey" data-native="${esc(x.key)}" title="${esc(x.key)}">${esc(x.label)}</button>`).join('')}</div></details>` : '';
  const text = has('text') ? `<div class="row"><input id="txt" placeholder="Type on the TV"><button data-act="text">Send</button></div>` : '';
  const shot = has('screenshot') ? `<button data-act="shot">Screenshot</button><img id="shot" alt="" hidden style="max-width:100%;margin-top:6px;border-radius:8px">` : '';
  el.innerHTML = `
    <h3>${esc(d.name)}</h3>
    <div class="rstat small dim" id="rstat"></div>
    ${power}
    <div class="rgrid r3">${k('back')}${k('home')}${k('options')}</div>
    <div class="dpad">
      <span></span>${dp('up','▲')}<span></span>
      ${dp('left','◀')}${dp('ok','OK','ok')}${dp('right','▶')}
      <span></span>${dp('down','▼')}<span></span>
    </div>
    <div class="rgrid r3">${k('rewind')}${k('play_pause')}${k('ff')}</div>
    <div class="rgrid r3">${k('prev')}${k('stop')}${k('next')}</div>
    <div class="rgrid r3">${k('replay')}${k('info')}${k('search')}</div>
    <div class="rgrid r3">${k('vol_down')}${k('mute')}${k('vol_up')}</div>
    <div class="rgrid r2">${k('ch_down')}${k('ch_up')}</div>
    ${text}
    <div class="row"><input id="rawKey" placeholder="raw ${d.type === 'roku' ? 'ECP key (e.g. InputHDMI2)' : 'keycode (e.g. KEYCODE_GUIDE)'}"><button data-act="rawkeybox">Send</button>${shot}</div>
    ${more}
    <div class="kbd small dim"><label><input type="checkbox" id="kbd" ${S.keyboard ? 'checked' : ''}> keyboard control (arrows, Enter, Esc, Space, m, +/−)</label></div>
    <div class="small dim" style="text-align:center;margin-top:6px">${esc(d.typeLabel)} · ${esc(d.host)}</div>`;
  $('#kbd').onchange = e => { S.keyboard = e.target.checked; localStorage.setItem('kbd', S.keyboard ? '1' : '0'); };
  renderRemoteStatus();
}
function renderRemoteStatus() {
  const st = S.status[S.devId], el = $('#rstat');
  if (!el) return;
  el.innerHTML = !st ? '…' : st.online
    ? `<span class="chip">${esc(st.power)}</span><span class="chip">${esc(st.appName || st.app || 'unknown app')}</span>`
    : `<span class="chip" style="color:var(--bad)">offline</span> ${esc(st.error || '')}`;
}
const sendKey = (btn, key) => run(btn, api('POST', devPath('/key'), {key}));
const sendNative = (btn, native) => run(btn, api('POST', devPath('/key'), {native}));
document.addEventListener('keydown', e => {
  if (!S.keyboard || !S.devId || /^(INPUT|TEXTAREA|SELECT)$/.test(e.target.tagName) || e.ctrlKey || e.metaKey || e.altKey) return;
  const key = KEYBOARD[e.key];
  if (!key) return;
  e.preventDefault();
  sendKey(document.querySelector(`#remote [data-key="${key}"]`), key);
});

/* ------------------------------------------------------------------ apps */
async function loadApps(force) {
  const d = dev(), el = $('#apps');
  if (!d) { el.textContent = 'Select a device'; return; }
  if (S.appsLoaded[d.id] && !force) { renderApps(); return; }
  el.textContent = 'Loading apps…';
  const r = await api('GET', devPath('/apps'));
  fetchLog();
  if (!Array.isArray(r)) { el.innerHTML = `<span style="color:var(--bad)">${esc(r.body || 'Could not list apps')}</span>`; return; }
  S.apps[d.id] = r; S.appsLoaded[d.id] = true; renderApps();
}
function renderApps() {
  const list = S.apps[S.devId] || [], st = S.status[S.devId] || {};
  const btn = a => `<button data-act="launch" data-app="${esc(a.id)}" class="${a.id === st.app ? 'active' : ''}" title="${esc(a.id)}">${esc(a.name)}</button>`;
  const apps = list.filter(a => a.type !== 'tvin'), inputs = list.filter(a => a.type === 'tvin');
  $('#apps').innerHTML = (apps.map(btn).join('') + (inputs.length ? '<div class="grp">TV inputs</div>' + inputs.map(btn).join('') : '')) || '<span class="dim">No apps found</span>';
}

/* ------------------------------------------------------------------ jellyfin sessions */
function activeSession() {
  if (S.sessSel !== 'auto') return S.sessions.find(s => s.Id === S.sessSel);
  return S.sessions.find(s => s.deviceId === S.devId);
}
async function pollSessions() {
  const r = await api('GET', '/api/jf/sessions');
  const ok = Array.isArray(r);
  $('#jfState').textContent = ok ? `Jellyfin: ${r.length} session(s)` : 'Jellyfin: unreachable';
  $('#jfState').style.color = ok ? '' : 'var(--bad)';
  if (!ok) return;
  S.sessions = r; renderSessions();
}
function renderSessions() {
  const sig = S.sessions.map(s => s.Id + s.Client + s.DeviceName + s.deviceId).join('|') + '#' + S.sessSel + '#' + S.devId;
  if (sig !== S.sig) {
    S.sig = sig;
    const mine = S.sessions.find(s => s.deviceId === S.devId);
    $('#sessSel').innerHTML = `<option value="auto">Auto: ${mine ? esc(mine.Client + ' / ' + mine.DeviceName) : 'none matched for ' + esc(dev()?.name || 'device')}</option>` +
      S.sessions.map(s => `<option value="${esc(s.Id)}" title="DeviceId ${esc(s.DeviceId)}">${esc(s.Client)} - ${esc(s.DeviceName)} (${esc(s.UserName)})</option>`).join('');
    $('#sessSel').value = S.sessSel;
  }
  renderNowPlaying();
}
const fmt = t => { t = Math.round(t); const h = Math.floor(t/3600), m = Math.floor(t%3600/60), s = t%60; return (h ? h + ':' + String(m).padStart(2,'0') : m) + ':' + String(s).padStart(2,'0'); };
const itemLabel = i => i.Type === 'Episode' ? `${i.SeriesName} S${i.Season}E${i.Episode} - ${i.Name}` : i.Name + (i.Year ? ` (${i.Year})` : '');
function renderNowPlaying() {
  const s = activeSession(), sup = s ? s.SupportedCommands : [];
  $('#caps').innerHTML = s ? `<span class="chip">RemoteControl: ${s.SupportsRemoteControl}</span>` + sup.map(c => `<span class="chip">${esc(c)}</span>`).join('') : '<span class="dim small">No session</span>';
  document.querySelectorAll('#cmds button').forEach(b => b.classList.toggle('unsup', !sup.includes(b.dataset.cmd)));
  document.querySelectorAll('#transport button').forEach(b => b.classList.toggle('unsup', !sup.includes('PlayState')));
  if (!s) { $('#np').innerHTML = '<span class="dim">No Jellyfin session for this device. Press “Prepare device”, or open the Jellyfin app.</span>'; return; }
  const n = s.NowPlaying, p = s.PlayState || {};
  if (!n) { $('#np').innerHTML = `<span class="dim">${esc(s.Client)} / ${esc(s.DeviceName)}: nothing playing</span>`; return; }
  const pos = (p.PositionTicks || 0) / TICKS, len = (n.RunTimeTicks || 0) / TICKS;
  $('#np').innerHTML = `<b>${esc(itemLabel(n))}</b> ${p.IsPaused ? '(paused)' : ''}
    <div class="bar"><div style="width:${len ? Math.min(100, pos/len*100) : 0}%"></div></div>
    <div class="small dim">${fmt(pos)} / ${fmt(len)} · volume ${esc(p.VolumeLevel)} ${p.IsMuted ? '(muted)' : ''} · audio ${esc(p.AudioStreamIndex)} · subs ${esc(p.SubtitleStreamIndex)} · ${esc(p.PlayMethod)}</div>`;
}
function needSession() {
  const s = activeSession();
  if (!s) alert('No Jellyfin session selected. Press “Prepare device” or pick one in the Session menu.');
  return s;
}
function jfAct(btn, path, body) {
  const s = needSession(); if (!s) return;
  return run(btn, api('POST', path, {sessionId: s.Id, ...body})).then(r => { setTimeout(pollSessions, 700); return r; });
}

/* ------------------------------------------------------------------ library / play */
const splitIds = () => $('#ids').value.split(',').map(x => x.trim()).filter(Boolean);
async function playIds(btn, itemIds, playCommand, startIndex) {
  if (!itemIds.length) return;
  const pos = $('#optPos').value;
  const body = {itemIds, playCommand, startIndex,
    startPositionTicks: pos === '' ? undefined : Math.round(pos * TICKS),
    audioStreamIndex: $('#optAud').value, subtitleStreamIndex: $('#optSub').value};
  if (S.devId && S.sessSel === 'auto') {
    $('#prepState').textContent = $('#prep').checked ? 'preparing…' : '';
    const r = await run(btn, api('POST', devPath('/play'), {...body, prepare: $('#prep').checked}));
    $('#prepState').textContent = r.ok ? 'playing' : (r.body || 'failed').split('\n').pop();
    pollSessions(); pollStatus(dev());
    return r;
  }
  return jfAct(btn, '/api/jf/play', body);   // an explicit session was chosen
}
function itemRow(i, extra = '') {
  const id = esc(i.Id);
  return `<div class="item"><span class="name">${esc(itemLabel(i))} <span class="dim small">${esc(i.Type)}</span></span>${extra}
    <button data-act="playids" data-ids='["${id}"]' data-cmd="PlayNow">Now</button>
    <button data-act="playids" data-ids='["${id}"]' data-cmd="PlayNext">Next</button>
    <button data-act="playids" data-ids='["${id}"]' data-cmd="PlayLast">Last</button>
    <button data-act="view" data-id="${id}" data-type="${esc(i.Type)}">Details</button></div>`;
}
async function search() {
  const r = await api('GET', '/api/jf/search?q=' + encodeURIComponent($('#q').value));
  fetchLog();
  if (!Array.isArray(r)) { $('#results').textContent = r.body || 'Search failed'; return; }
  $('#results').innerHTML = r.map(i => `<div>${itemRow(i, i.Type === 'Series' ? `<button data-act="episodes" data-id="${esc(i.Id)}">Episodes</button>` : '')}<div class="eps"></div></div>`).join('') || '<span class="dim">No results</span>';
}
async function episodes(btn) {
  const box = btn.closest('div.item').nextElementSibling;
  const r = await api('GET', '/api/jf/episodes/' + encodeURIComponent(btn.dataset.id));
  fetchLog();
  if (!Array.isArray(r)) return;
  const ids = r.map(e => e.Id);
  box.innerHTML = r.map((e, n) => itemRow(e, `<button data-act="playids" data-ids='${esc(JSON.stringify(ids))}' data-cmd="PlayNow" data-index="${n}">Play from here</button>`)).join('');
}

/* ------------------------------------------------------------------ click dispatch */
const ACTIONS = {
  selectdev: (b) => selectDevice(b.dataset.id),
  reload: async () => { await api('POST', '/api/reload'); S.appsLoaded = {}; S.status = {}; await loadConfig(); pollAll(); fetchLog(); },
  key: (b) => sendKey(b, b.dataset.key),
  rawkey: (b) => sendNative(b, b.dataset.native),
  rawkeybox: (b) => { const v = $('#rawKey').value.trim(); if (v) sendNative(b, v); },
  power: async (b) => { await run(b, api('POST', devPath('/power'), {state: b.dataset.state})); setTimeout(() => pollStatus(dev()), 1500); },
  text: (b) => run(b, api('POST', devPath('/text'), {text: $('#txt').value})),
  shot: (b) => { const i = $('#shot'); i.hidden = false; i.onerror = () => { i.hidden = true; fetchLog(); }; i.onload = fetchLog; i.src = devPath('/screenshot') + '?t=' + Date.now(); },
  refreshApps: () => loadApps(true),
  launch: async (b) => { await run(b, api('POST', devPath('/launch'), {app: b.dataset.app})); setTimeout(() => pollStatus(dev()), 1500); },
  launchCustom: (b) => run(b, api('POST', devPath('/launch'), {app: $('#lpApp').value.trim(), params: {contentId: $('#lpContent').value.trim(), mediaType: $('#lpType').value.trim()}})),
  prepare: async (b) => {
    if (!S.devId) return;
    $('#prepState').textContent = 'preparing… (see log)';
    const r = await run(b, api('POST', devPath('/prepare'), {}));
    $('#prepState').textContent = r.ok ? 'ready' : (r.body || 'failed').split('\n').pop();
    pollSessions(); pollStatus(dev());
  },
  playstate: (b) => jfAct(b, '/api/jf/playstate', {command: b.dataset.cmd}),
  relseek: (b) => { const s = needSession(); if (s) jfAct(b, '/api/jf/playstate', {command: 'Seek', seekPositionTicks: Math.max(0, Math.round(((s.PlayState.PositionTicks || 0) / TICKS + Number(b.dataset.d)) * TICKS))}); },
  seek: (b) => { const v = $('#seekSec').value; if (v !== '') jfAct(b, '/api/jf/playstate', {command: 'Seek', seekPositionTicks: Math.round(v * TICKS)}); },
  search: () => search(),
  episodes: (b) => episodes(b),
  playids: (b) => playIds(b, b.dataset.ids ? JSON.parse(b.dataset.ids) : splitIds(), b.dataset.cmd, b.dataset.index === undefined ? undefined : Number(b.dataset.index)),
  jfcmd: (b) => jfAct(b, '/api/jf/command', {command: b.dataset.cmd}),
  rawcmd: (b) => {
    let args; const t = $('#rawArgs').value.trim();
    try { args = t ? JSON.parse(t) : undefined; } catch (e) { return alert('Arguments are not valid JSON'); }
    jfAct(b, '/api/jf/command', {command: $('#rawCmd').value, arguments: args});
  },
  message: (b) => jfAct(b, '/api/jf/message', {header: $('#mHead').value, text: $('#mText').value, timeoutMs: $('#mTime').value}),
  view: (b) => jfAct(b, '/api/jf/viewing', {itemId: b.dataset.id, itemType: b.dataset.type}),
  viewform: (b) => { if ($('#vId').value) jfAct(b, '/api/jf/viewing', {itemId: $('#vId').value, itemType: $('#vType').value}); },
  logClear: async () => { await api('DELETE', '/api/log'); S.logAll = []; S.logLast = 0; renderLog(); },
};
document.addEventListener('click', e => {
  const b = e.target.closest('[data-act]');
  if (b && !b.disabled && ACTIONS[b.dataset.act]) ACTIONS[b.dataset.act](b);
});

/* ------------------------------------------------------------------ log */
async function fetchLog() {
  if ($('#fPause').checked) return;
  const r = await api('GET', '/api/log?since=' + S.logLast);
  if (!r.entries || !r.entries.length) return;
  S.logLast = r.last;
  S.logAll = S.logAll.concat(r.entries).slice(-2000);
  const kinds = [...new Set(S.logAll.map(e => e.kind))].sort(), sel = $('#fKind');
  if (kinds.join() !== [...sel.options].slice(1).map(o => o.value).join()) {
    const keep = sel.value;
    sel.innerHTML = '<option value="">all sources</option>' + kinds.map(k => `<option>${esc(k)}</option>`).join('');
    sel.value = keep;
  }
  renderLog();
}
const devName = id => S.devices.find(d => d.id === id)?.name || id || '';
function logMatches(e) {
  if ($('#fDevice').value && e.device !== $('#fDevice').value) return false;
  if ($('#fKind').value && e.kind !== $('#fKind').value) return false;
  if ($('#fErr').checked && e.ok !== false) return false;
  const t = $('#fText').value.toLowerCase();
  return !t || `${e.action} ${e.request} ${e.response} ${devName(e.device)}`.toLowerCase().includes(t);
}
function renderLog() {
  const rows = S.logAll.filter(logMatches).slice(-400).reverse().map(e => {
    const unit = e.kind === 'adb' ? 'exit' : 'HTTP';
    const st = e.status == null ? '' : `<span class="st ${e.ok ? 'good' : 'badc'}">${unit} ${e.status}</span>`;
    const okMark = e.status == null ? `<span class="${e.ok === false ? 'st badc' : ''}">${esc((e.response || '').split('\n')[0].slice(0, 160))}</span>` : '';
    return `<details ${e.ok === false ? 'open' : ''}><summary><span class="dim">${esc(e.ts)}</span>
      <span class="tag ${esc(e.kind)}">${esc(e.kind)}</span><span class="dim">${esc(devName(e.device))}</span>
      <b>${esc(e.action)}</b>${st}${okMark}<span class="dim">${e.ms == null ? '' : e.ms + ' ms'}</span></summary>
      <pre>${esc(e.request)}${e.request && e.response ? '\n→ ' : ''}${esc(e.response)}</pre></details>`;
  });
  $('#log').innerHTML = rows.join('') || '<div class="dim" style="padding:6px">No log entries yet</div>';
}

/* ------------------------------------------------------------------ init */
$('#transport').innerHTML = TRANSPORT.map(c => `<button data-act="playstate" data-cmd="${c}">${c}</button>`).join('');
$('#cmds').innerHTML = COMMANDS.filter(c => !TEMPLATES[c]).map(c => `<button data-act="jfcmd" data-cmd="${c}">${c}</button>`).join('');
$('#rawCmd').innerHTML = COMMANDS.map(c => `<option>${c}</option>`).join('');
$('#rawCmd').onchange = () => { $('#rawArgs').value = TEMPLATES[$('#rawCmd').value] || ''; };
$('#rawCmd').onchange();
$('#sessSel').onchange = e => { S.sessSel = e.target.value; S.sig = ''; renderSessions(); };
$('#q').onkeydown = e => { if (e.key === 'Enter') search(); };
['fDevice', 'fKind', 'fErr', 'fText'].forEach(id => $('#' + id).addEventListener('input', renderLog));
$('#fPause').addEventListener('change', fetchLog);

(async () => {
  await loadConfig();
  pollAll(); pollSessions(); fetchLog();
  setInterval(pollAll, 8000);
  setInterval(pollSessions, 3000);
  setInterval(fetchLog, 1000);
})();
