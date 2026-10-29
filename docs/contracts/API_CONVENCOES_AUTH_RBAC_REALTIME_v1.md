# RD Mídia Indoor — Contrato de API v1: convenções, autenticação, RBAC e tempo real

> Origem: AÇÃO 002 (2026-10-07). Fonte única para backend (FastAPI) e frontend (React). Qualquer mudança aqui exige atualizar os dois lados e os testes de contrato.
> Requisitos: ETAPA2 #41–#73, #616–#633, #820–#829, #857–#870, #4144–#4175; P001 §70, §83–§84, §93–§99; P002 itens 10, 16, 17, 52.
> **AÇÃO 003 (2026-10-08, ADR_011 / `stack/bk/003_SPEC_IMPLEMENTACAO.md`)**: primeiro acesso com guard no backend (§2.2), `GET /me.security_setup` (§3), `super_admin` reservado aos assentos (§3.3), segurança da instância e modo de aprovação ÚNICA × DUPLA (§6), evento `security.status_changed` (§5).

## 1. Convenções gerais

- Base: `/api/v1`. JSON UTF-8. Datas/horas em ISO-8601 **UTC** com sufixo `Z` nas respostas; o frontend converte para o timezone exibido (padrão America/Sao_Paulo, ou o do estabelecimento quando o dado for de um estabelecimento).
- Dinheiro: sempre inteiro em centavos, campos com sufixo `_cents` (nunca float). Percentuais: inteiros em basis points com sufixo `_bps` (500 = 5,00%) ou milésimos com sufixo `_milli` para pesos (1000 = 1,0).
- IDs internos: UUID. IDs públicos legíveis: ver `ID_PUBLICOS` abaixo.
- Erro (todas as rotas):
  ```json
  {"error": {"code": "CAMPAIGN_INVALID_TRANSITION", "message": "Mensagem em pt-BR para o usuário.", "details": {}}}
  ```
  Códigos HTTP: 400 validação de negócio, 401 não autenticado (inclui token ausente — nunca 403 para ausência de credencial), 403 sem permissão/escopo, 404 inexistente **ou fora do escopo** (não vazar existência), 409 conflito/concorrência/idempotência divergente, 422 schema inválido (formato Pydantic convertido para o envelope acima), 423 recurso bloqueado (ex.: saque bloqueado), 429 rate limit, 5xx erro interno (sem stack trace).
- Paginação: `?page=1&page_size=25` (máx 100) → `{"items": [...], "total": 123, "page": 1, "page_size": 25}`. Ordenação `?sort=-created_at`.
- Idempotência de operações sensíveis (checkout, saque, aprovação, comandos): header `Idempotency-Key: <uuid>`; repetição com o mesmo corpo devolve o mesmo resultado; com corpo diferente → 409.
- Nunca enviar token/segredo em query string (exceção única: `ticket` de WebSocket de uso único, ver §5).
- OpenAPI: `/api/v1/openapi.json` (desabilitado em produção por configuração).

### ID_PUBLICOS
| Entidade | Formato | Regra |
|---|---|---|
| Criativo (anúncio) | `RD-XXXXXX` | 6 caracteres aleatórios do alfabeto seguro `ABCDEFGHJMNPQRSTUVYZ23456789` (sem L, I, X, W, O, K, 0, 1); único; nunca muda; novo criativo/versão relevante = novo ID |
| Tela | `TEL-<UF>-XXXX` (ex.: `TEL-GO-7Q3M`) | XXXX do alfabeto seguro |
| Código de pareamento | `XXXX-XXXX` | alfabeto seguro, expira em 15 min |

## 2. Autenticação (usuários humanos)

Modelo: **access token JWT curto (15 min) mantido apenas em memória no frontend** + **refresh token opaco em cookie** `rd_refresh` (`HttpOnly; SameSite=Strict; Path=/api/v1/auth; Secure` quando `COOKIE_SECURE=true` — obrigatório em produção). Refresh com **rotação** e **detecção de reuso** (reuso revoga a família inteira de sessões). Access token contém `sid` (id da sessão) e é rejeitado se a sessão estiver revogada (checagem em Redis com fallback no banco).

CSRF: as rotas que usam o cookie (`/auth/refresh`, `/auth/logout`) exigem header `X-RD-CSRF: 1` **e** `Origin` permitido (CORS restritivo). As demais rotas usam `Authorization: Bearer` (imunes a CSRF).

| Método | Rota | Corpo | Resposta |
|---|---|---|---|
| POST | `/auth/login` | `{"login": "email ou telefone", "password": "..."}` | 200 `{"status":"ok","access_token","expires_in","user"}` + Set-Cookie; ou 200 `{"status":"mfa_required","mfa_token","expires_in":300}`; ou 200 `{"status":"mfa_setup_required","mfa_setup_token","expires_in":600,"user","setup": SetupState}` (perfil com 2FA obrigatório sem TOTP; sem cookie); 401 credenciais inválidas (mensagem genérica); 423 conta bloqueada temporariamente (`details.retry_after_s`); 429 |
| POST | `/auth/mfa/verify` | `{"mfa_token","code"}` (TOTP 6 dígitos ou código de recuperação) | igual ao login ok; 401 código inválido; anti-replay: o mesmo passo TOTP nunca é aceito duas vezes |
| POST | `/auth/refresh` | — (cookie + `X-RD-CSRF`) | 200 `{"access_token","expires_in"}` + novo cookie; 401 |
| POST | `/auth/logout` | — | 204; revoga a sessão atual |
| POST | `/auth/logout-all` | — (Bearer) | 204; revoga todas as sessões do usuário |
| GET | `/auth/sessions` | — | lista de sessões: `id, created_at, last_seen_at, ip, user_agent, device_label, current` |
| DELETE | `/auth/sessions/{id}` | — | 204 |
| POST | `/auth/password/forgot` | `{"email"}` | 202 sempre (sem enumeração); envia código de 6 dígitos por e-mail (válido 15 min, uso único) |
| POST | `/auth/password/reset` | `{"email","code","new_password"}` | 204; revoga todas as sessões |
| POST | `/auth/password/change` | `{"current_password","new_password"}` | 204 |
| POST | `/auth/invites/accept` | `{"token","name","password"}` | 200 como login ok (ou `mfa_setup_required` com `setup`); convite de `super_admin` (anterior à AÇÃO 003) ⇒ 400 `INVITE_INVALID` |
| POST | `/auth/mfa/totp/setup` | `{"password"}` | 200 `{"otpauth_uri","secret"}` — QR gerado **localmente** no frontend (nunca serviço de terceiros); setup não altera 2FA ativo; **403 `PASSWORD_CHANGE_REQUIRED`** enquanto a troca de senha estiver pendente (AÇÃO 003) |
| POST | `/auth/mfa/totp/enable` | `{"code"}` | 200 `{"recovery_codes":[...]}` (exibidos uma única vez); com token `mfa_setup` também emite a sessão (`access_token`, cookie); 403 `PASSWORD_CHANGE_REQUIRED` como acima |
| POST | `/auth/mfa/totp/disable` | `{"password","code"}` | 204 (proibido para perfis com 2FA obrigatório) |

Política: senha mínima 10 caracteres com letras e números, não pode conter o e-mail; bloqueio progressivo por conta (5 falhas → 15 min; contadores persistidos fora da transação da request); 2FA **obrigatório** para Super Admin, Admin e Financeiro (login devolve `mfa_setup_required` enquanto não configurado e só libera as rotas de setup); CAPTCHA: interface `CaptchaVerifier` acionada após 3 falhas — provedor não configurado localmente (dependência externa registrada).

Independência dos Admin Master (P001 §84; ADR_011 D7): nenhuma rota permite que um usuário altere senha, e-mail, 2FA, sessões, senha financeira ou `is_active` de um Admin Master que não seja ele mesmo (nenhuma rota de fatores/sessões recebe id de outro usuário); Admin Master não é visível em detalhes sensíveis para o outro (apenas nome e status), não é gerenciável por `/users` nem impersonável; no banco (trigger), o papel de runtime **não** muda assento, `is_active`, e-mail, anonimização nem `master_recovered_at` de titular, **não desliga nem re-chaveia** um TOTP já ativo e **não apaga** a senha financeira — mas **consegue** gravar `hashed_password`, `must_change_password` e uma senha financeira nova (são as escritas legítimas do próprio titular). Por isso a credencial do titular é **selada** (`users.master_credential_seal`, HMAC da aplicação sobre usuário, assento, e-mail, hash da senha, troca obrigatória, TOTP, segredo TOTP cifrado, hash da senha financeira e `is_active`; AÇÃO 003 — D1): escrita direta sem a chave da aplicação deixa o selo inválido e o titular passa a ser recusado com **401 genérico** (login antes de conferir a senha, `mfa/verify`, `refresh`, toda request autenticada, step-up/senha financeira, reset de senha) + alerta de integridade; o banco exige selo novo no mesmo UPDATE que muda um campo selado.

### 2.1 Impersonation
`POST /auth/impersonation` `{"user_id","reason"}` (permissão `users.impersonate`, motivo ≥ 10 caracteres) → access token com claims `imp_by` (usuário real) e `imp_id`; `DELETE /auth/impersonation` encerra. Auditoria de início/fim e de toda ação feita durante a impersonation registra usuário real e impersonado. Proibido impersonar Admin Master ou outro admin. O frontend exibe banner fixo enquanto `impersonation.active`.

### 2.2 Primeiro acesso (AÇÃO 003 — ADR_011 D6)
Passos **bloqueantes**, nesta ordem, calculados no servidor: `PASSWORD_CHANGE` (`must_change_password`) → `TOTP` (perfil com 2FA obrigatório sem TOTP) → `FINANCIAL_PASSWORD` (titular de assento de Admin Master sem senha financeira). Passo **não bloqueante**: `SECOND_MASTER` (modo DUAL, titular de assento, enquanto não houver 2 Masters ativos).

**Guard** (`get_auth_context`, negação por padrão): com passo bloqueante, toda rota responde 403 `PASSWORD_CHANGE_REQUIRED` | `MFA_SETUP_REQUIRED` | `FINANCIAL_PASSWORD_SETUP_REQUIRED` com `details.next_step` (inclusive `POST /realtime/ticket` ⇒ sem WebSocket). Liberadas: `GET/PATCH /me`, `/me/consents`, `/me/data-export`, `POST /me/deletion-request`, `POST /auth/logout|logout-all|refresh`, `/auth/sessions*`, `POST /auth/password/change`, `DELETE /auth/impersonation`, `POST /auth/mfa/totp/disable` + as rotas de setup abaixo. O teste `tests/unit/test_route_guard_inventory.py` falha se surgir rota fora do guard e fora das listas explícitas. O frontend só melhora a UX.

| Método | Rota | Auth | Corpo | Resposta / erros |
|---|---|---|---|---|
| GET | `/auth/setup/status` | token `mfa_setup` **ou** access (lenient) | — | 200 `SetupState`; 401 |
| POST | `/auth/setup/password` | token `mfa_setup` ou access | `{"current_password","new_password"}` | com token de setup: 200 `{"status":"mfa_setup_required","mfa_setup_token","expires_in","setup"}` (NOVO token; o anterior deixa de valer — o token de setup é vinculado ao hash da senha atual); com access: 200 `{"status":"ok","setup"}` (sessão atual mantida, demais revogadas). 409 `PASSWORD_CHANGE_NOT_REQUIRED`; 400 `INVALID_PASSWORD` (conta no lockout); 400 `PASSWORD_POLICY`; 400 `PASSWORD_REUSED`; 423 |
| POST | `/auth/setup/financial-password` | access; só se o passo atual for `FINANCIAL_PASSWORD` | `{"login_password","totp_code","financial_password"}` | 200 `{"status":"ok","setup"}`; 409 `SETUP_STEP_OUT_OF_ORDER` (`details.next_step`); 403 `FINANCIAL_STEP_UP_INVALID`; 400 `FINANCIAL_PASSWORD_POLICY` (8–128, diferente da senha de login); 423 `FINANCIAL_STEP_UP_LOCKED` |

`SetupState` (mesmo formato em `/auth/setup/status`, `GET /me.security_setup` e no `setup` do login):
```json
{"required": true, "next_step": "PASSWORD_CHANGE" | "TOTP" | "FINANCIAL_PASSWORD" | "SECOND_MASTER" | null,
 "steps": {"password_change": "PENDING|DONE", "totp": "PENDING|DONE|NOT_REQUIRED",
           "financial_password": "PENDING|DONE|NOT_REQUIRED",
           "second_master": "NOT_REQUIRED|NOT_INVITED|INVITE_PENDING|OTHER_MASTER_SETUP_PENDING|DONE"},
 "is_admin_master": true, "approval_mode": "SINGLE" | "DUAL" | null, "dual_approval_ready": true | false | null}
```
`required` = há passo bloqueante; `next_step` = primeiro bloqueante, senão `SECOND_MASTER` se `second_master` ∈ {`NOT_INVITED`,`INVITE_PENDING`,`OTHER_MASTER_SETUP_PENDING`}, senão `null`. `is_admin_master` = titular de assento (mesmo com setup pendente). `approval_mode` só para titular (demais `null`); `dual_approval_ready` só no modo DUAL (`null` no SINGLE e para não titulares). `second_master` = `DONE` também quando o OUTRO assento já é de um Master ativo (o setup do próprio é coberto pelos passos bloqueantes). Nunca contém segredo, token ou e-mail completo de terceiros.

### 2.3 Ativação do 2º Admin Master (pública; AÇÃO 003 — ADR_011 D7)
O convite é criado pelo Master ativo em `POST /security/masters/invites` (§6). O link `{PANEL_BASE_URL}/ativar-master#token=<token>` vai **somente** ao e-mail do convidado (fragmento: não chega a logs de servidor); o token nunca aparece na API, na auditoria ou em logs (banco guarda só SHA-256). Rate limit `master_activation_ip` 20/15 min por IP; Redis fora ⇒ 503.

| Método | Rota | Corpo | Resposta / erros |
|---|---|---|---|
| POST | `/auth/master-activation/inspect` | `{"token"}` | 200 `{"full_name","email_masked","expires_at","instance_name","steps":["PASSWORD","TOTP","FINANCIAL_PASSWORD"]}`; 400 `MASTER_INVITE_INVALID` (genérico: inexistente/expirado/revogado/aceito/bloqueado); 429; 503 |
| POST | `/auth/master-activation/totp` | `{"token"}` | 200 `{"otpauth_uri","secret"}` (gera e SUBSTITUI o segredo pendente do convite; QR local); 400 `MASTER_INVITE_INVALID` |
| POST | `/auth/master-activation/complete` | `{"token","password","totp_code","financial_password"}` | 200 = login ok (`status`,`access_token`,`expires_in`,`user`) + `recovery_codes` (uma vez) + Set-Cookie `rd_refresh`; sessão `amr=["pwd","otp"]`. 400 `MASTER_INVITE_INVALID`; 400 `PASSWORD_POLICY`; 400 `FINANCIAL_PASSWORD_POLICY` (inclui igual à senha de login); 400 `MFA_SETUP_NOT_STARTED` (TOTP não iniciado); 400 `INVALID_MFA_CODE` (+1 tentativa em transação independente; 5ª ⇒ convite `LOCKED` e 400 `MASTER_INVITE_LOCKED`); 409 `ADMIN_MASTER_SEATS_FULL`; 409 `EMAIL_ALREADY_REGISTERED` |

Todo convite carrega `seal` (HMAC da aplicação sobre id, assento, e-mail, convidante, `token_hash` e validade — AÇÃO 003 — D1): convite inserido direto no banco não é aceito por `inspect`/`totp`/`complete` (400 `MASTER_INVITE_INVALID` genérico + alerta) nem re-selado pelo reenvio (503 `SECURITY_INTEGRITY_ALERT`); `complete` também exige o selo de credencial válido do convidante (senão 401 `MASTER_INVITE_INVALID`). Operações concorrentes sobre o convite (ativação × reenvio/revogação/TOTP) seguem a mesma ordem de locks; deadlock/serialização/espera de lock ⇒ 409 `CONCURRENT_OPERATION` com `Retry-After: 1` (nunca 500).

`complete` é ATÔMICO: cria o usuário com a PRÓPRIA senha, TOTP e senha financeira do convidado e só então a função `rd_security_assign_master_seat(..., 'MASTER_INVITE', ...)` atribui o assento (exige os três fatores) — não existe "Master sem TOTP". Ativação a partir do mesmo IP **e** User-Agent da sessão mais recente do convidante ⇒ `same_device_as_inviter=true` na auditoria `admin_master.activated` + notificação de alerta aos dois.

## 3. Identidade, permissões e contextos

`GET /me` →
```json
{
  "user": {"id","name","email","phone","mfa_enabled","is_admin_master","theme"},
  "permissions": ["campaigns.view", "..."],
  "assignments": [{"role":"establishment_owner","scope_type":"ORGANIZATION","scope_id":"<uuid>","scope_label":"Mercado X"}],
  "contexts": [
    {"key":"rd","label":"RD Mídia Indoor","kind":"PLATFORM"},
    {"key":"est:<org_id>","label":"Minha unidade — Mercado X","kind":"ESTABLISHMENT","org_id":"<uuid>"},
    {"key":"adv:<org_id>","label":"Minhas campanhas — Mercado X","kind":"ADVERTISER","org_id":"<uuid>"}
  ],
  "impersonation": {"active": false, "real_user": null},
  "mfa": {"required": true, "configured": true},
  "must_change_password": false,
  "security_setup": { /* SetupState (§2.2) */ }
}
```
`GET /me` sem Bearer → 401 (o frontend faz `POST /auth/refresh` no boot e só então chama `/me`).
`PATCH /me` `{"theme":"dark"|"light"|"system", "name"?, "phone"?}` → 200 `{"user":{…}}` (preferências do próprio usuário; e-mail/senha/2FA têm rotas próprias).
`GET /notifications/unread-count` → `{"count": n}` (invalidado pelo evento WS `notification.created`; implementado pelo módulo de notificações).

O frontend envia o contexto ativo em `X-RD-Context: est:<org_id>` (apenas filtro de UX; **a autorização é sempre recalculada no servidor**).

### 3.1 Catálogo de permissões (códigos `recurso.ação`)
`dashboard.view` · `organizations.view|manage` · `users.view|manage|impersonate` · `roles.manage` · `establishments.view|manage` · `screens.view|manage|pair|command` · `media.view|upload|publish|delete` · `playlists.view|reorder|manage` · `campaigns.view|manage|approve|kill` · `creatives.view|manage` · `taxonomy.manage` · `conflicts.override` · `inventory.view|reserve` · `pricing.view|manage` · `checkout.create` · `payments.view` · `finance.view|manage|approve|reconcile` · `commissions.view|adjust` · `withdrawals.view|request` · `pix_keys.manage` · `crm.view|manage|discount_approve` · `design.view|work|request|approve` · `templates.manage` · `support.view|create|manage` · `equipment.view|manage` · `maintenance.manage` · `reports.view|export|financial` · `notifications.manage` · `settings.view|manage` · `audit.view|export` · `control_plane.view|manage` · `whitelabel.manage` · `lgpd.manage`.

Regras: o catálogo vive em código (`backend/app/core/permissions.py`) e é sincronizado com o banco; teste automatizado garante que todo código usado em rotas existe no catálogo e que todo perfil padrão referencia apenas códigos válidos. `finance.approve` só é efetivo para os dois Admin Master.

### 3.2 Escopos
Atribuição = (perfil, `scope_type`, `scope_id`/`scope_value`). `scope_type ∈ GLOBAL | ORGANIZATION | ESTABLISHMENT | CITY` (CITY usa código IBGE ou `UF:cidade-normalizada`). Toda rota resolve o escopo do recurso alvo (organização, estabelecimento, cidade) e checa permissão **naquele escopo**; listagens filtram pelos escopos concedidos. Recurso fora do escopo → 404.

### 3.3 Perfis padrão (templates editáveis)
| Perfil | Escopo típico | Permissões principais |
|---|---|---|
| `super_admin` | GLOBAL | todas; **exclusivo dos dois Admin Master (assentos 1 e 2)** — concedido somente pelas funções de assento (bootstrap/convite); `POST /auth/invites` e `POST /users/{id}/assignments` com `super_admin` ⇒ 403 `ROLE_RESERVED_FOR_ADMIN_MASTER` (AÇÃO 003, ADR_011 D9) |
| `admin` | GLOBAL | operação completa, `finance.view`, sem `finance.approve` |
| `employee` | GLOBAL/CITY | operação (estabelecimentos, telas, mídia, campanhas, suporte) sem financeiro e **sem `screens.command`** (sem ações remotas no player) |
| `financial` | GLOBAL | `finance.*` exceto approve, `payments.view`, `commissions.*`, `withdrawals.view`, `reports.financial` |
| `designer` | GLOBAL | `design.view|work`, `templates.manage`, `media.view|upload` |
| `sales` | GLOBAL/CITY | `crm.*` (sem discount_approve), `pricing.view`, `organizations.view|manage` |
| `technician` | GLOBAL/CITY | `equipment.*`, `maintenance.manage`, `screens.view|pair|command`, `support.view|manage` |
| `establishment_owner` | ORGANIZATION | `dashboard.view`, `establishments.view`, `screens.view`, `playlists.reorder`, `commissions.view`, `withdrawals.view|request`, `reports.view`, `support.create` |
| `advertiser` | ORGANIZATION | `campaigns.view|manage`, `creatives.view|manage`, `checkout.create`, `payments.view`, `design.request|approve`, `reports.view`, `support.create` |
| `agency` | várias ORGANIZATION | igual a `advertiser` em cada organização cliente atribuída |

**Ações remotas no player** (reiniciar app, reiniciar dispositivo, ressincronizar, limpar cache, baixar novamente, atualizar versão, PIN técnico — `POST /screens/{id}/commands`, `/screens/{id}/tech-pin`, `/establishments/{id}/tech-pin`) exigem `screens.command`, concedida **somente** a `super_admin`, `admin` e `technician` (no escopo da atribuição, ex.: CITY do técnico). São sensíveis: confirmação explícita para reboot/limpar cache/atualizar versão e auditoria de cada comando (E2 #1386–#1391; REQ-RBAC-006 — pós-janela AÇÃO 002). Interromper criativo (`campaigns.kill`) é ação de conteúdo, fora deste grupo.

## 4. Auditoria
Toda ação relevante grava `audit_log` (append-only, protegido por trigger; encadeamento por hash): `actor_user_id`, `real_user_id` (impersonation), `action`, `entity_type`, `entity_id`, `before`, `after`, `reason`, `ip`, `user_agent`, `origin` (api|worker|player|system), `request_id`, `created_at`. `GET /audit` (`audit.view`) com filtros; `GET /audit/export` (`audit.export`).

## 5. Tempo real (painel)
- `POST /realtime/ticket` (Bearer) → `{"ticket","expires_in":30}` (uso único, 30 s, guardado no Redis).
- `GET ws(s)://<host>/ws/admin?ticket=<ticket>`.
- Envelope servidor→cliente: `{"type":"screen.status","id":"<uuid>","ts":"<iso>","data":{...}}`. Cliente→servidor: `{"type":"ping"}` / `{"type":"subscribe","topics":[...]}`.
- Tipos: `screen.status`, `screen.sync`, `campaign.updated`, `approval.pending`, `payment.updated`, `media.processing`, `inventory.updated`, `notification.created`, `finance.approval_request`, `withdrawal.updated`, `ticket.updated`, `designer.updated`, `security.status_changed` (AÇÃO 003: entregue ao `user_id` de cada titular de assento após transição de modo, convite criado/revogado/expirado/bloqueado, ativação, recuperação e senha financeira do setup; `data: {"reason"}`; o frontend invalida `me` e as queries de `/security/*`).
- Entrega filtrada no servidor pelo escopo do usuário (nunca vazar eventos de outra organização).
- Arquitetura: Redis Pub/Sub (qualquer processo — API ou worker Celery — publica; cada processo da API repassa às suas conexões). Sem sessão de banco presa à conexão.
- O frontend **não faz polling**: cada evento atualiza/invalida a query correspondente do TanStack Query; na reconexão (backoff exponencial com jitter) faz um único refetch das queries ativas.

## 6. Segurança da instância (AÇÃO 003 — ADR_011; SPEC `stack/bk/003_SPEC_IMPLEMENTACAO.md` §5.2–§7)

Modo de aprovação financeira **por instância**: `SINGLE` (aprovação única: o próprio Master autoriza com TOTP + senha financeira + código por e-mail) ou `DUAL` (A solicita → B aprova). Fonte da verdade: trilha append-only selada `instance_approval_mode_history` (modo efetivo = maior `mode_version`). `RD_BOOTSTRAP_DUAL_MASTER_APPROVAL` só é lida pelo `bootstrap-instance` na 1ª inicialização; depois é ignorada (divergência sinalizada). **Não há switch**: mudança só por pedido `APPROVAL_MODE_CHANGE` no motor de aprovações (SINGLE→DUAL: autorização única com fatores; DUAL→SINGLE: A solicita com step-up, B aprova com 3 fatores; impossível com o 2º Master pendente).

| Método | Rota | Auth | Corpo | Resposta / erros |
|---|---|---|---|---|
| GET | `/security/approval-mode` | `settings.view` ou `finance.view` GLOBAL | — | 200 `{"mode","mode_version","initialized_at","initialized_source","dual_approval_ready" (bool no DUAL; null no SINGLE),"active_masters","pending_change_request": {"id","target_mode","requested_by_name","requested_at","expires_at"}\|null,"history":[{"mode_version","kind","from_mode","to_mode","source","requested_by_name","authorized_by_name","reason","approval_request_id","created_at"}]\|null,"integrity":{"ok"}\|null,"env":{"divergence","unsupported_var_present"}\|null}` — `history`/`integrity`/`env` só para titular de assento ou `audit.view` GLOBAL; 503 `INSTANCE_NOT_INITIALIZED` |
| GET | `/security/masters` | titular de assento (setup completo pelo guard) | — | 200 `{"approval_mode","seats":[{"seat","name","status":"ACTIVE"\|"SETUP_PENDING"\|"VACANT","is_self"}],"invite": Invite\|null,"can_invite"}` (nunca e-mail completo do outro Master); 403 `ADMIN_MASTER_REQUIRED` |
| POST | `/security/masters/invites` | Master ATIVO, sem impersonation | `{"full_name","email","totp_code"}` | 201 `Invite` (SEM token); 409 `APPROVAL_MODE_SINGLE`; 409 `ADMIN_MASTER_SEATS_FULL`; 409 `MASTER_INVITE_ALREADY_PENDING` (inclusive sob concorrência); 409 `EMAIL_ALREADY_REGISTERED`; 400 `EMAIL_INVALID`; 400 `INVALID_MFA_CODE` (TOTP do convidante; conta no lockout); 403 `ADMIN_MASTER_REQUIRED` |
| POST | `/security/masters/invites/{id}/resend` | Master ativo | — | 200 `{"sent":true,"expires_at"}` (novo token, o anterior deixa de valer; validade 72 h limitada a 7 dias desde a criação); 404; 429 `MASTER_INVITE_RATE_LIMITED` (3/h por convite; máx. 5 envios) |
| DELETE | `/security/masters/invites/{id}` | Master ativo | `{"reason"?}` | 204 (`REVOKED`); 404 |
| GET | `/security/integrity` | titular de assento ou `audit.view` GLOBAL | — | 200 `{"ok","problems":["…"]}` (descrições sem segredo) |

`Invite` = `{"id","seat","full_name","email_masked","status","expires_at","invited_by_name","sent_count"}`. Validade 72 h; um convite `PENDING` por vez (UNIQUE parcial + trigger + lock); vencidos viram `EXPIRED` (auditados) ao listar/criar; 5 TOTP errados na ativação ⇒ `LOCKED`.

**Aprovações financeiras (agente C; `/financial/approvals`; SPEC §4.2/§5.3).** Todas exigem `finance.approve` GLOBAL (só Masters; sem impersonation), salvo leitura (`finance.view`). Todo pedido nasce com o snapshot `approval_mode` (= modo efetivo; imutável) e só é decidido nesse modo.

| Método | Caminho | Regras / corpo | Erros novos |
|---|---|---|---|
| GET | `/actions` | 200 `{"approval_mode": "SINGLE"\|"DUAL"\|null, "items": [{"action_type","label","pix_out","is_mode_change"}]}` — inclui `APPROVAL_MODE_CHANGE` ("mudança do modo de aprovação financeira") | — |
| POST | `/financial/approvals` (criar) | `{"action_type","payload","reason", "step_up"?: {"totp_code","financial_password"}}` — `step_up` obrigatório só para `APPROVAL_MODE_CHANGE` em DUAL (ignorado nos demais casos). `APPROVAL_MODE_CHANGE`: `payload` **somente** `{"target_mode":"SINGLE"\|"DUAL"}` (o servidor grava `{"target_mode","from_mode","expected_version"}`), `reason` ≥ 20, `amount_cents` null. Em DUAL notifica o outro Master; em SINGLE não. | 409 `SECOND_MASTER_REQUIRED`; 400 `APPROVAL_MODE_CHANGE_INVALID` (alvo = modo atual, alvo inválido, chaves extras); 400 `REASON_REQUIRED`; 403 `FINANCIAL_STEP_UP_INVALID`; 503 `SECURITY_INTEGRITY_ALERT`/`INSTANCE_NOT_INITIALIZED` |
| POST | `/{id}/email-code` | DUAL: só o **não** solicitante, finalidade `approval`; SINGLE: só o **solicitante**, finalidade `self_authorization`. O código vale só para (usuário, finalidade, pedido): código de outro pedido ou de outra finalidade ⇒ `FINANCIAL_EMAIL_CODE_INVALID`. | 403 `SELF_APPROVAL_FORBIDDEN` (DUAL); 403 `ONLY_REQUESTER_AUTHORIZES` (SINGLE) |
| POST | `/{id}/approve` | **DUAL**: o OUTRO Master, `{"totp_code","financial_password","payload_sha256","email_code"}` | 409 `APPROVAL_MODE_SINGLE`; 409 `APPROVAL_MODE_CHANGED`; 409 `SECOND_MASTER_REQUIRED`; 423 `MASTER_RECENTLY_RECOVERED`; 503 |
| POST | `/{id}/authorize` | **NOVA — SINGLE**: só o próprio solicitante; mesmo corpo do `approve` (código de finalidade `self_authorization`). Grava `self_authorized_by/at`; `approved_by` fica NULL; evento `SELF_AUTHORIZED`; auditoria `finance.self_authorized` com `factors`. | 409 `APPROVAL_MODE_DUAL`; 403 `ONLY_REQUESTER_AUTHORIZES`; 409 `APPROVAL_NOT_PENDING`/`APPROVAL_EXPIRED`/`APPROVAL_PAYLOAD_MISMATCH`/`APPROVAL_MODE_CHANGED`; 403 `FINANCIAL_STEP_UP_INVALID`/`FINANCIAL_EMAIL_CODE_REQUIRED`/`FINANCIAL_EMAIL_CODE_INVALID`; 423 `MASTER_RECENTLY_RECOVERED`/`FINANCIAL_STEP_UP_LOCKED`; 503 |
| POST | `/{id}/reject` | só DUAL (no SINGLE o solicitante usa `/{id}/cancel`) | 409 `APPROVAL_MODE_SINGLE` |

Objeto do pedido (lista/detalhe/respostas) ganha: `approval_mode`, `is_mode_change`, `self_authorized_by`, `self_authorized_by_name`, `self_authorized_at`, `can_authorize` (SINGLE ∧ viewer = solicitante ∧ `PENDING`), `can_approve` (DUAL ∧ viewer ≠ solicitante ∧ `PENDING`). Execução de `APPROVAL_MODE_CHANGE`: `execution_state="EXECUTED"`, `execution_ref="mode_version:<n>"`; na mesma transação todo pedido `PENDING` do modo antigo vira **status `CANCELLED`** com **evento** `CANCELLED_MODE_CHANGE` (auditoria `finance.approval_cancelled_mode_change`; notificação `finance.approval_decided` ao solicitante). Criação/aprovação/autorização usam lock consultivo compartilhado; `APPROVAL_MODE_CHANGE`, exclusivo (nenhuma autorização atravessa uma troca). Lançamentos gerados na execução: `metadata = {"authorization_mode","requested_by","authorized_by"}`. Configurações `financial=True` só mudam pela execução de um `SETTING_CHANGE` autorizado no modo da instância (chamada direta ⇒ 409 `SETTING_REQUIRES_DUAL_APPROVAL`, mensagem "…aprovação única ou dupla, conforme a instância"). Recusas dos guards do banco viram 409 (nunca 500): `RD_SECOND_MASTER_REQUIRED`→`SECOND_MASTER_REQUIRED`, `RD_APPROVAL_MODE_MISMATCH`→`APPROVAL_MODE_CHANGED`, `RD_APPROVER_NOT_ACTIVE_MASTER`→`APPROVER_NOT_ACTIVE_MASTER`, `RD_SELF_AUTHORIZER_INVALID`→`SELF_AUTHORIZER_INVALID`, `RD_REQUESTER_NOT_ACTIVE_MASTER`→`REQUESTER_NOT_ACTIVE_MASTER` (estes três só com estado inconsistente; o frontend exibe a mensagem do servidor). `MASTER_RECENTLY_RECOVERED` (423, `details.blocked_until`): step-up financeiro bloqueado por `finance.password_reset_cooldown_hours` após `recover-master`. Saques do estabelecimento: inalterados (não dependem do modo).

**Prontidão financeira** (toda criação/aprovação/autorização): lock consultivo do modo → integridade (selos das trilhas + titulares) → DUAL com < 2 Masters ativos ⇒ 409 `SECOND_MASTER_REQUIRED` `details={"approval_mode":"DUAL","second_master":"NOT_INVITED|INVITE_PENDING|OTHER_MASTER_SETUP_PENDING"}`. Falha de integridade ⇒ 503 `SECURITY_INTEGRITY_ALERT` **somente** nas operações financeiras (o resto do sistema funciona), auditoria + notificação crítica + métrica.

Códigos de erro novos (pt-BR, envelope §1): `FINANCIAL_PASSWORD_SETUP_REQUIRED` 403 · `SETUP_STEP_OUT_OF_ORDER` 409 · `PASSWORD_CHANGE_NOT_REQUIRED` 409 · `ROLE_RESERVED_FOR_ADMIN_MASTER` 403 · `MASTER_INVITE_ALREADY_PENDING` 409 · `ADMIN_MASTER_SEATS_FULL` 409 · `APPROVAL_MODE_SINGLE` 409 · `APPROVAL_MODE_DUAL` 409 · `APPROVAL_MODE_CHANGED` 409 · `APPROVAL_MODE_CHANGE_INVALID` 400 · `ONLY_REQUESTER_AUTHORIZES` 403 · `SECOND_MASTER_REQUIRED` 409 · `EMAIL_ALREADY_REGISTERED` 409 · `MASTER_INVITE_INVALID` 400 · `MASTER_INVITE_LOCKED` 400 · `MASTER_INVITE_RATE_LIMITED` 429 · `MASTER_RECENTLY_RECOVERED` 423 · `INSTANCE_NOT_INITIALIZED` 503 · `SECURITY_INTEGRITY_ALERT` 503 · `ADMIN_MASTER_REQUIRED` 403 · `APPROVER_NOT_ACTIVE_MASTER` 409 · `SELF_AUTHORIZER_INVALID` 409 · `REQUESTER_NOT_ACTIVE_MASTER` 409 · `CONCURRENT_OPERATION` 409 (`Retry-After`; deadlock/serialização/espera de lock nas operações de convite/ativação). Selo de credencial do titular inválido ⇒ a resposta é a MESMA de credencial inválida da rota (`INVALID_CREDENTIALS`/`INVALID_TOKEN`/`INVALID_REFRESH`/`INVALID_MFA_TOKEN` 401); o motivo fica só na auditoria `security.master_credential_seal_invalid`. Erros do banco `RD_MASTER_CREDENTIAL_SEAL_REQUIRED`/`RD_MASTER_INVITE_SEAL_REQUIRED` ⇒ 503 `SECURITY_INTEGRITY_ALERT`.

Integridade (`verify_integrity`, `GET /security/integrity`, `security-verify`, beat, toda operação financeira): cadeias seladas do modo e dos assentos, titular de cada assento = último evento, **selo de credencial de cada titular** (NULL = adoção pendente: o serviço `bootstrap` adota uma única vez — TOFU — com auditoria `security.master_credential_seal_adopted`) e **selo de cada convite PENDING**. Qualquer falha ⇒ 503 `SECURITY_INTEGRITY_ALERT` no dinheiro, `security-verify` = 1, `rd_security_integrity_ok` = 0.

Notificações (críticas, equipe): `security.second_master_activated`, `security.master_activation_same_device`, `security.approval_mode_changed` (dedupe por versão), `security.master_recovered` (todos os titulares), `security.master_seat_assigned` (CLI), `security.integrity_alert` (dedupe diário), `security.env_divergence` (só IN_APP). Métricas Prometheus (`/metrics`): `rd_security_approval_mode{mode}`, `rd_security_approval_mode_version`, `rd_security_active_masters`, `rd_security_integrity_ok`, `rd_security_env_divergence` (calculadas pelo beat a cada 15 min, no startup e sob demanda se o snapshot sumir).

CLI (operador, shell no host): `bootstrap-instance` (serviço one-shot `bootstrap`), `bootstrap-masters --seat 1|2` (restrito; sem `--force-reassign`), `recover-master --seat N --reason "…"` (mesmo titular; fatores zerados; auditado e notificado), `security-verify` (0/1); `set-password`/`lock-password` recusam titulares de assento.
