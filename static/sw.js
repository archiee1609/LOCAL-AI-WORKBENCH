// Streamlit PWA Service Worker
const CACHE_NAME = 'ai-workbench-streamlit-v1';
const STATIC_ASSETS = [
  '/app/static/manifest.webmanifest',
  '/app/static/icons/icon.svg',
  '/app/static/icons/icon-192.png',
  '/app/static/icons/icon-512.png',
  '/app/static/icons/icon-maskable-512.png'
];

self.addEventListener('install', (event) => {
  event.waitUntil(
    caches.open(CACHE_NAME).then((cache) => {
      return cache.addAll(STATIC_ASSETS).catch((err) => {
        console.warn('[Streamlit PWA] Pre-cache partial warning:', err);
      });
    }).then(() => self.skipWaiting())
  );
});

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

self.addEventListener('fetch', (event) => {
  // Let WebSocket, upload, and dynamic streamlit messages pass through directly
  const url = new URL(event.request.url);
  if (
    url.pathname.includes('/_stcore/') ||
    url.pathname.includes('/stream') ||
    event.request.method !== 'GET'
  ) {
    return;
  }

  // Network-first for everything else
  event.respondWith(
    fetch(event.request).catch(() => caches.match(event.request))
  );
});
