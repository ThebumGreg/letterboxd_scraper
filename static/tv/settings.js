'use strict';
/* TV apps page: which apps each TV's remote shows, quick launch, and a few per-browser preferences. */
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
  onSelect: renderAppGrid,
  onStatus: () => {},
  onApps: renderAppGrid,
  onPrefs: renderAppGrid,
  onNotices: renderNews,
});
document.addEventListener('change', e => { if (e.target.id === 'showSys') { S.showSystem = e.target.checked; renderAppGrid(); } });
$('#appFilter').addEventListener('input', e => { S.filter = e.target.value; renderAppGrid(); });
$('#kbd').checked = localStorage.getItem('kbd') !== '0';
$('#kbd').onchange = e => localStorage.setItem('kbd', e.target.checked ? '1' : '0');
startApp();
