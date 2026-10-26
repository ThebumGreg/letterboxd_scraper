/* static/js/dashboard.js (v2 dashboard) */
(() => {
    "use strict";

    const $ = (id) => document.getElementById(id);
    const KEY = "synchub.dashboard.v2";

    const searchInput = $("searchInput");
    const typeSel = $("mediaTypeFilter");
    const userSel = $("userFilter");
    const statusSel = $("statusFilter");
    const watchSel = $("watchFilter");
    const certSel = $("certFilter");
    const yearFrom = $("yearFrom");
    const yearTo = $("yearTo");
    const chipBox = $("activeFilters");
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
    const rows = Array.from(tbody ? tbody.querySelectorAll(".item-row") : []);

    const FIELDS = { q: searchInput, type: typeSel, user: userSel, status: statusSel, watch: watchSel, cert: certSel, yfrom: yearFrom, yto: yearTo, sort: sortSel, per: perSel };
    const FILTERS = [   // what each filter is called on its chip, and how to show its value
        [searchInput, "Search", (e) => `"${e.value.trim()}"`], [typeSel, "Type", (e) => e.selectedOptions[0].textContent],
        [userSel, "Requested or watched by", (e) => e.selectedOptions[0].textContent], [statusSel, "Status", (e) => e.selectedOptions[0].textContent],
        [watchSel, "My progress", (e) => e.selectedOptions[0].textContent], [certSel, "Age rating", (e) => e.value],
        [yearFrom, "From year", (e) => e.value], [yearTo, "To year", (e) => e.value],
    ];
    let page = 1;
    let view = "list";

    function restore() {
        try {
            const s = JSON.parse(localStorage.getItem(KEY) || "{}");
            for (const [k, el] of Object.entries(FIELDS)) {
                if (el && typeof s[k] === "string") el.value = s[k];
            }
            if (s.view === "grid") view = "grid";
        } catch (_) { }
        if (sortSel && !sortSel.value) sortSel.value = "users-desc";
        if (perSel && !perSel.value) perSel.value = "18";
    }

    function persist() {
        try {
            const s = { view };
            for (const [k, el] of Object.entries(FIELDS)) {
                if(el) s[k] = el.value;
            }
            localStorage.setItem(KEY, JSON.stringify(s));
        } catch (_) { }
    }

    // Where the "Watch now" button goes; watch.js turns it into the menu (This device / each TV). The plain link is what shows without it.
    function watchSlot(link, item, title, size) {
        const slot = el("span", size === "card" ? "d-block" : "");
        slot.dataset.watchNow = "";
        slot.dataset.link = link;
        slot.dataset.item = item || "";
        slot.dataset.title = title || "";
        slot.dataset.size = size;
        const a = el("a", "btn btn-sm btn-success" + (size === "card" ? " w-100 p-1 py-0" : ""), "Watch now");
        a.href = link;
        a.dataset.jellyfinItem = item || "";
        a.target = "_blank";
        a.rel = "noopener noreferrer";
        slot.appendChild(a);
        return slot;
    }

    function el(tag, cls, text) {
        const node = document.createElement(tag);
        if (cls) node.className = cls;
        if (text !== undefined) node.textContent = text;
        return node;
    }

    const IN_FLIGHT = ["downloading", "downloaded", "transferring", "working", "encoding", "transferring_library"];

    function buildCard(row) {
        const d = row.dataset;
        const count = parseInt(d.userCount, 10) || 0;
        const col = el("div", "col");
        const card = el("div", "card h-100");

        const imgLink = el("a", "d-block text-decoration-none");
        imgLink.href = d.url;
        const img = el("img", "card-img-top poster poster-grid");
        img.src = d.poster;
        img.alt = "";
        img.loading = "lazy";
        imgLink.appendChild(img);
        card.appendChild(imgLink);

        const body = el("div", "card-body p-2 d-flex flex-column justify-content-between");
        const top = el("div");

        const badges = el("div", "d-flex justify-content-between align-items-center mb-1");
        badges.appendChild(el("span", "badge " + (d.kind === "show" ? "badge-tv" : "badge-movie") + " me-1", d.kind === "show" ? "TV" : "MOVIE"));
        badges.appendChild(el("span", "badge bg-primary", count + " Profile" + (count === 1 ? "" : "s")));
        top.appendChild(badges);

        const titleLink = el("a", "text-decoration-none text-reset");
        titleLink.href = d.url;
        const title = el("h6", "card-title text-truncate mb-1 sse-title", d.displayTitle);
        title.title = d.displayTitle;
        if (d.year) title.appendChild(el("span", "badge bg-secondary fw-normal ms-1", d.year));
        titleLink.appendChild(title);
        top.appendChild(titleLink);

        if (d.summary) top.appendChild(el("small", "text-success d-block text-truncate fw-bold", d.summary));
        if (d.addedBy) top.appendChild(el("small", "text-body-secondary d-block text-truncate", "Wanted by: " + d.addedBy));
        if (d.addedLabel) top.appendChild(el("small", "text-body-secondary d-block text-truncate", "Added " + d.addedLabel));
        body.appendChild(top);

        const foot = el("div", "mt-2 text-end sse-badge-container");
        if (d.state === "in_library" && d.link) {
            foot.appendChild(watchSlot(d.link, d.jellyfinItem, d.displayTitle || d.title, "card"));
        } else {
            const dark = ["info", "warning"].includes(d.stateColor) ? " text-dark" : "";
            const pct = d.state === "downloading" && d.progress ? " " + d.progress + "%" : "";
            foot.appendChild(el("span", "badge bg-" + d.stateColor + dark + " w-100 py-1 sse-badge", d.stateLabel + pct));
        }
        body.appendChild(foot);
        card.appendChild(body);
        col.appendChild(card);
        return col;
    }

    const titleCmp = (a, b) => a.dataset.title.localeCompare(b.dataset.title);
    const addedCmp = (a, b) => (b.dataset.added || "").localeCompare(a.dataset.added || "");     // newest first; titles with no date last
    const yearCmp = (a, b) => (parseInt(b.dataset.year, 10) || 0) - (parseInt(a.dataset.year, 10) || 0);

    function render() {
        if (!searchInput) return;

        const words = searchInput.value.trim().toLowerCase().split(/\s+/).filter(Boolean);     // every word must appear in the title, year, requester or rating
        const type = typeSel.value;
        const user = userSel.value.toLowerCase();
        const status = statusSel.value;
        const seen = watchSel ? watchSel.value : "";
        const cert = certSel ? certSel.value : "";
        const from = yearFrom ? parseInt(yearFrom.value, 10) : NaN;
        const to = yearTo ? parseInt(yearTo.value, 10) : NaN;

        const visible = rows.filter((r) => {
            const d = r.dataset;
            const year = parseInt(d.year, 10);
            return words.every((w) => d.search.includes(w))
                && (!type || d.kind === type)
                && (!user || d.users.includes("|" + user + "|"))
                && (!status || (status === "in_flight" ? IN_FLIGHT.includes(d.state) : d.state === status))
                && (!seen || d.watch === seen)
                && (!cert || d.cert === cert)
                && (isNaN(from) || (year && year >= from))
                && (isNaN(to) || (year && year <= to));
        });
        renderChips();

        visible.sort((a, b) => {
            if (sortSel.value === "title-asc") return titleCmp(a, b);
            if (sortSel.value === "year-desc") return yearCmp(a, b) || titleCmp(a, b);
            if (sortSel.value === "added-desc") return addedCmp(a, b) || titleCmp(a, b);
            return (parseInt(b.dataset.userCount, 10) - parseInt(a.dataset.userCount, 10)) || titleCmp(a, b);
        });

        const perPage = perSel.value === "all" ? 1e9 : parseInt(perSel.value, 10);
        const total = visible.length;
        const totalPages = Math.max(1, Math.ceil(total / perPage));

        page = Math.min(Math.max(page, 1), totalPages);
        const start = (page - 1) * perPage;
        const slice = visible.slice(start, start + perPage);

        const shown = new Set(slice);
        rows.forEach((r) => r.classList.toggle("d-none", view !== "list" || !shown.has(r)));
        if (view === "list") slice.forEach((r) => tbody.appendChild(r));

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

    function clearFilter(el) {
        if (el) el.value = "";
    }

    function resetFilters() {
        if(!searchInput) return;
        FILTERS.forEach(([el]) => clearFilter(el));
        page = 1;
        render();
    }

    // The filters in force as removable chips, so it is clear why a title is missing.
    function renderChips() {
        if (!chipBox) return;
        const chips = FILTERS.filter(([el]) => el && el.value.trim() !== "").map(([el, name, show]) => {
            const b = document.createElement("button");
            b.type = "button";
            b.className = "btn btn-sm btn-outline-secondary d-inline-flex align-items-center gap-1";
            b.setAttribute("aria-label", `Remove filter ${name}: ${show(el)}`);
            b.append(`${name}: ${show(el)} `, Object.assign(document.createElement("i"), { className: "bi bi-x-lg" }));
            b.addEventListener("click", () => { clearFilter(el); rerenderFromStart(); });
            return b;
        });
        if (chips.length > 1) {
            const all = document.createElement("button");
            all.type = "button";
            all.className = "btn btn-sm btn-link";
            all.textContent = "Clear all";
            all.addEventListener("click", resetFilters);
            chips.push(all);
        }
        chipBox.replaceChildren(...chips);
        chipBox.classList.toggle("d-none", chips.length === 0);
    }

    const rerenderFromStart = () => { page = 1; render(); };
    let debounce;

    if (searchInput) {
        searchInput.addEventListener("input", () => { clearTimeout(debounce); debounce = setTimeout(rerenderFromStart, 150); });
        [typeSel, userSel, statusSel, watchSel, certSel, sortSel, perSel].forEach((s) => s && s.addEventListener("change", rerenderFromStart));
        [yearFrom, yearTo].forEach((s) => s && s.addEventListener("input", () => { clearTimeout(debounce); debounce = setTimeout(rerenderFromStart, 300); }));

        btnList.addEventListener("click", () => { view = "list"; render(); });
        btnGrid.addEventListener("click", () => { view = "grid"; render(); });
        btnPrev.addEventListener("click", () => { page--; render(); });
        btnNext.addEventListener("click", () => { page++; render(); });
        $("btnResetFilters").addEventListener("click", resetFilters);
    }

    if (tbody) {
        tbody.addEventListener("click", (e) => {
            const row = e.target.closest(".item-row");
            if (!row) return;
            if (e.target.closest("a, button, input, select")) return;

            const detailsUrl = row.dataset.url;
            window.location.href = detailsUrl;
        });
    }

    function applyPreset(card) {
        if(!searchInput) return;
        FILTERS.forEach(([el]) => clearFilter(el));
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
    
    // Live updates (Server-Sent Events). "rows" carries the new state of titles that changed on the server (download
    // progress, a finished import, ...); the older per-event messages give an instant hint before the server has finished reading.
    function paintStatusCell(row) {
        const cell = row.querySelector("td:last-child");
        if (!cell) return;
        const d = row.dataset;
        cell.textContent = "";
        if (d.state === "in_library" && d.link) {
            cell.appendChild(watchSlot(d.link, d.jellyfinItem, d.displayTitle, "row"));
            return;
        }
        const holder = el("span", "sse-badge-container");
        const dark = ["info", "warning"].includes(d.stateColor) ? " text-dark" : "";
        const badge = el("span", "badge bg-" + d.stateColor + dark + " sse-badge");
        if (d.stateIcon) badge.appendChild(el("i", "bi " + d.stateIcon));
        badge.appendChild(document.createTextNode((d.stateIcon ? " " : "") + d.stateLabel + (d.state === "downloading" && d.progress ? " " + d.progress + "%" : "")));
        holder.appendChild(badge);
        cell.appendChild(holder);
    }

    function applyRows(updates) {
        let changed = false;
        updates.forEach((u) => {
            const row = document.querySelector(`.item-row[data-id="${u.id}"]`);
            if (!row) return;
            Object.assign(row.dataset, {
                state: u.state, stateLabel: u.label, stateColor: u.color, stateIcon: u.icon || "",
                progress: u.progress === null || u.progress === undefined ? "" : String(u.progress),
                link: u.link || "", jellyfinItem: u.jellyfin_item || "", summary: u.summary || "",
                added: u.added || row.dataset.added || "", addedLabel: u.added_label || row.dataset.addedLabel || "",
            });
            if (row.dataset.addedLabel) {
                let note = row.querySelector("[data-added-text]");
                if (!note) { note = el("small", "text-body-secondary d-block"); note.dataset.addedText = ""; row.querySelectorAll("td")[1]?.appendChild(note); }
                note.textContent = "Added " + row.dataset.addedLabel;
            }
            paintStatusCell(row);
            changed = true;
        });
        if (changed && view === "grid") render();
    }

    if (window.EventSource) {
        const evtSource = new EventSource("/api/stream/events");
        evtSource.onmessage = function(event) {
            try {
                const data = JSON.parse(event.data);
                if (data.event === "rows") {
                    applyRows(data.rows || []);
                    return;
                }
                let matchedRows = [];

                if (data.tmdb_id) {
                    matchedRows = document.querySelectorAll(`[data-tmdb-id="${data.tmdb_id}"]`);
                } else if (data.tvdb_id) {
                    matchedRows = document.querySelectorAll(`[data-tvdb-id="${data.tvdb_id}"]`);
                }

                matchedRows.forEach(row => {
                    row.dataset.state = data.state || "downloading";
                    const badgeCell = row.querySelector("td:last-child");

                    if (badgeCell) {
                        const existingBadge = badgeCell.querySelector('.sse-badge');
                        if (existingBadge) {
                            existingBadge.innerText = data.queue_status || 'Downloading...';
                        } else if (!badgeCell.querySelector('[data-watch-now]') && !badgeCell.querySelector('.btn-outline-info')) {
                            badgeCell.innerHTML = `<span class="badge bg-info text-dark sse-badge">${data.queue_status || 'Downloading...'}</span>`;
                        }
                    }
                });

                if(matchedRows.length > 0 && view === "grid") {
                    render();
                }
            } catch (err) {
                console.warn("Error parsing SSE data", err);
            }
        };
    }

    restore();
    render();
})();