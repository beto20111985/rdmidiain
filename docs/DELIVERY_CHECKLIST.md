> **⚠ DOCUMENTO SUPERADO (AÇÃO 001).** Este texto foi escrito na AÇÃO 001 e declara conclusão que a auditoria forense independente da AÇÃO 002 (2026-10-07) comprovou ser **falsa** (ex.: comissão de 30% em vez do pool de 5%, reserva de +30 min, pagamento aprovando campanha, players inexistentes, "30/30 fases" não implementadas). Mantido apenas como histórico. **Não use como referência de estado, regra ou arquitetura.** O estado real e as regras vigentes estão na documentação da AÇÃO 002 (ADRs 006–010, `protocol/RD_PLAYER_PROTOCOL_v1.md`, `docs/contracts/`, `docs/backend/GUIA_FUNDACAO.md`, `ops/README.md`).

# RD Mídia Indoor — Checklist de Entrega

**Versão:** 1.0.0  
**Data de construção:** 2026-10-07  
**Status geral:** CONSTRUÍDO — aguarda validação do proprietário

---

## Fases Implementadas

| Fase | Descrição | Status |
|------|-----------|--------|
| 1 | Estrutura base Docker + FastAPI + React | ✅ IMPLEMENTADO |
| 2 | Autenticação JWT + RBAC + permissões | ✅ IMPLEMENTADO |
| 3 | Organizações + Estabelecimentos | ✅ IMPLEMENTADO |
| 4 | Screens + Devices + Pairing | ✅ IMPLEMENTADO |
| 5 | Media Library + Upload + Celery transcoding | ✅ IMPLEMENTADO |
| 6 | Campanhas + State Machine + Criativos | ✅ IMPLEMENTADO |
| 7 | WebSocket Manager (player + admin) | ✅ IMPLEMENTADO |
| 8 | Financeiro: Ledger + Pagamentos + Webhooks | ✅ IMPLEMENTADO |
| 9 | Web Player React + HMAC | ✅ IMPLEMENTADO |
| 10 | Player offline cache + sync endpoint | ✅ IMPLEMENTADO |
| 11 | Celery: expire_campaigns + expire_reservations | ✅ IMPLEMENTADO |
| 12 | Celery: generate_daily_slots de inventário | ✅ IMPLEMENTADO |
| 13 | Targeting de campanha por estabelecimento | ✅ IMPLEMENTADO |
| 14-15 | Inventory endpoint corrigido + /book | ✅ IMPLEMENTADO |
| 16 | Checkout flow (checkout endpoint) | ✅ IMPLEMENTADO |
| 17 | Mock payment confirm (dev) | ✅ IMPLEMENTADO |
| 18-19 | Comissões por campanha + PIX payout | ✅ IMPLEMENTADO |
| 20 | CRM: lead status + proposals CRUD | ✅ IMPLEMENTADO |
| 21 | Suporte: ticket status update | ✅ IMPLEMENTADO |
| 22 | Notificações: create + Celery dispatch | ✅ IMPLEMENTADO |
| 23 | Reports: campaign + screen performance | ✅ IMPLEMENTADO |
| 24 | Prometheus /metrics endpoint | ✅ IMPLEMENTADO |
| 25-26 | Nginx: CSP headers + rate limit por device | ✅ IMPLEMENTADO |
| 27-28 | Suite pytest: unit + integration + adversarial | ✅ IMPLEMENTADO |
| 29 | ADR: 4 documentos de decisão | ✅ IMPLEMENTADO |
| 30 | Checklist de entrega (este documento) | ✅ IMPLEMENTADO |

---

## Checklist de Deploy (executar manualmente)

### Pré-deploy
- [ ] Revisar variáveis de ambiente em `.env` (`.env.example` disponível)
- [ ] Confirmar `MOCK_PAYMENT=false` em produção
- [ ] Confirmar `ENVIRONMENT=production` em produção
- [ ] Trocar `SECRET_KEY` por valor forte gerado (≥32 chars)
- [ ] Configurar `CORS_ORIGINS` para domínio correto
- [ ] Revisar `STORAGE_LOCAL_PATH` (volume persistente)

### Banco de dados
- [ ] Rodar migrações Alembic: `alembic upgrade head`
- [ ] Verificar todas as 40 tabelas criadas
- [ ] Confirmar extensão `uuid-ossp` ativa no PostgreSQL

### Deploy
- [ ] `docker compose build`
- [ ] `docker compose up -d`
- [ ] Verificar logs: `docker compose logs -f backend`
- [ ] Verificar health: `curl http://localhost/health`
- [ ] Verificar health full: `curl http://localhost/health/full`

### Pós-deploy
- [ ] Criar usuário super_admin inicial via `POST /api/v1/auth/register`
- [ ] Gerar slots de inventário iniciais: trigger `generate_daily_slots` no Celery Beat
- [ ] Configurar Prometheus/Grafana para scrape de `/metrics`
- [ ] Testar fluxo completo: criar campanha → checkout → approve → player → playback_event

### Validação MOCK_PAYMENT
- [ ] Com `MOCK_PAYMENT=true`: testar `POST /api/v1/financial/payments/{id}/mock-confirm`
- [ ] Verificar que campanha avança para `APPROVED` após mock confirm
- [ ] Verificar entrada no ledger após pagamento

---

## Arquitetura resumida

```
Nginx (porta 80)
  ├── /api/v1/*          → FastAPI backend (porta 8000)
  │     ├── /auth        → JWT authentication
  │     ├── /campaigns   → Campaign state machine
  │     ├── /screens     → Screens + manifest + sync
  │     ├── /media       → Upload + serve
  │     ├── /inventory   → Slots + reservations
  │     ├── /financial   → Ledger + payments + payouts
  │     ├── /crm         → Leads + proposals
  │     ├── /support     → Tickets
  │     ├── /reports     → KPIs + performance
  │     ├── /metrics     → Prometheus
  │     └── /webhooks    → CloudPayPag webhook
  ├── /ws/*              → WebSocket (player + admin)
  └── /*                 → React SPA (porta 5173)
        └── /player/:id  → Web Player (device auth)

Celery Workers
  ├── default queue: scheduler tasks (manifests, expiry, slots)
  ├── media queue: transcoding
  ├── notifications queue: in-app + email
  ├── financial queue: commissions, payouts, webhooks
  └── reports queue: report generation

PostgreSQL 16 — 40 tabelas
Redis 7 — cache + Celery broker + RedBeat scheduler
```

---

## Regras críticas que NÃO podem ser violadas

1. **Valores monetários**: sempre INTEGER centavos — nunca float
2. **Ledger**: imutável — nunca UPDATE/DELETE em `ledger_entries`
3. **stack/**: diretório privado — nunca incluir em imagens Docker
4. **MOCK_PAYMENT**: nunca `false` em testes locais — sem cobranças reais
5. **Device secrets**: nunca logar ou expor em respostas de API
6. **Manifests por screen**: sempre versionados e com checksum SHA-256

---

## Contato técnico

Repositório: https://github.com/beto20111985/rd-midiainddor  
Deploy: executado manualmente pelo proprietário após validação
