(() => {
    "use strict";

    const $ = (id) => document.getElementById(id);
    const consoleEl = $("logConsole");
    const levelSel = $("logLevelFilter");
    const limitSel = $("logLimit");
    const searchInput = $("logSearch");
    const autoToggle = $("autoRefreshToggle");
    const countEl = $("logCount");
    const errorEl = $("logError");
    const followBadge = $("followBadge");

    const LEVEL_CLASS = { ERROR: "log-err", CRITICAL: "log-err", WARNING: "log-warn", DEBUG: "log-dbg" };
    const LEVEL_RE = /\[(DEBUG|INFO|WARNING|ERROR|CRITICAL)\]/;

    let raw = [];
    let clearedLast = null;   // last line that was visible when "Clear view" was pressed
    let timer = null;
    let inFlight = false;
    let visibleLines = [];

    const nearBottom = () => consoleEl.scrollHeight - consoleEl.scrollTop - consoleEl.clientHeight < 40;

    function currentLines() {
        let lines = raw;
        if (clearedLast !== null) {
            const i = lines.lastIndexOf(clearedLast);
            if (i >= 0) lines = lines.slice(i + 1);
            else clearedLast = null;   // the window has moved past the cleared point
        }
        const level = levelSel.value;
        const q = searchInput.value.toLowerCase();
        return lines.filter((l) => (!level || l.includes(`[${level}]`)) && (!q || l.toLowerCase().includes(q)));
    }

    function render() {
        const follow = nearBottom();
        const prevTop = consoleEl.scrollTop;

        visibleLines = currentLines();
        const frag = document.createDocumentFragment();
        for (const line of visibleLines) {
            const div = document.createElement("div");
            const m = line.match(LEVEL_RE);
            div.className = "log-line" + (m && LEVEL_CLASS[m[1]] ? " " + LEVEL_CLASS[m[1]] : "");
            div.textContent = line;   // textContent keeps log content from being interpreted as HTML
            frag.appendChild(div);
        }
        if (!visibleLines.length) {
            const div = document.createElement("div");
            div.textContent = raw.length ? "No matching logs found." : "No logs yet.";
            frag.appendChild(div);
        }
        consoleEl.replaceChildren(frag);

        // Only jump to the newest line if the user was already following the tail.
        consoleEl.scrollTop = follow ? consoleEl.scrollHeight : prevTop;
        followBadge.classList.toggle("d-none", follow);
        countEl.textContent = `Showing ${visibleLines.length} / ${raw.length} lines`;
    }

    async function fetchLogs() {
        if (inFlight) return;
        inFlight = true;
        try {
            const res = await fetch(`/api/logs?tail=${encodeURIComponent(limitSel.value)}`, { headers: { Accept: "application/json" } });
            if (!res.ok) throw new Error(`HTTP ${res.status}`);
            const data = await res.json();
            const lines = Array.isArray(data.logs) ? data.logs : [];
            raw = lines.slice(-parseInt(limitSel.value, 10));
            errorEl.classList.add("d-none");
            render();
        } catch (err) {
            // Keep the existing console content and just flag the problem.
            errorEl.textContent = `Could not refresh logs (${err.message}). Showing the last successful result.`;
            errorEl.classList.remove("d-none");
        } finally {
            inFlight = false;
        }
    }

    function schedule() {
        clearTimeout(timer);
        if (!autoToggle.checked || document.hidden) return;
        timer = setTimeout(async () => { await fetchLogs(); schedule(); }, 3000);
    }

    $("btnRefreshLogs").addEventListener("click", fetchLogs);

    $("btnClearLogs").addEventListener("click", () => {
        clearedLast = raw.length ? raw[raw.length - 1] : null;
        render();
        countEl.textContent = "View cleared. New lines will appear here.";
    });

    $("btnDownloadLogs").addEventListener("click", () => {
        const blob = new Blob([visibleLines.join("\n") + "\n"], { type: "text/plain" });
        const a = document.createElement("a");
        const stamp = new Date().toISOString().replace(/[:T]/g, "-").slice(0, 19);
        a.href = URL.createObjectURL(blob);
        a.download = `synchub-logs-${stamp}.txt`;
        document.body.appendChild(a);
        a.click();
        a.remove();
        setTimeout(() => URL.revokeObjectURL(a.href), 1000);
    });

    levelSel.addEventListener("change", render);
    searchInput.addEventListener("input", render);
    limitSel.addEventListener("change", fetchLogs);
    autoToggle.addEventListener("change", schedule);
    consoleEl.addEventListener("scroll", () => followBadge.classList.toggle("d-none", nearBottom()));

    // Pause polling while the tab is hidden; catch up when it returns.
    document.addEventListener("visibilitychange", () => {
        if (document.hidden) clearTimeout(timer);
        else fetchLogs().finally(schedule);
    });

    fetchLogs().finally(schedule);
})();
