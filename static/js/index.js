(() => {
    "use strict";

    const $ = (id) => document.getElementById(id);
    const KEY = "synchub.dashboard.v1";

    const searchInput = $("searchInput");
    const typeSel = $("mediaTypeFilter");
    const userSel = $("userFilter");
    const statusSel = $("statusFilter");
    const sortSel = $("sortOrder");
    const perSel = $("itemsPerPage");

    const btnList = $("btnListView");
    const btnGrid = $("btnGridView");
    const listBox = $("filmList");
    const gridBox = $("filmGrid");
    const tbody = $("listTableBody");
    const btnPrev = $("btnPrevPage");
    const btnNext = $("btnNextPage");
    const pageInfo = $("pageInfo");
    const emptyState = $("emptyState");
    const rows = Array.from(tbody.querySelectorAll(".item-row"));

    const FIELDS = { q: searchInput, type: typeSel, user: userSel, status: statusSel, sort: sortSel, per: perSel };
    let page = 1;
    let view = "list";

    /* ---------- persistence (best effort) ---------- */
    function restore() {
        try {
            const s = JSON.parse(localStorage.getItem(KEY) || "{}");
            for (const [k, el] of Object.entries(FIELDS)) {
                if (typeof s[k] === "string") el.value = s[k];
            }
            if (s.view === "grid") view = "grid";
        } catch (_) { /* storage unavailable or corrupt */ }
        if (!sortSel.value) sortSel.value = "users-desc";
        if (!perSel.value) perSel.value = "18";
    }

    function persist() {
        try {
            const s = { view };
            for (const [k, el] of Object.entries(FIELDS)) s[k] = el.value;
            localStorage.setItem(KEY, JSON.stringify(s));
        } catch (_) { /* ignore */ }
    }

    /* ---------- grid card builder ---------- */
    function el(tag, cls, text) {
        const node = document.createElement(tag);
        if (cls) node.className = cls;
        if (text !== undefined) node.textContent = text;
        return node;
    }

    function buildCard(row) {
        const d = row.dataset;
        const count = parseInt(d.userCount, 10) || 0;
        const col = el("div", "col");
        const card = el("div", "card h-100");

        const img = el("img", "card-img-top poster poster-grid");
        img.src = "/poster/" + encodeURIComponent(d.slug);
        img.alt = "";
        img.loading = "lazy";
        card.appendChild(img);

        const body = el("div", "card-body p-2 d-flex flex-column justify-content-between");
        const top = el("div");

        const badges = el("div", "d-flex justify-content-between align-items-center mb-1");
        badges.appendChild(el("span", "badge " + (d.mediaType === "tv" ? "badge-tv" : "badge-movie") + " me-1", d.mediaType.toUpperCase()));
        badges.appendChild(el("span", "badge bg-primary", count + " Profile" + (count === 1 ? "" : "s")));
        top.appendChild(badges);

        let displayTitle = d.displayTitle;
        if (d.season) {
            displayTitle += ` (S${d.season}`;
            if (d.episode) displayTitle += `E${d.episode}`;
            displayTitle += `)`;
        }
        
        const title = el("h6", "card-title text-truncate mb-1", displayTitle);
        title.title = displayTitle;
        top.appendChild(title);

        if (d.availableSeasons) {
            top.appendChild(el("small", "text-success d-block text-truncate fw-bold", `Available: S${d.availableSeasons.split(', ').join(', S')}`));
        }

        top.appendChild(el("small", "text-body-secondary d-block text-truncate", "Added by: " + d.addedBy));
        body.appendChild(top);

        const foot = el("div", "mt-2 text-end");
        if (d.status === "jellyfin" && d.link) {
            const a = el("a", "btn btn-sm btn-success w-100 p-1 py-0 jellyfin-link", "Watch");
            a.href = d.link;
            a.dataset.appLink = d.appLink || "";
            a.target = "_blank";
            a.rel = "noopener noreferrer";
            foot.appendChild(a);
        } else if (d.status === "queued") {
            foot.appendChild(el("span", "badge bg-info text-dark w-100 py-1", "Queued"));
        } else {
            foot.appendChild(el("span", "badge bg-danger w-100 py-1", "Missing"));
        }
        body.appendChild(foot);
        card.appendChild(body);
        col.appendChild(card);
        return col;
    }

    /* ---------- render ---------- */
    const titleCmp = (a, b) => a.dataset.title.localeCompare(b.dataset.title);

    function render() {
        const q = searchInput.value.trim().toLowerCase();
        const type = typeSel.value;
        const user = userSel.value.toLowerCase();
        const status = statusSel.value;

        const visible = rows.filter((r) => {
            const d = r.dataset;
            return d.title.includes(q)
                && (!type || d.mediaType === type)
                && (!user || d.users.includes("|" + user + "|"))
                && (!status ||
                    (status === "missing" && (d.status === "missing" || d.status === "queued")) ||
                    (status === "missing_not_queued" && d.status === "missing") ||
                    (d.status === status)
                );
        });

        visible.sort(sortSel.value === "title-asc"
            ? titleCmp
            : (a, b) => (parseInt(b.dataset.userCount, 10) - parseInt(a.dataset.userCount, 10)) || titleCmp(a, b));

        const perPage = perSel.value === "all" ? 1e9 : parseInt(perSel.value, 10);
        const total = visible.length;
        const totalPages = Math.max(1, Math.ceil(total / perPage));

        page = Math.min(Math.max(page, 1), totalPages);
        const start = (page - 1) * perPage;
        const slice = visible.slice(start, start + perPage);

        // List view: reorder the real DOM nodes so the sort is actually visible.
        const shown = new Set(slice);
        rows.forEach((r) => r.classList.toggle("d-none", view !== "list" || !shown.has(r)));
        if (view === "list") slice.forEach((r) => tbody.appendChild(r));

        // Grid view: build only the cards on the current page.
        if (view === "grid") gridBox.replaceChildren(...slice.map(buildCard));
        else gridBox.replaceChildren();

        listBox.classList.toggle("d-none", view !== "list" || total === 0);
        gridBox.classList.toggle("d-none", view !== "grid" || total === 0);
        emptyState.classList.toggle("d-none", total !== 0 || rows.length === 0);

        btnList.classList.toggle("active", view === "list");
        btnGrid.classList.toggle("active", view === "grid");
        btnList.setAttribute("aria-pressed", String(view === "list"));
        btnGrid.setAttribute("aria-pressed", String(view === "grid"));

        btnPrev.disabled = page <= 1;
        btnNext.disabled = page >= totalPages;

        pageInfo.textContent = total === 0
            ? "No items to show"
            : `Showing ${start + 1}-${Math.min(start + perPage, total)} of ${total} items (Page ${page} of ${totalPages})`;

        persist();
    }

    function resetFilters() {
        searchInput.value = "";
        typeSel.value = "";
        userSel.value = "";
        statusSel.value = "";
        page = 1;
        render();
    }

    /* ---------- events ---------- */
    const rerenderFromStart = () => { page = 1; render(); };
    let debounce;

    searchInput.addEventListener("input", () => { clearTimeout(debounce); debounce = setTimeout(rerenderFromStart, 150); });
    [typeSel, userSel, statusSel, sortSel, perSel].forEach((s) => s.addEventListener("change", rerenderFromStart));

    btnList.addEventListener("click", () => { view = "list"; render(); });
    btnGrid.addEventListener("click", () => { view = "grid"; render(); });
    btnPrev.addEventListener("click", () => { page--; render(); });
    btnNext.addEventListener("click", () => { page++; render(); });
    $("btnResetFilters").addEventListener("click", resetFilters);

    // Clickable stat cards act as filter presets.
    function applyPreset(card) {
        searchInput.value = "";
        userSel.value = "";
        typeSel.value = card.dataset.setType || "";
        statusSel.value = card.dataset.setStatus || "";
        rerenderFromStart();
    }

    document.querySelectorAll(".stat-card-link").forEach((card) => {
        card.addEventListener("click", () => applyPreset(card));
        card.addEventListener("keydown", (e) => {
            if (e.key === "Enter" || e.key === " ") { e.preventDefault(); applyPreset(card); }
        });
    });

    // "/" focuses search (unless already typing somewhere).
    document.addEventListener("keydown", (e) => {
        const t = e.target;
        if (e.key === "/" && !/^(INPUT|TEXTAREA|SELECT)$/.test(t.tagName) && !t.isContentEditable) {
            e.preventDefault();
            searchInput.focus();
        }
    });

    // Poster fallback (error events don't bubble, so listen in the capture phase).
    const PLACEHOLDER = "data:image/svg+xml;utf8," + encodeURIComponent(
        '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 90 130"><rect width="90" height="130" fill="#2c3440"/>' +
        '<text x="45" y="68" font-size="11" fill="#8899aa" text-anchor="middle" font-family="sans-serif">No poster</text></svg>');

    document.addEventListener("error", (e) => {
        const t = e.target;
        if (t && t.tagName === "IMG" && t.classList.contains("poster") && !t.dataset.fallback) {
            t.dataset.fallback = "1";
            t.src = PLACEHOLDER;
        }
    }, true);
    
    // iOS App Link Fallback
    document.addEventListener("click", (e) => {
        const btn = e.target.closest(".jellyfin-link");
        if (btn && /iPad|iPhone|iPod/.test(navigator.userAgent)) {
            e.preventDefault();
            const appLink = btn.dataset.appLink;
            const webLink = btn.href;
            
            if (appLink) {
                window.location.href = appLink;
                setTimeout(() => {
                    window.location.href = webLink;
                }, 1500);
            } else {
                window.location.href = webLink;
            }
        }
    });

    restore();
    render();
})();