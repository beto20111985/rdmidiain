/**
 * Entrada do RD Player Web. Monta a UI, escolhe adaptadores (OPFS/IndexedDB/WebCrypto/Worker), carrega a
 * configuração (RD_NATIVE → config.json → window.location) e inicia o PlayerApp.
 * Nenhum script inline; nenhum recurso de CDN.
 */

import './ui/styles.css';
import workerUrl from './adapters/web/download.worker.ts?worker&url';
import { openPlayerDb } from './adapters/web/idb';
import { IdbMediaStore } from './adapters/web/idbMediaStore';
import { IdbKeyStore } from './adapters/web/keyStore';
import { OpfsMediaStore } from './adapters/web/opfsMediaStore';
import { createBridge, type PlatformBridge } from './adapters/web/platform';
import { IdbStateStore } from './adapters/web/stateStore';
import { acquireSingleInstance, type LockManagerLike } from './adapters/web/singleInstance';
import { requestPersistence, storageEstimate } from './adapters/web/storageManager';
import { trustedScriptUrl } from './adapters/web/trusted';
import { WorkerDownloadExecutor } from './adapters/web/workerExecutor';
import { PlayerApp } from './app';
import { loadConfig } from './config';
import { MemoryKeyStore, MemoryMediaStore, MemoryStateStore } from './core/memoryStores';
import type { KeyStore, MediaStore, StateStore } from './core/storage';
import { errorMessage } from './core/util';
import type { WebSocketLike } from './core/ws';
import { FooterView } from './ui/footer';
import { Overlays } from './ui/overlays';
import { ScreenOrienter } from './ui/orientation';
import { DomRenderer } from './ui/renderer';

function makeLogger(bridge: PlatformBridge) {
  return (level: 'info' | 'warn' | 'error', msg: string): void => {
    const line = `[rd-player] ${msg}`;
    if (level === 'error') console.error(line);
    else if (level === 'warn') console.warn(line);
    else console.info(line);
    bridge.log(level, msg);
  };
}

async function registerServiceWorker(bridge: PlatformBridge, log: ReturnType<typeof makeLogger>): Promise<void> {
  // App shell offline. Na casca Windows o shell já é local (o SW é opcional e pode falhar sem problema).
  if (import.meta.env.DEV || bridge.native || !('serviceWorker' in navigator)) return;
  try {
    await navigator.serviceWorker.register(trustedScriptUrl('./sw.js'), { scope: './' });
  } catch (e) {
    log('warn', `Service Worker não registrado: ${errorMessage(e)}`);
  }
}

function keepAwake(): void {
  const nav = navigator as unknown as { wakeLock?: { request(type: 'screen'): Promise<unknown> } };
  if (!nav.wakeLock) return;
  const request = (): void => {
    nav.wakeLock?.request('screen').catch(() => undefined);
  };
  request();
  document.addEventListener('visibilitychange', () => {
    if (document.visibilityState === 'visible') request();
  });
}

function fullscreenOnInteraction(): void {
  const go = (): void => {
    if (!document.fullscreenElement) document.documentElement.requestFullscreen?.().catch(() => undefined);
  };
  window.addEventListener('pointerdown', go);
  window.addEventListener('keydown', (ev) => {
    if (ev.key === 'F' || ev.key === 'f') go();
  });
}

async function main(): Promise<void> {
  const app = document.getElementById('app')!;
  // Tela lógica: tudo (camadas, rodapé, overlays) fica dentro de .screen, girado por software se necessário.
  const root = document.createElement('div');
  root.className = 'screen';
  app.append(root);
  const orienter = new ScreenOrienter(root, window);
  orienter.relayout();
  const stage = document.createElement('div');
  stage.className = 'stage';
  root.append(stage);
  const footer = new FooterView(root, stage, document, () => orienter.logicalHeight);
  const renderer = new DomRenderer(stage, footer);
  const overlays = new Overlays(root);
  const bridge = createBridge();
  const log = makeLogger(bridge);
  renderer.onPlaybackIssue = (m) => log('warn', m);

  if (!globalThis.isSecureContext || !globalThis.crypto?.subtle) {
    overlays.showMessage('Contexto inseguro', 'O RD Player exige HTTPS (ou localhost) para a identidade criptográfica e o armazenamento offline.');
    return;
  }

  // Uma única instância por origem (a identidade do dispositivo é compartilhada entre abas).
  const instance = await acquireSingleInstance((navigator as unknown as { locks?: LockManagerLike }).locks, () => {
    overlays.showMessage('Player já aberto', 'Outra janela do RD Player está ativa neste navegador. Esta janela assume quando a outra for fechada.');
  });
  overlays.hideMessage();
  if (instance === 'UNSUPPORTED') console.warn('[rd-player] Web Locks indisponível: sem proteção de instância única');

  const config = await loadConfig({ location: window.location, fetch: (u, i) => fetch(u, i), bridge, appVersion: __APP_VERSION__ });
  log('info', `RD Player ${config.appVersion} (${config.platform}) — API ${config.apiBase} [${config.source}]`);
  void registerServiceWorker(bridge, log);

  const persistence = await requestPersistence();
  if (!persistence.persisted) log('warn', 'navigator.storage.persist() negado: armazenamento pode ser despejado');

  let state: StateStore;
  let keys: KeyStore;
  let db: IDBDatabase | null = null;
  try {
    db = await openPlayerDb();
    state = new IdbStateStore(db);
    keys = new IdbKeyStore(db);
  } catch (e) {
    log('error', `IndexedDB indisponível (${errorMessage(e)}): estado só em memória`);
    state = new MemoryStateStore();
    keys = new MemoryKeyStore();
  }

  let media: MediaStore | null = await OpfsMediaStore.open('writable');
  let workerExecutor: WorkerDownloadExecutor | null = null;
  if (media) {
    workerExecutor = await WorkerDownloadExecutor.create(workerUrl);
    log('info', workerExecutor ? 'downloads: Worker + OPFS (SyncAccessHandle)' : 'downloads: OPFS na thread principal');
  } else if (db) {
    media = new IdbMediaStore(db);
    log('warn', 'OPFS indisponível: mídia em IndexedDB (Blob)');
  } else {
    media = new MemoryMediaStore();
    log('error', 'sem OPFS/IndexedDB: mídia só em memória');
  }

  const player = new PlayerApp({
    config,
    bridge,
    state,
    keys,
    media,
    workerExecutor: workerExecutor ? (urlFor, headers) => workerExecutor!.executor(urlFor, headers) : null,
    renderer,
    footer,
    overlays,
    orienter,
    wsFactory: (url) => new WebSocket(url) as unknown as WebSocketLike,
    storagePersistent: persistence.persisted,
    estimate: storageEstimate,
    log,
    reloadPage: () => window.location.reload(),
    techMode: window.location.hash === '#tecnico',
  });
  window.addEventListener('hashchange', () => overlays.showTech(window.location.hash === '#tecnico'));
  keepAwake();
  fullscreenOnInteraction();
  await player.start();
}

main().catch((e) => {
  console.error('[rd-player] falha fatal', e);
  const msg = document.createElement('pre');
  msg.textContent = `RD Player: falha ao iniciar — ${errorMessage(e)}`;
  msg.className = 'fatal';
  document.body.append(msg);
  // Recupera sozinho depois de um tempo (ex.: rede/armazenamento indisponível no boot).
  setTimeout(() => window.location.reload(), 60_000);
});
