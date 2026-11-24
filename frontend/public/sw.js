// Local AI Workbench - Progressive Web App Service Worker
const CACHE_NAME = 'ai-workbench-shell-v1';
const SHELL_ASSETS = [
  '/',
  '/index.html',
  '/manifest.webmanifest',
  '/icons/icon.svg',
  '/icons/icon-192.png',
  '/icons/icon-512.png',
  '/icons/icon-maskable-512.png'
];

// 1. Install Event: Pre-cache core shell resources
self.addEventListener('install', (event) => {
  event.waitUntil(
    caches.open(CACHE_NAME).then((cache) => {
      return cache.addAll(SHELL_ASSETS).catch((err) => {
        console.warn('[PWA SW] Pre-caching partial shell failure:', err);
      });
    }).then(() => self.skipWaiting())
  );
});

// 2. Activate Event: Clean up legacy caches
self.addEventListener('activate', (event) => {
  event.waitUntil(
    caches.keys().then((keys) => {
      return Promise.all(
        keys.map((key) => {
          if (key !== CACHE_NAME) {
            return caches.delete(key);
          }
        })
      );
    }).then(() => self.clients.claim())
  );
});

// 3. Fetch Event: Network-first for APIs/dynamic data; Stale-While-Revalidate for static UI assets
self.addEventListener('fetch', (event) => {
  const url = new URL(event.request.url);

  // Always bypass cache for REST API endpoints, streaming SSE, and health checks
  if (
    url.pathname.startsWith('/api/') ||
    url.pathname.startsWith('/static/generated/') ||
    url.pathname.includes('/events') ||
    event.request.method !== 'GET'
  ) {
    return;
  }

  // Handle navigation requests (SPA HTML entry)
  if (event.request.mode === 'navigate') {
    event.respondWith(
      fetch(event.request).catch(async () => {
        const cache = await caches.open(CACHE_NAME);
        const cachedIndex = await cache.match('/index.html');
        return cachedIndex || new Response('Offline: Local AI Workbench server is currently unavailable.', {
          status: 503,
          headers: { 'Content-Type': 'text/plain' }
        });
      })
    );
    return;
  }

  // Stale-While-Revalidate for bundled assets, icons, and fonts
  event.respondWith(
    caches.match(event.request).then((cachedResponse) => {
      const fetchPromise = fetch(event.request).then((networkResponse) => {
        if (networkResponse && networkResponse.status === 200) {
          const responseToCache = networkResponse.clone();
          caches.open(CACHE_NAME).then((cache) => {
            cache.put(event.request, responseToCache);
          });
        }
        return networkResponse;
      }).catch(() => cachedResponse);

      return cachedResponse || fetchPromise;
    })
  );
});
