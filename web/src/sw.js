// Service worker: keeps the files of THIS build (BUILD, FILES are filled in by
// build.py) in a cache named after it, so the next visits open without
// downloading ~10 MB again, and offline. Cache first: a file is fetched from the
// network only the first time.
//
// Builds never mix. Every file but index.html lives under b/<build id>/, and a
// page only asks for files under its own build's directory; this worker only
// answers for the paths of its own build. A new build means a new sw.js: it takes
// over at once (skipWaiting + claim) and drops the old build's cache. An open page
// of the old build then reloads if it is idle on the login screen (main.js), or
// carries on: it needs nothing else once the chat runs, and any file of its own
// build it still asks for comes from the network under the old build's path
// (or fails with a 404), never from another build.
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
