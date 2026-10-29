# Ponte JS ↔ nativo do Player Windows — PROPOSTA `rd-native/1`

> Autor: agente W-WIN (AÇÃO 002, 2026-10-07). Status: **ACEITA pelo coordenador como especificação** (W-PWEB a adota em `player-web/BRIDGE.md`; se houver divergência, prevalece o `BRIDGE.md` e a casca se adapta).
> Revisão 2026-10-07 (W-WIN): `update_app` passou a responder rápido (`ACCEPTED`) e a reportar o download pelo evento `update_status`, para não estourar o timeout de 10 s dos pedidos do bundle.
> Implementação de referência: `src/RdPlayer.Shell/BridgeHost.cs` + tipos puros em `src/RdPlayer.Core/Bridge/`.
> Protocolo de negócio (pareamento, manifesto, PoP, WS) continua 100% no bundle web, conforme `protocol/RD_PLAYER_PROTOCOL_v1.md` (RDPP/1). A casca só provê **plataforma**.

## 1. Detecção e configuração (síncrona, antes de qualquer script da página)

A casca injeta (via `AddScriptToExecuteOnDocumentCreatedAsync`) um objeto congelado:

```js
window.RD_NATIVE = {
  bridge: "rd-native/1",
  platform: "windows",
  shell_version: "1.0.0",            // versão do pacote Windows (= app_version da plataforma)
  launched_by_watchdog: true,
  config: {                          // vem de %PROGRAMDATA%\RdMidiaIndoor\Player\config.json — sem domínio hardcoded
    api_base_url: "https://api.exemplo.com.br",
    ws_base_url:  "wss://api.exemplo.com.br",
    panel_url:    "https://painel.exemplo.com.br",
    update_check_interval_hours: 6,
    config_ok: true,                 // false => config ausente/inválida (ver config_errors)
    config_errors: []
  },
  capabilities: ["telemetry","restart_app","reboot","update_app","watchdog","kiosk","power"]
}
```

- Se `window.RD_NATIVE` **não** existir, o bundle está num navegador comum (modo web).
- Origem do bundle no Windows: `https://player.rd.local/` (mapeamento de pasta local do WebView2, contexto seguro: WebCrypto/OPFS/IndexedDB funcionam). **O backend deve aceitar essa origem em CORS e no `Origin` do WebSocket** (configurável; o host virtual pode ser mudado em `config.json → web.virtual_host`).
- Service Worker **não** é necessário no Windows (o app shell já é local). O bundle deve tolerar falha no `navigator.serviceWorker.register` (o WebView2 não garante SW em host virtual).

## 2. Transporte

`window.chrome.webview.postMessage(obj)` (JS→nativo) e `window.chrome.webview.addEventListener("message", ev => ev.data)` (nativo→JS). Mensagens são objetos JSON:

| Direção | Forma |
|---|---|
| JS → nativo (pedido) | `{"rd":1,"kind":"request","id":"<uuid>","method":"<nome>","params":{…}}` |
| nativo → JS (resposta) | `{"rd":1,"kind":"response","id":"<mesmo id>","ok":true,"result":{…}}` ou `{"rd":1,"kind":"response","id":"…","ok":false,"error":{"code":"…","message":"…"}}` |
| JS → nativo (notificação, sem resposta) | `{"rd":1,"kind":"notify","method":"<nome>","params":{…}}` |
| nativo → JS (evento) | `{"rd":1,"kind":"event","event":"<nome>","data":{…}}` |

Mensagens com `rd != 1`, sem `kind`, maiores que 64 KiB ou de origem diferente da origem do bundle são descartadas (a casca confere `CoreWebView2WebMessageReceivedEventArgs.Source`).

## 3. Métodos (request → response)

| method | params | result |
|---|---|---|
| `get_info` | `{}` | `{platform, shell_version, os, launched_by_watchdog, config:{…como em RD_NATIVE.config}, capabilities}` |
| `get_telemetry` | `{}` | Campos do `telemetry.data` do RDPP §4: `{cpu_pct, ram_used_mb, ram_total_mb, disk_free_mb, disk_total_mb, uptime_s, os, app_version, platform:"windows", resolution:"1920x1080", orientation:"LANDSCAPE"|"PORTRAIT", wifi:{ssid, rssi_dbm}|null, hdmi_connected:null}` — **sem IP**. O bundle completa/enviará pelo WS. |
| `command` | `{command_id, command, args}` com `command ∈ restart_app | reboot | update_app` | `{status:"ACCEPTED"|"DONE"|"FAILED"|"UNSUPPORTED", detail, delay_ms?}` — o bundle converte em `command_ack` (RDPP §4). `ACCEPTED` em `restart_app`/`reboot` significa "vai acontecer em `delay_ms`": envie o `command_ack` **antes** desse prazo. Comandos de negócio (`resync`, `clear_cache`, `redownload`, `reload_manifest`) são do bundle; a casca responde `UNSUPPORTED` se recebê-los. |
| `update_app` | `{access_token?, update?:{version,url,sha256,size,signature,key_id?,mandatory}, mode?:"check"|"apply"|"apply_now"}` | Responde em ≤ ~8 s (só valida o anúncio): `{status:"NO_UPDATE"|"AVAILABLE"(check)|"ACCEPTED"(download iniciado em segundo plano)|"REJECTED"|"FAILED", version?, detail}`. O andamento/resultado do download/aplicação chega pelo evento `update_status`. `apply` aplica se `update.auto_apply` (ou `mandatory`); `apply_now` sempre aplica (usado pelo comando remoto `update_app`). |
| `reload` | `{reason}` | `{status:"ACCEPTED"}` (recarrega o WebView) |

### 3.1 Atualização (RDPP §8)
- Se `update` vier preenchido, a casca usa esse objeto (o bundle já chamou `GET /api/v1/player/app-update?platform=windows&version=<shell_version>`). Senão a casca faz a chamada com `Authorization: Bearer <access_token>` (o token fica só em memória, nunca em log/disco).
- A casca **não confia no bundle**: verifica a assinatura ES256 com as chaves de atualização **fixadas** em `config.json → update.public_keys` e depois o SHA-256/tamanho do pacote baixado.
- **Mensagem assinada** (RDJ1, RDPP §0): `{"platform":"windows","purpose":"rd-app-update-v1","sha256":"<hex minúsculo>","size":<int>,"version":"<x.y.z>"}`. A `url` não é assinada (pode ser de CDN/assinada por requisição); a integridade vem do `sha256`.
- Download com `Authorization: Bearer` apenas se a URL for da mesma origem de `api_base_url`.
- Versão menor ou igual à atual é recusada (salvo `update.allow_downgrade`); versão que falhou na probação fica em lista negra local até surgir versão maior.

## 4. Notificações (JS → nativo)

| method | params | uso |
|---|---|---|
| `progress` | `{state:"PLAYING"|"IDLE"|"SYNCING"|"ERROR"|"FROZEN"|"UNPAIRED", creative_code?, item_started_at_ms?}` | **Obrigatória a cada ≤ 10 s** e a cada início de item. É o sinal de vida do bundle para o watchdog: sem `progress` por 30 s (após 90 s de carência inicial) a casca recarrega o WebView; 3 recargas sem recuperação em 10 min ⇒ reinício do processo. `FROZEN` reportado pelo próprio bundle também conta como falha. |
| `ready` | `{bundle_version}` | Bundle carregou e inicializou (usado na probação pós-atualização). |
| `tech_pin` | `{algo:"pbkdf2-sha256", iterations, salt_b64u, hash_b64u, set_at_ms}` (o MESMO registro do `set_tech_pin`, RDPP §4; **nunca** o PIN em claro — com `pin` a casca recusa) | (pós-janela, 2026-10-08) Enviar logo após aplicar `set_tech_pin` e uma vez após `ready` se houver registro. A casca guarda só o hash em `state\shell_state.json` e passa a usá-lo no menu técnico nativo. **Fonte de verdade única = servidor (painel)**; o PIN do `install.ps1` (`kiosk.tech_pin_hash`) é só semente até o 1º PIN do servidor. Registro mais antigo (`set_at_ms`) ou idêntico é ignorado; aceito só da origem do player. |
| `log` | `{level:"debug"|"info"|"warn"|"error", message}` | Vai para o log local da casca (mensagem truncada em 2 KB e com redação de tokens). **Não** envie segredos. |

## 5. Eventos (nativo → JS)

| event | data |
|---|---|
| `display_changed` | `{resolution, orientation}` |
| `power_resumed` | `{}` (retorno de suspensão — ressincronizar relógio/WS) |
| `update_status` | `{state:"DOWNLOADING"|"VERIFYING"|"STAGED"|"APPLYING"|"FAILED", version, detail}` durante o processo; após reiniciar na versão nova (ou voltar à anterior), no primeiro `ready`: `{state:"APPLIED"|"ROLLED_BACK"|"FAILED", version, detail}` (uma única vez). |
| `watchdog_reload` | `{reason}` (enviado após uma recarga forçada, para o bundle registrar) |

## 6. Erros

Códigos: `BAD_REQUEST`, `UNKNOWN_METHOD`, `UNSUPPORTED`, `POLICY_DENIED`, `UPDATE_KEYS_NOT_CONFIGURED`, `SIGNATURE_INVALID`, `HASH_MISMATCH`, `DOWNGRADE`, `NETWORK`, `INTERNAL`.
