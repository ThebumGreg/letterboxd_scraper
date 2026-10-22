/* Opens Jellyfin items in the Jellyfin app (Swiftfin) on an iPhone or iPad.
   Everything else, and any failure to open the app, follows the normal web link.
   The app needs swiftfin://<server id>/<user id>/item/<item id>: the server ID comes from SyncHub,
   the user is the Jellyfin user signed in to the app on this device, chosen once and remembered here. */
(() => {
    const configEl = document.getElementById("jellyfin-app-config");
    if (!configEl) return;
    let cfg;
    try { cfg = JSON.parse(configEl.textContent); } catch (e) { return; }
    const isIOS = /iPad|iPhone|iPod/.test(navigator.userAgent) || (navigator.platform === "MacIntel" && navigator.maxTouchPoints > 1);
    if (!isIOS || !cfg.server_id || !(cfg.users || []).length) return;

    const KEY = "synchub.jellyfinUserId";
    const saved = () => { try { return localStorage.getItem(KEY); } catch (e) { return null; } };
    const remember = (id) => { try { localStorage.setItem(KEY, id); } catch (e) { /* private mode: asked again next time */ } };
    const known = (id) => cfg.users.find((u) => u.id === id) || null;
    // The profile chosen on the "Who is watching?" page wins; a guest falls back to what was picked on this device, or the only user there is.
    const currentUser = () => known(cfg.current_user) || known(saved()) || (cfg.users.length === 1 ? cfg.users[0] : null);

    function openInApp(link, user) {
        const url = `swiftfin://${encodeURIComponent(cfg.server_id)}/${encodeURIComponent(user.id)}/item/${encodeURIComponent(link.dataset.jellyfinItem)}`;
        let left = false;
        const mark = () => { if (document.hidden) left = true; };
        document.addEventListener("visibilitychange", mark);
        window.addEventListener("pagehide", () => { left = true; }, { once: true });
        window.location.href = url;
        // If the app took over, the page is hidden by now. If not (app missing, wrong user), fall back to the web page.
        setTimeout(() => {
            document.removeEventListener("visibilitychange", mark);
            if (!left && !document.hidden && document.hasFocus()) window.location.href = link.href;
        }, 1800);
    }

    let modal = null;
    function chooseUser(done) {
        const box = document.getElementById("jellyfinUserChoices");
        const el = document.getElementById("jellyfinUserModal");
        if (!box || !el || !window.bootstrap) { done(cfg.users[0]); return; }
        box.textContent = "";
        cfg.users.forEach((user) => {
            const button = document.createElement("button");
            button.type = "button";
            button.className = "btn btn-outline-success";
            button.textContent = user.name;
            button.addEventListener("click", () => {
                remember(user.id);
                modal.hide();
                done(user);
            });
            box.appendChild(button);
        });
        modal = bootstrap.Modal.getOrCreateInstance(el);
        modal.show();
    }

    document.addEventListener("click", (e) => {
        const link = e.target.closest("a[data-jellyfin-item]");
        if (!link || !link.dataset.jellyfinItem || e.metaKey || e.ctrlKey || e.shiftKey) return;
        e.preventDefault();
        const user = currentUser();
        if (user) openInApp(link, user); else chooseUser((chosen) => openInApp(link, chosen));
    });

    const change = document.getElementById("jellyfinUserChange");
    if (change && cfg.users.length > 1 && !known(cfg.current_user)) {
        change.classList.remove("d-none");
        change.addEventListener("click", (e) => {
            e.preventDefault();
            chooseUser(() => {});
        });
    }
})();
