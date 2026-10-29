# RD Player Protocol v1 (RDPP/1)

> Contrato ÚNICO entre backend e todos os players (Web, Windows, Android). Origem: AÇÃO 002 (2026-10-07). ADRs: `stack/bk/ADR_006_INVENTARIO_CAPACIDADE_CICLO_ANCORADO.md`, `stack/bk/ADR_007_PROTOCOLO_PLAYER_IDENTIDADE_CRIPTOGRAFICA.md`.
> Requisitos: ETAPA2 #159–#323, #359–#365, #590–#615, #1197–#1241, #1316–#1334, #1386–#1391; P001 §19–§20, §24, §30–§37, §58–§75; P002 itens 18–22, 50, 52.
> Conformidade: `protocol/vectors/*.json` (gerados pela implementação de referência do backend) DEVEM passar nos testes do backend (Python), do player web/Windows (TypeScript) e do Android (Kotlin). Mudança incompatível = nova versão do protocolo.

## 0. Convenções

- Tempo: inteiros em **milissegundos desde a época Unix (UTC)**, campos com sufixo `_ms`. Durações também em ms. Nunca float.
- IDs: UUID v4 em texto minúsculo. Códigos legíveis: criativo `RD-XXXXXX`, tela `TEL-UF-XXXX`, pareamento `XXXX-XXXX` (alfabeto `ABCDEFGHJMNPQRSTUVYZ23456789`).
- **JSON canônico RDJ1** (para assinar/hash): objeto serializado com chaves ordenadas por ponto de código, sem espaços (`,` e `:`), strings com escapes mínimos do JSON (`\"`, `\\`, `\b`, `\f`, `\n`, `\r`, `\t`, demais controles `\u00xx` minúsculo; não-ASCII sem escape), **somente inteiros** (proibido float), booleanos e `null`. Bytes assinados = UTF-8 do texto canônico. A ordenação de chaves é **por ponto de código Unicode** (normativo); difere do RFC 8785 (UTF-16) apenas para chaves fora do BMP, que o protocolo não usa — todas as chaves dos objetos RDPP são ASCII.
- Assinaturas: raw r‖s em base64url **sem padding** (`=` rejeitado); DER rejeitado; high-S aceito (não exigir low-S). Flags `SEQ_GAP`/`CHAIN_BREAK` apenas sinalizam (não invalidam sozinhas).
- Criptografia **ES256** = ECDSA P-256 + SHA-256. Assinatura em formato **bruto r‖s (64 bytes)**, base64url sem padding. Chave pública = SPKI DER em base64url sem padding.
- Envelope HTTP de erro: o do contrato de API (`{"error":{"code","message","details"}}`). Toda resposta de `/api/v1/player/*` traz o header `X-Server-Time-Ms`.
- Nenhum segredo, token ou chave em URL (exceção: `ticket` de WS de uso único, 30 s).

## 1. Identidade do dispositivo

1. Na primeira execução o player gera um par ES256 **não exportável** (Web/Windows-WebView2: WebCrypto `extractable:false` guardado em IndexedDB; Android: Android Keystore). O servidor só conhece a chave pública.
2. `device_id` (UUID) é atribuído pelo servidor no pareamento. Um dispositivo revogado nunca volta a ser válido; re-pareamento gera novo `device_id` e nova chave.

## 2. Pareamento

| Passo | Chamada |
|---|---|
| 2.1 Dispositivo cria pedido | `POST /api/v1/player/pairing/requests` `{"public_key":"<spki b64u>","key_alg":"ES256","platform":"web|windows|android","app_version":"1.0.0","device_info":{"model":"…","os":"…","resolution":"1920x1080"}}` → 201 `{"pairing_request_id","pairing_code":"ABCD-2345","expires_at_ms","panel_pair_url":"<url do painel>/telas/parear?code=ABCD-2345","ws_path":"/ws/player/pairing"}` |
| 2.2 Dispositivo aguarda | WS `ws(s)://<host>/ws/player/pairing?request_id=<id>` (sem segredo; só recebe `{"type":"pairing_claimed"}` ou `{"type":"pairing_expired"}`). Se o WS não estiver disponível: `GET /api/v1/player/pairing/requests/{id}` com backoff exponencial (≥ 5 s, máx. 60 s, com jitter) — retry, não polling contínuo. |
| 2.3a Admin/técnico no painel | `POST /api/v1/screens/{screen_id}/pairing` `{"pairing_code":"ABCD-2345"}` (permissão `screens.pair` no escopo da tela) |
| 2.3b Técnico no dispositivo (login controlado) | `POST /api/v1/player/pairing/requests/{id}/claim` `{"login","password","totp"?, "screen_code":"TEL-GO-7Q3M"}` (usuário com `screens.pair`; credenciais nunca armazenadas no dispositivo) |
| 2.4 Efeito | cria `Device` (chave pública, plataforma), vincula à tela, **revoga o dispositivo ativo anterior da mesma tela** (fecha o WS dele com `device_revoked`), auditoria, publica `pairing_claimed`. |
| 2.5 Dispositivo conclui | `GET /api/v1/player/pairing/requests/{id}/result` assinado (header `X-RD-Device-Signature` sobre RDJ1 `{"pairing_request_id","purpose":"rd-pairing-result-v1","ts_ms"}`) → `{"device_id","screen_id","screen_code","server_keys":[{"key_id","public_key","alg":"ES256"}]}` |

O QR exibido pelo player codifica `panel_pair_url` (URL base do painel vem da configuração do player; sem domínio hardcoded).

## 3. Autenticação do dispositivo

1. `POST /api/v1/player/auth/challenge` `{"device_id"}` → `{"challenge":"<32 bytes b64u>","expires_at_ms"}` (uso único, 60 s).
2. `POST /api/v1/player/auth/token` `{"device_id","challenge","signature"}` onde `signature` = ES256 sobre RDJ1 `{"challenge":"…","device_id":"…","purpose":"rd-player-auth-v1"}` → `{"access_token","expires_in":900,"server_time_ms"}`. JWT com `typ=device`, `sub=device_id`, `scr=screen_id`, `org=<org do estabelecimento>`, `sid`.
3. Dispositivo revogado → 401 `DEVICE_REVOKED` (player limpa credenciais e volta ao pareamento, mantendo a reprodução do cache? **Não**: revogado = parar de reproduzir conteúdo pago e exibir tela de pareamento; conteúdo é da RD).
4. Todas as rotas `/api/v1/player/*` (exceto pareamento/challenge/token) exigem `Authorization: Bearer <access_token>`.

## 4. WebSocket do player

- `POST /api/v1/player/ws-ticket` (Bearer) → `{"ticket","expires_in":30}`; conectar `ws(s)://<host>/ws/player?ticket=<ticket>`.
- Envelope: `{"type":"…","id":"<uuid>","ts_ms":<int>,"data":{…}}` (tipos em **minúsculas** com `_`).
- Servidor → player: `server_time {server_time_ms}`, `sync_required {version}`, `command {command_id, command, args, expires_at_ms}`, `kill {creative_ids:[…], reason}`, `device_revoked {}`, `flush_events {}`.
  - `command ∈ restart_app | resync | clear_cache | redownload | update_app | reboot | reload_manifest | set_tech_pin` (reboot só se a plataforma suportar; senão `command_ack` com `status:"UNSUPPORTED"`).
  - `set_tech_pin` (acrescentado em 2026-10-07 a pedido do player Android; restrito a admin/técnico, auditado): `args = {"algo":"pbkdf2-sha256","iterations":210000,"salt_b64u":"…","hash_b64u":"…"}` — o servidor NUNCA envia o PIN em claro; o player guarda o hash para o menu técnico/saída do kiosk.
- Player → servidor: `heartbeat` (a cada `settings.heartbeat_interval_ms`, padrão 30 000), `telemetry` (a cada `settings.telemetry_interval_ms`, padrão 300 000, ou em mudança relevante), `command_ack {command_id, status: RECEIVED|DONE|FAILED|UNSUPPORTED, detail}`, `manifest_applied {version}`, `manifest_failed {version, reason}`, `media_error {media_id, reason, attempts}`.
- `heartbeat.data`: `{"state":"PLAYING|IDLE|SYNCING|ERROR|FROZEN|UNPAIRED","current":{"creative_code","started_at_ms"}|null,"manifest_version_applied":42,"manifest_version_pending":43|null,"download":{"done_bytes","total_bytes"}|null,"last_sync_ms","last_play_ms","storage":{"free_bytes","used_bytes","persistent":true},"clock_offset_ms","alerts":["OFFLINE_7D"]}`.
- `telemetry.data`: `{"cpu_pct","ram_used_mb","ram_total_mb","disk_free_mb","disk_total_mb","uptime_s","os":"Android 11","app_version","platform","resolution":"1920x1080","orientation":"LANDSCAPE|PORTRAIT","wifi":{"ssid","rssi_dbm"}|null,"hdmi_connected":true|null}` — **sem IP**.
- Presença: o servidor guarda estado no Redis (TTL 90 s); banco só em transições (online↔offline, mudança de versão, erro) e flush coalescido. Estados exibidos: conectado, online, offline, sincronizando, sincronizado, reproduzindo, travado, erro.
- Reconexão: backoff exponencial com jitter (1 s → 60 s). Na reconexão o player busca o manifesto (`If-None-Match`) e envia o outbox de eventos.

## 5. Manifesto

`GET /api/v1/player/manifest` (Bearer; `If-None-Match: "<version>"`) → 304 ou 200:
```json
{"alg":"ES256","key_id":"srv-1","signature":"<b64u sobre RDJ1(manifest)>","manifest":{ … }}
```
Objeto `manifest` (todas as chaves obrigatórias; mapas podem ser vazios):
```json
{
  "schema": "rd.manifest/1",
  "manifest_id": "uuid", "version": 42, "screen_id": "uuid", "screen_code": "TEL-GO-7Q3M",
  "generated_at_ms": 0, "timezone": "America/Manaus",
  "rotation": {"internal_ms": 120000, "external_ms": 120000, "cycle_ms": 240000, "unit_ms": 30000, "policy_id": "uuid"},
  "anchor_ms": 1791380000000,
  "horizon": {"from_ms": 0, "to_ms": 0, "repeat_period_ms": 604800000},
  "settings": {"image_default_ms": 30000, "fade_ms": 400, "orientation": "LANDSCAPE",
               "footer": {"enabled": true, "height_permille": 60},
               "offline_alert_days": [7, 28], "heartbeat_interval_ms": 30000, "telemetry_interval_ms": 300000,
               "panel_url": "https://…"},
  "media": {"m1": {"media_id": "uuid", "sha256": "hex64", "size": 1234, "mime": "video/mp4", "width": 1920, "height": 1080, "duration_ms": 30000}},
  "creatives": {"c1": {"creative_id": "uuid", "code": "RD-A7M2P9", "kind": "EXTERNAL", "campaign_id": "uuid", "advertiser_id": "uuid",
                       "media": "m1", "duration_ms": 30000, "valid_from_ms": 0, "valid_until_ms": 1791999999999, "label": "Publicidade"}},
  "internal_pattern": [[{"c": "c7", "d": 60000}, {"c": "c8", "d": 60000}]],
  "fallback": [{"c": "f1", "d": 30000}],
  "external_plan": {"1791381600000": [{"i": "uuid", "c": "c1", "u": 0, "d": 30000}]},
  "emergency": [{"id": "uuid", "c": "c9", "from_ms": 0, "until_ms": 0}],
  "revoked": ["uuid-do-criativo"]
}
```
Regras do servidor (gerador ÚNICO; ver ADR_006):
- `cycle_ms` divide 3 600 000; `internal_ms` e `external_ms` múltiplos de `unit_ms` (30 000). Durações de criativo ∈ {30 000, 60 000, 120 000}.
- `external_plan`: chave = início do *bucket* `floor(t / cycle_ms) · cycle_ms` (UTC, em texto decimal); valor = entradas do bloco externo que começa dentro desse bucket; `u` = unidade inicial (0…E−1), `d` = duração. Unidades não vendidas já vêm preenchidas com conteúdo INTERNO (kind INTERNAL); nunca duas entradas de concorrentes no mesmo bloco; 120 s ocupa o bloco inteiro (2+2).
- Horizonte padrão 7 dias (`repeat_period_ms` = 7 dias). Gerado na publicação/alteração/kill, diariamente e sob demanda.
- Inclui somente criativos APROVADOS com mídia PRONTA (sha256 conhecido) e não revogados. `valid_until_ms` = 23:59:59.999 local do último dia contratado.
- `label`: EXTERNAL/INSTITUTIONAL → `"Publicidade"`; INTERNAL → `"Conteúdo do estabelecimento"`; FALLBACK → `"RD Mídia Indoor"`.

### 5.1 Execução normativa no player (tempo absoluto, determinística)
Seja `t` o horário corrigido (`relógio local + clock_offset_ms`), `A = anchor_ms`, `C = cycle_ms`, `I = internal_ms`, `U = unit_ms`.
1. Âncora: se `anchor_ms` for `null` e ainda não houver âncora local, no **primeiro start válido** (manifesto ativado e primeira mídia pronta) a âncora = `t` arredondado para baixo ao segundo; grava localmente e envia `POST /api/v1/player/anchor {"anchor_ms"}` → resposta `{"anchor_ms"}` (o servidor é write-once: se já existir, devolve o valor existente e o player adota). Reboot **nunca** recalcula a âncora.
2. `d = t − A`, `k = floor(d / C)`, `fase = d − k·C`.
3. Bloco INTERNO se `fase < I`: `início = A + k·C`; sequência = `internal_pattern[k mod len]` (se vazio → `fallback`); item j começa em `início + Σ d anteriores`.
4. Bloco EXTERNO caso contrário: `s = A + k·C + I`; `b = floor(s / C)·C`; se `b` fora de `[horizon.from_ms, horizon.to_ms)`, usar `b' = b − n·repeat_period_ms` (n mínimo que cai no horizonte); entradas = `external_plan[b']` (ausente → todo o bloco é fallback). Entrada `e` toca de `s + e.u·U` até `s + e.u·U + e.d`.
5. Uma entrada só é tocada se: criativo não revogado (lista `revoked` + comandos `kill` locais), `valid_from_ms ≤ t < valid_until_ms`, mídia presente e com sha256 verificado. Senão o intervalo é preenchido por `fallback` em unidades de 30 s.
6. Entrar no meio de um item (boot/reboot/troca): exibir fallback até a próxima fronteira de item; o item parcialmente perdido gera evento `SKIPPED` (sem validade para comissão).
7. `emergency` ativo (`from_ms ≤ t < until_ms`) preempta tudo; itens preemptados geram `SKIPPED`.
8. Troca de manifesto: só na fronteira de ciclo (`fase == 0`), após o manifesto novo estar 100% baixado/verificado (ver §6).
9. Vídeo nunca é cortado nem esticado; se a mídia for menor que `d` em até 1 000 ms, segura o último quadro; imagem permanece `d`. Pré-carregar o próximo item (double buffer), transição com fade de `fade_ms`, sem quadro preto.
10. Rodapé: tarja preta com texto branco `"<label> — <code>"`, altura `height_permille`/1000 da tela; a área útil do anúncio é reduzida para não ser coberta (letterbox, nunca esticar). Alertas no rodapé: sem sincronização há ≥ `offline_alert_days[0]` dias → aviso destacado no rodapé; ≥ `offline_alert_days[1]` dias → overlay central grande; ambos somem após sincronizar. Relógio inválido (offset > 120 s sem rede para corrigir, ou data anterior ao `generated_at_ms`) → alerta "Corrija a data e hora".

### 5.2 Regras normativas complementares (fechadas em 2026-10-07)
- **Função pura** `resolve(manifest, anchor_ms, local_state, t) → Segment`, onde `local_state = {revoked:[creative_id], media_ready:[media_key]}` e `Segment = {block: "INTERNAL"|"EXTERNAL"|"EMERGENCY", kind: "ITEM"|"FILLER", creative_key: string|null, start_ms, end_ms}`. É ela que os vetores testam.
- **Preenchimento por fallback** de um intervalo `[s,e)`: itens de `fallback` em ordem cíclica começando no índice `floor((s − A)/U) mod len(fallback)`; cada item só entra se couber inteiro (nunca cortar); quando o restante do intervalo for menor que o próximo item, esse restante é `FILLER` exibindo `fallback[índice]` **sem PoP**. `fallback` vazio ⇒ todo o intervalo é `FILLER` com `creative_key = null` (tela institucional embutida do player).
- `internal_pattern` vazio ⇒ bloco interno inteiro preenchido por fallback. Item interno que ultrapasse o fim do bloco não toca; o resto do bloco vai para fallback.
- **Validade** checada no INÍCIO do item: `valid_from_ms ≤ início < valid_until_ms`; mídia em `media_ready`; criativo fora de `revoked`. Item inválido ⇒ seu intervalo vai para fallback.
- **Entrada no meio de um ITEM** (boot, reboot, troca de manifesto, retorno de emergência): o player exibe `FILLER` (fallback) até o fim do item e registra evento `SKIPPED` do item (sem validade financeira). `resolve()` continua devolvendo o ITEM; a regra de meio de item é do runtime.
- **Emergência:** vale a primeira entrada da lista com `from_ms ≤ t < until_ms`; o criativo repete em passos de `duration_ms` a partir de `from_ms`; a última repetição que não couber até `until_ms` vira `FILLER`; itens normais (não-fallback) preemptados geram `SKIPPED`.
- **Horizonte:** `b` fora de `[horizon.from_ms, horizon.to_ms)` ⇒ `b' = b − n·repeat_period_ms` com o menor `|n|` que faça `b'` cair no intervalo (n ≥ 1 se `b ≥ to_ms`; n ≤ −1 se `b < from_ms`); se mesmo assim não cair ⇒ bloco externo inteiro em fallback. A validade dos itens é sempre checada contra o `t` real (não o `b'`).

## 6. Download e atualização atômica
Estados: `RECEIVED → VERIFY_SIG → PLAN → DOWNLOADING → VERIFY_SHA → STAGED → COMMIT → ACTIVE` (falha → mantém a versão ativa).
- Mídia: `GET /api/v1/player/media/{media_id}` (Bearer; autorizado só se a mídia pertence a um manifesto recente da tela) com `Range`/`If-Range` (ETag = sha256). Retomada por offset; SHA-256 incremental (nunca carregar o arquivo inteiro em memória); 2–3 downloads paralelos; backoff com jitter.
- Falha permanente de uma mídia (5 tentativas em ≤ 30 min): ativa a versão sem ela (entradas viram fallback) e envia `media_error`.
- Commit = troca atômica de um ponteiro de versão ativa; mantém a versão anterior para rollback; GC remove somente mídias fora de {ativa, staged, anterior} ou vencidas.
- Após ativar: `manifest_applied`.

## 7. Prova de exibição (PoP)

Evento (objeto assinado; todas as chaves presentes):
```json
{"schema":"rd.pop/1","playback_uuid":"uuid","device_id":"uuid","screen_id":"uuid",
 "creative_id":"uuid","creative_code":"RD-A7M2P9","campaign_id":"uuid|null","insertion_id":"uuid|null","kind":"EXTERNAL",
 "manifest_version":42,"started_at_ms":0,"finished_at_ms":0,"expected_ms":30000,"actual_ms":30000,
 "result":"COMPLETED|INTERRUPTED|ERROR|SKIPPED","seq":1017,"nonce":"<16 bytes b64u>",
 "prev_hash":"<sha256 hex do RDJ1 do evento anterior, ou 64 zeros>","clock_offset_ms":0,"offline":false}
```
- `actual_ms` medido por relógio **monotônico** entre início efetivo da exibição e o fim (vídeo: evento de fim do decodificador; imagem: permanência visível completa). `COMPLETED` somente se terminou integralmente.
- `seq` monotônico por dispositivo, persistido (nunca reinicia em reload/reboot).
- `started_at_ms` e `finished_at_ms` são SEMPRE em tempo **corrigido** (relógio local + `clock_offset_ms` vigente), com `finished_at_ms = started_at_ms + actual_ms` (monotônico). `clock_offset_ms` é **informativo** (auditoria/antifraude): o servidor **não** o soma novamente.
- Envio: `POST /api/v1/player/events:batch` `{"events":[{"event":{…},"signature":"<b64u ES256 sobre RDJ1(event)>"}]}` (≤ 200 por lote) → `{"results":[{"playback_uuid","status":"ACCEPTED|DUPLICATE|REJECTED","valid":true,"reason":null}],"ack_upto_seq":1017}`. O player remove do outbox apenas os eventos confirmados (ACCEPTED/DUPLICATE/REJECTED definitivos).
- Validação no servidor (válido para comissão somente se TODAS passarem): assinatura com a chave do dispositivo; dispositivo ativo e vinculado à tela no `started_at_ms`; `playback_uuid` inédito (idempotência: repetido → DUPLICATE, sem efeito); `result = COMPLETED` e `actual_ms ≥ expected_ms` (**sem tolerância**: 29 999/30 000 é inválido); criativo/campanha presentes no manifesto `manifest_version` daquela tela; campanha vigente em `started_at_ms`; `seq` maior que o último aceito (rollback de sequência → inválido) com lacunas sinalizadas; `nonce` inédito; relógio plausível (`started_at_ms` — já corrigido pelo player — não pode estar no futuro do servidor além de 120 s nem antes do pareamento); volume plausível (soma de `actual_ms` por dispositivo numa janela ≤ tempo decorrido; sobreposição de intervalos → inválido). Eventos offline **não** são invalidados por chegarem tarde.
- Retenção: eventos brutos 90 dias (configurável) após a apropriação financeira; agregados preservados.

## 8. Comandos, kill e atualização do app
- Comandos persistidos (emissor, motivo, auditoria, TTL); entregues por WS e reentregues na reconexão até `command_ack` final.
- Kill: persistido; entra em `revoked` do próximo manifesto; publicado imediatamente às telas afetadas; o player aplica na hora e persiste.
- `GET /api/v1/player/app-update?platform=…&version=…` → 204 ou `{"version","url","sha256","size","signature","key_id","mandatory":false}` (ondas/grupos controlados no servidor — sem atualizar todos de uma vez). `signature` = ES256 sobre RDJ1 `{"platform","purpose":"rd-app-update-v1","sha256","size","version"}` (a `url` fica fora da assinatura), feita com a **chave de release** (distinta da chave de manifesto), cujo SPKI é fixado na configuração do player (`update.public_keys`). O player só instala se sha256 e assinatura conferirem.
- Origens permitidas (CORS e checagem de `Origin` no WS do player) são configuráveis e incluem a origem do player web (`player.<domínio>`; local `http://localhost:8088`) e a origem virtual da casca Windows `https://player.rd.local`.

## 8.1 Ponte nativa (cascas que hospedam o web player)
Especificação `rd-native/1` em `player-web/BRIDGE.md` (proposta original: `player-windows/BRIDGE_PROPOSTA.md`): a casca injeta `window.RD_NATIVE = {bridge:"rd-native/1", platform, shell_version, config:{api_base_url, ws_base_url, panel_url, config_ok, …}, capabilities:[…]}` antes dos scripts; transporte `window.chrome.webview.postMessage`/evento `message` com envelopes `request/response/notify/event`; métodos `get_info`, `get_telemetry`, `command`, `update_app`, `reload`; o bundle notifica `ready` na inicialização e `progress` a cada ≤ 10 s (o watchdog recarrega o WebView após 30 s sem `progress`).

## 8.2 Esclarecimentos normativos (2026-10-07, perguntas dos implementadores)
1. Itens de `fallback` e o criativo de `emergency` **não** passam por checagem de validade/`media_ready`/`revoked` em `resolve()`; se a mídia faltar no runtime, o player mostra a tela institucional embutida, sem PoP.
2. Intervalos de fallback não se fundem: cada item inválido é um intervalo próprio, e cada lacuna máxima sem entradas no bloco externo é outro; o índice inicial do fallback é `floor((s − A)/U) mod len` em cada intervalo.
3. Emergência: a última repetição que não cabe até `until_ms` vira `FILLER` com `creative_key` = o próprio criativo da emergência (imagem mantida/último quadro), `block = "EMERGENCY"`, sem PoP; `duration_ms` = `creatives[c].duration_ms`.
4. Entrada de `external_plan` que ultrapasse o fim do bloco externo ou se sobreponha a outra entrada não toca; seu intervalo (recortado ao bloco) vai para fallback (o gerador do servidor nunca deve produzir isso — é defesa). Algoritmo exato: entradas processadas em ordem estável por `u`; um cursor marca o fim do último intervalo ocupado; entrada com início < cursor (sobreposição) ou fim > fim do bloco não toca e o intervalo `[max(início, cursor), min(fim, fim_do_bloco))` vira fallback; a PRIMEIRA entrada de um par sobreposto toca normalmente (só a posterior é rejeitada); entrada com início ≥ fim do bloco é ignorada; lacunas máximas entre entradas são intervalos de fallback próprios; `revoked` efetivo = `manifest.revoked ∪ local_state.revoked`.
5. Pareamento §2.5: headers `X-RD-Device-Ts-Ms: <int>` e `X-RD-Device-Signature: <b64u>` (assinatura sobre RDJ1 `{"pairing_request_id","purpose":"rd-pairing-result-v1","ts_ms"}`; servidor aceita ±120 s).
6. `GET /api/v1/player/pairing/requests/{id}` → `{"status":"PENDING|CLAIMED|EXPIRED","expires_at_ms"}`. Mensagens do WS de pareamento usam o envelope padrão `{type,id,ts_ms,data}` (`pairing_claimed`, `pairing_expired`).
7. Manifesto: resposta 200 com `ETag: "<version>"`; requisição com `If-None-Match: "<version>"` (com aspas).
8. Mídia: `ETag: "<sha256>"`; `If-Range: "<sha256>"` (com aspas).
9. PoP `SKIPPED`: `started_at_ms = finished_at_ms` = instante corrigido da detecção da perda, `actual_ms = 0`, `expected_ms = d` do item. Eventos são emitidos para todos os kinds tocados (EXTERNAL, INSTITUTIONAL, INTERNAL, FALLBACK); `FILLER` nunca gera PoP. Apenas EXTERNAL válido gera comissão.
10. Intervalos de PoP de itens consecutivos são contíguos e não sobrepostos (fim do anterior = início visível do seguinte; o crossfade conta para o seguinte).

## 9. Vetores de conformidade (`protocol/vectors/`)
Gerados pela implementação de referência do backend (Python); consumidos por Python, TypeScript e Kotlin. Todos os arquivos têm `{"version": 1, "generated_by": "...", "cases": [...]}`.
- `canonical_json.json`: `cases[] = {"name", "input": <valor JSON>, "canonical": "<texto RDJ1>"}` ou `{"name", "input", "error": "FLOAT_NOT_ALLOWED"}` (floats são representados na entrada como string marcada `{"$float": "1.5"}` para não depender do parser; a implementação deve rejeitar o float real).
- `es256_signatures.json`: `"test_key": {"note": "SOMENTE TESTE", "private_pkcs8_b64u", "public_spki_b64u"}`; `cases[] = {"name", "message": <objeto>, "canonical": "<RDJ1>", "signature_b64u", "public_spki_b64u", "valid": true|false}` (ECDSA não é determinístico: implementações VERIFICAM as assinaturas dadas e fazem round-trip próprio de assinar+verificar).
- `cycle_resolution.json`: `cases[] = {"name", "manifest": {…objeto manifest completo…}, "anchor_ms", "local_state": {"revoked": [], "media_ready": ["m1", …]}, "queries": [{"t_ms", "expected": {"block", "kind", "creative_key", "start_ms", "end_ms"}}]}`.
- `pop_validation.json` (backend; players usam apenas a parte de forma): `"device_key": {…teste…}`; `cases[] = {"name", "event": {…}, "canonical": "<RDJ1>", "signature_b64u", "context": {…estado do servidor…}, "expected": {"status": "ACCEPTED|DUPLICATE|REJECTED", "valid": true|false, "reason": "<código>"|null}}`.
