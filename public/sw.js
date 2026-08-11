const CACHE = 'qms-static-v2';
const ASSETS = ['/qms-icon-192.png','/qms-icon-512.png','/qms-icon-180.png','/favicon.ico','/manifest.json','/qms-splash-bsc.png'];
self.addEventListener('install', e => { e.waitUntil(caches.open(CACHE).then(c => c.addAll(ASSETS)).catch(()=>{})); self.skipWaiting(); });
self.addEventListener('activate', e => { e.waitUntil((async () => {
  const ks = await caches.keys(); await Promise.all(ks.filter(k => k !== CACHE).map(k => caches.delete(k))); await self.clients.claim();
})()); });
// Only static icons are served from cache; ALL app pages & API/data go to network (never stale)
self.addEventListener('fetch', e => {
  const u = new URL(e.request.url);
  if (e.request.method === 'GET' && ASSETS.includes(u.pathname)) {
    e.respondWith(caches.match(e.request).then(r => r || fetch(e.request)));
  }
});
