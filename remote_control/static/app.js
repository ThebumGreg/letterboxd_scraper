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
// Logos (simple-icons, CC0) drawn white on the brand colour. Matched as whole words against "name package".
const LOGOS = [['jellyfin','jellyfin','#00a4dc'],['netflix','netflix','#e50914'],['youtube tv','youtubetv','#ff0000'],['youtube music','youtubemusic','#ff0000'],
  ['youtube','youtube','#ff0000'],['prime video','primevideo','#00a8e1'],['prime','primevideo','#00a8e1'],['max','max','#5822b4'],['plex','plex','#e5a00d'],
  ['spotify','spotify','#1db954'],['apple tv','appletv','#3a3a3c'],['apple','appletv','#3a3a3c'],['paramount','paramountplus','#0064ff'],['twitch','twitch','#9146ff'],
  ['crunchyroll','crunchyroll','#f47521'],['tubi','tubi','#fa382f'],['play store','googleplay','#01875f'],['kodi','kodi','#17b2e7'],['roku','roku','#6c1d99'],
  ['fandango','fandango','#ff7300'],['nbc','nbc','#444444'],['cbs','cbs','#0033a0']];
// Brands with no logo file: an initials badge in the brand colour
const COLOURS = [['disney','#113ccf'],['hulu','#1ce783','#000'],['peacock','#222'],['espn','#c00'],['home','#4a5a6a'],['live tv','#4a5a6a']];

const S = {
  devices: [], devId: localStorage.getItem('devId') || '', status: {}, apps: {}, appsLoaded: {}, publicUrl: '',
  sessions: [], sessSel: 'auto', logAll: [], logLast: 0, keyboard: localStorage.getItem('kbd') === '1', sig: '', items: {},
  iconFiles: [], badIcons: new Set(), showSystem: false,
};
const W = {item: null, devId: null};
const POWER_LABEL = {dreaming: 'screensaver'};
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

/* ------------------------------------------------------------------ icons */
function brandOf(app) {
  const hay = `${app.name} ${app.id}`.toLowerCase().replace(/[^a-z0-9+]+/g, ' ');
  const has = kw => new RegExp(`(^| )${kw}( |$)`).test(hay);
  const logo = LOGOS.find(l => has(l[0]));
  if (logo) return {logo: logo[1], color: logo[2]};
  const c = COLOURS.find(x => has(x[0]));
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
  const c = await api('GET', '/api/config');
  S.devices = c.devices || [];
  S.publicUrl = c.jellyfinPublicUrl || '';
  S.iconFiles = c.iconFiles || [];
  if (!dev()) S.devId = S.devices[0]?.id || '';
  const banner = $('#banner');
  banner.hidden = !c.configError;
  banner.textContent = c.configError || '';
  $('#fDevice').innerHTML = '<option value="">all devices</option>' + S.devices.map(d => `<option value="${esc(d.id)}">${esc(d.name)}</option>`).join('');
  renderDevBar(); selectDevice(S.devId, true);
}
function sessionFor(id) { return S.sessions.find(s => s.deviceId === id); }
function playingTitle(id) {
  if (S.status[id] && !S.status[id].online) return null;   // a lingering Jellyfin session says nothing about an offline TV
  const s = sessionFor(id), n = s && s.NowPlaying;
  if (n) return {title: itemLabel(n), paused: s.PlayState?.IsPaused};
  const m = S.status[id]?.media;
  return m ? {title: m.title || (S.status[id].appName || 'media'), paused: m.state === 'paused'} : null;
}
function renderDevBar() {
  $('#devbar').innerHTML = S.devices.map(d => {
    const st = S.status[d.id], pl = playingTitle(d.id);
    const cls = !st ? '' : !st.online ? 'offline' : st.power || '';
    const sub = !st ? '…' : !st.online ? (st.unresponsive ? 'asleep?' : 'offline') : [POWER_LABEL[st.power] || st.power, pl ? (pl.paused ? '⏸ ' : '▶ ') + pl.title : st.appName].filter(Boolean).join(' · ');
    return `<div class="dev ${d.id === S.devId ? 'sel' : ''}" data-act="selectdev" data-id="${esc(d.id)}"><span class="dot ${cls}"></span><div>${esc(d.name)}<div class="sub">${esc(sub)}</div></div></div>`;
  }).join('') || '<span class="dim small">No devices configured</span>';
}
function selectDevice(id, force) {
  if (id === S.devId && !force) return;
  S.devId = id; localStorage.setItem('devId', id); S.sessSel = 'auto';
  renderDevBar(); renderRemote(); renderSessions(); loadApps();
}
async function pollStatus(d) {
  S.status[d.id] = await api('GET', `/api/devices/${encodeURIComponent(d.id)}/status?poll=1`);
  renderDevBar();
  if (d.id === S.devId) { renderRemoteStatus(); renderNow(); }
}
const pollAll = () => Promise.all(S.devices.map(pollStatus));

/* ------------------------------------------------------------------ remote */
function renderRemote() {
  const d = dev(), el = $('#remote');
  if (!d) { el.innerHTML = '<div class="dim">No device selected</div>'; return; }
  const k = key => `<button data-act="key" data-key="${key}" ${d.keys[key] ? '' : 'disabled'} title="${key}">${KEY_LABELS[key] || key}</button>`;
  const dp = (key, label, cls = '') => `<button data-act="key" data-key="${key}" class="${cls}" ${d.keys[key] ? '' : 'disabled'} title="${key}">${label}</button>`;
  const pairing = has('pairing') ? `<div class="pair" id="pairBox" hidden><b>Pairing required</b>
      <div class="small dim">Press Start, then type the code that appears on the TV.</div>
      <div class="row"><button data-act="pairStart">Start pairing</button><input id="pairCode" placeholder="code on TV" maxlength="8" style="max-width:120px"><button data-act="pairFinish">Finish</button></div></div>` : '';
  const sleepLabel = d.sleepMode === 'screensaver' ? 'Sleep (screensaver)' : 'Sleep (standby)';
  const power = has('power_on') ? `<div class="rgrid r2"><button data-act="power" data-state="on" class="pwr">⏻ Wake / power on</button><button data-act="power" data-state="off" class="pwr" title="This device's sleep mode is ${esc(d.sleepMode)} (set &quot;sleep&quot; in devices.json)">⏻ ${sleepLabel}</button></div>
    <div class="rgrid r2">${has('screensaver') ? '<button data-act="screensaver" title="Soft sleep: the TV stays reachable and any key wakes it">🖼 Screensaver</button>' : ''}<button data-act="wol" title="Send a Wake-on-LAN magic packet">⚡ Wake-on-LAN</button><button data-act="detectMac" title="Read the device's MAC address (it must be on)">Detect MAC</button><button data-act="probe" title="Which network ports answer right now? Run it on and in standby">Probe network</button>${has('diagnose') ? '<button data-act="diagnose" title="Read power, screensaver and network settings from the TV">Diagnose</button>' : ''}</div>
    <div class="small dim" id="macLine" style="text-align:center"></div>` : '';
  const more = d.extraKeys.length ? `<details class="adv"><summary>More keys</summary><div class="rgrid r2">${d.extraKeys.map(x => `<button data-act="rawkey" data-native="${esc(x.key)}" title="${esc(x.key)}">${esc(x.label)}</button>`).join('')}</div></details>` : '';
  const text = has('text') ? `<div class="row"><input id="txt" placeholder="Type on the TV"><button data-act="text">Send</button></div>` : '';
  const shot = has('screenshot') ? `<button data-act="shot">Screenshot</button>` : '';
  el.innerHTML = `
    <h3>${esc(d.name)}</h3>
    <div class="rstat small dim" id="rstat"></div>
    ${pairing}${power}
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
    <img id="shot" alt="" hidden style="max-width:100%;margin-top:6px;border-radius:8px">
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
    ? `<span class="chip">${esc(POWER_LABEL[st.power] || st.power)}</span><span class="chip">${esc(st.appName || st.app || 'unknown app')}</span>` +
      (st.volume ? `<span class="chip">vol ${esc(st.volume.level)}/${esc(st.volume.max)}${st.volume.muted ? ' muted' : ''}</span>` : '')
    : st.unresponsive ? `<span class="chip" style="color:var(--warn)">asleep? not answering</span> ${esc(st.error || '')}`
    : `<span class="chip" style="color:var(--bad)">offline</span> ${esc(st.error || '')}`;
  const pair = $('#pairBox'); if (pair) pair.hidden = !(st && st.needsPairing);
  const mac = $('#macLine'); if (mac) mac.textContent = st && st.mac ? `MAC ${st.mac}` : 'MAC unknown (press Detect MAC while the device is on)';
}
const sendKey = (btn, key) => run(btn, api('POST', devPath('/key'), {key}));
const sendNative = (btn, native) => run(btn, api('POST', devPath('/key'), {native}));
document.addEventListener('keydown', e => {
  if (!S.keyboard || !S.devId || /^(INPUT|TEXTAREA|SELECT)$/.test(e.target.tagName) || e.ctrlKey || e.metaKey || e.altKey || $('#watch').open) return;
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
  const d = dev(); if (!d) return;
  const list = S.apps[d.id] || [], st = S.status[d.id] || {};
  const tile = a => `<button data-act="launch" data-app="${esc(a.id)}" class="tile ${a.id === st.app ? 'active' : ''}" title="${esc(a.id)}">${iconHtml(d, a)}<span class="n">${esc(a.name)}</span></button>`;
  const system = list.filter(a => a.system), inputs = list.filter(a => a.type === 'tvin');
  const apps = list.filter(a => a.type !== 'tvin' && (S.showSystem || !a.system));
  $('#apps').innerHTML = (apps.map(tile).join('') +
    (inputs.length ? '<div class="grp small dim">TV inputs</div>' + inputs.map(tile).join('') : '') +
    (system.length ? `<div class="grp small dim"><label><input type="checkbox" id="showSys" ${S.showSystem ? 'checked' : ''}> show ${system.length} system app(s)</label></div>` : '')) || '<span class="dim">No apps found</span>';
}
document.addEventListener('change', e => { if (e.target.id === 'showSys') { S.showSystem = e.target.checked; renderApps(); } });

/* ------------------------------------------------------------------ jellyfin sessions + now playing */
function activeSession() {
  if (S.sessSel !== 'auto') return S.sessions.find(s => s.Id === S.sessSel);
  return sessionFor(S.devId);
}
async function pollSessions() {
  const r = await api('GET', '/api/jf/sessions');
  const ok = Array.isArray(r);
  $('#jfState').textContent = ok ? `Jellyfin: ${r.length} session(s)` : 'Jellyfin: unreachable';
  $('#jfState').style.color = ok ? '' : 'var(--bad)';
  if (!ok) return;
  S.sessions = r; renderSessions(); renderDevBar();
}
function renderSessions() {
  const sig = S.sessions.map(s => s.Id + s.Client + s.DeviceName + s.deviceId).join('|') + '#' + S.sessSel + '#' + S.devId;
  if (sig !== S.sig) {
    S.sig = sig;
    const mine = sessionFor(S.devId);
    $('#sessSel').innerHTML = `<option value="auto">Auto: ${mine ? esc(mine.Client + ' / ' + mine.DeviceName) : 'none matched for ' + esc(dev()?.name || 'device')}</option>` +
      S.sessions.map(s => `<option value="${esc(s.Id)}" title="DeviceId ${esc(s.DeviceId)}">${esc(s.Client)} - ${esc(s.DeviceName)} (${esc(s.UserName)})</option>`).join('');
    $('#sessSel').value = S.sessSel;
  }
  renderNow(); renderCaps();
}
const fmt = t => { t = Math.round(t); const h = Math.floor(t/3600), m = Math.floor(t%3600/60), s = t%60; return (h ? h + ':' + String(m).padStart(2,'0') : m) + ':' + String(s).padStart(2,'0'); };
const itemLabel = i => i.Type === 'Episode' ? `${i.SeriesName} S${i.Season}E${i.Episode} - ${i.Name}` : i.Name + (i.Year ? ` (${i.Year})` : '');
function renderCaps() {
  const s = activeSession(), sup = s ? s.SupportedCommands : [];
  $('#caps').innerHTML = s ? `<span class="chip">RemoteControl: ${s.SupportsRemoteControl}</span>` + sup.map(c => `<span class="chip">${esc(c)}</span>`).join('') : '<span class="dim small">No session</span>';
  document.querySelectorAll('#cmds button').forEach(b => b.classList.toggle('unsup', !sup.includes(b.dataset.cmd)));
  document.querySelectorAll('#transport button').forEach(b => b.classList.toggle('unsup', !sup.includes('PlayState')));
}
function renderNow() {
  const d = dev(), st = S.status[S.devId], s = activeSession(), el = $('#now');
  if (!d) { el.textContent = 'Select a device'; return; }
  const app = st && st.online ? ((S.apps[d.id] || []).find(a => a.id === st.app) || {id: st.app, name: st.appName || st.app || 'Unknown'}) : null;
  const head = app ? `<div class="np-head">${iconHtml(d, app)}<div><div class="np-app">${esc(app.name)}</div>
      <div><span class="chip">${esc(st.power)}</span>${st.volume ? `<span class="chip">vol ${esc(st.volume.level)}/${esc(st.volume.max)}${st.volume.muted ? ' muted' : ''}</span>` : ''}</div></div></div>`
    : `<div class="np-head"><div><div class="np-app">${esc(d.name)}</div><div class="np-sub">${st ? (st.online ? '' : 'offline — ' + esc(st.error || 'not reachable')) : 'checking…'}</div></div></div>`;
  const offline = st && !st.online;
  const n = !offline && s && s.NowPlaying, p = (s && s.PlayState) || {}, m = st && st.media;
  let body;
  if (n) {
    const pos = (p.PositionTicks || 0) / TICKS, len = (n.RunTimeTicks || 0) / TICKS;
    body = `<div class="np-title">${esc(itemLabel(n))}</div>
      <div class="np-sub">${[p.IsPaused ? '⏸ paused' : '▶ playing', `via Jellyfin (${s.Client})`,
        p.VolumeLevel != null && `volume ${p.VolumeLevel}${p.IsMuted ? ' (muted)' : ''}`,
        p.AudioStreamIndex != null && `audio ${p.AudioStreamIndex}`, p.SubtitleStreamIndex != null && `subs ${p.SubtitleStreamIndex}`,
        p.PlayMethod].filter(Boolean).map(esc).join(' · ')}</div>
      <div class="bar"><div style="width:${len ? Math.min(100, pos/len*100) : 0}%"></div></div>
      <div class="small dim">${fmt(pos)} / ${fmt(len)}</div>`;
  } else if (m) {
    const pos = (m.positionMs || 0) / 1000, len = (m.durationMs || 0) / 1000;
    body = `<div class="np-title">${esc(m.title || (app ? app.name : 'Playing'))}</div>
      <div class="np-sub">${m.state === 'paused' ? '⏸ paused' : m.state === 'playing' ? '▶ playing' : esc(m.state)} · ${esc(m.source)}${m.title ? '' : ' (this app does not report a title)'}</div>
      ${len ? `<div class="bar"><div style="width:${Math.min(100, pos/len*100)}%"></div></div><div class="small dim">${fmt(pos)} / ${fmt(len)}</div>` : (m.positionMs != null ? `<div class="small dim">at ${fmt(pos)}</div>` : '')}`;
  } else {
    body = `<div class="np-sub">${st && st.online ? (s ? `Jellyfin session ready (${esc(s.Client)} / ${esc(s.DeviceName)}): nothing playing` : 'Nothing playing, and no Jellyfin session for this device') : ''}</div>`;
  }
  el.className = '';
  el.innerHTML = head + body;
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
    $('#prepState').textContent = !r.ok ? lastLine(r.body) : r.confirmed === false ? 'accepted, but not playing' : r.confirmed ? 'playing' : 'sent';
    pollSessions(); pollStatus(dev());
    return r;
  }
  return jfAct(btn, '/api/jf/play', body);   // an explicit session was chosen
}
function itemRow(i, extra = '') {
  S.items[i.Id] = i;
  const id = esc(i.Id);
  return `<div class="item"><span class="name">${esc(itemLabel(i))} <span class="dim small">${esc(i.Type)}</span></span>${extra}
    ${i.Type === 'Series' ? '' : `<button data-act="watchItem" data-id="${id}" class="primary">Watch…</button>`}
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

/* ------------------------------------------------------------------ "Watch a movie…" */
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
  const mac = st.mac || d.hasMac;
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
  if (r.ok) { S.devId = devId; localStorage.setItem('devId', devId); renderDevBar(); renderRemote(); renderApps(); }
}
function watchPhone() {
  const base = (S.publicUrl || '').replace(/\/$/, '');
  if (!base) return alert('Jellyfin is not configured.');
  window.open(`${base}/web/index.html#/details?id=${encodeURIComponent(W.item.Id)}`, '_blank', 'noopener');
}

/* ------------------------------------------------------------------ click dispatch */
const ACTIONS = {
  selectdev: (b) => selectDevice(b.dataset.id),
  reload: async () => { await api('POST', '/api/reload'); S.appsLoaded = {}; S.status = {}; await loadConfig(); pollAll(); fetchLog(); },
  key: (b) => sendKey(b, b.dataset.key),
  rawkey: (b) => sendNative(b, b.dataset.native),
  rawkeybox: (b) => { const v = $('#rawKey').value.trim(); if (v) sendNative(b, v); },
  power: async (b) => { await run(b, api('POST', devPath('/power'), {state: b.dataset.state})); pollStatus(dev()); },
  screensaver: async (b) => { await run(b, api('POST', devPath('/screensaver'), {})); pollStatus(dev()); },
  probe: (b) => run(b, api('POST', devPath('/probe'), {})),
  diagnose: (b) => run(b, api('POST', devPath('/diagnose'), {})),
  wol: async (b) => { await run(b, api('POST', devPath('/wol'), {})); setTimeout(() => pollStatus(dev()), 4000); },
  detectMac: async (b) => { await run(b, api('POST', devPath('/detect-mac'), {})); pollStatus(dev()); },
  pairStart: (b) => run(b, api('POST', devPath('/pair/start'), {})),
  pairFinish: async (b) => { const r = await run(b, api('POST', devPath('/pair/finish'), {code: $('#pairCode').value})); if (r.ok) { $('#pairCode').value = ''; pollStatus(dev()); } },
  text: (b) => run(b, api('POST', devPath('/text'), {text: $('#txt').value})),
  shot: () => { const i = $('#shot'); i.hidden = false; i.onerror = () => { i.hidden = true; fetchLog(); }; i.onload = fetchLog; i.src = devPath('/screenshot') + '?t=' + Date.now(); },
  refreshApps: () => loadApps(true),
  launch: async (b) => { await run(b, api('POST', devPath('/launch'), {app: b.dataset.app})); setTimeout(() => pollStatus(dev()), 1500); },
  launchCustom: (b) => run(b, api('POST', devPath('/launch'), {app: $('#lpApp').value.trim(), params: {contentId: $('#lpContent').value.trim(), mediaType: $('#lpType').value.trim()}})),
  prepare: async (b) => {
    if (!S.devId) return;
    $('#prepState').textContent = 'preparing… (see log)';
    const r = await run(b, api('POST', devPath('/prepare'), {}));
    $('#prepState').textContent = r.ok ? 'ready' : lastLine(r.body);
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
  watch: () => openWatch(null),
  watchClose: () => $('#watch').close(),
  wsearch: () => watchSearch(),
  watchItem: (b) => openWatch(S.items[b.dataset.id]),
  watchBack: () => showWatchStep('wSearch'),
  watchAgain: () => chooseTarget(W.item),
  watchOn: (b) => watchOn(b.dataset.dev),
  watchPhone: () => watchPhone(),
};
document.addEventListener('click', e => {
  const b = e.target.closest('[data-act]');
  if (b && !b.disabled && ACTIONS[b.dataset.act]) ACTIONS[b.dataset.act](b);
});

/* ------------------------------------------------------------------ log */
async function fetchLog() {
  if ($('#fPause').checked) return;
  const r = await api('GET', '/api/log?since=' + S.logLast);
  if (r.build) { if (!S.build) S.build = r.build; else if (S.build !== r.build) return location.reload(); }   // server was updated
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
    const inline = e.status == null ? `<span class="${e.ok === false ? 'st badc' : ''}">${esc((e.response || '').split('\n')[0].slice(0, 160))}</span>` : '';
    return `<details ${e.ok === false ? 'open' : ''}><summary><span class="dim">${esc(e.ts)}</span>
      <span class="tag ${esc(e.kind)}">${esc(e.kind)}</span><span class="dim">${esc(devName(e.device))}</span>
      <b>${esc(e.action)}</b>${st}${inline}<span class="dim">${e.ms == null ? '' : e.ms + ' ms'}</span></summary>
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
$('#wq').onkeydown = e => { if (e.key === 'Enter') watchSearch(); };
['fDevice', 'fKind', 'fErr', 'fText'].forEach(id => $('#' + id).addEventListener('input', renderLog));
$('#fPause').addEventListener('change', fetchLog);

(async () => {
  await loadConfig();
  pollAll(); pollSessions(); fetchLog();
  setInterval(pollAll, 5000);
  setInterval(pollSessions, 3000);
  setInterval(fetchLog, 1000);
})();
