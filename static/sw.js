const CACHE_NAME = 'synchub-v1';
const STATIC_ASSETS = [
    '/static/css/synchub.css',
    '/static/js/index.js',
    '/static/js/app.js',
    '/static/placeholder.jpg',
    '/static/manifest.json'
];

self.addEventListener('install', (e) => {
    e.waitUntil(
        caches.open(CACHE_NAME).then((cache) => cache.addAll(STATIC_ASSETS))
    );
    self.skipWaiting();
});

self.addEventListener('activate', (e) => {
    e.waitUntil(
        caches.keys().then((keys) => {
            return Promise.all(
                keys.map((key) => {
                    if (key !== CACHE_NAME) return caches.delete(key);
                })
            );
        })
    );
    self.clients.claim();
});

// Cache posters aggressively, fall back to network for everything else
self.addEventListener('fetch', (e) => {
    const url = new URL(e.request.url);
    
    if (url.pathname.startsWith('/poster/')) {
        e.respondWith(
            caches.match(e.request).then((cached) => {
                return cached || fetch(e.request).then((response) => {
                    const clone = response.clone();
                    caches.open(CACHE_NAME).then((cache) => cache.put(e.request, clone));
                    return response;
                }).catch(() => caches.match('/static/placeholder.jpg')); // Offline fallback
            })
        );
    } else if (STATIC_ASSETS.some(asset => url.pathname.endsWith(asset))) {
        e.respondWith(
            caches.match(e.request).then(cached => cached || fetch(e.request))
        );
    }
});