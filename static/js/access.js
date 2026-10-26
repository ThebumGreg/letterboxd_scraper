// Access controls: pages, rating limits and PINs (used by the People page and each person's page).
(() => {
    const token = document.querySelector('meta[name="csrf-token"]')?.content || '';
    const box = document.getElementById('accessMessage');
    const say = (text, ok) => { box.className = 'alert ' + (ok ? 'alert-success' : 'alert-danger'); box.textContent = text; box.scrollIntoView({ block: 'nearest' }); };
    async function post(url, body) {
        try {
            const res = await fetch(url, { method: 'POST', headers: { 'Content-Type': 'application/json', 'X-CSRFToken': token }, body: JSON.stringify(body || {}) });
            return await res.json();
        } catch (e) { return { ok: false, message: 'Could not reach the server.' }; }
    }
    const after = (data, reload) => { say(data.message || (data.ok ? 'Done.' : 'Failed.'), data.ok); if (data.ok && reload) setTimeout(() => location.reload(), 900); };
    const ticked = (owner) => [...document.querySelectorAll(`[data-perm-grid="${owner}"] input:checked`)].map((i) => i.value);

    document.addEventListener('click', async (e) => {
        const t = e.target.closest('button');
        if (!t) return;
        if (t.dataset.preset) {
            const keys = t.dataset.keys ? t.dataset.keys.split(',') : [];
            document.querySelectorAll(`[data-perm-grid="${t.dataset.preset}"] input`).forEach((i) => { i.checked = keys.includes(i.value); });
        } else if (t.dataset.savePerms) {
            const owner = t.dataset.savePerms;
            after(await post(owner === 'guest' ? '/api/access/guest' : `/api/access/profile/${owner}/permissions`, { permissions: ticked(owner) }), true);
        } else if (t.dataset.saveRating) {
            const owner = t.dataset.saveRating;
            after(await post(owner === 'guest' ? '/api/access/guest-rating' : `/api/access/profile/${owner}/rating`, { max_rating: document.getElementById(`rating-${owner}`).value }), true);
        } else if (t.dataset.setPin) {
            const input = document.getElementById(`pin-${t.dataset.setPin}`);
            const data = await post(`/api/access/profile/${t.dataset.setPin}/pin`, { pin: input.value });
            if (data.ok) input.value = '';
            after(data, true);
        } else if (t.dataset.clearPin) {
            if (!confirm(`Remove the PIN of ${t.dataset.name}? Anyone on your network will be able to choose this profile.`)) return;
            after(await post(`/api/access/profile/${t.dataset.clearPin}/pin/clear`), true);
        } else if (t.dataset.signout) {
            after(await post(`/api/access/profile/${t.dataset.signout}/signout`), false);
        } else if (t.dataset.unlock) {
            after(await post(`/api/access/profile/${t.dataset.unlock}/unlock`), true);
        } else if (t.id === 'saveSessionDays') {
            after(await post('/api/access/session-days', { days: document.getElementById('sessionDays').value }), false);
        }
    });
})();
