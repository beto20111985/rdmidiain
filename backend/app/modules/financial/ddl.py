"""
RD Mídia Indoor — DDL extra do domínio financeiro (W-FIN; ADR_009 item 2).

Consumido por ``app.db.ddl`` (registro ``DOMAIN_DDL_MODULES``): a migration deve
executar ``for s in statements(): op.execute(s)`` e o harness de testes aplica após
``create_all``. Todos os statements são idempotentes e não contêm ``:nome``.

Garantias no BANCO (não só no código):
1. ``rd_ledger_check_balanced``: CONSTRAINT TRIGGER ``DEFERRABLE INITIALLY DEFERRED``
   em ``ledger_postings`` e ``ledger_transactions`` — no COMMIT, cada transação
   contábil precisa ter ≥ 2 partidas e Σ amount_cents = 0.
2. ``rd_ledger_posting_guard`` / ``rd_ledger_transaction_guard``: competência da
   partida = competência do cabeçalho; competência dentro de período FECHADO
   (``financial_period_closings``) é rejeitada.
3. Append-only (UPDATE/DELETE/TRUNCATE proibidos) de ``ledger_transactions``,
   ``ledger_postings``, ``financial_approval_events`` e ``financial_period_closings``
   via ``app.db.ddl.APPEND_ONLY_TABLES`` (função ``rd_forbid_mutation``).
4. ``rd_webhook_events_guard``: corpo bruto/headers/ids do webhook imutáveis; DELETE e
   TRUNCATE proibidos (só o estado de processamento muda).
5. ``rd_approval_request_guard``: payload, hash, valor, ação, solicitante imutáveis;
   status só sai de PENDING uma vez; execução não regride (EXECUTING nunca volta a
   PENDING → sem execução dupla de PIX OUT); DELETE proibido. AÇÃO 003 (0007): modo
   (``approval_mode``) e autorização única imutáveis; PENDING → APPROVED conferido por
   ``rd_approval_check_authorization`` (modo atual; DUAL: outro Master ativo; SINGLE: o
   próprio solicitante, Master ativo) e NOVO ``rd_approval_request_insert_guard`` (pedido só
   no modo atual, solicitante Master ativo, DUAL exige 2 Masters ativos).
6. ``rd_withdrawal_guard``: valor/destino/tipo imutáveis; máquina de estados
   monotônica (EXECUTING nunca volta a REQUESTED); DELETE proibido.

REVOKE (produção): o papel de runtime NÃO deve ser dono das tabelas. Ver
``revoke_statements(role)`` — o agente de banco/infra aplica na migration com um
papel de owner separado (``rd_owner``) e o papel de aplicação (``rd_app``).
"""
from __future__ import annotations

from typing import Iterable, Optional

LEDGER_BALANCED_FN = """
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
"""

LEDGER_TX_GUARD_FN = """
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
"""

LEDGER_POSTING_GUARD_FN = """
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
"""

WEBHOOK_GUARD_FN = """
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
"""

# Corpo da 0004 (antes do modo de aprovação, AÇÃO 003). CONGELADO: é o que a 0004 aplica num
# replay a partir do zero (``statements(None)``). NÃO editar — mudanças vão para APPROVAL_GUARD_FN.
APPROVAL_GUARD_FN_V1 = """
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
    IF OLD.status <> 'PENDING' AND NEW.status IS DISTINCT FROM OLD.status THEN
        RAISE EXCEPTION 'RD_APPROVAL_FINAL: status % é final', OLD.status USING ERRCODE = 'P0001';
    END IF;
    IF OLD.approved_by IS NOT NULL AND NEW.approved_by IS DISTINCT FROM OLD.approved_by THEN
        RAISE EXCEPTION 'RD_IMMUTABLE: aprovador não pode mudar' USING ERRCODE = 'P0001';
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
"""

# ─── AÇÃO 003 (ADR_011; SPEC §2.4): modo de aprovação SINGLE × DUAL ──────────
# Dependem de ``app.modules.security.ddl`` (rd_security_current_approval_mode, rd_is_active_master,
# rd_active_master_count) e das colunas da 0007. Referências qualificadas (``public.``): tabela
# temporária do papel de runtime nunca "sombreia" as reais.

APPROVAL_CHECK_AUTHORIZATION_FN = """
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
"""

APPROVAL_GUARD_FN = """
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
"""

APPROVAL_INSERT_GUARD_FN = """
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
"""

WITHDRAWAL_GUARD_FN = """
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
"""


def _trigger(name: str, table: str, timing: str, fn: str, *, constraint: bool = False, statement: bool = False) -> list[str]:
    drop = f"DROP TRIGGER IF EXISTS {name} ON {table}"
    if constraint:
        create = (f"CREATE CONSTRAINT TRIGGER {name} {timing} ON {table} "
                  f"DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION {fn}()")
    else:
        level = "STATEMENT" if statement else "ROW"
        create = f"CREATE TRIGGER {name} {timing} ON {table} FOR EACH {level} EXECUTE FUNCTION {fn}()"
    return [drop, create]


def approval_mode_statements() -> list[str]:
    """DDL da AÇÃO 003 em ``financial_approval_requests`` (migration 0007 em diante):
    ``rd_approval_check_authorization`` + guard de UPDATE atualizado + NOVO guard de INSERT
    (modo atual, solicitante Master ativo, 2 Masters ativos no DUAL, aprovação conferida)."""
    out = [APPROVAL_CHECK_AUTHORIZATION_FN, APPROVAL_GUARD_FN, APPROVAL_INSERT_GUARD_FN]
    out += _trigger("trg_financial_approval_requests_guard", "financial_approval_requests",
                    "BEFORE UPDATE OR DELETE", "rd_approval_request_guard")
    out += _trigger("trg_financial_approval_requests_insert_guard", "financial_approval_requests",
                    "BEFORE INSERT", "rd_approval_request_insert_guard")
    out += _trigger("trg_financial_approval_requests_no_truncate", "financial_approval_requests",
                    "BEFORE TRUNCATE", "rd_forbid_mutation", statement=True)
    return out


def approval_mode_drop_statements() -> list[str]:
    """Desfaz ``approval_mode_statements`` (downgrade 0007 — o chamador restaura ``APPROVAL_GUARD_FN_V1``)."""
    return [
        "DROP TRIGGER IF EXISTS trg_financial_approval_requests_insert_guard ON financial_approval_requests",
        "DROP FUNCTION IF EXISTS rd_approval_request_insert_guard()",
        "DROP FUNCTION IF EXISTS rd_approval_check_authorization(financial_approval_requests)",
    ]


def statements(existing_tables: Optional[Iterable[str]] = None) -> list[str]:
    """DDL idempotente do financeiro (funções + triggers). Requer ``rd_forbid_mutation`` (W1).

    ``existing_tables=None`` é o REPLAY da migration 0004 (``app.db.ddl.domain_statements(strict=True)``
    sem lista de tabelas): emite o estado da época — guard de aprovação V1, sem o DDL do modo de
    aprovação (AÇÃO 003), que depende de colunas/funções criadas só na 0007 (a 0007 aplica
    ``approval_mode_statements()``). Com a lista de tabelas (harness de testes), o estado atual.
    """
    tables = set(existing_tables) if existing_tables is not None else None

    def has(*names: str) -> bool:
        return tables is None or all(n in tables for n in names)

    with_approval_mode = tables is not None and "instance_approval_mode_history" in tables
    out: list[str] = []
    if has("ledger_transactions", "ledger_postings", "financial_period_closings"):
        out += [LEDGER_BALANCED_FN, LEDGER_TX_GUARD_FN, LEDGER_POSTING_GUARD_FN]
        out += _trigger("trg_ledger_postings_balanced", "ledger_postings", "AFTER INSERT",
                        "rd_ledger_check_balanced", constraint=True)
        out += _trigger("trg_ledger_transactions_balanced", "ledger_transactions", "AFTER INSERT",
                        "rd_ledger_check_balanced", constraint=True)
        out += _trigger("trg_ledger_transactions_guard", "ledger_transactions", "BEFORE INSERT",
                        "rd_ledger_transaction_guard")
        out += _trigger("trg_ledger_postings_guard", "ledger_postings", "BEFORE INSERT", "rd_ledger_posting_guard")
    if has("webhook_events"):
        out += [WEBHOOK_GUARD_FN]
        out += _trigger("trg_webhook_events_guard", "webhook_events", "BEFORE UPDATE OR DELETE",
                        "rd_webhook_events_guard")
        out += _trigger("trg_webhook_events_no_truncate", "webhook_events", "BEFORE TRUNCATE",
                        "rd_forbid_mutation", statement=True)
    if has("financial_approval_requests"):
        if with_approval_mode:
            out += approval_mode_statements()
        else:
            out += [APPROVAL_GUARD_FN_V1]
            out += _trigger("trg_financial_approval_requests_guard", "financial_approval_requests",
                            "BEFORE UPDATE OR DELETE", "rd_approval_request_guard")
            out += _trigger("trg_financial_approval_requests_no_truncate", "financial_approval_requests",
                            "BEFORE TRUNCATE", "rd_forbid_mutation", statement=True)
    if has("withdrawals"):
        out += [WITHDRAWAL_GUARD_FN]
        out += _trigger("trg_withdrawals_guard", "withdrawals", "BEFORE UPDATE OR DELETE", "rd_withdrawal_guard")
        out += _trigger("trg_withdrawals_no_truncate", "withdrawals", "BEFORE TRUNCATE", "rd_forbid_mutation",
                        statement=True)
    return out


def hardened_statements(existing_tables: Optional[Iterable[str]] = None) -> list[str]:
    """0008 (AÇÃO 003): corpos VIGENTES endurecidos — ``search_path = pg_catalog, public, pg_temp`` e tabelas
    qualificadas. Os constantes acima são HISTÓRICOS (replay 0004/0007). Ver ``app.db.ddl.harden_function``."""
    from app.db.ddl import harden_function

    tables = set(existing_tables) if existing_tables is not None else None

    def has(*names: str) -> bool:
        return tables is None or all(n in tables for n in names)

    out: list[str] = []
    if has("ledger_transactions", "ledger_postings", "financial_period_closings"):
        out += [
            harden_function(LEDGER_BALANCED_FN, [("FROM ledger_postings p", "FROM public.ledger_postings p")]),
            harden_function(LEDGER_TX_GUARD_FN, [("FROM financial_period_closings c",
                                                  "FROM public.financial_period_closings c")]),
            harden_function(LEDGER_POSTING_GUARD_FN, [
                ("FROM ledger_transactions t", "FROM public.ledger_transactions t"),
                ("FROM financial_period_closings c", "FROM public.financial_period_closings c"),
            ]),
        ]
    if has("webhook_events"):
        out.append(harden_function(WEBHOOK_GUARD_FN))
    if has("withdrawals"):
        out.append(harden_function(WITHDRAWAL_GUARD_FN))
    if has("financial_approval_requests", "instance_approval_mode_history"):
        out += [harden_function(APPROVAL_CHECK_AUTHORIZATION_FN), harden_function(APPROVAL_GUARD_FN),
                harden_function(APPROVAL_INSERT_GUARD_FN)]
    return out


def drop_statements() -> list[str]:
    return [
        "DROP TRIGGER IF EXISTS trg_ledger_postings_balanced ON ledger_postings",
        "DROP TRIGGER IF EXISTS trg_ledger_transactions_balanced ON ledger_transactions",
        "DROP TRIGGER IF EXISTS trg_ledger_transactions_guard ON ledger_transactions",
        "DROP TRIGGER IF EXISTS trg_ledger_postings_guard ON ledger_postings",
        "DROP TRIGGER IF EXISTS trg_webhook_events_guard ON webhook_events",
        "DROP TRIGGER IF EXISTS trg_webhook_events_no_truncate ON webhook_events",
        "DROP TRIGGER IF EXISTS trg_financial_approval_requests_guard ON financial_approval_requests",
        "DROP TRIGGER IF EXISTS trg_financial_approval_requests_insert_guard ON financial_approval_requests",
        "DROP TRIGGER IF EXISTS trg_financial_approval_requests_no_truncate ON financial_approval_requests",
        "DROP TRIGGER IF EXISTS trg_withdrawals_guard ON withdrawals",
        "DROP TRIGGER IF EXISTS trg_withdrawals_no_truncate ON withdrawals",
        "DROP FUNCTION IF EXISTS rd_ledger_check_balanced()",
        "DROP FUNCTION IF EXISTS rd_ledger_transaction_guard()",
        "DROP FUNCTION IF EXISTS rd_ledger_posting_guard()",
        "DROP FUNCTION IF EXISTS rd_webhook_events_guard()",
        "DROP FUNCTION IF EXISTS rd_approval_request_guard()",
        "DROP FUNCTION IF EXISTS rd_approval_request_insert_guard()",
        "DROP FUNCTION IF EXISTS rd_withdrawal_guard()",
    ]


IMMUTABLE_TABLES = ("ledger_transactions", "ledger_postings", "financial_approval_events", "financial_period_closings")


def revoke_statements(runtime_role: str) -> list[str]:
    """REVOKE de mutação para o papel de runtime (aplicar com o papel OWNER separado)."""
    if not runtime_role.replace("_", "").isalnum():
        raise ValueError("papel inválido")
    out = [f"REVOKE UPDATE, DELETE, TRUNCATE ON {t} FROM {runtime_role}" for t in IMMUTABLE_TABLES]
    out += [f"REVOKE DELETE, TRUNCATE ON {t} FROM {runtime_role}"
            for t in ("webhook_events", "financial_approval_requests", "withdrawals")]
    return out
