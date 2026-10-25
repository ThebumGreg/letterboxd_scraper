'use strict';
/* TV tools page: every device tool, the app list (with quick-launch pinning), Jellyfin testing and the log. */
const COMMANDS = ['MoveUp','MoveDown','MoveLeft','MoveRight','PageUp','PageDown','PreviousLetter','NextLetter','ToggleOsd','ToggleContextMenu','Select','Back','TakeScreenshot','SendKey','SendString','GoHome','GoToSettings','VolumeUp','VolumeDown','Mute','Unmute','ToggleMute','SetVolume','SetAudioStreamIndex','SetSubtitleStreamIndex','ToggleFullscreen','DisplayContent','GoToSearch','DisplayMessage','SetRepeatMode','ChannelUp','ChannelDown','Guide','ToggleStats','PlayMediaSource','PlayTrailers','SetShuffleQueue','PlayState','PlayNext','ToggleOsdMenu','Play','SetMaxStreamingBitrate','SetPlaybackOrder'];
const TEMPLATES = {SetVolume:'{"Volume":"50"}',SetAudioStreamIndex:'{"Index":"1"}',SetSubtitleStreamIndex:'{"Index":"-1"}',SetRepeatMode:'{"RepeatMode":"RepeatNone"}',SetShuffleQueue:'{"ShuffleMode":"Shuffle"}',SetMaxStreamingBitrate:'{"Bitrate":"20000000"}',SendString:'{"String":"hello"}',SendKey:'{"Key":"Enter"}',DisplayContent:'{"ItemId":"","ItemType":"Movie","ItemName":""}',DisplayMessage:'{"Header":"Hi","Text":"Test","TimeoutMs":"3000"}',PlayMediaSource:'{"ItemId":"","MediaSourceId":""}'};
const TRANSPORT = ['Pause','Unpause','PlayPause','Stop','PreviousTrack','NextTrack','Rewind','FastForward'];
const ALL_KEYS = [['up','▲'],['down','▼'],['left','◀'],['right','▶'],['ok','OK'],['back','Back'],['home','Home'],['options','✱ Options'],['info','Info'],['search','Search'],
  ['play_pause','⏯'],['stop','⏹'],['rewind','⏪'],['ff','⏩'],['prev','⏮'],['next','⏭'],['replay','↺ Replay'],['vol_up','Vol +'],['vol_down','Vol −'],['mute','Mute'],
  ['ch_up','Ch +'],['ch_down','Ch −'],['backspace','⌫'],['enter','Enter']];

/* ------------------------------------------------------------------ device tools */
function renderTools() {
  const d = dev(), el = $('#tools');
  $('#viaLabel').hidden = !(has('url_play') || has('deeplink'));   // only devices with another route offer the choice
  if (!d) { el.innerHTML = '<div class="dim">No device selected</div>'; return; }
  const pairing = has('pairing') ? `<div class="pair" id="pairBox" hidden><b>Pairing required</b>
      <div class="small dim">Press Start, then type the code that appears on the TV.</div>
      <div class="tv-row"><button data-act="pairStart">Start pairing</button><input id="pairCode" placeholder="code on TV" maxlength="8" style="max-width:120px"><button data-act="pairFinish">Finish</button></div></div>` : '';
  const sleepLabel = d.sleepMode === 'screensaver' ? 'Sleep (screensaver)' : 'Sleep (standby)';
  const power = has('power_on') ? `<div class="rgrid r2"><button data-act="power" data-state="on" class="pwr">⏻ Wake / power on</button><button data-act="power" data-state="off" class="pwr" title="This device's sleep mode is ${esc(d.sleepMode)} (set &quot;sleep&quot; in devices.json)">⏻ ${sleepLabel}</button></div>
    <div class="rgrid r2">${has('screensaver') ? '<button data-act="screensaver" title="Soft sleep: the TV stays reachable and any key wakes it">🖼 Screensaver</button>' : ''}<button data-act="wol" title="Send a Wake-on-LAN magic packet">⚡ Wake-on-LAN</button><button data-act="detectMac" title="Read the device's MAC address (it must be on)">Detect MAC</button><button data-act="probe" title="Which network ports answer right now? Run it on and in standby">Probe network</button>${has('diagnose') ? '<button data-act="diagnose" title="Read power, screensaver and network settings from the TV">Diagnose</button>' : ''}</div>
    <div class="small dim" id="macLine" style="text-align:center"></div>` : '';
  const more = d.extraKeys.length ? `<details class="adv"><summary>More keys (${esc(d.type)})</summary><div class="rgrid r2">${d.extraKeys.map(x => `<button data-act="rawkey" data-native="${esc(x.key)}" title="${esc(x.key)}">${esc(x.label)}</button>`).join('')}</div></details>` : '';
  el.innerHTML = `<h2>Device tools</h2>
    <h3 style="margin:0">${esc(d.name)}</h3>
    <div class="rstat small dim" id="rstat"></div>
    ${pairing}${power}
    <h2 style="margin-top:10px">All keys</h2>
    <div class="rgrid r4">${ALL_KEYS.map(([k, label]) => `<button data-act="key" data-key="${k}" ${d.keys[k] ? '' : 'disabled'} title="${k}">${label}</button>`).join('')}</div>
    ${has('text') ? '<div class="tv-row"><input id="txt" placeholder="Type on the TV"><button data-act="text">Send</button></div>' : ''}
    <div class="tv-row"><input id="rawKey" placeholder="raw ${d.type === 'roku' ? 'ECP key (e.g. InputHDMI2)' : 'keycode (e.g. KEYCODE_GUIDE)'}"><button data-act="rawkeybox">Send</button>${has('screenshot') ? '<button data-act="shot">Screenshot</button>' : ''}</div>
    <img id="shot" alt="" hidden style="max-width:100%;margin-top:6px;border-radius:8px">
    ${more}
    <div class="small dim" style="margin-top:8px"><label><input type="checkbox" id="kbd" ${S.keyboard ? 'checked' : ''}> keyboard control on the Remote page (arrows, Enter, Esc, Space, m, +/−)</label></div>
    <div class="small dim" style="margin-top:6px">${esc(d.typeLabel)} · ${esc(d.host)} · wake: ${esc(d.wakeMethods.join(', '))}</div>`;
  $('#kbd').onchange = e => { S.keyboard = e.target.checked; localStorage.setItem('kbd', S.keyboard ? '1' : '0'); };
  renderToolsStatus();
}
function renderToolsStatus() {
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

/* ------------------------------------------------------------------ apps + quick launch */
function renderApps() {
  const d = dev(), el = $('#apps'); if (!d || !el) return;
  if (S.appsLoading[d.id]) { el.textContent = 'Loading apps…'; renderQuickStrip(); return; }
  if (S.appsError[d.id]) { el.innerHTML = `<span style="color:var(--bad)">${esc(S.appsError[d.id])}</span>`; renderQuickStrip(); return; }
  const list = S.apps[d.id] || [], st = S.status[d.id] || {}, pinned = quickApps(d).map(a => a.id);
  const tile = a => `<div class="tilebox"><button data-act="launch" data-app="${esc(a.id)}" class="tile ${a.id === st.app ? 'active' : ''}" title="${esc(a.id)}">${iconHtml(d, a)}<span class="n">${esc(a.name)}</span></button>
      <button class="pin ${pinned.includes(a.id) ? 'on' : ''}" data-act="pin" data-app="${esc(a.id)}" data-name="${esc(a.name)}" title="${pinned.includes(a.id) ? 'Remove from' : 'Add to'} quick launch">${pinned.includes(a.id) ? '★' : '☆'}</button></div>`;
  const system = list.filter(a => a.system), inputs = list.filter(a => a.type === 'tvin');
  const apps = list.filter(a => a.type !== 'tvin' && (S.showSystem || !a.system));
  el.innerHTML = (apps.map(tile).join('') +
    (inputs.length ? '<div class="grp small dim">TV inputs</div>' + inputs.map(tile).join('') : '') +
    (system.length ? `<div class="grp small dim"><label><input type="checkbox" id="showSys" ${S.showSystem ? 'checked' : ''}> show ${system.length} system app(s)</label></div>` : '')) || '<span class="dim">No apps found</span>';
  renderQuickStrip();
}
document.addEventListener('change', e => { if (e.target.id === 'showSys') { S.showSystem = e.target.checked; renderApps(); } });

/* ------------------------------------------------------------------ playback test + sessions */
function renderSessions() {
  const sel = $('#sessSel'); if (!sel) return;
  const sig = S.sessions.map(s => s.Id + s.Client + s.DeviceName + s.deviceId).join('|') + '#' + S.sessSel + '#' + S.devId;
  if (sig !== S.sig) {
    S.sig = sig;
    const mine = sessionFor(S.devId);
    sel.innerHTML = `<option value="auto">Auto: ${mine ? esc(mine.Client + ' / ' + mine.DeviceName) : 'none matched for ' + esc(dev()?.name || 'device')}</option>` +
      S.sessions.map(s => `<option value="${esc(s.Id)}" title="DeviceId ${esc(s.DeviceId)}">${esc(s.Client)} - ${esc(s.DeviceName)} (${esc(s.UserName)})</option>`).join('');
    sel.value = S.sessSel;
  }
  renderNowDetail(); renderCaps(); renderSessionTable();
}
const ruleText = r => Object.entries(r || {}).map(([k, v]) => `${k.replace('_', ' ')} “${v}”`).join(' and ') || 'nothing';
function renderSessionTable() {
  const d = dev(), el = $('#sessTable'), line = $('#ruleLine'); if (!el || !d) return;
  line.innerHTML = `<b>${esc(d.name)}</b> looks for a session with ${esc(ruleText(d.sessionRule))} ` +
    (d.sessionOverride ? '(your choice) <button class="small" data-act="sessReset">reset to devices.json</button>' : '(from devices.json, or the default for this kind of device)');
  const how = d.playVia === 'auto' && has('deeplink')
    ? 'a deep link into the Jellyfin app (the official Roku app cannot take session commands), streaming a URL only if the link request fails'
    : d.playVia === 'deeplink' ? 'a deep link into the Jellyfin app' : {
    url: d.playLearned ? 'streaming a URL <span class="dim">(learned: this device\'s Jellyfin app cannot be remote controlled)</span> <button class="small" data-act="playReset">try the Jellyfin app again</button>'
      : 'streaming a URL <span class="dim">(devices.json)</span>',
    auto: 'the Jellyfin app first, streaming a URL if that app cannot be remote controlled',
    session: 'the Jellyfin app\'s session',
  }[d.playVia];
  $('#playLine').innerHTML = (has('url_play') || has('deeplink')) ? `<b>${esc(d.name)}</b> plays through ${how}.` : '';
  el.innerHTML = S.sessions.length ? S.sessions.map(s => `<div class="item"><span class="name"><b>${esc(s.Client)}</b>${s.Version ? ' ' + esc(s.Version) : ''} · ${esc(s.DeviceName)}
      <span class="dim small">· id ${esc(String(s.DeviceId).slice(0, 8))}… · ${esc(s.UserName)} · remote control: ${s.SupportsRemoteControl ? 'yes' : '<span style="color:var(--bad)">no</span>'}</span>
      ${s.deviceId ? `<span class="chip">→ ${esc(devName(s.deviceId))}</span>` : ''}</span>
      <button data-act="sessUse" data-id="${esc(s.Id)}">Use for ${esc(d.name)}</button></div>`).join('')
    : '<span class="dim">Jellyfin lists no sessions right now. Open the Jellyfin app on the TV.</span>';
}
function renderCaps() {
  const s = activeSession(), sup = s ? s.SupportedCommands : [];
  $('#caps').innerHTML = s ? `<span class="chip">RemoteControl: ${s.SupportsRemoteControl}</span>` + sup.map(c => `<span class="chip">${esc(c)}</span>`).join('') : '<span class="dim small">No session</span>';
  document.querySelectorAll('#cmds button').forEach(b => b.classList.toggle('unsup', !sup.includes(b.dataset.cmd)));
  document.querySelectorAll('#transport button').forEach(b => b.classList.toggle('unsup', !sup.includes('PlayState')));
}
function renderNowDetail() {
  const d = dev(), el = $('#now'); if (!d) { el.textContent = 'Select a device'; return; }
  const {st, s, offline, app, n, p, m} = nowParts(d, activeSession());
  const head = app ? `<div class="np-head">${iconHtml(d, app)}<div><div class="np-app">${esc(app.name)}</div>
      <div><span class="chip">${esc(POWER_LABEL[st.power] || st.power)}</span>${st.volume ? `<span class="chip">vol ${esc(st.volume.level)}/${esc(st.volume.max)}${st.volume.muted ? ' muted' : ''}</span>` : ''}</div></div></div>`
    : `<div class="np-head"><div><div class="np-app">${esc(d.name)}</div><div class="np-sub">${st ? (st.online ? '' : 'offline — ' + esc(st.error || 'not reachable')) : 'checking…'}</div></div></div>`;
  let body;
  if (n) {
    const len = (n.RunTimeTicks || 0) / TICKS, key = clockKey(d.id, n.Id);
    const now = Clock.sync(key, (p.EstimatedTicks ?? p.PositionTicks ?? 0) / TICKS, !p.IsPaused, len);
    body = `<div class="np-title">${esc(itemLabel(n))}</div>
      <div class="np-sub">${[now.playing ? '▶ playing' : '⏸ paused', `via Jellyfin (${s.Client})`,
        p.VolumeLevel != null && `volume ${p.VolumeLevel}${p.IsMuted ? ' (muted)' : ''}`,
        p.AudioStreamIndex != null && `audio ${p.AudioStreamIndex}`, p.SubtitleStreamIndex != null && `subs ${p.SubtitleStreamIndex}`,
        p.PlayMethod].filter(Boolean).map(esc).join(' · ')}</div>
      ${clockBar(key, len)}
      <div class="small dim">${clockText(key, len)}</div>`;
  } else if (m) {
    const len = (m.durationMs || 0) / 1000, key = clockKey(d.id, 'media');
    Clock.sync(key, (m.positionMs || 0) / 1000, m.state === 'playing', len);
    body = `<div class="np-title">${esc(m.title || (app ? app.name : 'Playing'))}</div>
      <div class="np-sub">${m.state === 'paused' ? '⏸ paused' : m.state === 'playing' ? '▶ playing' : esc(m.state)} · ${esc(m.source)}${m.title ? '' : ' (this app does not report a title)'}</div>
      ${len ? clockBar(key, len) : ''}${m.positionMs != null ? `<div class="small dim">${clockText(key, len)}</div>` : ''}`;
  } else {
    body = `<div class="np-sub">${st && st.online ? (s ? `Jellyfin session ready (${esc(s.Client)} / ${esc(s.DeviceName)}): nothing playing` : 'Nothing playing, and no Jellyfin session for this device') : ''}</div>`;
  }
  el.className = '';
  const html = head + body;
  if (el.dataset.sig !== html) { el.dataset.sig = html; el.innerHTML = html; }
  tickClocks();
}

/* ------------------------------------------------------------------ library / play */
const splitIds = () => $('#ids').value.split(',').map(x => x.trim()).filter(Boolean);
async function playIds(btn, itemIds, playCommand, startIndex) {
  if (!itemIds.length) return;
  const pos = $('#optPos').value;
  const body = {itemIds, playCommand, startIndex,
    startPositionTicks: pos === '' ? undefined : Math.round(pos * TICKS),
    audioStreamIndex: $('#optAud').value, subtitleStreamIndex: $('#optSub').value, via: $('#via').value || undefined};
  if (S.devId && S.sessSel === 'auto') {
    $('#prepState').textContent = $('#prep').checked ? 'preparing…' : '';
    const r = await run(btn, api('POST', devPath('/play'), {...body, prepare: $('#prep').checked}));
    $('#prepState').textContent = !r.ok ? lastLine(r.body) : r.confirmed === false ? 'accepted, but not playing' : r.confirmed ? 'playing' : 'sent';
    if (r.learned) refreshDevices();
    pollSessions(); pollStatus(dev());
    return r;
  }
  return jfAct(btn, '/api/tv/setup/jf/play', body);   // an explicit session was chosen
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
  const r = await api('GET', '/api/tv/jf/search?q=' + encodeURIComponent($('#q').value));
  fetchLog();
  if (!Array.isArray(r)) { $('#results').textContent = r.body || 'Search failed'; return; }
  $('#results').innerHTML = r.map(i => `<div>${itemRow(i, i.Type === 'Series' ? `<button data-act="episodes" data-id="${esc(i.Id)}">Episodes</button>` : '')}<div class="eps"></div></div>`).join('') || '<span class="dim">No results</span>';
}
async function episodes(btn) {
  const box = btn.closest('div.item').nextElementSibling;
  const r = await api('GET', '/api/tv/jf/episodes/' + encodeURIComponent(btn.dataset.id));
  fetchLog();
  if (!Array.isArray(r)) return;
  const ids = r.map(e => e.Id);
  box.innerHTML = r.map((e, n) => itemRow(e, `<button data-act="playids" data-ids='${esc(JSON.stringify(ids))}' data-cmd="PlayNow" data-index="${n}">Play from here</button>`)).join('');
}

/* ------------------------------------------------------------------ log */
const devName = id => S.devices.find(d => d.id === id)?.name || id || '';
function logMatches(e) {
  if ($('#fDevice').value && e.device !== $('#fDevice').value) return false;
  if ($('#fKind').value && e.kind !== $('#fKind').value) return false;
  if ($('#fErr').checked && e.ok !== false) return false;
  const t = $('#fText').value.toLowerCase();
  return !t || `${e.action} ${e.request} ${e.response} ${devName(e.device)}`.toLowerCase().includes(t);
}
function renderLog() {
  const kinds = [...new Set(S.logAll.map(e => e.kind))].sort(), sel = $('#fKind');
  if (kinds.join() !== [...sel.options].slice(1).map(o => o.value).join()) {   // source filter builds itself from what was logged
    const keep = sel.value;
    sel.innerHTML = '<option value="">all sources</option>' + kinds.map(k => `<option>${esc(k)}</option>`).join('');
    sel.value = keep;
  }
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

/* ------------------------------------------------------------------ actions only this page has */
Object.assign(ACTIONS, {
  reload: async () => { await api('POST', '/api/tv/setup/reload'); S.appsLoaded = {}; S.status = {}; await loadConfig(); pollAll(); fetchLog(); },
  rawkey: b => sendNative(b, b.dataset.native),
  rawkeybox: b => { const v = $('#rawKey').value.trim(); if (v) sendNative(b, v); },
  screensaver: async b => { await run(b, api('POST', devPath('/screensaver'), {})); pollStatus(dev()); },
  probe: b => run(b, api('POST', devPath('/probe'), {})),
  diagnose: b => run(b, api('POST', devPath('/diagnose'), {})),
  wol: async b => { await run(b, api('POST', devPath('/wol'), {})); setTimeout(() => pollStatus(dev()), 4000); },
  detectMac: async b => { await run(b, api('POST', devPath('/detect-mac'), {})); pollStatus(dev()); },
  pairStart: b => run(b, api('POST', devPath('/pair/start'), {})),
  pairFinish: async b => { const r = await run(b, api('POST', devPath('/pair/finish'), {code: $('#pairCode').value})); if (r.ok) { $('#pairCode').value = ''; pollStatus(dev()); } },
  text: b => run(b, api('POST', devPath('/text'), {text: $('#txt').value})),
  shot: () => { const i = $('#shot'); i.hidden = false; i.onerror = () => { i.hidden = true; fetchLog(); }; i.onload = fetchLog; i.src = devPath('/screenshot') + '?t=' + Date.now(); },
  refreshApps: () => loadApps(true),
  launchCustom: b => run(b, api('POST', devPath('/launch'), {app: $('#lpApp').value.trim(), params: {contentId: $('#lpContent').value.trim(), mediaType: $('#lpType').value.trim()}})),
  sessUse: async b => {
    const s = S.sessions.find(x => x.Id === b.dataset.id); if (!s) return;
    const r = await run(b, api('PUT', `/api/tv/setup/prefs/session/${encodeURIComponent(S.devId)}`, {client: s.Client, device: s.DeviceName}));
    if (r.ok) { await refreshDevices(); pollSessions(); } else alert(r.body || 'Could not save');
  },
  playReset: async () => { await api('DELETE', `/api/tv/setup/prefs/play/${encodeURIComponent(S.devId)}`); await refreshDevices(); },
  sessReset: async () => { await api('DELETE', `/api/tv/setup/prefs/session/${encodeURIComponent(S.devId)}`); await refreshDevices(); pollSessions(); },
  prepare: async b => {
    if (!S.devId) return;
    $('#prepState').textContent = 'preparing… (see log)';
    const r = await run(b, api('POST', devPath('/prepare'), {}));
    $('#prepState').textContent = r.ok ? 'ready' : lastLine(r.body);
    pollSessions(); pollStatus(dev());
  },
  seek: b => {
    const v = $('#seekSec').value; if (v === '') return;
    const key = activeClockKey(); if (key) { Clock.seekTo(key, Number(v)); hook('onSessions'); tickClocks(); }
    jfAct(b, '/api/tv/jf/playstate', {command: 'Seek', seekPositionTicks: Math.round(v * TICKS)});
  },
  search: () => search(),
  episodes: b => episodes(b),
  playids: b => playIds(b, b.dataset.ids ? JSON.parse(b.dataset.ids) : splitIds(), b.dataset.cmd, b.dataset.index === undefined ? undefined : Number(b.dataset.index)),
  jfcmd: b => jfAct(b, '/api/tv/setup/jf/command', {command: b.dataset.cmd}),
  rawcmd: b => {
    let args; const t = $('#rawArgs').value.trim();
    try { args = t ? JSON.parse(t) : undefined; } catch (e) { return alert('Arguments are not valid JSON'); }
    jfAct(b, '/api/tv/setup/jf/command', {command: $('#rawCmd').value, arguments: args});
  },
  message: b => jfAct(b, '/api/tv/setup/jf/message', {header: $('#mHead').value, text: $('#mText').value, timeoutMs: $('#mTime').value}),
  view: b => jfAct(b, '/api/tv/setup/jf/viewing', {itemId: b.dataset.id, itemType: b.dataset.type}),
  viewform: b => { if ($('#vId').value) jfAct(b, '/api/tv/setup/jf/viewing', {itemId: $('#vId').value, itemType: $('#vType').value}); },
  logClear: async () => { await api('DELETE', '/api/tv/setup/log'); S.logAll = []; S.logLast = 0; renderLog(); },
});

Object.assign(Page, {
  onSelect: () => { renderTools(); renderSessions(); },
  onStatus: d => { if (d.id === S.devId) { renderToolsStatus(); renderNowDetail(); renderApps(); } },
  onSessions: renderSessions,
  onApps: renderApps,
  onPrefs: renderApps,
  onLog: renderLog,
});

/* ------------------------------------------------------------------ init */
S.sig = '';
$('#transport').innerHTML = TRANSPORT.map(c => `<button data-act="playstate" data-cmd="${c}">${c}</button>`).join('');
$('#cmds').innerHTML = COMMANDS.filter(c => !TEMPLATES[c]).map(c => `<button data-act="jfcmd" data-cmd="${c}">${c}</button>`).join('');
$('#rawCmd').innerHTML = COMMANDS.map(c => `<option>${c}</option>`).join('');
$('#rawCmd').onchange = () => { $('#rawArgs').value = TEMPLATES[$('#rawCmd').value] || ''; };
$('#rawCmd').onchange();
$('#sessSel').onchange = e => { S.sessSel = e.target.value; S.sig = ''; renderSessions(); };
$('#q').onkeydown = e => { if (e.key === 'Enter') search(); };
['fDevice', 'fKind', 'fErr', 'fText'].forEach(id => $('#' + id).addEventListener('input', renderLog));
$('#fPause').addEventListener('change', fetchLog);
startApp();
