// Ticket Takeaway service worker.
// Strategy: network-first for navigations (HTML), fall back to cached shell when offline.
// API requests (/api/*, /{pid}/api/*) and uploads are NEVER cached — always pass through.
// Static assets (/icon*, /manifest*) get cached on first hit.

const CACHE = 'tt-shell-v1';
const SHELL_FALLBACK = '/';

self.addEventListener('install', (event) => {
  event.waitUntil(
    caches.open(CACHE).then((c) => c.add(SHELL_FALLBACK)).catch(() => {})
  );
  self.skipWaiting();
});

self.addEventListener('activate', (event) => {
  event.waitUntil((async () => {
    const keys = await caches.keys();
    await Promise.all(keys.filter((k) => k !== CACHE).map((k) => caches.delete(k)));
    await self.clients.claim();
  })());
});

function isApiRequest(url) {
  // Matches /api/... and /{anything}/api/...
  return /(^|\/)api\//.test(url.pathname);
}

function isStaticAsset(url) {
  return /^\/(icon[\w.-]*\.(png|svg)|manifest\.webmanifest)$/.test(url.pathname);
}

self.addEventListener('fetch', (event) => {
  const req = event.request;
  if (req.method !== 'GET') return;
  const url = new URL(req.url);
  if (url.origin !== self.location.origin) return;
  if (isApiRequest(url)) return;  // bypass — always live

  if (isStaticAsset(url)) {
    event.respondWith((async () => {
      const cached = await caches.match(req);
      if (cached) return cached;
      try {
        const fresh = await fetch(req);
        if (fresh && fresh.ok) {
          const cache = await caches.open(CACHE);
          cache.put(req, fresh.clone());
        }
        return fresh;
      } catch (_) {
        return cached || Response.error();
      }
    })());
    return;
  }

  // Navigations + everything else: network-first, fall back to shell.
  event.respondWith((async () => {
    try {
      const fresh = await fetch(req);
      if (fresh && fresh.ok && req.mode === 'navigate') {
        const cache = await caches.open(CACHE);
        cache.put(SHELL_FALLBACK, fresh.clone());
      }
      return fresh;
    } catch (_) {
      const cached = await caches.match(req);
      if (cached) return cached;
      const shell = await caches.match(SHELL_FALLBACK);
      if (shell) return shell;
      return Response.error();
    }
  })());
});
