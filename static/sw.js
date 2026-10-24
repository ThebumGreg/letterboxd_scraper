// The old app registered a caching service worker. This one replaces it so browsers that still have the old
// one stop serving stale pages, scripts and posters, then removes itself. Nothing in the app registers it now.
self.addEventListener('install', () => self.skipWaiting());

self.addEventListener('activate', (event) => {
    event.waitUntil((async () => {
        for (const key of await caches.keys()) await caches.delete(key);
        await self.registration.unregister();
        for (const client of await self.clients.matchAll()) client.navigate(client.url);
    })());
});
