// Installable shell for the monitor and worker apps. API responses are never cached.
const CACHE = 'karry-device-v3';
self.addEventListener('install', (event) => {
  // Offline copy is best-effort: a failed fetch must not keep the worker from activating.
  const shell = new URL(self.registration.scope).pathname;
  event.waitUntil(caches.open(CACHE).then((cache) => cache.add(shell)).catch(() => {}));
  self.skipWaiting();
});
self.addEventListener('activate', (event) => event.waitUntil(self.clients.claim()));
self.addEventListener('fetch', (event) => {
  if (event.request.method !== 'GET' || event.request.mode !== 'navigate') return;
  event.respondWith(fetch(event.request).catch(() => caches.match(new URL(event.request.url).pathname)
    .then((hit) => hit || new Response('オフラインです。電波の届く場所で開き直してください。', {headers: {'Content-Type': 'text/plain; charset=utf-8'}}))));
});
