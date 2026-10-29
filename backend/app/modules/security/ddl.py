"""
RD Mídia Indoor — DDL de segurança da instância (AÇÃO 003 / ADR_011; SPEC §2.3–§2.5).

Consumido por ``app.db.ddl`` (registro ``DOMAIN_DDL_MODULES``; o harness de testes aplica após
``create_all``) e pela migration ``0007`` (``statements(tabelas)``, ``revoke_statements(rd_app)``;
downgrade: ``drop_statements()``). Statements IDEMPOTENTES, sem ``:nome``.

Garantias no BANCO (o papel de runtime ``rd_app`` não as contorna):
1. Trilhas ``instance_approval_mode_history`` (modo efetivo = maior ``mode_version``) e
   ``admin_master_seat_events``: append-only (``app.db.ddl.APPEND_ONLY_TABLES``) e SEM
   INSERT/UPDATE/DELETE/TRUNCATE para o ``rd_app`` — só as funções ``SECURITY DEFINER`` abaixo
   gravam, conferindo a máquina de estados e o encadeamento de selos sob advisory lock.
2. ``rd_users_master_guard``: titular de assento não muda de assento/id/e-mail/ativação/anonimização/
   ``master_recovered_at``, não desliga nem re-chaveia o TOTP e não perde a senha financeira; ninguém
   insere usuário já com assento — exceto o dono das tabelas/superusuário (``rd_security_privileged``),
   isto é, as funções definer e as migrations.
3. ``rd_role_assignments_master_guard``: o ``super_admin`` de um titular não é removido/alterado.
4. ``rd_admin_master_invite_guard``: convite nasce PENDING (assento vago, convidante Master ativo,
   modo DUAL), colunas de identidade imutáveis, estado final imutável, ``ACCEPTED`` só pela função
   definer; DELETE/TRUNCATE proibidos. Um PENDING por assento: índice único parcial.
5. ``RAISE LOG 'RD_SECURITY_EVENT …'`` (log do PostgreSQL, fora do alcance do ``rd_app``) em toda
   escrita das funções definer e em toda mudança relevante de usuário titular — só ids/assento/modo/
   versão/nomes de colunas, NUNCA hash, selo ou segredo.

6. 0009 (D1 do revisor): selos da aplicação — ``admin_master_invites.seal`` (obrigatório no INSERT do papel de
   runtime; muda junto com ``token_hash``) e ``users.master_credential_seal`` (titular: não volta a NULL; muda junto
   com hash/fatores/e-mail/ativação/assento). O banco NÃO confere o HMAC (não tem a chave): exige o rastro; quem
   confere é a aplicação (login, ativação, ``verify_integrity``). Corpos vigentes: ``*_V3``.

Endurecimento: toda referência a tabela é qualificada (``public.`` / ``pg_catalog.``) e as funções
definer usam ``search_path = pg_catalog, public, pg_temp`` — tabela temporária criada pelo ``rd_app``
nunca "sombreia" uma tabela real (o ``pg_temp`` é pesquisado PRIMEIRO quando não listado).

Erros: ``RAISE EXCEPTION 'RD_<CODIGO>: …' USING ERRCODE = 'P0001'`` (códigos em ``ERROR_CODES``).
Advisory locks (ordem global de aquisição: BOOTSTRAP → APPROVAL_MODE → MASTER_SEATS).
"""
from __future__ import annotations

from typing import Iterable, Optional

LOCK_APPROVAL_MODE = 7240030001
LOCK_MASTER_SEATS = 7240030002
LOCK_BOOTSTRAP = 7240030003

TRAIL_TABLES = ("instance_approval_mode_history", "admin_master_seat_events")
SECURITY_TABLES = ("admin_master_invites",) + TRAIL_TABLES

# Funções SECURITY DEFINER (assinaturas para REVOKE/GRANT/DROP)
DEFINER_FUNCTIONS = (
    "rd_security_init_approval_mode(text, text, text, text)",
    "rd_security_apply_mode_transition(uuid, text, text)",
    "rd_security_assign_master_seat(uuid, smallint, text, uuid, uuid, text, text)",
    "rd_security_recover_master(uuid, text, uuid, text, text)",
)
READ_FUNCTIONS = (
    "rd_security_privileged()",
    "rd_security_current_approval_mode()",
    "rd_is_active_master(uuid)",
    "rd_active_master_count()",
)
TRIGGER_FUNCTIONS = (
    "rd_users_master_guard()",
    "rd_users_master_log()",
    "rd_role_assignments_master_guard()",
    "rd_admin_master_invite_guard()",
)

# Códigos levantados pelo banco (contrato com B/C — mapeamento para a API em approval_mode.map_db_error)
ERROR_CODES = (
    "RD_INSTANCE_NOT_INITIALIZED",      # modo de aprovação ainda não inicializado
    "RD_SEAL_CHAIN_MISMATCH",           # p_prev_seal ≠ cabeça atual da cadeia
    "RD_SEAL_INVALID",                  # selo fora do formato (64 hex minúsculos)
    "RD_APPROVAL_MODE_INVALID",         # modo/origem/env inválidos na inicialização
    "RD_APPROVAL_MODE_MISMATCH",        # pedido de outro modo / versão desatualizada (modo mudou)
    "RD_MODE_CHANGE_INVALID",           # pedido APPROVAL_MODE_CHANGE inválido (ação, status, execução, alvo, motivo, evento)
    "RD_MODE_CHANGE_REUSED",            # pedido já usado numa transição
    "RD_APPROVER_NOT_ACTIVE_MASTER",    # DUAL: aprovador ausente/igual ao solicitante/não-Master ativo
    "RD_SELF_AUTHORIZER_INVALID",       # SINGLE: autorizador ≠ solicitante ou não-Master ativo
    "RD_REQUESTER_NOT_ACTIVE_MASTER",   # solicitante não é Master ativo
    "RD_SECOND_MASTER_REQUIRED",        # DUAL com menos de 2 Masters ativos
    "RD_APPROVAL_INVALID_STATE",        # pedido inserido com status ≠ PENDING/APPROVED
    "RD_MASTER_SEAT_INVALID",           # assento/origem/parâmetros inválidos
    "RD_MASTER_SEAT_OCCUPIED",          # assento já ocupado
    "RD_MASTER_SEAT_INVALID_STATE",     # pré-condição da origem não atendida (bootstrap, CLI, adoção)
    "RD_MASTER_SEAT_USER_INVALID",      # usuário inexistente/inativo/anonimizado/sem fatores
    "RD_MASTER_INVITE_INVALID",         # convite inexistente/não pendente/expirado/de outro usuário
    "RD_MASTER_INVITE_INVALID_STATE",   # trigger do convite: estado/transição inválidos
    "RD_MASTER_INVITE_IMMUTABLE",       # identidade do convite imutável
    "RD_MASTER_RECOVERY_INVALID",       # recuperação de quem não é titular / hash inválido
    "RD_ROLE_MISSING",                  # perfil super_admin inexistente (catálogo não sincronizado)
    "RD_MASTER_SEAT_PROTECTED",         # assento só pelas funções de segurança
    "RD_MASTER_PROTECTED",              # id/e-mail/ativação/anonimização/recuperação de titular; DELETE de titular
    "RD_MASTER_TOTP_PROTECTED",         # desligar/re-chavear TOTP de titular
    "RD_MASTER_FINANCIAL_PASSWORD_PROTECTED",  # apagar senha financeira de titular
    "RD_MASTER_ROLE_PROTECTED",         # remover/alterar super_admin de titular
    "RD_APPEND_ONLY",                   # trilhas/convites: UPDATE/DELETE/TRUNCATE proibidos
    "RD_MASTER_INVITE_SEAL_REQUIRED",   # 0009: convite sem selo / token e selo não mudaram juntos / selo removido
    "RD_MASTER_CREDENTIAL_SEAL_REQUIRED",  # 0009: credencial de titular mudou sem novo selo / selo removido
)

_SEAL_RE = "^[0-9a-f]{64}$"
_GENESIS_SQL = "repeat('0', 64)"
_CTX = "session_user, current_setting('application_name', true), inet_client_addr()"

# Predicado único de "Master ativo" (SPEC §2.3) — alias ``u`` para ``public.users``.
ACTIVE_MASTER_PREDICATE = (
    "u.admin_master_seat IS NOT NULL AND u.is_active AND u.anonymized_at IS NULL AND u.totp_enabled"
    " AND u.financial_password_hash IS NOT NULL AND NOT u.must_change_password"
    " AND EXISTS (SELECT 1 FROM public.role_assignments ra JOIN public.roles r ON r.id = ra.role_id"
    " WHERE ra.user_id = u.id AND r.slug = 'super_admin' AND ra.scope_type = 'GLOBAL'"
    " AND (ra.expires_at IS NULL OR ra.expires_at > now()))"
)

# ─── Leitura (SECURITY INVOKER, STABLE) ──────────────────────────────────────

PRIVILEGED_FN = """
CREATE OR REPLACE FUNCTION rd_security_privileged() RETURNS boolean
LANGUAGE sql STABLE AS $$
    SELECT current_user = (SELECT pg_catalog.pg_get_userbyid(c.relowner)
                             FROM pg_catalog.pg_class c WHERE c.oid = 'public.users'::regclass)
        OR COALESCE((SELECT r.rolsuper FROM pg_catalog.pg_roles r WHERE r.rolname = current_user), false)
$$
"""

CURRENT_MODE_FN = """
CREATE OR REPLACE FUNCTION rd_security_current_approval_mode() RETURNS text
LANGUAGE sql STABLE AS $$
    SELECT h.to_mode::text FROM public.instance_approval_mode_history h ORDER BY h.mode_version DESC LIMIT 1
$$
"""

IS_ACTIVE_MASTER_FN = f"""
CREATE OR REPLACE FUNCTION rd_is_active_master(p_user uuid) RETURNS boolean
LANGUAGE sql STABLE AS $$
    SELECT EXISTS (SELECT 1 FROM public.users u WHERE u.id = p_user AND {ACTIVE_MASTER_PREDICATE})
$$
"""

ACTIVE_MASTER_COUNT_FN = f"""
CREATE OR REPLACE FUNCTION rd_active_master_count() RETURNS integer
LANGUAGE sql STABLE AS $$
    SELECT count(*)::integer FROM public.users u WHERE {ACTIVE_MASTER_PREDICATE}
$$
"""

# ─── Escrita (SECURITY DEFINER) ──────────────────────────────────────────────

INIT_MODE_FN = f"""
CREATE OR REPLACE FUNCTION rd_security_init_approval_mode(p_mode text, p_source text, p_env_value text,
                                                          p_app_seal text)
RETURNS TABLE(approval_mode text, mode_version integer, created boolean)
LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, public, pg_temp AS $$
#variable_conflict use_column
DECLARE
    cur record;
BEGIN
    PERFORM pg_catalog.pg_advisory_xact_lock({LOCK_APPROVAL_MODE});
    SELECT h.to_mode, h.mode_version INTO cur
      FROM public.instance_approval_mode_history h ORDER BY h.mode_version DESC LIMIT 1;
    IF FOUND THEN
        -- idempotente: já inicializado ⇒ estado atual, nada gravado
        approval_mode := cur.to_mode;
        mode_version := cur.mode_version;
        created := false;
        RETURN NEXT;
        RETURN;
    END IF;
    IF p_mode IS NULL OR p_mode NOT IN ('SINGLE','DUAL') THEN
        RAISE EXCEPTION 'RD_APPROVAL_MODE_INVALID: modo % inválido', p_mode USING ERRCODE = 'P0001';
    END IF;
    IF p_source IS NULL OR p_source NOT IN ('BOOTSTRAP_ENV','LEGACY_DUAL_ADOPTED') THEN
        RAISE EXCEPTION 'RD_APPROVAL_MODE_INVALID: origem % inválida', p_source USING ERRCODE = 'P0001';
    END IF;
    IF p_source = 'LEGACY_DUAL_ADOPTED' AND p_mode <> 'DUAL' THEN
        RAISE EXCEPTION 'RD_APPROVAL_MODE_INVALID: instalação legada só é adotada em DUAL' USING ERRCODE = 'P0001';
    END IF;
    IF p_env_value IS NOT NULL AND p_env_value NOT IN ('true','false') THEN
        RAISE EXCEPTION 'RD_APPROVAL_MODE_INVALID: valor normalizado do ENV deve ser true, false ou NULL'
            USING ERRCODE = 'P0001';
    END IF;
    IF p_app_seal IS NULL OR p_app_seal !~ '{_SEAL_RE}' THEN
        RAISE EXCEPTION 'RD_SEAL_INVALID: selo fora do formato' USING ERRCODE = 'P0001';
    END IF;
    INSERT INTO public.instance_approval_mode_history
        (mode_version, from_mode, to_mode, kind, source, approval_request_id, requested_by, authorized_by,
         reason, env_value, prev_seal, app_seal)
    VALUES (1, NULL, p_mode, 'INITIALIZED', p_source, NULL, NULL, NULL, NULL, p_env_value, {_GENESIS_SQL},
            p_app_seal);
    RAISE LOG 'RD_SECURITY_EVENT approval_mode_initialized mode=% version=1 source=% by=% app=% addr=%',
        p_mode, p_source, {_CTX};
    approval_mode := p_mode;
    mode_version := 1;
    created := true;
    RETURN NEXT;
END;
$$
"""

APPLY_TRANSITION_FN = f"""
CREATE OR REPLACE FUNCTION rd_security_apply_mode_transition(p_request_id uuid, p_prev_seal text,
                                                             p_app_seal text)
RETURNS integer
LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, public, pg_temp AS $$
DECLARE
    cur record;
    req record;
    v_target text;
    v_expected text;
    v_kind text;
    v_auth uuid;
    v_new integer;
BEGIN
    PERFORM pg_catalog.pg_advisory_xact_lock({LOCK_APPROVAL_MODE});
    SELECT h.mode_version, h.to_mode, h.app_seal INTO cur
      FROM public.instance_approval_mode_history h ORDER BY h.mode_version DESC LIMIT 1;
    IF NOT FOUND THEN
        RAISE EXCEPTION 'RD_INSTANCE_NOT_INITIALIZED: modo de aprovação não inicializado' USING ERRCODE = 'P0001';
    END IF;
    IF p_prev_seal IS DISTINCT FROM cur.app_seal THEN
        RAISE EXCEPTION 'RD_SEAL_CHAIN_MISMATCH: selo anterior não confere com a versão %', cur.mode_version
            USING ERRCODE = 'P0001';
    END IF;
    IF p_app_seal IS NULL OR p_app_seal !~ '{_SEAL_RE}' THEN
        RAISE EXCEPTION 'RD_SEAL_INVALID: selo fora do formato' USING ERRCODE = 'P0001';
    END IF;
    SELECT r.id, r.action_type, r.status, r.execution_state, r.approval_mode, r.payload, r.reason,
           r.requested_by, r.approved_by, r.self_authorized_by
      INTO req
      FROM public.financial_approval_requests r WHERE r.id = p_request_id FOR UPDATE;
    IF NOT FOUND THEN
        RAISE EXCEPTION 'RD_MODE_CHANGE_INVALID: pedido % inexistente', p_request_id USING ERRCODE = 'P0001';
    END IF;
    IF req.action_type <> 'APPROVAL_MODE_CHANGE' THEN
        RAISE EXCEPTION 'RD_MODE_CHANGE_INVALID: pedido % não é APPROVAL_MODE_CHANGE', p_request_id
            USING ERRCODE = 'P0001';
    END IF;
    IF req.status <> 'APPROVED' THEN
        RAISE EXCEPTION 'RD_MODE_CHANGE_INVALID: pedido % não está APPROVED (%)', p_request_id, req.status
            USING ERRCODE = 'P0001';
    END IF;
    IF req.execution_state <> 'NOT_STARTED' THEN
        RAISE EXCEPTION 'RD_MODE_CHANGE_INVALID: pedido % já executado/em execução (%)', p_request_id,
            req.execution_state USING ERRCODE = 'P0001';
    END IF;
    IF EXISTS (SELECT 1 FROM public.instance_approval_mode_history h WHERE h.approval_request_id = p_request_id) THEN
        RAISE EXCEPTION 'RD_MODE_CHANGE_REUSED: pedido % já aplicado', p_request_id USING ERRCODE = 'P0001';
    END IF;
    v_expected := req.payload ->> 'expected_version';
    IF v_expected IS NULL OR v_expected !~ '^[0-9]{{1,9}}$' THEN
        RAISE EXCEPTION 'RD_APPROVAL_MODE_MISMATCH: pedido % sem versão esperada válida', p_request_id
            USING ERRCODE = 'P0001';
    END IF;
    IF req.approval_mode IS DISTINCT FROM cur.to_mode
       OR (req.payload ->> 'from_mode') IS DISTINCT FROM cur.to_mode
       OR v_expected::integer <> cur.mode_version THEN
        RAISE EXCEPTION 'RD_APPROVAL_MODE_MISMATCH: pedido % não corresponde ao modo atual (% v%)', p_request_id,
            cur.to_mode, cur.mode_version USING ERRCODE = 'P0001';
    END IF;
    v_target := req.payload ->> 'target_mode';
    IF v_target IS NULL OR v_target NOT IN ('SINGLE','DUAL') OR v_target = cur.to_mode THEN
        RAISE EXCEPTION 'RD_MODE_CHANGE_INVALID: modo alvo inválido' USING ERRCODE = 'P0001';
    END IF;
    IF req.reason IS NULL OR length(btrim(req.reason)) < 20 THEN
        RAISE EXCEPTION 'RD_MODE_CHANGE_INVALID: motivo com menos de 20 caracteres' USING ERRCODE = 'P0001';
    END IF;
    IF cur.to_mode = 'DUAL' THEN
        -- DUAL → SINGLE: A solicita, B (outro Master ativo) aprova
        IF req.approved_by IS NULL OR req.approved_by = req.requested_by
           OR NOT public.rd_is_active_master(req.approved_by) THEN
            RAISE EXCEPTION 'RD_APPROVER_NOT_ACTIVE_MASTER: aprovador inválido para a mudança de modo'
                USING ERRCODE = 'P0001';
        END IF;
        IF NOT public.rd_is_active_master(req.requested_by) THEN
            RAISE EXCEPTION 'RD_REQUESTER_NOT_ACTIVE_MASTER: solicitante não é Admin Master ativo' USING ERRCODE = 'P0001';
        END IF;
        IF NOT EXISTS (SELECT 1 FROM public.financial_approval_events e
                        WHERE e.request_id = p_request_id AND e.event = 'APPROVED' AND e.actor_user_id = req.approved_by) THEN
            RAISE EXCEPTION 'RD_MODE_CHANGE_INVALID: evento APPROVED do aprovador ausente' USING ERRCODE = 'P0001';
        END IF;
        v_kind := 'DUAL_TO_SINGLE';
        v_auth := req.approved_by;
    ELSE
        -- SINGLE → DUAL: o próprio Master autoriza (fatores conferidos pela aplicação)
        IF req.self_authorized_by IS NULL OR req.self_authorized_by <> req.requested_by
           OR NOT public.rd_is_active_master(req.requested_by) THEN
            RAISE EXCEPTION 'RD_SELF_AUTHORIZER_INVALID: autorização única inválida para a mudança de modo'
                USING ERRCODE = 'P0001';
        END IF;
        IF NOT EXISTS (SELECT 1 FROM public.financial_approval_events e
                        WHERE e.request_id = p_request_id AND e.event = 'SELF_AUTHORIZED'
                          AND e.actor_user_id = req.requested_by) THEN
            RAISE EXCEPTION 'RD_MODE_CHANGE_INVALID: evento SELF_AUTHORIZED do solicitante ausente' USING ERRCODE = 'P0001';
        END IF;
        v_kind := 'SINGLE_TO_DUAL';
        v_auth := req.self_authorized_by;
    END IF;
    v_new := cur.mode_version + 1;
    INSERT INTO public.instance_approval_mode_history
        (mode_version, from_mode, to_mode, kind, source, approval_request_id, requested_by, authorized_by,
         reason, env_value, prev_seal, app_seal)
    VALUES (v_new, cur.to_mode, v_target, v_kind, 'APPROVAL_REQUEST', p_request_id, req.requested_by, v_auth,
            req.reason, NULL, p_prev_seal, p_app_seal);
    RAISE LOG 'RD_SECURITY_EVENT approval_mode_changed from=% to=% version=% request=% requested_by=% authorized_by=% by=% app=% addr=%',
        cur.to_mode, v_target, v_new, p_request_id, req.requested_by, v_auth, {_CTX};
    RETURN v_new;
END;
$$
"""

ASSIGN_SEAT_FN = f"""
CREATE OR REPLACE FUNCTION rd_security_assign_master_seat(p_user_id uuid, p_seat smallint, p_source text,
                                                          p_invite_id uuid, p_actor uuid, p_prev_seal text,
                                                          p_app_seal text)
RETURNS void
LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, public, pg_temp AS $$
DECLARE
    v_mode text;
    v_head text;
    v_role uuid;
    u record;
    inv record;
BEGIN
    PERFORM pg_catalog.pg_advisory_xact_lock({LOCK_MASTER_SEATS});
    v_mode := public.rd_security_current_approval_mode();
    IF v_mode IS NULL THEN
        RAISE EXCEPTION 'RD_INSTANCE_NOT_INITIALIZED: modo de aprovação não inicializado' USING ERRCODE = 'P0001';
    END IF;
    IF p_seat IS NULL OR p_seat NOT IN (1, 2) THEN
        RAISE EXCEPTION 'RD_MASTER_SEAT_INVALID: assento % inválido', p_seat USING ERRCODE = 'P0001';
    END IF;
    IF p_source IS NULL OR p_source NOT IN ('BOOTSTRAP_ENV','MASTER_INVITE','CLI_BOOTSTRAP','LEGACY_ADOPTED') THEN
        RAISE EXCEPTION 'RD_MASTER_SEAT_INVALID: origem % inválida', p_source USING ERRCODE = 'P0001';
    END IF;
    IF (p_source = 'MASTER_INVITE') <> (p_invite_id IS NOT NULL) THEN
        RAISE EXCEPTION 'RD_MASTER_SEAT_INVALID: convite só (e sempre) na origem MASTER_INVITE' USING ERRCODE = 'P0001';
    END IF;
    IF p_app_seal IS NULL OR p_app_seal !~ '{_SEAL_RE}' THEN
        RAISE EXCEPTION 'RD_SEAL_INVALID: selo fora do formato' USING ERRCODE = 'P0001';
    END IF;
    SELECT e.app_seal INTO v_head FROM public.admin_master_seat_events e ORDER BY e.id DESC LIMIT 1;
    IF p_prev_seal IS DISTINCT FROM COALESCE(v_head, {_GENESIS_SQL}) THEN
        RAISE EXCEPTION 'RD_SEAL_CHAIN_MISMATCH: selo anterior não confere com a cabeça da trilha de assentos'
            USING ERRCODE = 'P0001';
    END IF;
    SELECT x.id, x.email, x.is_active, x.anonymized_at, x.admin_master_seat, x.must_change_password,
           x.totp_enabled, x.financial_password_hash
      INTO u FROM public.users x WHERE x.id = p_user_id FOR UPDATE;
    IF NOT FOUND THEN
        RAISE EXCEPTION 'RD_MASTER_SEAT_USER_INVALID: usuário % inexistente', p_user_id USING ERRCODE = 'P0001';
    END IF;
    IF NOT u.is_active OR u.anonymized_at IS NOT NULL THEN
        RAISE EXCEPTION 'RD_MASTER_SEAT_USER_INVALID: usuário % inativo ou anonimizado', p_user_id USING ERRCODE = 'P0001';
    END IF;

    IF p_source = 'LEGACY_ADOPTED' THEN
        -- instalação existente: sela o titular que JÁ ocupa o assento (não altera users)
        IF u.admin_master_seat IS DISTINCT FROM p_seat THEN
            RAISE EXCEPTION 'RD_MASTER_SEAT_INVALID_STATE: usuário % não ocupa o assento %', p_user_id, p_seat
                USING ERRCODE = 'P0001';
        END IF;
        IF EXISTS (SELECT 1 FROM public.admin_master_seat_events e WHERE e.seat = p_seat) THEN
            RAISE EXCEPTION 'RD_MASTER_SEAT_INVALID_STATE: assento % já possui eventos', p_seat USING ERRCODE = 'P0001';
        END IF;
    ELSE
        IF u.admin_master_seat IS NOT NULL THEN
            RAISE EXCEPTION 'RD_MASTER_SEAT_INVALID_STATE: usuário % já ocupa o assento %', p_user_id,
                u.admin_master_seat USING ERRCODE = 'P0001';
        END IF;
        IF EXISTS (SELECT 1 FROM public.users x WHERE x.admin_master_seat = p_seat) THEN
            RAISE EXCEPTION 'RD_MASTER_SEAT_OCCUPIED: assento % ocupado', p_seat USING ERRCODE = 'P0001';
        END IF;
        IF p_source = 'BOOTSTRAP_ENV' OR (p_source = 'CLI_BOOTSTRAP' AND p_seat = 1) THEN
            IF p_seat <> 1 THEN
                RAISE EXCEPTION 'RD_MASTER_SEAT_INVALID: o bootstrap só atribui o assento 1' USING ERRCODE = 'P0001';
            END IF;
            IF EXISTS (SELECT 1 FROM public.admin_master_seat_events)
               OR EXISTS (SELECT 1 FROM public.users x WHERE x.admin_master_seat IS NOT NULL) THEN
                RAISE EXCEPTION 'RD_MASTER_SEAT_INVALID_STATE: assento 1 só por bootstrap em instância sem assentos'
                    USING ERRCODE = 'P0001';
            END IF;
            IF NOT u.must_change_password THEN
                RAISE EXCEPTION 'RD_MASTER_SEAT_INVALID_STATE: Master de bootstrap nasce com troca de senha obrigatória'
                    USING ERRCODE = 'P0001';
            END IF;
        ELSIF p_source = 'CLI_BOOTSTRAP' THEN
            IF v_mode <> 'DUAL' THEN
                RAISE EXCEPTION 'RD_MASTER_SEAT_INVALID_STATE: assento 2 só no modo DUAL' USING ERRCODE = 'P0001';
            END IF;
            IF NOT EXISTS (SELECT 1 FROM public.users x WHERE x.admin_master_seat = 1) THEN
                RAISE EXCEPTION 'RD_MASTER_SEAT_INVALID_STATE: assento 2 exige o assento 1 ocupado' USING ERRCODE = 'P0001';
            END IF;
            IF EXISTS (SELECT 1 FROM public.admin_master_invites i WHERE i.status = 'PENDING') THEN
                RAISE EXCEPTION 'RD_MASTER_SEAT_INVALID_STATE: existe convite pendente para Admin Master'
                    USING ERRCODE = 'P0001';
            END IF;
            IF NOT u.must_change_password THEN
                RAISE EXCEPTION 'RD_MASTER_SEAT_INVALID_STATE: Master criado pela CLI nasce com troca de senha obrigatória'
                    USING ERRCODE = 'P0001';
            END IF;
        ELSE
            -- MASTER_INVITE: ativação do 2º Master com os PRÓPRIOS fatores já definidos
            SELECT i.id, i.seat, i.email, i.status, i.expires_at, i.activation_user_id, i.invited_by
              INTO inv FROM public.admin_master_invites i WHERE i.id = p_invite_id FOR UPDATE;
            IF NOT FOUND OR inv.status <> 'PENDING' OR inv.expires_at <= now() OR inv.seat <> p_seat
               OR inv.activation_user_id <> p_user_id OR lower(inv.email) <> lower(u.email) THEN
                RAISE EXCEPTION 'RD_MASTER_INVITE_INVALID: convite inválido, expirado ou de outro usuário'
                    USING ERRCODE = 'P0001';
            END IF;
            IF v_mode <> 'DUAL' THEN
                RAISE EXCEPTION 'RD_MASTER_INVITE_INVALID: convite de 2º Master só no modo DUAL' USING ERRCODE = 'P0001';
            END IF;
            IF NOT public.rd_is_active_master(inv.invited_by) THEN
                RAISE EXCEPTION 'RD_MASTER_INVITE_INVALID: convidante não é Admin Master ativo' USING ERRCODE = 'P0001';
            END IF;
            IF NOT (u.totp_enabled AND u.financial_password_hash IS NOT NULL AND NOT u.must_change_password) THEN
                RAISE EXCEPTION 'RD_MASTER_SEAT_USER_INVALID: 2º Master exige TOTP, senha financeira e senha própria'
                    USING ERRCODE = 'P0001';
            END IF;
            UPDATE public.admin_master_invites
               SET status = 'ACCEPTED', accepted_at = now(), accepted_user_id = p_user_id
             WHERE id = p_invite_id;
        END IF;
        UPDATE public.users SET admin_master_seat = p_seat WHERE id = p_user_id;
    END IF;

    SELECT r.id INTO v_role FROM public.roles r WHERE r.slug = 'super_admin';
    IF v_role IS NULL THEN
        RAISE EXCEPTION 'RD_ROLE_MISSING: perfil super_admin inexistente (sincronize o catálogo RBAC)'
            USING ERRCODE = 'P0001';
    END IF;
    INSERT INTO public.role_assignments (id, user_id, role_id, scope_type, scope_id, scope_value, granted_by, created_at)
    VALUES (gen_random_uuid(), p_user_id, v_role, 'GLOBAL', NULL, NULL, p_actor, now())
    ON CONFLICT DO NOTHING;
    INSERT INTO public.admin_master_seat_events
        (seat, user_id, event, source, invite_id, actor_user_id, prev_seal, app_seal)
    VALUES (p_seat, p_user_id, 'ASSIGNED', p_source, p_invite_id, p_actor, p_prev_seal, p_app_seal);
    RAISE LOG 'RD_SECURITY_EVENT master_seat_assigned seat=% user=% source=% invite=% actor=% by=% app=% addr=%',
        p_seat, p_user_id, p_source, p_invite_id, p_actor, {_CTX};
END;
$$
"""

RECOVER_FN = f"""
CREATE OR REPLACE FUNCTION rd_security_recover_master(p_user_id uuid, p_new_password_hash text, p_actor uuid,
                                                      p_prev_seal text, p_app_seal text)
RETURNS smallint
LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, public, pg_temp AS $$
DECLARE
    v_head text;
    v_seat smallint;
BEGIN
    PERFORM pg_catalog.pg_advisory_xact_lock({LOCK_MASTER_SEATS});
    IF p_app_seal IS NULL OR p_app_seal !~ '{_SEAL_RE}' THEN
        RAISE EXCEPTION 'RD_SEAL_INVALID: selo fora do formato' USING ERRCODE = 'P0001';
    END IF;
    SELECT e.app_seal INTO v_head FROM public.admin_master_seat_events e ORDER BY e.id DESC LIMIT 1;
    IF p_prev_seal IS DISTINCT FROM COALESCE(v_head, {_GENESIS_SQL}) THEN
        RAISE EXCEPTION 'RD_SEAL_CHAIN_MISMATCH: selo anterior não confere com a cabeça da trilha de assentos'
            USING ERRCODE = 'P0001';
    END IF;
    SELECT x.admin_master_seat INTO v_seat FROM public.users x WHERE x.id = p_user_id FOR UPDATE;
    IF v_seat IS NULL THEN
        RAISE EXCEPTION 'RD_MASTER_RECOVERY_INVALID: usuário % não ocupa assento de Admin Master', p_user_id
            USING ERRCODE = 'P0001';
    END IF;
    IF p_new_password_hash IS NULL OR length(p_new_password_hash) < 20 THEN
        RAISE EXCEPTION 'RD_MASTER_RECOVERY_INVALID: hash de senha temporária inválido' USING ERRCODE = 'P0001';
    END IF;
    UPDATE public.users
       SET hashed_password = p_new_password_hash, password_changed_at = now(), must_change_password = true,
           totp_enabled = false, totp_secret_enc = NULL, totp_pending_secret_enc = NULL, totp_last_step = NULL,
           totp_enabled_at = NULL, financial_password_hash = NULL, financial_password_changed_at = NULL,
           failed_login_attempts = 0, locked_until = NULL, master_recovered_at = now()
     WHERE id = p_user_id;
    DELETE FROM public.user_recovery_codes WHERE user_id = p_user_id;
    INSERT INTO public.admin_master_seat_events
        (seat, user_id, event, source, invite_id, actor_user_id, prev_seal, app_seal)
    VALUES (v_seat, p_user_id, 'RECOVERED', 'CLI_RECOVERY', NULL, p_actor, p_prev_seal, p_app_seal);
    RAISE LOG 'RD_SECURITY_EVENT master_recovered seat=% user=% actor=% by=% app=% addr=%',
        v_seat, p_user_id, p_actor, {_CTX};
    RETURN v_seat;
END;
$$
"""

# ─── Triggers ────────────────────────────────────────────────────────────────

USERS_GUARD_FN = """
CREATE OR REPLACE FUNCTION rd_users_master_guard() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    IF public.rd_security_privileged() THEN
        IF TG_OP = 'DELETE' THEN
            RETURN OLD;
        END IF;
        RETURN NEW;
    END IF;
    IF TG_OP = 'INSERT' THEN
        IF NEW.admin_master_seat IS NOT NULL THEN
            RAISE EXCEPTION 'RD_MASTER_SEAT_PROTECTED: assento de Admin Master só é atribuído pelas funções de segurança'
                USING ERRCODE = 'P0001';
        END IF;
        RETURN NEW;
    END IF;
    IF TG_OP = 'DELETE' THEN
        IF OLD.admin_master_seat IS NOT NULL THEN
            RAISE EXCEPTION 'RD_MASTER_PROTECTED: titular de assento de Admin Master não pode ser apagado'
                USING ERRCODE = 'P0001';
        END IF;
        RETURN OLD;
    END IF;
    IF NEW.admin_master_seat IS DISTINCT FROM OLD.admin_master_seat THEN
        RAISE EXCEPTION 'RD_MASTER_SEAT_PROTECTED: assento de Admin Master só muda pelas funções de segurança'
            USING ERRCODE = 'P0001';
    END IF;
    IF OLD.admin_master_seat IS NOT NULL THEN
        IF NEW.id IS DISTINCT FROM OLD.id
           OR NEW.email IS DISTINCT FROM OLD.email
           OR NEW.is_active IS DISTINCT FROM OLD.is_active
           OR NEW.anonymized_at IS DISTINCT FROM OLD.anonymized_at
           OR NEW.master_recovered_at IS DISTINCT FROM OLD.master_recovered_at THEN
            RAISE EXCEPTION 'RD_MASTER_PROTECTED: identidade/ativação do Admin Master só muda pelas funções de segurança'
                USING ERRCODE = 'P0001';
        END IF;
        IF OLD.totp_enabled AND (NOT NEW.totp_enabled OR NEW.totp_secret_enc IS DISTINCT FROM OLD.totp_secret_enc) THEN
            RAISE EXCEPTION 'RD_MASTER_TOTP_PROTECTED: TOTP do Admin Master não pode ser desligado nem trocado'
                USING ERRCODE = 'P0001';
        END IF;
        IF OLD.financial_password_hash IS NOT NULL AND NEW.financial_password_hash IS NULL THEN
            RAISE EXCEPTION 'RD_MASTER_FINANCIAL_PASSWORD_PROTECTED: senha financeira do Admin Master não pode ser removida'
                USING ERRCODE = 'P0001';
        END IF;
    END IF;
    RETURN NEW;
END;
$$
"""

_LOGGED_USER_COLUMNS = (
    "hashed_password", "must_change_password", "totp_enabled", "totp_secret_enc", "financial_password_hash",
    "email", "phone", "is_active", "anonymized_at", "admin_master_seat", "master_recovered_at",
)

USERS_LOG_FN = (
    """
CREATE OR REPLACE FUNCTION rd_users_master_log() RETURNS trigger
LANGUAGE plpgsql AS $$
DECLARE
    cols text[] := ARRAY[]::text[];
BEGIN
"""
    + "".join(
        f"    IF NEW.{c} IS DISTINCT FROM OLD.{c} THEN cols := array_append(cols, '{c}'); END IF;\n"
        for c in _LOGGED_USER_COLUMNS
    )
    + f"""    IF cardinality(cols) > 0 THEN
        RAISE LOG 'RD_SECURITY_EVENT master_user_changed user=% seat=% columns=% by=% app=% addr=%',
            NEW.id, COALESCE(NEW.admin_master_seat, OLD.admin_master_seat), array_to_string(cols, ','), {_CTX};
    END IF;
    RETURN NULL;
END;
$$
"""
)

ROLE_ASSIGNMENTS_GUARD_FN = """
CREATE OR REPLACE FUNCTION rd_role_assignments_master_guard() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    IF NOT public.rd_security_privileged()
       AND EXISTS (SELECT 1 FROM public.roles r JOIN public.users u ON u.id = OLD.user_id
                    WHERE r.id = OLD.role_id AND r.slug = 'super_admin' AND u.admin_master_seat IS NOT NULL) THEN
        RAISE EXCEPTION 'RD_MASTER_ROLE_PROTECTED: o perfil super_admin do Admin Master não pode ser removido nem alterado'
            USING ERRCODE = 'P0001';
    END IF;
    IF TG_OP = 'DELETE' THEN
        RETURN OLD;
    END IF;
    RETURN NEW;
END;
$$
"""

INVITE_GUARD_FN = """
CREATE OR REPLACE FUNCTION rd_admin_master_invite_guard() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    IF TG_OP = 'DELETE' THEN
        RAISE EXCEPTION 'RD_APPEND_ONLY: convite de Admin Master não pode ser apagado' USING ERRCODE = 'P0001';
    END IF;
    IF TG_OP = 'INSERT' THEN
        IF NEW.status IS DISTINCT FROM 'PENDING' THEN
            RAISE EXCEPTION 'RD_MASTER_INVITE_INVALID_STATE: convite nasce PENDING' USING ERRCODE = 'P0001';
        END IF;
        IF EXISTS (SELECT 1 FROM public.users x WHERE x.admin_master_seat = NEW.seat) THEN
            RAISE EXCEPTION 'RD_MASTER_INVITE_INVALID_STATE: assento % ocupado', NEW.seat USING ERRCODE = 'P0001';
        END IF;
        IF NOT public.rd_is_active_master(NEW.invited_by) THEN
            RAISE EXCEPTION 'RD_MASTER_INVITE_INVALID_STATE: convidante não é Admin Master ativo' USING ERRCODE = 'P0001';
        END IF;
        IF public.rd_security_current_approval_mode() IS DISTINCT FROM 'DUAL' THEN
            RAISE EXCEPTION 'RD_MASTER_INVITE_INVALID_STATE: convite de 2º Master só no modo DUAL' USING ERRCODE = 'P0001';
        END IF;
        RETURN NEW;
    END IF;
    IF NEW.id IS DISTINCT FROM OLD.id OR NEW.seat IS DISTINCT FROM OLD.seat OR NEW.email IS DISTINCT FROM OLD.email
       OR NEW.full_name IS DISTINCT FROM OLD.full_name OR NEW.invited_by IS DISTINCT FROM OLD.invited_by
       OR NEW.activation_user_id IS DISTINCT FROM OLD.activation_user_id
       OR NEW.created_at IS DISTINCT FROM OLD.created_at THEN
        RAISE EXCEPTION 'RD_MASTER_INVITE_IMMUTABLE: identidade do convite é imutável' USING ERRCODE = 'P0001';
    END IF;
    IF OLD.status <> 'PENDING' THEN
        IF to_jsonb(NEW) IS DISTINCT FROM to_jsonb(OLD) THEN
            RAISE EXCEPTION 'RD_MASTER_INVITE_INVALID_STATE: convite % é final', OLD.status USING ERRCODE = 'P0001';
        END IF;
        RETURN NEW;
    END IF;
    IF NEW.status IS DISTINCT FROM OLD.status THEN
        IF NEW.status NOT IN ('ACCEPTED','REVOKED','EXPIRED','LOCKED') THEN
            RAISE EXCEPTION 'RD_MASTER_INVITE_INVALID_STATE: transição PENDING → % proibida', NEW.status
                USING ERRCODE = 'P0001';
        END IF;
        IF NEW.status = 'ACCEPTED' AND NOT public.rd_security_privileged() THEN
            RAISE EXCEPTION 'RD_MASTER_INVITE_INVALID_STATE: aceite só pela função de ativação do Admin Master'
                USING ERRCODE = 'P0001';
        END IF;
    END IF;
    RETURN NEW;
END;
$$
"""


# ─── 0009 (AÇÃO 003 / D1 do revisor): corpos VIGENTES dos guards de titular, log e convite ─────────────
# = endurecidos da 0008 + selo da aplicação (o banco NÃO confere HMAC: exige rastro; quem confere é a aplicação).
# Os constantes ``USERS_GUARD_FN``/``USERS_LOG_FN``/``INVITE_GUARD_FN`` acima são HISTÓRICOS (replay da 0007).

USERS_GUARD_FN_V3 = """
CREATE OR REPLACE FUNCTION rd_users_master_guard() RETURNS trigger
LANGUAGE plpgsql
SET search_path = pg_catalog, public, pg_temp AS $$
BEGIN
    IF public.rd_security_privileged() THEN
        IF TG_OP = 'DELETE' THEN
            RETURN OLD;
        END IF;
        RETURN NEW;
    END IF;
    IF TG_OP = 'INSERT' THEN
        IF NEW.admin_master_seat IS NOT NULL THEN
            RAISE EXCEPTION 'RD_MASTER_SEAT_PROTECTED: assento de Admin Master só é atribuído pelas funções de segurança'
                USING ERRCODE = 'P0001';
        END IF;
        RETURN NEW;
    END IF;
    IF TG_OP = 'DELETE' THEN
        IF OLD.admin_master_seat IS NOT NULL THEN
            RAISE EXCEPTION 'RD_MASTER_PROTECTED: titular de assento de Admin Master não pode ser apagado'
                USING ERRCODE = 'P0001';
        END IF;
        RETURN OLD;
    END IF;
    IF NEW.admin_master_seat IS DISTINCT FROM OLD.admin_master_seat THEN
        RAISE EXCEPTION 'RD_MASTER_SEAT_PROTECTED: assento de Admin Master só muda pelas funções de segurança'
            USING ERRCODE = 'P0001';
    END IF;
    IF OLD.admin_master_seat IS NOT NULL THEN
        IF NEW.id IS DISTINCT FROM OLD.id
           OR NEW.email IS DISTINCT FROM OLD.email
           OR NEW.is_active IS DISTINCT FROM OLD.is_active
           OR NEW.anonymized_at IS DISTINCT FROM OLD.anonymized_at
           OR NEW.master_recovered_at IS DISTINCT FROM OLD.master_recovered_at THEN
            RAISE EXCEPTION 'RD_MASTER_PROTECTED: identidade/ativação do Admin Master só muda pelas funções de segurança'
                USING ERRCODE = 'P0001';
        END IF;
        IF OLD.totp_enabled AND (NOT NEW.totp_enabled OR NEW.totp_secret_enc IS DISTINCT FROM OLD.totp_secret_enc) THEN
            RAISE EXCEPTION 'RD_MASTER_TOTP_PROTECTED: TOTP do Admin Master não pode ser desligado nem trocado'
                USING ERRCODE = 'P0001';
        END IF;
        IF OLD.financial_password_hash IS NOT NULL AND NEW.financial_password_hash IS NULL THEN
            RAISE EXCEPTION 'RD_MASTER_FINANCIAL_PASSWORD_PROTECTED: senha financeira do Admin Master não pode ser removida'
                USING ERRCODE = 'P0001';
        END IF;
        -- 0009 (D1): selo de credenciais da aplicação — nunca volta a NULL e muda junto com hash/fatores
        IF OLD.master_credential_seal IS NOT NULL
           AND (NEW.master_credential_seal IS NULL OR btrim(NEW.master_credential_seal) = '') THEN
            RAISE EXCEPTION 'RD_MASTER_CREDENTIAL_SEAL_REQUIRED: selo de credenciais do Admin Master não pode ser removido'
                USING ERRCODE = 'P0001';
        END IF;
        IF (NEW.hashed_password IS DISTINCT FROM OLD.hashed_password
            OR NEW.must_change_password IS DISTINCT FROM OLD.must_change_password
            OR NEW.totp_enabled IS DISTINCT FROM OLD.totp_enabled
            OR NEW.totp_secret_enc IS DISTINCT FROM OLD.totp_secret_enc
            OR NEW.financial_password_hash IS DISTINCT FROM OLD.financial_password_hash
            OR NEW.email IS DISTINCT FROM OLD.email
            OR NEW.is_active IS DISTINCT FROM OLD.is_active
            OR NEW.admin_master_seat IS DISTINCT FROM OLD.admin_master_seat)
           AND (NEW.master_credential_seal IS NULL OR btrim(NEW.master_credential_seal) = ''
                OR NEW.master_credential_seal IS NOT DISTINCT FROM OLD.master_credential_seal) THEN
            RAISE EXCEPTION 'RD_MASTER_CREDENTIAL_SEAL_REQUIRED: mudança de credencial do Admin Master exige novo selo da aplicação'
                USING ERRCODE = 'P0001';
        END IF;
    END IF;
    RETURN NEW;
END;
$$
"""

USERS_LOG_FN_V3 = """
CREATE OR REPLACE FUNCTION rd_users_master_log() RETURNS trigger
LANGUAGE plpgsql
SET search_path = pg_catalog, public, pg_temp AS $$
DECLARE
    cols text[] := ARRAY[]::text[];
BEGIN
    IF NEW.hashed_password IS DISTINCT FROM OLD.hashed_password THEN cols := array_append(cols, 'hashed_password'); END IF;
    IF NEW.must_change_password IS DISTINCT FROM OLD.must_change_password THEN cols := array_append(cols, 'must_change_password'); END IF;
    IF NEW.totp_enabled IS DISTINCT FROM OLD.totp_enabled THEN cols := array_append(cols, 'totp_enabled'); END IF;
    IF NEW.totp_secret_enc IS DISTINCT FROM OLD.totp_secret_enc THEN cols := array_append(cols, 'totp_secret_enc'); END IF;
    IF NEW.financial_password_hash IS DISTINCT FROM OLD.financial_password_hash THEN cols := array_append(cols, 'financial_password_hash'); END IF;
    IF NEW.email IS DISTINCT FROM OLD.email THEN cols := array_append(cols, 'email'); END IF;
    IF NEW.phone IS DISTINCT FROM OLD.phone THEN cols := array_append(cols, 'phone'); END IF;
    IF NEW.is_active IS DISTINCT FROM OLD.is_active THEN cols := array_append(cols, 'is_active'); END IF;
    IF NEW.anonymized_at IS DISTINCT FROM OLD.anonymized_at THEN cols := array_append(cols, 'anonymized_at'); END IF;
    IF NEW.admin_master_seat IS DISTINCT FROM OLD.admin_master_seat THEN cols := array_append(cols, 'admin_master_seat'); END IF;
    IF NEW.master_recovered_at IS DISTINCT FROM OLD.master_recovered_at THEN cols := array_append(cols, 'master_recovered_at'); END IF;
    IF NEW.master_credential_seal IS DISTINCT FROM OLD.master_credential_seal THEN cols := array_append(cols, 'master_credential_seal'); END IF;
    IF cardinality(cols) > 0 THEN
        RAISE LOG 'RD_SECURITY_EVENT master_user_changed user=% seat=% columns=% by=% app=% addr=%',
            NEW.id, COALESCE(NEW.admin_master_seat, OLD.admin_master_seat), array_to_string(cols, ','), session_user, current_setting('application_name', true), inet_client_addr();
    END IF;
    RETURN NULL;
END;
$$
"""

INVITE_GUARD_FN_V3 = """
CREATE OR REPLACE FUNCTION rd_admin_master_invite_guard() RETURNS trigger
LANGUAGE plpgsql
SET search_path = pg_catalog, public, pg_temp AS $$
BEGIN
    IF TG_OP = 'DELETE' THEN
        RAISE EXCEPTION 'RD_APPEND_ONLY: convite de Admin Master não pode ser apagado' USING ERRCODE = 'P0001';
    END IF;
    IF TG_OP = 'INSERT' THEN
        -- 0009 (D1): convite do papel de runtime exige o selo da aplicação (conferido na ativação)
        IF NOT public.rd_security_privileged() AND (NEW.seal IS NULL OR btrim(NEW.seal) = '') THEN
            RAISE EXCEPTION 'RD_MASTER_INVITE_SEAL_REQUIRED: convite exige o selo da aplicação' USING ERRCODE = 'P0001';
        END IF;
        IF NEW.status IS DISTINCT FROM 'PENDING' THEN
            RAISE EXCEPTION 'RD_MASTER_INVITE_INVALID_STATE: convite nasce PENDING' USING ERRCODE = 'P0001';
        END IF;
        IF EXISTS (SELECT 1 FROM public.users x WHERE x.admin_master_seat = NEW.seat) THEN
            RAISE EXCEPTION 'RD_MASTER_INVITE_INVALID_STATE: assento % ocupado', NEW.seat USING ERRCODE = 'P0001';
        END IF;
        IF NOT public.rd_is_active_master(NEW.invited_by) THEN
            RAISE EXCEPTION 'RD_MASTER_INVITE_INVALID_STATE: convidante não é Admin Master ativo' USING ERRCODE = 'P0001';
        END IF;
        IF public.rd_security_current_approval_mode() IS DISTINCT FROM 'DUAL' THEN
            RAISE EXCEPTION 'RD_MASTER_INVITE_INVALID_STATE: convite de 2º Master só no modo DUAL' USING ERRCODE = 'P0001';
        END IF;
        RETURN NEW;
    END IF;
    IF NEW.id IS DISTINCT FROM OLD.id OR NEW.seat IS DISTINCT FROM OLD.seat OR NEW.email IS DISTINCT FROM OLD.email
       OR NEW.full_name IS DISTINCT FROM OLD.full_name OR NEW.invited_by IS DISTINCT FROM OLD.invited_by
       OR NEW.activation_user_id IS DISTINCT FROM OLD.activation_user_id
       OR NEW.created_at IS DISTINCT FROM OLD.created_at THEN
        RAISE EXCEPTION 'RD_MASTER_INVITE_IMMUTABLE: identidade do convite é imutável' USING ERRCODE = 'P0001';
    END IF;
    -- 0009 (D1): token e selo só mudam JUNTOS (reenvio); selo nunca volta a NULL
    IF NOT public.rd_security_privileged()
       AND ((NEW.token_hash IS DISTINCT FROM OLD.token_hash) <> (NEW.seal IS DISTINCT FROM OLD.seal)
            OR (OLD.seal IS NOT NULL AND (NEW.seal IS NULL OR btrim(NEW.seal) = ''))) THEN
        RAISE EXCEPTION 'RD_MASTER_INVITE_SEAL_REQUIRED: token e selo do convite só mudam juntos (reenvio)'
            USING ERRCODE = 'P0001';
    END IF;
    IF OLD.status <> 'PENDING' THEN
        IF to_jsonb(NEW) IS DISTINCT FROM to_jsonb(OLD) THEN
            RAISE EXCEPTION 'RD_MASTER_INVITE_INVALID_STATE: convite % é final', OLD.status USING ERRCODE = 'P0001';
        END IF;
        RETURN NEW;
    END IF;
    IF NEW.status IS DISTINCT FROM OLD.status THEN
        IF NEW.status NOT IN ('ACCEPTED','REVOKED','EXPIRED','LOCKED') THEN
            RAISE EXCEPTION 'RD_MASTER_INVITE_INVALID_STATE: transição PENDING → % proibida', NEW.status
                USING ERRCODE = 'P0001';
        END IF;
        IF NEW.status = 'ACCEPTED' AND NOT public.rd_security_privileged() THEN
            RAISE EXCEPTION 'RD_MASTER_INVITE_INVALID_STATE: aceite só pela função de ativação do Admin Master'
                USING ERRCODE = 'P0001';
        END IF;
    END IF;
    RETURN NEW;
END;
$$
"""


def _trigger(name: str, table: str, timing: str, fn: str, *, statement: bool = False, when: str = "") -> list[str]:
    level = "STATEMENT" if statement else "ROW"
    cond = f" WHEN ({when})" if when else ""
    return [
        f"DROP TRIGGER IF EXISTS {name} ON {table}",
        f"CREATE TRIGGER {name} {timing} ON {table} FOR EACH {level}{cond} EXECUTE FUNCTION {fn}()",
    ]


def function_statements() -> list[str]:
    """Funções (leitura + definer) e o REVOKE de EXECUTE do PUBLIC nas definer."""
    out = [PRIVILEGED_FN, CURRENT_MODE_FN, IS_ACTIVE_MASTER_FN, ACTIVE_MASTER_COUNT_FN,
           INIT_MODE_FN, APPLY_TRANSITION_FN, ASSIGN_SEAT_FN, RECOVER_FN]
    out += [f"REVOKE ALL ON FUNCTION {sig} FROM PUBLIC" for sig in DEFINER_FUNCTIONS]
    return out


def trigger_statements() -> list[str]:
    out = [USERS_GUARD_FN, USERS_LOG_FN, ROLE_ASSIGNMENTS_GUARD_FN, INVITE_GUARD_FN]
    out += _trigger("trg_users_master_guard", "users", "BEFORE INSERT OR UPDATE OR DELETE", "rd_users_master_guard")
    out += _trigger("trg_users_master_log", "users", "AFTER UPDATE", "rd_users_master_log",
                    when="OLD.admin_master_seat IS NOT NULL OR NEW.admin_master_seat IS NOT NULL")
    out += _trigger("trg_role_assignments_master_guard", "role_assignments", "BEFORE UPDATE OR DELETE",
                    "rd_role_assignments_master_guard")
    out += _trigger("trg_admin_master_invites_guard", "admin_master_invites", "BEFORE INSERT OR UPDATE OR DELETE",
                    "rd_admin_master_invite_guard")
    out += _trigger("trg_admin_master_invites_no_truncate", "admin_master_invites", "BEFORE TRUNCATE",
                    "rd_forbid_mutation", statement=True)
    return out


def statements(existing_tables: Optional[Iterable[str]] = None) -> list[str]:
    """DDL idempotente da segurança da instância. Requer ``rd_forbid_mutation`` (W1) e as tabelas da 0007.

    ``existing_tables=None`` = todas presentes (a 0004 nunca chama este módulo: ver
    ``app.db.ddl.DOMAIN_DDL_MODULES_SINCE``). O append-only das trilhas vem de
    ``app.db.ddl.APPEND_ONLY_TABLES``.
    """
    tables = set(existing_tables) if existing_tables is not None else None
    needed = {"users", "roles", "role_assignments", "financial_approval_requests", "financial_approval_events",
              "user_recovery_codes", *SECURITY_TABLES}
    if tables is not None and not needed <= tables:
        return []
    return function_statements() + trigger_statements()


def hardened_statements(existing_tables: Optional[Iterable[str]] = None) -> list[str]:
    """Corpos VIGENTES das funções INVOKER (leitura e triggers): ``search_path`` seguro (0008) e, nos guards de
    titular/convite e no log, o selo da aplicação (0009, ``*_V3``). As SECURITY DEFINER já nasceram no padrão.
    Os constantes sem sufixo são HISTÓRICOS (replay da 0007)."""
    from app.db.ddl import harden_function

    tables = set(existing_tables) if existing_tables is not None else None
    if tables is not None and not set(SECURITY_TABLES) <= tables:
        return []
    # 0009 (D1): guards de titular/log/convite com o selo da aplicação (corpos vigentes V3, já endurecidos)
    return [harden_function(PRIVILEGED_FN), harden_function(CURRENT_MODE_FN), harden_function(IS_ACTIVE_MASTER_FN),
            harden_function(ACTIVE_MASTER_COUNT_FN), USERS_GUARD_FN_V3, USERS_LOG_FN_V3,
            harden_function(ROLE_ASSIGNMENTS_GUARD_FN), INVITE_GUARD_FN_V3]


def drop_statements() -> list[str]:
    """Remove triggers e funções da segurança (as tabelas são da migration)."""
    out = [
        "DROP TRIGGER IF EXISTS trg_users_master_guard ON users",
        "DROP TRIGGER IF EXISTS trg_users_master_log ON users",
        "DROP TRIGGER IF EXISTS trg_role_assignments_master_guard ON role_assignments",
        "DROP TRIGGER IF EXISTS trg_admin_master_invites_guard ON admin_master_invites",
        "DROP TRIGGER IF EXISTS trg_admin_master_invites_no_truncate ON admin_master_invites",
    ]
    out += [f"DROP FUNCTION IF EXISTS {sig}" for sig in TRIGGER_FUNCTIONS + DEFINER_FUNCTIONS + READ_FUNCTIONS]
    return out


def _role(runtime_role: str) -> str:
    if not runtime_role.replace("_", "").isalnum():
        raise ValueError("papel inválido")
    return runtime_role


def revoke_statements(runtime_role: str) -> list[str]:
    """Privilégios do papel de runtime (aplicar com o papel OWNER, após ``statements``).

    - trilhas: só SELECT (sem INSERT/UPDATE/DELETE/TRUNCATE; sem uso da sequência de identidade);
    - convites: SELECT/INSERT/UPDATE (sem DELETE/TRUNCATE);
    - funções definer: EXECUTE só para o papel de runtime (PUBLIC revogado em ``statements``).
    """
    r = _role(runtime_role)
    out: list[str] = []
    for t in TRAIL_TABLES:
        out += [
            f"REVOKE INSERT, UPDATE, DELETE, TRUNCATE ON {t} FROM {r}",
            f"GRANT SELECT ON {t} TO {r}",
            f"REVOKE ALL ON SEQUENCE {t}_id_seq FROM {r}",
        ]
    out += [
        f"GRANT SELECT, INSERT, UPDATE ON admin_master_invites TO {r}",
        f"REVOKE DELETE, TRUNCATE ON admin_master_invites FROM {r}",
    ]
    for sig in DEFINER_FUNCTIONS:
        out += [f"REVOKE ALL ON FUNCTION {sig} FROM PUBLIC", f"GRANT EXECUTE ON FUNCTION {sig} TO {r}"]
    return out
