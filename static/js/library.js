// Small helpers for the Library pages: a POST with the CSRF token, a message line, and confirmations on buttons.
(() => {
    const csrf = document.querySelector('meta[name="csrf-token"]')?.content || '';
    window.libraryPost = async (url, body) => {
        try {
            const res = await fetch(url, { method: 'POST', headers: { 'Content-Type': 'application/json', 'X-CSRFToken': csrf }, body: JSON.stringify(body || {}) });
            return await res.json();
        } catch (e) { return { ok: false, message: 'Could not reach the server.' }; }
    };
    window.librarySay = (text, ok, id = 'libMessage') => {
        const box = document.getElementById(id);
        if (!box) return;
        box.className = 'alert ' + (ok ? 'alert-success' : 'alert-danger');
        box.textContent = text;
        box.scrollIntoView({ block: 'nearest' });
    };
})();
