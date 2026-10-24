'use strict';
/* Shared by the Remote page (/) and the Settings & testing page (/settings): state, polling,
   icons, quick-launch preferences, the "Watch a movie…" dialog and the log. Pages plug in through Page hooks. */
const $ = s => document.querySelector(s);
const esc = s => String(s ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const TICKS = 1e7;
const POWER_LABEL = {dreaming: 'screensaver'};

// Logos (simple-icons, CC0) drawn white on the brand colour. Matched as whole words against "name package".
const LOGOS = [['jellyfin','jellyfin','#00a4dc'],['netflix','netflix','#e50914'],['youtube tv','youtubetv','#ff0000'],['youtube music','youtubemusic','#ff0000'],
  ['youtube','youtube','#ff0000'],['prime video','primevideo','#00a8e1'],['prime','primevideo','#00a8e1'],['max','max','#5822b4'],['plex','plex','#e5a00d'],
  ['spotify','spotify','#1db954'],['apple tv','appletv','#3a3a3c'],['apple','appletv','#3a3a3c'],['paramount','paramountplus','#0064ff'],['twitch','twitch','#9146ff'],
  ['crunchyroll','crunchyroll','#f47521'],['tubi','tubi','#fa382f'],['play store','googleplay','#01875f'],['kodi','kodi','#17b2e7'],['roku','roku','#6c1d99'],
  ['fandango','fandango','#ff7300'],['nbc','nbc','#444444'],['cbs','cbs','#0033a0']];
// Brands with no logo file: an initials badge in the brand colour
const COLOURS = [['disney','#113ccf'],['hulu','#1ce783','#000'],['peacock','#222'],['espn','#c00'],['home','#4a5a6a'],['live tv','#4a5a6a']];
// What to pin on a remote until you choose your own
const QUICK_WANTED = ['jellyfin', 'netflix', 'youtube', 'disney', 'prime video', 'hulu', 'plex', 'max'];

const S = {
  devices: [], devId: localStorage.getItem('devId') || '', status: {}, publicUrl: '',
  apps: {}, appsLoaded: {}, appsLoading: {}, appsError: {}, showSystem: false,
  sessions: [], sessSel: 'auto', items: {}, iconFiles: [], badIcons: new Set(),
  prefs: {quick: {}}, quickMax: 4, logAll: [], logLast: 0, build: null,
};
const W = {item: null, devId: null};
const Page = {};   // hooks a page can set: onSelect, onStatus, onSessions, onApps, onPrefs, onLog
const hook = (name, ...args) => Page[name] && Page[name](...args);
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
    const cls = res && res.ok && res.confirmed !== false ? 'ok' : 'bad';
    btn.classList.add(cls); setTimeout(() => btn.classList.remove(cls), 600);
  }
  fetchLog();
  return res;
}
const devPath = (p, id = S.devId) => `/api/devices/${encodeURIComponent(id)}${p}`;
const lastLine = t => String(t || 'failed').split('\n').filter(Boolean).pop();
const fmt = t => { t = Math.round(t); const h = Math.floor(t/3600), m = Math.floor(t%3600/60), s = t%60; return (h ? h + ':' + String(m).padStart(2,'0') : m) + ':' + String(s).padStart(2,'0'); };
const itemLabel = i => i.Type === 'Episode' ? `${i.SeriesName} S${i.Season}E${i.Episode} - ${i.Name}` : i.Name + (i.Year ? ` (${i.Year})` : '');

/* ------------------------------------------------------------------ icons */
function brandOf(app) {
  const hay = `${app.name} ${app.id}`.toLowerCase().replace(/[^a-z0-9+]+/g, ' ');
  const word = kw => new RegExp(`(^| )${kw}( |$)`).test(hay);
  const logo = LOGOS.find(l => word(l[0]));
  if (logo) return {logo: logo[1], color: logo[2]};
  const c = COLOURS.find(x => word(x[0]));
  return c ? {color: c[1], text: c[2]} : null;
}
function badge(name, brand) {
  let hue = 0; for (const c of String(name)) hue = (hue * 31 + c.charCodeAt(0)) % 360;
  const el = document.createElement('span');
  el.className = 'ico';
  el.style.background = brand ? brand.color : `hsl(${hue} 45% 38%)`;
  if (brand && brand.text) el.style.color = brand.text;
  el.textContent = String(name || '?').replace(/[^A-Za-z0-9+]/g, ' ').trim().split(/\s+/).slice(0, 2).map(w => w[0]).join('').toUpperCase() || '?';
  return el;
}
function iconHtml(d, app) {
  if (!app || !app.id) return badge(app && app.name || '?').outerHTML;
  const override = S.iconFiles.find(f => f.replace(/\.[^.]+$/, '') === app.id);   // a file you dropped into static/icons/
  if (override) return `<img class="ico" src="/static/icons/${encodeURIComponent(override)}" alt="">`;
  const key = `${d.id}:${app.id}`;
  if (d.capabilities.includes('icons') && !S.badIcons.has(key))                     // the device's own icon (Roku)
    return `<img class="ico" src="${esc(devPath('/icon/' + encodeURIComponent(app.id), d.id))}" data-bad="${esc(key)}" data-fallback="${esc(app.name)}" alt="">`;
  const brand = brandOf(app);
  if (brand && brand.logo) return `<span class="ico logo" style="--bg:${brand.color};--m:url(/static/icons/logos/${brand.logo}.svg)"></span>`;
  return badge(app.name, brand).outerHTML;
}
document.addEventListener('error', e => {   // a device icon that failed: remember it so it is never requested again
  const i = e.target;
  if (i.tagName === 'IMG' && i.dataset.bad) { S.badIcons.add(i.dataset.bad); i.replaceWith(badge(i.dataset.fallback, brandOf({name: i.dataset.fallback, id: ''}))); }
}, true);

/* ------------------------------------------------------------------ devices */
async function loadConfig() {
  const [c, p] = await Promise.all([api('GET', '/api/config'), api('GET', '/api/prefs')]);
  S.devices = c.devices || [];
  S.publicUrl = c.jellyfinPublicUrl || '';
  S.iconFiles = c.iconFiles || [];
  S.prefs = p.prefs || {quick: {}};
  S.quickMax = p.quickMax || 4;
  if (!dev()) S.devId = S.devices[0]?.id || '';
  const banner = $('#banner');
  if (banner) { banner.hidden = !c.configError; banner.textContent = c.configError || ''; }
  const fd = $('#fDevice');
  if (fd) fd.innerHTML = '<option value="">all devices</option>' + S.devices.map(d => `<option value="${esc(d.id)}">${esc(d.name)}</option>`).join('');
  renderDevBar(); selectDevice(S.devId, true);
}
const sessionFor = id => S.sessions.find(s => s.deviceId === id);
function playingTitle(id) {
  if (S.status[id] && !S.status[id].online) return null;   // a lingering Jellyfin session says nothing about an offline TV
  const s = sessionFor(id), n = s && s.NowPlaying;
  if (n) return {title: itemLabel(n), paused: s.PlayState?.IsPaused};
  const m = S.status[id]?.media;
  return m ? {title: m.title || (S.status[id].appName || 'media'), paused: m.state === 'paused'} : null;
}
function renderDevBar() {
  const el = $('#devbar'); if (!el) return;
  el.innerHTML = S.devices.map(d => {
    const st = S.status[d.id], pl = playingTitle(d.id);
    const cls = !st ? '' : !st.online ? 'offline' : st.power || '';
    const sub = !st ? '…' : !st.online ? (st.unresponsive ? 'asleep?' : 'offline') : [POWER_LABEL[st.power] || st.power, pl ? (pl.paused ? '⏸ ' : '▶ ') + pl.title : st.appName].filter(Boolean).join(' · ');
    return `<div class="dev ${d.id === S.devId ? 'sel' : ''}" data-act="selectdev" data-id="${esc(d.id)}"><span class="dot ${cls}"></span><div>${esc(d.name)}<div class="sub">${esc(sub)}</div></div></div>`;
  }).join('') || '<span class="dim small">No devices configured</span>';
}
function selectDevice(id, force) {
  if (id === S.devId && !force) return;
  S.devId = id; localStorage.setItem('devId', id); S.sessSel = 'auto';
  renderDevBar(); hook('onSelect'); loadApps();
}
async function pollStatus(d) {
  S.status[d.id] = await api('GET', `/api/devices/${encodeURIComponent(d.id)}/status?poll=1`);
  renderDevBar(); hook('onStatus', d);
}
const pollAll = () => Promise.all(S.devices.map(pollStatus));

/* ------------------------------------------------------------------ apps and quick launch */
async function loadApps(force) {
  const d = dev(); if (!d) return;
  if (S.appsLoading[d.id]) return;
  if (S.appsLoaded[d.id] && !force) { hook('onApps'); return; }
  S.appsLoading[d.id] = true; hook('onApps');
  const r = await api('GET', devPath('/apps', d.id) + (force ? '?refresh=1' : ''));
  S.appsLoading[d.id] = false;
  fetchLog();
  if (Array.isArray(r)) { S.apps[d.id] = r; S.appsLoaded[d.id] = true; delete S.appsError[d.id]; }
  else S.appsError[d.id] = r.body || 'Could not list apps';
  hook('onApps');
}
function defaultQuick(d) {
  const list = (S.apps[d.id] || []).filter(a => a.type !== 'tvin' && !a.system), out = [];
  for (const want of QUICK_WANTED) {
    const a = list.find(x => !out.includes(x) && `${x.name} ${x.id}`.toLowerCase().includes(want));
    if (a) out.push(a);
    if (out.length >= S.quickMax) break;
  }
  return out.map(a => ({id: a.id, name: a.name}));
}
const quickSaved = d => !!S.prefs.quick[d.id];
const quickApps = d => S.prefs.quick[d.id] || defaultQuick(d);
async function setQuick(d, apps) {
  S.prefs.quick[d.id] = apps.slice(0, S.quickMax); hook('onPrefs');
  const r = await api('PUT', `/api/prefs/quick/${encodeURIComponent(d.id)}`, {apps: S.prefs.quick[d.id]});
  if (!r.ok) alert(r.body || 'Could not save');
}
async function resetQuick(d) {
  delete S.prefs.quick[d.id]; hook('onPrefs');
  await api('DELETE', `/api/prefs/quick/${encodeURIComponent(d.id)}`);
}

/* ------------------------------------------------------------------ jellyfin sessions + now playing */
function activeSession() {
  if (S.sessSel !== 'auto') return S.sessions.find(s => s.Id === S.sessSel);
  return sessionFor(S.devId);
}
async function pollSessions() {
  const r = await api('GET', '/api/jf/sessions');
  const ok = Array.isArray(r), chip = $('#jfState');
  if (chip) { chip.textContent = ok ? `Jellyfin: ${r.length} session(s)` : 'Jellyfin: unreachable'; chip.style.color = ok ? '' : 'var(--bad)'; }
  if (!ok) return;
  S.sessions = r; hook('onSessions'); renderDevBar();
}
// What a device is doing right now, from its status and Jellyfin session (each page lays it out its own way)
function nowParts(d, s) {
  const st = S.status[d.id], offline = !!st && !st.online;
  const app = st && st.online ? ((S.apps[d.id] || []).find(a => a.id === st.app) || {id: st.app, name: st.appName || st.app || 'Unknown'}) : null;
  return {st, s, offline, app, n: !offline && s && s.NowPlaying, p: (s && s.PlayState) || {}, m: !offline && st && st.media};
}
function needSession() {
  const s = activeSession();
  if (!s) alert('No Jellyfin session for this device. Use “Prepare device” in Settings, or open the Jellyfin app on it.');
  return s;
}
function jfAct(btn, path, body) {
  const s = needSession(); if (!s) return;
  return run(btn, api('POST', path, {sessionId: s.Id, ...body})).then(r => { setTimeout(pollSessions, 700); return r; });
}

/* ------------------------------------------------------------------ "Watch a movie…" */
const WATCH_HTML = `<dialog id="watch">
  <div class="dlg-head"><b id="wTitle">Watch a movie</b><button data-act="watchClose" class="small">✕</button></div>
  <div id="wSearch">
    <div class="row"><input id="wq" placeholder="Search for a movie…"><button data-act="wsearch">Search</button></div>
    <div id="wResults"></div>
  </div>
  <div id="wChoose" hidden>
    <div id="wItem"></div>
    <label class="small" id="wResumeRow" hidden><input type="checkbox" id="wResume" checked> Resume at <span id="wResumeAt"></span></label>
    <div class="dim small" style="margin:8px 0 4px">Where do you want to watch?</div>
    <div id="wTargets"></div>
    <button data-act="watchBack" class="small" id="wBackSearch">← pick a different movie</button>
  </div>
  <div id="wProgress" hidden>
    <div id="wSteps" class="steps"></div>
    <div id="wResult"></div>
    <div class="row"><button data-act="watchAgain">Choose another place</button><button data-act="watchClose">Close</button></div>
  </div>
</dialog>`;
document.body.insertAdjacentHTML('beforeend', WATCH_HTML);
$('#wq').onkeydown = e => { if (e.key === 'Enter') watchSearch(); };

function openWatch(item) {
  const dlg = $('#watch');
  if (!dlg.open) dlg.showModal();
  if (item) chooseTarget(item); else showWatchStep('wSearch');
  // hints describe what will happen from each TV's current state, so make sure that state is fresh
  pollAll().then(() => { if (W.item && !$('#wChoose').hidden) chooseTarget(W.item); });
}
function showWatchStep(id) {
  ['wSearch', 'wChoose', 'wProgress'].forEach(x => $('#' + x).hidden = x !== id);
  $('#wTitle').textContent = id === 'wSearch' ? 'Watch a movie' : id === 'wChoose' ? 'Where to watch?' : 'Starting playback';
  if (id === 'wSearch') $('#wq').focus();
}
async function watchSearch() {
  const r = await api('GET', '/api/jf/search?q=' + encodeURIComponent($('#wq').value));
  if (!Array.isArray(r)) { $('#wResults').textContent = r.body || 'Search failed'; return; }
  const found = r.filter(i => i.Type !== 'Series');
  found.forEach(i => S.items[i.Id] = i);
  $('#wResults').innerHTML = found.map(i => `<div class="item"><span class="name">${esc(itemLabel(i))} <span class="dim small">${esc(i.Type)}</span></span><button data-act="watchItem" data-id="${esc(i.Id)}" class="primary">Select</button></div>`).join('') || '<span class="dim">No movies found</span>';
}
function stateHint(d) {
  const st = S.status[d.id];
  if (!st) return 'checking…';
  if (!st.online) return st.needsPairing ? 'needs pairing first' : `${st.unresponsive ? 'asleep' : 'unreachable'} — will try ${d.wakeMethods.join(', ')}, then open Jellyfin and play`;
  if (st.power !== 'on') return `${POWER_LABEL[st.power] || st.power} — will wake it, open Jellyfin and play`;
  if (/jellyfin/i.test(st.appName || '')) return 'in Jellyfin — will start playing right away';
  return `on, in ${st.appName || 'another app'} — will switch to Jellyfin and play`;
}
function chooseTarget(item) {
  W.item = item;
  $('#wItem').innerHTML = `<div class="np-title">${esc(itemLabel(item))}</div>`;
  const resume = item.ResumeTicks > 0;
  $('#wResumeRow').hidden = !resume;
  if (resume) $('#wResumeAt').textContent = fmt(item.ResumeTicks / TICKS);
  $('#wTargets').innerHTML = `<button class="target" data-act="watchPhone"><span class="ico badge-phone">📱</span><div><div class="t-name">This phone</div><div class="t-sub">opens the movie in Jellyfin (web) on this device</div></div></button>` +
    S.devices.map(d => {
      const st = S.status[d.id], cls = !st ? '' : !st.online ? 'offline' : st.power || '';
      return `<button class="target" data-act="watchOn" data-dev="${esc(d.id)}"><span class="dot ${cls}"></span><div><div class="t-name">${esc(d.name)}</div><div class="t-sub">${esc(stateHint(d))}</div></div></button>`;
    }).join('');
  $('#wBackSearch').hidden = !$('#wResults').innerHTML;
  showWatchStep('wChoose');
}
async function watchOn(devId) {
  const d = S.devices.find(x => x.id === devId), item = W.item;
  W.devId = devId;
  showWatchStep('wProgress');
  $('#wResult').innerHTML = '';
  $('#wSteps').innerHTML = `<div>Starting ${esc(itemLabel(item))} on ${esc(d.name)}…</div>`;
  const startId = S.logLast;
  const timer = setInterval(() => {   // live progress from the server log while the request is running
    const lines = S.logAll.filter(e => e.id > startId && e.device === devId && e.kind === 'system').map(e => `<div class="${e.ok === false ? 'bad' : ''}">${esc(e.response || e.action)}</div>`);
    if (lines.length) $('#wSteps').innerHTML = lines.join('');
  }, 700);
  const resume = !$('#wResumeRow').hidden && $('#wResume').checked;
  const r = await api('POST', devPath('/play', devId), {itemIds: [item.Id], playCommand: 'PlayNow', prepare: true,
    startPositionTicks: resume ? item.ResumeTicks : undefined});
  clearInterval(timer);
  fetchLog(); pollSessions(); pollStatus(d);
  $('#wSteps').innerHTML = (r.steps || [lastLine(r.body)]).map(x => `<div>${esc(x)}</div>`).join('');
  $('#wResult').innerHTML = r.ok && r.confirmed ? `<div class="wres good">✓ Now playing on ${esc(d.name)}</div>`
    : r.ok ? `<div class="wres warn">⚠ ${esc(d.name)} accepted the command, but Jellyfin never reported it playing. The app may not support remote play; see the log.</div>`
    : `<div class="wres fail">✗ ${esc(lastLine(r.body))}</div>`;
  if (r.ok) selectDevice(devId);
}
function watchPhone() {
  const base = (S.publicUrl || '').replace(/\/$/, '');
  if (!base) return alert('Jellyfin is not configured.');
  window.open(`${base}/web/index.html#/details?id=${encodeURIComponent(W.item.Id)}`, '_blank', 'noopener');
}

/* ------------------------------------------------------------------ actions shared by both pages */
const sendKey = (btn, key) => run(btn, api('POST', devPath('/key'), {key}));
const sendNative = (btn, native) => run(btn, api('POST', devPath('/key'), {native}));
const ACTIONS = {
  selectdev: b => selectDevice(b.dataset.id),
  key: b => sendKey(b, b.dataset.key),
  power: async b => { await run(b, api('POST', devPath('/power'), {state: b.dataset.state})); pollStatus(dev()); },
  launch: async b => { await run(b, api('POST', devPath('/launch'), {app: b.dataset.app})); setTimeout(() => pollStatus(dev()), 1500); },
  playstate: b => jfAct(b, '/api/jf/playstate', {command: b.dataset.cmd}),
  relseek: b => { const s = needSession(); if (s) jfAct(b, '/api/jf/playstate', {command: 'Seek', seekPositionTicks: Math.max(0, Math.round(((s.PlayState.PositionTicks || 0) / TICKS + Number(b.dataset.d)) * TICKS))}); },
  watch: () => openWatch(null),
  watchClose: () => $('#watch').close(),
  wsearch: () => watchSearch(),
  watchItem: b => openWatch(S.items[b.dataset.id]),
  watchBack: () => showWatchStep('wSearch'),
  watchAgain: () => chooseTarget(W.item),
  watchOn: b => watchOn(b.dataset.dev),
  watchPhone: () => watchPhone(),
};
document.addEventListener('click', e => {
  const b = e.target.closest('[data-act]');
  if (b && !b.disabled && ACTIONS[b.dataset.act]) ACTIONS[b.dataset.act](b);
});

/* ------------------------------------------------------------------ log (kept on every page; drawn where there is a #log) */
async function fetchLog() {
  if ($('#fPause')?.checked) return;
  const r = await api('GET', '/api/log?since=' + S.logLast);
  if (r.build) { if (!S.build) S.build = r.build; else if (S.build !== r.build) return location.reload(); }   // server was updated
  if (!r.entries || !r.entries.length) return;
  S.logLast = r.last;
  S.logAll = S.logAll.concat(r.entries).slice(-2000);
  hook('onLog');
}

/* ------------------------------------------------------------------ start */
async function startApp() {
  await loadConfig();
  pollAll(); pollSessions(); fetchLog();
  setInterval(pollAll, 5000);
  setInterval(pollSessions, 3000);
  setInterval(fetchLog, 1000);
}
