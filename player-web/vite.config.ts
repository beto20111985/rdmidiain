/**
 * Vite + Vitest — RD Player Web.
 * - base relativa ("./"): o mesmo bundle roda em http://localhost:8088/ (nginx → Vite dev),
 *   em player.<domínio>/ e na origem virtual da casca Windows (https://player.rd.local/).
 * - Sem domínio hardcoded: a URL da API vem de window.RD_NATIVE, de ./config.json ou de window.location.
 * - O plugin `rdServiceWorkerManifest` injeta a lista de arquivos do app shell e a versão no sw.js
 *   copiado de public/ (a mídia NUNCA passa pelo Service Worker).
 */
import { createHash } from 'node:crypto';
import { readFileSync, writeFileSync, existsSync } from 'node:fs';
import { resolve } from 'node:path';
import { defineConfig, type Plugin } from 'vitest/config';
import { resolveAllowedHosts } from './dev-hosts';

const pkg = JSON.parse(readFileSync(new URL('./package.json', import.meta.url), 'utf8')) as { version: string };

function rdServiceWorkerManifest(): Plugin {
  let outDir = 'dist';
  const files: string[] = [];
  return {
    name: 'rd-sw-manifest',
    apply: 'build',
    configResolved(cfg) {
      outDir = cfg.build.outDir;
    },
    writeBundle(_opts, bundle) {
      // writeBundle: todos os arquivos finais (JS, worker, CSS) já existem.
      for (const name of Object.keys(bundle)) {
        if (!name.endsWith('.map') && !files.includes(name)) files.push(name);
      }
    },
    closeBundle() {
      const swPath = resolve(outDir, 'sw.js');
      if (!existsSync(swPath)) return;
      const shell = ['./', 'index.html', 'manifest.webmanifest', 'icon.svg', ...files.filter((f) => f !== 'index.html').sort()];
      const version = createHash('sha256').update(pkg.version + '\n' + shell.join('\n')).digest('hex').slice(0, 16);
      const src = readFileSync(swPath, 'utf8');
      const marker = /\/\*__RD_BUILD__\*\/[\s\S]*?\/\*__RD_BUILD_END__\*\//;
      if (!marker.test(src)) throw new Error('sw.js sem marcador __RD_BUILD__');
      const build = JSON.stringify({ version: `${pkg.version}-${version}`, files: shell });
      writeFileSync(swPath, src.replace(marker, `/*__RD_BUILD__*/${build}/*__RD_BUILD_END__*/`));
    },
  };
}

// HMR: por padrão o cliente usa esquema/host/porta da PÁGINA (http://localhost:8088 → ws://…:8088;
// https://<túnel> → wss://<túnel>:443). VITE_HMR_CLIENT_PORT só vale se definido explicitamente
// (o antigo padrão fixo 8088 quebraria o HMR por túnel https).
const hmrClientPortRaw = (process.env.VITE_HMR_CLIENT_PORT ?? '').trim();
const hmrClientPort = hmrClientPortRaw ? Number(hmrClientPortRaw) : Number.NaN;
const inDocker = existsSync('/.dockerenv');
// Hosts aceitos: SEMPRE um ARRAY (nunca `true`) = base (localhost, 127.0.0.1, nomes internos do
// Docker) + host de cada origem de RD_DEV_PLAYER_ALLOWED_ORIGINS (ops/dev/dev-origins.env — túnel
// próprio do player; ver ops/README.md §3.1) + VITE_ALLOWED_HOSTS (legado, mesmo parser defensivo).
// Entrada inválida → o Vite não sobe. (*.localhost já é aceito pelo próprio Vite.)
const allowedHosts = resolveAllowedHosts({
  base: ['localhost', '127.0.0.1', 'player-web', 'rd-player-web'],
  origins: process.env.RD_DEV_PLAYER_ALLOWED_ORIGINS,
  originsSource: 'RD_DEV_PLAYER_ALLOWED_ORIGINS',
  hosts: process.env.VITE_ALLOWED_HOSTS,
  hostsSource: 'VITE_ALLOWED_HOSTS',
});

export default defineConfig({
  base: './',
  define: {
    __APP_VERSION__: JSON.stringify(pkg.version),
  },
  server: {
    host: '0.0.0.0',
    port: 5174,
    strictPort: true,
    allowedHosts,
    // Atrás do nginx o cliente HMR usa a porta PÚBLICA da página; clientPort só se configurado.
    hmr: Number.isFinite(hmrClientPort) && hmrClientPort > 0 ? { clientPort: hmrClientPort } : undefined,
    // Em container com bind mount do Windows não chegam eventos inotify do host: usa polling (só src/public).
    watch: inDocker && process.env.VITE_USE_POLLING !== '0' ? { usePolling: true, interval: 1000 } : undefined,
  },
  preview: {
    host: '0.0.0.0',
    port: 4174,
    strictPort: true,
  },
  build: {
    target: 'es2018',
    outDir: process.env.RD_OUT_DIR ?? 'dist',
    emptyOutDir: true,
    sourcemap: false,
    assetsInlineLimit: 0,
    modulePreload: { polyfill: false },
    cssCodeSplit: false,
  },
  worker: {
    format: 'iife',
  },
  plugins: [rdServiceWorkerManifest()],
  test: {
    environment: 'node',
    include: ['test/**/*.test.ts'],
    testTimeout: 60_000,
    hookTimeout: 60_000,
    reporters: ['verbose'],
    // --expose-gc: o teste de hash incremental mede a memória retida após GC.
    pool: 'forks',
    poolOptions: { forks: { execArgv: ['--expose-gc'] } },
  },
});
