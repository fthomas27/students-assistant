const VERSION = 'jarvis-v2';
const STATIC_CACHE = `${VERSION}-static`;

self.addEventListener('install', (event) => {
  self.skipWaiting();
});

self.addEventListener('activate', (event) => {
  event.waitUntil((async () => {
    const keys = await caches.keys();
    await Promise.all(keys.filter(k => !k.startsWith(VERSION)).map(k => caches.delete(k)));
    await self.clients.claim();
  })());
});

self.addEventListener('fetch', (event) => {
  const req = event.request;
  if (req.method !== 'GET') return;

  const url = new URL(req.url);
  if (url.origin !== self.location.origin) return;

  // API requests are live data: never answer them from a cache. A replayed
  // API response looks exactly like fresh data (frozen heart rate, stale
  // tasks) with no way for the page to tell. Let failures surface — the
  // page marks the affected widget stale and retries on its own.
  if (url.pathname.startsWith('/api/')) return;

  // Network-first for HTML so deploys take effect, with an offline fallback.
  if (req.destination === 'document') {
    event.respondWith((async () => {
      try {
        const fresh = await fetch(req);
        if (fresh && fresh.status === 200) {
          const cache = await caches.open(STATIC_CACHE);
          cache.put(req, fresh.clone());
        }
        return fresh;
      } catch (err) {
        const cached = await caches.match(req);
        if (cached) return cached;
        throw err;
      }
    })());
    return;
  }

  // Cache-first for everything else (fonts, images, etc.).
  event.respondWith((async () => {
    const cached = await caches.match(req);
    if (cached) return cached;
    const fresh = await fetch(req);
    if (fresh && fresh.status === 200) {
      const cache = await caches.open(STATIC_CACHE);
      cache.put(req, fresh.clone());
    }
    return fresh;
  })());
});
