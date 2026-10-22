// BSC Internal Portal — service worker.
// Strategy: API and action/redirect routes are ALWAYS live (never cached, no stale data).
// Everything else is network-first (so deploys show immediately) with a cache fallback
// for offline, plus an offline page for navigations. Bump VERSION to force a refresh.
const VERSION = 'bsc-portal-v67';
const SHELL = [
  '/', '/index.html', '/home.html', '/app.html', '/outpass.html',
  '/expense.html', '/admin.html', '/labour.html', '/final.html', '/send-accounts.html', '/accounts.html',
  '/css/theme.css?v=5', '/css/console.css', '/js/common.js', '/offline.html',
  '/icon-192.png', '/icon-512.png', '/favicon.ico'
];
// Live routes that must never be served from cache.
const BYPASS = ['/api/', '/sso', '/rc/', '/rr/', '/rd/', '/t/', '/oga/', '/ogr/'];

self.addEventListener('install', (e) => {
  self.skipWaiting();
  e.waitUntil(caches.open(VERSION).then((c) => c.addAll(SHELL).catch(() => {})));
});

self.addEventListener('activate', (e) => {
  e.waitUntil(
    caches.keys()
      .then((keys) => Promise.all(keys.filter((k) => k !== VERSION).map((k) => caches.delete(k))))
      .then(() => self.clients.claim())
  );
});

self.addEventListener('fetch', (e) => {
  const req = e.request;
  if (req.method !== 'GET') return;                       // never touch POST/PUT/DELETE
  const url = new URL(req.url);
  if (url.origin !== location.origin) return;             // skip cross-origin (CDN fonts etc.)
  if (BYPASS.some((p) => url.pathname.startsWith(p))) return; // always live

  e.respondWith(
    fetch(req)
      .then((res) => {
        if (res && res.ok && res.type === 'basic') {
          const copy = res.clone();
          caches.open(VERSION).then((c) => c.put(req, copy)).catch(() => {});
        }
        return res;
      })
      .catch(() =>
        caches.match(req).then((hit) => hit || (req.mode === 'navigate' ? caches.match('/offline.html') : Response.error()))
      )
  );
});
