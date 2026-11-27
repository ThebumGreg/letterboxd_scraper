/* "This phone or browser": turns on Web Push for the device in hand. Runs on a person's own page (People). Everything here is served by SyncHub itself. */
(() => {
    "use strict";
    const panel = document.getElementById("pushPanel");
    if (!panel) return;
    const button = document.getElementById("pushEnable");
    const out = document.getElementById("pushResult");
    const csrfToken = () => document.querySelector('meta[name="csrf-token"]')?.content || "";
    const say = (text, good) => { out.textContent = text; out.className = "small ms-2 " + (good ? "text-success" : "text-danger"); };

    const isIOS = /iPad|iPhone|iPod/.test(navigator.userAgent) || (navigator.platform === "MacIntel" && navigator.maxTouchPoints > 1);
    const standalone = window.navigator.standalone === true || window.matchMedia("(display-mode: standalone)").matches;
    if (isIOS) {
        document.getElementById("pushIos").hidden = false;
        document.getElementById("pushOther").hidden = true;
    }

    const supported = "serviceWorker" in navigator && "PushManager" in window && "Notification" in window;
    if (!supported) {
        button.disabled = true;
        say(isIOS && !standalone ? "Add SyncHub to the Home Screen first (see above), then open it from there." : "This browser cannot receive push notifications.", false);
        return;
    }
    if (isIOS && !standalone) say("Open SyncHub from its Home Screen icon to turn this on.", false);
    if (Notification.permission === "denied") say("Notifications are blocked for SyncHub on this device. Allow them in the device's settings, then try again.", false);

    const toKey = (b64) => {
        const pad = "=".repeat((4 - (b64.length % 4)) % 4);
        const raw = atob((b64 + pad).replace(/-/g, "+").replace(/_/g, "/"));
        return Uint8Array.from(raw, (c) => c.charCodeAt(0));
    };
    async function call(url, body) {
        const res = await fetch(url, {
            method: body ? "POST" : "GET",
            headers: { "Content-Type": "application/json", Accept: "application/json", "X-CSRFToken": csrfToken() },
            body: body ? JSON.stringify(body) : undefined,
        });
        let data = {};
        try { data = await res.json(); } catch (e) { /* the message below says so */ }
        if (!res.ok || data.ok === false) throw new Error(data.message || `That did not work (HTTP ${res.status}).`);
        return data;
    }

    button.addEventListener("click", async () => {
        button.disabled = true;
        say("Working…", true);
        try {
            const { key } = await call("/api/notifications/push/key");
            const registration = await navigator.serviceWorker.register("/sw.js", { scope: "/" });
            await navigator.serviceWorker.ready;
            // iOS asks only here, because a person tapped the button.
            if ((await Notification.requestPermission()) !== "granted") throw new Error("Notifications were not allowed on this device.");
            const subscription = (await registration.pushManager.getSubscription())
                || (await registration.pushManager.subscribe({ userVisibleOnly: true, applicationServerKey: toKey(key) }));
            const label = isIOS ? "My iPhone or iPad" : "This browser";
            const saved = await call("/api/notifications/push/subscribe", { subscription: subscription.toJSON(), label });
            say(saved.message + " Reloading…", true);
            setTimeout(() => location.reload(), 900);
        } catch (e) {
            say(e.message || "That did not work.", false);
            button.disabled = false;
        }
    });
})();
