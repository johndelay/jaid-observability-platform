// Minimal service worker — enables "Add to Home Screen" (PWA) without offline caching.
// The dashboard is live data, so we intentionally do NOT cache responses.
self.addEventListener('install', () => self.skipWaiting());
self.addEventListener('activate', (e) => e.waitUntil(self.clients.claim()));
self.addEventListener('fetch', () => {});
