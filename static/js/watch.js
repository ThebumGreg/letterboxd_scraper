/* "Watch now": the one button wherever a title can be watched.
   Its menu offers "This device" (the Jellyfin link, which jellyfin-app.js opens in the Jellyfin app on an iPhone or iPad
   and in Jellyfin web everywhere else) and every TV SyncHub controls (it wakes the TV, opens Jellyfin and plays the title).
   Pages only place <span data-watch-now data-link data-item data-title data-size> where the button goes; this script builds it,
   including for rows the page adds later. Without the TV permission the button is just the "This device" link. */
(() => {
    "use strict";
    const configEl = document.getElementById("jellyfin-app-config");
    let cfg = {};
    try { cfg = JSON.parse(configEl.textContent); } catch (e) { /* no config: plain links */ }

    const BUTTON = { lg: "btn btn-success", row: "btn btn-sm btn-success", card: "btn btn-sm btn-success w-100 p-1 py-0", ep: "btn btn-sm btn-success py-0 px-3" };
    const make = (tag, className, text) => { const n = document.createElement(tag); if (className) n.className = className; if (text != null) n.textContent = text; return n; };
    const icon = (name) => { const i = make("i", `bi bi-${name} me-1`); i.setAttribute("aria-hidden", "true"); return i; };

    function deviceLink(slot, className) {
        const a = make("a", className);
        a.href = slot.dataset.link;
        a.dataset.jellyfinItem = slot.dataset.item;       // jellyfin-app.js opens the Jellyfin app from this on iOS
        a.target = "_blank";
        a.rel = "noopener noreferrer";
        return a;
    }

    function build(slot) {
        if (slot.dataset.watchReady || !slot.dataset.item || !slot.dataset.link) return;
        slot.dataset.watchReady = "1";
        const size = BUTTON[slot.dataset.size] || BUTTON.lg;
        slot.textContent = "";
        if (!cfg.tv) {                                    // nothing to choose between
            const a = deviceLink(slot, size);
            a.append(icon("play-circle"), "Watch now");
            a.setAttribute("aria-label", `Watch ${slot.dataset.title || "this title"} now`);
            slot.appendChild(a);
            return;
        }
        const wrap = make("div", "dropdown watch-now-menu" + (slot.dataset.size === "card" ? " w-100" : " d-inline-block"));
        wrap.dataset.item = slot.dataset.item;
        wrap.dataset.title = slot.dataset.title || "this title";
        const toggle = make("button", size + " dropdown-toggle");
        toggle.type = "button";
        toggle.dataset.bsToggle = "dropdown";
        toggle.dataset.bsPopperConfig = '{"strategy":"fixed"}';   // so a table that scrolls sideways cannot clip the menu
        toggle.setAttribute("aria-expanded", "false");
        toggle.setAttribute("aria-label", `Watch ${wrap.dataset.title} now: choose where`);
        toggle.append(icon("play-circle"), "Watch now");
        const menu = make("ul", "dropdown-menu dropdown-menu-end");
        const here = make("li");
        const a = deviceLink(slot, "dropdown-item");
        a.append(icon("phone"), "This device");
        here.appendChild(a);
        const divider = make("li"); divider.appendChild(make("hr", "dropdown-divider"));
        const header = make("li"); header.appendChild(make("h6", "dropdown-header", "On a TV"));
        const loading = make("li", "px-3 pb-1 small text-body-secondary", "Loading TVs...");
        loading.dataset.tvSlot = "1";
        menu.append(here, divider, header, loading);
        wrap.append(toggle, menu);
        slot.appendChild(wrap);
    }

    // ---- the TVs (asked for once per page, the first time a menu opens)
    let tvList = null;
    const loadTvs = () => tvList || (tvList = fetch("/api/tv/config", { headers: { Accept: "application/json" } })
        .then((r) => (r.ok ? r.json() : Promise.reject(new Error("HTTP " + r.status))))
        .then((d) => d.devices || [])
        .catch(() => null));

    async function fillTvs(wrap) {
        const slot = wrap.querySelector("[data-tv-slot]");
        if (!slot) return;
        const devices = await loadTvs();
        const rows = [];
        if (devices === null) rows.push(make("li", "px-3 pb-1 small text-danger", "Could not load the TVs."));
        else if (!devices.length) {
            const li = make("li", "px-3 pb-1 small text-body-secondary", "No TVs added yet.");
            if (cfg.tv_setup) { li.append(" "); const link = make("a", "text-decoration-none", "Add one"); link.href = "/tv/devices"; li.appendChild(link); }
            rows.push(li);
        } else {
            devices.forEach((d) => {
                const li = make("li");
                const b = make("button", "dropdown-item");
                b.type = "button";
                b.dataset.watchTv = d.id;
                b.dataset.tvName = d.name;
                const dot = make("i", "bi bi-circle-fill me-2 small text-body-secondary");
                dot.setAttribute("aria-hidden", "true");
                b.append(dot, d.name);
                li.appendChild(b);
                rows.push(li);
                fetch(`/api/tv/devices/${encodeURIComponent(d.id)}/status?poll=1`, { headers: { Accept: "application/json" } })
                    .then((r) => r.json())
                    .then((st) => {                          // a hint only: playing wakes a sleeping TV anyway
                        const color = !st.online ? "text-danger" : st.power === "on" ? "text-success" : "text-warning";
                        dot.className = `bi bi-circle-fill me-2 small ${color}`;
                        b.title = !st.online ? "Not answering; SyncHub will try to wake it" : st.power === "on" ? "On" : "Asleep; SyncHub will wake it";
                    })
                    .catch(() => { /* leave the grey dot */ });
            });
        }
        slot.replaceWith(...rows);
    }

    // A TV choice opens that TV's remote, which starts the title there and shows each step (waking a TV can take a minute).
    function playOn(wrap, button) {
        const params = new URLSearchParams({ tv: button.dataset.watchTv, play: wrap.dataset.item, title: wrap.dataset.title });
        window.location.href = `/tv/remote?${params}`;
    }

    document.addEventListener("show.bs.dropdown", (e) => {
        const wrap = e.target.closest(".watch-now-menu");
        if (wrap && !wrap.dataset.tvsRequested) { wrap.dataset.tvsRequested = "1"; fillTvs(wrap); }
    });
    document.addEventListener("click", (e) => {
        const button = e.target.closest("button[data-watch-tv]");
        if (button) playOn(button.closest(".watch-now-menu"), button);
    });

    const upgrade = (root) => root.querySelectorAll("[data-watch-now]").forEach(build);
    upgrade(document);
    new MutationObserver((changes) => {
        for (const change of changes) for (const node of change.addedNodes) {
            if (node.nodeType !== 1) continue;
            if (node.matches("[data-watch-now]")) build(node); else upgrade(node);
        }
    }).observe(document.body, { childList: true, subtree: true });
})();
