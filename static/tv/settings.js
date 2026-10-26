'use strict';
/* Remote Apps page: which apps each TV's remote shows, quick launch, and a few per-browser preferences. */
S.filter = '';

function renderAppGrid() {
  const d = dev(), el = $('#appGrid'); if (!d || !el) return;
  $('#appsFor').innerHTML = `Remote for <b>${esc(d.name)}</b>`;
  if (S.appsLoading[d.id]) { el.textContent = 'Loading apps…'; return; }
  if (S.appsError[d.id]) { el.innerHTML = `<span style="color:var(--bad)">${esc(S.appsError[d.id])}</span>`; return; }
  const all = (S.apps[d.id] || []).filter(a => a.type !== 'tvin');
  const system = all.filter(a => a.system), shown = new Set(visibleApps(d).map(a => a.id)), pinned = new Set(quickApps(d).map(a => a.id));
  const q = S.filter.trim().toLowerCase();
  const list = all.filter(a => (S.showSystem || !a.system || shown.has(a.id)) && (!q || `${a.name} ${a.id}`.toLowerCase().includes(q)));
  $('#appsCount').textContent = `${visibleApps(d).length} ${visibleApps(d).length === 1 ? 'app' : 'apps'} shown on the remote` +
    (quickSaved(d) || pinned.size ? `, ${pinned.size} in quick launch` : '') + (S.prefs.apps[d.id] ? '' : ' (you have not chosen any yet)');
  el.innerHTML = list.map(a => `<div class="tilebox"><button class="tile pick ${shown.has(a.id) ? 'shown' : ''}" data-act="appToggle" data-app="${esc(a.id)}" data-name="${esc(a.name)}" aria-pressed="${shown.has(a.id)}" title="${esc(a.id)}">${iconHtml(d, a)}<span class="n">${esc(a.name)}</span></button>
      <button class="pin ${pinned.has(a.id) ? 'on' : ''}" data-act="pin" data-app="${esc(a.id)}" data-name="${esc(a.name)}" title="${pinned.has(a.id) ? 'Remove from' : 'Add to'} quick launch" aria-label="${pinned.has(a.id) ? 'Remove from' : 'Add to'} quick launch">${pinned.has(a.id) ? '★' : '☆'}</button></div>`).join('') +
    (system.length ? `<div class="grp small dim"><label><input type="checkbox" id="showSys" ${S.showSystem ? 'checked' : ''}> include ${system.length} system app(s)</label></div>` : '') ||
    '<span class="dim">No apps found</span>';
  $('#appsHint').textContent = d.type === 'googletv' && !d.capabilities.includes('screenshot')
    ? 'This TV is controlled without ADB, which cannot list installed apps, so only common apps are shown. Tick “Also use ADB” for it on the TVs page to see everything installed.' : '';
  renderQuickStrip();
}

/* How a title picks up where the viewer stopped on a TV that is only sent a deep link (a Roku): the choices and what each one costs. */
const RESUME_OPTIONS = [
  ['e', 'Save the place in Jellyfin, then open the title', 'The app may resume from Jellyfin\u2019s own record. SyncHub checks where it started and says so in the play steps, so you can tell whether it worked.'],
  ['a', 'Stream it from the place', 'Always starts at the right time, but Jellyfin does not record progress or watched state for that viewing.'],
  ['b', 'Open the title and say where to skip to', 'Starts from the beginning. The play steps give the time to skip to.'],
  ['c', 'Open the title, then skip ahead with the remote\u2019s keys (experimental)', 'Presses Right until it should be near the place, assuming one press skips 10 seconds (set resume_key_seconds on the TV to change that). Rough, and SyncHub reports where it landed.'],
];

function renderResume() {
  const d = dev(), body = $('#resumeBody'); if (!d || !body) return;
  $('#resumeFor').innerHTML = `Applies to <b>${esc(d.name)}</b>`;
  if (!d.resumeMode) {
    body.innerHTML = `<div class="small dim">${esc(d.name)} resumes through its Jellyfin session, which moves straight to where you stopped, so there is nothing to choose.</div>`;
    return;
  }
  body.innerHTML = RESUME_OPTIONS.map(([key, title, why]) => `<label class="resume-opt" style="display:block;margin:6px 0">
      <input type="radio" name="resumeMode" value="${key}" ${d.resumeMode === key ? 'checked' : ''}> <b>${esc(title)}</b>
      <div class="small dim" style="margin-left:22px">${esc(why)}</div></label>`).join('') + '<div class="small" id="resumeSaved" role="status"></div>';
}

function renderNews() {
  $('#news').innerHTML = S.notices.map(n => `<div class="newsitem"><b>${esc(n.title)}</b> <span class="dim small">${esc(n.date || '')}</span>
    <div class="small">${esc(n.body)}</div>${n.href && n.href !== location.pathname ? `<a href="${esc(n.href)}">${esc(n.action || 'Open')} →</a>` : ''}</div>`).join('') || '<span class="dim">Nothing yet</span>';
}

Object.assign(ACTIONS, {
  appToggle: b => {
    const d = dev(), cur = visibleApps(d), i = cur.findIndex(a => a.id === b.dataset.app);
    setVisible(d, i >= 0 ? cur.filter((_, n) => n !== i) : [...cur, {id: b.dataset.app, name: b.dataset.name}]);
  },
  showAll: () => { const d = dev(); setVisible(d, (S.apps[d.id] || []).filter(a => a.type !== 'tvin' && !a.system).map(a => ({id: a.id, name: a.name}))); },
  hideAll: () => setVisible(dev(), []),
  appsRefresh: () => loadApps(true),
});
Object.assign(Page, {
  onSelect: () => { renderAppGrid(); renderResume(); },
  onStatus: () => {},
  onApps: renderAppGrid,
  onPrefs: renderAppGrid,
  onNotices: renderNews,
});
document.addEventListener('change', async e => {
  if (e.target.name === 'resumeMode') {
    const d = dev(), out = $('#resumeSaved');
    const r = await api('PUT', `/api/tv/prefs/resume/${encodeURIComponent(d.id)}`, {mode: e.target.value});
    if (r.ok) await refreshDevices();
    if (out) out.textContent = r.ok ? 'Saved.' : (r.message || r.body || 'Could not save.');
    return;
  } if (e.target.id === 'showSys') { S.showSystem = e.target.checked; renderAppGrid(); } });
$('#appFilter').addEventListener('input', e => { S.filter = e.target.value; renderAppGrid(); });
$('#kbd').checked = localStorage.getItem('kbd') !== '0';
$('#kbd').onchange = e => localStorage.setItem('kbd', e.target.checked ? '1' : '0');
startApp();
