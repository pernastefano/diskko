/* DISKKO mobile service worker (scope /m/).
 *
 * What it keeps: the offline page, the icons and the scripts and styles the pages load
 * (served stale while they are refreshed). What it never keeps: the pages and their fragments
 * (they are the user's library and need the session), audio, covers, and anything that is
 * not a GET. Offline, a page that cannot be reached shows the offline page. */
const VERSION = 'diskko-m-1';
const SHELL = ['/m/offline', '/static/icons/icon-192.png', '/static/theme.css'];

self.addEventListener('install', (event) => {
  event.waitUntil(caches.open(VERSION).then((cache) => cache.addAll(SHELL)).then(() => self.skipWaiting()));
});

self.addEventListener('activate', (event) => {
  event.waitUntil(
    caches.keys()
      .then((keys) => Promise.all(keys.filter((k) => k !== VERSION).map((k) => caches.delete(k))))
      .then(() => self.clients.claim())
  );
});

const ASSET_HOSTS = ['cdn.tailwindcss.com', 'unpkg.com', 'cdn.jsdelivr.net'];

function isAsset(url) {
  if (url.origin === self.location.origin) return url.pathname.startsWith('/static/');
  return ASSET_HOSTS.includes(url.hostname);
}

self.addEventListener('fetch', (event) => {
  const request = event.request;
  if (request.method !== 'GET') return;
  const url = new URL(request.url);

  if (request.mode === 'navigate') {
    event.respondWith(
      fetch(request).catch(() => caches.match('/m/offline'))
    );
    return;
  }
  if (isAsset(url)) {
    event.respondWith(
      caches.open(VERSION).then((cache) =>
        cache.match(request).then((cached) => {
          const fresh = fetch(request).then((response) => {
            if (response && (response.ok || response.type === 'opaque')) cache.put(request, response.clone());
            return response;
          }).catch(() => cached);
          return cached || fresh;
        })
      )
    );
  }
});
