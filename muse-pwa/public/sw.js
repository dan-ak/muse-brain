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

// How long the network gets before the cache answers instead. Long enough that
// a healthy LAN always wins, short enough that a black-holed link does not
// leave the user looking at nothing.
const NETWORK_TIMEOUT_MS = 1500;

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

const offlineResponse = () =>
  new Response('Offline and nothing cached yet.', {
    status: 503,
    headers: { 'Content-Type': 'text/plain' },
  });

/** Prefer the network, but never let a hanging request hold up the page.
 *
 * A phone associated to the AP whose packets are being black-holed — marginal
 * range, or a captive portal — takes tens of seconds to fail by TCP timeout.
 * Plain network-first meant staring at a blank screen for that whole period on
 * every launch, so the cache wins if the network has not answered in time and
 * the request is revalidated in the background regardless.
 *
 * `isNavigation` decides the fallback: the app shell is the right answer for a
 * navigation and the wrong answer for a subresource, where handing back
 * index.html produces an unparseable manifest and undecodable images instead of
 * a clean failure.
 */
const networkFirst = (event, isNavigation) => {
  const fromNetwork = fetch(event.request).then((response) => {
    if (response && response.status === 200 && response.type === 'basic') {
      putInCache(event.request, response.clone());
    }
    return response;
  });

  const fallback = async () => {
    const cached = await caches.match(event.request);
    if (cached) return cached;
    if (isNavigation) {
      const shell = await caches.match('/');
      if (shell) return shell;
    }
    return offlineResponse();
  };

  // Every branch resolves to a Response: awaiting caches.match() rather than
  // testing the Promise for truthiness is what keeps this from resolving to
  // undefined, which surfaced as a worker TypeError and a browser error page.
  return (async () => {
    const raced = await Promise.race([
      fromNetwork.catch(() => null),
      new Promise((resolve) => setTimeout(() => resolve(undefined), NETWORK_TIMEOUT_MS)),
    ]);
    if (raced) return raced;

    const cached = await fallback();
    // If the cache had nothing, the network is still the only hope — wait it out
    // rather than reporting offline while a slow request may yet succeed.
    if (cached.status === 503) {
      const late = await fromNetwork.catch(() => null);
      if (late) return late;
    }
    return cached;
  })();
};

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

  const isNavigation =
    request.mode === 'navigate' || url.pathname === '/' || url.pathname === '/index.html';

  if (isNavigation) {
    event.respondWith(networkFirst(event, true));
    return;
  }

  if (url.pathname.startsWith('/assets/')) {
    event.respondWith(cacheFirst(event));
    return;
  }

  // Subresources get the same freshness treatment but no shell fallback.
  event.respondWith(networkFirst(event, false));
});
