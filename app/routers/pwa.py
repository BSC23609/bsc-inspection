"""PWA plumbing — web app manifests, the service worker and an offline page,
all served from the site root so the service worker can control the whole app.

Two manifests from one origin give two installable home-screen apps:
  • /manifest.webmanifest          → staff QMS (scope "/")
  • /manifest.customer.webmanifest → customer complaint portal (scope "/customer")

The service worker is navigation-network-first (authenticated pages always load
fresh, with an offline fallback) and cache-first only for /static assets and
Google Fonts, so no signed-in HTML is ever cached."""
import json
from fastapi import APIRouter
from fastapi.responses import Response, HTMLResponse

router = APIRouter(tags=["pwa"])

_ICONS = [
    {"src": "/static/icon-192.png", "sizes": "192x192", "type": "image/png", "purpose": "any maskable"},
    {"src": "/static/icon-512.png", "sizes": "512x512", "type": "image/png", "purpose": "any maskable"},
]

_MANIFEST_STAFF = {
    "name": "Bharat Steel QMS",
    "short_name": "BSC QMS",
    "id": "/",
    "start_url": "/",
    "scope": "/",
    "display": "standalone",
    "orientation": "portrait-primary",
    "background_color": "#eef3f9",
    "theme_color": "#0d3a5c",
    "description": "Bharat Steel (Chennai) quality management — inspections, complaints and reports.",
    "icons": _ICONS,
}

_MANIFEST_CUSTOMER = {
    "name": "Bharat Steel — Quality Complaints",
    "short_name": "BSC Complaints",
    "id": "/customer",
    "start_url": "/customer",
    "scope": "/customer",
    "display": "standalone",
    "orientation": "portrait-primary",
    "background_color": "#eef3f9",
    "theme_color": "#0d3a5c",
    "description": "Raise and track quality complaints with Bharat Steel (Chennai).",
    "icons": _ICONS,
}

_MANIFEST_HEADERS = {"Cache-Control": "no-cache"}


@router.get("/manifest.webmanifest")
async def manifest_staff():
    return Response(json.dumps(_MANIFEST_STAFF), media_type="application/manifest+json",
                    headers=_MANIFEST_HEADERS)


@router.get("/manifest.customer.webmanifest")
async def manifest_customer():
    return Response(json.dumps(_MANIFEST_CUSTOMER), media_type="application/manifest+json",
                    headers=_MANIFEST_HEADERS)


_SW = r"""
const CACHE = 'bscqms-v2';
const SHELL = ['/offline', '/static/app.css', '/static/bsc-logo.png',
               '/static/icon-192.png', '/static/icon-512.png', '/static/apple-touch-180.png'];

self.addEventListener('install', function (e) {
  e.waitUntil(caches.open(CACHE).then(function (c) {
    return Promise.all(SHELL.map(function (u) { return c.add(u).catch(function () {}); }));
  }).then(function () { return self.skipWaiting(); }));
});

self.addEventListener('activate', function (e) {
  e.waitUntil(caches.keys().then(function (keys) {
    return Promise.all(keys.filter(function (k) { return k !== CACHE; })
                           .map(function (k) { return caches.delete(k); }));
  }).then(function () { return self.clients.claim(); }));
});

self.addEventListener('fetch', function (e) {
  var req = e.request;
  if (req.method !== 'GET') return;
  var url = new URL(req.url);

  // Page navigations: always try the network first (keeps authed pages fresh),
  // fall back to the cached offline page when there is no connection.
  if (req.mode === 'navigate') {
    e.respondWith(fetch(req).catch(function () { return caches.match('/offline'); }));
    return;
  }

  // Static assets: cache-first, then network (and cache what we fetch).
  if (url.origin === location.origin && url.pathname.indexOf('/static/') === 0) {
    e.respondWith(caches.match(req).then(function (hit) {
      return hit || fetch(req).then(function (res) {
        var copy = res.clone();
        caches.open(CACHE).then(function (c) { c.put(req, copy); });
        return res;
      });
    }));
    return;
  }

  // Google Fonts: stale-while-revalidate.
  if (url.origin === 'https://fonts.googleapis.com' || url.origin === 'https://fonts.gstatic.com') {
    e.respondWith(caches.match(req).then(function (hit) {
      var net = fetch(req).then(function (res) {
        var copy = res.clone();
        caches.open(CACHE).then(function (c) { c.put(req, copy); });
        return res;
      }).catch(function () { return hit; });
      return hit || net;
    }));
    return;
  }

  // Everything else (APIs, data, cross-origin): network only, never cached.
});
"""


@router.get("/sw.js")
async def service_worker():
    return Response(_SW, media_type="application/javascript",
                    headers={"Service-Worker-Allowed": "/", "Cache-Control": "no-cache"})


_OFFLINE = """<!doctype html><html lang="en"><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>Offline · Bharat Steel QMS</title>
<style>
:root{--blue:#0069aa;--navy:#0d3a5c;--ink:#0e1524;--muted:#5d6879;--bg:#eef3f9}
*{box-sizing:border-box}html,body{margin:0;height:100%}
body{background:var(--bg);color:var(--ink);font-family:"Inter",system-ui,-apple-system,Arial,sans-serif;
  display:flex;align-items:center;justify-content:center;padding:24px;text-align:center}
.card{max-width:360px}
.mark{width:72px;height:72px;border-radius:18px;background:#fff;border:1px solid #dbe4ef;
  display:flex;align-items:center;justify-content:center;margin:0 auto 18px;box-shadow:0 8px 24px rgba(8,36,56,.08)}
.mark img{width:46px;height:46px}
h1{font-size:19px;margin:0 0 8px}
p{color:var(--muted);font-size:14px;line-height:1.55;margin:0 0 20px}
button{border:none;background:var(--blue);color:#fff;font:inherit;font-weight:700;font-size:14px;
  padding:11px 22px;border-radius:11px;cursor:pointer}
button:hover{background:var(--navy)}
</style></head><body>
<div class="card">
  <div class="mark"><img src="/static/icon-192.png" alt="BSC"></div>
  <h1>You're offline</h1>
  <p>This page needs a connection. Check your network and try again — anything you've already loaded still works.</p>
  <button onclick="location.reload()">Try again</button>
</div></body></html>"""


@router.get("/offline", response_class=HTMLResponse)
async def offline_page():
    return HTMLResponse(_OFFLINE, headers={"Cache-Control": "no-cache"})
