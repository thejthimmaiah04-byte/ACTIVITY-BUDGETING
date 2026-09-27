const CACHE = 'liana-v1';
const ASSETS = [
  '/ACTIVITY-BUDGETING/',
  '/ACTIVITY-BUDGETING/index.html',
  '/ACTIVITY-BUDGETING/manifest.json',
  '/ACTIVITY-BUDGETING/icon-192.png',
  '/ACTIVITY-BUDGETING/icon-512.png'
];

self.addEventListener('install', e => {
  e.waitUntil(caches.open(CACHE).then(c => c.addAll(ASSETS)));
  self.skipWaiting();
});

self.addEventListener('activate', e => {
  e.waitUntil(caches.keys().then(keys =>
    Promise.all(keys.filter(k => k !== CACHE).map(k => caches.delete(k)))
  ));
  self.clients.claim();
});

self.addEventListener('fetch', e => {
  // Always fetch config.json fresh so URL updates propagate
  if (e.request.url.includes('config.json') || e.request.url.includes('trycloudflare') || e.request.url.includes('tailscale')) {
    e.respondWith(fetch(e.request));
    return;
  }
  e.respondWith(
    caches.match(e.request).then(r => r || fetch(e.request))
  );
});
