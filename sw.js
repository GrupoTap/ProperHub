/* ProperHub — Service Worker
 * Estratégia:
 *   - Navegação/HTML  -> network-first (sempre busca a versão nova; cai no cache só offline)
 *   - Estáticos same-origin (GET) -> stale-while-revalidate (rápido + atualiza em 2º plano)
 *   - Cross-origin (ex.: GAS /exec em script.google.com) -> NUNCA intercepta: passa direto pra rede
 *   - Requisições não-GET (POST do pipeline/GAS) -> passam direto, nunca são cacheadas
 *
 * DISCIPLINA DE VERSÃO: bump em CACHE a cada deploy do app.
 * O activate abaixo apaga qualquer cache antigo com prefixo 'properhub-'.
 */
const CACHE = 'properhub-v20'; // 28/08/2026: par do index v20 (card de Inspeção passa a mandar ?modo=inspecao)
const SCOPE_PREFIX = '/ProperHub/';
const APP_SHELL = [
  '/ProperHub/',
  '/ProperHub/index.html',
  '/ProperHub/manifest.webmanifest',
  '/ProperHub/icon-192.png',
  '/ProperHub/icon-512.png',
  '/ProperHub/icon-maskable-512.png',
  '/ProperHub/apple-touch-icon.png',
  // v18 — faltavam no shell: dependiam do stale-while-revalidate oportunista,
  // então o primeiro boot offline depois de instalar podia ficar sem o
  // bootstrap PWA. O .catch do install já tolera asset ausente.
  '/ProperHub/proper-pwa.js',
  '/ProperHub/sw.js'
];

self.addEventListener('install', (event) => {
  event.waitUntil(
    caches.open(CACHE)
      .then((c) => c.addAll(APP_SHELL.map((u) => new Request(u, { cache: 'reload' }))))
      .catch(() => { /* algum asset pode não existir ainda; não bloquear a instalação */ })
      .then(() => self.skipWaiting())
  );
});

self.addEventListener('activate', (event) => {
  event.waitUntil(
    caches.keys()
      .then((keys) => Promise.all(
        keys.filter((k) => k.startsWith('properhub-') && k !== CACHE)
            .map((k) => caches.delete(k))
      ))
      .then(() => self.clients.claim())
  );
});

self.addEventListener('fetch', (event) => {
  const req = event.request;
  const url = new URL(req.url);

  // Só cuida do mesmo origin e da própria pasta; o resto (GAS, fontes, etc.) segue pra rede
  if (url.origin !== self.location.origin) return;
  if (req.method !== 'GET') return;
  if (!url.pathname.startsWith(SCOPE_PREFIX)) return;

  const isNavigation =
    req.mode === 'navigate' ||
    (req.headers.get('accept') || '').includes('text/html');

  if (isNavigation) {
    // v18 — network-first COM CORRIDA de 2500ms contra o cache (forma portada
    // do sw.js do PCF v76). Antes: o cache só entrava quando o fetch REJEITAVA,
    // então uma rede viva-porém-lenta segurava a tela branca do Hub — a
    // primeira tela de todo mundo — por segundos. Agora: se a rede não
    // respondeu em 2,5s e HÁ cache, serve o cache na hora; a rede continua em
    // background e atualiza o cache para a abertura seguinte.
    // ⚠ A disciplina de versão não muda: o bump do CACHE a cada deploy
    // continua obrigatório — a versão nova entra na abertura seguinte.
    event.respondWith((async () => {
      const cachedPromise = caches.match(req)
        .then((hit) => hit || caches.match('/ProperHub/index.html'))
        .catch(() => null);

      // rede: sempre atualiza o cache quando responder, mesmo perdendo a corrida
      const networkPromise = fetch(req)
        .then((res) => {
          const copy = res.clone();
          caches.open(CACHE).then((c) => c.put(req, copy)).catch(() => {});
          return res;
        });

      const timer = new Promise((resolve) => setTimeout(() => resolve('TIMEOUT'), 2500));
      const first = await Promise.race([networkPromise.catch(() => 'NETFAIL'), timer]);

      if (first !== 'TIMEOUT' && first !== 'NETFAIL') return first;  // rede chegou a tempo

      const cached = await cachedPromise;
      if (cached) {
        networkPromise.catch(() => {}); // segue atualizando em background, sem unhandled
        return cached;
      }
      // sem cache ainda (1ª instalação): só resta esperar a rede de verdade
      return networkPromise.catch(() =>
        caches.match('/ProperHub/index.html').then((hit) => hit || Response.error())
      );
    })());
    return;
  }

  // estáticos: stale-while-revalidate
  event.respondWith(
    caches.match(req).then((cached) => {
      const network = fetch(req)
        .then((res) => {
          if (res && res.status === 200) {
            const copy = res.clone();
            caches.open(CACHE).then((c) => c.put(req, copy)).catch(() => {});
          }
          return res;
        })
        .catch(() => cached);
      return cached || network;
    })
  );
});

// Permite que a página force a ativação imediata de uma versão nova do SW
self.addEventListener('message', (event) => {
  if (event.data === 'SKIP_WAITING') self.skipWaiting();
});
