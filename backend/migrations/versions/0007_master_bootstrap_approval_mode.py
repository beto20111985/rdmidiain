"""master_bootstrap_approval_mode — AÇÃO 003 / agente E (ADR_011; SPEC §2, §14)

Bootstrap do Admin Master, modo de aprovação financeira ÚNICA × DUPLA por instância e 2º Master por
convite. Gerada com ``autogenerate`` contra banco descartável em ``0006`` e REVISADA à mão.

Upgrade (nesta ordem):
1. Tabelas novas: ``admin_master_invites`` → ``admin_master_seat_events`` → ``instance_approval_mode_history``
   (as duas trilhas são append-only e seladas; modo efetivo = maior ``mode_version``).
2. ``users.master_recovered_at``.
3. ``financial_approval_requests``: ``approval_mode`` (NOT NULL; linhas existentes = ``'DUAL'`` — a AÇÃO 002
   era dupla aprovação por construção — e o default temporário é removido: o código sempre informa),
   ``self_authorized_by`` (FK users RESTRICT), ``self_authorized_at``; CHECK ``action_valid`` recriado com
   ``APPROVAL_MODE_CHANGE``; NOVOS ``mode_valid`` e ``mode_consistent``; REMOVIDO ``approved_consistent``
   (absorvido por ``mode_consistent``). O autogenerate não compara CHECK: todos escritos à mão.
4. DDL: append-only das trilhas (``rd_forbid_mutation``); ``app.modules.security.ddl.statements`` (funções
   de leitura, 4 funções SECURITY DEFINER, guards de ``users``/``role_assignments``/convites, log
   ``RD_SECURITY_EVENT``); ``app.modules.financial.ddl.approval_mode_statements`` (guard de UPDATE do pedido
   atualizado, NOVO guard de INSERT, ``rd_approval_check_authorization``).
5. Privilégios do papel de runtime ``rd_app`` (se existir no cluster — o db-init roda antes do migrate):
   trilhas só SELECT (sem INSERT/UPDATE/DELETE/TRUNCATE nem uso da sequência), convites
   SELECT/INSERT/UPDATE (sem DELETE/TRUNCATE), EXECUTE das funções definer só para ``rd_app``.
   ACL explícita ⇒ o db-init nunca re-concede.
6. DADOS (ADR_011 D9), auditados (``audit_logs``, origem ``system``, sem segredos/e-mail):
   - toda atribuição ``super_admin`` de usuário SEM assento é removida
     (``migration.0007.super_admin_revoked``, uma linha por atribuição, com o estado anterior);
   - usuário que ficou sem nenhuma atribuição é DESATIVADO e tem as sessões revogadas
     (``revoked_reason='migration_0007'``; ``migration.0007.legacy_user_deactivated``);
   - pedidos financeiros existentes marcados ``DUAL`` (``migration.0007.approval_mode_backfilled``, só se houver).
   A migration NÃO inicializa o modo, NÃO cria Master, NÃO lê ENV nem segredo (isso é o serviço
   ``bootstrap`` — ``python -m app.cli bootstrap-instance``).

Downgrade: RECUSA (RuntimeError, nada alterado) se existir pedido ``approval_mode='SINGLE'`` ou
``action_type='APPROVAL_MODE_CHANGE'`` (sem representação no formato 0006). Senão remove triggers/funções/
tabelas/colunas e restaura os CHECKs e o guard de aprovação da 0006 (corpo congelado abaixo).
IRREVERSÍVEL/PERDIDO no downgrade (documentado; auditoria ``migration.0007.downgrade`` com as contagens):
as trilhas seladas de modo e de assentos, os convites, ``master_recovered_at`` (o bloqueio pós-recuperação
deixa de valer) e ``self_authorized_*``. NÃO restaura o ``super_admin`` revogado nem reativa o usuário
legado. Os assentos (``users.admin_master_seat``) e os papéis concedidos pelas funções permanecem; num novo
upgrade + ``bootstrap`` a instância com assento ocupado é adotada como ``LEGACY_DUAL_ADOPTED`` (DUAL —
falha fechada) e os assentos são re-selados (``LEGACY_ADOPTED``).

Revision ID: 0007
Revises: 0006
Create Date: 2026-10-08
"""
from __future__ import annotations

import json
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0007"
down_revision: Union[str, None] = "0006"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

RUNTIME_ROLE = "rd_app"

# ─── Listas/textos CONGELADOS (mudanças futuras entram por migrations novas) ──
TRAILS_0007 = ("instance_approval_mode_history", "admin_master_seat_events")

ACTION_VALID_0006 = (
    "action_type IN ('RD_WITHDRAWAL','REFUND_PIX_OUT','EXPENSE_CREATE','EXPENSE_UPDATE','PAYABLE_SETTLEMENT',"
    "'LEDGER_ADJUSTMENT','COMMISSION_ADJUSTMENT','COMMISSION_EXCEPTION','SETTING_CHANGE',"
    "'RECONCILIATION_ADJUSTMENT','DEPOSIT_REFUND')"
)
ACTION_VALID_0007 = (
    "action_type IN ('RD_WITHDRAWAL','REFUND_PIX_OUT','EXPENSE_CREATE','EXPENSE_UPDATE','PAYABLE_SETTLEMENT',"
    "'LEDGER_ADJUSTMENT','COMMISSION_ADJUSTMENT','COMMISSION_EXCEPTION','SETTING_CHANGE',"
    "'RECONCILIATION_ADJUSTMENT','DEPOSIT_REFUND','APPROVAL_MODE_CHANGE')"
)
APPROVED_CONSISTENT_0006 = "(status = 'APPROVED') = (approved_by IS NOT NULL)"
MODE_VALID_0007 = "approval_mode IN ('SINGLE','DUAL')"
MODE_CONSISTENT_0007 = (
    "(approval_mode = 'DUAL' AND self_authorized_by IS NULL AND self_authorized_at IS NULL"
    " AND ((status = 'APPROVED') = (approved_by IS NOT NULL)))"
    " OR (approval_mode = 'SINGLE' AND approved_by IS NULL AND rejected_by IS NULL"
    " AND ((status = 'APPROVED') = (self_authorized_by IS NOT NULL))"
    " AND (self_authorized_by IS NULL OR self_authorized_by = requested_by)"
    " AND ((self_authorized_by IS NULL) = (self_authorized_at IS NULL)))"
)
MODE_SHAPE_0007 = (
    "(kind = 'INITIALIZED' AND mode_version = 1 AND from_mode IS NULL AND approval_request_id IS NULL"
    " AND requested_by IS NULL AND authorized_by IS NULL AND source IN ('BOOTSTRAP_ENV','LEGACY_DUAL_ADOPTED')"
    " AND (source <> 'LEGACY_DUAL_ADOPTED' OR to_mode = 'DUAL'))"
    " OR (kind = 'SINGLE_TO_DUAL' AND mode_version > 1 AND from_mode = 'SINGLE' AND to_mode = 'DUAL'"
    " AND source = 'APPROVAL_REQUEST' AND approval_request_id IS NOT NULL AND requested_by IS NOT NULL"
    " AND authorized_by = requested_by AND length(btrim(reason)) >= 20)"
    " OR (kind = 'DUAL_TO_SINGLE' AND mode_version > 1 AND from_mode = 'DUAL' AND to_mode = 'SINGLE'"
    " AND source = 'APPROVAL_REQUEST' AND approval_request_id IS NOT NULL AND requested_by IS NOT NULL"
    " AND authorized_by IS NOT NULL AND authorized_by <> requested_by AND length(btrim(reason)) >= 20)"
)

# Corpo do guard de aprovação da 0006 (= ``app.modules.financial.ddl.APPROVAL_GUARD_FN_V1``), restaurado no
# downgrade. CONGELADO — byte a byte igual ao aplicado pela 0004 (catálogo idêntico no round-trip).
APPROVAL_GUARD_FN_0006 = """
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

REVOKE_REASON = ("ADR_011 D9: o perfil super_admin é reservado aos titulares de assento de Admin Master "
                 "(AÇÃO 003, migration 0007)")


# ─── helpers ─────────────────────────────────────────────────────────────────

def _exec(sql: str, **params):
    return op.get_bind().execute(sa.text(sql), params)


def _scalar(sql: str, **params):
    return _exec(sql, **params).scalar()


def _rows(sql: str, **params) -> list[dict]:
    return [dict(r._mapping) for r in _exec(sql, **params)]


def _role_exists(role: str) -> bool:
    return bool(_scalar("SELECT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = :r)", r=role))


def _json(value) -> str | None:
    return None if value is None else json.dumps(value, default=str, ensure_ascii=False, sort_keys=True)


def _audit(action: str, entity_type: str | None, entity_id, *, before=None, after=None,
           reason: str | None = None) -> None:
    """Registro append-only na cadeia de auditoria (trigger da 0003 calcula seq/hash)."""
    _exec(
        "INSERT INTO audit_logs (id, action, entity_type, entity_id, before_data, after_data, reason, origin, "
        "created_at) VALUES (gen_random_uuid(), :a, :et, :eid, CAST(:b AS jsonb), CAST(:af AS jsonb), :r, "
        "'system', now())",
        a=action, et=entity_type, eid=None if entity_id is None else str(entity_id),
        b=_json(before), af=_json(after), r=reason,
    )


# ─── UPGRADE ─────────────────────────────────────────────────────────────────

def _create_tables() -> None:
    op.create_table(
        "admin_master_invites",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("seat", sa.SmallInteger(), nullable=False),
        sa.Column("email", sa.String(length=255), nullable=False),
        sa.Column("full_name", sa.String(length=255), nullable=False),
        sa.Column("token_hash", sa.String(length=64), nullable=False),
        sa.Column("activation_user_id", sa.UUID(), nullable=False),
        sa.Column("invited_by", sa.UUID(), nullable=False),
        sa.Column("status", sa.String(length=10), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("totp_pending_secret_enc", sa.Text(), nullable=True),
        sa.Column("failed_attempts", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("sent_count", sa.Integer(), server_default=sa.text("1"), nullable=False),
        sa.Column("last_sent_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("accepted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("accepted_user_id", sa.UUID(), nullable=True),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("revoked_by", sa.UUID(), nullable=True),
        sa.Column("revoke_reason", sa.String(length=300), nullable=True),
        sa.CheckConstraint("(status = 'ACCEPTED') = (accepted_at IS NOT NULL AND accepted_user_id IS NOT NULL)",
                           name=op.f("ck_admin_master_invites_accepted_consistent")),
        sa.CheckConstraint("expires_at > created_at AND expires_at <= created_at + interval '7 days'",
                           name=op.f("ck_admin_master_invites_expires_window")),
        sa.CheckConstraint("status <> 'REVOKED' OR revoked_at IS NOT NULL",
                           name=op.f("ck_admin_master_invites_revoked_consistent")),
        sa.CheckConstraint("status IN ('PENDING','ACCEPTED','REVOKED','EXPIRED','LOCKED')",
                           name=op.f("ck_admin_master_invites_status_valid")),
        sa.CheckConstraint("accepted_user_id IS NULL OR accepted_user_id = activation_user_id",
                           name=op.f("ck_admin_master_invites_accepted_user_matches")),
        sa.CheckConstraint("failed_attempts >= 0", name=op.f("ck_admin_master_invites_failed_attempts_nonneg")),
        sa.CheckConstraint("seat IN (1, 2)", name=op.f("ck_admin_master_invites_seat_valid")),
        sa.CheckConstraint("sent_count >= 1", name=op.f("ck_admin_master_invites_sent_count_positive")),
        sa.ForeignKeyConstraint(["accepted_user_id"], ["users.id"],
                                name=op.f("fk_admin_master_invites_accepted_user_id_users"), ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["invited_by"], ["users.id"],
                                name=op.f("fk_admin_master_invites_invited_by_users"), ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["revoked_by"], ["users.id"],
                                name=op.f("fk_admin_master_invites_revoked_by_users"), ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_admin_master_invites")),
        sa.UniqueConstraint("activation_user_id", name="uq_admin_master_invites_activation_user_id"),
        sa.UniqueConstraint("token_hash", name="uq_admin_master_invites_token_hash"),
    )
    op.create_index("ix_admin_master_invites_status", "admin_master_invites", ["status"], unique=False)
    op.create_index("uq_admin_master_invites_pending_seat", "admin_master_invites", ["seat"], unique=True,
                    postgresql_where=sa.text("status = 'PENDING'"))

    op.create_table(
        "admin_master_seat_events",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=False), nullable=False),
        sa.Column("seat", sa.SmallInteger(), nullable=False),
        sa.Column("user_id", sa.UUID(), nullable=False),
        sa.Column("event", sa.String(length=10), nullable=False),
        sa.Column("source", sa.String(length=20), nullable=False),
        sa.Column("invite_id", sa.UUID(), nullable=True),
        sa.Column("actor_user_id", sa.UUID(), nullable=True),
        sa.Column("prev_seal", sa.String(length=64), nullable=False),
        sa.Column("app_seal", sa.String(length=64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.CheckConstraint("(event = 'RECOVERED') = (source = 'CLI_RECOVERY')",
                           name=op.f("ck_admin_master_seat_events_recovered_source")),
        sa.CheckConstraint("(source = 'MASTER_INVITE') = (invite_id IS NOT NULL)",
                           name=op.f("ck_admin_master_seat_events_invite_source")),
        sa.CheckConstraint("event IN ('ASSIGNED','RECOVERED')", name=op.f("ck_admin_master_seat_events_event_valid")),
        sa.CheckConstraint("source IN ('BOOTSTRAP_ENV','MASTER_INVITE','CLI_BOOTSTRAP','CLI_RECOVERY','LEGACY_ADOPTED')",
                           name=op.f("ck_admin_master_seat_events_source_valid")),
        sa.CheckConstraint("seat IN (1, 2)", name=op.f("ck_admin_master_seat_events_seat_valid")),
        sa.ForeignKeyConstraint(["actor_user_id"], ["users.id"],
                                name=op.f("fk_admin_master_seat_events_actor_user_id_users"), ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["invite_id"], ["admin_master_invites.id"],
                                name=op.f("fk_admin_master_seat_events_invite_id_admin_master_invites"),
                                ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"],
                                name=op.f("fk_admin_master_seat_events_user_id_users"), ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_admin_master_seat_events")),
        sa.UniqueConstraint("app_seal", name="uq_admin_master_seat_events_app_seal"),
    )
    op.create_index("uq_admin_master_seat_events_invite_id", "admin_master_seat_events", ["invite_id"], unique=True,
                    postgresql_where=sa.text("invite_id IS NOT NULL"))

    op.create_table(
        "instance_approval_mode_history",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=False), nullable=False),
        sa.Column("mode_version", sa.Integer(), nullable=False),
        sa.Column("from_mode", sa.String(length=6), nullable=True),
        sa.Column("to_mode", sa.String(length=6), nullable=False),
        sa.Column("kind", sa.String(length=16), nullable=False),
        sa.Column("source", sa.String(length=24), nullable=False),
        sa.Column("approval_request_id", sa.UUID(), nullable=True),
        sa.Column("requested_by", sa.UUID(), nullable=True),
        sa.Column("authorized_by", sa.UUID(), nullable=True),
        sa.Column("reason", sa.Text(), nullable=True),
        sa.Column("env_value", sa.String(length=16), nullable=True),
        sa.Column("prev_seal", sa.String(length=64), nullable=False),
        sa.Column("app_seal", sa.String(length=64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.CheckConstraint(MODE_SHAPE_0007, name=op.f("ck_instance_approval_mode_history_shape")),
        sa.CheckConstraint("from_mode IS NULL OR from_mode IN ('SINGLE','DUAL')",
                           name=op.f("ck_instance_approval_mode_history_from_mode_valid")),
        sa.CheckConstraint("kind IN ('INITIALIZED','SINGLE_TO_DUAL','DUAL_TO_SINGLE')",
                           name=op.f("ck_instance_approval_mode_history_kind_valid")),
        sa.CheckConstraint("source IN ('BOOTSTRAP_ENV','LEGACY_DUAL_ADOPTED','APPROVAL_REQUEST')",
                           name=op.f("ck_instance_approval_mode_history_source_valid")),
        sa.CheckConstraint("to_mode IN ('SINGLE','DUAL')", name=op.f("ck_instance_approval_mode_history_to_mode_valid")),
        sa.CheckConstraint("mode_version >= 1", name=op.f("ck_instance_approval_mode_history_mode_version_positive")),
        sa.ForeignKeyConstraint(["approval_request_id"], ["financial_approval_requests.id"],
                                name="fk_instance_approval_mode_history_approval_request_id", ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["authorized_by"], ["users.id"],
                                name=op.f("fk_instance_approval_mode_history_authorized_by_users"), ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["requested_by"], ["users.id"],
                                name=op.f("fk_instance_approval_mode_history_requested_by_users"), ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_instance_approval_mode_history")),
        sa.UniqueConstraint("app_seal", name="uq_instance_approval_mode_history_app_seal"),
        sa.UniqueConstraint("approval_request_id", name="uq_instance_approval_mode_history_approval_request_id"),
        sa.UniqueConstraint("mode_version", name="uq_instance_approval_mode_history_mode_version"),
    )


def _alter_existing() -> int:
    op.add_column("users", sa.Column("master_recovered_at", sa.DateTime(timezone=True), nullable=True))

    t = "financial_approval_requests"
    backfilled = int(_scalar(f"SELECT count(*) FROM {t}"))
    # NOT NULL sem default no model: entra com default temporário (preenche as linhas existentes = DUAL,
    # a AÇÃO 002 era dupla aprovação por construção) e o default é removido em seguida.
    op.add_column(t, sa.Column("approval_mode", sa.String(length=6), server_default=sa.text("'DUAL'"), nullable=False))
    op.alter_column(t, "approval_mode", server_default=None)
    op.add_column(t, sa.Column("self_authorized_by", sa.UUID(), nullable=True))
    op.add_column(t, sa.Column("self_authorized_at", sa.DateTime(timezone=True), nullable=True))
    op.create_foreign_key(op.f("fk_financial_approval_requests_self_authorized_by_users"), t, "users",
                          ["self_authorized_by"], ["id"], ondelete="RESTRICT")
    op.drop_constraint(op.f("ck_financial_approval_requests_approved_consistent"), t, type_="check")
    op.drop_constraint(op.f("ck_financial_approval_requests_action_valid"), t, type_="check")
    op.create_check_constraint(op.f("ck_financial_approval_requests_action_valid"), t, ACTION_VALID_0007)
    op.create_check_constraint(op.f("ck_financial_approval_requests_mode_valid"), t, MODE_VALID_0007)
    op.create_check_constraint(op.f("ck_financial_approval_requests_mode_consistent"), t, MODE_CONSISTENT_0007)
    return backfilled


def _extra_ddl() -> None:
    from app.db import ddl
    from app.modules.financial import ddl as fin_ddl
    from app.modules.security import ddl as sec_ddl

    for t in TRAILS_0007:
        for stmt in ddl.append_only_trigger(t):
            op.execute(stmt)
    tables = set(sa.inspect(op.get_bind()).get_table_names())
    sec = sec_ddl.statements(tables)
    if not sec:
        raise RuntimeError("0007: DDL de segurança vazio — tabelas necessárias ausentes")
    for stmt in sec:
        op.execute(stmt)
    for stmt in fin_ddl.approval_mode_statements():
        op.execute(stmt)


def _runtime_privileges() -> None:
    """Sem o papel no cluster (instalação nova antes do db-init) nada é feito: o db-init roda ANTES do migrate."""
    if not _role_exists(RUNTIME_ROLE):
        return
    from app.modules.security import ddl as sec_ddl

    for stmt in sec_ddl.revoke_statements(RUNTIME_ROLE):
        op.execute(stmt)


def _revoke_legacy_super_admin() -> tuple[int, int]:
    """ADR_011 D9: ``super_admin`` só para titulares de assento. Retorna (atribuições removidas, usuários desativados)."""
    rows = _rows(
        "SELECT ra.id, ra.user_id, ra.scope_type, ra.scope_id, ra.scope_value, ra.granted_by, ra.created_at, "
        "ra.expires_at FROM role_assignments ra JOIN roles r ON r.id = ra.role_id JOIN users u ON u.id = ra.user_id "
        "WHERE r.slug = 'super_admin' AND u.admin_master_seat IS NULL ORDER BY ra.created_at, ra.id"
    )
    affected: list = []
    for r in rows:
        _audit("migration.0007.super_admin_revoked", "user", r["user_id"],
               before={"role": "super_admin", "assignment_id": r["id"], "scope_type": r["scope_type"],
                       "scope_id": r["scope_id"], "scope_value": r["scope_value"], "granted_by": r["granted_by"],
                       "created_at": r["created_at"], "expires_at": r["expires_at"]},
               after={"role": None}, reason=REVOKE_REASON)
        _exec("DELETE FROM role_assignments WHERE id = :i", i=r["id"])
        if r["user_id"] not in affected:
            affected.append(r["user_id"])

    deactivated = 0
    for uid in affected:
        if _scalar("SELECT EXISTS (SELECT 1 FROM role_assignments WHERE user_id = :u)", u=uid):
            continue  # ainda tem outro perfil: só perde o super_admin
        was_active = bool(_scalar("SELECT is_active FROM users WHERE id = :u", u=uid))
        sessions = int(_scalar(
            "WITH s AS (UPDATE user_sessions SET revoked_at = now(), revoked_reason = 'migration_0007' "
            "WHERE user_id = :u AND revoked_at IS NULL RETURNING 1) SELECT count(*) FROM s", u=uid))
        _exec("UPDATE users SET is_active = false, updated_at = now() WHERE id = :u", u=uid)
        _audit("migration.0007.legacy_user_deactivated", "user", uid,
               before={"is_active": was_active, "active_sessions": sessions},
               after={"is_active": False, "sessions_revoked": sessions, "revoked_reason": "migration_0007"},
               reason="ADR_011 D9: usuário legado sem nenhum perfil após a revogação do super_admin")
        deactivated += 1
    return len(rows), deactivated


def upgrade() -> None:
    _create_tables()
    backfilled = _alter_existing()
    _extra_ddl()
    _runtime_privileges()
    _revoke_legacy_super_admin()
    if backfilled:
        _audit("migration.0007.approval_mode_backfilled", "financial_approval_requests", None,
               after={"approval_mode": "DUAL", "requests": backfilled},
               reason="AÇÃO 002 era dupla aprovação por construção")


# ─── DOWNGRADE ───────────────────────────────────────────────────────────────

def downgrade() -> None:
    single = int(_scalar("SELECT count(*) FROM financial_approval_requests WHERE approval_mode = 'SINGLE'"))
    mode_change = int(_scalar("SELECT count(*) FROM financial_approval_requests "
                              "WHERE action_type = 'APPROVAL_MODE_CHANGE'"))
    if single or mode_change:
        raise RuntimeError(
            f"downgrade 0007 recusado: {single} pedido(s) de aprovação ÚNICA (SINGLE) e {mode_change} pedido(s) "
            "APPROVAL_MODE_CHANGE — sem representação no formato 0006 (dupla aprovação obrigatória)")
    lost = {t: int(_scalar(f"SELECT count(*) FROM {t}"))
            for t in ("instance_approval_mode_history", "admin_master_seat_events", "admin_master_invites")}
    lost["users_master_recovered_at"] = int(_scalar("SELECT count(*) FROM users WHERE master_recovered_at IS NOT NULL"))

    from app.db import ddl
    from app.modules.security import ddl as sec_ddl

    # financeiro: guard da 0006 (congelado) e remoção do DDL do modo de aprovação
    op.execute(APPROVAL_GUARD_FN_0006)
    for stmt in (
        "DROP TRIGGER IF EXISTS trg_financial_approval_requests_insert_guard ON financial_approval_requests",
        "DROP FUNCTION IF EXISTS rd_approval_request_insert_guard()",
        "DROP FUNCTION IF EXISTS rd_approval_check_authorization(financial_approval_requests)",
    ):
        op.execute(stmt)
    # segurança: triggers/funções, append-only das trilhas, tabelas
    for stmt in sec_ddl.drop_statements():
        op.execute(stmt)
    for t in TRAILS_0007:
        for stmt in ddl.drop_append_only_trigger(t):
            op.execute(stmt)
    op.drop_table("instance_approval_mode_history")
    op.drop_index("uq_admin_master_seat_events_invite_id", table_name="admin_master_seat_events",
                  postgresql_where=sa.text("invite_id IS NOT NULL"))
    op.drop_table("admin_master_seat_events")
    op.drop_index("uq_admin_master_invites_pending_seat", table_name="admin_master_invites",
                  postgresql_where=sa.text("status = 'PENDING'"))
    op.drop_index("ix_admin_master_invites_status", table_name="admin_master_invites")
    op.drop_table("admin_master_invites")

    t = "financial_approval_requests"
    op.drop_constraint(op.f("ck_financial_approval_requests_mode_consistent"), t, type_="check")
    op.drop_constraint(op.f("ck_financial_approval_requests_mode_valid"), t, type_="check")
    op.drop_constraint(op.f("ck_financial_approval_requests_action_valid"), t, type_="check")
    op.create_check_constraint(op.f("ck_financial_approval_requests_action_valid"), t, ACTION_VALID_0006)
    op.create_check_constraint(op.f("ck_financial_approval_requests_approved_consistent"), t, APPROVED_CONSISTENT_0006)
    op.drop_constraint(op.f("fk_financial_approval_requests_self_authorized_by_users"), t, type_="foreignkey")
    op.drop_column(t, "self_authorized_at")
    op.drop_column(t, "self_authorized_by")
    op.drop_column(t, "approval_mode")
    op.drop_column("users", "master_recovered_at")
    _audit("migration.0007.downgrade", None, None, after={"discarded": lost},
           reason="downgrade 0007: trilhas seladas/convites descartados; super_admin revogado NÃO restaurado")
