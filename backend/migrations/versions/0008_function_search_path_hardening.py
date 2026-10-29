"""function_search_path_hardening — AÇÃO 003 / agente E (achado do agente H: sombra por tabela TEMPORÁRIA)

Problema (vulnerabilidade ALTA, provada): o papel de runtime ``rd_app`` tem o privilégio TEMPORARY e, quando
``pg_temp`` não está no ``search_path``, o PostgreSQL pesquisa o esquema temporário PRIMEIRO para tabelas,
sequências e tipos. Funções SECURITY INVOKER sem ``search_path`` e com nomes não qualificados liam a tabela
temporária do atacante: ``CREATE TEMP TABLE financial_period_closings (LIKE public.financial_period_closings)``
fazia ``rd_ledger_transaction_guard``/``rd_ledger_posting_guard`` aceitarem lançamento em período FECHADO;
uma ``ledger_postings`` temporária fazia ``rd_ledger_check_balanced`` aceitar transação DESBALANCEADA; uma
``audit_logs``/sequência ``rd_audit_seq`` temporárias corrompiam o encadeamento da auditoria; idem
``rd_reset_screen_anchor``. Tipos não qualificados em DECLARE (``uuid``, ``text``, ``jsonb``) também eram
sombreáveis em todas as funções sem ``search_path``.

Correção: TODAS as 25 funções de ``public`` que ainda não estavam no padrão (inventário completo — as 4
SECURITY DEFINER da 0007 já estavam) são recriadas com ``SET search_path = pg_catalog, public, pg_temp`` e
referências a tabela/sequência qualificadas (``public.``). Só ``CREATE OR REPLACE FUNCTION``: mesmos OIDs,
triggers, assinaturas e ACLs (o REVOKE de EXECUTE do PUBLIC nas definer é preservado). Corpos vigentes:
``app.db.ddl.hardened_function_statements()`` na data da 0008, CONGELADOS em ``FUNCTIONS_0008`` (D3); o
harness aplica o vigente no fim de ``all_statements``.

Âncora write-once (achado na varredura, mesma revisão por decisão do coordenador): ``rd_anchor_write_once``
liberava a troca só pelo GUC ``rd.anchor_reset``, que QUALQUER sessão define com ``SET LOCAL`` — o ``rd_app``
trocava a âncora sem justificativa nem histórico. Agora ``rd_reset_screen_anchor`` é SECURITY DEFINER (EXECUTE
revogado do PUBLIC e concedido só ao ``rd_app``; validações e histórico inalterados) e o write-once só libera
com o GUC **e** ``current_user`` = dono dessa função (lido do catálogo). Downgrade: DROP + CREATE do reset (volta
INVOKER com a ACL padrão) e write-once da 0007.

Funções LEGADAS fora das migrations (``set_updated_at()``, ``normalize_search(text)`` — criadas pelo
``backend/scripts/init-db.sql`` no initdb do container de desenvolvimento; sem uso): se existirem, recebem o
``search_path`` seguro por ``ALTER FUNCTION`` (corpo intocado). Qualquer OUTRA função de ``public`` fora do padrão
faz a migration falhar (transação desfeita) — achado no ensaio sobre a cópia do banco de dev.

Replay histórico preservado: 0003/0004/0007 continuam aplicando os constantes HISTÓRICOS (não editados);
nenhuma migration anterior foi alterada.

Downgrade: restaura os corpos da 0007 a partir da cópia CONGELADA abaixo (``FUNCTIONS_0007`` — byte a byte
igual ao que 0003/0004/0007 criam; o round-trip compara o catálogo, inclusive o md5 de cada função).
Reversível sem perda de dados (nenhuma tabela/linha é tocada).

Revision ID: 0008
Revises: 0007
Create Date: 2026-10-08
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0008"
down_revision: Union[str, None] = "0007"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

SAFE_SEARCH_PATH_0008 = "search_path=pg_catalog, public, pg_temp"

# ─── Corpos da 0007, CONGELADOS (restaurados no downgrade; NÃO editar) ────────
FUNCTIONS_0007: tuple[tuple[str, str], ...] = (
    ("rd_forbid_mutation()", """
CREATE OR REPLACE FUNCTION rd_forbid_mutation() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    RAISE EXCEPTION 'RD_APPEND_ONLY: tabela % é somente-inserção (% proibido)', TG_TABLE_NAME, TG_OP
        USING ERRCODE = 'P0001';
END;
$$
"""),
    ("rd_forbid_column_change()", """
CREATE OR REPLACE FUNCTION rd_forbid_column_change() RETURNS trigger
LANGUAGE plpgsql AS $$
DECLARE
    col text;
    o jsonb;
    n jsonb;
BEGIN
    o := to_jsonb(OLD);
    n := to_jsonb(NEW);
    FOREACH col IN ARRAY TG_ARGV LOOP
        IF (o -> col) IS DISTINCT FROM (n -> col) THEN
            RAISE EXCEPTION 'RD_IMMUTABLE_COLUMN: %.% não pode ser alterada', TG_TABLE_NAME, col
                USING ERRCODE = 'P0001';
        END IF;
    END LOOP;
    RETURN NEW;
END;
$$
"""),
    ("rd_audit_hash_material(audit_logs, text)", """
CREATE OR REPLACE FUNCTION rd_audit_hash_material(r audit_logs, prev text) RETURNS text
LANGUAGE sql STABLE AS $$
    SELECT jsonb_build_array(
        prev,
        r.seq,
        (extract(epoch FROM r.created_at) * 1000000)::bigint,
        r.actor_user_id, r.real_user_id, r.actor_device_id, r.org_id,
        r.action, r.entity_type, r.entity_id,
        r.before_data, r.after_data, r.reason,
        r.ip_address, r.user_agent, r.origin, r.request_id
    )::text
$$
"""),
    ("rd_audit_chain()", """
CREATE OR REPLACE FUNCTION rd_audit_chain() RETURNS trigger
LANGUAGE plpgsql AS $$
DECLARE
    last_hash text;
BEGIN
    IF current_setting('transaction_isolation') <> 'read committed' THEN
        RAISE EXCEPTION 'RD_AUDIT_ISOLATION: auditoria exige READ COMMITTED (atual: %)',
            current_setting('transaction_isolation');
    END IF;
    -- serializa a cadeia: a linha anterior já está confirmada quando o lock é obtido
    PERFORM pg_advisory_xact_lock(7240010001);
    NEW.seq := nextval('rd_audit_seq');
    IF NEW.created_at IS NULL THEN
        NEW.created_at := now();
    END IF;
    SELECT a.row_hash INTO last_hash FROM audit_logs a ORDER BY a.seq DESC LIMIT 1;
    NEW.prev_hash := COALESCE(last_hash, '0000000000000000000000000000000000000000000000000000000000000000');
    NEW.row_hash := encode(sha256(convert_to(rd_audit_hash_material(NEW, NEW.prev_hash), 'UTF8')), 'hex');
    RETURN NEW;
END;
$$
"""),
    ("rd_ledger_check_balanced()", """
CREATE OR REPLACE FUNCTION rd_ledger_check_balanced() RETURNS trigger
LANGUAGE plpgsql AS $$
DECLARE
    tx_id uuid;
    total numeric;
    cnt integer;
BEGIN
    IF TG_TABLE_NAME = 'ledger_transactions' THEN
        tx_id := NEW.id;
    ELSE
        tx_id := NEW.transaction_id;
    END IF;
    SELECT COALESCE(SUM(p.amount_cents), 0), COUNT(*) INTO total, cnt
      FROM ledger_postings p WHERE p.transaction_id = tx_id;
    IF cnt < 2 OR total <> 0 THEN
        RAISE EXCEPTION 'RD_LEDGER_UNBALANCED: transação % desbalanceada (soma %, partidas %)', tx_id, total, cnt
            USING ERRCODE = 'P0001';
    END IF;
    RETURN NULL;
END;
$$
"""),
    ("rd_ledger_transaction_guard()", """
CREATE OR REPLACE FUNCTION rd_ledger_transaction_guard() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    IF EXISTS (SELECT 1 FROM financial_period_closings c
               WHERE NEW.competence_date BETWEEN c.period_start AND c.period_end) THEN
        RAISE EXCEPTION 'RD_LEDGER_PERIOD_CLOSED: competência % pertence a período fechado', NEW.competence_date
            USING ERRCODE = 'P0001';
    END IF;
    RETURN NEW;
END;
$$
"""),
    ("rd_ledger_posting_guard()", """
CREATE OR REPLACE FUNCTION rd_ledger_posting_guard() RETURNS trigger
LANGUAGE plpgsql AS $$
DECLARE
    tx_comp date;
BEGIN
    SELECT t.competence_date INTO tx_comp FROM ledger_transactions t WHERE t.id = NEW.transaction_id;
    IF tx_comp IS NULL THEN
        RAISE EXCEPTION 'RD_LEDGER_ORPHAN_POSTING: transação % inexistente', NEW.transaction_id
            USING ERRCODE = 'P0001';
    END IF;
    IF NEW.competence_date IS DISTINCT FROM tx_comp THEN
        RAISE EXCEPTION 'RD_LEDGER_COMPETENCE_MISMATCH: partida % ≠ transação %', NEW.competence_date, tx_comp
            USING ERRCODE = 'P0001';
    END IF;
    IF EXISTS (SELECT 1 FROM financial_period_closings c
               WHERE NEW.competence_date BETWEEN c.period_start AND c.period_end) THEN
        RAISE EXCEPTION 'RD_LEDGER_PERIOD_CLOSED: competência % pertence a período fechado', NEW.competence_date
            USING ERRCODE = 'P0001';
    END IF;
    RETURN NEW;
END;
$$
"""),
    ("rd_webhook_events_guard()", """
CREATE OR REPLACE FUNCTION rd_webhook_events_guard() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    IF TG_OP = 'DELETE' THEN
        RAISE EXCEPTION 'RD_APPEND_ONLY: webhook_events não pode ser apagada' USING ERRCODE = 'P0001';
    END IF;
    IF NEW.raw_body IS DISTINCT FROM OLD.raw_body
       OR NEW.headers IS DISTINCT FROM OLD.headers
       OR NEW.webhook_event_id IS DISTINCT FROM OLD.webhook_event_id
       OR NEW.provider IS DISTINCT FROM OLD.provider
       OR NEW.instance_slug IS DISTINCT FROM OLD.instance_slug
       OR NEW.signature_valid IS DISTINCT FROM OLD.signature_valid
       OR NEW.received_at IS DISTINCT FROM OLD.received_at THEN
        RAISE EXCEPTION 'RD_IMMUTABLE: dados brutos do webhook são imutáveis' USING ERRCODE = 'P0001';
    END IF;
    RETURN NEW;
END;
$$
"""),
    ("rd_withdrawal_guard()", """
CREATE OR REPLACE FUNCTION rd_withdrawal_guard() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    IF TG_OP = 'DELETE' THEN
        RAISE EXCEPTION 'RD_APPEND_ONLY: saque não pode ser apagado' USING ERRCODE = 'P0001';
    END IF;
    IF NEW.amount_cents IS DISTINCT FROM OLD.amount_cents
       OR NEW.destination_enc IS DISTINCT FROM OLD.destination_enc
       OR NEW.pix_key_type IS DISTINCT FROM OLD.pix_key_type
       OR NEW.kind IS DISTINCT FROM OLD.kind
       OR NEW.establishment_id IS DISTINCT FROM OLD.establishment_id
       OR NEW.approval_request_id IS DISTINCT FROM OLD.approval_request_id
       OR NEW.idempotency_key IS DISTINCT FROM OLD.idempotency_key THEN
        RAISE EXCEPTION 'RD_IMMUTABLE: valor/destino do saque são imutáveis' USING ERRCODE = 'P0001';
    END IF;
    IF OLD.provider_external_id IS NOT NULL AND NEW.provider_external_id IS DISTINCT FROM OLD.provider_external_id THEN
        RAISE EXCEPTION 'RD_IMMUTABLE: external_id do saque não pode mudar' USING ERRCODE = 'P0001';
    END IF;
    IF NEW.status IS DISTINCT FROM OLD.status AND NOT (
           (OLD.status = 'REQUESTED' AND NEW.status IN ('EXECUTING','CANCELLED','FAILED'))
        OR (OLD.status = 'EXECUTING' AND NEW.status IN ('PROCESSING','COMPLETED','FAILED','CANCELLED','UNKNOWN'))
        OR (OLD.status = 'PROCESSING' AND NEW.status IN ('COMPLETED','FAILED','CANCELLED','UNKNOWN'))
        OR (OLD.status = 'UNKNOWN' AND NEW.status IN ('PROCESSING','COMPLETED','FAILED','CANCELLED'))
    ) THEN
        RAISE EXCEPTION 'RD_WITHDRAWAL_TRANSITION: % → % proibido', OLD.status, NEW.status USING ERRCODE = 'P0001';
    END IF;
    RETURN NEW;
END;
$$
"""),
    ("rd_approval_check_authorization(financial_approval_requests)", """
CREATE OR REPLACE FUNCTION rd_approval_check_authorization(r financial_approval_requests) RETURNS void
LANGUAGE plpgsql AS $$
DECLARE
    cur text;
BEGIN
    cur := public.rd_security_current_approval_mode();
    IF cur IS NULL THEN
        RAISE EXCEPTION 'RD_INSTANCE_NOT_INITIALIZED: modo de aprovação não inicializado' USING ERRCODE = 'P0001';
    END IF;
    IF r.approval_mode IS DISTINCT FROM cur THEN
        RAISE EXCEPTION 'RD_APPROVAL_MODE_MISMATCH: pedido % é do modo %, instância está em %', r.id, r.approval_mode, cur
            USING ERRCODE = 'P0001';
    END IF;
    IF r.approval_mode = 'DUAL' THEN
        IF r.approved_by IS NULL OR r.approved_by = r.requested_by OR NOT public.rd_is_active_master(r.approved_by) THEN
            RAISE EXCEPTION 'RD_APPROVER_NOT_ACTIVE_MASTER: aprovador do pedido % não é outro Admin Master ativo', r.id
                USING ERRCODE = 'P0001';
        END IF;
    ELSE
        IF r.self_authorized_by IS NULL OR r.self_authorized_by <> r.requested_by
           OR NOT public.rd_is_active_master(r.self_authorized_by) THEN
            RAISE EXCEPTION 'RD_SELF_AUTHORIZER_INVALID: autorização única do pedido % inválida', r.id
                USING ERRCODE = 'P0001';
        END IF;
    END IF;
END;
$$
"""),
    ("rd_approval_request_guard()", """
CREATE OR REPLACE FUNCTION rd_approval_request_guard() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    IF TG_OP = 'DELETE' THEN
        RAISE EXCEPTION 'RD_APPEND_ONLY: pedido de aprovação não pode ser apagado' USING ERRCODE = 'P0001';
    END IF;
    IF NEW.action_type IS DISTINCT FROM OLD.action_type
       OR NEW.payload IS DISTINCT FROM OLD.payload
       OR NEW.payload_sha256 IS DISTINCT FROM OLD.payload_sha256
       OR NEW.amount_cents IS DISTINCT FROM OLD.amount_cents
       OR NEW.reason IS DISTINCT FROM OLD.reason
       OR NEW.requested_by IS DISTINCT FROM OLD.requested_by
       OR NEW.requested_at IS DISTINCT FROM OLD.requested_at
       OR NEW.idempotency_key IS DISTINCT FROM OLD.idempotency_key
       OR NEW.expires_at IS DISTINCT FROM OLD.expires_at THEN
        RAISE EXCEPTION 'RD_IMMUTABLE: conteúdo do pedido de aprovação é imutável' USING ERRCODE = 'P0001';
    END IF;
    IF NEW.approval_mode IS DISTINCT FROM OLD.approval_mode THEN
        RAISE EXCEPTION 'RD_IMMUTABLE: modo de aprovação do pedido é imutável' USING ERRCODE = 'P0001';
    END IF;
    IF OLD.status <> 'PENDING' AND NEW.status IS DISTINCT FROM OLD.status THEN
        RAISE EXCEPTION 'RD_APPROVAL_FINAL: status % é final', OLD.status USING ERRCODE = 'P0001';
    END IF;
    IF OLD.approved_by IS NOT NULL AND NEW.approved_by IS DISTINCT FROM OLD.approved_by THEN
        RAISE EXCEPTION 'RD_IMMUTABLE: aprovador não pode mudar' USING ERRCODE = 'P0001';
    END IF;
    IF (OLD.self_authorized_by IS NOT NULL AND NEW.self_authorized_by IS DISTINCT FROM OLD.self_authorized_by)
       OR (OLD.self_authorized_at IS NOT NULL AND NEW.self_authorized_at IS DISTINCT FROM OLD.self_authorized_at) THEN
        RAISE EXCEPTION 'RD_IMMUTABLE: autorização única não pode mudar' USING ERRCODE = 'P0001';
    END IF;
    IF OLD.status = 'PENDING' AND NEW.status = 'APPROVED' THEN
        PERFORM public.rd_approval_check_authorization(NEW);
    END IF;
    IF NEW.execution_state IS DISTINCT FROM OLD.execution_state AND NOT (
           (OLD.execution_state = 'NOT_STARTED' AND NEW.execution_state IN ('PENDING','EXECUTED','BLOCKED_EXTERNAL','FAILED'))
        OR (OLD.execution_state = 'PENDING' AND NEW.execution_state IN ('EXECUTING','EXECUTED','FAILED','BLOCKED_EXTERNAL'))
        OR (OLD.execution_state = 'EXECUTING' AND NEW.execution_state IN ('EXECUTED','FAILED','UNKNOWN'))
        OR (OLD.execution_state = 'UNKNOWN' AND NEW.execution_state IN ('EXECUTED','FAILED'))
        OR (OLD.execution_state = 'BLOCKED_EXTERNAL' AND NEW.execution_state IN ('PENDING','EXECUTED'))
    ) THEN
        RAISE EXCEPTION 'RD_EXECUTION_TRANSITION: % → % proibido', OLD.execution_state, NEW.execution_state
            USING ERRCODE = 'P0001';
    END IF;
    RETURN NEW;
END;
$$
"""),
    ("rd_approval_request_insert_guard()", """
CREATE OR REPLACE FUNCTION rd_approval_request_insert_guard() RETURNS trigger
LANGUAGE plpgsql AS $$
DECLARE
    cur text;
BEGIN
    cur := public.rd_security_current_approval_mode();
    IF cur IS NULL THEN
        RAISE EXCEPTION 'RD_INSTANCE_NOT_INITIALIZED: modo de aprovação não inicializado' USING ERRCODE = 'P0001';
    END IF;
    IF NEW.approval_mode IS DISTINCT FROM cur THEN
        RAISE EXCEPTION 'RD_APPROVAL_MODE_MISMATCH: pedido do modo %, instância está em %', NEW.approval_mode, cur
            USING ERRCODE = 'P0001';
    END IF;
    IF NOT public.rd_is_active_master(NEW.requested_by) THEN
        RAISE EXCEPTION 'RD_REQUESTER_NOT_ACTIVE_MASTER: solicitante não é Admin Master ativo' USING ERRCODE = 'P0001';
    END IF;
    IF cur = 'DUAL' AND public.rd_active_master_count() < 2 THEN
        RAISE EXCEPTION 'RD_SECOND_MASTER_REQUIRED: dupla aprovação exige dois Admin Master ativos'
            USING ERRCODE = 'P0001';
    END IF;
    IF NEW.status IS NULL OR NEW.status NOT IN ('PENDING','APPROVED') THEN
        RAISE EXCEPTION 'RD_APPROVAL_INVALID_STATE: pedido nasce PENDING (ou APPROVED já autorizado), não %', NEW.status
            USING ERRCODE = 'P0001';
    END IF;
    IF NEW.status = 'APPROVED' THEN
        PERFORM public.rd_approval_check_authorization(NEW);
    END IF;
    RETURN NEW;
END;
$$
"""),
    ("rd_anchor_write_once()", """
CREATE OR REPLACE FUNCTION rd_anchor_write_once() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    IF OLD.anchor_utc IS NOT NULL
       AND NEW.anchor_utc IS DISTINCT FROM OLD.anchor_utc
       AND coalesce(current_setting('rd.anchor_reset', true), '') <> 'on' THEN
        RAISE EXCEPTION 'RD_ANCHOR_WRITE_ONCE: a âncora da tela % já foi definida', OLD.screen_id
            USING ERRCODE = 'P0001';
    END IF;
    RETURN NEW;
END;
$$
"""),
    ("rd_reset_screen_anchor(uuid, timestamptz, text, uuid)", """
CREATE OR REPLACE FUNCTION rd_reset_screen_anchor(p_screen uuid, p_new timestamptz, p_reason text, p_actor uuid)
RETURNS timestamptz
LANGUAGE plpgsql AS $$
DECLARE
    v_old timestamptz;
BEGIN
    IF p_reason IS NULL OR length(btrim(p_reason)) < 10 THEN
        RAISE EXCEPTION 'RD_ANCHOR_RESET_REASON: justificativa obrigatória (mínimo 10 caracteres)'
            USING ERRCODE = 'P0001';
    END IF;
    SELECT s.anchor_utc INTO v_old FROM screen_inventory_state s WHERE s.screen_id = p_screen FOR UPDATE;
    IF NOT FOUND THEN
        RAISE EXCEPTION 'RD_ANCHOR_RESET_NOT_FOUND: tela % sem estado de inventário', p_screen
            USING ERRCODE = 'P0001';
    END IF;
    PERFORM set_config('rd.anchor_reset', 'on', true);
    UPDATE screen_inventory_state
       SET anchor_utc = p_new,
           anchor_source = CASE WHEN p_new IS NULL THEN NULL ELSE 'ADMIN_RESET' END,
           anchor_device_id = NULL,
           anchor_reported_at = now(),
           updated_at = now()
     WHERE screen_id = p_screen;
    PERFORM set_config('rd.anchor_reset', 'off', true);
    INSERT INTO screen_anchor_history (id, screen_id, old_anchor, new_anchor, source, reason, actor_user_id, created_at)
    VALUES (gen_random_uuid(), p_screen, v_old, p_new, 'ADMIN_RESET', p_reason, p_actor, now());
    RETURN p_new;
END;
$$
"""),
    ("rd_contract_template_guard()", """
CREATE OR REPLACE FUNCTION rd_contract_template_guard() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    IF TG_OP = 'DELETE' THEN
        IF OLD.status <> 'DRAFT' THEN
            RAISE EXCEPTION 'RD_CONTRACT_TEMPLATE_IMMUTABLE: modelo publicado não pode ser apagado'
                USING ERRCODE = 'P0001';
        END IF;
        RETURN OLD;
    END IF;
    IF OLD.status <> 'DRAFT' THEN
        IF NEW.body IS DISTINCT FROM OLD.body OR NEW.body_sha256 IS DISTINCT FROM OLD.body_sha256
           OR NEW.title IS DISTINCT FROM OLD.title OR NEW.kind IS DISTINCT FROM OLD.kind
           OR NEW.version IS DISTINCT FROM OLD.version THEN
            RAISE EXCEPTION 'RD_CONTRACT_TEMPLATE_IMMUTABLE: texto de modelo publicado não pode mudar'
                USING ERRCODE = 'P0001';
        END IF;
        IF NEW.status = 'DRAFT' THEN
            RAISE EXCEPTION 'RD_CONTRACT_TEMPLATE_IMMUTABLE: modelo publicado não volta a rascunho'
                USING ERRCODE = 'P0001';
        END IF;
    END IF;
    RETURN NEW;
END;
$$
"""),
    ("rd_screen_decommission_guard()", """
CREATE OR REPLACE FUNCTION rd_screen_decommission_guard() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    IF OLD.status = 'DECOMMISSIONED' AND (NEW.status <> 'DECOMMISSIONED' OR NEW.sellable) THEN
        RAISE EXCEPTION 'RD_SCREEN_DECOMMISSIONED: tela retirada definitivamente não pode ser reativada'
            USING ERRCODE = 'P0001';
    END IF;
    RETURN NEW;
END;
$$
"""),
    ("rd_design_version_guard()", """
CREATE OR REPLACE FUNCTION rd_design_version_guard() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    IF TG_OP = 'DELETE' THEN
        IF OLD.locked THEN
            RAISE EXCEPTION 'RD_DESIGN_VERSION_LOCKED: versão % aprovada é imutável', OLD.id USING ERRCODE = 'P0001';
        END IF;
        RETURN OLD;
    END IF;
    IF OLD.locked THEN
        RAISE EXCEPTION 'RD_DESIGN_VERSION_LOCKED: versão % aprovada é imutável', OLD.id USING ERRCODE = 'P0001';
    END IF;
    IF NEW.storage_key IS DISTINCT FROM OLD.storage_key OR NEW.sha256 IS DISTINCT FROM OLD.sha256
       OR NEW.number IS DISTINCT FROM OLD.number OR NEW.request_id IS DISTINCT FROM OLD.request_id
       OR NEW.size_bytes IS DISTINCT FROM OLD.size_bytes OR NEW.content_type IS DISTINCT FROM OLD.content_type
       OR NEW.created_by IS DISTINCT FROM OLD.created_by THEN
        RAISE EXCEPTION 'RD_DESIGN_VERSION_IMMUTABLE: conteúdo da versão % não pode mudar', OLD.id USING ERRCODE = 'P0001';
    END IF;
    RETURN NEW;
END;
$$
"""),
    ("rd_security_privileged()", """
CREATE OR REPLACE FUNCTION rd_security_privileged() RETURNS boolean
LANGUAGE sql STABLE AS $$
    SELECT current_user = (SELECT pg_catalog.pg_get_userbyid(c.relowner)
                             FROM pg_catalog.pg_class c WHERE c.oid = 'public.users'::regclass)
        OR COALESCE((SELECT r.rolsuper FROM pg_catalog.pg_roles r WHERE r.rolname = current_user), false)
$$
"""),
    ("rd_security_current_approval_mode()", """
CREATE OR REPLACE FUNCTION rd_security_current_approval_mode() RETURNS text
LANGUAGE sql STABLE AS $$
    SELECT h.to_mode::text FROM public.instance_approval_mode_history h ORDER BY h.mode_version DESC LIMIT 1
$$
"""),
    ("rd_is_active_master(uuid)", """
CREATE OR REPLACE FUNCTION rd_is_active_master(p_user uuid) RETURNS boolean
LANGUAGE sql STABLE AS $$
    SELECT EXISTS (SELECT 1 FROM public.users u WHERE u.id = p_user AND u.admin_master_seat IS NOT NULL AND u.is_active AND u.anonymized_at IS NULL AND u.totp_enabled AND u.financial_password_hash IS NOT NULL AND NOT u.must_change_password AND EXISTS (SELECT 1 FROM public.role_assignments ra JOIN public.roles r ON r.id = ra.role_id WHERE ra.user_id = u.id AND r.slug = 'super_admin' AND ra.scope_type = 'GLOBAL' AND (ra.expires_at IS NULL OR ra.expires_at > now())))
$$
"""),
    ("rd_active_master_count()", """
CREATE OR REPLACE FUNCTION rd_active_master_count() RETURNS integer
LANGUAGE sql STABLE AS $$
    SELECT count(*)::integer FROM public.users u WHERE u.admin_master_seat IS NOT NULL AND u.is_active AND u.anonymized_at IS NULL AND u.totp_enabled AND u.financial_password_hash IS NOT NULL AND NOT u.must_change_password AND EXISTS (SELECT 1 FROM public.role_assignments ra JOIN public.roles r ON r.id = ra.role_id WHERE ra.user_id = u.id AND r.slug = 'super_admin' AND ra.scope_type = 'GLOBAL' AND (ra.expires_at IS NULL OR ra.expires_at > now()))
$$
"""),
    ("rd_users_master_guard()", """
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
"""),
    ("rd_users_master_log()", """
CREATE OR REPLACE FUNCTION rd_users_master_log() RETURNS trigger
LANGUAGE plpgsql AS $$
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
    IF cardinality(cols) > 0 THEN
        RAISE LOG 'RD_SECURITY_EVENT master_user_changed user=% seat=% columns=% by=% app=% addr=%',
            NEW.id, COALESCE(NEW.admin_master_seat, OLD.admin_master_seat), array_to_string(cols, ','), session_user, current_setting('application_name', true), inet_client_addr();
    END IF;
    RETURN NULL;
END;
$$
"""),
    ("rd_role_assignments_master_guard()", """
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
"""),
    ("rd_admin_master_invite_guard()", """
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
"""),
)


# ─── Statements APLICADOS pela 0008, CONGELADOS (D3 do revisor — NÃO editar) ─────────────────
# Gerados do código vigente na data da 0008 (= o que foi aplicado no dev em 2026-10-08 21:31; prova por
# pg_get_functiondef em tests/migrations/test_migrations_0008.py e E3_*.log). Mudança futura de corpo entra
# por NOVA migration — a 0008 não depende mais de app.db.ddl.hardened_function_statements().
FUNCTIONS_0008: tuple[str, ...] = (
    """
CREATE OR REPLACE FUNCTION rd_forbid_mutation() RETURNS trigger
LANGUAGE plpgsql
SET search_path = pg_catalog, public, pg_temp AS $$
BEGIN
    RAISE EXCEPTION 'RD_APPEND_ONLY: tabela % é somente-inserção (% proibido)', TG_TABLE_NAME, TG_OP
        USING ERRCODE = 'P0001';
END;
$$
""",
    """
CREATE OR REPLACE FUNCTION rd_forbid_column_change() RETURNS trigger
LANGUAGE plpgsql
SET search_path = pg_catalog, public, pg_temp AS $$
DECLARE
    col text;
    o jsonb;
    n jsonb;
BEGIN
    o := to_jsonb(OLD);
    n := to_jsonb(NEW);
    FOREACH col IN ARRAY TG_ARGV LOOP
        IF (o -> col) IS DISTINCT FROM (n -> col) THEN
            RAISE EXCEPTION 'RD_IMMUTABLE_COLUMN: %.% não pode ser alterada', TG_TABLE_NAME, col
                USING ERRCODE = 'P0001';
        END IF;
    END LOOP;
    RETURN NEW;
END;
$$
""",
    """
CREATE OR REPLACE FUNCTION rd_audit_hash_material(r audit_logs, prev text) RETURNS text
LANGUAGE sql STABLE
SET search_path = pg_catalog, public, pg_temp AS $$
    SELECT jsonb_build_array(
        prev,
        r.seq,
        (extract(epoch FROM r.created_at) * 1000000)::bigint,
        r.actor_user_id, r.real_user_id, r.actor_device_id, r.org_id,
        r.action, r.entity_type, r.entity_id,
        r.before_data, r.after_data, r.reason,
        r.ip_address, r.user_agent, r.origin, r.request_id
    )::text
$$
""",
    """
CREATE OR REPLACE FUNCTION rd_audit_chain() RETURNS trigger
LANGUAGE plpgsql
SET search_path = pg_catalog, public, pg_temp AS $$
DECLARE
    last_hash text;
BEGIN
    IF current_setting('transaction_isolation') <> 'read committed' THEN
        RAISE EXCEPTION 'RD_AUDIT_ISOLATION: auditoria exige READ COMMITTED (atual: %)',
            current_setting('transaction_isolation');
    END IF;
    -- serializa a cadeia: a linha anterior já está confirmada quando o lock é obtido
    PERFORM pg_advisory_xact_lock(7240010001);
    NEW.seq := nextval('public.rd_audit_seq');
    IF NEW.created_at IS NULL THEN
        NEW.created_at := now();
    END IF;
    SELECT a.row_hash INTO last_hash FROM public.audit_logs a ORDER BY a.seq DESC LIMIT 1;
    NEW.prev_hash := COALESCE(last_hash, '0000000000000000000000000000000000000000000000000000000000000000');
    NEW.row_hash := encode(sha256(convert_to(public.rd_audit_hash_material(NEW, NEW.prev_hash), 'UTF8')), 'hex');
    RETURN NEW;
END;
$$
""",
    """
CREATE OR REPLACE FUNCTION rd_ledger_check_balanced() RETURNS trigger
LANGUAGE plpgsql
SET search_path = pg_catalog, public, pg_temp AS $$
DECLARE
    tx_id uuid;
    total numeric;
    cnt integer;
BEGIN
    IF TG_TABLE_NAME = 'ledger_transactions' THEN
        tx_id := NEW.id;
    ELSE
        tx_id := NEW.transaction_id;
    END IF;
    SELECT COALESCE(SUM(p.amount_cents), 0), COUNT(*) INTO total, cnt
      FROM public.ledger_postings p WHERE p.transaction_id = tx_id;
    IF cnt < 2 OR total <> 0 THEN
        RAISE EXCEPTION 'RD_LEDGER_UNBALANCED: transação % desbalanceada (soma %, partidas %)', tx_id, total, cnt
            USING ERRCODE = 'P0001';
    END IF;
    RETURN NULL;
END;
$$
""",
    """
CREATE OR REPLACE FUNCTION rd_ledger_transaction_guard() RETURNS trigger
LANGUAGE plpgsql
SET search_path = pg_catalog, public, pg_temp AS $$
BEGIN
    IF EXISTS (SELECT 1 FROM public.financial_period_closings c
               WHERE NEW.competence_date BETWEEN c.period_start AND c.period_end) THEN
        RAISE EXCEPTION 'RD_LEDGER_PERIOD_CLOSED: competência % pertence a período fechado', NEW.competence_date
            USING ERRCODE = 'P0001';
    END IF;
    RETURN NEW;
END;
$$
""",
    """
CREATE OR REPLACE FUNCTION rd_ledger_posting_guard() RETURNS trigger
LANGUAGE plpgsql
SET search_path = pg_catalog, public, pg_temp AS $$
DECLARE
    tx_comp date;
BEGIN
    SELECT t.competence_date INTO tx_comp FROM public.ledger_transactions t WHERE t.id = NEW.transaction_id;
    IF tx_comp IS NULL THEN
        RAISE EXCEPTION 'RD_LEDGER_ORPHAN_POSTING: transação % inexistente', NEW.transaction_id
            USING ERRCODE = 'P0001';
    END IF;
    IF NEW.competence_date IS DISTINCT FROM tx_comp THEN
        RAISE EXCEPTION 'RD_LEDGER_COMPETENCE_MISMATCH: partida % ≠ transação %', NEW.competence_date, tx_comp
            USING ERRCODE = 'P0001';
    END IF;
    IF EXISTS (SELECT 1 FROM public.financial_period_closings c
               WHERE NEW.competence_date BETWEEN c.period_start AND c.period_end) THEN
        RAISE EXCEPTION 'RD_LEDGER_PERIOD_CLOSED: competência % pertence a período fechado', NEW.competence_date
            USING ERRCODE = 'P0001';
    END IF;
    RETURN NEW;
END;
$$
""",
    """
CREATE OR REPLACE FUNCTION rd_webhook_events_guard() RETURNS trigger
LANGUAGE plpgsql
SET search_path = pg_catalog, public, pg_temp AS $$
BEGIN
    IF TG_OP = 'DELETE' THEN
        RAISE EXCEPTION 'RD_APPEND_ONLY: webhook_events não pode ser apagada' USING ERRCODE = 'P0001';
    END IF;
    IF NEW.raw_body IS DISTINCT FROM OLD.raw_body
       OR NEW.headers IS DISTINCT FROM OLD.headers
       OR NEW.webhook_event_id IS DISTINCT FROM OLD.webhook_event_id
       OR NEW.provider IS DISTINCT FROM OLD.provider
       OR NEW.instance_slug IS DISTINCT FROM OLD.instance_slug
       OR NEW.signature_valid IS DISTINCT FROM OLD.signature_valid
       OR NEW.received_at IS DISTINCT FROM OLD.received_at THEN
        RAISE EXCEPTION 'RD_IMMUTABLE: dados brutos do webhook são imutáveis' USING ERRCODE = 'P0001';
    END IF;
    RETURN NEW;
END;
$$
""",
    """
CREATE OR REPLACE FUNCTION rd_withdrawal_guard() RETURNS trigger
LANGUAGE plpgsql
SET search_path = pg_catalog, public, pg_temp AS $$
BEGIN
    IF TG_OP = 'DELETE' THEN
        RAISE EXCEPTION 'RD_APPEND_ONLY: saque não pode ser apagado' USING ERRCODE = 'P0001';
    END IF;
    IF NEW.amount_cents IS DISTINCT FROM OLD.amount_cents
       OR NEW.destination_enc IS DISTINCT FROM OLD.destination_enc
       OR NEW.pix_key_type IS DISTINCT FROM OLD.pix_key_type
       OR NEW.kind IS DISTINCT FROM OLD.kind
       OR NEW.establishment_id IS DISTINCT FROM OLD.establishment_id
       OR NEW.approval_request_id IS DISTINCT FROM OLD.approval_request_id
       OR NEW.idempotency_key IS DISTINCT FROM OLD.idempotency_key THEN
        RAISE EXCEPTION 'RD_IMMUTABLE: valor/destino do saque são imutáveis' USING ERRCODE = 'P0001';
    END IF;
    IF OLD.provider_external_id IS NOT NULL AND NEW.provider_external_id IS DISTINCT FROM OLD.provider_external_id THEN
        RAISE EXCEPTION 'RD_IMMUTABLE: external_id do saque não pode mudar' USING ERRCODE = 'P0001';
    END IF;
    IF NEW.status IS DISTINCT FROM OLD.status AND NOT (
           (OLD.status = 'REQUESTED' AND NEW.status IN ('EXECUTING','CANCELLED','FAILED'))
        OR (OLD.status = 'EXECUTING' AND NEW.status IN ('PROCESSING','COMPLETED','FAILED','CANCELLED','UNKNOWN'))
        OR (OLD.status = 'PROCESSING' AND NEW.status IN ('COMPLETED','FAILED','CANCELLED','UNKNOWN'))
        OR (OLD.status = 'UNKNOWN' AND NEW.status IN ('PROCESSING','COMPLETED','FAILED','CANCELLED'))
    ) THEN
        RAISE EXCEPTION 'RD_WITHDRAWAL_TRANSITION: % → % proibido', OLD.status, NEW.status USING ERRCODE = 'P0001';
    END IF;
    RETURN NEW;
END;
$$
""",
    """
CREATE OR REPLACE FUNCTION rd_approval_check_authorization(r financial_approval_requests) RETURNS void
LANGUAGE plpgsql
SET search_path = pg_catalog, public, pg_temp AS $$
DECLARE
    cur text;
BEGIN
    cur := public.rd_security_current_approval_mode();
    IF cur IS NULL THEN
        RAISE EXCEPTION 'RD_INSTANCE_NOT_INITIALIZED: modo de aprovação não inicializado' USING ERRCODE = 'P0001';
    END IF;
    IF r.approval_mode IS DISTINCT FROM cur THEN
        RAISE EXCEPTION 'RD_APPROVAL_MODE_MISMATCH: pedido % é do modo %, instância está em %', r.id, r.approval_mode, cur
            USING ERRCODE = 'P0001';
    END IF;
    IF r.approval_mode = 'DUAL' THEN
        IF r.approved_by IS NULL OR r.approved_by = r.requested_by OR NOT public.rd_is_active_master(r.approved_by) THEN
            RAISE EXCEPTION 'RD_APPROVER_NOT_ACTIVE_MASTER: aprovador do pedido % não é outro Admin Master ativo', r.id
                USING ERRCODE = 'P0001';
        END IF;
    ELSE
        IF r.self_authorized_by IS NULL OR r.self_authorized_by <> r.requested_by
           OR NOT public.rd_is_active_master(r.self_authorized_by) THEN
            RAISE EXCEPTION 'RD_SELF_AUTHORIZER_INVALID: autorização única do pedido % inválida', r.id
                USING ERRCODE = 'P0001';
        END IF;
    END IF;
END;
$$
""",
    """
CREATE OR REPLACE FUNCTION rd_approval_request_guard() RETURNS trigger
LANGUAGE plpgsql
SET search_path = pg_catalog, public, pg_temp AS $$
BEGIN
    IF TG_OP = 'DELETE' THEN
        RAISE EXCEPTION 'RD_APPEND_ONLY: pedido de aprovação não pode ser apagado' USING ERRCODE = 'P0001';
    END IF;
    IF NEW.action_type IS DISTINCT FROM OLD.action_type
       OR NEW.payload IS DISTINCT FROM OLD.payload
       OR NEW.payload_sha256 IS DISTINCT FROM OLD.payload_sha256
       OR NEW.amount_cents IS DISTINCT FROM OLD.amount_cents
       OR NEW.reason IS DISTINCT FROM OLD.reason
       OR NEW.requested_by IS DISTINCT FROM OLD.requested_by
       OR NEW.requested_at IS DISTINCT FROM OLD.requested_at
       OR NEW.idempotency_key IS DISTINCT FROM OLD.idempotency_key
       OR NEW.expires_at IS DISTINCT FROM OLD.expires_at THEN
        RAISE EXCEPTION 'RD_IMMUTABLE: conteúdo do pedido de aprovação é imutável' USING ERRCODE = 'P0001';
    END IF;
    IF NEW.approval_mode IS DISTINCT FROM OLD.approval_mode THEN
        RAISE EXCEPTION 'RD_IMMUTABLE: modo de aprovação do pedido é imutável' USING ERRCODE = 'P0001';
    END IF;
    IF OLD.status <> 'PENDING' AND NEW.status IS DISTINCT FROM OLD.status THEN
        RAISE EXCEPTION 'RD_APPROVAL_FINAL: status % é final', OLD.status USING ERRCODE = 'P0001';
    END IF;
    IF OLD.approved_by IS NOT NULL AND NEW.approved_by IS DISTINCT FROM OLD.approved_by THEN
        RAISE EXCEPTION 'RD_IMMUTABLE: aprovador não pode mudar' USING ERRCODE = 'P0001';
    END IF;
    IF (OLD.self_authorized_by IS NOT NULL AND NEW.self_authorized_by IS DISTINCT FROM OLD.self_authorized_by)
       OR (OLD.self_authorized_at IS NOT NULL AND NEW.self_authorized_at IS DISTINCT FROM OLD.self_authorized_at) THEN
        RAISE EXCEPTION 'RD_IMMUTABLE: autorização única não pode mudar' USING ERRCODE = 'P0001';
    END IF;
    IF OLD.status = 'PENDING' AND NEW.status = 'APPROVED' THEN
        PERFORM public.rd_approval_check_authorization(NEW);
    END IF;
    IF NEW.execution_state IS DISTINCT FROM OLD.execution_state AND NOT (
           (OLD.execution_state = 'NOT_STARTED' AND NEW.execution_state IN ('PENDING','EXECUTED','BLOCKED_EXTERNAL','FAILED'))
        OR (OLD.execution_state = 'PENDING' AND NEW.execution_state IN ('EXECUTING','EXECUTED','FAILED','BLOCKED_EXTERNAL'))
        OR (OLD.execution_state = 'EXECUTING' AND NEW.execution_state IN ('EXECUTED','FAILED','UNKNOWN'))
        OR (OLD.execution_state = 'UNKNOWN' AND NEW.execution_state IN ('EXECUTED','FAILED'))
        OR (OLD.execution_state = 'BLOCKED_EXTERNAL' AND NEW.execution_state IN ('PENDING','EXECUTED'))
    ) THEN
        RAISE EXCEPTION 'RD_EXECUTION_TRANSITION: % → % proibido', OLD.execution_state, NEW.execution_state
            USING ERRCODE = 'P0001';
    END IF;
    RETURN NEW;
END;
$$
""",
    """
CREATE OR REPLACE FUNCTION rd_approval_request_insert_guard() RETURNS trigger
LANGUAGE plpgsql
SET search_path = pg_catalog, public, pg_temp AS $$
DECLARE
    cur text;
BEGIN
    cur := public.rd_security_current_approval_mode();
    IF cur IS NULL THEN
        RAISE EXCEPTION 'RD_INSTANCE_NOT_INITIALIZED: modo de aprovação não inicializado' USING ERRCODE = 'P0001';
    END IF;
    IF NEW.approval_mode IS DISTINCT FROM cur THEN
        RAISE EXCEPTION 'RD_APPROVAL_MODE_MISMATCH: pedido do modo %, instância está em %', NEW.approval_mode, cur
            USING ERRCODE = 'P0001';
    END IF;
    IF NOT public.rd_is_active_master(NEW.requested_by) THEN
        RAISE EXCEPTION 'RD_REQUESTER_NOT_ACTIVE_MASTER: solicitante não é Admin Master ativo' USING ERRCODE = 'P0001';
    END IF;
    IF cur = 'DUAL' AND public.rd_active_master_count() < 2 THEN
        RAISE EXCEPTION 'RD_SECOND_MASTER_REQUIRED: dupla aprovação exige dois Admin Master ativos'
            USING ERRCODE = 'P0001';
    END IF;
    IF NEW.status IS NULL OR NEW.status NOT IN ('PENDING','APPROVED') THEN
        RAISE EXCEPTION 'RD_APPROVAL_INVALID_STATE: pedido nasce PENDING (ou APPROVED já autorizado), não %', NEW.status
            USING ERRCODE = 'P0001';
    END IF;
    IF NEW.status = 'APPROVED' THEN
        PERFORM public.rd_approval_check_authorization(NEW);
    END IF;
    RETURN NEW;
END;
$$
""",
    """
CREATE OR REPLACE FUNCTION rd_anchor_write_once() RETURNS trigger
LANGUAGE plpgsql
SET search_path = pg_catalog, public, pg_temp AS $$
BEGIN
    IF OLD.anchor_utc IS NOT NULL
       AND NEW.anchor_utc IS DISTINCT FROM OLD.anchor_utc
       AND NOT (coalesce(current_setting('rd.anchor_reset', true), '') = 'on'
                AND current_user::text IS NOT DISTINCT FROM (
                    SELECT pg_catalog.pg_get_userbyid(p.proowner)::text FROM pg_catalog.pg_proc p
                     WHERE p.oid = pg_catalog.to_regprocedure('public.rd_reset_screen_anchor(uuid,timestamptz,text,uuid)'))) THEN
        RAISE EXCEPTION 'RD_ANCHOR_WRITE_ONCE: a âncora da tela % já foi definida', OLD.screen_id
            USING ERRCODE = 'P0001';
    END IF;
    RETURN NEW;
END;
$$
""",
    """
CREATE OR REPLACE FUNCTION rd_reset_screen_anchor(p_screen uuid, p_new timestamptz, p_reason text, p_actor uuid)
RETURNS timestamptz
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, public, pg_temp AS $$
DECLARE
    v_old timestamptz;
BEGIN
    IF p_reason IS NULL OR length(btrim(p_reason)) < 10 THEN
        RAISE EXCEPTION 'RD_ANCHOR_RESET_REASON: justificativa obrigatória (mínimo 10 caracteres)'
            USING ERRCODE = 'P0001';
    END IF;
    SELECT s.anchor_utc INTO v_old FROM public.screen_inventory_state s WHERE s.screen_id = p_screen FOR UPDATE;
    IF NOT FOUND THEN
        RAISE EXCEPTION 'RD_ANCHOR_RESET_NOT_FOUND: tela % sem estado de inventário', p_screen
            USING ERRCODE = 'P0001';
    END IF;
    PERFORM set_config('rd.anchor_reset', 'on', true);
    UPDATE public.screen_inventory_state
       SET anchor_utc = p_new,
           anchor_source = CASE WHEN p_new IS NULL THEN NULL ELSE 'ADMIN_RESET' END,
           anchor_device_id = NULL,
           anchor_reported_at = now(),
           updated_at = now()
     WHERE screen_id = p_screen;
    PERFORM set_config('rd.anchor_reset', 'off', true);
    INSERT INTO public.screen_anchor_history (id, screen_id, old_anchor, new_anchor, source, reason, actor_user_id, created_at)
    VALUES (gen_random_uuid(), p_screen, v_old, p_new, 'ADMIN_RESET', p_reason, p_actor, now());
    RETURN p_new;
END;
$$
""",
    """REVOKE ALL ON FUNCTION rd_reset_screen_anchor(uuid, timestamptz, text, uuid) FROM PUBLIC""",
    """
CREATE OR REPLACE FUNCTION rd_contract_template_guard() RETURNS trigger
LANGUAGE plpgsql
SET search_path = pg_catalog, public, pg_temp AS $$
BEGIN
    IF TG_OP = 'DELETE' THEN
        IF OLD.status <> 'DRAFT' THEN
            RAISE EXCEPTION 'RD_CONTRACT_TEMPLATE_IMMUTABLE: modelo publicado não pode ser apagado'
                USING ERRCODE = 'P0001';
        END IF;
        RETURN OLD;
    END IF;
    IF OLD.status <> 'DRAFT' THEN
        IF NEW.body IS DISTINCT FROM OLD.body OR NEW.body_sha256 IS DISTINCT FROM OLD.body_sha256
           OR NEW.title IS DISTINCT FROM OLD.title OR NEW.kind IS DISTINCT FROM OLD.kind
           OR NEW.version IS DISTINCT FROM OLD.version THEN
            RAISE EXCEPTION 'RD_CONTRACT_TEMPLATE_IMMUTABLE: texto de modelo publicado não pode mudar'
                USING ERRCODE = 'P0001';
        END IF;
        IF NEW.status = 'DRAFT' THEN
            RAISE EXCEPTION 'RD_CONTRACT_TEMPLATE_IMMUTABLE: modelo publicado não volta a rascunho'
                USING ERRCODE = 'P0001';
        END IF;
    END IF;
    RETURN NEW;
END;
$$
""",
    """
CREATE OR REPLACE FUNCTION rd_screen_decommission_guard() RETURNS trigger
LANGUAGE plpgsql
SET search_path = pg_catalog, public, pg_temp AS $$
BEGIN
    IF OLD.status = 'DECOMMISSIONED' AND (NEW.status <> 'DECOMMISSIONED' OR NEW.sellable) THEN
        RAISE EXCEPTION 'RD_SCREEN_DECOMMISSIONED: tela retirada definitivamente não pode ser reativada'
            USING ERRCODE = 'P0001';
    END IF;
    RETURN NEW;
END;
$$
""",
    """
CREATE OR REPLACE FUNCTION rd_design_version_guard() RETURNS trigger
LANGUAGE plpgsql
SET search_path = pg_catalog, public, pg_temp AS $$
BEGIN
    IF TG_OP = 'DELETE' THEN
        IF OLD.locked THEN
            RAISE EXCEPTION 'RD_DESIGN_VERSION_LOCKED: versão % aprovada é imutável', OLD.id USING ERRCODE = 'P0001';
        END IF;
        RETURN OLD;
    END IF;
    IF OLD.locked THEN
        RAISE EXCEPTION 'RD_DESIGN_VERSION_LOCKED: versão % aprovada é imutável', OLD.id USING ERRCODE = 'P0001';
    END IF;
    IF NEW.storage_key IS DISTINCT FROM OLD.storage_key OR NEW.sha256 IS DISTINCT FROM OLD.sha256
       OR NEW.number IS DISTINCT FROM OLD.number OR NEW.request_id IS DISTINCT FROM OLD.request_id
       OR NEW.size_bytes IS DISTINCT FROM OLD.size_bytes OR NEW.content_type IS DISTINCT FROM OLD.content_type
       OR NEW.created_by IS DISTINCT FROM OLD.created_by THEN
        RAISE EXCEPTION 'RD_DESIGN_VERSION_IMMUTABLE: conteúdo da versão % não pode mudar', OLD.id USING ERRCODE = 'P0001';
    END IF;
    RETURN NEW;
END;
$$
""",
    """
CREATE OR REPLACE FUNCTION rd_security_privileged() RETURNS boolean
LANGUAGE sql STABLE
SET search_path = pg_catalog, public, pg_temp AS $$
    SELECT current_user = (SELECT pg_catalog.pg_get_userbyid(c.relowner)
                             FROM pg_catalog.pg_class c WHERE c.oid = 'public.users'::regclass)
        OR COALESCE((SELECT r.rolsuper FROM pg_catalog.pg_roles r WHERE r.rolname = current_user), false)
$$
""",
    """
CREATE OR REPLACE FUNCTION rd_security_current_approval_mode() RETURNS text
LANGUAGE sql STABLE
SET search_path = pg_catalog, public, pg_temp AS $$
    SELECT h.to_mode::text FROM public.instance_approval_mode_history h ORDER BY h.mode_version DESC LIMIT 1
$$
""",
    """
CREATE OR REPLACE FUNCTION rd_is_active_master(p_user uuid) RETURNS boolean
LANGUAGE sql STABLE
SET search_path = pg_catalog, public, pg_temp AS $$
    SELECT EXISTS (SELECT 1 FROM public.users u WHERE u.id = p_user AND u.admin_master_seat IS NOT NULL AND u.is_active AND u.anonymized_at IS NULL AND u.totp_enabled AND u.financial_password_hash IS NOT NULL AND NOT u.must_change_password AND EXISTS (SELECT 1 FROM public.role_assignments ra JOIN public.roles r ON r.id = ra.role_id WHERE ra.user_id = u.id AND r.slug = 'super_admin' AND ra.scope_type = 'GLOBAL' AND (ra.expires_at IS NULL OR ra.expires_at > now())))
$$
""",
    """
CREATE OR REPLACE FUNCTION rd_active_master_count() RETURNS integer
LANGUAGE sql STABLE
SET search_path = pg_catalog, public, pg_temp AS $$
    SELECT count(*)::integer FROM public.users u WHERE u.admin_master_seat IS NOT NULL AND u.is_active AND u.anonymized_at IS NULL AND u.totp_enabled AND u.financial_password_hash IS NOT NULL AND NOT u.must_change_password AND EXISTS (SELECT 1 FROM public.role_assignments ra JOIN public.roles r ON r.id = ra.role_id WHERE ra.user_id = u.id AND r.slug = 'super_admin' AND ra.scope_type = 'GLOBAL' AND (ra.expires_at IS NULL OR ra.expires_at > now()))
$$
""",
    """
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
    END IF;
    RETURN NEW;
END;
$$
""",
    """
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
    IF cardinality(cols) > 0 THEN
        RAISE LOG 'RD_SECURITY_EVENT master_user_changed user=% seat=% columns=% by=% app=% addr=%',
            NEW.id, COALESCE(NEW.admin_master_seat, OLD.admin_master_seat), array_to_string(cols, ','), session_user, current_setting('application_name', true), inet_client_addr();
    END IF;
    RETURN NULL;
END;
$$
""",
    """
CREATE OR REPLACE FUNCTION rd_role_assignments_master_guard() RETURNS trigger
LANGUAGE plpgsql
SET search_path = pg_catalog, public, pg_temp AS $$
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
""",
    """
CREATE OR REPLACE FUNCTION rd_admin_master_invite_guard() RETURNS trigger
LANGUAGE plpgsql
SET search_path = pg_catalog, public, pg_temp AS $$
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
""",
)
RUNTIME_GRANT_0008 = "GRANT EXECUTE ON FUNCTION rd_reset_screen_anchor(uuid, timestamptz, text, uuid) TO {role}"


# Funções LEGADAS criadas fora das migrations por ``backend/scripts/init-db.sql`` (initdb do container de
# desenvolvimento, só no banco principal; não usadas por trigger nem código): recebem apenas o search_path
# seguro via ALTER (corpo intocado), se existirem. Downgrade: RESET.
LEGACY_INIT_DB_FUNCTIONS = ("set_updated_at()", "normalize_search(text)")
RUNTIME_ROLE = "rd_app"
ANCHOR_RESET_SIGNATURE_0007 = "rd_reset_screen_anchor(uuid, timestamptz, text, uuid)"


def bind_role_exists(role: str) -> bool:
    return bool(op.get_bind().execute(sa.text("SELECT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = :r)"),
                                      {"r": role}).scalar())


def _legacy_present() -> list[str]:
    bind = op.get_bind()
    out = []
    for sig in LEGACY_INIT_DB_FUNCTIONS:
        if bind.execute(sa.text(
                "SELECT to_regprocedure(:s) IS NOT NULL AND NOT EXISTS (SELECT 1 FROM pg_depend d WHERE "
                "d.classid = 'pg_proc'::regclass AND d.objid = to_regprocedure(:s) AND d.deptype = 'e')"),
                {"s": f"public.{sig}"}).scalar():
            out.append(sig)
    return out


def upgrade() -> None:
    n_functions = sum(1 for s in FUNCTIONS_0008 if "CREATE OR REPLACE FUNCTION" in s)
    if n_functions != len(FUNCTIONS_0007):
        raise RuntimeError(f"0008: bloco congelado inconsistente ({n_functions} ≠ {len(FUNCTIONS_0007)} funções)")
    for stmt in FUNCTIONS_0008:
        op.execute(stmt)
    # reset de âncora agora é SECURITY DEFINER: EXECUTE só para o papel de runtime (PUBLIC revogado acima)
    if bind_role_exists(RUNTIME_ROLE):
        op.execute(RUNTIME_GRANT_0008.format(role=RUNTIME_ROLE))
    for sig in _legacy_present():
        op.execute(f"ALTER FUNCTION public.{sig} SET search_path = pg_catalog, public, pg_temp")
    # conferência no próprio banco: nenhuma função de public (fora extensões) sem o search_path seguro —
    # função desconhecida falha ALTO (transação desfeita) em vez de ficar sombreável
    missing = op.get_bind().execute(sa.text(
        "SELECT string_agg(p.oid::regprocedure::text, ', ') FROM pg_proc p "
        "WHERE p.pronamespace = 'public'::regnamespace AND p.prokind = 'f' "
        "AND NOT EXISTS (SELECT 1 FROM pg_depend d WHERE d.classid = 'pg_proc'::regclass AND d.objid = p.oid "
        "AND d.deptype = 'e') AND NOT (coalesce(p.proconfig, '{}') @> ARRAY[:sp])"), {"sp": SAFE_SEARCH_PATH_0008}
    ).scalar()
    if missing:
        raise RuntimeError(f"0008: funções sem search_path seguro: {missing}")


def downgrade() -> None:
    # o reset de âncora volta a ser INVOKER com a ACL padrão (EXECUTE para PUBLIC): DROP + CREATE restaura
    # exatamente o estado da 0007 (nenhum trigger/objeto depende da função)
    op.execute(f"DROP FUNCTION IF EXISTS {ANCHOR_RESET_SIGNATURE_0007}")
    for _sig, body in FUNCTIONS_0007:
        op.execute(body)
    for sig in _legacy_present():
        op.execute(f"ALTER FUNCTION public.{sig} RESET search_path")
