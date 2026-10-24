'use strict';
/* TVs page: the TVs SyncHub controls, adding/editing them, and finding new ones on the network. */
Page.noApps = true;
const T = {tvs: [], found: [], editing: null, scanning: false};
const TYPE_NAME = {googletv: 'Google TV', androidtv: 'Android TV', roku: 'Roku'};

async function loadTvs() {
  const r = await api('GET', '/api/tv/setup/tvs');
  T.tvs = r.tvs || [];
  const files = T.tvs.filter(t => !t.managed).length;
  $('#tvFileNote').textContent = files ? `${files} ${files === 1 ? 'TV comes' : 'TVs come'} from ${String(r.configPath).split(/[\\/]/).pop()} and can only be edited there; the rest are saved in the app.` : '';
  renderTvs();
}
function statText(id) {
  const st = S.status[id];
  if (!st) return '<span class="dim">checking…</span>';
  if (!st.online) return `<span style="color:${st.unresponsive ? 'var(--warn)' : 'var(--bad)'}">${st.unresponsive ? 'asleep? not answering' : 'offline'}</span>${st.needsPairing ? ' · needs pairing' : ''}`;
  return `<span style="color:var(--ok)">${esc(POWER_LABEL[st.power] || st.power || 'online')}</span>${st.appName ? ' · ' + esc(st.appName) : ''}`;
}
function renderTvs() {
  const el = $('#tvList');
  el.innerHTML = T.tvs.map(t => {
    const meta = [t.typeLabel, t.host, t.mac ? 'MAC ' + t.mac : 'no MAC', t.sleep === 'screensaver' ? 'sleeps to screensaver' : null].filter(Boolean).map(esc).join(' · ');
    return `<div class="tvcard" data-id="${esc(t.id)}">
      <div class="tvhead"><b>${esc(t.name)}</b><span class="chip">${esc(t.managed ? 'saved in the app' : 'devices.json')}</span><span class="grow"></span><span class="small" data-stat>${statText(t.id)}</span></div>
      <div class="small dim">${meta}</div>
      ${t.type === 'googletv' ? `<div class="pair" data-pair hidden><b>Pairing required</b> <span class="small dim">Press Start, then type the code that appears on the TV.</span>
        <div class="tv-row"><button data-act="tvPairStart" data-id="${esc(t.id)}">Start pairing</button><input data-code placeholder="code on TV" maxlength="8" style="max-width:130px" aria-label="Pairing code"><button data-act="tvPairFinish" data-id="${esc(t.id)}">Finish</button></div></div>` : ''}
      <div class="tv-row">
        ${t.managed ? `<button data-act="tvEdit" data-id="${esc(t.id)}">Edit</button><button data-act="tvRemove" data-id="${esc(t.id)}">Remove</button>` : '<span class="small dim">Set in devices.json</span>'}
        <button data-act="tvMac" data-id="${esc(t.id)}" title="Read the MAC address (the TV must be on)">${t.mac ? 'Re-detect MAC' : 'Detect MAC'}</button>
      </div></div>`;
  }).join('') || '<div class="dim">No TVs yet. Use “Scan network” below, or add one manually.</div>';
  paintStatus();
}
function paintStatus() {   // status changes touch only the status text, so an open pairing field is never wiped
  document.querySelectorAll('.tvcard').forEach(card => {
    const id = card.dataset.id, st = S.status[id];
    card.querySelector('[data-stat]').innerHTML = statText(id);
    const pair = card.querySelector('[data-pair]'); if (pair) pair.hidden = !(st && st.needsPairing);
  });
}

/* ---- add / edit form */
function openForm(tv, preset) {
  T.editing = tv ? tv.id : null;
  const v = tv || preset || {};
  $('#tvFormTitle').textContent = tv ? `Edit ${tv.name}` : 'Add a TV';
  $('#fName').value = v.name || '';
  $('#fType').value = v.type || 'googletv';
  $('#fType').disabled = !!tv;
  $('#fHost').value = v.host || '';
  $('#fMac').value = v.mac || '';
  $('#fAdb').checked = !!v.adb;
  $('#fSleep').value = v.sleep || 'standby';
  $('#tvFormError').textContent = '';
  formKind();
  const dlg = $('#tvForm'); if (!dlg.open) dlg.showModal();
}
function formKind() {
  const kind = $('#fType').value;
  $('#fAdbRow').hidden = kind !== 'googletv';
  const screensaverOk = kind === 'androidtv' || (kind === 'googletv' && $('#fAdb').checked);
  $('#fSleep').querySelector('[value=screensaver]').disabled = !screensaverOk;
  if (!screensaverOk && $('#fSleep').value === 'screensaver') $('#fSleep').value = 'standby';
  $('#fSleep').closest('label').hidden = kind === 'roku';
}
async function saveForm(btn) {
  const body = {name: $('#fName').value, type: $('#fType').value, host: $('#fHost').value.trim(), mac: $('#fMac').value.trim(),
    adb: $('#fAdb').checked, sleep: $('#fSleep').value};
  const r = await run(btn, T.editing ? api('PUT', `/api/tv/setup/tvs/${encodeURIComponent(T.editing)}`, body) : api('POST', '/api/tv/setup/tvs', body));
  if (!r.ok) { $('#tvFormError').textContent = r.body || 'Could not save'; return; }
  $('#tvForm').close();
  const added = !T.editing;
  await refreshAll();
  if (added && body.type === 'googletv') $('#scanState').textContent = `${body.name || 'The TV'} is added. If it shows “Pairing required”, press Start pairing and enter the code the TV displays.`;
}
async function refreshAll() {
  S.status = {}; S.devId = '';
  await loadConfig();    // re-reads the device list for the other pages' sake
  await loadTvs();
  pollAll();
  renderFound();
}

/* ---- scanning */
async function prefillRanges() {
  const r = await api('GET', '/api/tv/setup/discovery/ranges');
  if (!$('#scanRanges').value && Array.isArray(r.ranges)) $('#scanRanges').value = r.ranges.join(', ');
}
function renderFound() {
  const el = $('#found');
  const added = host => T.tvs.find(t => t.host === host);
  el.innerHTML = T.found.map((f, i) => {
    const have = added(f.host);
    return `<div class="item"><span class="name"><b>${esc(f.name)}</b> <span class="chip">${esc(TYPE_NAME[f.type] || f.type)}</span>
      <span class="dim small">${esc(f.host)}${f.model ? ' · ' + esc(f.model) : ''}${f.mac ? ' · ' + esc(f.mac) : ''} · found by ${esc((f.via || []).join(' + '))}</span></span>
      ${have ? `<span class="chip">added as ${esc(have.name)}</span>` : `<button data-act="tvAddFound" data-i="${i}" class="primary">Add</button>`}</div>`;
  }).join('');
}
async function scan(btn) {
  if (T.scanning) return;
  T.scanning = true; btn.disabled = true;
  $('#scanState').textContent = 'Scanning… this takes up to 10 seconds.';
  $('#found').innerHTML = '';
  const ranges = $('#scanRanges').value.split(/[\s,]+/).filter(Boolean);
  const r = await api('POST', '/api/tv/setup/discovery/scan', {ranges});
  T.scanning = false; btn.disabled = false;
  fetchLog();
  if (!r.ok) { $('#scanState').innerHTML = `<span style="color:var(--bad)">${esc(r.body || 'Scan failed')}</span>`; return; }
  T.found = r.found || [];
  $('#scanState').innerHTML = (T.found.length ? `Found ${T.found.length} ${T.found.length === 1 ? 'TV' : 'TVs'}.` : '') + (r.notes || []).map(n => ` <span class="dim">${esc(n)}</span>`).join('');
  renderFound();
}

Object.assign(ACTIONS, {
  tvNew: () => openForm(null),
  tvEdit: b => openForm(T.tvs.find(t => t.id === b.dataset.id)),
  tvCancel: () => $('#tvForm').close(),
  tvSave: b => saveForm(b),
  tvRemove: async b => {
    const t = T.tvs.find(x => x.id === b.dataset.id);
    if (!confirm(`Remove ${t.name}? Its saved app choices go too.`)) return;
    await run(b, api('DELETE', `/api/tv/setup/tvs/${encodeURIComponent(t.id)}`));
    refreshAll();
  },
  tvScan: b => scan(b),
  tvAddFound: b => {
    const f = T.found[Number(b.dataset.i)];
    openForm(null, {name: f.name, type: f.type, host: f.host, mac: f.mac, adb: f.type === 'googletv' && f.adbOpen});
  },
  tvMac: async b => {
    const t = T.tvs.find(x => x.id === b.dataset.id);
    const r = await run(b, api('POST', devPath('/detect-mac', t.id), {}));
    if (!r.ok) return alert(r.body || 'Could not read the MAC address');
    if (t.managed) { await api('PUT', `/api/tv/setup/tvs/${encodeURIComponent(t.id)}`, {...t, mac: r.mac}); refreshAll(); }
    else alert(`${r.mac}\nAdd "mac": "${r.mac}" to ${t.id} in devices.json to keep it.`);
  },
  tvPairStart: b => run(b, api('POST', devPath('/pair/start', b.dataset.id), {})),
  tvPairFinish: async b => {
    const input = b.closest('.tvcard').querySelector('[data-code]');
    const r = await run(b, api('POST', devPath('/pair/finish', b.dataset.id), {code: input.value}));
    if (r.ok) { input.value = ''; pollAll(); } else alert(r.body || 'Pairing failed');
  },
});
Object.assign(Page, {onStatus: paintStatus, onSelect: () => {}});
$('#fType').addEventListener('change', formKind);
$('#fAdb').addEventListener('change', formKind);
$('#tvFormEl').addEventListener('submit', e => { e.preventDefault(); saveForm($('#tvSaveBtn')); });
$('#scanRanges').addEventListener('keydown', e => { if (e.key === 'Enter') ACTIONS.tvScan($('#scanBtn')); });
startApp().then(() => { loadTvs(); prefillRanges(); });
