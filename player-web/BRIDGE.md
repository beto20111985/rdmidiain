# Ponte JS ↔ casca nativa — `rd-native/1` (ESPECIFICAÇÃO OFICIAL)

> Dono: player web (W-PWEB, AÇÃO 002, 2026-10-07). Origem: proposta do agente da casca Windows
> (`player-windows/BRIDGE_PROPOSTA.md`, incluindo a revisão de `update_app`), **adotada sem divergências de contrato**.
> Implementação no bundle: `src/adapters/web/platform.ts` (`NativeBridge`/`WebBridge`, `createBridge()`), uso em `src/app.ts`.
> O protocolo de negócio (pareamento, manifesto, PoP, WebSocket, comandos de negócio) continua 100% no bundle
> conforme `protocol/RD_PLAYER_PROTOCOL_v1.md` (RDPP/1). A casca provê só **plataforma**.
> Mudança incompatível = `rd-native/2`.

## 1. Detecção e configuração

A casca injeta, **antes de qualquer script da página** (WebView2: `AddScriptToExecuteOnDocumentCreatedAsync`), um objeto congelado:

```js
window.RD_NATIVE = {
  bridge: "rd-native/1",
  platform: "windows",              // "windows" | "android" (futuro)
  shell_version: "1.0.0",           // vira o app_version reportado pelo bundle
  launched_by_watchdog: false,
  config: {                         // sem domínio hardcoded (config.json da casca)
    api_base_url: "https://api.exemplo.com.br",
    ws_base_url:  "wss://api.exemplo.com.br",
    panel_url:    "https://painel.exemplo.com.br",
    update_check_interval_hours: 6,
    config_ok: true,                // false ⇒ o bundle ignora este bloco
    config_errors: []
  },
  capabilities: ["telemetry","restart_app","reboot","update_app","watchdog","kiosk","power"]
}
```

Comportamento do bundle:
- `window.RD_NATIVE.bridge === "rd-native/1"` **e** `window.chrome.webview` presentes ⇒ modo nativo (`NativeBridge`). Caso contrário ⇒ modo navegador (`WebBridge`).
- Configuração (prioridade): `RD_NATIVE.config` (se `config_ok !== false` e houver `api_base_url`) → `./config.json` junto ao bundle → `window.location.origin`. A base é a **origem**; o bundle acrescenta `/api/v1/player/...` e `/ws/player...`. `ws_base_url` ausente ⇒ derivado de `api_base_url` (http→ws, https→wss).
- `panel_url` presente ⇒ o QR de pareamento codifica `<panel_url>/telas/parear?code=<código>`; senão usa `panel_pair_url` devolvido pelo servidor.
- `platform` do pareamento/telemetria = `RD_NATIVE.platform`; `app_version` = `RD_NATIVE.shell_version` (fallback: versão do bundle).
- Origem do bundle no Windows: `https://player.rd.local/` (contexto seguro). O bundle usa **base relativa** (`./`) e **não registra Service Worker** em modo nativo (o app shell já é local).

## 2. Transporte

`window.chrome.webview.postMessage(obj)` (JS→nativo) e `window.chrome.webview.addEventListener("message", ev => ev.data)` (nativo→JS). O bundle aceita `ev.data` como objeto (`PostWebMessageAsJson`) ou string JSON (`PostWebMessageAsString`).

| Direção | Forma |
|---|---|
| JS → nativo (pedido) | `{"rd":1,"kind":"request","id":"<uuid>","method":"<nome>","params":{…}}` |
| nativo → JS (resposta) | `{"rd":1,"kind":"response","id":"<mesmo id>","ok":true,"result":{…}}` ou `{"rd":1,"kind":"response","id":"…","ok":false,"error":{"code":"…","message":"…"}}` |
| JS → nativo (notificação) | `{"rd":1,"kind":"notify","method":"<nome>","params":{…}}` |
| nativo → JS (evento) | `{"rd":1,"kind":"event","event":"<nome>","data":{…}}` |

- Pedidos do bundle têm **timeout de 10 s** (resposta ausente ⇒ erro `TIMEOUT` local; o bundle segue sem a casca).
- Mensagens com `rd != 1`, sem `kind`, > 64 KiB ou de origem diferente da do bundle são descartadas pela casca. O bundle ignora mensagens que não sejam envelopes `rd:1` válidos.

## 3. Métodos (request → response)

| method | params | result |
|---|---|---|
| `get_info` | `{}` | `{platform, shell_version, os, launched_by_watchdog, config, capabilities}` |
| `get_telemetry` | `{}` | Campos do `telemetry.data` do RDPP §4: `{cpu_pct, ram_used_mb, ram_total_mb, disk_free_mb, disk_total_mb, uptime_s, os, app_version, platform, resolution:"1920x1080", orientation:"LANDSCAPE"\|"PORTRAIT", wifi:{ssid, rssi_dbm}\|null, hdmi_connected:true\|false\|null}` — **sem IP**. O bundle sobrepõe estes campos aos medidos pelo navegador e envia `telemetry` pelo WS a cada `telemetry_interval_ms` (5 min). |
| `command` | `{command_id, command, args}`, `command ∈ restart_app \| reboot` | `{status:"ACCEPTED"\|"DONE"\|"FAILED"\|"UNSUPPORTED", detail, delay_ms?}`. `ACCEPTED` = vai acontecer em `delay_ms`. |
| `update_app` | `{access_token?, update?:{version,url,sha256,size,signature,key_id?,mandatory}, mode?:"check"\|"apply"\|"apply_now"}` | Resposta rápida (≤ ~8 s): `{status:"NO_UPDATE"\|"AVAILABLE"(check)\|"ACCEPTED"(download em segundo plano)\|"REJECTED"\|"FAILED", version?, detail}`. Andamento pelo evento `update_status`. |
| `reload` | `{reason}` | `{status:"ACCEPTED"}` (recarrega o WebView) |

### 3.1 Comandos remotos (RDPP §4) × ponte

| comando do servidor | quem executa | `command_ack` enviado pelo bundle |
|---|---|---|
| `restart_app` | casca (`command`) — em navegador: `location.reload()` | `RECEIVED` na hora; estado persistido `PENDING_RESTART`; `DONE` após voltar (reentrega do servidor não reexecuta). |
| `reboot` | casca (`command`, capability `reboot`) — navegador: `UNSUPPORTED` | idem `restart_app`; `UNSUPPORTED`/`FAILED` repassados. |
| `update_app` | casca (`update_app` com `mode:"apply_now"` e o `access_token` em memória) — navegador: atualização do Service Worker | `ACCEPTED` ⇒ `RECEIVED` + `PENDING_RESTART`; `update_status` `FAILED`/`ROLLED_BACK` ⇒ `FAILED`; `APPLIED` (após reiniciar) ou reconexão ⇒ `DONE`; `NO_UPDATE` ⇒ `DONE`. |
| `resync`, `reload_manifest`, `clear_cache`, `redownload` | **bundle** (a casca responde `UNSUPPORTED` se receber) | `RECEIVED` → `DONE`/`FAILED`. |

### 3.2 Atualização (RDPP §8/§8.1)
- Se `update` vier preenchido, a casca usa esse objeto; senão chama `GET /api/v1/player/app-update?platform=windows&version=<shell_version>` com `Authorization: Bearer <access_token>` (token só em memória).
- A casca **não confia no bundle**: verifica ES256 com as chaves de release **fixadas** na própria configuração sobre RDJ1 `{"platform","purpose":"rd-app-update-v1","sha256","size","version"}` e depois sha256/tamanho do pacote.

## 4. Notificações (JS → nativo)

| method | params | quando o bundle envia |
|---|---|---|
| `ready` | `{bundle_version}` | uma vez, ao terminar a inicialização (`PlayerApp.start`). |
| `progress` | `{state:"PLAYING"\|"IDLE"\|"SYNCING"\|"ERROR"\|"FROZEN"\|"UNPAIRED", creative_code?, item_started_at_ms?}` | a cada **5 s** (requisito: ≤ 10 s) e a cada início de item. `FROZEN` só é enviado se o watchdog interno do bundle não se recuperou em 15 s (para não duplicar a reação da casca). |
| `log` | `{level, message}` | eventos operacionais (≤ 2 KB; **sem segredos/tokens**). |

Watchdog da casca: sem `progress` por 30 s (após 90 s de carência) ⇒ recarrega o WebView; 3 recargas sem recuperação em 10 min ⇒ reinicia o processo. O watchdog interno do bundle (rAF congelado > 5 s, vídeo parado > 6 s, `waiting` > 8 s) recria as camadas e, após 3 recuperações em 10 min, pede `reload` à casca.

## 5. Eventos (nativo → JS)

| event | data | reação do bundle |
|---|---|---|
| `display_changed` | `{resolution, orientation}` | recalcula a altura do rodapé/área útil. |
| `power_resumed` | `{}` | reconecta o WS imediatamente (o relógio é remedido pela resposta do servidor). |
| `update_status` | `{state:"DOWNLOADING"\|"VERIFYING"\|"STAGED"\|"APPLYING"\|"FAILED", version, detail}`; após reiniciar, uma vez: `{state:"APPLIED"\|"ROLLED_BACK"\|"FAILED", …}` | registra; conclui o `update_app` pendente (`FAILED`/`ROLLED_BACK` ⇒ ack `FAILED`; `APPLIED` ⇒ `DONE`). |
| `watchdog_reload` | `{reason}` | registra em log. |

## 6. Erros

Códigos da casca: `BAD_REQUEST`, `UNKNOWN_METHOD`, `UNSUPPORTED`, `POLICY_DENIED`, `UPDATE_KEYS_NOT_CONFIGURED`, `SIGNATURE_INVALID`, `HASH_MISMATCH`, `DOWNGRADE`, `NETWORK`, `INTERNAL`. Local do bundle: `TIMEOUT` (sem resposta em 10 s). `UNSUPPORTED`/`UNKNOWN_METHOD` em `command` viram `command_ack UNSUPPORTED`.

## 7. Segurança

- A ponte não transporta chaves privadas: o par ES256 do dispositivo é WebCrypto não exportável no IndexedDB do WebView2 (pasta de dados persistente da casca).
- O `access_token` (JWT de 15 min) só trafega em `update_app` quando necessário, em memória; nunca em log.
- Telemetria sem IP (RDPP §4).
