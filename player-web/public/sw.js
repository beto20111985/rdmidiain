/*
 * RD Player — Service Worker APENAS do app shell (versionado).
 * Mídia (/api/v1/player/media/*), API e WebSocket NUNCA passam por aqui: vão direto à rede (a mídia fica no OPFS).
 * A lista de arquivos e a versão são injetadas no build (vite.config.ts, plugin rd-sw-manifest).
 */
/* eslint-disable no-restricted-globals */
const BUILD = /*__RD_BUILD__*/{"version":"dev","files":[]}/*__RD_BUILD_END__*/;
const PREFIX = 'rd-player-shell-';
const CACHE = PREFIX + BUILD.version;
const SCOPE = self.registration.scope;
const INDEX = new URL('index.html', SCOPE).href;

self.addEventListener('install', (event) => {
  event.waitUntil(
    (async () => {
      if (BUILD.version !== 'dev' && BUILD.files.length) {
        const cache = await caches.open(CACHE);
        await cache.addAll(BUILD.files.map((f) => new URL(f, SCOPE).href));
      }
      await self.skipWaiting();
    })(),
  );
});

self.addEventListener('activate', (event) => {
  event.waitUntil(
    (async () => {
      for (const key of await caches.keys()) {
        if (key.startsWith(PREFIX) && key !== CACHE) await caches.delete(key);
      }
      await self.clients.claim();
    })(),
  );
});

function isShell(url) {
  if (url.origin !== self.location.origin) return false;
  if (!url.href.startsWith(SCOPE)) return false;
  const path = url.pathname;
  return !path.includes('/api/') && !path.includes('/ws/');
}

async function networkFirst(request, cacheKey) {
  const cache = await caches.open(CACHE);
  try {
    const res = await fetch(request, { cache: 'no-store' });
    if (res.ok) await cache.put(cacheKey, res.clone());
    return res;
  } catch (e) {
    const hit = await cache.match(cacheKey);
    if (hit) return hit;
    throw e;
  }
}

self.addEventListener('fetch', (event) => {
  const req = event.request;
  if (req.method !== 'GET' || req.headers.has('range')) return;
  const url = new URL(req.url);
  if (!isShell(url)) return; // mídia/API/WS: rede direta
  if (req.mode === 'navigate') {
    event.respondWith(networkFirst(req, INDEX).catch(async () => (await caches.match(SCOPE)) || Response.error()));
    return;
  }
  if (url.pathname.endsWith('/config.json')) {
    event.respondWith(networkFirst(req, url.href).catch(() => new Response('', { status: 404 })));
    return;
  }
  // Assets com hash no nome: cache-first (imutáveis por versão).
  event.respondWith(
    (async () => {
      const cache = await caches.open(CACHE);
      const hit = await cache.match(req);
      if (hit) return hit;
      const res = await fetch(req);
      if (res.ok && res.type === 'basic') await cache.put(req, res.clone());
      return res;
    })(),
  );
});
