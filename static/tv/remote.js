'use strict';
/* The Remote page: one device at a time, laid out like a physical remote. */
S.keyboard = localStorage.getItem('kbd') !== '0';   // arrows / Enter / Esc / Space drive the remote on a computer
const KEYBOARD = {ArrowUp:'up',ArrowDown:'down',ArrowLeft:'left',ArrowRight:'right',Enter:'ok',Escape:'back',Backspace:'back',Home:'home',' ':'play_pause',m:'mute','+':'vol_up','=':'vol_up','-':'vol_down'};
const KEYS = {home:'Home',back:'Back',options:'✱',play_pause:'⏯',rewind:'⏪',ff:'⏩',vol_up:'Vol +',vol_down:'Vol −',mute:'Mute'};

function renderRemote() {
  const d = dev(), el = $('#remote');
  if (!d) { el.innerHTML = '<div class="dim">No TV added yet. Open <a href="/tv/devices">TVs</a> to find yours on the network and add it.</div>'; return; }
  const k = (key, label, cls = '') => `<button data-act="key" data-key="${key}" class="${cls}" ${d.keys[key] ? '' : 'disabled'} title="${key}">${label || KEYS[key]}</button>`;
  const volume = ['vol_down', 'mute', 'vol_up'].some(x => d.keys[x]);
  // Volume buttons are press-and-hold buttons (see "volume" below), so they use data-hold rather than data-act.
  const vol = (key, label) => `<button data-hold="${key}" class="vol" ${d.keys[key] ? '' : 'disabled'} title="${key}: tap, tap repeatedly, or hold">${label}</button>`;
  // On a phone the two groups stack; on a tablet they sit side by side (info and apps left, the pad right)
  el.innerHTML = `
    <div class="r-top"><h3>${esc(d.name)}</h3><div id="rstat" class="small"></div></div>
    <div id="pairNote" class="pair" hidden>Not paired with this TV yet. Open <a href="/tv/devices">TVs</a> and use “Start pairing”.</div>
    <div class="r-info">
      <div id="now" class="np"></div>
      ${has('power_on') ? `<div class="rgrid r2"><button data-act="power" data-state="on" class="pwr">⏻ Power on</button><button data-act="power" data-state="off" class="pwr">☾ Sleep</button></div>` : ''}
      ${has('launch') ? `<button data-act="openjf" class="jfopen">▶ Open Jellyfin</button>` : ''}
      ${has('stream_view') && CAM.enabled ? `<button data-act="cameras" class="jfopen">📹 Cameras</button>` : ''}
      <div id="quick" class="quick"></div>
    </div>
    <div class="r-pad">
      <div class="rgrid r3">${k('back')}${k('home')}${k('options')}</div>
      <div class="dpad">
        <span></span>${k('up', '▲')}<span></span>
        ${k('left', '◀')}${k('ok', 'OK', 'ok')}${k('right', '▶')}
        <span></span>${k('down', '▼')}<span></span>
      </div>
      <div class="rgrid r3">${k('rewind')}${k('play_pause')}${k('ff')}</div>
      ${volume ? `<div class="rgrid r3">${vol('vol_down', 'Vol −')}${k('mute')}${vol('vol_up', 'Vol +')}</div>` : ''}
    </div>
    <button data-act="watch" class="primary watchbtn">🎬 Watch a movie…</button>
    <div id="rapps" class="r-apps"></div>`;
  renderStatus(); renderNow(); renderQuick(); renderApps();
}

function renderStatus() {
  const st = S.status[S.devId], el = $('#rstat');
  if (!el) return;
  el.innerHTML = !st ? '<span class="dim">checking…</span>' : st.online
    ? `<span class="chip">${esc(POWER_LABEL[st.power] || st.power)}</span>` + (st.volume ? `<span class="chip">vol ${esc(st.volume.level)}${st.volume.muted ? ' muted' : ''}</span>` : '')
    : st.unresponsive ? '<span class="chip" style="color:var(--warn)">asleep?</span>'
    : '<span class="chip" style="color:var(--bad)">offline</span>';
  const note = $('#pairNote'); if (note) note.hidden = !(st && st.needsPairing);
}

const REC = {msg: '', ok: true};      // the answer to the last Record press, kept so a redraw of Now playing does not wipe it
// Now playing is part of the remote: what the TV is showing, with Jellyfin's own controls when it is playing a Jellyfin item.
function renderNow() {
  const d = dev(), el = $('#now'); if (!d || !el) return;
  const {st, s, offline, app, n, p, m} = nowParts(d, sessionFor(d.id));
  const head = app ? `<div class="np-head">${iconHtml(d, app)}<div class="np-who"><div class="np-app">${esc(app.name)}</div></div></div>` : '';
  let body = '';
  if (!st) body = '<div class="np-sub">checking…</div>';
  else if (offline) body = `<div class="np-sub">${st.unresponsive ? 'The TV is asleep and not answering.' : 'The TV is not reachable.'} Power on will try to wake it.</div>`;
  else if (n) {
    const len = (n.RunTimeTicks || 0) / TICKS, key = clockKey(d.id, n.Id);
    const now = Clock.sync(key, (p.EstimatedTicks ?? p.PositionTicks ?? 0) / TICKS, !p.IsPaused, len);   // runs smoothly between updates
    body = `<div class="np-title">${esc(itemLabel(n))}</div>
      <div class="np-sub">${now.playing ? '▶ playing' : '⏸ paused'} · ${clockText(key, len)}</div>
      ${clockBar(key, len)}
      ${n.Type === 'TvChannel' ? `<div class="np-ctl"><button data-act="record" data-channel="${esc(n.Id)}" title="Record what is on now. If it started a while ago, only the rest is recorded.">⏺ Record this airing</button>
        <button data-act="record" data-series="1" data-channel="${esc(n.Id)}" title="Record every new episode of this series">⏺ Record series</button></div><div class="np-sub" style="${REC.ok ? '' : 'color: var(--bad, #e66);'}">${esc(REC.msg)}</div>` : ''}
      ${s.SupportsRemoteControl ? `<div class="np-ctl"><button data-act="relseek" data-d="-30" title="Back 30 seconds">−30s</button>
        <button data-act="playstate" data-cmd="PlayPause">${now.playing ? '⏸ Pause' : '▶ Resume'}</button>
        <button data-act="relseek" data-d="30" title="Forward 30 seconds">+30s</button>
        <button data-act="playstate" data-cmd="Stop" title="Stop">⏹</button></div>` : ''}`;
  } else if (m) {
    const len = (m.durationMs || 0) / 1000, key = clockKey(d.id, 'media'), now = Clock.sync(key, (m.positionMs || 0) / 1000, m.state === 'playing', len);
    body = `<div class="np-title">${esc(m.title || (app ? app.name : 'Playing'))}</div>
      <div class="np-sub">${m.state === 'paused' ? '⏸ paused' : m.state === 'playing' ? '▶ playing' : esc(m.state)}${m.positionMs != null ? ` · ${clockText(key, len)}` : ''}</div>
      ${len ? clockBar(key, len) : ''}`;
  } else body = '<div class="np-sub">Nothing playing</div>';
  const html = head + body;
  if (el.dataset.sig !== html) { el.dataset.sig = html; el.innerHTML = html; }   // skip no-op redraws so a click is never lost mid-redraw
  tickClocks();
}

function renderQuick() {
  const d = dev(), el = $('#quick'); if (!d || !el) return;
  const st = S.status[d.id] || {}, list = quickApps(d);
  let html;
  if (list.length) html = list.map(a => `<button data-act="launch" data-app="${esc(a.id)}" class="tile ${a.id === st.app ? 'active' : ''}" title="${esc(a.name)}">${iconHtml(d, a)}<span class="n">${esc(a.name)}</span></button>`).join('');
  else if (S.appsLoading[d.id]) html = '<div class="dim small quick-note">Loading apps…</div>';
  else html = `<div class="dim small quick-note">${S.appsError[d.id] ? esc(S.appsError[d.id]) : 'No quick-launch apps yet.'} Pick some on <a href="/tv/settings">Remote Apps</a>.</div>`;
  if (el.dataset.sig !== html) { el.dataset.sig = html; el.innerHTML = html; }
}

// The apps chosen on the Remote Apps page, below the quick-launch row (anything already in that row is not repeated)
function renderApps() {
  const d = dev(), el = $('#rapps'); if (!d || !el) return;
  const st = S.status[d.id] || {}, quick = new Set(quickApps(d).map(a => a.id));
  const list = visibleApps(d).filter(a => !quick.has(a.id));
  const html = list.length ? `<div class="r-apps-h small dim">Apps</div><div class="agrid">${list.map(a =>
    `<button data-act="launch" data-app="${esc(a.id)}" class="tile ${a.id === st.app ? 'active' : ''}" title="${esc(a.name)}">${iconHtml(d, a)}<span class="n">${esc(a.name)}</span></button>`).join('')}</div>` : '';
  if (el.dataset.sig !== html) { el.dataset.sig = html; el.innerHTML = html; }
}

// A link from Watch now (/tv/remote?tv=<tv>&play=<item>&title=<name>) opens this TV's remote and starts the title with the usual progress box.
function startFromLink() {
  const q = new URLSearchParams(location.search), play = q.get('play');
  if (!play) return;
  history.replaceState(null, '', location.pathname);   // so a refresh does not start it again
  const d = S.devices.find(x => x.id === q.get('tv'));
  if (!d) return alert('That TV is not in SyncHub any more.');
  W.item = {Id: play, Name: q.get('title') || 'this title', Type: 'Movie'};
  W.startOver = q.get('start') === '0';
  $('#watch').showModal();
  watchOn(d.id);
}

/* ------------------------------------------------------------------ cameras (UniFi Protect)
   Cameras pauses what the TV is playing and shows a camera full screen; this box then switches camera or closes it and carries on. */
const CAM = {enabled: false, cameras: [], error: '', relay: true, state: null, busy: false, steps: ''};
$('.tvr').insertAdjacentHTML('beforeend', `<dialog id="cam">
  <div class="dlg-head"><b>Cameras</b><button data-act="camHide" class="small">✕</button></div>
  <div id="camNow" class="small"></div>
  <div id="camList"></div>
  <div id="camMsg" class="small dim"></div>
  <div class="tv-row"><button data-act="camClose" id="camCloseBtn" class="primary" hidden>✕ Close camera and carry on</button></div>
</dialog>`);

async function loadCameras() {
  const r = await api('GET', '/api/tv/cameras');
  Object.assign(CAM, {enabled: !!r.enabled, cameras: r.cameras || [], error: r.error || '', relay: r.relay !== false});
  if (dev()) renderRemote();
}
async function loadCamState() {
  const r = await api('GET', devPath('/camera'));
  CAM.state = r.state || null; renderCam();
}
function renderCam() {
  const st = CAM.state, d = dev();
  $('#camNow').innerHTML = st ? `Showing <b>${esc(st.title)}</b> on ${esc(d.name)}.` + (st.resume ? ` Closing carries on with <b>${esc(st.resume.title)}</b>${st.resume.wasPlaying ? '' : ' (it was paused)'} at ${fmt(st.resume.ticks / TICKS)}.` : ' Nothing was playing, so there is nothing to carry on with.')
    : `Pick a camera to show full screen on ${esc(d.name)}. What is playing in Jellyfin is paused and remembered.`;
  $('#camList').innerHTML = CAM.error ? `<div class="pair">${esc(CAM.error)}</div>`
    : !CAM.cameras.length ? '<div class="dim small">No cameras found. Check the Protect settings.</div>'
    : (CAM.relay ? '' : '<div class="pair">SyncHub does not know where the camera relay is yet. Choose the relay worker in Settings and let it poll once, or type its address.</div>')
      + CAM.cameras.map(c => `<button class="target" data-act="camShow" data-cam="${esc(c.name)}" ${CAM.busy ? 'disabled' : ''}><span class="dot ${st && st.camera === c.name ? 'on' : ''}"></span><div><div class="t-name">${esc(c.title)}</div></div></button>`).join('');
  $('#camCloseBtn').hidden = !st;
  $('#camCloseBtn').disabled = CAM.busy;
  $('#camMsg').textContent = CAM.busy ? 'Working…' : CAM.steps;
}
async function camCall(btn, path, body) {
  CAM.busy = true; renderCam();
  const r = await run(btn, api('POST', devPath(path), body));
  CAM.busy = false; CAM.steps = r.ok ? '' : lastLine(r.body);
  if (r.state !== undefined) CAM.state = r.state;
  renderCam(); setTimeout(() => pollStatus(dev()), 1500);
  return r;
}
Object.assign(ACTIONS, {
  record: async b => {
    const series = !!b.dataset.series;
    if (series && !confirm('Record every new episode of this series? It keeps recording until you cancel the rule in Jellyfin.')) return;
    const res = await run(b, api('POST', devPath('/record'), {channelId: b.dataset.channel, series}));
    REC.msg = (res && res.body) || 'No answer from SyncHub.'; REC.ok = !!(res && res.ok);
    renderNow();
  },
  cameras: async () => {
    CAM.steps = ''; $('#cam').showModal();
    await loadCameras(); await loadCamState();
    if (!CAM.state && CAM.cameras.length === 1 && CAM.relay) camCall(null, '/camera', {camera: CAM.cameras[0].name});
  },
  camShow: b => camCall(b, '/camera', {camera: b.dataset.cam}),
  camClose: async b => { await camCall(b, '/camera/close'); if (!CAM.state) $('#cam').close(); },
  camHide: () => $('#cam').close(),
});

Object.assign(Page, {
  onStart: startFromLink,
  onSelect: renderRemote,
  onStatus: d => { if (d.id === S.devId) { renderStatus(); renderNow(); renderQuick(); renderApps(); } },
  onSessions: renderNow,
  onApps: () => { renderQuick(); renderApps(); },
  onPrefs: () => { renderQuick(); renderApps(); },
});

/* ------------------------------------------------------------------ volume
   Tap = one step. Tap repeatedly = one step per tap. Hold = a steady repeat that speeds up the longer you hold.
   Requests never overlap: steps that pile up while one is in flight go out together as a single "x N" press, so a slow
   device (ADB takes about 1.5 s a call) still ends up at the right volume instead of queueing dozens of calls. */
const HOLD_DELAY = 380, HOLD_EVERY = 120;
const pumps = {};
function pushKey(key, n = 1) {
  const p = pumps[key] ||= {pending: 0, busy: false};
  p.pending = Math.min(60, p.pending + n);
  if (!p.busy) drainKey(key);
}
async function drainKey(key) {
  const p = pumps[key]; p.busy = true;
  while (p.pending) {
    const count = Math.min(p.pending, 30); p.pending -= count;
    const r = await api('POST', devPath('/key'), {key, count});
    if (!r.ok) { p.pending = 0; break; }
  }
  p.busy = false; fetchLog();
  clearTimeout(p.refresh); p.refresh = setTimeout(() => pollStatus(dev()), 700);   // refresh the volume chip once things settle
}
let hold = null;
function startHold(btn, key) {
  stopHold();
  btn.classList.add('holding');
  pushKey(key, 1);                                  // the tap itself, sent straight away
  const t0 = Date.now();
  hold = {btn, timer: setTimeout(() => {
    hold.timer = setInterval(() => {                // still down: keep stepping, faster after 1.5 s and 3 s
      const held = (Date.now() - t0 - HOLD_DELAY) / 1000;
      pushKey(key, held > 3 ? 3 : held > 1.5 ? 2 : 1);
    }, HOLD_EVERY);
  }, HOLD_DELAY)};
}
function stopHold() {
  if (!hold) return;
  clearTimeout(hold.timer); clearInterval(hold.timer);
  hold.btn.classList.remove('holding'); hold = null;
}
document.addEventListener('pointerdown', e => {
  const b = e.target.closest('[data-hold]');
  if (!b || b.disabled || e.button > 0) return;
  e.preventDefault();                               // no text selection, focus ring or callout while holding
  try { b.setPointerCapture(e.pointerId); } catch (_) {}        // keep receiving the release even if the finger drifts
  startHold(b, b.dataset.hold);
});
['pointerup', 'pointercancel', 'lostpointercapture'].forEach(t => document.addEventListener(t, stopHold));
document.addEventListener('contextmenu', e => { if (e.target.closest('[data-hold]')) e.preventDefault(); });
document.addEventListener('click', e => {           // Enter/Space on a focused button arrives as a click with no pointer
  const b = e.target.closest('[data-hold]');
  if (b && e.detail === 0 && !b.disabled) pushKey(b.dataset.hold);
});

document.addEventListener('keydown', e => {
  if (!S.keyboard || !S.devId || /^(INPUT|TEXTAREA|SELECT)$/.test(e.target.tagName) || e.ctrlKey || e.metaKey || e.altKey || $('#watch').open) return;
  const key = KEYBOARD[e.key];
  if (!key) return;
  e.preventDefault();
  if (key === 'vol_up' || key === 'vol_down') return pushKey(key);   // the keyboard's own auto-repeat gives the hold
  sendKey(document.querySelector(`#remote [data-key="${key}"]`), key);
});

loadCameras();
startApp();
