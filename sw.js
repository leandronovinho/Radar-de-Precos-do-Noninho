// Radar de Preços: permite instalar como aplicativo.
// Sempre busca a versão nova na internet; o que ficou guardado só é usado se estiver sem conexão.
const CACHE = "radar-v2";
self.addEventListener("install", e => {
  e.waitUntil(caches.open(CACHE).then(c => c.addAll(["./", "./index.html", "./manifest.webmanifest", "./icone-192.png"])).then(() => self.skipWaiting()));
});
self.addEventListener("activate", e => {
  e.waitUntil(caches.keys().then(ks => Promise.all(ks.filter(k => k !== CACHE).map(k => caches.delete(k)))).then(() => self.clients.claim()));
});
self.addEventListener("fetch", e => {
  const url = new URL(e.request.url);
  if (e.request.method !== "GET" || url.origin !== location.origin) return;
  e.respondWith(fetch(e.request).then(r => {
    if (r.ok) { const copia = r.clone(); caches.open(CACHE).then(c => c.put(e.request.url.split("?")[0], copia)); }
    return r;
  }).catch(() => caches.match(e.request.url.split("?")[0]).then(r => r || caches.match("./index.html"))));
});
