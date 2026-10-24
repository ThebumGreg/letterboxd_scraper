'use strict';
/* The Remote page: one device at a time, laid out like a physical remote. */
S.keyboard = localStorage.getItem('kbd') !== '0';   // arrows / Enter / Esc / Space drive the remote on a computer
const KEYBOARD = {ArrowUp:'up',ArrowDown:'down',ArrowLeft:'left',ArrowRight:'right',Enter:'ok',Escape:'back',Backspace:'back',Home:'home',' ':'play_pause',m:'mute','+':'vol_up','=':'vol_up','-':'vol_down'};
const KEYS = {home:'Home',back:'Back',options:'✱',play_pause:'⏯',rewind:'⏪',ff:'⏩',vol_up:'Vol +',vol_down:'Vol −',mute:'Mute'};

function renderRemote() {
  const d = dev(), el = $('#remote');
  if (!d) { el.innerHTML = '<div class="dim">No device configured. Add one to devices.json.</div>'; return; }
  const k = (key, label, cls = '') => `<button data-act="key" data-key="${key}" class="${cls}" ${d.keys[key] ? '' : 'disabled'} title="${key}">${label || KEYS[key]}</button>`;
  const volume = ['vol_down', 'mute', 'vol_up'].some(x => d.keys[x]);
  // Volume buttons are press-and-hold buttons (see "volume" below), so they use data-hold rather than data-act.
  const vol = (key, label) => `<button data-hold="${key}" class="vol" ${d.keys[key] ? '' : 'disabled'} title="${key}: tap, tap repeatedly, or hold">${label}</button>`;
  // On a phone the two groups stack; on a tablet they sit side by side (info and apps left, the pad right)
  el.innerHTML = `
    <div class="r-top"><h3>${esc(d.name)}</h3><div id="rstat" class="small"></div></div>
    <div id="pairNote" class="pair" hidden>Not paired with this TV yet. <a href="/settings">Open Settings</a> and use “Start pairing”.</div>
    <div class="r-info">
      <div id="now" class="np"></div>
      ${has('power_on') ? `<div class="rgrid r2"><button data-act="power" data-state="on" class="pwr">⏻ Power on</button><button data-act="power" data-state="off" class="pwr">☾ Sleep</button></div>` : ''}
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
    <button data-act="watch" class="primary watchbtn">🎬 Watch a movie…</button>`;
  renderStatus(); renderNow(); renderQuick();
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

// Now playing is part of the remote: what the TV is showing, with Jellyfin's own controls when it is playing a Jellyfin item.
function renderNow() {
  const d = dev(), el = $('#now'); if (!d || !el) return;
  const {st, s, offline, app, n, p, m} = nowParts(d, sessionFor(d.id));
  const head = app ? `<div class="np-head">${iconHtml(d, app)}<div class="np-who"><div class="np-app">${esc(app.name)}</div></div></div>` : '';
  let body = '';
  if (!st) body = '<div class="np-sub">checking…</div>';
  else if (offline) body = `<div class="np-sub">${st.unresponsive ? 'The TV is asleep and not answering.' : 'The TV is not reachable.'} Power on will try to wake it.</div>`;
  else if (n) {
    const pos = (p.PositionTicks || 0) / TICKS, len = (n.RunTimeTicks || 0) / TICKS;
    body = `<div class="np-title">${esc(itemLabel(n))}</div>
      <div class="np-sub">${p.IsPaused ? '⏸ paused' : '▶ playing'} · ${fmt(pos)} / ${fmt(len)}</div>
      <div class="bar"><div style="width:${len ? Math.min(100, pos / len * 100) : 0}%"></div></div>
      ${s.SupportsRemoteControl ? `<div class="np-ctl"><button data-act="relseek" data-d="-30" title="Back 30 seconds">−30s</button>
        <button data-act="playstate" data-cmd="PlayPause">${p.IsPaused ? '▶ Resume' : '⏸ Pause'}</button>
        <button data-act="relseek" data-d="30" title="Forward 30 seconds">+30s</button>
        <button data-act="playstate" data-cmd="Stop" title="Stop">⏹</button></div>` : ''}`;
  } else if (m) {
    const pos = (m.positionMs || 0) / 1000, len = (m.durationMs || 0) / 1000;
    body = `<div class="np-title">${esc(m.title || (app ? app.name : 'Playing'))}</div>
      <div class="np-sub">${m.state === 'paused' ? '⏸ paused' : m.state === 'playing' ? '▶ playing' : esc(m.state)}${len ? ` · ${fmt(pos)} / ${fmt(len)}` : m.positionMs != null ? ` · at ${fmt(pos)}` : ''}</div>
      ${len ? `<div class="bar"><div style="width:${Math.min(100, pos / len * 100)}%"></div></div>` : ''}`;
  } else body = '<div class="np-sub">Nothing playing</div>';
  const html = head + body;
  if (el.dataset.sig !== html) { el.dataset.sig = html; el.innerHTML = html; }   // skip no-op redraws so a click is never lost mid-redraw
}

function renderQuick() {
  const d = dev(), el = $('#quick'); if (!d || !el) return;
  const st = S.status[d.id] || {}, list = quickApps(d);
  let html;
  if (list.length) html = list.map(a => `<button data-act="launch" data-app="${esc(a.id)}" class="tile ${a.id === st.app ? 'active' : ''}" title="${esc(a.name)}">${iconHtml(d, a)}<span class="n">${esc(a.name)}</span></button>`).join('');
  else if (S.appsLoading[d.id]) html = '<div class="dim small quick-note">Loading apps…</div>';
  else html = `<div class="dim small quick-note">${S.appsError[d.id] ? esc(S.appsError[d.id]) : 'No quick-launch apps yet.'} Pick some in <a href="/settings">Settings</a>.</div>`;
  if (el.dataset.sig !== html) { el.dataset.sig = html; el.innerHTML = html; }
}

Object.assign(Page, {
  onSelect: renderRemote,
  onStatus: d => { if (d.id === S.devId) { renderStatus(); renderNow(); renderQuick(); } },
  onSessions: renderNow,
  onApps: renderQuick,
  onPrefs: renderQuick,
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

startApp();
