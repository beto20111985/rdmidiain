# RD Player Web (RDPP/1)

Player web separado do painel (ADR_008), em Vite + TypeScript estrito, sem React. O mesmo bundle roda no navegador
(origem própria `player.<domínio>`; local `http://localhost:8088`) e dentro da casca Windows (.NET 8 + WebView2,
origem `https://player.rd.local/`). Contrato: `protocol/RD_PLAYER_PROTOCOL_v1.md`. Ponte nativa: [`BRIDGE.md`](BRIDGE.md).

## Estrutura

| Pasta | Conteúdo |
|---|---|
| `src/core/` | Núcleo **sem DOM**, testável em Node: RDJ1 (`canonical`), ES256 (`crypto`), tipos/validação (`protocol`), resolução normativa do ciclo (`cycle` — `resolve()` §5.2), runtime de reprodução (`playback`), sincronização atômica (`sync`), download com Range + SHA-256 incremental (`downloader`), PoP/outbox (`pop`), relógio (`clock`), alertas 7/28 dias (`alerts`), WebSocket (`ws`), pareamento (`pairing`), autenticação (`auth`), comandos (`commands`), kill (`kills`), interfaces de armazenamento (`storage`). |
| `src/adapters/web/` | IndexedDB (estado/outbox/chave), OPFS (mídia por sha256; escrita por offset no Worker com `createSyncAccessHandle`), fallback Blob em IndexedDB, WebCrypto não exportável, `persist()`/`estimate()`, Worker de download, Trusted Types, ponte `rd-native/1`. |
| `src/ui/` | Renderer double buffer A/B com crossfade e letterbox, rodapé, overlays (pareamento com QR local, alerta 28 dias, mensagens), watchdog web. |
| `public/sw.js` | Service Worker só do app shell versionado (mídia/API/WS nunca passam por ele). |
| `test/` | Vitest (Node + fake-indexeddb + jsdom) e carregador dos vetores `protocol/vectors/*.json`. |

## Configuração (sem domínio hardcoded)

Ordem de prioridade (`src/config.ts`):
1. `window.RD_NATIVE.config` (casca nativa; ignorado se `config_ok === false`): `api_base_url`, `ws_base_url`, `panel_url`.
2. `./config.json` servido junto ao bundle (Smart TVs, API em outra origem) — ver `config.example.json`:
   `{"api_base_url":"https://api.exemplo","ws_base_url":"wss://api.exemplo","panel_url":"https://painel.exemplo"}`.
3. `window.location.origin` (padrão): a origem do player faz proxy same-origin de `/api/v1/player/**` e `/ws/player/**`.

A base é sempre uma **origem** (`esquema://host[:porta]`); o núcleo acrescenta `/api/v1/player/...` e `/ws/player...`.
`ws_base_url` ausente ⇒ derivado (http→ws, https→wss). `panel_url` ausente ⇒ o QR usa o `panel_pair_url` do servidor.
URL `#tecnico` exibe o login controlado do técnico na tela de pareamento (credenciais nunca armazenadas).

## Comandos (sempre em container; nada instalado no host)

```sh
# dependências (node_modules num volume nomeado, fora do repositório)
docker run --rm -v D:/oficial/midiaindoor/player-web:/app -v rdpweb_nm:/app/node_modules -w /app node:20-alpine npm ci
# testes (monte protocol/ para os vetores)
docker run --rm -v D:/oficial/midiaindoor/player-web:/app -v rdpweb_nm:/app/node_modules \
  -v D:/oficial/midiaindoor/protocol:/protocol:ro -w /app node:20-alpine sh -c "npx tsc --noEmit -p tsconfig.json && npx tsc --noEmit -p tsconfig.test.json && npx vitest run"
# build de produção fora do repositório
docker run --rm -v D:/oficial/midiaindoor/player-web:/app -v rdpweb_nm:/app/node_modules -v <saida>:/out -w /app node:20-alpine sh -c "RD_OUT_DIR=/out npx vite build"
```

Dev: o serviço `player-web` do compose roda `npm run dev -- --host 0.0.0.0 --port 5174`; o nginx expõe em
`http://localhost:8088` (HMR na porta/esquema da própria página; `server.hmr.clientPort` só se `VITE_HMR_CLIENT_PORT`
for definido). Hosts aceitos (`server.allowedHosts`, sempre array): base + host de cada origem de
`RD_DEV_PLAYER_ALLOWED_ORIGINS` (túnel próprio do player, `ops/dev/dev-origins.env`) + `VITE_ALLOWED_HOSTS`
(legado) — parse em `dev-hosts.ts` (cópia de `frontend/dev-hosts.ts`); ver `ops/README.md` §3.1.

## CSP exigida (sem script/estilo inline, sem eval, sem WASM, sem CDN)

```
default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' blob: data:; media-src 'self' blob:;
connect-src 'self' <api/ws se em outra origem>; worker-src 'self'; manifest-src 'self'; object-src 'none';
base-uri 'self'; frame-ancestors 'none'; require-trusted-types-for 'script'; trusted-types rd-player
```
(Em dev o Vite precisa de `style-src 'unsafe-inline'` e `connect-src ws://localhost:8088` para o HMR.)

## Limitações conhecidas do navegador

- **Contexto seguro obrigatório** (HTTPS ou localhost) para WebCrypto, OPFS e Service Worker.
- **Autoplay:** vídeos são mudos e `playsinline` (permitido sem gesto no Chrome/Edge/WebView2). Em navegadores com
  política mais restrita, usar `--autoplay-policy=no-user-gesture-required` (kiosk) ou a casca nativa.
- **Persistência/quota:** sem `navigator.storage.persist()` concedido, o navegador pode despejar OPFS/IndexedDB sob
  pressão de disco — o rodapé mostra "ARMAZENAMENTO NÃO PERSISTENTE". Chrome concede para PWA instalado/site muito
  usado; a casca Windows não tem esse limite. A quota consultada é a do navegador, não o disco físico.
- **OPFS na thread principal** (sem Worker/SyncAccessHandle): o progresso do download só fica durável no `close()`.
- **Smart TVs (Tizen/webOS antigos):** sem OPFS ⇒ mídia em Blob no IndexedDB; navegadores muito antigos podem não ter
  Worker de módulo/Trusted Types (o bundle usa worker clássico e degrada), mas WebCrypto ECDSA P-256 e ES2018 são
  exigidos. Watchdog de processo depende da plataforma (o bundle só recarrega a própria página).
- **Telemetria web:** CPU/RAM usada/SSID/HDMI não são acessíveis ao navegador (vão `null`); a casca nativa preenche.
- **Aba oculta/minimizada:** timers são estrangulados; itens exibidos com a página oculta nunca são COMPLETED.
- **Orientação da tela:** `settings.orientation` do manifesto (LANDSCAPE/PORTRAIT) ≠ orientação física da saída ⇒
  rotação por software ±90° da tela lógica inteira (`.screen`, `src/ui/orientation.ts`), aplicada quando o manifesto é
  ativado (fronteira de ciclo) ou no resize; a telemetria reporta a orientação física.
- **Uma instância por origem:** abas na mesma origem compartilham a identidade do dispositivo; a segunda aba espera
  (Web Locks, `src/adapters/web/singleInstance.ts`) e assume quando a primeira fechar.
- **Dev em container (bind mount do Windows):** o Vite usa polling (`/.dockerenv`; desligar com `VITE_USE_POLLING=0`).
