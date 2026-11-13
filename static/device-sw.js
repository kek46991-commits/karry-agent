// Installable shell for /monitor and /companion. API responses are never cached.
const CACHE = 'karry-device-v1';
self.addEventListener('install', (event) => {
  event.waitUntil(caches.open(CACHE).then((cache) => cache.addAll(['/monitor', '/companion'].filter((p) => self.registration.scope.endsWith(p)))));
  self.skipWaiting();
});
self.addEventListener('activate', (event) => event.waitUntil(self.clients.claim()));
self.addEventListener('fetch', (event) => {
  if (event.request.method !== 'GET' || event.request.mode !== 'navigate') return;
  event.respondWith(fetch(event.request).catch(() => caches.match(new URL(event.request.url).pathname)
    .then((hit) => hit || new Response('オフラインです。電波の届く場所で開き直してください。', {headers: {'Content-Type': 'text/plain; charset=utf-8'}}))));
});
