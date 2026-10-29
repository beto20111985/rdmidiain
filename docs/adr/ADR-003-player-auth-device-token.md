> **⚠ SUPERADO (AÇÃO 002, 2026-10-08).** O "device token" descrito aqui (segredo compartilhado/HMAC) foi removido na migration `0004` (dispositivos legados revogados). O modelo atual é o protocolo **RDPP/1**: identidade criptográfica do dispositivo (ES256, chave não extraível), desafio/assinatura → JWT de 15 min, ticket de WebSocket — `protocol/RD_PLAYER_PROTOCOL_v1.md`. Mantido só como histórico.

# ADR-003: Autenticação de Players via Device Token

**Status:** Aceito  
**Data:** 2026-10-07  
**Contexto:** Players (TVs/displays) precisam autenticar sem exposição de credenciais de usuário

## Decisão

Players autenticam via **device token** (`X-Device-Token` header ou `?token=` query param), separado do sistema JWT de usuários. O token é um `secrets.token_hex(32)` gerado no momento do pareamento (pairing).

## Justificativa

- Dispositivos físicos não têm usuário interativo — OAuth/OIDC seria overcomplexo.
- Device tokens são revogáveis individualmente sem afetar outros devices.
- Query param `?token=` necessário para URLs de mídia `<img src>` e `<video src>`, que não permitem headers customizados.
- HMAC-SHA256 sobre eventos de playback garante integridade sem chave privada no device.

## Consequências

- `device_token` e `device_secret` armazenados em `localStorage` do navegador do player.
- Rotação de token: novo pareamento revoga o token anterior.
- Rate limiting por `$http_x_device_token` no nginx (zone `device`, 120 req/min).
- Players offline ficam em cache de manifesto no localStorage — sincronizam na reconexão via `POST /screens/{id}/sync`.

## Segurança

- Device secrets nunca trafegam pela rede após o pareamento inicial.
- HMAC assina: `playback_uuid:creative_id:started_at:sequence_number`.
- Validação assíncrona pelo worker `process_pending_playback` a cada 2 minutos.
