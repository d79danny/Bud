const version = "budai-shell-1";
const shell = ["/", "/app.js", "/common.js", "/chat.js", "/computer.js", "/browser.js", "/studio.js",
  "/voice.js", "/pages.js", "/settings.js", "/style.css", "/manifest.webmanifest", "/budai-192.png", "/budai-512.png"];

self.addEventListener("install", event => {
  event.waitUntil(caches.open(version).then(cache => cache.addAll(shell)).then(() => self.skipWaiting()));
});
self.addEventListener("activate", event => {
  event.waitUntil(caches.keys().then(keys => Promise.all(keys.filter(key =>
    key.startsWith("budai-shell-") && key !== version).map(key => caches.delete(key)))).then(() => self.clients.claim()));
});
self.addEventListener("fetch", event => {
  const url = new URL(event.request.url);
  if (event.request.method !== "GET" || url.origin !== self.location.origin || !shell.includes(url.pathname)) return;
  event.respondWith(fetch(event.request).catch(() => caches.open(version).then(cache => cache.match(url.pathname))));
});
