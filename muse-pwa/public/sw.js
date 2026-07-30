// Caching strategy, chosen per request type rather than one rule for everything.
//
// The previous version was cache-first for every GET, which meant the cached
// index.html was served before the network was ever consulted. Since index.html
// names the content-hashed bundle, a device could be pinned to an old build
// indefinitely: the new bundle existed on the server, but nothing ever asked for
// it. That happened in the field — a fix was deployed and the device kept
// running the old code, with a reload making no difference.
//
// The rules now:
//   - navigations and the app shell: network-first, cache as offline fallback
//   - /assets/*: cache-first, safe because the filenames are content-hashed
//   - APIs, websockets, non-GET, cross-origin: never intercepted
//
// Bumping CACHE_NAME purges everything from the old scheme on activation.
const CACHE_NAME = 'muse-cache-v2';

// Requests that must never be served from cache. Telemetry and session control
// are live state; a cached answer is always the wrong answer.
const NEVER_CACHE = ['/healthz', '/api/'];

self.addEventListener('install', (event) => {
  // Take over from the previous worker immediately rather than waiting for
  // every tab to close, so a fix reaches devices on the next navigation.
  event.waitUntil(
    caches.open(CACHE_NAME).then((cache) => cache.add('/').catch(() => {}))
  );
  self.skipWaiting();
});

self.addEventListener('activate', (event) => {
  event.waitUntil(
    caches
      .keys()
      .then((names) =>
        Promise.all(names.filter((n) => n !== CACHE_NAME).map((n) => caches.delete(n)))
      )
      .then(() => self.clients.claim())
  );
});

const putInCache = (request, response) =>
  caches
    .open(CACHE_NAME)
    .then((cache) => cache.put(request, response))
    .catch(() => {});

/** Always go to the network; fall back to cache only when truly offline. */
const networkFirst = (event) =>
  fetch(event.request)
    .then((response) => {
      if (response && response.status === 200 && response.type === 'basic') {
        putInCache(event.request, response.clone());
      }
      return response;
    })
    .catch(() =>
      caches.match(event.request).then(
        (cached) =>
          cached ||
          // For a navigation, the app shell is a better offline answer than an
          // error page, and the app itself reports the lost connection.
          caches.match('/') ||
          new Response('Offline and nothing cached yet.', {
            status: 503,
            headers: { 'Content-Type': 'text/plain' },
          })
      )
    );

/** Content-hashed filenames never change meaning, so cache wins. */
const cacheFirst = (event) =>
  caches.match(event.request).then((cached) => {
    if (cached) return cached;
    return fetch(event.request).then((response) => {
      if (response && response.status === 200 && response.type === 'basic') {
        putInCache(event.request, response.clone());
      }
      return response;
    });
  });

self.addEventListener('fetch', (event) => {
  const { request } = event;

  // Let the browser handle anything we have no business caching. Note POST in
  // particular: cache.put() throws on non-GET, which the old version did
  // silently on every session start/stop.
  if (request.method !== 'GET') return;

  const url = new URL(request.url);
  if (url.origin !== self.location.origin) return;
  if (NEVER_CACHE.some((prefix) => url.pathname.startsWith(prefix))) return;

  if (request.mode === 'navigate' || url.pathname === '/' || url.pathname === '/index.html') {
    event.respondWith(networkFirst(event));
    return;
  }

  if (url.pathname.startsWith('/assets/')) {
    event.respondWith(cacheFirst(event));
    return;
  }

  event.respondWith(networkFirst(event));
});
