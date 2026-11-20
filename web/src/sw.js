// Service worker: keeps the files of THIS build (BUILD, FILES are filled in by
// build.py) in a cache named after it, so the next visits open without
// downloading ~10 MB again, and offline. Cache first: a file is fetched from the
// network only the first time. A new build means a new sw.js; the browser
// installs it on a later visit, and that drops the old build's cache. The page
// never mixes builds: each build has its own cache and file list.
//
// Only GET requests for files of this build, from this origin, are touched;
// anything else (the WebSocket to the chat server included) passes through.
const BUILD = "__BUILD__";
const FILES = new Set(__FILES__);
const CACHE = "crptochat-" + BUILD;

self.addEventListener("install", () => self.skipWaiting());

self.addEventListener("activate", (event) => {
  event.waitUntil((async () => {
    for (const name of await caches.keys()) {
      if (name.startsWith("crptochat-") && name !== CACHE) await caches.delete(name);
    }
    await self.clients.claim();
  })());
});

function buildPath(request) {
  if (request.method !== "GET") return null;
  const url = new URL(request.url);
  if (url.origin !== self.location.origin || url.search) return null;
  const scope = new URL(self.registration.scope).pathname;
  if (!url.pathname.startsWith(scope)) return null;
  const rel = url.pathname.slice(scope.length) || "index.html";
  return FILES.has(rel) ? rel : null;
}

self.addEventListener("fetch", (event) => {
  const rel = buildPath(event.request);
  if (!rel) return;
  event.respondWith((async () => {
    const cache = await caches.open(CACHE);
    const key = new URL(rel, self.registration.scope).href;
    const hit = await cache.match(key);
    if (hit) return hit;
    const resp = await fetch(event.request);
    if (resp.ok && resp.type === "basic") await cache.put(key, resp.clone());
    return resp;
  })());
});
