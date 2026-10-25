/* The "Playing now" strip at the top of the dashboard: which TVs are playing something through Jellyfin, with a link to each one's remote.
   Shown only to people who may use the TV remote, and only while something is playing. */
(() => {
    "use strict";
    const strip = document.getElementById("tvNow");
    const configEl = document.getElementById("jellyfin-app-config");
    if (!strip || !configEl) return;
    let cfg = {};
    try { cfg = JSON.parse(configEl.textContent); } catch (e) { return; }
    if (!cfg.tv) return;

    const make = (tag, className, text) => { const n = document.createElement(tag); if (className) n.className = className; if (text != null) n.textContent = text; return n; };

    function paint(tvs) {
        strip.textContent = "";
        strip.hidden = !tvs.length;
        if (!tvs.length) return;
        const row = make("div", "alert alert-secondary d-flex flex-wrap align-items-center gap-3 py-2 mb-0");
        const heading = make("strong", "text-nowrap");
        const icon = make("i", "bi bi-tv me-1");
        icon.setAttribute("aria-hidden", "true");
        heading.append(icon, "Playing now");
        row.appendChild(heading);
        tvs.forEach((tv) => {
            const item = make("span", "d-inline-flex flex-wrap align-items-baseline gap-2");
            item.append(make("span", "", tv.paused ? "⏸" : "▶"), make("strong", "", tv.name), make("span", "text-body-secondary", tv.title));
            const link = make("a", "small text-decoration-none", "Open remote");
            link.href = `/tv/remote?tv=${encodeURIComponent(tv.id)}`;
            item.appendChild(link);
            row.appendChild(item);
        });
        strip.appendChild(row);
    }

    let timer = null;
    async function poll() {
        clearTimeout(timer);
        if (!document.hidden) {
            try {
                const res = await fetch("/api/tv/now", { headers: { Accept: "application/json" } });
                if ([401, 403, 404].includes(res.status)) return;       // not allowed (or not there): stop quietly
                if (res.ok) paint((await res.json()).tvs || []);
            } catch (e) { /* the next tick tries again */ }
        }
        timer = setTimeout(poll, 5000);
    }
    document.addEventListener("visibilitychange", () => { if (!document.hidden) poll(); });
    poll();
})();
