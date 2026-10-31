(() => {
    "use strict";

    const csrfToken = () => document.querySelector('meta[name="csrf-token"]')?.content || "";
    const csrfHeaders = () => { const t = csrfToken(); return t ? { "X-CSRFToken": t } : {}; };

    /* ---------- confirm prompts + double-submit guard (all forms) ---------- */
    document.addEventListener("submit", (e) => {
        const f = e.target;
        if (!(f instanceof HTMLFormElement)) return;
        if (f.dataset.confirm && !window.confirm(f.dataset.confirm)) { e.preventDefault(); return; }
        if (e.defaultPrevented) return;   // a form-level validator already blocked it
        f.dataset.submitting = "1";
        setTimeout(() => f.querySelectorAll('button[type="submit"]').forEach((b) => { b.disabled = true; }), 0);
    });

    // Restore buttons if the page comes back from the back/forward cache.
    window.addEventListener("pageshow", (e) => {
        if (!e.persisted) return;
        document.querySelectorAll("form[data-submitting]").forEach((f) => {
            delete f.dataset.submitting;
            f.querySelectorAll('button[type="submit"]').forEach((b) => { b.disabled = false; });
        });
    });

    /* ---------- missing posters fall back to a placeholder on every page ---------- */
    const NO_POSTER = "data:image/svg+xml;utf8," + encodeURIComponent(
        '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 90 130"><rect width="90" height="130" fill="#2c3440"/>' +
        '<text x="45" y="68" font-size="11" fill="#8899aa" text-anchor="middle" font-family="sans-serif">No poster</text></svg>');
    document.addEventListener("error", (e) => {
        const t = e.target;
        if (t && t.tagName === "IMG" && t.classList.contains("poster") && !t.dataset.fallback) {
            t.dataset.fallback = "1";
            t.src = NO_POSTER;
        }
    }, true);

    /* ---------- live task status (Tools page) ---------- */
    if (document.querySelector("[data-task-badge]")) {
        const BADGE = { in_progress: "bg-primary", pending: "bg-warning text-dark", completed: "bg-success", failed: "bg-danger" };
        const isActive = (t) => ["in_progress", "pending"].includes(String(t?.status || "").toLowerCase());

        const applyTasks = (tasks) => {
            const allActive = isActive(tasks.all);
            let anyActive = false;
            for (const [key, t] of Object.entries(tasks)) {
                if (!t || typeof t !== "object") continue;
                const sv = String(t.status || "idle").toLowerCase();
                if (isActive(t)) anyActive = true;
                document.querySelectorAll(`[data-task-badge="${key}"]`).forEach((b) => {
                    b.className = "badge " + (BADGE[sv] || "bg-secondary");
                    b.textContent = sv.toUpperCase();
                });
                document.querySelectorAll(`[data-task-msg="${key}"]`).forEach((m) => { m.textContent = t.message || "No active logs"; });
                document.querySelectorAll(`[data-task-last="${key}"]`).forEach((m) => { m.textContent = t.last_run || "Never"; });
            }
            // Disable a task's button while it runs, and everything else while the full pipeline runs.
            document.querySelectorAll("[data-task-btn]").forEach((b) => {
                const key = b.dataset.taskBtn;
                b.disabled = isActive(tasks[key]) || (allActive && key !== "all");
            });
            return anyActive;
        };

        let timer = null;
        const poll = async () => {
            clearTimeout(timer);
            if (document.hidden) return;
            let delay = 15000;
            try {
                const res = await fetch("/api/tasks", { headers: { Accept: "application/json" } });
                if ([401, 403, 404].includes(res.status)) return;   // endpoint not available: stop quietly
                if (!res.ok) throw new Error("HTTP " + res.status);
                const data = await res.json();
                delay = applyTasks(data.tasks || data) ? 3000 : 15000;
            } catch (_) { delay = 15000; }
            timer = setTimeout(poll, delay);
        };
        document.addEventListener("visibilitychange", () => { if (!document.hidden) poll(); });
        poll();
    }

    /* ---------- Fix & Match helpers (Tools page) ---------- */
    const fixForm = document.getElementById("fixMatchForm");
    if (fixForm) {
        const ids = ["tmdb_id", "imdb_id", "tvdb_id"].map((n) => fixForm.elements[n]);
        const clear = () => ids[0].setCustomValidity("");
        ids.forEach((i) => i.addEventListener("input", clear));

        fixForm.addEventListener("submit", (e) => {
            if (ids.every((i) => !i.value.trim())) {
                ids[0].setCustomValidity("Enter at least one ID (TMDB, IMDb or TVDB).");
                ids[0].reportValidity();
                e.preventDefault();
            }
        });

        document.querySelectorAll("[data-fill-slug]").forEach((btn) => {
            btn.addEventListener("click", () => {
                fixForm.elements.movie_slug.value = btn.dataset.fillSlug;
                const target = btn.dataset.fillType === "tv" ? fixForm.elements.tvdb_id : fixForm.elements.tmdb_id;
                fixForm.scrollIntoView({ behavior: "smooth", block: "center" });
                target.focus({ preventScroll: true });
            });
        });
    }

    /* ---------- Settings page (one form per tab, each saved on its own) ---------- */
    const settingsForms = Array.from(document.querySelectorAll("form.settings-form"));
    if (settingsForms.length) {
        // Username boxes: show one name per line, store as a JSON list (the format the backend already uses).
        const parseList = (text) => {
            const t = text.trim();
            if (t.startsWith("[")) { try { const v = JSON.parse(t); if (Array.isArray(v)) return v.map(String); } catch (_) { /* fall through */ } }
            return t.split(/[\n,]+/).map((s) => s.trim()).filter(Boolean);
        };

        // Max delay must not be below min delay.
        [["request_delay_min", "request_delay_max"], ["user_delay_min", "user_delay_max"]].forEach(([minId, maxId]) => {
            const dMin = document.getElementById(minId);
            const dMax = document.getElementById(maxId);
            if (!dMin || !dMax) return;
            const checkDelays = () => {
                const bad = dMin.value !== "" && dMax.value !== "" && parseFloat(dMax.value) < parseFloat(dMin.value);
                dMax.setCustomValidity(bad ? "Max delay must be greater than or equal to min delay." : "");
            };
            dMin.addEventListener("input", checkDelays);
            dMax.addEventListener("input", checkDelays);
            checkDelays();
        });

        // Dim sections whose feature is switched off (fields stay editable and are still submitted).
        const applyDims = (cb) => {
            const target = document.querySelector(cb.dataset.dims);
            if (!target) return;
            const dim = cb.dataset.dimsWhen === "checked" ? cb.checked : !cb.checked;
            target.classList.toggle("is-dimmed", dim);
        };
        document.querySelectorAll("[data-dims]").forEach((cb) => { applyDims(cb); cb.addEventListener("change", () => applyDims(cb)); });

        // Unsaved-changes indicator (per tab) + leave warning.
        const dirty = new Set();
        let saving = false;
        settingsForms.forEach((form) => {
            const badge = form.querySelector(".dirty-badge");
            const markDirty = () => { dirty.add(form); badge?.classList.remove("d-none"); };
            form.addEventListener("input", markDirty);
            form.addEventListener("change", markDirty);
            const boxes = Array.from(form.querySelectorAll("textarea[data-usernames]"));
            boxes.forEach((box) => {
                const t = box.value.trim();
                if (t.startsWith("[")) {
                    try { const v = JSON.parse(t); if (Array.isArray(v)) box.value = v.join("\n"); } catch (_) { /* leave raw text */ }
                }
                box.addEventListener("input", () => box.setCustomValidity(""));
            });
            form.addEventListener("submit", (e) => {
                // Validate usernames, then convert each box back to a JSON list.
                for (const box of boxes) {
                    const names = parseList(box.value);
                    const bad = names.find((n) => !/^[\w.\-]+$/.test(n));
                    if (bad) {
                        box.setCustomValidity(`"${bad}" is not a valid username (letters, numbers, _ . - only).`);
                        box.reportValidity();
                        e.preventDefault();
                        return;
                    }
                    box.value = JSON.stringify([...new Set(names)]);
                }
                if (!e.defaultPrevented) saving = true;
            });
        });
        window.addEventListener("beforeunload", (e) => { if (dirty.size && !saving) { e.preventDefault(); e.returnValue = ""; } });

        // "Set up in Radarr/Sonarr" buttons: ask the service to create the SyncHub webhook. Blank secrets mean "use the saved value".
        document.querySelectorAll("[data-webhook-setup]").forEach((btn) => {
            btn.addEventListener("click", async () => {
                const service = btn.dataset.webhookSetup;
                const out = document.querySelector(`[data-webhook-result="${service}"]`);
                const fields = {};
                btn.dataset.webhookFields.split(",").forEach((n) => { const i = document.getElementById(n); if (i) fields[n] = i.type === "checkbox" ? (i.checked ? "1" : "0") : i.value; });
                const show = (text, cls) => { out.className = "small d-block " + cls; out.textContent = text; };
                btn.disabled = true;
                show("Working...", "text-body-secondary");
                try {
                    const res = await fetch("/api/webhook-setup/" + encodeURIComponent(service), {
                        method: "POST",
                        headers: { "Content-Type": "application/json", Accept: "application/json", ...csrfHeaders() },
                        body: JSON.stringify({ fields }),
                    });
                    const data = await res.json().catch(() => ({}));
                    show(data.message || (res.ok ? "Done." : `Failed (HTTP ${res.status})`), res.ok && data.ok !== false ? "text-success" : "text-danger");
                } catch (err) {
                    show(err.message, "text-danger");
                } finally {
                    btn.disabled = false;
                }
            });
        });

        // Keep the webhook addresses shown in step with the SyncHub address field.
        const addressField = document.getElementById("synchub_url");
        if (addressField) {
            const base = () => (addressField.value.trim() || addressField.placeholder).replace(/\/+$/, "");
            addressField.addEventListener("input", () => {
                document.querySelectorAll("[data-webhook-url]").forEach((el) => { el.textContent = `${base()}/api/webhook/${el.dataset.webhookUrl}`; });
            });
        }

        // "Test connection" buttons. Blank secret fields mean "use the saved value" on the server.
        document.querySelectorAll("[data-test-service]").forEach((btn) => {
            btn.addEventListener("click", async () => {
                const service = btn.dataset.testService;
                const out = document.querySelector(`[data-test-result="${service}"]`);
                const fields = {};
                btn.dataset.testFields.split(",").forEach((n) => { const i = document.getElementById(n); if (i) fields[n] = i.type === "checkbox" ? (i.checked ? "1" : "0") : i.value; });
                const show = (text, cls) => { out.className = "small ms-2 " + cls; out.textContent = text; };

                btn.disabled = true;
                show("Testing...", "text-body-secondary");
                try {
                    const res = await fetch("/api/test-connection/" + encodeURIComponent(service), {
                        method: "POST",
                        headers: { "Content-Type": "application/json", Accept: "application/json", ...csrfHeaders() },
                        body: JSON.stringify({ fields }),
                    });
                    if (res.status === 404) throw new Error("The server has no test endpoint for this service.");
                    const data = await res.json().catch(() => ({}));
                    const ok = res.ok && data.ok !== false;
                    show(data.message || (ok ? "Connection OK" : `Failed (HTTP ${res.status})`), ok ? "text-success" : "text-danger");
                } catch (err) {
                    show(err.message, "text-danger");
                } finally {
                    btn.disabled = false;
                }
            });
        });
    }
})();
