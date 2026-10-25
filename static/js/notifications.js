/* The Notifications page: add a delivery method (check the key, pick a device, add), send a test, switch off, remove, and the history. */
(() => {
    "use strict";
    const root = document.getElementById("notifyPage");
    if (!root) return;

    const csrfToken = () => document.querySelector('meta[name="csrf-token"]')?.content || "";
    const el = (tag, text, cls) => { const n = document.createElement(tag); if (text !== undefined) n.textContent = text; if (cls) n.className = cls; return n; };
    const BADGE = { sent: "bg-success", pending: "bg-warning text-dark", held: "bg-info text-dark", failed: "bg-danger", dropped: "bg-secondary" };

    async function post(url, body) {
        try {
            const res = await fetch(url, {
                method: "POST",
                headers: { "Content-Type": "application/json", Accept: "application/json", "X-CSRFToken": csrfToken() },
                body: JSON.stringify(body || {}),
            });
            let data = {};
            try { data = await res.json(); } catch (e) { /* not JSON: the message below says so */ }
            return { ok: res.ok && data.ok !== false, data, message: data.message || (res.ok ? "Done." : `That did not work (HTTP ${res.status}).`) };
        } catch (e) {
            return { ok: false, data: {}, message: "Could not reach SyncHub." };
        }
    }

    const when = (utc) => {
        const d = new Date(String(utc).replace(" ", "T") + "Z");
        return isNaN(d) ? utc : d.toLocaleString(undefined, { month: "short", day: "numeric", hour: "numeric", minute: "2-digit" });
    };
    const localTimes = () => root.querySelectorAll("[data-utc]").forEach((n) => { n.textContent = when(n.dataset.utc); });
    localTimes();

    function say(out, text, good) {
        out.textContent = text;
        out.className = (out.dataset.base || "small ms-2") + " " + (good ? "text-success" : "text-danger");
    }

    /* ---------- the history ---------- */
    function renderHistory(rows) {
        const body = document.getElementById("historyBody");
        if (!body) return;
        body.textContent = "";
        if (!rows.length) {
            const tr = el("tr"); const td = el("td", "Nothing has been sent to you yet.", "text-center text-muted p-4"); td.colSpan = 4; tr.appendChild(td); body.appendChild(tr);
            return;
        }
        rows.forEach((h) => {
            const tr = el("tr");
            const time = el("td", undefined, "ps-4"); time.appendChild(el("small", when(h.sent_at || h.created_at))); tr.appendChild(time);
            const what = el("td"); what.appendChild(el("div", h.title)); what.appendChild(el("small", h.body, "text-body-secondary")); tr.appendChild(what);
            tr.appendChild(el("td", h.channel_label));
            const result = el("td"); result.appendChild(el("span", h.status.charAt(0).toUpperCase() + h.status.slice(1), "badge " + (BADGE[h.status] || "bg-secondary")));
            if (h.error) result.appendChild(el("div", h.error, "small text-body-secondary"));
            tr.appendChild(result); body.appendChild(tr);
        });
    }
    async function refreshHistory() {
        try {
            const res = await fetch("/api/notifications/history", { headers: { Accept: "application/json" } });
            const data = await res.json();
            if (res.ok && data.ok) renderHistory(data.history);
        } catch (e) { /* the page still shows what it had */ }
    }

    /* ---------- the buttons on each delivery method ---------- */
    root.addEventListener("click", async (e) => {
        const button = e.target.closest("button[data-act]");
        if (!button) return;
        const item = button.closest("[data-channel]");
        const out = item.querySelector("[data-result]");
        const base = `/api/notifications/channels/${encodeURIComponent(item.dataset.channel)}`;
        if (button.dataset.act === "delete" && !window.confirm("Remove this delivery method? Its history stays.")) return;
        button.disabled = true;
        try {
            if (button.dataset.act === "test") {
                say(out, "Sending...", true);
                const r = await post(`${base}/test`);
                say(out, r.message, r.ok);
                const badge = item.querySelector("[data-status]");
                if (r.ok && badge) { badge.textContent = "Working"; badge.className = "badge bg-success ms-1"; }
                refreshHistory();
            } else if (button.dataset.act === "toggle") {
                const r = await post(`${base}/enabled`, { enabled: button.dataset.enabled !== "true" });
                if (r.ok) window.location.reload(); else say(out, r.message, false);
            } else if (button.dataset.act === "delete") {
                const r = await post(`${base}/delete`);
                if (r.ok) window.location.reload(); else say(out, r.message, false);
            }
        } finally {
            if (document.body.contains(button)) button.disabled = false;
        }
    });

    /* ---------- adding one: check the key (which lists the choices the service offers), then add ---------- */
    const form = document.getElementById("addChannel");
    if (!form) return;
    const select = document.getElementById("provider");
    const sections = [...form.querySelectorAll("[data-provider]")];
    const result = document.getElementById("addResult");
    const after = document.getElementById("afterCheck");
    const current = () => (select ? select.value : sections[0].dataset.provider);
    const fields = () => [...form.querySelectorAll(`[data-provider="${current()}"] [data-field]`)];
    const config = () => Object.fromEntries(fields().map((i) => [i.dataset.field, i.value]));

    const reset = () => {
        after.hidden = true; result.textContent = "";
        form.querySelectorAll("select[data-field]").forEach((s) => {      // a choice list made by a check goes back to the plain box
            const box = el("input", undefined, "form-control"); box.type = "text"; box.id = s.id; box.dataset.field = s.dataset.field; box.autocomplete = "off";
            s.replaceWith(box);
        });
    };
    select?.addEventListener("change", () => { sections.forEach((s) => { s.hidden = s.dataset.provider !== current(); }); reset(); });
    form.addEventListener("input", (e) => { if (e.target.matches("input[data-field]") && !after.hidden) reset(); });

    document.getElementById("checkBtn").addEventListener("click", async (e) => {
        const button = e.currentTarget;
        button.disabled = true; say(result, "Checking...", true);
        const r = await post("/api/notifications/verify", { provider: current(), config: config() });
        button.disabled = false;
        if (!r.ok) { say(result, r.message, false); after.hidden = true; return; }
        say(result, r.message, true);
        Object.entries(r.data.choices || {}).forEach(([name, options]) => {
            const box = form.querySelector(`[data-provider="${current()}"] [data-field="${name}"]`);
            if (!box || !options.length) return;
            const choose = el("select", undefined, "form-select"); choose.id = box.id; choose.dataset.field = name;
            const listed = box.value && !options.includes(box.value) ? [...options, box.value] : options;      // what was typed stays a choice
            choose.appendChild(new Option("All of them", ""));
            listed.forEach((o) => choose.appendChild(new Option(o, o)));
            choose.value = box.value || "";
            box.replaceWith(choose);
        });
        after.hidden = false;
    });

    document.getElementById("addBtn").addEventListener("click", async (e) => {
        const button = e.currentTarget;
        button.disabled = true; say(result, "Adding...", true);
        const r = await post("/api/notifications/channels", { provider: current(), label: document.getElementById("channelLabel").value, config: config() });
        if (r.ok) { window.location.reload(); return; }
        button.disabled = false; say(result, r.message, false);
    });

    /* ---------- what to tell me about, and quiet hours ---------- */
    root.querySelectorAll("[data-rule]").forEach((box) => {
        box.addEventListener("change", async () => {
            const out = document.getElementById("ruleResult");
            const got = await post("/api/notifications/rules", { event_type: box.dataset.rule, enabled: box.checked });
            if (!got.ok) box.checked = !box.checked;
            say(out, got.message, got.ok);
        });
    });
    const zone = document.getElementById("quietZone");
    if (zone && zone.value === "UTC" && zone.dataset.saved === "0") {
        try { zone.value = Intl.DateTimeFormat().resolvedOptions().timeZone || "UTC"; } catch (e) { /* keep UTC */ }
    }
    const quietSave = document.getElementById("quietSave");
    if (quietSave) {
        quietSave.addEventListener("click", async () => {
            const got = await post("/api/notifications/quiet", {
                quiet_on: document.getElementById("quietOn").checked,
                quiet_start: document.getElementById("quietStart").value,
                quiet_end: document.getElementById("quietEnd").value,
                timezone: zone.value.trim(),
                batch_minutes: parseInt(document.getElementById("batchMinutes").value, 10) || 0,
            });
            say(document.getElementById("quietResult"), got.message, got.ok);
        });
    }
})();
