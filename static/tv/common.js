'use strict';
/* Shared by every page (Remote, Remote Apps, TVs, Remote Tools): state, polling, icons, app
   preferences, the "Watch a movie…" dialog, notices and the log. Pages plug in through Page hooks. */
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
  prefs: {quick: {}, apps: {}}, quickMax: 4, notices: [], logAll: [], logLast: 0, build: null,
};
const W = {item: null, devId: null, startOver: false};   // startOver: the link that opened this page said to begin at the start
const Page = {};   // hooks a page can set: onStart, onSelect, onStatus, onSessions, onApps, onPrefs, onLog (Page.noApps skips listing apps)
const hook = (name, ...args) => Page[name] && Page[name](...args);
const dev = () => S.devices.find(d => d.id === S.devId);
const has = cap => (dev()?.capabilities || []).includes(cap);

/* ------------------------------------------------------------------ api */
async function api(method, path, body) {
  try {
    const token = document.querySelector('meta[name="csrf-token"]')?.content;
    const headers = {...(body === undefined ? {} : {'Content-Type': 'application/json'}), ...(token ? {'X-CSRFToken': token} : {})};
    const r = await fetch(path, {method, headers, body: body === undefined ? undefined : JSON.stringify(body)});
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
// Everyday device calls are /api/tv/devices/..; probes, pairing, screenshots and the like need the TV setup permission
const SETUP_PATH = /^\/(sleep|screensaver|probe|diagnose|wol|detect-mac|pair|adb|connect|screenshot|prepare)/;
const devPath = (p, id = S.devId) => `/api/tv/${SETUP_PATH.test(p) ? 'setup/' : ''}devices/${encodeURIComponent(id)}${p}`;
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
  const override = S.iconFiles.find(f => f.replace(/\.[^.]+$/, '') === app.id);   // a file you dropped into static/tv/icons/
  if (override) return `<img class="ico" src="/static/tv/icons/${encodeURIComponent(override)}" alt="">`;
  const key = `${d.id}:${app.id}`;
  if (d.capabilities.includes('icons') && !S.badIcons.has(key))                     // the device's own icon (Roku)
    return `<img class="ico" src="${esc(devPath('/icon/' + encodeURIComponent(app.id), d.id))}" data-bad="${esc(key)}" data-fallback="${esc(app.name)}" alt="">`;
  const brand = brandOf(app);
  if (brand && brand.logo) return `<span class="ico logo" style="--bg:${brand.color};--m:url(/static/tv/logos/${brand.logo}.svg)"></span>`;
  return badge(app.name, brand).outerHTML;
}
document.addEventListener('error', e => {   // a device icon that failed: remember it so it is never requested again
  const i = e.target;
  if (i.tagName === 'IMG' && i.dataset.bad) { S.badIcons.add(i.dataset.bad); i.replaceWith(badge(i.dataset.fallback, brandOf({name: i.dataset.fallback, id: ''}))); }
}, true);

/* ------------------------------------------------------------------ devices */
async function loadConfig() {
  const [c, p] = await Promise.all([api('GET', '/api/tv/config'), api('GET', '/api/tv/prefs')]);
  S.devices = c.devices || [];
  S.publicUrl = c.jellyfinPublicUrl || '';
  S.iconFiles = c.iconFiles || [];
  S.prefs = {quick: {}, apps: {}, ...(p.prefs || {})};
  S.quickMax = p.quickMax || 4;
  const wanted = new URLSearchParams(location.search).get('tv');   // a link such as Watch now's opens a particular TV's remote
  if (wanted && S.devices.some(d => d.id === wanted)) S.devId = wanted;
  if (!dev()) S.devId = S.devices[0]?.id || '';
  const banner = $('#banner');
  if (banner) { banner.hidden = !c.configError; banner.textContent = c.configError || ''; }
  const fd = $('#fDevice');
  if (fd) fd.innerHTML = '<option value="">all devices</option>' + S.devices.map(d => `<option value="${esc(d.id)}">${esc(d.name)}</option>`).join('');
  renderDevBar(); selectDevice(S.devId, true);
}
// Re-read the device list (its session rules change when one is chosen on the Remote Tools page)
async function refreshDevices() {
  const c = await api('GET', '/api/tv/config');
  if (c.devices) S.devices = c.devices;
  hook('onSessions');
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
  S.status[d.id] = await api('GET', `/api/tv/devices/${encodeURIComponent(d.id)}/status?poll=1`);
  renderDevBar(); hook('onStatus', d);
}
const pollAll = () => Promise.all(S.devices.map(pollStatus));

/* ------------------------------------------------------------------ apps and quick launch */
async function loadApps(force) {
  const d = dev(); if (!d || Page.noApps) return;
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
  const r = await api('PUT', `/api/tv/prefs/quick/${encodeURIComponent(d.id)}`, {apps: S.prefs.quick[d.id]});
  if (!r.ok) alert(r.body || 'Could not save');
}
async function resetQuick(d) {
  delete S.prefs.quick[d.id]; hook('onPrefs');
  await api('DELETE', `/api/tv/prefs/quick/${encodeURIComponent(d.id)}`);
}

// Apps shown on the remote beyond the quick-launch row (chosen on the Remote Apps page; none until chosen)
const visibleApps = d => S.prefs.apps[d.id] || [];
async function setVisible(d, apps) {
  S.prefs.apps[d.id] = apps; hook('onPrefs');
  const r = await api('PUT', `/api/tv/prefs/apps/${encodeURIComponent(d.id)}`, {apps});
  if (!r.ok) alert(r.body || 'Could not save');
}
async function resetVisible(d) {
  delete S.prefs.apps[d.id]; hook('onPrefs');
  await api('DELETE', `/api/tv/prefs/apps/${encodeURIComponent(d.id)}`);
}
function renderQuickStrip() {
  const d = dev(), el = $('#quickStrip'); if (!d || !el) return;
  const list = quickApps(d);
  el.innerHTML = `<div class="small"><b>Quick launch</b> ${quickSaved(d) ? '' : '<span class="dim">(defaults. Pin or reorder to save your own)</span>'}
      ${quickSaved(d) ? '<button class="small" data-act="qreset">reset to defaults</button>' : ''}</div>` +
    (list.length ? `<div class="qrow">${list.map((a, i) => `<span class="qchip">${iconHtml(d, a)}<span>${esc(a.name)}</span>
        <button class="small" data-act="qmove" data-i="${i}" data-d="-1" ${i === 0 ? 'disabled' : ''} title="Move earlier">‹</button>
        <button class="small" data-act="qmove" data-i="${i}" data-d="1" ${i === list.length - 1 ? 'disabled' : ''} title="Move later">›</button>
        <button class="small" data-act="qdel" data-i="${i}" title="Remove">✕</button></span>`).join('')}</div>`
      : '<div class="dim small">Nothing pinned yet.</div>');
}

/* ------------------------------------------------------------------ what's-new notices */
function seenNotices() { try { return JSON.parse(localStorage.getItem('seenNotices') || '[]'); } catch (_) { return []; } }
function markSeen(id) { try { localStorage.setItem('seenNotices', JSON.stringify([...new Set([...seenNotices(), id])])); } catch (_) {} }
async function loadNotices() {
  const r = await api('GET', '/api/tv/notices');
  S.notices = r.notices || [];
  S.notices.filter(n => n.href === location.pathname).forEach(n => markSeen(n.id));   // you are already looking at it
  renderNotices(); hook('onNotices');
}
function renderNotices() {   // one at a time, so a notice never pushes the remote off a phone screen
  const el = $('#notices'); if (!el) return;
  const seen = seenNotices(), unseen = S.notices.filter(n => !seen.includes(n.id)), n = unseen[0];
  el.innerHTML = !n ? '' : `<div class="notice" role="status"><div class="ntext"><b>${esc(n.title)}</b>${unseen.length > 1 ? ` <span class="dim small">(${unseen.length - 1} more)</span>` : ''}<div class="small">${esc(n.body)}</div></div>
    <div class="nbtns">${n.href ? `<a class="tv-btn primary" href="${esc(n.href)}" data-notice="${esc(n.id)}">${esc(n.action || 'Open')}</a>` : ''}<button data-act="dismissNotice" data-id="${esc(n.id)}">Dismiss</button></div></div>`;
}
document.addEventListener('click', e => { const a = e.target.closest('[data-notice]'); if (a) markSeen(a.dataset.notice); });

/* ------------------------------------------------------------------ the playback clock (see clock.js)
   The position text and bar of a playing item are drawn empty and filled in by tickClocks(), which counts forward in the browser between
   the server's updates, so they run smoothly instead of jumping at each poll. */
const clockText = (key, len) => `<span data-clock="text" data-key="${esc(key)}" data-len="${len || 0}"></span>`;
const clockBar = (key, len) => `<div class="bar"><div data-clock="fill" data-key="${esc(key)}" data-len="${len || 0}" style="width:0"></div></div>`;
function tickClocks() {
  document.querySelectorAll('[data-clock]').forEach(el => {
    const len = Number(el.dataset.len), pos = Clock.position(el.dataset.key);
    if (el.dataset.clock === 'text') el.textContent = len ? `${fmt(Math.floor(pos))} / ${fmt(len)}` : `at ${fmt(Math.floor(pos))}`;   // whole seconds, rounded down like a player
    else el.style.width = len ? Math.min(100, pos / len * 100) + '%' : '0';
  });
}
setInterval(tickClocks, 250);
const clockKey = (devId, id) => `${devId}:${id}`;
const activeClockKey = () => { const s = activeSession(); return s && s.NowPlaying ? clockKey(S.devId, s.NowPlaying.Id) : null; };

/* ------------------------------------------------------------------ jellyfin sessions + now playing */
function activeSession() {
  if (S.sessSel !== 'auto') return S.sessions.find(s => s.Id === S.sessSel);
  return sessionFor(S.devId);
}
async function pollSessions() {
  const r = await api('GET', '/api/tv/jf/sessions');
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
  if (!s) alert('No Jellyfin session for this device. Use “Prepare device” on Remote Tools, or open the Jellyfin app on it.');
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
    <div class="tv-row"><input id="wq" placeholder="Search for a movie…"><button data-act="wsearch">Search</button></div>
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
    <div class="tv-row"><button data-act="watchRetry" id="wRetry" hidden>Try again</button><button data-act="watchAgain" id="wAgain">Choose another place</button><button data-act="watchClose" id="wClose">Close</button></div>
  </div>
</dialog>`;
($('.tvr') || document.body).insertAdjacentHTML('beforeend', WATCH_HTML);
$('#wq').onkeydown = e => { if (e.key === 'Enter') watchSearch(); };
$('#watch').addEventListener('click', e => { if (e.target === e.currentTarget && W.done === 'good') e.currentTarget.close(); });   // a tap outside closes it, but only once playback started

function openWatch(item) {
  W.startOver = false;                    // the search dialog has its own "Resume at" choice
  const dlg = $('#watch');
  if (!dlg.open) dlg.showModal();
  if (item) chooseTarget(item); else showWatchStep('wSearch');
  // hints describe what will happen from each TV's current state, so make sure that state is fresh
  pollAll().then(() => { if (W.item && !$('#wChoose').hidden) chooseTarget(W.item); });
}
function showWatchStep(id) {
  if (id !== 'wProgress') W.done = null;
  ['wSearch', 'wChoose', 'wProgress'].forEach(x => $('#' + x).hidden = x !== id);
  $('#wTitle').textContent = id === 'wSearch' ? 'Watch a movie' : id === 'wChoose' ? 'Where to watch?' : 'Starting playback';
  if (id === 'wSearch') $('#wq').focus();
}
async function watchSearch() {
  const r = await api('GET', '/api/tv/jf/search?q=' + encodeURIComponent($('#wq').value));
  if (!Array.isArray(r)) { $('#wResults').textContent = r.body || 'Search failed'; return; }
  const found = r.filter(i => i.Type !== 'Series');
  found.forEach(i => S.items[i.Id] = i);
  $('#wResults').innerHTML = found.map(i => `<div class="item"><span class="name">${esc(itemLabel(i))} <span class="dim small">${esc(i.Type)}</span></span><button data-act="watchItem" data-id="${esc(i.Id)}" class="primary">Select</button></div>`).join('') || '<span class="dim">No movies found</span>';
}
function stateHint(d) {
  const st = S.status[d.id];
  if (!st) return 'checking…';
  if (d.capabilities.includes('deeplink') && ['auto', 'deeplink'].includes(d.playVia))   // opens the Jellyfin app straight onto the movie
    return !st.online ? `${st.unresponsive ? 'asleep' : 'unreachable'} — will try ${d.wakeMethods.join(', ')}, then open Jellyfin and play`
      : st.power !== 'on' ? 'standby — will wake it, open Jellyfin and play (a deep link starts from the beginning unless the app can be moved)' : 'will open the Jellyfin app on the movie (starts from the beginning unless the app can be moved)';
  if (d.playVia === 'auto' && st.online && st.power === 'on')
    return 'will use the Jellyfin app if it accepts remote control, otherwise stream the movie directly';
  if (d.playVia === 'url')   // streams straight from Jellyfin: no Jellyfin app or session involved
    return !st.online ? `${st.unresponsive ? 'asleep' : 'unreachable'} — will try ${d.wakeMethods.join(', ')}, then stream it directly`
      : st.power !== 'on' ? 'standby — will wake it, then stream it directly' : 'will start the stream directly (no Jellyfin app needed)';
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
  W.devId = devId; W.done = null;
  showWatchStep('wProgress');
  $('#wResult').innerHTML = '';
  setWatchButtons(null);
  $('#wSteps').innerHTML = `<div>Starting ${esc(itemLabel(item))} on ${esc(d.name)}…</div>`;
  const startId = S.logLast;
  const timer = setInterval(() => {   // live progress from the server log while the request is running
    const lines = S.logAll.filter(e => e.id > startId && e.device === devId && e.kind === 'system').map(e => `<div class="${e.ok === false ? 'bad' : ''}">${esc(e.response || e.action)}</div>`);
    if (lines.length) $('#wSteps').innerHTML = lines.join('');
  }, 700);
  // Where to start: the dialog's own "Resume at" box decides when it is showing; a link that said start=0 starts over; otherwise the server
  // picks up where this profile's Jellyfin user left off (sending 0 means the beginning, sending nothing means "wherever they stopped").
  const offered = !$('#wResumeRow').hidden, resume = offered && $('#wResume').checked;
  const r = await api('POST', devPath('/play', devId), {itemIds: [item.Id], playCommand: 'PlayNow', prepare: true,
    startPositionTicks: resume ? item.ResumeTicks : (offered || W.startOver) ? 0 : undefined});
  clearInterval(timer);
  fetchLog(); pollSessions(); pollStatus(d);
  $('#wSteps').innerHTML = (r.steps || [lastLine(r.body)]).map(x => `<div>${esc(x)}</div>`).join('');
  $('#wResult').innerHTML = r.ok && r.confirmed ? `<div class="wres good">✓ Now playing on ${esc(d.name)}</div>`
    : r.ok ? `<div class="wres warn">⚠ ${esc(d.name)} accepted the command, but Jellyfin never reported it playing. The app may not support remote play; see the log.</div>`
    : `<div class="wres fail">✗ ${esc(lastLine(r.body))}</div>`;
  W.done = r.ok && r.confirmed ? 'good' : 'problem';
  setWatchButtons(W.done);
  if (r.learned) refreshDevices();   // the app learned this device cannot be remote controlled and now streams
  if (r.ok) selectDevice(devId);
}
/* Playing: the box is only in the way, so Close is the main button. Anything else: it stays open, with the steps to read and a way to try again. */
function setWatchButtons(outcome) {
  $('#wRetry').hidden = outcome !== 'problem';
  $('#wAgain').hidden = outcome === 'good';
  $('#wClose').classList.toggle('primary', outcome === 'good');
  if (outcome === 'good') $('#wClose').focus();
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
  openjf: async b => { await run(b, api('POST', devPath('/jellyfin'))); setTimeout(() => pollStatus(dev()), 1500); },
  launch: async b => { await run(b, api('POST', devPath('/launch'), {app: b.dataset.app})); setTimeout(() => pollStatus(dev()), 1500); },
  playstate: b => {
    const key = activeClockKey(), cmd = b.dataset.cmd;   // pause and resume show at once; the server confirms a moment later
    if (key && ['PlayPause', 'Pause', 'Unpause'].includes(cmd)) { Clock.setPlaying(key, cmd === 'PlayPause' ? undefined : cmd === 'Unpause'); hook('onSessions'); tickClocks(); }
    return jfAct(b, '/api/tv/jf/playstate', {command: cmd});
  },
  relseek: b => {
    const s = needSession(); if (!s) return;
    const key = activeClockKey(), at = key ? Clock.position(key) : (s.PlayState.EstimatedTicks ?? s.PlayState.PositionTicks ?? 0) / TICKS;
    if (key) { Clock.shift(key, Number(b.dataset.d)); hook('onSessions'); tickClocks(); }
    jfAct(b, '/api/tv/jf/playstate', {command: 'Seek', seekPositionTicks: Math.max(0, Math.round((at + Number(b.dataset.d)) * TICKS))});
  },
  watch: () => openWatch(null),
  watchClose: () => $('#watch').close(),
  wsearch: () => watchSearch(),
  watchItem: b => openWatch(S.items[b.dataset.id]),
  watchBack: () => showWatchStep('wSearch'),
  watchAgain: () => chooseTarget(W.item),
  watchRetry: () => watchOn(W.devId),
  watchOn: b => watchOn(b.dataset.dev),
  watchPhone: () => watchPhone(),
  dismissNotice: b => { markSeen(b.dataset.id); renderNotices(); },
  pin: b => {
    const d = dev(), cur = quickApps(d), i = cur.findIndex(a => a.id === b.dataset.app);
    if (i >= 0) return setQuick(d, cur.filter((_, n) => n !== i));
    if (cur.length >= S.quickMax) return alert(`Quick launch holds ${S.quickMax} apps. Remove one first.`);
    setQuick(d, [...cur, {id: b.dataset.app, name: b.dataset.name}]);
  },
  qdel: b => { const d = dev(); setQuick(d, quickApps(d).filter((_, n) => n !== Number(b.dataset.i))); },
  qmove: b => {
    const d = dev(), cur = [...quickApps(d)], i = Number(b.dataset.i), j = i + Number(b.dataset.d);
    if (j < 0 || j >= cur.length) return;
    [cur[i], cur[j]] = [cur[j], cur[i]]; setQuick(d, cur);
  },
  qreset: () => resetQuick(dev()),
};
document.addEventListener('click', e => {
  const b = e.target.closest('[data-act]');
  if (b && !b.disabled && ACTIONS[b.dataset.act]) ACTIONS[b.dataset.act](b);
});

/* ------------------------------------------------------------------ log (kept on every page; drawn where there is a #log) */
async function fetchLog() {
  if ($('#fPause')?.checked) return;
  const r = await api('GET', '/api/tv/log?since=' + S.logLast);
  if (r.build) { if (!S.build) S.build = r.build; else if (S.build !== r.build) return location.reload(); }   // server was updated
  if (!r.entries || !r.entries.length) return;
  S.logLast = r.last;
  S.logAll = S.logAll.concat(r.entries).slice(-2000);
  hook('onLog');
}

/* ------------------------------------------------------------------ start */
async function startApp() {
  loadNotices();
  await loadConfig();
  hook('onStart');
  pollAll(); pollSessions(); fetchLog();
  setInterval(pollAll, 5000);
  setInterval(pollSessions, 3000);
  setInterval(fetchLog, 1000);
}
