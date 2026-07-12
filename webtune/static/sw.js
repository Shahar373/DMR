// Service Worker מינימלי (כמו AIR-AM): מפעיל מיד ותופס שליטה, אבל *בלי* caching
// של fetch => עדכון ברשת המקומית (install.sh) לעולם לא מוגש מ-cache ישן.
self.addEventListener("install", (e) => self.skipWaiting());
self.addEventListener("activate", (e) => e.waitUntil(self.clients.claim()));
self.addEventListener("fetch", (e) => { /* pass-through */ });
