"""domain_consolidation — AÇÃO 002 / W-DB

Consolida no Alembic TODAS as mudanças de schema feitas pelos domínios da onda 3
(W-ORG, W-MEDIA, W-CAMP, W-INV, W-PLAY, W-FIN, W-BIZ) sobre a 0003 (W1).

Gerada com ``autogenerate`` (``produce_migrations``) contra banco descartável em
``0003`` e REVISADA À MÃO operação por operação:

- tabelas NOVAS (116, inclusive ``payment_orders``/``webhook_events``/``playback_events``
  RECRIADAS — estrutura totalmente nova, PK nova em playback_events) em ordem de
  dependência; nomes > 63 caracteres trocados pelos nomes truncados que o
  ``create_all`` gera (sufixo de hash), para drift zero;
- tabelas legadas VAZIAS removidas (inventory_slots, inventory_reservations,
  campaign_schedules, player_manifests, establishment_categories, ledger_entries,
  commissions, payouts) — guarda aborta se houver linhas;
- tabelas existentes alteradas NO LUGAR com migração de dados:
  organizations.type (bitmask) → business_roles; establishments city_key/region/CEP;
  screens status PAIRED/UNPAIRED/ONLINE → ACTIVE, orientação maiúscula, inches
  float → int, estimated_audience → estimated_daily_audience; campaigns status EN → PT
  (+ datas em America/Sao_Paulo, rejected_reason → rejection_reason, histórico em
  campaign_status_history); creatives.creative_id → code (RENOMEADA, dados preservados);
  devices legados REVOGADOS (segredos em claro descartados) e pairing_requests
  legados apagados (efêmeros); notifications legadas apagadas (W-BIZ); leads
  status → stage, assigned_to → owner_id; proposals/proposal_items/tickets/
  ticket_messages reescritas (número, vendedor, status PT, content → body);
- CHECKs das tabelas existentes (o autogenerate NÃO compara CHECK) criados à mão a
  partir dos models;
- DDL extra (``app.db.ddl``): ``btree_gist`` ANTES das tabelas; ``rd_forbid_column_change``;
  append-only das tabelas novas; colunas imutáveis (screens.friendly_id,
  equipment_items.type/tracking, creatives.code); ``ddl.domain_statements(strict=True)``
  (financeiro, inventário, organizações, designer);
- privilégios do papel de runtime ``rd_app`` (se existir no cluster): REVOKE
  UPDATE/DELETE/TRUNCATE nas tabelas append-only e REVOKE DELETE/TRUNCATE nas
  tabelas financeiras com guarda (webhook_events, financial_approval_requests,
  withdrawals);
- toda conversão de dados existente fica registrada em ``audit_logs``
  (``migration.0004.*``, origem ``system``, antes/depois — sem segredos).

Os seeds de sistema (políticas de conteúdo, taxonomia de referência, política de
rotação GLOBAL 120/120) estão na 0005.

Revision ID: 0004
Revises: 0003
Create Date: 2026-10-07
"""
from __future__ import annotations

import json
import re
import unicodedata
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy import String, Text  # noqa: F401  (render do autogenerate)
from sqlalchemy.dialects import postgresql
from sqlalchemy.dialects.postgresql import UUID  # noqa: F401  (render do autogenerate)

from app.db import ddl

revision: str = "0004"
down_revision: Union[str, None] = "0003"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

# ─── Listas CONGELADAS (mudanças futuras entram por migrations novas) ─────────
LEGACY_DROP_TABLES = (
    "commissions", "inventory_slots", "inventory_reservations", "establishment_categories",
    "campaign_schedules", "ledger_entries", "payouts", "player_manifests",
)
RECREATED_TABLES = ("webhook_events", "payment_orders", "playback_events")
# sem caminho automático de dados (upload/estoque legados nunca funcionaram — A2)
MUST_BE_EMPTY = LEGACY_DROP_TABLES + RECREATED_TABLES + ("media_files", "equipment_items", "equipment_movements")

APPEND_ONLY_0004 = (
    "equipment_movements", "screen_history", "contract_acceptances", "maintenance_events", "audience_estimates",
    "ledger_transactions", "ledger_postings", "financial_approval_events", "financial_period_closings",
    "design_approvals", "design_events", "lead_stage_events", "ticket_events", "rd_price_floor_history",
    "cp_events", "delivery_proofs", "campaign_status_history", "conflict_decisions",
)
APPEND_ONLY_0003 = ("audit_logs", "system_setting_history", "consent_records")
IMMUTABLE_0004 = {
    "screens": ["friendly_id"],
    "equipment_items": ["type", "tracking"],
    "creatives": ["code"],
}
# tabelas financeiras com guarda própria: runtime não apaga nem trunca
NO_DELETE_0004 = ("webhook_events", "financial_approval_requests", "withdrawals")
RUNTIME_ROLE = "rd_app"

NEW_FUNCTIONS = (
    "rd_anchor_write_once()", "rd_approval_request_guard()", "rd_contract_template_guard()",
    "rd_design_version_guard()", "rd_ledger_check_balanced()", "rd_ledger_posting_guard()",
    "rd_ledger_transaction_guard()", "rd_reset_screen_anchor(uuid, timestamptz, text, uuid)",
    "rd_screen_decommission_guard()", "rd_webhook_events_guard()", "rd_withdrawal_guard()",
    "rd_forbid_column_change()",
)

UF_REGION = {
    **{uf: "NORTE" for uf in ("AC", "AM", "AP", "PA", "RO", "RR", "TO")},
    **{uf: "NORDESTE" for uf in ("AL", "BA", "CE", "MA", "PB", "PE", "PI", "RN", "SE")},
    **{uf: "CENTRO_OESTE" for uf in ("DF", "GO", "MS", "MT")},
    **{uf: "SUDESTE" for uf in ("ES", "MG", "RJ", "SP")},
    **{uf: "SUL" for uf in ("PR", "RS", "SC")},
}

CAMPAIGN_STATUS_EN_PT = {
    # W-CAMP: sem pagamento/revisão no modelo novo não há versão aprovada válida
    "DRAFT": "RASCUNHO", "AWAITING_PAYMENT": "RASCUNHO", "AWAITING_MEDIA": "RASCUNHO",
    "IN_REVIEW": "RASCUNHO", "APPROVED": "RASCUNHO", "SCHEDULED": "RASCUNHO", "SYNCING": "RASCUNHO",
    "ACTIVE": "RASCUNHO", "PAUSED": "RASCUNHO",
    "REJECTED": "REJEITADA", "FINISHED": "FINALIZADA", "COMPLETED": "FINALIZADA", "CANCELLED": "CANCELADA",
}
CAMPAIGN_STATUS_PT_EN = {
    "RASCUNHO": "DRAFT", "AGUARDANDO_PAGAMENTO": "AWAITING_PAYMENT", "AGUARDANDO_MIDIA": "AWAITING_MEDIA",
    "EM_ANALISE": "IN_REVIEW", "APROVADA": "APPROVED", "AGENDADA": "SCHEDULED", "SINCRONIZANDO": "SYNCING",
    "ATIVA": "ACTIVE", "PAUSADA": "PAUSED", "FINALIZADA": "FINISHED", "REJEITADA": "REJECTED",
    "CANCELADA": "CANCELLED",
}
CREATIVE_STATUS_EN_PT = {
    "DRAFT": "RASCUNHO", "PENDING_REVIEW": "EM_ANALISE", "IN_REVIEW": "EM_ANALISE", "APPROVED": "APROVADO",
    "REJECTED": "REJEITADO", "ARCHIVED": "ARQUIVADO",
}
LEAD_STATUS_EN_PT = {
    "NEW": "NOVO", "CONTACTED": "CONTATO", "QUALIFIED": "QUALIFICADO", "PROPOSAL": "PROPOSTA",
    "NEGOTIATION": "NEGOCIACAO", "CONVERTED": "GANHO", "WON": "GANHO", "LOST": "PERDIDO",
}
PROPOSAL_STATUS_EN_PT = {
    "DRAFT": "RASCUNHO", "SENT": "ENVIADA", "ACCEPTED": "ACEITA", "REJECTED": "RECUSADA", "EXPIRED": "EXPIRADA",
}
TICKET_STATUS_EN_PT = {
    "OPEN": "ABERTO", "IN_PROGRESS": "EM_ATENDIMENTO", "WAITING": "AGUARDANDO_CLIENTE",
    "WAITING_CUSTOMER": "AGUARDANDO_CLIENTE", "RESOLVED": "RESOLVIDO", "CLOSED": "FECHADO",
    "CANCELLED": "CANCELADO",
}
TICKET_PRIORITY_EN_PT = {
    "LOW": "BAIXA", "MEDIUM": "MEDIA", "NORMAL": "MEDIA", "HIGH": "ALTA", "CRITICAL": "CRITICA", "URGENT": "CRITICA",
}
TICKET_CATEGORIES = ("DUVIDA", "PROBLEMA_TELA", "RETIRADA_ANUNCIO", "CAMPANHA", "FINANCEIRO", "INSTALACAO",
                     "MANUTENCAO", "OUTRO")


# ─── utilitários ─────────────────────────────────────────────────────────────

def _bind():
    return op.get_bind()


def _exec(sql: str, **params):
    return _bind().execute(sa.text(sql), params)


def _scalar(sql: str, **params):
    return _exec(sql, **params).scalar()


def _rows(sql: str, **params):
    return _exec(sql, **params).mappings().all()


def _count(table: str) -> int:
    return int(_scalar(f"SELECT count(*) FROM {table}"))


def _table_exists(table: str) -> bool:
    return bool(_scalar("SELECT to_regclass(:t) IS NOT NULL", t=f"public.{table}"))


def _role_exists(role: str) -> bool:
    return bool(_scalar("SELECT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = :r)", r=role))


def _case(column: str, mapping: dict, default_sql: str) -> str:
    """CASE upper(col) WHEN 'A' THEN 'B' ... ELSE <default_sql> END (valores de mapa fixos, sem entrada externa)."""
    whens = " ".join(f"WHEN '{k}' THEN '{v}'" for k, v in mapping.items())
    return f"CASE upper({column}) {whens} ELSE {default_sql} END"


def _json(value) -> str | None:
    return None if value is None else json.dumps(value, default=str, ensure_ascii=False, sort_keys=True)


def _audit(action: str, entity_type: str, entity_id, *, before=None, after=None, reason: str | None = None,
           org_id=None) -> None:
    """Registro append-only na cadeia de auditoria (trigger da 0003 calcula seq/hash)."""
    _exec(
        "INSERT INTO audit_logs (id, action, entity_type, entity_id, org_id, before_data, after_data, reason, "
        "origin, created_at) VALUES (gen_random_uuid(), :a, :et, :eid, CAST(:org AS uuid), CAST(:b AS jsonb), "
        "CAST(:af AS jsonb), :r, 'system', now())",
        a=action, et=entity_type, eid=None if entity_id is None else str(entity_id),
        org=None if org_id is None else str(org_id), b=_json(before), af=_json(after), r=reason,
    )


def _add_not_null(table: str, column: sa.Column, fill_sql: str) -> None:
    """Coluna NOT NULL sem server_default no model: entra com default temporário (preenche as
    linhas existentes) e o default é removido em seguida (estado final = model)."""
    column.server_default = sa.schema.DefaultClause(sa.text(fill_sql))
    column.nullable = False
    op.add_column(table, column)
    op.alter_column(table, column.name, server_default=None)


SAFE_ALPHABET = "ABCDEFGHJMNPQRSTUVYZ23456789"  # = app.core.ids.SAFE_ALPHABET (sem L I X W O K 0 1)


def _assign_codes(table: str, prefix_sql: str, size: int = 6) -> None:
    """Número público no MESMO formato dos serviços (``<prefixo><6 do alfabeto seguro>``), único."""
    import secrets

    used = {r[0] for r in _exec(f"SELECT number FROM {table} WHERE number IS NOT NULL")}
    for r in _rows(f"SELECT id, {prefix_sql} AS prefix FROM {table} WHERE number IS NULL ORDER BY created_at, id"):
        while True:
            code = r["prefix"] + "".join(secrets.choice(SAFE_ALPHABET) for _ in range(size))
            if code not in used:
                used.add(code)
                break
        _exec(f"UPDATE {table} SET number = :n WHERE id = :id", n=code, id=r["id"])


def _slug(text: str) -> str:
    t = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode("ascii").lower()
    return re.sub(r"[^a-z0-9]+", "-", t).strip("-")


def _city_key(uf: str, city: str) -> str:
    """Igual a ``app.core.authz.city_key`` (congelado aqui)."""
    return f"{(uf or '').strip().upper()}:{_slug(city or '')}"


# ─── guardas ─────────────────────────────────────────────────────────────────

def _guards() -> None:
    problems = []
    for t in MUST_BE_EMPTY:
        if _table_exists(t):
            n = _count(t)
            if n:
                problems.append(f"'{t}' tem {n} linha(s) sem caminho automático de dados")
    n = _scalar("SELECT count(*) FROM establishments WHERE city IS NULL OR btrim(city) = '' OR state IS NULL")
    if n:
        problems.append(f"{n} estabelecimento(s) sem cidade/UF (obrigatórios: city_key/region)")
    bad_uf = [r["state"] for r in _rows("SELECT DISTINCT upper(btrim(state)) AS state FROM establishments "
                                         "WHERE state IS NOT NULL")
              if r["state"] not in UF_REGION]
    if bad_uf:
        problems.append(f"UF inválida em establishments: {sorted(bad_uf)}")
    n = _scalar("SELECT count(*) FROM creatives WHERE creative_id !~ '^RD-[ABCDEFGHJMNPQRSTUVYZ2-9]{6}$'")
    if n:
        problems.append(f"{n} criativo(s) com código fora do formato RD-XXXXXX (ID público imutável: não é reescrito)")
    n = _scalar("SELECT count(*) FROM creatives WHERE duration_seconds NOT IN (30, 60, 120)")
    if n:
        problems.append(f"{n} criativo(s) com duração fora de 30/60/120 s")
    n = _scalar("SELECT count(*) FROM campaign_targets WHERE upper(target_type) NOT IN "
                "('COUNTRY','REGION','STATE','CITY','NEIGHBORHOOD','POSTAL_CODE','CATEGORY','ESTABLISHMENT')")
    if n:
        problems.append(f"{n} alvo(s) de campanha com tipo não suportado (ex.: SCREEN — tela individual proibida)")
    n = _scalar("SELECT count(*) FROM screens WHERE upper(tv_owner) NOT IN ('RD','ESTABLISHMENT')")
    if n:
        problems.append(f"{n} tela(s) com tv_owner inválido")
    if problems:
        raise RuntimeError("0004: dados legados que não podem ser migrados automaticamente — " + "; ".join(problems)
                           + ". Exporte/trate manualmente antes do upgrade.")


# ─── 1. DDL base / remoção do legado ─────────────────────────────────────────

def _base_ddl() -> None:
    for stmt in ddl.extension_statements():  # btree_gist ANTES de rotation_policies (EXCLUDE)
        op.execute(stmt)
    op.execute(ddl.FORBID_COLUMN_CHANGE_FN)


def _drop_legacy_tables() -> None:
    op.drop_constraint("fk_establishments_category_id_establishment_categories", "establishments", type_="foreignkey")
    op.drop_index("ix_commissions_campaign_id", table_name="commissions")
    op.drop_index("ix_commissions_establishment_id", table_name="commissions")
    op.drop_index("ix_commissions_period", table_name="commissions")
    op.drop_index("ix_commissions_screen_id", table_name="commissions")
    op.drop_index("ix_commissions_status", table_name="commissions")
    op.drop_table("commissions")
    op.drop_index("ix_inventory_slots_campaign_id", table_name="inventory_slots")
    op.drop_index("ix_inventory_slots_screen_id_date", table_name="inventory_slots")
    op.drop_index("ix_inventory_slots_status", table_name="inventory_slots")
    op.drop_table("inventory_slots")
    op.drop_index("ix_inventory_reservations_campaign_id", table_name="inventory_reservations")
    op.drop_index("ix_inventory_reservations_expires_at", table_name="inventory_reservations")
    op.drop_table("inventory_reservations")
    op.drop_index("ix_establishment_categories_parent_id", table_name="establishment_categories")
    op.drop_index("ix_establishment_categories_slug", table_name="establishment_categories")
    op.drop_table("establishment_categories")
    op.drop_index("ix_campaign_schedules_campaign_id", table_name="campaign_schedules")
    op.drop_table("campaign_schedules")
    op.drop_index("ix_ledger_entries_created_at", table_name="ledger_entries")
    op.drop_index("ix_ledger_entries_org_id_type", table_name="ledger_entries")
    op.drop_index("ix_ledger_entries_ref", table_name="ledger_entries")
    op.drop_table("ledger_entries")
    op.drop_index("ix_payouts_establishment_id", table_name="payouts")
    op.drop_index("ix_payouts_status", table_name="payouts")
    op.drop_table("payouts")
    op.drop_index("ix_player_manifests_activated_at", table_name="player_manifests")
    op.drop_index("ix_player_manifests_expires_at", table_name="player_manifests")
    op.drop_index("ix_player_manifests_screen_id", table_name="player_manifests")
    op.drop_table("player_manifests")
    # recriadas com estrutura nova (W-FIN/W-PLAY): vazias (guarda)
    op.drop_table("webhook_events")
    op.drop_table("payment_orders")
    op.drop_table("playback_events")


# ─── 2. tabelas novas (autogenerate, revisado) ───────────────────────────────

def _create_new_tables() -> None:
    op.create_table('commission_accrual_events',
    sa.Column('playback_uuid', sa.UUID(), nullable=False),
    sa.Column('campaign_id', sa.UUID(), nullable=False),
    sa.Column('screen_id', sa.UUID(), nullable=False),
    sa.Column('local_day', sa.Date(), nullable=False),
    sa.Column('counted', sa.Boolean(), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.PrimaryKeyConstraint('playback_uuid', name=op.f('pk_commission_accrual_events'))
    )
    op.create_index('ix_commission_accrual_events_key', 'commission_accrual_events', ['campaign_id', 'screen_id', 'local_day'], unique=False)
    op.create_table('conflict_decisions',
    sa.Column('id', sa.UUID(), nullable=False),
    sa.Column('campaign_id', sa.UUID(), nullable=True),
    sa.Column('creative_id', sa.UUID(), nullable=True),
    sa.Column('establishment_id', sa.UUID(), nullable=True),
    sa.Column('other_creative_id', sa.UUID(), nullable=True),
    sa.Column('result', sa.String(length=5), nullable=False),
    sa.Column('reason_code', sa.String(length=40), nullable=False),
    sa.Column('rule_path', postgresql.JSONB(astext_type=Text()), nullable=False),
    sa.Column('engine_version', sa.String(length=20), nullable=False),
    sa.Column('facts_hash', sa.String(length=64), nullable=False),
    sa.Column('evaluated_by', sa.UUID(), nullable=True),
    sa.Column('evaluated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.CheckConstraint("result IN ('ALLOW', 'BLOCK')", name=op.f('ck_conflict_decisions_result_valid')),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_conflict_decisions'))
    )
    op.create_index('ix_conflict_decisions_campaign_est', 'conflict_decisions', ['campaign_id', 'establishment_id', 'evaluated_at'], unique=False)
    op.create_index('ix_conflict_decisions_creative', 'conflict_decisions', ['creative_id'], unique=False)
    op.create_table('cp_events',
    sa.Column('id', sa.UUID(), nullable=False),
    sa.Column('type', sa.String(length=60), nullable=False),
    sa.Column('instance_id', sa.UUID(), nullable=True),
    sa.Column('release_id', sa.UUID(), nullable=True),
    sa.Column('rollout_id', sa.UUID(), nullable=True),
    sa.Column('data', postgresql.JSONB(astext_type=Text()), nullable=True),
    sa.Column('actor_id', sa.UUID(), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_cp_events'))
    )
    op.create_index('ix_cp_events_instance_id', 'cp_events', ['instance_id'], unique=False)
    op.create_index('ix_cp_events_rollout_id', 'cp_events', ['rollout_id'], unique=False)
    op.create_table('cp_instance_groups',
    sa.Column('id', sa.UUID(), nullable=False),
    sa.Column('name', sa.String(length=100), nullable=False),
    sa.Column('description', sa.String(length=500), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_cp_instance_groups')),
    sa.UniqueConstraint('name', name='uq_cp_instance_groups_name')
    )
    op.create_table('instance_signing_keys',
    sa.Column('kid', sa.String(length=40), nullable=False),
    sa.Column('algorithm', sa.String(length=10), nullable=False),
    sa.Column('public_pem', sa.Text(), nullable=False),
    sa.Column('private_enc', sa.Text(), nullable=False),
    sa.Column('is_active', sa.Boolean(), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('retired_at', sa.DateTime(timezone=True), nullable=True),
    sa.CheckConstraint("algorithm = 'ES256'", name=op.f('ck_instance_signing_keys_algorithm_es256')),
    sa.PrimaryKeyConstraint('kid', name=op.f('pk_instance_signing_keys'))
    )
    op.create_index('uq_instance_signing_keys_active', 'instance_signing_keys', ['is_active'], unique=True, postgresql_where=sa.text('is_active'))
    op.create_table('ledger_accounts',
    sa.Column('id', sa.UUID(), nullable=False),
    sa.Column('account_key', sa.String(length=200), nullable=False),
    sa.Column('code', sa.String(length=120), nullable=False),
    sa.Column('name', sa.String(length=200), nullable=False),
    sa.Column('kind', sa.String(length=12), nullable=False),
    sa.Column('owner_type', sa.String(length=20), nullable=False),
    sa.Column('owner_id', sa.UUID(), nullable=True),
    sa.Column('org_id', sa.UUID(), nullable=True),
    sa.Column('is_cash', sa.Boolean(), server_default=sa.text('false'), nullable=False),
    sa.Column('dre_group', sa.String(length=40), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.CheckConstraint("(owner_type = 'RD') = (owner_id IS NULL)", name=op.f('ck_ledger_accounts_owner_id_matches_type')),
    sa.CheckConstraint("kind IN ('ASSET','LIABILITY','EQUITY','INCOME','EXPENSE')", name=op.f('ck_ledger_accounts_kind_valid')),
    sa.CheckConstraint("owner_type IN ('RD','ESTABLISHMENT','ADVERTISER','SELLER')", name=op.f('ck_ledger_accounts_owner_type_valid')),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_ledger_accounts')),
    sa.UniqueConstraint('account_key', name='uq_ledger_accounts_account_key')
    )
    op.create_index('ix_ledger_accounts_code', 'ledger_accounts', ['code'], unique=False)
    op.create_index(op.f('ix_ledger_accounts_org_id'), 'ledger_accounts', ['org_id'], unique=False)
    op.create_index('ix_ledger_accounts_owner', 'ledger_accounts', ['owner_type', 'owner_id'], unique=False)
    op.create_table('reconciliation_runs',
    sa.Column('id', sa.UUID(), nullable=False),
    sa.Column('kind', sa.String(length=12), nullable=False),
    sa.Column('status', sa.String(length=22), nullable=False),
    sa.Column('provider_balance_cents', sa.BigInteger(), nullable=True),
    sa.Column('internal_cash_cents', sa.BigInteger(), nullable=True),
    sa.Column('summary', postgresql.JSONB(astext_type=Text()), nullable=False),
    sa.Column('created_by', sa.UUID(), nullable=True),
    sa.Column('started_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('finished_at', sa.DateTime(timezone=True), nullable=True),
    sa.CheckConstraint("status IN ('RUNNING','COMPLETED','FAILED','PROVIDER_UNAVAILABLE')", name=op.f('ck_reconciliation_runs_status_valid')),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_reconciliation_runs'))
    )
    op.create_table('segments',
    sa.Column('id', sa.UUID(), nullable=False),
    sa.Column('parent_id', sa.UUID(), nullable=True),
    sa.Column('name', sa.String(length=120), nullable=False),
    sa.Column('slug', sa.String(length=140), nullable=False),
    sa.Column('path', sa.Text(), nullable=False),
    sa.Column('depth', sa.SmallInteger(), nullable=False),
    sa.Column('is_active', sa.Boolean(), server_default=sa.text('true'), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.CheckConstraint('(parent_id IS NULL) = (depth = 0)', name=op.f('ck_segments_root_depth_consistent')),
    sa.CheckConstraint('depth >= 0 AND depth <= 8', name=op.f('ck_segments_depth_range')),
    sa.ForeignKeyConstraint(['parent_id'], ['segments.id'], name=op.f('fk_segments_parent_id_segments'), ondelete='RESTRICT'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_segments')),
    sa.UniqueConstraint('slug', name=op.f('uq_segments_slug'))
    )
    op.create_index('ix_segments_parent_id', 'segments', ['parent_id'], unique=False)
    op.create_index('ix_segments_path', 'segments', ['path'], unique=False, postgresql_ops={'path': 'text_pattern_ops'})
    op.create_table('taxonomy_nodes',
    sa.Column('id', sa.UUID(), nullable=False),
    sa.Column('kind', sa.String(length=12), nullable=False),
    sa.Column('parent_id', sa.UUID(), nullable=True),
    sa.Column('name', sa.String(length=120), nullable=False),
    sa.Column('slug', sa.String(length=140), nullable=False),
    sa.Column('path', sa.Text(), nullable=False),
    sa.Column('depth', sa.SmallInteger(), nullable=False),
    sa.Column('is_active', sa.Boolean(), server_default=sa.text('true'), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.CheckConstraint("(kind = 'SECTOR' AND parent_id IS NULL AND depth = 0) OR (kind = 'CATEGORY' AND parent_id IS NOT NULL AND depth = 1) OR (kind = 'SUBCATEGORY' AND parent_id IS NOT NULL AND depth = 2)", name=op.f('ck_taxonomy_nodes_kind_depth_consistent')),
    sa.CheckConstraint("kind IN ('SECTOR','CATEGORY','SUBCATEGORY')", name=op.f('ck_taxonomy_nodes_kind_valid')),
    sa.ForeignKeyConstraint(['parent_id'], ['taxonomy_nodes.id'], name=op.f('fk_taxonomy_nodes_parent_id_taxonomy_nodes'), ondelete='RESTRICT'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_taxonomy_nodes')),
    sa.UniqueConstraint('slug', name=op.f('uq_taxonomy_nodes_slug'))
    )
    op.create_index('ix_taxonomy_nodes_parent_id', 'taxonomy_nodes', ['parent_id'], unique=False)
    op.create_index('ix_taxonomy_nodes_path', 'taxonomy_nodes', ['path'], unique=False, postgresql_ops={'path': 'text_pattern_ops'})
    op.create_table('app_releases',
    sa.Column('id', sa.UUID(), nullable=False),
    sa.Column('platform', sa.String(length=20), nullable=False),
    sa.Column('version', sa.String(length=40), nullable=False),
    sa.Column('url', sa.Text(), nullable=False),
    sa.Column('sha256', sa.String(length=64), nullable=False),
    sa.Column('size', sa.BigInteger(), nullable=False),
    sa.Column('signature', sa.Text(), nullable=False),
    sa.Column('key_id', sa.String(length=64), nullable=False),
    sa.Column('mandatory', sa.Boolean(), nullable=False),
    sa.Column('status', sa.String(length=20), nullable=False),
    sa.Column('rollout_percent', sa.Integer(), nullable=False),
    sa.Column('target_groups', postgresql.JSONB(astext_type=Text()), nullable=True),
    sa.Column('max_concurrent', sa.Integer(), nullable=False),
    sa.Column('notes', sa.Text(), nullable=True),
    sa.Column('created_by_user_id', sa.UUID(), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('published_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.ForeignKeyConstraint(['created_by_user_id'], ['users.id'], name=op.f('fk_app_releases_created_by_user_id_users'), ondelete='SET NULL'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_app_releases'))
    )
    op.create_index('ix_app_releases_platform_status', 'app_releases', ['platform', 'status'], unique=False)
    op.create_index('uq_app_releases_platform_version', 'app_releases', ['platform', 'version'], unique=True)
    op.create_table('conflict_rules',
    sa.Column('id', sa.UUID(), nullable=False),
    sa.Column('subject_type', sa.String(length=20), nullable=False),
    sa.Column('subject_id', sa.UUID(), nullable=False),
    sa.Column('object_type', sa.String(length=20), nullable=False),
    sa.Column('object_id', sa.UUID(), nullable=False),
    sa.Column('include_descendants', sa.Boolean(), nullable=False),
    sa.Column('effect', sa.String(length=5), nullable=False),
    sa.Column('reason', sa.Text(), nullable=False),
    sa.Column('active', sa.Boolean(), nullable=False),
    sa.Column('valid_from', sa.DateTime(timezone=True), nullable=True),
    sa.Column('valid_until', sa.DateTime(timezone=True), nullable=True),
    sa.Column('version', sa.Integer(), nullable=False),
    sa.Column('created_by', sa.UUID(), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('updated_by', sa.UUID(), nullable=True),
    sa.Column('updated_at', sa.DateTime(timezone=True), nullable=True),
    sa.CheckConstraint("effect IN ('BLOCK', 'ALLOW')", name=op.f('ck_conflict_rules_effect_valid')),
    sa.CheckConstraint("object_type IN ('AD_CATEGORY', 'AD_SEGMENT', 'AD_PRODUCT', 'AD_BRAND', 'AD_ADVERTISER')", name=op.f('ck_conflict_rules_object_type_valid')),
    sa.CheckConstraint("subject_type IN ('EST_CATEGORY', 'EST_SEGMENT', 'EST_ORG', 'ESTABLISHMENT')", name=op.f('ck_conflict_rules_subject_type_valid')),
    sa.CheckConstraint('length(reason) >= 5', name=op.f('ck_conflict_rules_reason_len')),
    sa.CheckConstraint('valid_until IS NULL OR valid_from IS NULL OR valid_until > valid_from', name=op.f('ck_conflict_rules_validity')),
    sa.ForeignKeyConstraint(['created_by'], ['users.id'], name=op.f('fk_conflict_rules_created_by_users'), ondelete='RESTRICT'),
    sa.ForeignKeyConstraint(['updated_by'], ['users.id'], name=op.f('fk_conflict_rules_updated_by_users'), ondelete='RESTRICT'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_conflict_rules'))
    )
    op.create_index('ix_conflict_rules_object', 'conflict_rules', ['object_type', 'object_id'], unique=False)
    op.create_index('ix_conflict_rules_subject', 'conflict_rules', ['subject_type', 'subject_id'], unique=False)
    op.create_table('content_policies',
    sa.Column('segment_id', sa.UUID(), nullable=False),
    sa.Column('effect', sa.String(length=5), nullable=False),
    sa.Column('reason', sa.Text(), nullable=False),
    sa.Column('is_default', sa.Boolean(), server_default=sa.text('false'), nullable=False),
    sa.Column('updated_by', sa.UUID(), nullable=True),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.CheckConstraint("effect IN ('BLOCK','ALLOW')", name=op.f('ck_content_policies_effect_valid')),
    sa.ForeignKeyConstraint(['segment_id'], ['segments.id'], name=op.f('fk_content_policies_segment_id_segments'), ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['updated_by'], ['users.id'], name=op.f('fk_content_policies_updated_by_users'), ondelete='SET NULL'),
    sa.PrimaryKeyConstraint('segment_id', name=op.f('pk_content_policies'))
    )
    op.create_table('contract_templates',
    sa.Column('id', sa.UUID(), nullable=False),
    sa.Column('kind', sa.String(length=20), nullable=False),
    sa.Column('version', sa.Integer(), nullable=False),
    sa.Column('title', sa.String(length=200), nullable=False),
    sa.Column('body', sa.Text(), nullable=False),
    sa.Column('body_sha256', sa.String(length=64), nullable=False),
    sa.Column('status', sa.String(length=10), nullable=False),
    sa.Column('published_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('retired_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('created_by', sa.UUID(), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.CheckConstraint("kind IN ('PARTNERSHIP','ADVERTISER_TERMS','COMODATO')", name=op.f('ck_contract_templates_kind_valid')),
    sa.CheckConstraint("status IN ('DRAFT','PUBLISHED','RETIRED')", name=op.f('ck_contract_templates_status_valid')),
    sa.CheckConstraint('char_length(body_sha256) = 64', name=op.f('ck_contract_templates_body_sha256_len')),
    sa.CheckConstraint('version >= 1', name=op.f('ck_contract_templates_version_positive')),
    sa.ForeignKeyConstraint(['created_by'], ['users.id'], name=op.f('fk_contract_templates_created_by_users'), ondelete='SET NULL'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_contract_templates')),
    sa.UniqueConstraint('kind', 'version', name='uq_contract_templates_kind_version')
    )
    op.create_index('uq_contract_templates_one_published', 'contract_templates', ['kind'], unique=True, postgresql_where=sa.text("status = 'PUBLISHED'"))
    op.create_table('coupons',
    sa.Column('id', sa.UUID(), nullable=False),
    sa.Column('code', sa.String(length=40), nullable=False),
    sa.Column('description', sa.String(length=255), nullable=True),
    sa.Column('discount_type', sa.String(length=10), nullable=False),
    sa.Column('discount_bps', sa.Integer(), nullable=False),
    sa.Column('discount_cents', sa.BigInteger(), nullable=False),
    sa.Column('valid_from', sa.DateTime(timezone=True), nullable=False),
    sa.Column('valid_until', sa.DateTime(timezone=True), nullable=False),
    sa.Column('max_uses', sa.Integer(), nullable=True),
    sa.Column('uses_count', sa.Integer(), server_default='0', nullable=False),
    sa.Column('max_uses_per_org', sa.Integer(), nullable=True),
    sa.Column('min_order_cents', sa.BigInteger(), nullable=False),
    sa.Column('plan_ids', postgresql.JSONB(astext_type=Text()), nullable=True),
    sa.Column('active', sa.Boolean(), nullable=False),
    sa.Column('created_by', sa.UUID(), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.CheckConstraint("(discount_type = 'PERCENT' AND discount_bps BETWEEN 1 AND 10000 AND discount_cents = 0) OR (discount_type = 'FIXED' AND discount_cents > 0 AND discount_bps = 0)", name=op.f('ck_coupons_discount_consistent')),
    sa.CheckConstraint("discount_type IN ('PERCENT', 'FIXED')", name=op.f('ck_coupons_discount_type_valid')),
    sa.CheckConstraint('code = upper(code)', name=op.f('ck_coupons_code_upper')),
    sa.CheckConstraint('max_uses IS NULL OR uses_count <= max_uses', name=op.f('ck_coupons_uses_within_limit')),
    sa.CheckConstraint('uses_count >= 0', name=op.f('ck_coupons_uses_nonneg')),
    sa.CheckConstraint('valid_until > valid_from', name=op.f('ck_coupons_validity')),
    sa.ForeignKeyConstraint(['created_by'], ['users.id'], name=op.f('fk_coupons_created_by_users'), ondelete='RESTRICT'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_coupons')),
    sa.UniqueConstraint('code', name='uq_coupons_code')
    )
    op.create_table('cp_instances',
    sa.Column('id', sa.UUID(), nullable=False),
    sa.Column('slug', sa.String(length=60), nullable=False),
    sa.Column('name', sa.String(length=120), nullable=False),
    sa.Column('kind', sa.String(length=20), nullable=False),
    sa.Column('base_url', sa.String(length=255), nullable=True),
    sa.Column('group_id', sa.UUID(), nullable=True),
    sa.Column('status', sa.String(length=15), nullable=False),
    sa.Column('current_version', sa.String(length=40), nullable=True),
    sa.Column('last_deployed_version', sa.String(length=40), nullable=True),
    sa.Column('health_status', sa.String(length=15), nullable=False),
    sa.Column('health', postgresql.JSONB(astext_type=Text()), nullable=True),
    sa.Column('last_heartbeat_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('token_hash', sa.String(length=64), nullable=True),
    sa.Column('license', postgresql.JSONB(astext_type=Text()), nullable=True),
    sa.Column('notes', sa.Text(), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.CheckConstraint("kind IN ('RD_PRODUCAO','DEV','WHITE_LABEL','FRANQUIA','LICENCA')", name=op.f('ck_cp_instances_kind_valid')),
    sa.CheckConstraint("status IN ('ATIVA','INATIVA','MANUTENCAO')", name=op.f('ck_cp_instances_status_valid')),
    sa.ForeignKeyConstraint(['group_id'], ['cp_instance_groups.id'], name=op.f('fk_cp_instances_group_id_cp_instance_groups'), ondelete='SET NULL'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_cp_instances')),
    sa.UniqueConstraint('slug', name='uq_cp_instances_slug')
    )
    op.create_index('ix_cp_instances_group_id', 'cp_instances', ['group_id'], unique=False)
    op.create_table('financial_approval_requests',
    sa.Column('id', sa.UUID(), nullable=False),
    sa.Column('action_type', sa.String(length=40), nullable=False),
    sa.Column('payload', postgresql.JSONB(astext_type=Text()), nullable=False),
    sa.Column('payload_sha256', sa.String(length=64), nullable=False),
    sa.Column('amount_cents', sa.BigInteger(), nullable=True),
    sa.Column('reason', sa.Text(), nullable=False),
    sa.Column('requested_by', sa.UUID(), nullable=False),
    sa.Column('requested_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('status', sa.String(length=10), nullable=False),
    sa.Column('approved_by', sa.UUID(), nullable=True),
    sa.Column('approved_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('rejected_by', sa.UUID(), nullable=True),
    sa.Column('rejected_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('rejection_reason', sa.Text(), nullable=True),
    sa.Column('cancelled_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('expires_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('execution_state', sa.String(length=18), nullable=False),
    sa.Column('execution_ref', sa.String(length=120), nullable=True),
    sa.Column('execution_error', sa.String(length=500), nullable=True),
    sa.Column('executed_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('idempotency_key', sa.String(length=120), nullable=False),
    sa.Column('email_code_hash', sa.String(length=64), nullable=True),
    sa.Column('email_code_expires_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('email_code_attempts', sa.Integer(), server_default=sa.text('0'), nullable=False),
    sa.Column('email_code_user_id', sa.UUID(), nullable=True),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.CheckConstraint("(status = 'APPROVED') = (approved_by IS NOT NULL)", name=op.f('ck_financial_approval_requests_approved_consistent')),
    sa.CheckConstraint("action_type IN ('RD_WITHDRAWAL','REFUND_PIX_OUT','EXPENSE_CREATE','EXPENSE_UPDATE','PAYABLE_SETTLEMENT','LEDGER_ADJUSTMENT','COMMISSION_ADJUSTMENT','COMMISSION_EXCEPTION','SETTING_CHANGE','RECONCILIATION_ADJUSTMENT','DEPOSIT_REFUND')", name=op.f('ck_financial_approval_requests_action_valid')),
    sa.CheckConstraint("execution_state IN ('NOT_STARTED','PENDING','EXECUTING','EXECUTED','FAILED','UNKNOWN','BLOCKED_EXTERNAL')", name=op.f('ck_financial_approval_requests_execution_valid')),
    sa.CheckConstraint("status IN ('PENDING','APPROVED','REJECTED','CANCELLED','EXPIRED')", name=op.f('ck_financial_approval_requests_status_valid')),
    sa.CheckConstraint('amount_cents IS NULL OR amount_cents >= 0', name=op.f('ck_financial_approval_requests_amount_nonneg')),
    sa.CheckConstraint('approved_by IS NULL OR approved_by <> requested_by', name=op.f('ck_financial_approval_requests_approver_not_requester')),
    sa.CheckConstraint('rejected_by IS NULL OR rejected_by <> requested_by', name=op.f('ck_financial_approval_requests_rejecter_not_requester')),
    sa.ForeignKeyConstraint(['approved_by'], ['users.id'], name=op.f('fk_financial_approval_requests_approved_by_users'), ondelete='RESTRICT'),
    sa.ForeignKeyConstraint(['rejected_by'], ['users.id'], name=op.f('fk_financial_approval_requests_rejected_by_users'), ondelete='RESTRICT'),
    sa.ForeignKeyConstraint(['requested_by'], ['users.id'], name=op.f('fk_financial_approval_requests_requested_by_users'), ondelete='RESTRICT'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_financial_approval_requests')),
    sa.UniqueConstraint('idempotency_key', name='uq_financial_approval_requests_idempotency_key')
    )
    op.create_index('ix_financial_approval_requests_status', 'financial_approval_requests', ['status'], unique=False)
    op.create_table('financial_password_reset_codes',
    sa.Column('id', sa.UUID(), nullable=False),
    sa.Column('user_id', sa.UUID(), nullable=False),
    sa.Column('code_hash', sa.String(length=64), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('expires_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('used_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('invalidated_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('attempts', sa.Integer(), server_default=sa.text('0'), nullable=False),
    sa.Column('request_ip', sa.String(length=64), nullable=True),
    sa.CheckConstraint('attempts >= 0', name=op.f('ck_financial_password_reset_codes_attempts_nonneg')),
    sa.ForeignKeyConstraint(['user_id'], ['users.id'], name=op.f('fk_financial_password_reset_codes_user_id_users'), ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_financial_password_reset_codes'))
    )
    op.create_index('ix_financial_password_reset_codes_user', 'financial_password_reset_codes', ['user_id', 'created_at'], unique=False)
    op.create_table('financial_period_closings',
    sa.Column('id', sa.UUID(), nullable=False),
    sa.Column('period_start', sa.Date(), nullable=False),
    sa.Column('period_end', sa.Date(), nullable=False),
    sa.Column('closed_by', sa.UUID(), nullable=True),
    sa.Column('snapshot', postgresql.JSONB(astext_type=Text()), nullable=False),
    sa.Column('snapshot_sha256', sa.String(length=64), nullable=False),
    sa.Column('transaction_count', sa.BigInteger(), nullable=False),
    sa.Column('closed_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.CheckConstraint('extract(day FROM period_start) = 1', name=op.f('ck_financial_period_closings_period_starts_day_1')),
    sa.CheckConstraint('period_end >= period_start', name=op.f('ck_financial_period_closings_period_valid')),
    sa.ForeignKeyConstraint(['closed_by'], ['users.id'], name=op.f('fk_financial_period_closings_closed_by_users'), ondelete='RESTRICT'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_financial_period_closings')),
    sa.UniqueConstraint('period_start', name='uq_financial_period_closings_period')
    )
    op.create_table('instance_profile',
    sa.Column('id', sa.SmallInteger(), autoincrement=False, nullable=False),
    sa.Column('instance_slug', sa.String(length=60), nullable=False),
    sa.Column('display_name', sa.String(length=120), nullable=False),
    sa.Column('legal_name', sa.String(length=255), nullable=True),
    sa.Column('document', sa.String(length=20), nullable=True),
    sa.Column('is_white_label', sa.Boolean(), nullable=False),
    sa.Column('support_email', sa.String(length=255), nullable=True),
    sa.Column('support_phone', sa.String(length=30), nullable=True),
    sa.Column('primary_color', sa.String(length=7), nullable=True),
    sa.Column('accent_color', sa.String(length=7), nullable=True),
    sa.Column('logo_key', sa.String(length=512), nullable=True),
    sa.Column('logo_content_type', sa.String(length=50), nullable=True),
    sa.Column('logo_sha256', sa.String(length=64), nullable=True),
    sa.Column('subdomain', sa.String(length=63), nullable=True),
    sa.Column('payment_provider', sa.String(length=20), server_default=sa.text("'CLOUDPAYPAG'"), nullable=False),
    sa.Column('payment_credentials_ref', sa.String(length=255), nullable=True),
    sa.Column('payment_account_label', sa.String(length=120), nullable=True),
    sa.Column('version', sa.Integer(), nullable=False),
    sa.Column('updated_by', sa.UUID(), nullable=True),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.CheckConstraint("payment_provider = 'CLOUDPAYPAG'", name=op.f('ck_instance_profile_payment_provider_cloudpaypag')),
    sa.CheckConstraint('id = 1', name=op.f('ck_instance_profile_singleton')),
    sa.ForeignKeyConstraint(['updated_by'], ['users.id'], name=op.f('fk_instance_profile_updated_by_users'), ondelete='SET NULL'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_instance_profile'))
    )
    op.create_table('notification_cooldowns',
    sa.Column('user_id', sa.UUID(), nullable=False),
    sa.Column('event_type', sa.String(length=100), nullable=False),
    sa.Column('entity_key', sa.String(length=200), nullable=False),
    sa.Column('last_sent_at', sa.DateTime(timezone=True), nullable=False),
    sa.ForeignKeyConstraint(['user_id'], ['users.id'], name=op.f('fk_notification_cooldowns_user_id_users'), ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('user_id', 'event_type', 'entity_key', name='pk_notification_cooldowns')
    )
    op.create_table('notification_templates',
    sa.Column('id', sa.UUID(), nullable=False),
    sa.Column('event_type', sa.String(length=100), nullable=False),
    sa.Column('channel', sa.String(length=20), nullable=False),
    sa.Column('title_template', sa.String(length=255), nullable=False),
    sa.Column('body_template', sa.Text(), nullable=False),
    sa.Column('is_active', sa.Boolean(), nullable=False),
    sa.Column('version', sa.Integer(), nullable=False),
    sa.Column('updated_by', sa.UUID(), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.CheckConstraint("channel IN ('IN_APP','EMAIL','PUSH')", name=op.f('ck_notification_templates_channel_valid')),
    sa.ForeignKeyConstraint(['updated_by'], ['users.id'], name=op.f('fk_notification_templates_updated_by_users'), ondelete='SET NULL'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_notification_templates')),
    sa.UniqueConstraint('event_type', 'channel', name='uq_notification_templates_event_channel')
    )
    op.create_table('partner_plans',
    sa.Column('id', sa.UUID(), nullable=False),
    sa.Column('name', sa.String(length=120), nullable=False),
    sa.Column('slug', sa.String(length=140), nullable=False),
    sa.Column('description', sa.Text(), nullable=True),
    sa.Column('monthly_fee_cents', sa.Integer(), server_default=sa.text('0'), nullable=False),
    sa.Column('tv_provision', sa.String(length=16), nullable=False),
    sa.Column('default_tv_inches', sa.Integer(), nullable=True),
    sa.Column('deposit_required', sa.Boolean(), nullable=False),
    sa.Column('deposit_cents', sa.Integer(), server_default=sa.text('0'), nullable=False),
    sa.Column('installation_fee_required', sa.Boolean(), nullable=False),
    sa.Column('installation_fee_cents', sa.Integer(), server_default=sa.text('0'), nullable=False),
    sa.Column('upgrade_policy', sa.String(length=12), nullable=False),
    sa.Column('upgrade_fee_cents', sa.Integer(), nullable=True),
    sa.Column('receives_commission', sa.Boolean(), server_default=sa.text('true'), nullable=False),
    sa.Column('default_internal_s', sa.Integer(), nullable=True),
    sa.Column('default_external_s', sa.Integer(), nullable=True),
    sa.Column('allow_external_overflow', sa.Boolean(), server_default=sa.text('false'), nullable=False),
    sa.Column('is_active', sa.Boolean(), server_default=sa.text('true'), nullable=False),
    sa.Column('created_by', sa.UUID(), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.CheckConstraint("tv_provision IN ('OWN_TV','RD_TV_COMODATO')", name=op.f('ck_partner_plans_tv_provision_valid')),
    sa.CheckConstraint("upgrade_policy IN ('FREE','PAID','NEGOTIATED')", name=op.f('ck_partner_plans_upgrade_policy_valid')),
    sa.CheckConstraint('(default_internal_s IS NULL) = (default_external_s IS NULL)', name=op.f('ck_partner_plans_rotation_pair')),
    sa.CheckConstraint('default_internal_s IS NULL OR (default_internal_s >= 0 AND default_external_s >= 30 AND default_internal_s % 30 = 0 AND default_external_s % 30 = 0 AND 3600 % (default_internal_s + default_external_s) = 0)', name=op.f('ck_partner_plans_rotation_valid')),
    sa.CheckConstraint('default_tv_inches IS NULL OR default_tv_inches > 0', name=op.f('ck_partner_plans_tv_inches_positive')),
    sa.CheckConstraint('deposit_cents >= 0 AND (deposit_required OR deposit_cents = 0)', name=op.f('ck_partner_plans_deposit_consistent')),
    sa.CheckConstraint('installation_fee_cents >= 0 AND (installation_fee_required OR installation_fee_cents = 0)', name=op.f('ck_partner_plans_installation_fee_consistent')),
    sa.CheckConstraint('monthly_fee_cents >= 0', name=op.f('ck_partner_plans_monthly_fee_nonneg')),
    sa.CheckConstraint('upgrade_fee_cents IS NULL OR upgrade_fee_cents >= 0', name=op.f('ck_partner_plans_upgrade_fee_nonneg')),
    sa.ForeignKeyConstraint(['created_by'], ['users.id'], name=op.f('fk_partner_plans_created_by_users'), ondelete='SET NULL'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_partner_plans')),
    sa.UniqueConstraint('slug', name=op.f('uq_partner_plans_slug'))
    )
    op.create_table('price_tables',
    sa.Column('id', sa.UUID(), nullable=False),
    sa.Column('name', sa.String(length=120), nullable=False),
    sa.Column('version', sa.Integer(), nullable=False),
    sa.Column('status', sa.String(length=12), nullable=False),
    sa.Column('notes', sa.Text(), nullable=True),
    sa.Column('cloned_from_id', sa.UUID(), nullable=True),
    sa.Column('created_by', sa.UUID(), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('activated_by', sa.UUID(), nullable=True),
    sa.Column('activated_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('archived_at', sa.DateTime(timezone=True), nullable=True),
    sa.CheckConstraint("status IN ('RASCUNHO', 'ATIVA', 'ARQUIVADA')", name=op.f('ck_price_tables_status_valid')),
    sa.ForeignKeyConstraint(['activated_by'], ['users.id'], name=op.f('fk_price_tables_activated_by_users'), ondelete='RESTRICT'),
    sa.ForeignKeyConstraint(['cloned_from_id'], ['price_tables.id'], name=op.f('fk_price_tables_cloned_from_id_price_tables'), ondelete='RESTRICT'),
    sa.ForeignKeyConstraint(['created_by'], ['users.id'], name=op.f('fk_price_tables_created_by_users'), ondelete='RESTRICT'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_price_tables')),
    sa.UniqueConstraint('version', name='uq_price_tables_version')
    )
    op.create_index('ux_price_tables_one_active', 'price_tables', ['status'], unique=True, postgresql_where=sa.text("status = 'ATIVA'"))
    op.create_table('promotions',
    sa.Column('id', sa.UUID(), nullable=False),
    sa.Column('name', sa.String(length=120), nullable=False),
    sa.Column('discount_bps', sa.Integer(), nullable=False),
    sa.Column('valid_from', sa.DateTime(timezone=True), nullable=False),
    sa.Column('valid_until', sa.DateTime(timezone=True), nullable=False),
    sa.Column('plan_ids', postgresql.JSONB(astext_type=Text()), nullable=True),
    sa.Column('active', sa.Boolean(), nullable=False),
    sa.Column('created_by', sa.UUID(), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.CheckConstraint('discount_bps BETWEEN 1 AND 10000', name=op.f('ck_promotions_discount_range')),
    sa.CheckConstraint('valid_until > valid_from', name=op.f('ck_promotions_validity')),
    sa.ForeignKeyConstraint(['created_by'], ['users.id'], name=op.f('fk_promotions_created_by_users'), ondelete='RESTRICT'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_promotions'))
    )
    op.create_table('rd_price_floor_history',
    sa.Column('id', sa.UUID(), nullable=False),
    sa.Column('key', sa.String(length=80), nullable=False),
    sa.Column('old_min_price_cents', sa.BigInteger(), nullable=True),
    sa.Column('new_min_price_cents', sa.BigInteger(), nullable=False),
    sa.Column('version', sa.Integer(), nullable=False),
    sa.Column('changed_by', sa.UUID(), nullable=True),
    sa.Column('reason', sa.String(length=500), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.ForeignKeyConstraint(['changed_by'], ['users.id'], name=op.f('fk_rd_price_floor_history_changed_by_users'), ondelete='SET NULL'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_rd_price_floor_history'))
    )
    op.create_index(op.f('ix_rd_price_floor_history_key'), 'rd_price_floor_history', ['key'], unique=False)
    op.create_table('rd_price_floors',
    sa.Column('key', sa.String(length=80), nullable=False),
    sa.Column('label', sa.String(length=200), nullable=False),
    sa.Column('unit', sa.String(length=80), nullable=False),
    sa.Column('min_price_cents', sa.BigInteger(), nullable=False),
    sa.Column('note', sa.String(length=500), nullable=True),
    sa.Column('version', sa.Integer(), nullable=False),
    sa.Column('updated_by', sa.UUID(), nullable=True),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.CheckConstraint('min_price_cents >= 0', name=op.f('ck_rd_price_floors_min_price_non_negative')),
    sa.ForeignKeyConstraint(['updated_by'], ['users.id'], name=op.f('fk_rd_price_floors_updated_by_users'), ondelete='SET NULL'),
    sa.PrimaryKeyConstraint('key', name=op.f('pk_rd_price_floors'))
    )
    op.create_table('report_exports',
    sa.Column('id', sa.UUID(), nullable=False),
    sa.Column('report_key', sa.String(length=60), nullable=False),
    sa.Column('params', postgresql.JSONB(astext_type=Text()), nullable=False),
    sa.Column('format', sa.String(length=5), nullable=False),
    sa.Column('status', sa.String(length=10), nullable=False),
    sa.Column('storage_key', sa.String(length=512), nullable=True),
    sa.Column('filename', sa.String(length=200), nullable=True),
    sa.Column('content_type', sa.String(length=100), nullable=True),
    sa.Column('size_bytes', sa.BigInteger(), nullable=True),
    sa.Column('sha256', sa.String(length=64), nullable=True),
    sa.Column('row_count', sa.Integer(), nullable=True),
    sa.Column('emailed_to', sa.Integer(), nullable=True),
    sa.Column('error', sa.String(length=500), nullable=True),
    sa.Column('requested_by', sa.UUID(), nullable=True),
    sa.Column('org_id', sa.UUID(), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('expires_at', sa.DateTime(timezone=True), nullable=True),
    sa.CheckConstraint("format IN ('csv','xlsx','pdf')", name=op.f('ck_report_exports_format_valid')),
    sa.CheckConstraint("status IN ('CONCLUIDO','FALHOU')", name=op.f('ck_report_exports_status_valid')),
    sa.ForeignKeyConstraint(['requested_by'], ['users.id'], name=op.f('fk_report_exports_requested_by_users'), ondelete='SET NULL'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_report_exports'))
    )
    op.create_index('ix_report_exports_requested_by', 'report_exports', ['requested_by', 'created_at'], unique=False)
    op.create_table('rotation_policies',
    sa.Column('id', sa.UUID(), nullable=False),
    sa.Column('scope', sa.String(length=20), nullable=False),
    sa.Column('scope_id', sa.UUID(), nullable=True),
    sa.Column('scope_key', sa.Text(), sa.Computed("scope || ':' || coalesce(scope_id::text, '*')", persisted=True), nullable=False),
    sa.Column('internal_seconds', sa.Integer(), nullable=False),
    sa.Column('external_seconds', sa.Integer(), nullable=False),
    sa.Column('allow_external_overflow', sa.Boolean(), server_default='false', nullable=False),
    sa.Column('overflow_max_seconds', sa.Integer(), server_default='0', nullable=False),
    sa.Column('effective_from', sa.DateTime(timezone=True), nullable=False),
    sa.Column('effective_to', sa.DateTime(timezone=True), nullable=True),
    sa.Column('version', sa.Integer(), server_default='1', nullable=False),
    sa.Column('reason', sa.Text(), nullable=False),
    sa.Column('created_by', sa.UUID(), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    postgresql.ExcludeConstraint((sa.column('scope_key'), '='), (sa.text("tstzrange(effective_from, effective_to, '[)')"), '&&'), using='gist', name='ex_rotation_policies_no_overlap'),
    sa.CheckConstraint("(scope = 'GLOBAL') = (scope_id IS NULL)", name=op.f('ck_rotation_policies_scope_id_matches_scope')),
    sa.CheckConstraint("scope IN ('GLOBAL','PARTNER_PLAN','ESTABLISHMENT','SCREEN')", name=op.f('ck_rotation_policies_scope')),
    sa.CheckConstraint('3600 % (internal_seconds + external_seconds) = 0', name=op.f('ck_rotation_policies_cycle_divides_hour')),
    sa.CheckConstraint('allow_external_overflow OR overflow_max_seconds = 0', name=op.f('ck_rotation_policies_overflow_requires_flag')),
    sa.CheckConstraint('effective_to IS NULL OR effective_to > effective_from', name=op.f('ck_rotation_policies_effective_range')),
    sa.CheckConstraint('external_seconds > 0 AND external_seconds % 30 = 0', name=op.f('ck_rotation_policies_external_multiple_30')),
    sa.CheckConstraint('internal_seconds > 0 AND internal_seconds % 30 = 0', name=op.f('ck_rotation_policies_internal_multiple_30')),
    sa.CheckConstraint('length(btrim(reason)) >= 5', name=op.f('ck_rotation_policies_reason_required')),
    sa.CheckConstraint('overflow_max_seconds >= 0 AND overflow_max_seconds % 30 = 0 AND overflow_max_seconds <= internal_seconds', name=op.f('ck_rotation_policies_overflow_limit')),
    sa.ForeignKeyConstraint(['created_by'], ['users.id'], name=op.f('fk_rotation_policies_created_by_users'), ondelete='RESTRICT'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_rotation_policies'))
    )
    op.create_index('ix_rotation_policies_scope', 'rotation_policies', ['scope', 'scope_id'], unique=False)
    op.create_table('sales_reps',
    sa.Column('user_id', sa.UUID(), nullable=False),
    sa.Column('kind', sa.String(length=20), nullable=False),
    sa.Column('region_label', sa.String(length=120), nullable=True),
    sa.Column('city_key', sa.String(length=120), nullable=True),
    sa.Column('uf', sa.String(length=2), nullable=True),
    sa.Column('manager_id', sa.UUID(), nullable=True),
    sa.Column('compensation', sa.String(length=20), nullable=False),
    sa.Column('commission_bps', sa.Integer(), nullable=True),
    sa.Column('is_active', sa.Boolean(), nullable=False),
    sa.Column('id', sa.UUID(), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.CheckConstraint("compensation IN ('COMISSAO','SALARIO_FIXO','AMBOS')", name=op.f('ck_sales_reps_compensation_valid')),
    sa.CheckConstraint("kind IN ('VENDEDOR','REPRESENTANTE','FRANQUIA','GERENTE')", name=op.f('ck_sales_reps_kind_valid')),
    sa.CheckConstraint('commission_bps IS NULL OR (commission_bps >= 0 AND commission_bps <= 10000)', name=op.f('ck_sales_reps_commission_bps_range')),
    sa.ForeignKeyConstraint(['manager_id'], ['sales_reps.id'], name=op.f('fk_sales_reps_manager_id_sales_reps'), ondelete='SET NULL'),
    sa.ForeignKeyConstraint(['user_id'], ['users.id'], name=op.f('fk_sales_reps_user_id_users'), ondelete='RESTRICT'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_sales_reps')),
    sa.UniqueConstraint('user_id', name='uq_sales_reps_user_id')
    )
    op.create_table('advertiser_profiles',
    sa.Column('org_id', sa.UUID(), nullable=False),
    sa.Column('category_id', sa.UUID(), nullable=True),
    sa.Column('updated_by', sa.UUID(), nullable=True),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.ForeignKeyConstraint(['category_id'], ['taxonomy_nodes.id'], name=op.f('fk_advertiser_profiles_category_id_taxonomy_nodes'), ondelete='RESTRICT'),
    sa.ForeignKeyConstraint(['org_id'], ['organizations.id'], name=op.f('fk_advertiser_profiles_org_id_organizations'), ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['updated_by'], ['users.id'], name=op.f('fk_advertiser_profiles_updated_by_users'), ondelete='SET NULL'),
    sa.PrimaryKeyConstraint('org_id', name=op.f('pk_advertiser_profiles'))
    )
    op.create_index('ix_advertiser_profiles_category_id', 'advertiser_profiles', ['category_id'], unique=False)
    op.create_table('advertiser_segments',
    sa.Column('org_id', sa.UUID(), nullable=False),
    sa.Column('segment_id', sa.UUID(), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.ForeignKeyConstraint(['org_id'], ['organizations.id'], name=op.f('fk_advertiser_segments_org_id_organizations'), ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['segment_id'], ['segments.id'], name=op.f('fk_advertiser_segments_segment_id_segments'), ondelete='RESTRICT'),
    sa.PrimaryKeyConstraint('org_id', 'segment_id', name=op.f('pk_advertiser_segments'))
    )
    op.create_index('ix_advertiser_segments_segment_id', 'advertiser_segments', ['segment_id'], unique=False)
    op.create_table('agency_clients',
    sa.Column('id', sa.UUID(), nullable=False),
    sa.Column('agency_org_id', sa.UUID(), nullable=False),
    sa.Column('client_org_id', sa.UUID(), nullable=False),
    sa.Column('status', sa.String(length=10), nullable=False),
    sa.Column('created_by', sa.UUID(), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('ended_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('ended_by', sa.UUID(), nullable=True),
    sa.Column('end_reason', sa.Text(), nullable=True),
    sa.CheckConstraint("status IN ('ACTIVE','ENDED')", name=op.f('ck_agency_clients_status_valid')),
    sa.CheckConstraint('agency_org_id <> client_org_id', name=op.f('ck_agency_clients_distinct_orgs')),
    sa.ForeignKeyConstraint(['agency_org_id'], ['organizations.id'], name=op.f('fk_agency_clients_agency_org_id_organizations'), ondelete='RESTRICT'),
    sa.ForeignKeyConstraint(['client_org_id'], ['organizations.id'], name=op.f('fk_agency_clients_client_org_id_organizations'), ondelete='RESTRICT'),
    sa.ForeignKeyConstraint(['created_by'], ['users.id'], name=op.f('fk_agency_clients_created_by_users'), ondelete='SET NULL'),
    sa.ForeignKeyConstraint(['ended_by'], ['users.id'], name=op.f('fk_agency_clients_ended_by_users'), ondelete='SET NULL'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_agency_clients'))
    )
    op.create_index('ix_agency_clients_agency_org_id', 'agency_clients', ['agency_org_id'], unique=False)
    op.create_index('ix_agency_clients_client_org_id', 'agency_clients', ['client_org_id'], unique=False)
    op.create_index('uq_agency_clients_active_pair', 'agency_clients', ['agency_org_id', 'client_org_id'], unique=True, postgresql_where=sa.text("status = 'ACTIVE'"))
    op.create_table('brands',
    sa.Column('id', sa.UUID(), nullable=False),
    sa.Column('name', sa.String(length=120), nullable=False),
    sa.Column('slug', sa.String(length=140), nullable=False),
    sa.Column('owner_org_id', sa.UUID(), nullable=True),
    sa.Column('is_active', sa.Boolean(), server_default=sa.text('true'), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.ForeignKeyConstraint(['owner_org_id'], ['organizations.id'], name=op.f('fk_brands_owner_org_id_organizations'), ondelete='RESTRICT'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_brands')),
    sa.UniqueConstraint('slug', name=op.f('uq_brands_slug'))
    )
    op.create_index('ix_brands_owner_org_id', 'brands', ['owner_org_id'], unique=False)
    op.create_table('competitor_links',
    sa.Column('id', sa.UUID(), nullable=False),
    sa.Column('org_a_id', sa.UUID(), nullable=False),
    sa.Column('org_b_id', sa.UUID(), nullable=False),
    sa.Column('reason', sa.Text(), nullable=False),
    sa.Column('created_by', sa.UUID(), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.CheckConstraint('org_a_id < org_b_id', name=op.f('ck_competitor_links_ordered_pair')),
    sa.ForeignKeyConstraint(['created_by'], ['users.id'], name=op.f('fk_competitor_links_created_by_users'), ondelete='SET NULL'),
    sa.ForeignKeyConstraint(['org_a_id'], ['organizations.id'], name=op.f('fk_competitor_links_org_a_id_organizations'), ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['org_b_id'], ['organizations.id'], name=op.f('fk_competitor_links_org_b_id_organizations'), ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_competitor_links')),
    sa.UniqueConstraint('org_a_id', 'org_b_id', name='uq_competitor_links_pair')
    )
    op.create_index('ix_competitor_links_org_b_id', 'competitor_links', ['org_b_id'], unique=False)
    op.create_table('cp_releases',
    sa.Column('id', sa.UUID(), nullable=False),
    sa.Column('version', sa.String(length=40), nullable=False),
    sa.Column('status', sa.String(length=15), nullable=False),
    sa.Column('image_ref', sa.String(length=255), nullable=True),
    sa.Column('changelog', sa.Text(), nullable=True),
    sa.Column('validated_on_instance_id', sa.UUID(), nullable=True),
    sa.Column('validated_by', sa.UUID(), nullable=True),
    sa.Column('validated_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('published_by', sa.UUID(), nullable=True),
    sa.Column('published_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('created_by', sa.UUID(), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.CheckConstraint("status IN ('RASCUNHO','VALIDADA','PUBLICADA','DESCONTINUADA')", name=op.f('ck_cp_releases_status_valid')),
    sa.ForeignKeyConstraint(['validated_on_instance_id'], ['cp_instances.id'], name=op.f('fk_cp_releases_validated_on_instance_id_cp_instances'), ondelete='SET NULL'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_cp_releases')),
    sa.UniqueConstraint('version', name='uq_cp_releases_version')
    )
    op.create_table('delivery_proofs',
    sa.Column('id', sa.UUID(), nullable=False),
    sa.Column('code', sa.String(length=16), nullable=False),
    sa.Column('campaign_id', sa.UUID(), nullable=False),
    sa.Column('org_id', sa.UUID(), nullable=False),
    sa.Column('period_start', sa.Date(), nullable=False),
    sa.Column('period_end', sa.Date(), nullable=False),
    sa.Column('source', sa.String(length=80), nullable=False),
    sa.Column('total_valid_plays', sa.BigInteger(), nullable=False),
    sa.Column('payload', postgresql.JSONB(astext_type=Text()), nullable=False),
    sa.Column('payload_sha256', sa.String(length=64), nullable=False),
    sa.Column('payload_signature', sa.Text(), nullable=False),
    sa.Column('kid', sa.String(length=40), nullable=False),
    sa.Column('pdf_key', sa.String(length=512), nullable=False),
    sa.Column('pdf_sha256', sa.String(length=64), nullable=False),
    sa.Column('pdf_signature', sa.Text(), nullable=False),
    sa.Column('created_by', sa.UUID(), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.ForeignKeyConstraint(['org_id'], ['organizations.id'], name=op.f('fk_delivery_proofs_org_id_organizations'), ondelete='RESTRICT'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_delivery_proofs')),
    sa.UniqueConstraint('code', name='uq_delivery_proofs_code'),
    sa.UniqueConstraint('pdf_sha256', name='uq_delivery_proofs_pdf_sha256')
    )
    op.create_index('ix_delivery_proofs_campaign_id', 'delivery_proofs', ['campaign_id'], unique=False)
    op.create_index('ix_delivery_proofs_org_id', 'delivery_proofs', ['org_id'], unique=False)
    op.create_table('design_templates',
    sa.Column('id', sa.UUID(), nullable=False),
    sa.Column('org_id', sa.UUID(), nullable=True),
    sa.Column('name', sa.String(length=120), nullable=False),
    sa.Column('description', sa.Text(), nullable=True),
    sa.Column('orientation', sa.String(length=12), nullable=False),
    sa.Column('width', sa.Integer(), nullable=False),
    sa.Column('height', sa.Integer(), nullable=False),
    sa.Column('spec', postgresql.JSONB(astext_type=Text()), nullable=False),
    sa.Column('status', sa.String(length=12), nullable=False),
    sa.Column('version', sa.Integer(), nullable=False),
    sa.Column('created_by', sa.UUID(), nullable=True),
    sa.Column('updated_by', sa.UUID(), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.CheckConstraint("(orientation = 'HORIZONTAL' AND width = 1920 AND height = 1080) OR (orientation = 'VERTICAL' AND width = 1080 AND height = 1920)", name=op.f('ck_design_templates_canvas_fhd')),
    sa.CheckConstraint("orientation IN ('HORIZONTAL','VERTICAL')", name=op.f('ck_design_templates_orientation_valid')),
    sa.CheckConstraint("status IN ('RASCUNHO','ATIVO','ARQUIVADO')", name=op.f('ck_design_templates_status_valid')),
    sa.ForeignKeyConstraint(['created_by'], ['users.id'], name=op.f('fk_design_templates_created_by_users'), ondelete='SET NULL'),
    sa.ForeignKeyConstraint(['org_id'], ['organizations.id'], name=op.f('fk_design_templates_org_id_organizations'), ondelete='RESTRICT'),
    sa.ForeignKeyConstraint(['updated_by'], ['users.id'], name=op.f('fk_design_templates_updated_by_users'), ondelete='SET NULL'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_design_templates'))
    )
    op.create_index('ix_design_templates_org_id', 'design_templates', ['org_id'], unique=False)
    op.create_index('ix_design_templates_status', 'design_templates', ['status'], unique=False)
    op.create_table('financial_approval_events',
    sa.Column('id', sa.UUID(), nullable=False),
    sa.Column('request_id', sa.UUID(), nullable=False),
    sa.Column('event', sa.String(length=30), nullable=False),
    sa.Column('actor_user_id', sa.UUID(), nullable=True),
    sa.Column('data', postgresql.JSONB(astext_type=Text()), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.ForeignKeyConstraint(['request_id'], ['financial_approval_requests.id'], name=op.f('fk_financial_approval_events_request_id_financial_appro_ca81'), ondelete='RESTRICT'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_financial_approval_events'))
    )
    op.create_index('ix_financial_approval_events_request', 'financial_approval_events', ['request_id', 'created_at'], unique=False)
    op.create_table('ledger_transactions',
    sa.Column('id', sa.UUID(), nullable=False),
    sa.Column('kind', sa.String(length=40), nullable=False),
    sa.Column('idempotency_key', sa.String(length=200), nullable=False),
    sa.Column('occurred_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('competence_date', sa.Date(), nullable=False),
    sa.Column('description', sa.String(length=500), nullable=False),
    sa.Column('created_by', sa.UUID(), nullable=True),
    sa.Column('approval_request_id', sa.UUID(), nullable=True),
    sa.Column('reverses_transaction_id', sa.UUID(), nullable=True),
    sa.Column('external_provider', sa.String(length=30), nullable=True),
    sa.Column('external_ref', sa.String(length=120), nullable=True),
    sa.Column('ref_type', sa.String(length=40), nullable=True),
    sa.Column('ref_id', sa.String(length=64), nullable=True),
    sa.Column('org_id', sa.UUID(), nullable=True),
    sa.Column('metadata', postgresql.JSONB(astext_type=Text()), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.ForeignKeyConstraint(['approval_request_id'], ['financial_approval_requests.id'], name=op.f('fk_ledger_transactions_approval_request_id_financial_ap_25e2'), ondelete='RESTRICT'),
    sa.ForeignKeyConstraint(['created_by'], ['users.id'], name=op.f('fk_ledger_transactions_created_by_users'), ondelete='RESTRICT'),
    sa.ForeignKeyConstraint(['reverses_transaction_id'], ['ledger_transactions.id'], name=op.f('fk_ledger_transactions_reverses_transaction_id_ledger_t_018b'), ondelete='RESTRICT'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_ledger_transactions')),
    sa.UniqueConstraint('idempotency_key', name='uq_ledger_transactions_idempotency_key'),
    sa.UniqueConstraint('reverses_transaction_id', name='uq_ledger_transactions_reverses')
    )
    op.create_index('ix_ledger_transactions_competence', 'ledger_transactions', ['competence_date'], unique=False)
    op.create_index('ix_ledger_transactions_external', 'ledger_transactions', ['external_provider', 'external_ref'], unique=False)
    op.create_index('ix_ledger_transactions_kind', 'ledger_transactions', ['kind'], unique=False)
    op.create_index('ix_ledger_transactions_org', 'ledger_transactions', ['org_id'], unique=False)
    op.create_index('ix_ledger_transactions_ref', 'ledger_transactions', ['ref_type', 'ref_id'], unique=False)
    op.create_table('orders',
    sa.Column('id', sa.UUID(), nullable=False),
    sa.Column('org_id', sa.UUID(), nullable=False),
    sa.Column('kind', sa.String(length=12), nullable=False),
    sa.Column('status', sa.String(length=24), nullable=False),
    sa.Column('idempotency_key', sa.String(length=100), nullable=False),
    sa.Column('request_hash', sa.String(length=64), nullable=False),
    sa.Column('media_cents', sa.BigInteger(), nullable=False),
    sa.Column('fees_cents', sa.BigInteger(), nullable=False),
    sa.Column('discount_cents', sa.BigInteger(), nullable=False),
    sa.Column('credit_applied_cents', sa.BigInteger(), nullable=False),
    sa.Column('total_cents', sa.BigInteger(), nullable=False),
    sa.Column('coupon_id', sa.UUID(), nullable=True),
    sa.Column('proposal_id', sa.UUID(), nullable=True),
    sa.Column('commercial_discount_bps', sa.Integer(), nullable=False),
    sa.Column('commercial_discount_approved_by', sa.UUID(), nullable=True),
    sa.Column('payment_order_id', sa.UUID(), nullable=True),
    sa.Column('response', postgresql.JSONB(astext_type=Text()), nullable=True),
    sa.Column('expires_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('paid_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('cancelled_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('created_by', sa.UUID(), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.CheckConstraint("kind IN ('CHECKOUT', 'CHANGE', 'PROPOSAL', 'RENEWAL')", name=op.f('ck_orders_kind_valid')),
    sa.CheckConstraint("status IN ('AGUARDANDO_PAGAMENTO', 'PAGO', 'EXPIRADO', 'CANCELADO')", name=op.f('ck_orders_status_valid')),
    sa.CheckConstraint('total_cents = media_cents + fees_cents - discount_cents - credit_applied_cents', name=op.f('ck_orders_total_consistent')),
    sa.CheckConstraint('total_cents >= 0 AND media_cents >= 0 AND fees_cents >= 0 AND discount_cents >= 0', name=op.f('ck_orders_amounts_nonneg')),
    sa.ForeignKeyConstraint(['commercial_discount_approved_by'], ['users.id'], name=op.f('fk_orders_commercial_discount_approved_by_users'), ondelete='RESTRICT'),
    sa.ForeignKeyConstraint(['coupon_id'], ['coupons.id'], name=op.f('fk_orders_coupon_id_coupons'), ondelete='RESTRICT'),
    sa.ForeignKeyConstraint(['created_by'], ['users.id'], name=op.f('fk_orders_created_by_users'), ondelete='RESTRICT'),
    sa.ForeignKeyConstraint(['org_id'], ['organizations.id'], name=op.f('fk_orders_org_id_organizations'), ondelete='RESTRICT'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_orders')),
    sa.UniqueConstraint('org_id', 'idempotency_key', name='uq_orders_org_idempotency')
    )
    op.create_index('ix_orders_expires_at', 'orders', ['expires_at'], unique=False)
    op.create_index('ix_orders_org_status', 'orders', ['org_id', 'status'], unique=False)
    op.create_table('organization_approval_policies',
    sa.Column('org_id', sa.UUID(), nullable=False),
    sa.Column('mode', sa.String(length=10), nullable=False),
    sa.Column('reason', sa.Text(), nullable=False),
    sa.Column('updated_by', sa.UUID(), nullable=True),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.CheckConstraint("mode IN ('MANUAL', 'AUTO')", name=op.f('ck_organization_approval_policies_mode_valid')),
    sa.ForeignKeyConstraint(['org_id'], ['organizations.id'], name=op.f('fk_organization_approval_policies_org_id_organizations'), ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['updated_by'], ['users.id'], name=op.f('fk_organization_approval_policies_updated_by_users'), ondelete='RESTRICT'),
    sa.PrimaryKeyConstraint('org_id', name=op.f('pk_organization_approval_policies'))
    )
    op.create_table('payment_orders',
    sa.Column('id', sa.UUID(), nullable=False),
    sa.Column('org_id', sa.UUID(), nullable=False),
    sa.Column('reference_type', sa.String(length=40), nullable=False),
    sa.Column('reference_id', sa.String(length=64), nullable=False),
    sa.Column('reference', postgresql.JSONB(astext_type=Text()), nullable=False),
    sa.Column('items', postgresql.JSONB(astext_type=Text()), nullable=False),
    sa.Column('amount_cents', sa.BigInteger(), nullable=False),
    sa.Column('eligible_revenue_cents', sa.BigInteger(), nullable=False),
    sa.Column('services_cents', sa.BigInteger(), nullable=False),
    sa.Column('currency', sa.String(length=3), server_default=sa.text("'BRL'"), nullable=False),
    sa.Column('method', sa.String(length=20), nullable=False),
    sa.Column('installments', sa.SmallInteger(), server_default=sa.text('1'), nullable=False),
    sa.Column('status', sa.String(length=24), nullable=False),
    sa.Column('dispute_status', sa.String(length=20), nullable=True),
    sa.Column('provider', sa.String(length=30), nullable=False),
    sa.Column('instance_slug', sa.String(length=64), nullable=False),
    sa.Column('provider_transaction_id', sa.BigInteger(), nullable=True),
    sa.Column('provider_external_ref', sa.String(length=80), nullable=True),
    sa.Column('provider_processor_ref', sa.String(length=120), nullable=True),
    sa.Column('provider_status', sa.String(length=30), nullable=True),
    sa.Column('provider_error', sa.String(length=300), nullable=True),
    sa.Column('pix_qr_code', sa.Text(), nullable=True),
    sa.Column('pix_end_to_end_id', sa.String(length=80), nullable=True),
    sa.Column('paid_amount_cents', sa.BigInteger(), nullable=True),
    sa.Column('fee_cents', sa.BigInteger(), nullable=True),
    sa.Column('net_cents', sa.BigInteger(), nullable=True),
    sa.Column('refunded_cents', sa.BigInteger(), server_default=sa.text('0'), nullable=False),
    sa.Column('seller_user_id', sa.UUID(), nullable=True),
    sa.Column('idempotency_key', sa.String(length=120), nullable=True),
    sa.Column('expires_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('paid_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('created_by', sa.UUID(), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.CheckConstraint("method IN ('pix','credit_card')", name=op.f('ck_payment_orders_method_valid')),
    sa.CheckConstraint("status IN ('CREATED','PENDING','CREATION_FAILED','CREATION_UNKNOWN','PAID','PARTIALLY_REFUNDED','REFUNDED','FAILED','CANCELLED','EXPIRED')", name=op.f('ck_payment_orders_status_valid')),
    sa.CheckConstraint('amount_cents > 0', name=op.f('ck_payment_orders_amount_positive')),
    sa.CheckConstraint('eligible_revenue_cents + services_cents = amount_cents', name=op.f('ck_payment_orders_parts_sum')),
    sa.CheckConstraint('eligible_revenue_cents >= 0 AND services_cents >= 0', name=op.f('ck_payment_orders_parts_nonneg')),
    sa.CheckConstraint('installments BETWEEN 1 AND 12', name=op.f('ck_payment_orders_installments_range')),
    sa.CheckConstraint('refunded_cents >= 0 AND refunded_cents <= amount_cents', name=op.f('ck_payment_orders_refunded_range')),
    sa.ForeignKeyConstraint(['created_by'], ['users.id'], name=op.f('fk_payment_orders_created_by_users'), ondelete='RESTRICT'),
    sa.ForeignKeyConstraint(['org_id'], ['organizations.id'], name=op.f('fk_payment_orders_org_id_organizations'), ondelete='RESTRICT'),
    sa.ForeignKeyConstraint(['seller_user_id'], ['users.id'], name=op.f('fk_payment_orders_seller_user_id_users'), ondelete='RESTRICT'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_payment_orders')),
    sa.UniqueConstraint('idempotency_key', name='uq_payment_orders_idempotency_key'),
    sa.UniqueConstraint('provider_external_ref', name='uq_payment_orders_provider_external_ref')
    )
    op.create_index('ix_payment_orders_org_id', 'payment_orders', ['org_id'], unique=False)
    op.create_index('ix_payment_orders_reference', 'payment_orders', ['reference_type', 'reference_id'], unique=False)
    op.create_index('ix_payment_orders_status', 'payment_orders', ['status'], unique=False)
    op.create_table('plans',
    sa.Column('id', sa.UUID(), nullable=False),
    sa.Column('name', sa.String(length=120), nullable=False),
    sa.Column('slug', sa.String(length=140), nullable=False),
    sa.Column('description', sa.Text(), nullable=True),
    sa.Column('kind', sa.String(length=12), nullable=False),
    sa.Column('org_id', sa.UUID(), nullable=True),
    sa.Column('status', sa.String(length=20), nullable=False),
    sa.Column('period_type', sa.String(length=12), nullable=False),
    sa.Column('period_days', sa.SmallInteger(), nullable=True),
    sa.Column('duration_s', sa.SmallInteger(), nullable=False),
    sa.Column('freq_kind', sa.String(length=20), nullable=False),
    sa.Column('freq_value', sa.Integer(), nullable=False),
    sa.Column('specific_times', postgresql.JSONB(astext_type=Text()), nullable=True),
    sa.Column('min_interval_s', sa.Integer(), nullable=False),
    sa.Column('days_mask', sa.SmallInteger(), nullable=False),
    sa.Column('time_bands', postgresql.JSONB(astext_type=Text()), nullable=False),
    sa.Column('screens_min', sa.Integer(), nullable=False),
    sa.Column('screens_max', sa.Integer(), nullable=False),
    sa.Column('priority', sa.String(length=10), nullable=False),
    sa.Column('exclusivity', sa.String(length=30), nullable=False),
    sa.Column('establishment_category_ids', postgresql.JSONB(astext_type=Text()), nullable=True),
    sa.Column('region_targets', postgresql.JSONB(astext_type=Text()), nullable=True),
    sa.Column('discount_bps', sa.Integer(), nullable=False),
    sa.Column('max_campaigns', sa.Integer(), nullable=True),
    sa.Column('max_creatives', sa.Integer(), nullable=True),
    sa.Column('max_screens', sa.Integer(), nullable=True),
    sa.Column('max_upload_mb', sa.Integer(), nullable=True),
    sa.Column('reference_quote', postgresql.JSONB(astext_type=Text()), nullable=True),
    sa.Column('margin_bps', sa.Integer(), nullable=True),
    sa.Column('margin_status', sa.String(length=12), nullable=True),
    sa.Column('version', sa.Integer(), nullable=False),
    sa.Column('cloned_from_id', sa.UUID(), nullable=True),
    sa.Column('created_by', sa.UUID(), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('approved_by', sa.UUID(), nullable=True),
    sa.Column('approved_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('rejection_reason', sa.Text(), nullable=True),
    sa.CheckConstraint("kind <> 'CUSTOM' OR org_id IS NOT NULL", name=op.f('ck_plans_custom_has_org')),
    sa.CheckConstraint("kind IN ('PREDEFINED', 'CUSTOM')", name=op.f('ck_plans_kind_valid')),
    sa.CheckConstraint("period_type <> 'AVULSO' OR period_days IS NOT NULL", name=op.f('ck_plans_avulso_days')),
    sa.CheckConstraint("period_type IN ('AVULSO', 'MENSAL', 'SEMESTRAL', 'ANUAL')", name=op.f('ck_plans_period_type_valid')),
    sa.CheckConstraint("status IN ('RASCUNHO', 'PENDENTE_APROVACAO', 'ATIVO', 'INATIVO', 'REJEITADO')", name=op.f('ck_plans_status_valid')),
    sa.CheckConstraint('days_mask BETWEEN 1 AND 127', name=op.f('ck_plans_days_mask_valid')),
    sa.CheckConstraint('discount_bps BETWEEN 0 AND 10000', name=op.f('ck_plans_discount_range')),
    sa.CheckConstraint('duration_s IN (30, 60, 120)', name=op.f('ck_plans_duration_valid')),
    sa.CheckConstraint('freq_value > 0', name=op.f('ck_plans_freq_value_positive')),
    sa.CheckConstraint('period_days IS NULL OR period_days BETWEEN 1 AND 366', name=op.f('ck_plans_period_days_range')),
    sa.CheckConstraint('screens_min >= 1 AND screens_max >= screens_min', name=op.f('ck_plans_screens_range')),
    sa.ForeignKeyConstraint(['approved_by'], ['users.id'], name=op.f('fk_plans_approved_by_users'), ondelete='RESTRICT'),
    sa.ForeignKeyConstraint(['cloned_from_id'], ['plans.id'], name=op.f('fk_plans_cloned_from_id_plans'), ondelete='RESTRICT'),
    sa.ForeignKeyConstraint(['created_by'], ['users.id'], name=op.f('fk_plans_created_by_users'), ondelete='RESTRICT'),
    sa.ForeignKeyConstraint(['org_id'], ['organizations.id'], name=op.f('fk_plans_org_id_organizations'), ondelete='RESTRICT'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_plans')),
    sa.UniqueConstraint('slug', name='uq_plans_slug')
    )
    op.create_index('ix_plans_org_id', 'plans', ['org_id'], unique=False)
    op.create_index('ix_plans_status', 'plans', ['status'], unique=False)
    op.create_table('price_items',
    sa.Column('id', sa.UUID(), nullable=False),
    sa.Column('table_id', sa.UUID(), nullable=False),
    sa.Column('kind', sa.String(length=30), nullable=False),
    sa.Column('key', sa.String(length=100), nullable=False),
    sa.Column('mode', sa.String(length=30), nullable=False),
    sa.Column('min_value', sa.Integer(), nullable=True),
    sa.Column('max_value', sa.Integer(), nullable=True),
    sa.Column('value', sa.BigInteger(), nullable=False),
    sa.Column('description', sa.String(length=255), nullable=True),
    sa.CheckConstraint("kind IN ('DURATION_BASE', 'FREQUENCY', 'SCREEN_COUNT', 'PERIOD', 'REGION', 'TIME_BAND', 'SCREEN_CLASS', 'VENUE_CLASS', 'ESTABLISHMENT', 'EXCLUSIVITY', 'PRIORITY', 'MIN_PRICE', 'FEE', 'TAX', 'COST_GATEWAY', 'COST_SELLER', 'COST_SERVER', 'COST_EQUIPMENT', 'COST_DESIGNER', 'COST_DIRECT')", name=op.f('ck_price_items_kind_valid')),
    sa.CheckConstraint("mode IN ('BASE_CENTS', 'MULTIPLIER_BPS', 'ADD_CENTS', 'FIXED_CENTS', 'PERCENT_BPS', 'PER_SCREEN_MONTH_CENTS')", name=op.f('ck_price_items_mode_valid')),
    sa.CheckConstraint('min_value IS NULL OR max_value IS NULL OR max_value >= min_value', name=op.f('ck_price_items_range_valid')),
    sa.CheckConstraint('value >= 0', name=op.f('ck_price_items_value_nonneg')),
    sa.ForeignKeyConstraint(['table_id'], ['price_tables.id'], name=op.f('fk_price_items_table_id_price_tables'), ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_price_items')),
    sa.UniqueConstraint('table_id', 'kind', 'key', 'mode', 'min_value', 'max_value', name='uq_price_items_identity')
    )
    op.create_index('ix_price_items_table_kind', 'price_items', ['table_id', 'kind'], unique=False)
    op.create_table('report_runs',
    sa.Column('id', sa.UUID(), nullable=False),
    sa.Column('kind', sa.String(length=30), nullable=False),
    sa.Column('org_id', sa.UUID(), nullable=False),
    sa.Column('period_start', sa.Date(), nullable=False),
    sa.Column('period_end', sa.Date(), nullable=False),
    sa.Column('status', sa.String(length=20), nullable=False),
    sa.Column('recipients', sa.Integer(), nullable=False),
    sa.Column('pdf_sha256', sa.String(length=64), nullable=True),
    sa.Column('detail', sa.String(length=500), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.CheckConstraint("kind IN ('MENSAL_ANUNCIANTE','MENSAL_ESTABELECIMENTO')", name=op.f('ck_report_runs_kind_valid')),
    sa.CheckConstraint("status IN ('ENVIADO','SEM_DESTINATARIOS','FALHOU')", name=op.f('ck_report_runs_status_valid')),
    sa.ForeignKeyConstraint(['org_id'], ['organizations.id'], name=op.f('fk_report_runs_org_id_organizations'), ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_report_runs')),
    sa.UniqueConstraint('kind', 'org_id', 'period_start', name='uq_report_runs_kind_org_period')
    )
    op.create_table('template_assets',
    sa.Column('id', sa.UUID(), nullable=False),
    sa.Column('org_id', sa.UUID(), nullable=True),
    sa.Column('storage_key', sa.String(length=512), nullable=False),
    sa.Column('filename', sa.String(length=255), nullable=False),
    sa.Column('content_type', sa.String(length=50), nullable=False),
    sa.Column('width', sa.Integer(), nullable=False),
    sa.Column('height', sa.Integer(), nullable=False),
    sa.Column('size_bytes', sa.BigInteger(), nullable=False),
    sa.Column('sha256', sa.String(length=64), nullable=False),
    sa.Column('uploaded_by', sa.UUID(), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.ForeignKeyConstraint(['org_id'], ['organizations.id'], name=op.f('fk_template_assets_org_id_organizations'), ondelete='RESTRICT'),
    sa.ForeignKeyConstraint(['uploaded_by'], ['users.id'], name=op.f('fk_template_assets_uploaded_by_users'), ondelete='SET NULL'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_template_assets'))
    )
    op.create_index('ix_template_assets_org_id', 'template_assets', ['org_id'], unique=False)
    op.create_table('agency_client_grants',
    sa.Column('id', sa.UUID(), nullable=False),
    sa.Column('agency_client_id', sa.UUID(), nullable=False),
    sa.Column('user_id', sa.UUID(), nullable=False),
    sa.Column('role_assignment_id', sa.UUID(), nullable=True),
    sa.Column('created_by', sa.UUID(), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('revoked_at', sa.DateTime(timezone=True), nullable=True),
    sa.ForeignKeyConstraint(['agency_client_id'], ['agency_clients.id'], name=op.f('fk_agency_client_grants_agency_client_id_agency_clients'), ondelete='RESTRICT'),
    sa.ForeignKeyConstraint(['created_by'], ['users.id'], name=op.f('fk_agency_client_grants_created_by_users'), ondelete='SET NULL'),
    sa.ForeignKeyConstraint(['role_assignment_id'], ['role_assignments.id'], name=op.f('fk_agency_client_grants_role_assignment_id_role_assignments'), ondelete='SET NULL'),
    sa.ForeignKeyConstraint(['user_id'], ['users.id'], name=op.f('fk_agency_client_grants_user_id_users'), ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_agency_client_grants'))
    )
    op.create_index('ix_agency_client_grants_link_id', 'agency_client_grants', ['agency_client_id'], unique=False)
    op.create_index('ix_agency_client_grants_user_id', 'agency_client_grants', ['user_id'], unique=False)
    op.create_table('commission_campaigns',
    sa.Column('campaign_id', sa.UUID(), nullable=False),
    sa.Column('payment_order_id', sa.UUID(), nullable=False),
    sa.Column('org_id', sa.UUID(), nullable=False),
    sa.Column('model', sa.String(length=20), nullable=False),
    sa.Column('pool_bps', sa.Integer(), nullable=False),
    sa.Column('eligible_revenue_cents', sa.BigInteger(), nullable=False),
    sa.Column('pool_cents', sa.BigInteger(), nullable=False),
    sa.Column('status', sa.String(length=12), nullable=False),
    sa.Column('cancelled_on', sa.Date(), nullable=True),
    sa.Column('snapshot', postgresql.JSONB(astext_type=Text()), nullable=False),
    sa.Column('registered_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.CheckConstraint("model IN ('A_POOL','B_ATTRIBUTED')", name=op.f('ck_commission_campaigns_model_valid')),
    sa.CheckConstraint("status IN ('ACTIVE','CANCELLED')", name=op.f('ck_commission_campaigns_status_valid')),
    sa.CheckConstraint('eligible_revenue_cents >= 0 AND pool_cents >= 0', name=op.f('ck_commission_campaigns_amounts_nonneg')),
    sa.CheckConstraint('pool_bps BETWEEN 0 AND 10000', name=op.f('ck_commission_campaigns_pool_bps_range')),
    sa.ForeignKeyConstraint(['payment_order_id'], ['payment_orders.id'], name=op.f('fk_commission_campaigns_payment_order_id_payment_orders'), ondelete='RESTRICT'),
    sa.PrimaryKeyConstraint('campaign_id', name=op.f('pk_commission_campaigns'))
    )
    op.create_table('commission_competence_runs',
    sa.Column('competence', sa.Date(), nullable=False),
    sa.Column('status', sa.String(length=10), nullable=False),
    sa.Column('pool_released_cents', sa.BigInteger(), nullable=False),
    sa.Column('commission_cents', sa.BigInteger(), nullable=False),
    sa.Column('residue_cents', sa.BigInteger(), nullable=False),
    sa.Column('carry_cents', sa.BigInteger(), nullable=False),
    sa.Column('rounding_residue_ucents', sa.BigInteger(), nullable=False),
    sa.Column('close_transaction_id', sa.UUID(), nullable=True),
    sa.Column('release_transaction_id', sa.UUID(), nullable=True),
    sa.Column('closed_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('released_at', sa.DateTime(timezone=True), nullable=True),
    sa.CheckConstraint("status IN ('CLOSED','RELEASED')", name=op.f('ck_commission_competence_runs_status_valid')),
    sa.CheckConstraint('extract(day FROM competence) = 1', name=op.f('ck_commission_competence_runs_competence_day_1')),
    sa.ForeignKeyConstraint(['close_transaction_id'], ['ledger_transactions.id'], name=op.f('fk_commission_competence_runs_close_transaction_id_ledg_54ab'), ondelete='RESTRICT'),
    sa.ForeignKeyConstraint(['release_transaction_id'], ['ledger_transactions.id'], name=op.f('fk_commission_competence_runs_release_transaction_id_le_0606'), ondelete='RESTRICT'),
    sa.PrimaryKeyConstraint('competence', name=op.f('pk_commission_competence_runs'))
    )
    op.create_table('conflict_exceptions',
    sa.Column('id', sa.UUID(), nullable=False),
    sa.Column('establishment_id', sa.UUID(), nullable=False),
    sa.Column('object_type', sa.String(length=12), nullable=False),
    sa.Column('object_id', sa.UUID(), nullable=False),
    sa.Column('reason', sa.Text(), nullable=False),
    sa.Column('approved_by', sa.UUID(), nullable=False),
    sa.Column('valid_from', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('valid_until', sa.DateTime(timezone=True), nullable=True),
    sa.Column('revoked_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('revoked_by', sa.UUID(), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.CheckConstraint("object_type IN ('ADVERTISER', 'BRAND', 'CAMPAIGN')", name=op.f('ck_conflict_exceptions_object_type_valid')),
    sa.CheckConstraint('length(reason) >= 10', name=op.f('ck_conflict_exceptions_reason_len')),
    sa.CheckConstraint('valid_until IS NULL OR valid_until > valid_from', name=op.f('ck_conflict_exceptions_validity')),
    sa.ForeignKeyConstraint(['approved_by'], ['users.id'], name=op.f('fk_conflict_exceptions_approved_by_users'), ondelete='RESTRICT'),
    sa.ForeignKeyConstraint(['establishment_id'], ['establishments.id'], name=op.f('fk_conflict_exceptions_establishment_id_establishments'), ondelete='RESTRICT'),
    sa.ForeignKeyConstraint(['revoked_by'], ['users.id'], name=op.f('fk_conflict_exceptions_revoked_by_users'), ondelete='RESTRICT'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_conflict_exceptions'))
    )
    op.create_index('ix_conflict_exceptions_establishment', 'conflict_exceptions', ['establishment_id'], unique=False)
    op.create_table('coupon_redemptions',
    sa.Column('id', sa.UUID(), nullable=False),
    sa.Column('coupon_id', sa.UUID(), nullable=False),
    sa.Column('org_id', sa.UUID(), nullable=False),
    sa.Column('order_id', sa.UUID(), nullable=False),
    sa.Column('discount_cents', sa.BigInteger(), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('released_at', sa.DateTime(timezone=True), nullable=True),
    sa.ForeignKeyConstraint(['coupon_id'], ['coupons.id'], name=op.f('fk_coupon_redemptions_coupon_id_coupons'), ondelete='RESTRICT'),
    sa.ForeignKeyConstraint(['order_id'], ['orders.id'], name=op.f('fk_coupon_redemptions_order_id_orders'), ondelete='RESTRICT'),
    sa.ForeignKeyConstraint(['org_id'], ['organizations.id'], name=op.f('fk_coupon_redemptions_org_id_organizations'), ondelete='RESTRICT'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_coupon_redemptions')),
    sa.UniqueConstraint('coupon_id', 'order_id', name='uq_coupon_redemptions_coupon_order')
    )
    op.create_index('ix_coupon_redemptions_coupon_org', 'coupon_redemptions', ['coupon_id', 'org_id'], unique=False)
    op.create_table('cp_rollouts',
    sa.Column('id', sa.UUID(), nullable=False),
    sa.Column('release_id', sa.UUID(), nullable=False),
    sa.Column('target_kind', sa.String(length=10), nullable=False),
    sa.Column('target_ref', sa.UUID(), nullable=True),
    sa.Column('max_concurrency', sa.Integer(), nullable=False),
    sa.Column('failure_policy', sa.String(length=10), nullable=False),
    sa.Column('executor', sa.String(length=20), nullable=False),
    sa.Column('status', sa.String(length=25), nullable=False),
    sa.Column('is_rollback', sa.Boolean(), nullable=False),
    sa.Column('rollback_of_id', sa.UUID(), nullable=True),
    sa.Column('reason', sa.String(length=500), nullable=True),
    sa.Column('created_by', sa.UUID(), nullable=True),
    sa.Column('started_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('finished_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.CheckConstraint("executor IN ('NULO','SIMULACAO_LOCAL')", name=op.f('ck_cp_rollouts_executor_valid')),
    sa.CheckConstraint("failure_policy IN ('PARAR','CONTINUAR')", name=op.f('ck_cp_rollouts_failure_policy_valid')),
    sa.CheckConstraint("status IN ('PENDENTE','EM_ANDAMENTO','PAUSADO','CONCLUIDO','SIMULADO','BLOQUEADO_DEPENDENCIA','FALHOU','CANCELADO')", name=op.f('ck_cp_rollouts_status_valid')),
    sa.CheckConstraint("target_kind IN ('UMA','GRUPO','TODAS')", name=op.f('ck_cp_rollouts_target_kind_valid')),
    sa.CheckConstraint('max_concurrency BETWEEN 1 AND 20', name=op.f('ck_cp_rollouts_max_concurrency_range')),
    sa.ForeignKeyConstraint(['release_id'], ['cp_releases.id'], name=op.f('fk_cp_rollouts_release_id_cp_releases'), ondelete='RESTRICT'),
    sa.ForeignKeyConstraint(['rollback_of_id'], ['cp_rollouts.id'], name=op.f('fk_cp_rollouts_rollback_of_id_cp_rollouts'), ondelete='RESTRICT'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_cp_rollouts'))
    )
    op.create_index('ix_cp_rollouts_status', 'cp_rollouts', ['status'], unique=False)
    op.create_table('design_requests',
    sa.Column('code', sa.String(length=12), nullable=False),
    sa.Column('org_id', sa.UUID(), nullable=False),
    sa.Column('establishment_id', sa.UUID(), nullable=True),
    sa.Column('kind', sa.String(length=20), nullable=False),
    sa.Column('title', sa.String(length=200), nullable=False),
    sa.Column('briefing', sa.Text(), nullable=False),
    sa.Column('texts', postgresql.JSONB(astext_type=Text()), nullable=True),
    sa.Column('references', postgresql.JSONB(astext_type=Text()), nullable=True),
    sa.Column('orientation', sa.String(length=12), nullable=False),
    sa.Column('media_type', sa.String(length=8), nullable=False),
    sa.Column('duration_s', sa.Integer(), nullable=False),
    sa.Column('status', sa.String(length=25), nullable=False),
    sa.Column('requested_by', sa.UUID(), nullable=False),
    sa.Column('assigned_to', sa.UUID(), nullable=True),
    sa.Column('billing', sa.String(length=12), nullable=False),
    sa.Column('price_cents', sa.BigInteger(), nullable=False),
    sa.Column('payment_status', sa.String(length=15), nullable=False),
    sa.Column('payment_order_id', sa.UUID(), nullable=True),
    sa.Column('revisions_included', sa.Integer(), nullable=False),
    sa.Column('extra_revisions', sa.Integer(), nullable=False),
    sa.Column('revisions_used', sa.Integer(), nullable=False),
    sa.Column('approved_version_id', sa.UUID(), nullable=True),
    sa.Column('approved_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('published_media_id', sa.UUID(), nullable=True),
    sa.Column('published_version_id', sa.UUID(), nullable=True),
    sa.Column('published_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('due_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('cancel_reason', sa.String(length=500), nullable=True),
    sa.Column('version', sa.Integer(), nullable=False),
    sa.Column('id', sa.UUID(), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.CheckConstraint("billing IN ('A_DEFINIR','COBRAR','NEGOCIADO','ISENTO')", name=op.f('ck_design_requests_billing_valid')),
    sa.CheckConstraint("kind IN ('ANUNCIO_EXTERNO','CONTEUDO_INTERNO')", name=op.f('ck_design_requests_kind_valid')),
    sa.CheckConstraint("media_type IN ('IMAGEM','VIDEO')", name=op.f('ck_design_requests_media_type_valid')),
    sa.CheckConstraint("orientation IN ('HORIZONTAL','VERTICAL')", name=op.f('ck_design_requests_orientation_valid')),
    sa.CheckConstraint("payment_status IN ('PENDENTE','PAGO','NAO_APLICAVEL')", name=op.f('ck_design_requests_payment_status_valid')),
    sa.CheckConstraint("status IN ('BRIEFING','AGUARDANDO_PAGAMENTO','NA_FILA','EM_DESIGN','EM_REVISAO','ALTERACAO_SOLICITADA','APROVADA','PUBLICADA','CANCELADA')", name=op.f('ck_design_requests_status_valid')),
    sa.CheckConstraint('price_cents >= 0', name=op.f('ck_design_requests_price_non_negative')),
    sa.CheckConstraint('revisions_used <= revisions_included + extra_revisions', name=op.f('ck_design_requests_revisions_within_limit')),
    sa.CheckConstraint('revisions_used >= 0 AND revisions_included >= 0 AND extra_revisions >= 0', name=op.f('ck_design_requests_revisions_non_negative')),
    sa.ForeignKeyConstraint(['assigned_to'], ['users.id'], name=op.f('fk_design_requests_assigned_to_users'), ondelete='SET NULL'),
    sa.ForeignKeyConstraint(['establishment_id'], ['establishments.id'], name=op.f('fk_design_requests_establishment_id_establishments'), ondelete='SET NULL'),
    sa.ForeignKeyConstraint(['org_id'], ['organizations.id'], name=op.f('fk_design_requests_org_id_organizations'), ondelete='RESTRICT'),
    sa.ForeignKeyConstraint(['requested_by'], ['users.id'], name=op.f('fk_design_requests_requested_by_users'), ondelete='RESTRICT'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_design_requests')),
    sa.UniqueConstraint('code', name='uq_design_requests_code')
    )
    op.create_index('ix_design_requests_assigned_to', 'design_requests', ['assigned_to'], unique=False)
    op.create_index('ix_design_requests_establishment_id', 'design_requests', ['establishment_id'], unique=False)
    op.create_index('ix_design_requests_org_id', 'design_requests', ['org_id'], unique=False)
    op.create_index('ix_design_requests_status', 'design_requests', ['status'], unique=False)
    op.create_table('establishment_commission_settings',
    sa.Column('id', sa.UUID(), nullable=False),
    sa.Column('establishment_id', sa.UUID(), nullable=False),
    sa.Column('receives_commission', sa.Boolean(), nullable=True),
    sa.Column('rate_bps', sa.Integer(), nullable=True),
    sa.Column('weight_milli', sa.Integer(), nullable=True),
    sa.Column('auto_payout', sa.Boolean(), server_default=sa.text('true'), nullable=False),
    sa.Column('valid_from', sa.DateTime(timezone=True), nullable=False),
    sa.Column('valid_to', sa.DateTime(timezone=True), nullable=True),
    sa.Column('approved_by', sa.UUID(), nullable=True),
    sa.Column('approval_request_id', sa.UUID(), nullable=True),
    sa.Column('reason', sa.Text(), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.CheckConstraint('rate_bps IS NULL OR rate_bps BETWEEN 0 AND 10000', name=op.f('ck_establishment_commission_settings_rate_range')),
    sa.CheckConstraint('valid_to IS NULL OR valid_to > valid_from', name=op.f('ck_establishment_commission_settings_validity')),
    sa.CheckConstraint('weight_milli IS NULL OR weight_milli BETWEEN 0 AND 100000', name=op.f('ck_establishment_commission_settings_weight_range')),
    sa.ForeignKeyConstraint(['approval_request_id'], ['financial_approval_requests.id'], name=op.f('fk_establishment_commission_settings_approval_request_i_63de'), ondelete='RESTRICT'),
    sa.ForeignKeyConstraint(['approved_by'], ['users.id'], name=op.f('fk_establishment_commission_settings_approved_by_users'), ondelete='RESTRICT'),
    sa.ForeignKeyConstraint(['establishment_id'], ['establishments.id'], name=op.f('fk_establishment_commission_settings_establishment_id_e_1171'), ondelete='RESTRICT'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_establishment_commission_settings'))
    )
    op.create_index('ix_establishment_commission_settings_est', 'establishment_commission_settings', ['establishment_id', 'valid_from'], unique=False)
    op.create_table('establishment_pix_keys',
    sa.Column('id', sa.UUID(), nullable=False),
    sa.Column('establishment_id', sa.UUID(), nullable=False),
    sa.Column('key_type', sa.String(length=10), nullable=False),
    sa.Column('key_value_enc', sa.Text(), nullable=False),
    sa.Column('key_fingerprint', sa.String(length=64), nullable=False),
    sa.Column('key_masked', sa.String(length=80), nullable=False),
    sa.Column('holder_name', sa.String(length=200), nullable=False),
    sa.Column('holder_document_enc', sa.Text(), nullable=False),
    sa.Column('holder_document_masked', sa.String(length=30), nullable=False),
    sa.Column('active_from', sa.DateTime(timezone=True), nullable=False),
    sa.Column('created_by', sa.UUID(), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('revoked_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('revoked_by', sa.UUID(), nullable=True),
    sa.Column('revoke_reason', sa.String(length=300), nullable=True),
    sa.CheckConstraint("key_type IN ('cpf','cnpj','email','phone','random')", name=op.f('ck_establishment_pix_keys_key_type_valid')),
    sa.ForeignKeyConstraint(['created_by'], ['users.id'], name=op.f('fk_establishment_pix_keys_created_by_users'), ondelete='RESTRICT'),
    sa.ForeignKeyConstraint(['establishment_id'], ['establishments.id'], name=op.f('fk_establishment_pix_keys_establishment_id_establishments'), ondelete='RESTRICT'),
    sa.ForeignKeyConstraint(['revoked_by'], ['users.id'], name=op.f('fk_establishment_pix_keys_revoked_by_users'), ondelete='RESTRICT'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_establishment_pix_keys'))
    )
    op.create_index('uq_establishment_pix_keys_current', 'establishment_pix_keys', ['establishment_id'], unique=True, postgresql_where=sa.text('revoked_at IS NULL'))
    op.create_table('establishment_segments',
    sa.Column('establishment_id', sa.UUID(), nullable=False),
    sa.Column('segment_id', sa.UUID(), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.ForeignKeyConstraint(['establishment_id'], ['establishments.id'], name=op.f('fk_establishment_segments_establishment_id_establishments'), ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['segment_id'], ['segments.id'], name=op.f('fk_establishment_segments_segment_id_segments'), ondelete='RESTRICT'),
    sa.PrimaryKeyConstraint('establishment_id', 'segment_id', name=op.f('pk_establishment_segments'))
    )
    op.create_index('ix_establishment_segments_segment_id', 'establishment_segments', ['segment_id'], unique=False)
    op.create_table('fiscal_document_requests',
    sa.Column('id', sa.UUID(), nullable=False),
    sa.Column('payment_order_id', sa.UUID(), nullable=False),
    sa.Column('org_id', sa.UUID(), nullable=False),
    sa.Column('status', sa.String(length=30), nullable=False),
    sa.Column('provider', sa.String(length=40), nullable=True),
    sa.Column('provider_ref', sa.String(length=120), nullable=True),
    sa.Column('detail', sa.String(length=300), nullable=True),
    sa.Column('requested_by', sa.UUID(), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.CheckConstraint("status IN ('REQUESTED','BLOCKED_EXTERNAL_DEPENDENCY','ISSUED','FAILED')", name=op.f('ck_fiscal_document_requests_status_valid')),
    sa.ForeignKeyConstraint(['payment_order_id'], ['payment_orders.id'], name=op.f('fk_fiscal_document_requests_payment_order_id_payment_orders'), ondelete='RESTRICT'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_fiscal_document_requests')),
    sa.UniqueConstraint('payment_order_id', name='uq_fiscal_document_requests_order')
    )
    op.create_table('lead_activities',
    sa.Column('id', sa.UUID(), nullable=False),
    sa.Column('lead_id', sa.UUID(), nullable=False),
    sa.Column('kind', sa.String(length=20), nullable=False),
    sa.Column('description', sa.Text(), nullable=False),
    sa.Column('occurred_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('actor_id', sa.UUID(), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.CheckConstraint("kind IN ('NOTA','LIGACAO','REUNIAO','EMAIL','WHATSAPP','VISITA')", name=op.f('ck_lead_activities_kind_valid')),
    sa.ForeignKeyConstraint(['actor_id'], ['users.id'], name=op.f('fk_lead_activities_actor_id_users'), ondelete='SET NULL'),
    sa.ForeignKeyConstraint(['lead_id'], ['leads.id'], name=op.f('fk_lead_activities_lead_id_leads'), ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_lead_activities'))
    )
    op.create_index('ix_lead_activities_lead_id', 'lead_activities', ['lead_id'], unique=False)
    op.create_table('lead_stage_events',
    sa.Column('id', sa.UUID(), nullable=False),
    sa.Column('lead_id', sa.UUID(), nullable=False),
    sa.Column('from_stage', sa.String(length=20), nullable=True),
    sa.Column('to_stage', sa.String(length=20), nullable=False),
    sa.Column('reason', sa.String(length=500), nullable=True),
    sa.Column('actor_id', sa.UUID(), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.ForeignKeyConstraint(['actor_id'], ['users.id'], name=op.f('fk_lead_stage_events_actor_id_users'), ondelete='SET NULL'),
    sa.ForeignKeyConstraint(['lead_id'], ['leads.id'], name=op.f('fk_lead_stage_events_lead_id_leads'), ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_lead_stage_events'))
    )
    op.create_index('ix_lead_stage_events_lead_id', 'lead_stage_events', ['lead_id'], unique=False)
    op.create_table('ledger_postings',
    sa.Column('id', sa.UUID(), nullable=False),
    sa.Column('transaction_id', sa.UUID(), nullable=False),
    sa.Column('account_id', sa.UUID(), nullable=False),
    sa.Column('amount_cents', sa.BigInteger(), nullable=False),
    sa.Column('competence_date', sa.Date(), nullable=False),
    sa.Column('memo', sa.String(length=300), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.CheckConstraint('amount_cents <> 0', name=op.f('ck_ledger_postings_amount_nonzero')),
    sa.ForeignKeyConstraint(['account_id'], ['ledger_accounts.id'], name=op.f('fk_ledger_postings_account_id_ledger_accounts'), ondelete='RESTRICT'),
    sa.ForeignKeyConstraint(['transaction_id'], ['ledger_transactions.id'], name=op.f('fk_ledger_postings_transaction_id_ledger_transactions'), ondelete='RESTRICT'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_ledger_postings'))
    )
    op.create_index('ix_ledger_postings_account_competence', 'ledger_postings', ['account_id', 'competence_date'], unique=False)
    op.create_index('ix_ledger_postings_transaction_id', 'ledger_postings', ['transaction_id'], unique=False)
    op.create_table('notification_deliveries',
    sa.Column('id', sa.UUID(), nullable=False),
    sa.Column('notification_id', sa.UUID(), nullable=False),
    sa.Column('user_id', sa.UUID(), nullable=False),
    sa.Column('channel', sa.String(length=20), nullable=False),
    sa.Column('status', sa.String(length=20), nullable=False),
    sa.Column('subject', sa.String(length=255), nullable=True),
    sa.Column('content', sa.Text(), nullable=True),
    sa.Column('detail', sa.String(length=500), nullable=True),
    sa.Column('attempts', sa.Integer(), server_default=sa.text('0'), nullable=False),
    sa.Column('next_attempt_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('claimed_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('sent_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.CheckConstraint("channel IN ('IN_APP','EMAIL','PUSH')", name=op.f('ck_notification_deliveries_channel_valid')),
    sa.CheckConstraint("status IN ('PENDING','SENDING','SENT','FAILED','UNAVAILABLE','SKIPPED')", name=op.f('ck_notification_deliveries_status_valid')),
    sa.ForeignKeyConstraint(['notification_id'], ['notifications.id'], name=op.f('fk_notification_deliveries_notification_id_notifications'), ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['user_id'], ['users.id'], name=op.f('fk_notification_deliveries_user_id_users'), ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_notification_deliveries')),
    sa.UniqueConstraint('notification_id', 'channel', name='uq_notification_deliveries_notification_channel')
    )
    op.create_index('ix_notification_deliveries_pending', 'notification_deliveries', ['next_attempt_at'], unique=False, postgresql_where=sa.text("status IN ('PENDING','SENDING')"))
    op.create_index('ix_notification_deliveries_user', 'notification_deliveries', ['user_id'], unique=False)
    op.create_table('onboarding_applications',
    sa.Column('id', sa.UUID(), nullable=False),
    sa.Column('origin', sa.String(length=12), nullable=False),
    sa.Column('status', sa.String(length=12), nullable=False),
    sa.Column('email', sa.String(length=255), nullable=False),
    sa.Column('token_hash', sa.String(length=64), nullable=True),
    sa.Column('token_expires_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('invited_by', sa.UUID(), nullable=True),
    sa.Column('data', postgresql.JSONB(astext_type=Text()), nullable=False),
    sa.Column('completed_steps', postgresql.ARRAY(String(length=20)), server_default=sa.text("'{}'::varchar[]"), nullable=False),
    sa.Column('submitted_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('decided_by', sa.UUID(), nullable=True),
    sa.Column('decided_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('decision_reason', sa.Text(), nullable=True),
    sa.Column('org_id', sa.UUID(), nullable=True),
    sa.Column('establishment_id', sa.UUID(), nullable=True),
    sa.Column('partner_plan_id', sa.UUID(), nullable=True),
    sa.Column('technician_user_id', sa.UUID(), nullable=True),
    sa.Column('installation_date', sa.Date(), nullable=True),
    sa.Column('created_by', sa.UUID(), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.CheckConstraint("(status = 'APPROVED') = (establishment_id IS NOT NULL AND org_id IS NOT NULL)", name=op.f('ck_onboarding_applications_approved_has_entities')),
    sa.CheckConstraint("origin = 'ADMIN' OR token_hash IS NOT NULL", name=op.f('ck_onboarding_applications_token_for_public')),
    sa.CheckConstraint("origin IN ('ADMIN','INVITE','SELF_SIGNUP')", name=op.f('ck_onboarding_applications_origin_valid')),
    sa.CheckConstraint("status IN ('INVITED','IN_PROGRESS','SUBMITTED','APPROVED','REJECTED','CANCELLED')", name=op.f('ck_onboarding_applications_status_valid')),
    sa.ForeignKeyConstraint(['created_by'], ['users.id'], name=op.f('fk_onboarding_applications_created_by_users'), ondelete='SET NULL'),
    sa.ForeignKeyConstraint(['decided_by'], ['users.id'], name=op.f('fk_onboarding_applications_decided_by_users'), ondelete='SET NULL'),
    sa.ForeignKeyConstraint(['establishment_id'], ['establishments.id'], name=op.f('fk_onboarding_applications_establishment_id_establishments'), ondelete='RESTRICT'),
    sa.ForeignKeyConstraint(['invited_by'], ['users.id'], name=op.f('fk_onboarding_applications_invited_by_users'), ondelete='SET NULL'),
    sa.ForeignKeyConstraint(['org_id'], ['organizations.id'], name=op.f('fk_onboarding_applications_org_id_organizations'), ondelete='RESTRICT'),
    sa.ForeignKeyConstraint(['partner_plan_id'], ['partner_plans.id'], name=op.f('fk_onboarding_applications_partner_plan_id_partner_plans'), ondelete='RESTRICT'),
    sa.ForeignKeyConstraint(['technician_user_id'], ['users.id'], name=op.f('fk_onboarding_applications_technician_user_id_users'), ondelete='SET NULL'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_onboarding_applications'))
    )
    op.create_index('ix_onboarding_applications_email', 'onboarding_applications', ['email'], unique=False)
    op.create_index('ix_onboarding_applications_status', 'onboarding_applications', ['status'], unique=False)
    op.create_index('uq_onboarding_applications_token_hash', 'onboarding_applications', ['token_hash'], unique=True, postgresql_where=sa.text('token_hash IS NOT NULL'))
    op.create_table('payables',
    sa.Column('id', sa.UUID(), nullable=False),
    sa.Column('description', sa.String(length=300), nullable=False),
    sa.Column('category', sa.String(length=60), nullable=False),
    sa.Column('supplier_name', sa.String(length=200), nullable=True),
    sa.Column('supplier_document', sa.String(length=20), nullable=True),
    sa.Column('amount_cents', sa.BigInteger(), nullable=False),
    sa.Column('competence_date', sa.Date(), nullable=False),
    sa.Column('due_date', sa.Date(), nullable=False),
    sa.Column('status', sa.String(length=10), nullable=False),
    sa.Column('approval_request_id', sa.UUID(), nullable=False),
    sa.Column('ledger_transaction_id', sa.UUID(), nullable=True),
    sa.Column('settlement_approval_id', sa.UUID(), nullable=True),
    sa.Column('settled_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.CheckConstraint("status IN ('OPEN','PAID','CANCELLED')", name=op.f('ck_payables_status_valid')),
    sa.CheckConstraint('amount_cents > 0', name=op.f('ck_payables_amount_positive')),
    sa.ForeignKeyConstraint(['approval_request_id'], ['financial_approval_requests.id'], name=op.f('fk_payables_approval_request_id_financial_approval_requests'), ondelete='RESTRICT'),
    sa.ForeignKeyConstraint(['ledger_transaction_id'], ['ledger_transactions.id'], name=op.f('fk_payables_ledger_transaction_id_ledger_transactions'), ondelete='RESTRICT'),
    sa.ForeignKeyConstraint(['settlement_approval_id'], ['financial_approval_requests.id'], name=op.f('fk_payables_settlement_approval_id_financial_approval_requests'), ondelete='RESTRICT'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_payables'))
    )
    op.create_index('ix_payables_status_due', 'payables', ['status', 'due_date'], unique=False)
    op.create_table('payment_order_callbacks',
    sa.Column('id', sa.UUID(), nullable=False),
    sa.Column('payment_order_id', sa.UUID(), nullable=False),
    sa.Column('name', sa.String(length=80), nullable=False),
    sa.Column('status', sa.String(length=10), nullable=False),
    sa.Column('attempts', sa.Integer(), server_default=sa.text('0'), nullable=False),
    sa.Column('last_error', sa.String(length=500), nullable=True),
    sa.Column('done_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.CheckConstraint("status IN ('PENDING','DONE','FAILED','REJECTED')", name=op.f('ck_payment_order_callbacks_status_valid')),
    sa.ForeignKeyConstraint(['payment_order_id'], ['payment_orders.id'], name=op.f('fk_payment_order_callbacks_payment_order_id_payment_orders'), ondelete='RESTRICT'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_payment_order_callbacks')),
    sa.UniqueConstraint('payment_order_id', 'name', name='uq_payment_order_callbacks_order_name')
    )
    op.create_table('products',
    sa.Column('id', sa.UUID(), nullable=False),
    sa.Column('name', sa.String(length=160), nullable=False),
    sa.Column('slug', sa.String(length=180), nullable=False),
    sa.Column('segment_id', sa.UUID(), nullable=False),
    sa.Column('brand_id', sa.UUID(), nullable=True),
    sa.Column('is_active', sa.Boolean(), server_default=sa.text('true'), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.ForeignKeyConstraint(['brand_id'], ['brands.id'], name=op.f('fk_products_brand_id_brands'), ondelete='RESTRICT'),
    sa.ForeignKeyConstraint(['segment_id'], ['segments.id'], name=op.f('fk_products_segment_id_segments'), ondelete='RESTRICT'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_products')),
    sa.UniqueConstraint('slug', name=op.f('uq_products_slug'))
    )
    op.create_index('ix_products_brand_id', 'products', ['brand_id'], unique=False)
    op.create_index('ix_products_segment_id', 'products', ['segment_id'], unique=False)
    op.create_table('reconciliation_items',
    sa.Column('id', sa.UUID(), nullable=False),
    sa.Column('run_id', sa.UUID(), nullable=True),
    sa.Column('kind', sa.String(length=40), nullable=False),
    sa.Column('dedupe_key', sa.String(length=200), nullable=False),
    sa.Column('internal_ref', sa.String(length=120), nullable=True),
    sa.Column('external_ref', sa.String(length=120), nullable=True),
    sa.Column('internal_amount_cents', sa.BigInteger(), nullable=True),
    sa.Column('external_amount_cents', sa.BigInteger(), nullable=True),
    sa.Column('detail', postgresql.JSONB(astext_type=Text()), nullable=False),
    sa.Column('status', sa.String(length=10), nullable=False),
    sa.Column('resolution_approval_id', sa.UUID(), nullable=True),
    sa.Column('resolution_transaction_id', sa.UUID(), nullable=True),
    sa.Column('resolved_by', sa.UUID(), nullable=True),
    sa.Column('resolved_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.CheckConstraint("status IN ('DIVERGENT','RESOLVED')", name=op.f('ck_reconciliation_items_status_valid')),
    sa.ForeignKeyConstraint(['resolution_approval_id'], ['financial_approval_requests.id'], name=op.f('fk_reconciliation_items_resolution_approval_id_financia_8817'), ondelete='RESTRICT'),
    sa.ForeignKeyConstraint(['resolution_transaction_id'], ['ledger_transactions.id'], name=op.f('fk_reconciliation_items_resolution_transaction_id_ledge_7a74'), ondelete='RESTRICT'),
    sa.ForeignKeyConstraint(['run_id'], ['reconciliation_runs.id'], name=op.f('fk_reconciliation_items_run_id_reconciliation_runs'), ondelete='RESTRICT'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_reconciliation_items'))
    )
    op.create_index('ix_reconciliation_items_status', 'reconciliation_items', ['status'], unique=False)
    op.create_index('uq_reconciliation_items_open_key', 'reconciliation_items', ['kind', 'dedupe_key'], unique=True, postgresql_where=sa.text("status = 'DIVERGENT'"))
    op.create_table('revenue_schedules',
    sa.Column('id', sa.UUID(), nullable=False),
    sa.Column('payment_order_id', sa.UUID(), nullable=False),
    sa.Column('campaign_id', sa.UUID(), nullable=False),
    sa.Column('org_id', sa.UUID(), nullable=False),
    sa.Column('eligible_cents', sa.BigInteger(), nullable=False),
    sa.Column('refunded_cents', sa.BigInteger(), server_default=sa.text('0'), nullable=False),
    sa.Column('start_date', sa.Date(), nullable=False),
    sa.Column('end_date', sa.Date(), nullable=False),
    sa.Column('status', sa.String(length=12), nullable=False),
    sa.Column('cancelled_on', sa.Date(), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.CheckConstraint("status IN ('ACTIVE','CANCELLED','COMPLETED')", name=op.f('ck_revenue_schedules_status_valid')),
    sa.CheckConstraint('eligible_cents >= 0', name=op.f('ck_revenue_schedules_eligible_nonneg')),
    sa.CheckConstraint('end_date >= start_date', name=op.f('ck_revenue_schedules_dates_valid')),
    sa.CheckConstraint('refunded_cents >= 0 AND refunded_cents <= eligible_cents', name=op.f('ck_revenue_schedules_refunded_range')),
    sa.ForeignKeyConstraint(['payment_order_id'], ['payment_orders.id'], name=op.f('fk_revenue_schedules_payment_order_id_payment_orders'), ondelete='RESTRICT'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_revenue_schedules')),
    sa.UniqueConstraint('payment_order_id', 'campaign_id', name='uq_revenue_schedules_order_campaign')
    )
    op.create_index('ix_revenue_schedules_campaign', 'revenue_schedules', ['campaign_id'], unique=False)
    op.create_index('ix_revenue_schedules_status', 'revenue_schedules', ['status'], unique=False)
    op.create_table('template_renders',
    sa.Column('id', sa.UUID(), nullable=False),
    sa.Column('template_id', sa.UUID(), nullable=False),
    sa.Column('template_version', sa.Integer(), nullable=False),
    sa.Column('org_id', sa.UUID(), nullable=True),
    sa.Column('format', sa.String(length=5), nullable=False),
    sa.Column('variables', postgresql.JSONB(astext_type=Text()), nullable=True),
    sa.Column('storage_key', sa.String(length=512), nullable=False),
    sa.Column('sha256', sa.String(length=64), nullable=False),
    sa.Column('size_bytes', sa.BigInteger(), nullable=False),
    sa.Column('width', sa.Integer(), nullable=False),
    sa.Column('height', sa.Integer(), nullable=False),
    sa.Column('media_id', sa.UUID(), nullable=True),
    sa.Column('created_by', sa.UUID(), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.CheckConstraint("format IN ('PNG','JPEG')", name=op.f('ck_template_renders_format_valid')),
    sa.ForeignKeyConstraint(['created_by'], ['users.id'], name=op.f('fk_template_renders_created_by_users'), ondelete='SET NULL'),
    sa.ForeignKeyConstraint(['org_id'], ['organizations.id'], name=op.f('fk_template_renders_org_id_organizations'), ondelete='RESTRICT'),
    sa.ForeignKeyConstraint(['template_id'], ['design_templates.id'], name=op.f('fk_template_renders_template_id_design_templates'), ondelete='RESTRICT'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_template_renders'))
    )
    op.create_index('ix_template_renders_template_id', 'template_renders', ['template_id'], unique=False)
    op.create_table('webhook_events',
    sa.Column('id', sa.UUID(), nullable=False),
    sa.Column('provider', sa.String(length=30), nullable=False),
    sa.Column('instance_slug', sa.String(length=64), nullable=False),
    sa.Column('webhook_event_id', sa.BigInteger(), nullable=False),
    sa.Column('raw_body', sa.Text(), nullable=False),
    sa.Column('headers', postgresql.JSONB(astext_type=Text()), nullable=False),
    sa.Column('signature_valid', sa.Boolean(), nullable=False),
    sa.Column('external_ref', sa.String(length=80), nullable=True),
    sa.Column('order_ref', sa.String(length=64), nullable=True),
    sa.Column('provider_status', sa.String(length=30), nullable=True),
    sa.Column('subconta_id', sa.BigInteger(), nullable=True),
    sa.Column('received_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('status', sa.String(length=12), nullable=False),
    sa.Column('attempts', sa.Integer(), server_default=sa.text('0'), nullable=False),
    sa.Column('last_error', sa.String(length=500), nullable=True),
    sa.Column('payment_order_id', sa.UUID(), nullable=True),
    sa.Column('processed_at', sa.DateTime(timezone=True), nullable=True),
    sa.CheckConstraint("status IN ('RECEIVED','PROCESSING','PROCESSED','IGNORED','DIVERGENT','REJECTED','FAILED')", name=op.f('ck_webhook_events_status_valid')),
    sa.ForeignKeyConstraint(['payment_order_id'], ['payment_orders.id'], name=op.f('fk_webhook_events_payment_order_id_payment_orders'), ondelete='RESTRICT'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_webhook_events')),
    sa.UniqueConstraint('provider', 'instance_slug', 'webhook_event_id', name='uq_webhook_events_provider_event')
    )
    op.create_index('ix_webhook_events_external_ref', 'webhook_events', ['external_ref'], unique=False)
    op.create_index('ix_webhook_events_status', 'webhook_events', ['status'], unique=False)
    op.create_table('audience_estimates',
    sa.Column('id', sa.UUID(), nullable=False),
    sa.Column('subject_type', sa.String(length=15), nullable=False),
    sa.Column('screen_id', sa.UUID(), nullable=True),
    sa.Column('establishment_id', sa.UUID(), nullable=False),
    sa.Column('daily_people', sa.Integer(), nullable=False),
    sa.Column('method', sa.String(length=15), nullable=False),
    sa.Column('basis', postgresql.JSONB(astext_type=Text()), nullable=True),
    sa.Column('note', sa.Text(), nullable=True),
    sa.Column('created_by', sa.UUID(), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('clock_timestamp()'), nullable=False),
    sa.CheckConstraint("(subject_type = 'SCREEN') = (screen_id IS NOT NULL)", name=op.f('ck_audience_estimates_subject_consistent')),
    sa.CheckConstraint("method IN ('MANUAL','FLOW_ESTIMATE')", name=op.f('ck_audience_estimates_method_valid')),
    sa.CheckConstraint("subject_type IN ('SCREEN','ESTABLISHMENT')", name=op.f('ck_audience_estimates_subject_type_valid')),
    sa.CheckConstraint('daily_people >= 0', name=op.f('ck_audience_estimates_daily_people_nonneg')),
    sa.ForeignKeyConstraint(['created_by'], ['users.id'], name=op.f('fk_audience_estimates_created_by_users'), ondelete='SET NULL'),
    sa.ForeignKeyConstraint(['establishment_id'], ['establishments.id'], name=op.f('fk_audience_estimates_establishment_id_establishments'), ondelete='RESTRICT'),
    sa.ForeignKeyConstraint(['screen_id'], ['screens.id'], name=op.f('fk_audience_estimates_screen_id_screens'), ondelete='RESTRICT'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_audience_estimates'))
    )
    op.create_index('ix_audience_estimates_establishment_id', 'audience_estimates', ['establishment_id'], unique=False)
    op.create_index('ix_audience_estimates_screen_id', 'audience_estimates', ['screen_id'], unique=False)
    op.create_table('commission_accruals',
    sa.Column('id', sa.UUID(), nullable=False),
    sa.Column('campaign_id', sa.UUID(), nullable=False),
    sa.Column('screen_id', sa.UUID(), nullable=False),
    sa.Column('establishment_id', sa.UUID(), nullable=False),
    sa.Column('local_day', sa.Date(), nullable=False),
    sa.Column('competence', sa.Date(), nullable=False),
    sa.Column('valid_count', sa.Integer(), nullable=False),
    sa.Column('expected_count', sa.Integer(), nullable=False),
    sa.Column('amount_ucents', sa.BigInteger(), nullable=False),
    sa.Column('reversed_ucents', sa.BigInteger(), server_default=sa.text('0'), nullable=False),
    sa.Column('closed_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.CheckConstraint('extract(day FROM competence) = 1', name=op.f('ck_commission_accruals_competence_day_1')),
    sa.CheckConstraint('valid_count >= 0 AND expected_count >= 0 AND amount_ucents >= 0', name=op.f('ck_commission_accruals_nonneg')),
    sa.ForeignKeyConstraint(['campaign_id'], ['commission_campaigns.campaign_id'], name=op.f('fk_commission_accruals_campaign_id_commission_campaigns'), ondelete='RESTRICT'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_commission_accruals')),
    sa.UniqueConstraint('campaign_id', 'screen_id', 'local_day', 'competence', name='uq_commission_accruals_key')
    )
    op.create_index('ix_commission_accruals_competence', 'commission_accruals', ['competence'], unique=False)
    op.create_index('ix_commission_accruals_est_comp', 'commission_accruals', ['establishment_id', 'competence'], unique=False)
    op.create_table('commission_campaign_screens',
    sa.Column('campaign_id', sa.UUID(), nullable=False),
    sa.Column('screen_id', sa.UUID(), nullable=False),
    sa.Column('establishment_id', sa.UUID(), nullable=False),
    sa.Column('establishment_org_id', sa.UUID(), nullable=True),
    sa.Column('receives_commission', sa.Boolean(), nullable=False),
    sa.Column('weight_milli', sa.Integer(), nullable=False),
    sa.Column('rate_bps', sa.Integer(), nullable=False),
    sa.Column('attributed_revenue_cents', sa.BigInteger(), nullable=False),
    sa.Column('expected_total', sa.BigInteger(), nullable=False),
    sa.Column('share_ucents', sa.BigInteger(), nullable=False),
    sa.Column('plan', postgresql.JSONB(astext_type=Text()), nullable=False),
    sa.CheckConstraint('expected_total >= 0 AND share_ucents >= 0', name=op.f('ck_commission_campaign_screens_nonneg')),
    sa.CheckConstraint('rate_bps BETWEEN 0 AND 10000', name=op.f('ck_commission_campaign_screens_rate_range')),
    sa.CheckConstraint('weight_milli >= 0', name=op.f('ck_commission_campaign_screens_weight_nonneg')),
    sa.ForeignKeyConstraint(['campaign_id'], ['commission_campaigns.campaign_id'], name=op.f('fk_commission_campaign_screens_campaign_id_commission_campaigns'), ondelete='RESTRICT'),
    sa.PrimaryKeyConstraint('campaign_id', 'screen_id', name=op.f('pk_commission_campaign_screens'))
    )
    op.create_index('ix_commission_campaign_screens_est', 'commission_campaign_screens', ['establishment_id'], unique=False)
    op.create_table('commission_closings',
    sa.Column('competence', sa.Date(), nullable=False),
    sa.Column('establishment_id', sa.UUID(), nullable=False),
    sa.Column('accrued_ucents', sa.BigInteger(), nullable=False),
    sa.Column('amount_cents', sa.BigInteger(), nullable=False),
    sa.Column('residue_ucents', sa.BigInteger(), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.CheckConstraint('accrued_ucents >= 0 AND amount_cents >= 0 AND residue_ucents >= 0', name=op.f('ck_commission_closings_nonneg')),
    sa.ForeignKeyConstraint(['competence'], ['commission_competence_runs.competence'], name=op.f('fk_commission_closings_competence_commission_competence_runs'), ondelete='RESTRICT'),
    sa.PrimaryKeyConstraint('competence', 'establishment_id', name=op.f('pk_commission_closings'))
    )
    op.create_table('commission_pool_releases',
    sa.Column('campaign_id', sa.UUID(), nullable=False),
    sa.Column('competence', sa.Date(), nullable=False),
    sa.Column('released_cents', sa.BigInteger(), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.ForeignKeyConstraint(['campaign_id'], ['commission_campaigns.campaign_id'], name=op.f('fk_commission_pool_releases_campaign_id_commission_campaigns'), ondelete='RESTRICT'),
    sa.PrimaryKeyConstraint('campaign_id', 'competence', name=op.f('pk_commission_pool_releases'))
    )
    op.create_table('comodato_agreements',
    sa.Column('id', sa.UUID(), nullable=False),
    sa.Column('org_id', sa.UUID(), nullable=False),
    sa.Column('establishment_id', sa.UUID(), nullable=False),
    sa.Column('screen_id', sa.UUID(), nullable=True),
    sa.Column('partner_plan_id', sa.UUID(), nullable=True),
    sa.Column('deposit_required', sa.Boolean(), nullable=False),
    sa.Column('deposit_cents', sa.Integer(), nullable=False),
    sa.Column('installation_fee_required', sa.Boolean(), nullable=False),
    sa.Column('installation_fee_cents', sa.Integer(), nullable=False),
    sa.Column('upgrade_policy', sa.String(length=12), nullable=False),
    sa.Column('upgrade_fee_cents', sa.Integer(), nullable=True),
    sa.Column('maintenance_paid_by', sa.String(length=15), nullable=False),
    sa.Column('breakage_paid_by', sa.String(length=15), nullable=False),
    sa.Column('status', sa.String(length=10), nullable=False),
    sa.Column('started_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('ended_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('end_reason', sa.Text(), nullable=True),
    sa.Column('notes', sa.Text(), nullable=True),
    sa.Column('created_by', sa.UUID(), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.CheckConstraint("(status = 'ENDED') = (ended_at IS NOT NULL)", name=op.f('ck_comodato_agreements_ended_consistent')),
    sa.CheckConstraint("breakage_paid_by IN ('RD','ESTABLISHMENT')", name=op.f('ck_comodato_agreements_breakage_paid_by_valid')),
    sa.CheckConstraint("maintenance_paid_by IN ('RD','ESTABLISHMENT')", name=op.f('ck_comodato_agreements_maintenance_paid_by_valid')),
    sa.CheckConstraint("status IN ('ACTIVE','ENDED')", name=op.f('ck_comodato_agreements_status_valid')),
    sa.CheckConstraint("upgrade_policy IN ('FREE','PAID','NEGOTIATED')", name=op.f('ck_comodato_agreements_upgrade_policy_valid')),
    sa.CheckConstraint('deposit_cents >= 0 AND (deposit_required OR deposit_cents = 0)', name=op.f('ck_comodato_agreements_deposit_consistent')),
    sa.CheckConstraint('installation_fee_cents >= 0 AND (installation_fee_required OR installation_fee_cents = 0)', name=op.f('ck_comodato_agreements_installation_fee_consistent')),
    sa.CheckConstraint('upgrade_fee_cents IS NULL OR upgrade_fee_cents >= 0', name=op.f('ck_comodato_agreements_upgrade_fee_nonneg')),
    sa.ForeignKeyConstraint(['created_by'], ['users.id'], name=op.f('fk_comodato_agreements_created_by_users'), ondelete='SET NULL'),
    sa.ForeignKeyConstraint(['establishment_id'], ['establishments.id'], name=op.f('fk_comodato_agreements_establishment_id_establishments'), ondelete='RESTRICT'),
    sa.ForeignKeyConstraint(['org_id'], ['organizations.id'], name=op.f('fk_comodato_agreements_org_id_organizations'), ondelete='RESTRICT'),
    sa.ForeignKeyConstraint(['partner_plan_id'], ['partner_plans.id'], name=op.f('fk_comodato_agreements_partner_plan_id_partner_plans'), ondelete='RESTRICT'),
    sa.ForeignKeyConstraint(['screen_id'], ['screens.id'], name=op.f('fk_comodato_agreements_screen_id_screens'), ondelete='RESTRICT'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_comodato_agreements'))
    )
    op.create_index('ix_comodato_agreements_establishment_id', 'comodato_agreements', ['establishment_id'], unique=False)
    op.create_index('ix_comodato_agreements_org_id', 'comodato_agreements', ['org_id'], unique=False)
    op.create_index('ix_comodato_agreements_screen_id', 'comodato_agreements', ['screen_id'], unique=False)
    op.create_table('cp_rollout_targets',
    sa.Column('id', sa.UUID(), nullable=False),
    sa.Column('rollout_id', sa.UUID(), nullable=False),
    sa.Column('instance_id', sa.UUID(), nullable=False),
    sa.Column('position', sa.Integer(), nullable=False),
    sa.Column('from_version', sa.String(length=40), nullable=True),
    sa.Column('to_version', sa.String(length=40), nullable=False),
    sa.Column('status', sa.String(length=25), nullable=False),
    sa.Column('attempt', sa.Integer(), nullable=False),
    sa.Column('detail', sa.String(length=1000), nullable=True),
    sa.Column('started_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('finished_at', sa.DateTime(timezone=True), nullable=True),
    sa.CheckConstraint("status IN ('NA_FILA','EM_EXECUCAO','CONCLUIDO','SIMULADO','BLOQUEADO_DEPENDENCIA','FALHOU','CANCELADO')", name=op.f('ck_cp_rollout_targets_status_valid')),
    sa.ForeignKeyConstraint(['instance_id'], ['cp_instances.id'], name=op.f('fk_cp_rollout_targets_instance_id_cp_instances'), ondelete='RESTRICT'),
    sa.ForeignKeyConstraint(['rollout_id'], ['cp_rollouts.id'], name=op.f('fk_cp_rollout_targets_rollout_id_cp_rollouts'), ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_cp_rollout_targets')),
    sa.UniqueConstraint('rollout_id', 'instance_id', name='uq_cp_rollout_targets_rollout_instance')
    )
    op.create_index('ix_cp_rollout_targets_rollout_status', 'cp_rollout_targets', ['rollout_id', 'status'], unique=False)
    op.create_index('uq_cp_rollout_targets_instance_running', 'cp_rollout_targets', ['instance_id'], unique=True, postgresql_where=sa.text("status = 'EM_EXECUCAO'"))
    op.create_table('creative_kills',
    sa.Column('id', sa.UUID(), nullable=False),
    sa.Column('creative_id', sa.UUID(), nullable=False),
    sa.Column('screen_id', sa.UUID(), nullable=True),
    sa.Column('reason', sa.Text(), nullable=False),
    sa.Column('issued_by_user_id', sa.UUID(), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('lifted_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('lifted_by_user_id', sa.UUID(), nullable=True),
    sa.ForeignKeyConstraint(['issued_by_user_id'], ['users.id'], name=op.f('fk_creative_kills_issued_by_user_id_users'), ondelete='SET NULL'),
    sa.ForeignKeyConstraint(['lifted_by_user_id'], ['users.id'], name=op.f('fk_creative_kills_lifted_by_user_id_users'), ondelete='SET NULL'),
    sa.ForeignKeyConstraint(['screen_id'], ['screens.id'], name=op.f('fk_creative_kills_screen_id_screens'), ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_creative_kills'))
    )
    op.create_index('ix_creative_kills_creative', 'creative_kills', ['creative_id'], unique=False)
    op.create_index('ix_creative_kills_screen', 'creative_kills', ['screen_id'], unique=False)
    op.create_table('design_attachments',
    sa.Column('id', sa.UUID(), nullable=False),
    sa.Column('request_id', sa.UUID(), nullable=False),
    sa.Column('kind', sa.String(length=12), nullable=False),
    sa.Column('storage_key', sa.String(length=512), nullable=False),
    sa.Column('filename', sa.String(length=255), nullable=False),
    sa.Column('content_type', sa.String(length=100), nullable=False),
    sa.Column('size_bytes', sa.BigInteger(), nullable=False),
    sa.Column('sha256', sa.String(length=64), nullable=False),
    sa.Column('uploaded_by', sa.UUID(), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.CheckConstraint("kind IN ('LOGOTIPO','IMAGEM','VIDEO','REFERENCIA','TEXTO','OUTRO')", name=op.f('ck_design_attachments_kind_valid')),
    sa.ForeignKeyConstraint(['request_id'], ['design_requests.id'], name=op.f('fk_design_attachments_request_id_design_requests'), ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['uploaded_by'], ['users.id'], name=op.f('fk_design_attachments_uploaded_by_users'), ondelete='RESTRICT'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_design_attachments'))
    )
    op.create_index('ix_design_attachments_request_id', 'design_attachments', ['request_id'], unique=False)
    op.create_table('design_events',
    sa.Column('id', sa.UUID(), nullable=False),
    sa.Column('request_id', sa.UUID(), nullable=False),
    sa.Column('actor_id', sa.UUID(), nullable=True),
    sa.Column('from_status', sa.String(length=25), nullable=True),
    sa.Column('to_status', sa.String(length=25), nullable=False),
    sa.Column('note', sa.String(length=1000), nullable=True),
    sa.Column('data', postgresql.JSONB(astext_type=Text()), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.ForeignKeyConstraint(['request_id'], ['design_requests.id'], name=op.f('fk_design_events_request_id_design_requests'), ondelete='RESTRICT'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_design_events'))
    )
    op.create_index('ix_design_events_request_id', 'design_events', ['request_id'], unique=False)
    op.create_table('design_extra_revisions',
    sa.Column('id', sa.UUID(), nullable=False),
    sa.Column('request_id', sa.UUID(), nullable=False),
    sa.Column('status', sa.String(length=25), nullable=False),
    sa.Column('price_cents', sa.BigInteger(), nullable=False),
    sa.Column('requested_by', sa.UUID(), nullable=False),
    sa.Column('reason', sa.String(length=1000), nullable=True),
    sa.Column('decided_by', sa.UUID(), nullable=True),
    sa.Column('decided_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('decision_note', sa.String(length=1000), nullable=True),
    sa.Column('payment_order_id', sa.UUID(), nullable=True),
    sa.Column('released_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.CheckConstraint("status IN ('SOLICITADA','AGUARDANDO_PAGAMENTO','LIBERADA','RECUSADA','CANCELADA')", name=op.f('ck_design_extra_revisions_status_valid')),
    sa.CheckConstraint('price_cents >= 0', name=op.f('ck_design_extra_revisions_price_non_negative')),
    sa.ForeignKeyConstraint(['decided_by'], ['users.id'], name=op.f('fk_design_extra_revisions_decided_by_users'), ondelete='SET NULL'),
    sa.ForeignKeyConstraint(['request_id'], ['design_requests.id'], name=op.f('fk_design_extra_revisions_request_id_design_requests'), ondelete='RESTRICT'),
    sa.ForeignKeyConstraint(['requested_by'], ['users.id'], name=op.f('fk_design_extra_revisions_requested_by_users'), ondelete='RESTRICT'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_design_extra_revisions'))
    )
    op.create_index('ix_design_extra_revisions_request_id', 'design_extra_revisions', ['request_id'], unique=False)
    op.create_index('uq_design_extra_revisions_open', 'design_extra_revisions', ['request_id'], unique=True, postgresql_where=sa.text("status IN ('SOLICITADA','AGUARDANDO_PAGAMENTO')"))
    op.create_table('design_versions',
    sa.Column('id', sa.UUID(), nullable=False),
    sa.Column('request_id', sa.UUID(), nullable=False),
    sa.Column('number', sa.Integer(), nullable=False),
    sa.Column('source', sa.String(length=10), nullable=False),
    sa.Column('template_id', sa.UUID(), nullable=True),
    sa.Column('storage_key', sa.String(length=512), nullable=False),
    sa.Column('filename', sa.String(length=255), nullable=False),
    sa.Column('content_type', sa.String(length=100), nullable=False),
    sa.Column('size_bytes', sa.BigInteger(), nullable=False),
    sa.Column('sha256', sa.String(length=64), nullable=False),
    sa.Column('width', sa.Integer(), nullable=True),
    sa.Column('height', sa.Integer(), nullable=True),
    sa.Column('notes', sa.Text(), nullable=True),
    sa.Column('status', sa.String(length=25), nullable=False),
    sa.Column('locked', sa.Boolean(), server_default=sa.text('false'), nullable=False),
    sa.Column('created_by', sa.UUID(), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.CheckConstraint("NOT locked OR status = 'APROVADA'", name=op.f('ck_design_versions_locked_only_when_approved')),
    sa.CheckConstraint("source IN ('UPLOAD','TEMPLATE')", name=op.f('ck_design_versions_source_valid')),
    sa.CheckConstraint("status IN ('EM_REVISAO','ALTERACAO_SOLICITADA','APROVADA','SUBSTITUIDA')", name=op.f('ck_design_versions_status_valid')),
    sa.ForeignKeyConstraint(['created_by'], ['users.id'], name=op.f('fk_design_versions_created_by_users'), ondelete='RESTRICT'),
    sa.ForeignKeyConstraint(['request_id'], ['design_requests.id'], name=op.f('fk_design_versions_request_id_design_requests'), ondelete='RESTRICT'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_design_versions')),
    sa.UniqueConstraint('request_id', 'number', name='uq_design_versions_request_number')
    )
    op.create_index('ix_design_versions_request_id', 'design_versions', ['request_id'], unique=False)
    op.create_table('establishment_products',
    sa.Column('establishment_id', sa.UUID(), nullable=False),
    sa.Column('product_id', sa.UUID(), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.ForeignKeyConstraint(['establishment_id'], ['establishments.id'], name=op.f('fk_establishment_products_establishment_id_establishments'), ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['product_id'], ['products.id'], name=op.f('fk_establishment_products_product_id_products'), ondelete='RESTRICT'),
    sa.PrimaryKeyConstraint('establishment_id', 'product_id', name=op.f('pk_establishment_products'))
    )
    op.create_index('ix_establishment_products_product_id', 'establishment_products', ['product_id'], unique=False)
    op.create_table('installations',
    sa.Column('id', sa.UUID(), nullable=False),
    sa.Column('establishment_id', sa.UUID(), nullable=False),
    sa.Column('screen_id', sa.UUID(), nullable=True),
    sa.Column('technician_user_id', sa.UUID(), nullable=False),
    sa.Column('status', sa.String(length=12), nullable=False),
    sa.Column('scheduled_date', sa.Date(), nullable=True),
    sa.Column('started_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('completed_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('checklist', postgresql.JSONB(astext_type=Text()), nullable=False),
    sa.Column('tech_lat', sa.Float(), nullable=True),
    sa.Column('tech_lng', sa.Float(), nullable=True),
    sa.Column('tech_accuracy_m', sa.Integer(), nullable=True),
    sa.Column('tech_geo_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('distance_to_establishment_m', sa.Integer(), nullable=True),
    sa.Column('connection_test', postgresql.JSONB(astext_type=Text()), nullable=True),
    sa.Column('connection_test_waiver', sa.Text(), nullable=True),
    sa.Column('signer_name', sa.String(length=255), nullable=True),
    sa.Column('signer_role', sa.String(length=120), nullable=True),
    sa.Column('signer_image_key', sa.String(length=500), nullable=True),
    sa.Column('signer_image_sha256', sa.String(length=64), nullable=True),
    sa.Column('signed_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('notes', sa.Text(), nullable=True),
    sa.Column('created_by', sa.UUID(), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.CheckConstraint("(status = 'COMPLETED') = (completed_at IS NOT NULL)", name=op.f('ck_installations_completed_consistent')),
    sa.CheckConstraint("status IN ('SCHEDULED','IN_PROGRESS','COMPLETED','CANCELLED')", name=op.f('ck_installations_status_valid')),
    sa.CheckConstraint('(tech_lat IS NULL) = (tech_lng IS NULL)', name=op.f('ck_installations_tech_coords_pair')),
    sa.CheckConstraint('tech_lat IS NULL OR (tech_lat >= -90 AND tech_lat <= 90)', name=op.f('ck_installations_tech_lat_range')),
    sa.CheckConstraint('tech_lng IS NULL OR (tech_lng >= -180 AND tech_lng <= 180)', name=op.f('ck_installations_tech_lng_range')),
    sa.ForeignKeyConstraint(['created_by'], ['users.id'], name=op.f('fk_installations_created_by_users'), ondelete='SET NULL'),
    sa.ForeignKeyConstraint(['establishment_id'], ['establishments.id'], name=op.f('fk_installations_establishment_id_establishments'), ondelete='RESTRICT'),
    sa.ForeignKeyConstraint(['screen_id'], ['screens.id'], name=op.f('fk_installations_screen_id_screens'), ondelete='RESTRICT'),
    sa.ForeignKeyConstraint(['technician_user_id'], ['users.id'], name=op.f('fk_installations_technician_user_id_users'), ondelete='RESTRICT'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_installations'))
    )
    op.create_index('ix_installations_establishment_id', 'installations', ['establishment_id'], unique=False)
    op.create_index('ix_installations_screen_id', 'installations', ['screen_id'], unique=False)
    op.create_index('ix_installations_technician_user_id', 'installations', ['technician_user_id'], unique=False)
    op.create_table('onboarding_documents',
    sa.Column('id', sa.UUID(), nullable=False),
    sa.Column('application_id', sa.UUID(), nullable=False),
    sa.Column('kind', sa.String(length=24), nullable=False),
    sa.Column('storage_key', sa.String(length=500), nullable=False),
    sa.Column('sha256', sa.String(length=64), nullable=False),
    sa.Column('content_type', sa.String(length=32), nullable=False),
    sa.Column('size_bytes', sa.BigInteger(), nullable=False),
    sa.Column('uploaded_by', sa.UUID(), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.CheckConstraint("content_type IN ('application/pdf','image/jpeg','image/png')", name=op.f('ck_onboarding_documents_content_type_valid')),
    sa.CheckConstraint("kind IN ('COMPANY_REGISTRATION','ID_DOCUMENT','PROOF_OF_ADDRESS','OTHER')", name=op.f('ck_onboarding_documents_kind_valid')),
    sa.CheckConstraint('size_bytes > 0', name=op.f('ck_onboarding_documents_size_positive')),
    sa.ForeignKeyConstraint(['application_id'], ['onboarding_applications.id'], name=op.f('fk_onboarding_documents_application_id_onboarding_applications'), ondelete='RESTRICT'),
    sa.ForeignKeyConstraint(['uploaded_by'], ['users.id'], name=op.f('fk_onboarding_documents_uploaded_by_users'), ondelete='SET NULL'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_onboarding_documents')),
    sa.UniqueConstraint('storage_key', name=op.f('uq_onboarding_documents_storage_key'))
    )
    op.create_index('ix_onboarding_documents_application_id', 'onboarding_documents', ['application_id'], unique=False)
    op.create_table('playback_daily_stats',
    sa.Column('id', sa.UUID(), nullable=False),
    sa.Column('screen_id', sa.UUID(), nullable=False),
    sa.Column('creative_id', sa.UUID(), nullable=False),
    sa.Column('creative_code', sa.String(length=16), nullable=False),
    sa.Column('campaign_id', sa.UUID(), nullable=True),
    sa.Column('kind', sa.String(length=16), nullable=False),
    sa.Column('local_day', sa.Date(), nullable=False),
    sa.Column('establishment_id', sa.UUID(), nullable=True),
    sa.Column('org_id', sa.UUID(), nullable=True),
    sa.Column('plays_accepted', sa.Integer(), nullable=False),
    sa.Column('plays_valid', sa.Integer(), nullable=False),
    sa.Column('plays_invalid', sa.Integer(), nullable=False),
    sa.Column('plays_rejected', sa.Integer(), nullable=False),
    sa.Column('valid_ms', sa.BigInteger(), nullable=False),
    sa.Column('accepted_ms', sa.BigInteger(), nullable=False),
    sa.Column('first_started_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('last_started_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('last_valid_started_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.ForeignKeyConstraint(['screen_id'], ['screens.id'], name=op.f('fk_playback_daily_stats_screen_id_screens'), ondelete='RESTRICT'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_playback_daily_stats'))
    )
    op.create_index('ix_playback_daily_stats_campaign_day', 'playback_daily_stats', ['campaign_id', 'local_day'], unique=False)
    op.create_index('ix_playback_daily_stats_org_day', 'playback_daily_stats', ['org_id', 'local_day'], unique=False)
    op.create_index('ix_playback_daily_stats_screen_day', 'playback_daily_stats', ['screen_id', 'local_day'], unique=False)
    op.create_index('uq_playback_daily_stats_key', 'playback_daily_stats', ['screen_id', 'creative_id', sa.text("COALESCE(campaign_id, '00000000-0000-0000-0000-000000000000'::uuid)"), 'local_day', 'kind'], unique=True)
    op.create_table('sales_attributions',
    sa.Column('id', sa.UUID(), nullable=False),
    sa.Column('proposal_id', sa.UUID(), nullable=False),
    sa.Column('order_id', sa.UUID(), nullable=False),
    sa.Column('seller_id', sa.UUID(), nullable=False),
    sa.Column('sales_rep_id', sa.UUID(), nullable=True),
    sa.Column('org_id', sa.UUID(), nullable=False),
    sa.Column('amount_cents', sa.BigInteger(), nullable=False),
    sa.Column('commission_bps', sa.Integer(), nullable=False),
    sa.Column('commission_cents', sa.BigInteger(), nullable=False),
    sa.Column('status', sa.String(length=20), server_default=sa.text("'PENDENTE'"), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.CheckConstraint("status IN ('PENDENTE','APURADA','PAGA','CANCELADA')", name=op.f('ck_sales_attributions_status_valid')),
    sa.CheckConstraint('amount_cents >= 0 AND commission_cents >= 0', name=op.f('ck_sales_attributions_amounts_non_negative')),
    sa.CheckConstraint('commission_bps >= 0 AND commission_bps <= 10000', name=op.f('ck_sales_attributions_commission_bps_range')),
    sa.ForeignKeyConstraint(['org_id'], ['organizations.id'], name=op.f('fk_sales_attributions_org_id_organizations'), ondelete='RESTRICT'),
    sa.ForeignKeyConstraint(['proposal_id'], ['proposals.id'], name=op.f('fk_sales_attributions_proposal_id_proposals'), ondelete='RESTRICT'),
    sa.ForeignKeyConstraint(['sales_rep_id'], ['sales_reps.id'], name=op.f('fk_sales_attributions_sales_rep_id_sales_reps'), ondelete='SET NULL'),
    sa.ForeignKeyConstraint(['seller_id'], ['users.id'], name=op.f('fk_sales_attributions_seller_id_users'), ondelete='RESTRICT'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_sales_attributions')),
    sa.UniqueConstraint('order_id', name='uq_sales_attributions_order_id')
    )
    op.create_index('ix_sales_attributions_seller_id', 'sales_attributions', ['seller_id'], unique=False)
    op.create_index('ix_sales_attributions_status', 'sales_attributions', ['status'], unique=False)
    op.create_table('screen_anchor_history',
    sa.Column('id', sa.UUID(), nullable=False),
    sa.Column('screen_id', sa.UUID(), nullable=False),
    sa.Column('old_anchor', sa.DateTime(timezone=True), nullable=True),
    sa.Column('new_anchor', sa.DateTime(timezone=True), nullable=True),
    sa.Column('source', sa.String(length=16), nullable=False),
    sa.Column('reason', sa.Text(), nullable=False),
    sa.Column('actor_user_id', sa.UUID(), nullable=True),
    sa.Column('device_id', sa.UUID(), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.CheckConstraint("source IN ('PLAYER', 'SERVER', 'ADMIN_RESET')", name=op.f('ck_screen_anchor_history_source')),
    sa.CheckConstraint('length(btrim(reason)) >= 3', name=op.f('ck_screen_anchor_history_reason_required')),
    sa.ForeignKeyConstraint(['actor_user_id'], ['users.id'], name=op.f('fk_screen_anchor_history_actor_user_id_users'), ondelete='RESTRICT'),
    sa.ForeignKeyConstraint(['screen_id'], ['screens.id'], name=op.f('fk_screen_anchor_history_screen_id_screens'), ondelete='RESTRICT'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_screen_anchor_history'))
    )
    op.create_index('ix_screen_anchor_history_screen', 'screen_anchor_history', ['screen_id', 'created_at'], unique=False)
    op.create_table('screen_history',
    sa.Column('id', sa.UUID(), nullable=False),
    sa.Column('screen_id', sa.UUID(), nullable=False),
    sa.Column('event', sa.String(length=20), nullable=False),
    sa.Column('establishment_id', sa.UUID(), nullable=False),
    sa.Column('from_establishment_id', sa.UUID(), nullable=True),
    sa.Column('before_data', postgresql.JSONB(astext_type=Text()), nullable=True),
    sa.Column('after_data', postgresql.JSONB(astext_type=Text()), nullable=True),
    sa.Column('reason', sa.Text(), nullable=True),
    sa.Column('actor_user_id', sa.UUID(), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('clock_timestamp()'), nullable=False),
    sa.CheckConstraint("event IN ('CREATED','UPDATED','HARDWARE_CHANGED','TV_REPLACED','PLAYER_REPLACED','MOVED','STATUS_CHANGED','SELLABLE_CHANGED','DECOMMISSIONED','AUDIENCE_UPDATED')", name=op.f('ck_screen_history_event_valid')),
    sa.ForeignKeyConstraint(['actor_user_id'], ['users.id'], name=op.f('fk_screen_history_actor_user_id_users'), ondelete='SET NULL'),
    sa.ForeignKeyConstraint(['establishment_id'], ['establishments.id'], name=op.f('fk_screen_history_establishment_id_establishments'), ondelete='RESTRICT'),
    sa.ForeignKeyConstraint(['from_establishment_id'], ['establishments.id'], name=op.f('fk_screen_history_from_establishment_id_establishments'), ondelete='RESTRICT'),
    sa.ForeignKeyConstraint(['screen_id'], ['screens.id'], name=op.f('fk_screen_history_screen_id_screens'), ondelete='RESTRICT'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_screen_history'))
    )
    op.create_index('ix_screen_history_event', 'screen_history', ['event'], unique=False)
    op.create_index('ix_screen_history_screen_id_created_at', 'screen_history', ['screen_id', 'created_at'], unique=False)
    op.create_table('screen_retirements',
    sa.Column('id', sa.UUID(), nullable=False),
    sa.Column('screen_id', sa.UUID(), nullable=False),
    sa.Column('establishment_id', sa.UUID(), nullable=True),
    sa.Column('reason', sa.Text(), nullable=False),
    sa.Column('affected', postgresql.JSONB(astext_type=Text()), nullable=False),
    sa.Column('status', sa.String(length=10), server_default='OPEN', nullable=False),
    sa.Column('resolution', sa.Text(), nullable=True),
    sa.Column('resolved_by', sa.UUID(), nullable=True),
    sa.Column('resolved_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.CheckConstraint("status IN ('OPEN','RESOLVED')", name=op.f('ck_screen_retirements_status')),
    sa.ForeignKeyConstraint(['resolved_by'], ['users.id'], name=op.f('fk_screen_retirements_resolved_by_users'), ondelete='RESTRICT'),
    sa.ForeignKeyConstraint(['screen_id'], ['screens.id'], name=op.f('fk_screen_retirements_screen_id_screens'), ondelete='RESTRICT'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_screen_retirements'))
    )
    op.create_index('ix_screen_retirements_status', 'screen_retirements', ['status', 'created_at'], unique=False)
    op.create_table('ticket_events',
    sa.Column('id', sa.UUID(), nullable=False),
    sa.Column('ticket_id', sa.UUID(), nullable=False),
    sa.Column('actor_id', sa.UUID(), nullable=True),
    sa.Column('type', sa.String(length=30), nullable=False),
    sa.Column('from_value', sa.String(length=100), nullable=True),
    sa.Column('to_value', sa.String(length=100), nullable=True),
    sa.Column('internal', sa.Boolean(), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.ForeignKeyConstraint(['ticket_id'], ['tickets.id'], name=op.f('fk_ticket_events_ticket_id_tickets'), ondelete='RESTRICT'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_ticket_events'))
    )
    op.create_index('ix_ticket_events_ticket_id', 'ticket_events', ['ticket_id'], unique=False)
    op.create_table('withdrawals',
    sa.Column('id', sa.UUID(), nullable=False),
    sa.Column('kind', sa.String(length=16), nullable=False),
    sa.Column('origin', sa.String(length=10), nullable=False),
    sa.Column('establishment_id', sa.UUID(), nullable=True),
    sa.Column('org_id', sa.UUID(), nullable=True),
    sa.Column('pix_key_id', sa.UUID(), nullable=True),
    sa.Column('pix_key_type', sa.String(length=10), nullable=False),
    sa.Column('destination_enc', sa.Text(), nullable=False),
    sa.Column('destination_masked', sa.String(length=80), nullable=False),
    sa.Column('amount_cents', sa.BigInteger(), nullable=False),
    sa.Column('sent_cents', sa.BigInteger(), nullable=True),
    sa.Column('fee_cents', sa.BigInteger(), nullable=True),
    sa.Column('net_cents', sa.BigInteger(), nullable=True),
    sa.Column('status', sa.String(length=12), nullable=False),
    sa.Column('approval_request_id', sa.UUID(), nullable=True),
    sa.Column('ref_type', sa.String(length=40), nullable=True),
    sa.Column('ref_id', sa.String(length=64), nullable=True),
    sa.Column('requested_by', sa.UUID(), nullable=True),
    sa.Column('idempotency_key', sa.String(length=120), nullable=False),
    sa.Column('provider_withdrawal_id', sa.BigInteger(), nullable=True),
    sa.Column('provider_external_id', sa.String(length=80), nullable=True),
    sa.Column('provider_status', sa.String(length=20), nullable=True),
    sa.Column('failure_reason', sa.String(length=300), nullable=True),
    sa.Column('requested_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('executing_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('completed_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.CheckConstraint("(kind IN ('ESTABLISHMENT','DEPOSIT')) = (establishment_id IS NOT NULL)", name=op.f('ck_withdrawals_establishment_matches_kind')),
    sa.CheckConstraint("kind = 'ESTABLISHMENT' OR approval_request_id IS NOT NULL", name=op.f('ck_withdrawals_rd_money_needs_approval')),
    sa.CheckConstraint("kind IN ('ESTABLISHMENT','RD','REFUND','PAYABLE','DEPOSIT')", name=op.f('ck_withdrawals_kind_valid')),
    sa.CheckConstraint("origin IN ('MANUAL','AUTOMATIC')", name=op.f('ck_withdrawals_origin_valid')),
    sa.CheckConstraint("pix_key_type IN ('cpf','cnpj','email','phone','random')", name=op.f('ck_withdrawals_pix_key_type_valid')),
    sa.CheckConstraint("status IN ('REQUESTED','EXECUTING','PROCESSING','COMPLETED','FAILED','CANCELLED','UNKNOWN')", name=op.f('ck_withdrawals_status_valid')),
    sa.CheckConstraint('amount_cents > 0', name=op.f('ck_withdrawals_amount_positive')),
    sa.ForeignKeyConstraint(['approval_request_id'], ['financial_approval_requests.id'], name=op.f('fk_withdrawals_approval_request_id_financial_approval_requests'), ondelete='RESTRICT'),
    sa.ForeignKeyConstraint(['establishment_id'], ['establishments.id'], name=op.f('fk_withdrawals_establishment_id_establishments'), ondelete='RESTRICT'),
    sa.ForeignKeyConstraint(['pix_key_id'], ['establishment_pix_keys.id'], name=op.f('fk_withdrawals_pix_key_id_establishment_pix_keys'), ondelete='RESTRICT'),
    sa.ForeignKeyConstraint(['requested_by'], ['users.id'], name=op.f('fk_withdrawals_requested_by_users'), ondelete='RESTRICT'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_withdrawals')),
    sa.UniqueConstraint('idempotency_key', name='uq_withdrawals_idempotency_key'),
    sa.UniqueConstraint('provider_external_id', name='uq_withdrawals_provider_external_id')
    )
    op.create_index('ix_withdrawals_establishment', 'withdrawals', ['establishment_id', 'status'], unique=False)
    op.create_index('ix_withdrawals_status', 'withdrawals', ['status'], unique=False)
    op.create_table('campaign_revisions',
    sa.Column('id', sa.UUID(), nullable=False),
    sa.Column('campaign_id', sa.UUID(), nullable=False),
    sa.Column('number', sa.Integer(), nullable=False),
    sa.Column('kind', sa.String(length=10), nullable=False),
    sa.Column('status', sa.String(length=30), nullable=False),
    sa.Column('spec', postgresql.JSONB(astext_type=Text()), nullable=False),
    sa.Column('screen_ids', postgresql.JSONB(astext_type=Text()), nullable=False),
    sa.Column('creative_ids', postgresql.JSONB(astext_type=Text()), nullable=False),
    sa.Column('change_summary', postgresql.JSONB(astext_type=Text()), nullable=True),
    sa.Column('quote', postgresql.JSONB(astext_type=Text()), nullable=True),
    sa.Column('quote_hash', sa.String(length=64), nullable=True),
    sa.Column('price_cents', sa.BigInteger(), nullable=False),
    sa.Column('price_diff_cents', sa.BigInteger(), nullable=False),
    sa.Column('order_id', sa.UUID(), nullable=True),
    sa.Column('hold_id', sa.UUID(), nullable=True),
    sa.Column('created_by', sa.UUID(), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('decided_by', sa.UUID(), nullable=True),
    sa.Column('decided_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('decision_reason', sa.Text(), nullable=True),
    sa.CheckConstraint("kind IN ('INITIAL', 'CHANGE', 'RENEWAL')", name=op.f('ck_campaign_revisions_kind_valid')),
    sa.CheckConstraint("status IN ('AGUARDANDO_PAGAMENTO', 'AGUARDANDO_MIDIA', 'EM_ANALISE', 'APROVADA', 'REJEITADA', 'SUBSTITUIDA', 'CANCELADA', 'EXPIRADA')", name=op.f('ck_campaign_revisions_status_valid')),
    sa.ForeignKeyConstraint(['campaign_id'], ['campaigns.id'], name=op.f('fk_campaign_revisions_campaign_id_campaigns'), ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['created_by'], ['users.id'], name=op.f('fk_campaign_revisions_created_by_users'), ondelete='RESTRICT'),
    sa.ForeignKeyConstraint(['decided_by'], ['users.id'], name=op.f('fk_campaign_revisions_decided_by_users'), ondelete='RESTRICT'),
    sa.ForeignKeyConstraint(['order_id'], ['orders.id'], name=op.f('fk_campaign_revisions_order_id_orders'), ondelete='RESTRICT'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_campaign_revisions')),
    sa.UniqueConstraint('campaign_id', 'number', name='uq_campaign_revisions_campaign_number')
    )
    op.create_index('ix_campaign_revisions_campaign_id', 'campaign_revisions', ['campaign_id'], unique=False)
    op.create_table('campaign_segments',
    sa.Column('campaign_id', sa.UUID(), nullable=False),
    sa.Column('segment_id', sa.UUID(), nullable=False),
    sa.ForeignKeyConstraint(['campaign_id'], ['campaigns.id'], name=op.f('fk_campaign_segments_campaign_id_campaigns'), ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['segment_id'], ['segments.id'], name=op.f('fk_campaign_segments_segment_id_segments'), ondelete='RESTRICT'),
    sa.PrimaryKeyConstraint('campaign_id', 'segment_id', name=op.f('pk_campaign_segments'))
    )
    op.create_table('campaign_status_history',
    sa.Column('id', sa.UUID(), nullable=False),
    sa.Column('campaign_id', sa.UUID(), nullable=False),
    sa.Column('from_status', sa.String(length=30), nullable=True),
    sa.Column('to_status', sa.String(length=30), nullable=False),
    sa.Column('trigger', sa.String(length=60), nullable=False),
    sa.Column('actor_user_id', sa.UUID(), nullable=True),
    sa.Column('real_user_id', sa.UUID(), nullable=True),
    sa.Column('origin', sa.String(length=20), nullable=False),
    sa.Column('reason', sa.Text(), nullable=True),
    sa.Column('revision_id', sa.UUID(), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.ForeignKeyConstraint(['campaign_id'], ['campaigns.id'], name=op.f('fk_campaign_status_history_campaign_id_campaigns'), ondelete='RESTRICT'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_campaign_status_history'))
    )
    op.create_index('ix_campaign_status_history_campaign_id', 'campaign_status_history', ['campaign_id', 'created_at'], unique=False)
    op.create_table('conflict_overrides',
    sa.Column('id', sa.UUID(), nullable=False),
    sa.Column('campaign_id', sa.UUID(), nullable=False),
    sa.Column('establishment_id', sa.UUID(), nullable=False),
    sa.Column('justification', sa.Text(), nullable=False),
    sa.Column('approved_by', sa.UUID(), nullable=False),
    sa.Column('approved_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('expires_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('revoked_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('revoked_by', sa.UUID(), nullable=True),
    sa.Column('revoke_reason', sa.Text(), nullable=True),
    sa.CheckConstraint('expires_at IS NULL OR expires_at > approved_at', name=op.f('ck_conflict_overrides_validity')),
    sa.CheckConstraint('length(btrim(justification)) >= 20', name=op.f('ck_conflict_overrides_justification_len')),
    sa.ForeignKeyConstraint(['approved_by'], ['users.id'], name=op.f('fk_conflict_overrides_approved_by_users'), ondelete='RESTRICT'),
    sa.ForeignKeyConstraint(['campaign_id'], ['campaigns.id'], name=op.f('fk_conflict_overrides_campaign_id_campaigns'), ondelete='RESTRICT'),
    sa.ForeignKeyConstraint(['establishment_id'], ['establishments.id'], name=op.f('fk_conflict_overrides_establishment_id_establishments'), ondelete='RESTRICT'),
    sa.ForeignKeyConstraint(['revoked_by'], ['users.id'], name=op.f('fk_conflict_overrides_revoked_by_users'), ondelete='RESTRICT'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_conflict_overrides'))
    )
    op.create_index('ux_conflict_overrides_active', 'conflict_overrides', ['campaign_id', 'establishment_id'], unique=True, postgresql_where=sa.text('revoked_at IS NULL'))
    op.create_table('contract_acceptances',
    sa.Column('id', sa.UUID(), nullable=False),
    sa.Column('template_id', sa.UUID(), nullable=False),
    sa.Column('kind', sa.String(length=20), nullable=False),
    sa.Column('version', sa.Integer(), nullable=False),
    sa.Column('body_sha256', sa.String(length=64), nullable=False),
    sa.Column('org_id', sa.UUID(), nullable=False),
    sa.Column('establishment_id', sa.UUID(), nullable=True),
    sa.Column('comodato_agreement_id', sa.UUID(), nullable=True),
    sa.Column('method', sa.String(length=15), nullable=False),
    sa.Column('accepted_by_user_id', sa.UUID(), nullable=True),
    sa.Column('recorded_by_user_id', sa.UUID(), nullable=True),
    sa.Column('signer_name', sa.String(length=255), nullable=False),
    sa.Column('ip_address', sa.String(length=64), nullable=True),
    sa.Column('user_agent', sa.String(length=500), nullable=True),
    sa.Column('accepted_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('pdf_storage_key', sa.String(length=500), nullable=True),
    sa.Column('pdf_sha256', sa.String(length=64), nullable=True),
    sa.Column('pdf_size', sa.BigInteger(), nullable=True),
    sa.Column('notes', sa.Text(), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('clock_timestamp()'), nullable=False),
    sa.CheckConstraint("(method = 'ELECTRONIC' AND accepted_by_user_id IS NOT NULL AND ip_address IS NOT NULL) OR (method = 'MANUAL_RECORD' AND recorded_by_user_id IS NOT NULL AND notes IS NOT NULL)", name=op.f('ck_contract_acceptances_method_consistent')),
    sa.CheckConstraint("method IN ('ELECTRONIC','MANUAL_RECORD')", name=op.f('ck_contract_acceptances_method_valid')),
    sa.CheckConstraint('char_length(body_sha256) = 64', name=op.f('ck_contract_acceptances_body_sha256_len')),
    sa.ForeignKeyConstraint(['accepted_by_user_id'], ['users.id'], name=op.f('fk_contract_acceptances_accepted_by_user_id_users'), ondelete='RESTRICT'),
    sa.ForeignKeyConstraint(['comodato_agreement_id'], ['comodato_agreements.id'], name=op.f('fk_contract_acceptances_comodato_agreement_id_comodato__76de'), ondelete='RESTRICT'),
    sa.ForeignKeyConstraint(['establishment_id'], ['establishments.id'], name=op.f('fk_contract_acceptances_establishment_id_establishments'), ondelete='RESTRICT'),
    sa.ForeignKeyConstraint(['org_id'], ['organizations.id'], name=op.f('fk_contract_acceptances_org_id_organizations'), ondelete='RESTRICT'),
    sa.ForeignKeyConstraint(['recorded_by_user_id'], ['users.id'], name=op.f('fk_contract_acceptances_recorded_by_user_id_users'), ondelete='RESTRICT'),
    sa.ForeignKeyConstraint(['template_id'], ['contract_templates.id'], name=op.f('fk_contract_acceptances_template_id_contract_templates'), ondelete='RESTRICT'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_contract_acceptances')),
    sa.UniqueConstraint('template_id', 'org_id', 'establishment_id', 'comodato_agreement_id', name='uq_contract_acceptances_subject', postgresql_nulls_not_distinct=True)
    )
    op.create_index('ix_contract_acceptances_establishment_id', 'contract_acceptances', ['establishment_id'], unique=False)
    op.create_index('ix_contract_acceptances_org_id', 'contract_acceptances', ['org_id'], unique=False)
    op.create_index('ix_contract_acceptances_template_id', 'contract_acceptances', ['template_id'], unique=False)
    op.create_table('creative_segments',
    sa.Column('creative_id', sa.UUID(), nullable=False),
    sa.Column('segment_id', sa.UUID(), nullable=False),
    sa.ForeignKeyConstraint(['creative_id'], ['creatives.id'], name=op.f('fk_creative_segments_creative_id_creatives'), ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['segment_id'], ['segments.id'], name=op.f('fk_creative_segments_segment_id_segments'), ondelete='RESTRICT'),
    sa.PrimaryKeyConstraint('creative_id', 'segment_id', name=op.f('pk_creative_segments'))
    )
    op.create_table('design_approvals',
    sa.Column('id', sa.UUID(), nullable=False),
    sa.Column('request_id', sa.UUID(), nullable=False),
    sa.Column('version_id', sa.UUID(), nullable=False),
    sa.Column('decision', sa.String(length=25), nullable=False),
    sa.Column('comment', sa.Text(), nullable=True),
    sa.Column('user_id', sa.UUID(), nullable=False),
    sa.Column('real_user_id', sa.UUID(), nullable=True),
    sa.Column('ip_address', sa.String(length=64), nullable=True),
    sa.Column('user_agent', sa.String(length=500), nullable=True),
    sa.Column('version_sha256', sa.String(length=64), nullable=False),
    sa.Column('revision_number', sa.Integer(), nullable=True),
    sa.Column('decided_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.CheckConstraint("decision IN ('APROVADA','ALTERACAO_SOLICITADA')", name=op.f('ck_design_approvals_decision_valid')),
    sa.ForeignKeyConstraint(['request_id'], ['design_requests.id'], name=op.f('fk_design_approvals_request_id_design_requests'), ondelete='RESTRICT'),
    sa.ForeignKeyConstraint(['user_id'], ['users.id'], name=op.f('fk_design_approvals_user_id_users'), ondelete='RESTRICT'),
    sa.ForeignKeyConstraint(['version_id'], ['design_versions.id'], name=op.f('fk_design_approvals_version_id_design_versions'), ondelete='RESTRICT'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_design_approvals'))
    )
    op.create_index('ix_design_approvals_request_id', 'design_approvals', ['request_id'], unique=False)
    op.create_index('uq_design_approvals_version_approved', 'design_approvals', ['version_id'], unique=True, postgresql_where=sa.text("decision = 'APROVADA'"))
    op.create_table('design_comments',
    sa.Column('id', sa.UUID(), nullable=False),
    sa.Column('request_id', sa.UUID(), nullable=False),
    sa.Column('version_id', sa.UUID(), nullable=True),
    sa.Column('author_id', sa.UUID(), nullable=False),
    sa.Column('body', sa.Text(), nullable=False),
    sa.Column('is_internal', sa.Boolean(), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.ForeignKeyConstraint(['author_id'], ['users.id'], name=op.f('fk_design_comments_author_id_users'), ondelete='RESTRICT'),
    sa.ForeignKeyConstraint(['request_id'], ['design_requests.id'], name=op.f('fk_design_comments_request_id_design_requests'), ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['version_id'], ['design_versions.id'], name=op.f('fk_design_comments_version_id_design_versions'), ondelete='SET NULL'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_design_comments'))
    )
    op.create_index('ix_design_comments_request_id', 'design_comments', ['request_id'], unique=False)
    op.create_table('device_commands',
    sa.Column('id', sa.UUID(), nullable=False),
    sa.Column('device_id', sa.UUID(), nullable=False),
    sa.Column('screen_id', sa.UUID(), nullable=False),
    sa.Column('command', sa.String(length=30), nullable=False),
    sa.Column('args', postgresql.JSONB(astext_type=Text()), nullable=False),
    sa.Column('reason', sa.Text(), nullable=False),
    sa.Column('confirmed', sa.Boolean(), nullable=False),
    sa.Column('issued_by_user_id', sa.UUID(), nullable=True),
    sa.Column('status', sa.String(length=20), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('expires_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('delivered_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('delivery_count', sa.Integer(), nullable=False),
    sa.Column('acked_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('ack_status', sa.String(length=20), nullable=True),
    sa.Column('result_detail', sa.Text(), nullable=True),
    sa.ForeignKeyConstraint(['device_id'], ['devices.id'], name=op.f('fk_device_commands_device_id_devices'), ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['issued_by_user_id'], ['users.id'], name=op.f('fk_device_commands_issued_by_user_id_users'), ondelete='SET NULL'),
    sa.ForeignKeyConstraint(['screen_id'], ['screens.id'], name=op.f('fk_device_commands_screen_id_screens'), ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_device_commands'))
    )
    op.create_index('ix_device_commands_device_status', 'device_commands', ['device_id', 'status'], unique=False)
    op.create_index('ix_device_commands_screen_created', 'device_commands', ['screen_id', 'created_at'], unique=False)
    op.create_table('device_sync_events',
    sa.Column('id', sa.BigInteger(), autoincrement=True, nullable=False),
    sa.Column('screen_id', sa.UUID(), nullable=False),
    sa.Column('device_id', sa.UUID(), nullable=False),
    sa.Column('org_id', sa.UUID(), nullable=True),
    sa.Column('establishment_id', sa.UUID(), nullable=True),
    sa.Column('type', sa.String(length=24), nullable=False),
    sa.Column('manifest_version', sa.Integer(), nullable=True),
    sa.Column('media_id', sa.UUID(), nullable=True),
    sa.Column('bytes', sa.BigInteger(), nullable=True),
    sa.Column('duration_ms', sa.BigInteger(), nullable=True),
    sa.Column('reason', sa.String(length=255), nullable=True),
    sa.Column('occurred_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.ForeignKeyConstraint(['device_id'], ['devices.id'], name=op.f('fk_device_sync_events_device_id_devices'), ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['screen_id'], ['screens.id'], name=op.f('fk_device_sync_events_screen_id_screens'), ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_device_sync_events'))
    )
    op.create_index('ix_device_sync_events_org_occurred', 'device_sync_events', ['org_id', 'occurred_at'], unique=False)
    op.create_index('ix_device_sync_events_screen_occurred', 'device_sync_events', ['screen_id', 'occurred_at'], unique=False)
    op.create_index('ix_device_sync_events_type_occurred', 'device_sync_events', ['type', 'occurred_at'], unique=False)
    op.create_table('device_telemetry',
    sa.Column('id', sa.BigInteger(), autoincrement=True, nullable=False),
    sa.Column('device_id', sa.UUID(), nullable=False),
    sa.Column('screen_id', sa.UUID(), nullable=False),
    sa.Column('recorded_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('data', postgresql.JSONB(astext_type=Text()), nullable=False),
    sa.ForeignKeyConstraint(['device_id'], ['devices.id'], name=op.f('fk_device_telemetry_device_id_devices'), ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['screen_id'], ['screens.id'], name=op.f('fk_device_telemetry_screen_id_screens'), ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_device_telemetry'))
    )
    op.create_index('ix_device_telemetry_device_recorded', 'device_telemetry', ['device_id', 'recorded_at'], unique=False)
    op.create_table('emergency_broadcasts',
    sa.Column('id', sa.UUID(), nullable=False),
    sa.Column('title', sa.String(length=200), nullable=False),
    sa.Column('creative_id', sa.UUID(), nullable=False),
    sa.Column('starts_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('ends_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('target_kind', sa.String(length=16), nullable=False),
    sa.Column('target_ids', postgresql.JSONB(astext_type=Text()), nullable=False),
    sa.Column('status', sa.String(length=12), server_default='ACTIVE', nullable=False),
    sa.Column('reason', sa.Text(), nullable=False),
    sa.Column('created_by', sa.UUID(), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('cancelled_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('cancelled_by', sa.UUID(), nullable=True),
    sa.Column('cancel_reason', sa.Text(), nullable=True),
    sa.CheckConstraint("status IN ('ACTIVE','CANCELLED')", name=op.f('ck_emergency_broadcasts_status')),
    sa.CheckConstraint("target_kind = 'ALL' OR jsonb_array_length(target_ids) > 0", name=op.f('ck_emergency_broadcasts_target_ids')),
    sa.CheckConstraint("target_kind IN ('ALL', 'SCREENS', 'ESTABLISHMENTS', 'STATES', 'CITIES')", name=op.f('ck_emergency_broadcasts_target_kind')),
    sa.CheckConstraint('ends_at > starts_at', name=op.f('ck_emergency_broadcasts_window')),
    sa.CheckConstraint('length(btrim(reason)) >= 10', name=op.f('ck_emergency_broadcasts_reason_required')),
    sa.ForeignKeyConstraint(['cancelled_by'], ['users.id'], name=op.f('fk_emergency_broadcasts_cancelled_by_users'), ondelete='RESTRICT'),
    sa.ForeignKeyConstraint(['created_by'], ['users.id'], name=op.f('fk_emergency_broadcasts_created_by_users'), ondelete='RESTRICT'),
    sa.ForeignKeyConstraint(['creative_id'], ['creatives.id'], name=op.f('fk_emergency_broadcasts_creative_id_creatives'), ondelete='RESTRICT'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_emergency_broadcasts'))
    )
    op.create_index('ix_emergency_broadcasts_window', 'emergency_broadcasts', ['status', 'starts_at', 'ends_at'], unique=False)
    op.create_table('fallback_content',
    sa.Column('id', sa.UUID(), nullable=False),
    sa.Column('scope', sa.String(length=16), nullable=False),
    sa.Column('establishment_id', sa.UUID(), nullable=True),
    sa.Column('creative_id', sa.UUID(), nullable=False),
    sa.Column('purpose', sa.String(length=32), nullable=False),
    sa.Column('position', sa.Integer(), nullable=False),
    sa.Column('active', sa.Boolean(), server_default='true', nullable=False),
    sa.Column('created_by', sa.UUID(), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.CheckConstraint("(scope = 'GLOBAL') = (establishment_id IS NULL)", name=op.f('ck_fallback_content_scope_matches_establishment')),
    sa.CheckConstraint("purpose IN ('ESTABLISHMENT_LOGO', 'RD_DEFAULT', 'RD_PROMO', 'INSTITUTIONAL_MESSAGE')", name=op.f('ck_fallback_content_purpose')),
    sa.CheckConstraint("scope IN ('GLOBAL','ESTABLISHMENT')", name=op.f('ck_fallback_content_scope')),
    sa.CheckConstraint('position >= 0', name=op.f('ck_fallback_content_position')),
    sa.ForeignKeyConstraint(['created_by'], ['users.id'], name=op.f('fk_fallback_content_created_by_users'), ondelete='RESTRICT'),
    sa.ForeignKeyConstraint(['creative_id'], ['creatives.id'], name=op.f('fk_fallback_content_creative_id_creatives'), ondelete='RESTRICT'),
    sa.ForeignKeyConstraint(['establishment_id'], ['establishments.id'], name=op.f('fk_fallback_content_establishment_id_establishments'), ondelete='RESTRICT'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_fallback_content'))
    )
    op.create_index('ix_fallback_content_scope', 'fallback_content', ['scope', 'establishment_id', 'position'], unique=False)
    op.create_table('installation_photos',
    sa.Column('id', sa.UUID(), nullable=False),
    sa.Column('installation_id', sa.UUID(), nullable=False),
    sa.Column('kind', sa.String(length=12), nullable=False),
    sa.Column('storage_key', sa.String(length=500), nullable=False),
    sa.Column('sha256', sa.String(length=64), nullable=False),
    sa.Column('content_type', sa.String(length=20), nullable=False),
    sa.Column('size_bytes', sa.BigInteger(), nullable=False),
    sa.Column('width', sa.Integer(), nullable=True),
    sa.Column('height', sa.Integer(), nullable=True),
    sa.Column('uploaded_by', sa.UUID(), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.CheckConstraint("content_type IN ('image/jpeg','image/png')", name=op.f('ck_installation_photos_content_type_valid')),
    sa.CheckConstraint("kind IN ('TV_WORKING','EQUIPMENT','SITE','OTHER')", name=op.f('ck_installation_photos_kind_valid')),
    sa.CheckConstraint('size_bytes > 0', name=op.f('ck_installation_photos_size_positive')),
    sa.ForeignKeyConstraint(['installation_id'], ['installations.id'], name=op.f('fk_installation_photos_installation_id_installations'), ondelete='RESTRICT'),
    sa.ForeignKeyConstraint(['uploaded_by'], ['users.id'], name=op.f('fk_installation_photos_uploaded_by_users'), ondelete='SET NULL'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_installation_photos')),
    sa.UniqueConstraint('storage_key', name=op.f('uq_installation_photos_storage_key'))
    )
    op.create_index('ix_installation_photos_installation_id', 'installation_photos', ['installation_id'], unique=False)
    op.create_table('internal_content',
    sa.Column('id', sa.UUID(), nullable=False),
    sa.Column('establishment_id', sa.UUID(), nullable=False),
    sa.Column('screen_id', sa.UUID(), nullable=True),
    sa.Column('creative_id', sa.UUID(), nullable=False),
    sa.Column('position', sa.Integer(), nullable=False),
    sa.Column('active', sa.Boolean(), server_default='true', nullable=False),
    sa.Column('created_by', sa.UUID(), nullable=True),
    sa.Column('updated_by', sa.UUID(), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.CheckConstraint('position >= 0', name=op.f('ck_internal_content_position')),
    sa.ForeignKeyConstraint(['created_by'], ['users.id'], name=op.f('fk_internal_content_created_by_users'), ondelete='RESTRICT'),
    sa.ForeignKeyConstraint(['creative_id'], ['creatives.id'], name=op.f('fk_internal_content_creative_id_creatives'), ondelete='RESTRICT'),
    sa.ForeignKeyConstraint(['establishment_id'], ['establishments.id'], name=op.f('fk_internal_content_establishment_id_establishments'), ondelete='RESTRICT'),
    sa.ForeignKeyConstraint(['screen_id'], ['screens.id'], name=op.f('fk_internal_content_screen_id_screens'), ondelete='RESTRICT'),
    sa.ForeignKeyConstraint(['updated_by'], ['users.id'], name=op.f('fk_internal_content_updated_by_users'), ondelete='RESTRICT'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_internal_content'))
    )
    op.create_index('ix_internal_content_creative', 'internal_content', ['creative_id'], unique=False)
    op.create_index('ix_internal_content_scope', 'internal_content', ['establishment_id', 'screen_id', 'position'], unique=False)
    op.create_table('inventory_holds',
    sa.Column('id', sa.UUID(), nullable=False),
    sa.Column('org_id', sa.UUID(), nullable=False),
    sa.Column('campaign_id', sa.UUID(), nullable=False),
    sa.Column('order_id', sa.UUID(), nullable=True),
    sa.Column('replaces_hold_id', sa.UUID(), nullable=True),
    sa.Column('status', sa.String(length=30), nullable=False),
    sa.Column('mode', sa.String(length=16), nullable=False),
    sa.Column('idempotency_key', sa.String(length=255), nullable=False),
    sa.Column('request_hash', sa.String(length=64), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('hold_expires_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('converted_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('released_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('release_reason', sa.String(length=64), nullable=True),
    sa.Column('screens_requested', sa.Integer(), nullable=False),
    sa.Column('screens_held', sa.Integer(), nullable=False),
    sa.Column('details', postgresql.JSONB(astext_type=Text()), nullable=True),
    sa.Column('created_by', sa.UUID(), nullable=True),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.CheckConstraint("mode IN ('ALL_OR_NOTHING','PARTIAL')", name=op.f('ck_inventory_holds_mode')),
    sa.CheckConstraint("status IN ('HELD', 'CONVERTED', 'EXPIRED', 'CANCELLED', 'PAID_WITHOUT_INVENTORY')", name=op.f('ck_inventory_holds_status')),
    sa.CheckConstraint('hold_expires_at > created_at', name=op.f('ck_inventory_holds_expires_after_created')),
    sa.CheckConstraint('length(idempotency_key) BETWEEN 1 AND 255', name=op.f('ck_inventory_holds_idempotency_key_len')),
    sa.ForeignKeyConstraint(['campaign_id'], ['campaigns.id'], name=op.f('fk_inventory_holds_campaign_id_campaigns'), ondelete='RESTRICT'),
    sa.ForeignKeyConstraint(['created_by'], ['users.id'], name=op.f('fk_inventory_holds_created_by_users'), ondelete='RESTRICT'),
    sa.ForeignKeyConstraint(['org_id'], ['organizations.id'], name=op.f('fk_inventory_holds_org_id_organizations'), ondelete='RESTRICT'),
    sa.ForeignKeyConstraint(['replaces_hold_id'], ['inventory_holds.id'], name=op.f('fk_inventory_holds_replaces_hold_id_inventory_holds'), ondelete='RESTRICT'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_inventory_holds')),
    sa.UniqueConstraint('org_id', 'idempotency_key', name='uq_inventory_holds_org_idempotency')
    )
    op.create_index('ix_inventory_holds_campaign', 'inventory_holds', ['campaign_id'], unique=False)
    op.create_index('ix_inventory_holds_expiring', 'inventory_holds', ['hold_expires_at'], unique=False, postgresql_where=sa.text("status = 'HELD'"))
    op.create_index('ix_inventory_holds_order', 'inventory_holds', ['order_id'], unique=False)
    op.create_index('ux_inventory_holds_one_held_per_campaign', 'inventory_holds', ['campaign_id'], unique=True, postgresql_where=sa.text("status = 'HELD'"))
    op.create_table('manifest_versions',
    sa.Column('screen_id', sa.UUID(), nullable=False),
    sa.Column('version', sa.BigInteger(), nullable=False),
    sa.Column('manifest_id', sa.UUID(), nullable=False),
    sa.Column('content_gz', sa.LargeBinary(), nullable=True),
    sa.Column('content_sha256', sa.String(length=64), nullable=False),
    sa.Column('plan_sha256', sa.String(length=64), nullable=False),
    sa.Column('size_bytes', sa.Integer(), nullable=False),
    sa.Column('signature', sa.Text(), nullable=False),
    sa.Column('key_id', sa.String(length=64), nullable=False),
    sa.Column('reason', sa.String(length=64), nullable=False),
    sa.Column('generated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('horizon_from', sa.DateTime(timezone=True), nullable=False),
    sa.Column('horizon_to', sa.DateTime(timezone=True), nullable=False),
    sa.Column('media_ids', postgresql.ARRAY(UUID()), nullable=False),
    sa.Column('creative_ids', postgresql.ARRAY(UUID()), nullable=False),
    sa.Column('campaign_ids', postgresql.ARRAY(UUID()), nullable=False),
    sa.Column('unplaced_count', sa.Integer(), server_default='0', nullable=False),
    sa.Column('applied_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('applied_by_device', sa.UUID(), nullable=True),
    sa.CheckConstraint('char_length(content_sha256) = 64 AND char_length(plan_sha256) = 64', name=op.f('ck_manifest_versions_sha_len')),
    sa.CheckConstraint('version > 0', name=op.f('ck_manifest_versions_version_positive')),
    sa.ForeignKeyConstraint(['applied_by_device'], ['devices.id'], name=op.f('fk_manifest_versions_applied_by_device_devices'), ondelete='SET NULL'),
    sa.ForeignKeyConstraint(['screen_id'], ['screens.id'], name=op.f('fk_manifest_versions_screen_id_screens'), ondelete='RESTRICT'),
    sa.PrimaryKeyConstraint('screen_id', 'version', name='pk_manifest_versions')
    )
    op.create_index('ix_manifest_versions_manifest_id', 'manifest_versions', ['manifest_id'], unique=True)
    op.create_index('ix_manifest_versions_screen_generated', 'manifest_versions', ['screen_id', 'generated_at'], unique=False)
    op.create_table('playback_events',
    sa.Column('playback_uuid', sa.UUID(), nullable=False),
    sa.Column('device_id', sa.UUID(), nullable=False),
    sa.Column('screen_id', sa.UUID(), nullable=False),
    sa.Column('establishment_id', sa.UUID(), nullable=True),
    sa.Column('org_id', sa.UUID(), nullable=True),
    sa.Column('creative_id', sa.UUID(), nullable=False),
    sa.Column('creative_code', sa.String(length=16), nullable=False),
    sa.Column('campaign_id', sa.UUID(), nullable=True),
    sa.Column('insertion_id', sa.UUID(), nullable=True),
    sa.Column('kind', sa.String(length=16), nullable=False),
    sa.Column('manifest_version', sa.Integer(), nullable=False),
    sa.Column('started_at_ms', sa.BigInteger(), nullable=False),
    sa.Column('finished_at_ms', sa.BigInteger(), nullable=False),
    sa.Column('started_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('finished_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('expected_ms', sa.Integer(), nullable=False),
    sa.Column('actual_ms', sa.Integer(), nullable=False),
    sa.Column('result', sa.String(length=16), nullable=False),
    sa.Column('seq', sa.BigInteger(), nullable=False),
    sa.Column('nonce', sa.String(length=32), nullable=False),
    sa.Column('prev_hash', sa.String(length=64), nullable=False),
    sa.Column('event_hash', sa.String(length=64), nullable=False),
    sa.Column('clock_offset_ms', sa.BigInteger(), nullable=False),
    sa.Column('offline', sa.Boolean(), nullable=False),
    sa.Column('signature', sa.String(length=100), nullable=False),
    sa.Column('claimed', postgresql.JSONB(astext_type=Text()), nullable=True),
    sa.Column('status', sa.String(length=12), nullable=False),
    sa.Column('valid', sa.Boolean(), nullable=False),
    sa.Column('reason', sa.String(length=40), nullable=True),
    sa.Column('reasons', postgresql.JSONB(astext_type=Text()), nullable=False),
    sa.Column('flags', postgresql.JSONB(astext_type=Text()), nullable=False),
    sa.Column('local_day', sa.Date(), nullable=False),
    sa.Column('received_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('aggregated_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('financial_status', sa.String(length=16), nullable=False),
    sa.Column('financial_appropriated_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('financial_attempts', sa.Integer(), nullable=False),
    sa.ForeignKeyConstraint(['device_id'], ['devices.id'], name=op.f('fk_playback_events_device_id_devices'), ondelete='RESTRICT'),
    sa.ForeignKeyConstraint(['screen_id'], ['screens.id'], name=op.f('fk_playback_events_screen_id_screens'), ondelete='RESTRICT'),
    sa.PrimaryKeyConstraint('playback_uuid', name=op.f('pk_playback_events'))
    )
    op.create_index('ix_playback_events_campaign_started', 'playback_events', ['campaign_id', 'started_at'], unique=False)
    op.create_index('ix_playback_events_device_nonce', 'playback_events', ['device_id', 'nonce'], unique=False)
    op.create_index('ix_playback_events_device_started_ms', 'playback_events', ['device_id', 'started_at_ms'], unique=False)
    op.create_index('ix_playback_events_fin_pending', 'playback_events', ['received_at'], unique=False, postgresql_where=sa.text("financial_status = 'PENDING'"))
    op.create_index('ix_playback_events_not_aggregated', 'playback_events', ['received_at'], unique=False, postgresql_where=sa.text('aggregated_at IS NULL'))
    op.create_index('ix_playback_events_received_at', 'playback_events', ['received_at'], unique=False)
    op.create_index('ix_playback_events_screen_started', 'playback_events', ['screen_id', 'started_at'], unique=False)
    op.create_index('ix_playback_events_screen_started_ms', 'playback_events', ['screen_id', 'started_at_ms'], unique=False)
    op.create_index('ix_playback_events_started_at_brin', 'playback_events', ['started_at'], unique=False, postgresql_using='brin')
    op.create_table('refunds',
    sa.Column('id', sa.UUID(), nullable=False),
    sa.Column('payment_order_id', sa.UUID(), nullable=False),
    sa.Column('campaign_id', sa.UUID(), nullable=True),
    sa.Column('org_id', sa.UUID(), nullable=False),
    sa.Column('policy', sa.String(length=20), nullable=False),
    sa.Column('as_of', sa.Date(), nullable=False),
    sa.Column('refund_cents', sa.BigInteger(), nullable=False),
    sa.Column('media_refund_cents', sa.BigInteger(), nullable=False),
    sa.Column('services_refund_cents', sa.BigInteger(), nullable=False),
    sa.Column('executed_valid', sa.BigInteger(), nullable=False),
    sa.Column('executed_expected', sa.BigInteger(), nullable=False),
    sa.Column('clawback_fraction_ppm', sa.Integer(), nullable=False),
    sa.Column('calculation', postgresql.JSONB(astext_type=Text()), nullable=False),
    sa.Column('status', sa.String(length=28), nullable=False),
    sa.Column('reason', sa.Text(), nullable=False),
    sa.Column('requested_by', sa.UUID(), nullable=True),
    sa.Column('approval_request_id', sa.UUID(), nullable=True),
    sa.Column('ledger_transaction_id', sa.UUID(), nullable=True),
    sa.Column('withdrawal_id', sa.UUID(), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.CheckConstraint("policy IN ('FULL_BEFORE_START','PROPORTIONAL','ADMIN_OVERRIDE')", name=op.f('ck_refunds_policy_valid')),
    sa.CheckConstraint("status IN ('AWAITING_APPROVAL','APPROVED','PAYING','PAID','BLOCKED_EXTERNAL_DEPENDENCY','REJECTED','FAILED','UNKNOWN')", name=op.f('ck_refunds_status_valid')),
    sa.CheckConstraint('refund_cents > 0', name=op.f('ck_refunds_amount_positive')),
    sa.ForeignKeyConstraint(['approval_request_id'], ['financial_approval_requests.id'], name=op.f('fk_refunds_approval_request_id_financial_approval_requests'), ondelete='RESTRICT'),
    sa.ForeignKeyConstraint(['ledger_transaction_id'], ['ledger_transactions.id'], name=op.f('fk_refunds_ledger_transaction_id_ledger_transactions'), ondelete='RESTRICT'),
    sa.ForeignKeyConstraint(['payment_order_id'], ['payment_orders.id'], name=op.f('fk_refunds_payment_order_id_payment_orders'), ondelete='RESTRICT'),
    sa.ForeignKeyConstraint(['requested_by'], ['users.id'], name=op.f('fk_refunds_requested_by_users'), ondelete='RESTRICT'),
    sa.ForeignKeyConstraint(['withdrawal_id'], ['withdrawals.id'], name=op.f('fk_refunds_withdrawal_id_withdrawals'), ondelete='RESTRICT'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_refunds'))
    )
    op.create_index('ix_refunds_order', 'refunds', ['payment_order_id'], unique=False)
    op.create_table('screen_inventory_state',
    sa.Column('screen_id', sa.UUID(), nullable=False),
    sa.Column('timezone', sa.String(length=64), nullable=False),
    sa.Column('policy_id', sa.UUID(), nullable=True),
    sa.Column('anchor_utc', sa.DateTime(timezone=True), nullable=True),
    sa.Column('anchor_source', sa.String(length=16), nullable=True),
    sa.Column('anchor_device_id', sa.UUID(), nullable=True),
    sa.Column('anchor_reported_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('sellable', sa.Boolean(), server_default='true', nullable=False),
    sa.Column('unsellable_reason', sa.Text(), nullable=True),
    sa.Column('inventory_version', sa.BigInteger(), server_default='0', nullable=False),
    sa.Column('manifest_version', sa.BigInteger(), server_default='0', nullable=False),
    sa.Column('last_manifest_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.CheckConstraint("anchor_source IS NULL OR anchor_source IN ('PLAYER', 'SERVER', 'ADMIN_RESET')", name=op.f('ck_screen_inventory_state_anchor_source')),
    sa.CheckConstraint('(anchor_utc IS NULL) = (anchor_source IS NULL)', name=op.f('ck_screen_inventory_state_anchor_source_present')),
    sa.CheckConstraint('inventory_version >= 0 AND manifest_version >= 0', name=op.f('ck_screen_inventory_state_versions')),
    sa.ForeignKeyConstraint(['anchor_device_id'], ['devices.id'], name=op.f('fk_screen_inventory_state_anchor_device_id_devices'), ondelete='SET NULL'),
    sa.ForeignKeyConstraint(['policy_id'], ['rotation_policies.id'], name=op.f('fk_screen_inventory_state_policy_id_rotation_policies'), ondelete='RESTRICT'),
    sa.ForeignKeyConstraint(['screen_id'], ['screens.id'], name=op.f('fk_screen_inventory_state_screen_id_screens'), ondelete='RESTRICT'),
    sa.PrimaryKeyConstraint('screen_id', name=op.f('pk_screen_inventory_state'))
    )
    op.create_index('ix_screen_inventory_state_sellable', 'screen_inventory_state', ['sellable'], unique=False)
    op.create_table('booking_patterns',
    sa.Column('id', sa.UUID(), nullable=False),
    sa.Column('hold_id', sa.UUID(), nullable=False),
    sa.Column('campaign_id', sa.UUID(), nullable=False),
    sa.Column('advertiser_org_id', sa.UUID(), nullable=False),
    sa.Column('date_from', sa.Date(), nullable=False),
    sa.Column('date_to', sa.Date(), nullable=False),
    sa.Column('days_mask', sa.SmallInteger(), nullable=False),
    sa.Column('holiday_mode', sa.String(length=10), server_default='INCLUDE', nullable=False),
    sa.Column('time_bands', postgresql.JSONB(astext_type=Text()), nullable=False),
    sa.Column('duration_units', sa.SmallInteger(), nullable=False),
    sa.Column('freq_kind', sa.String(length=16), nullable=False),
    sa.Column('freq_value', sa.Integer(), nullable=False),
    sa.Column('specific_times', postgresql.JSONB(astext_type=Text()), nullable=True),
    sa.Column('min_interval_seconds', sa.Integer(), nullable=False),
    sa.Column('priority', sa.String(length=10), nullable=False),
    sa.Column('allow_same_advertiser_consecutive', sa.Boolean(), nullable=False),
    sa.Column('exclusivity', sa.String(length=32), server_default='NONE', nullable=False),
    sa.Column('per_day_distribution', postgresql.JSONB(astext_type=Text()), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.CheckConstraint("exclusivity IN ('NONE','COMPETITOR_EXCLUSIVE')", name=op.f('ck_booking_patterns_exclusivity')),
    sa.CheckConstraint("freq_kind <> 'PER_DAY' OR per_day_distribution IS NOT NULL", name=op.f('ck_booking_patterns_per_day_distribution')),
    sa.CheckConstraint("freq_kind <> 'SPECIFIC_TIME' OR specific_times IS NOT NULL", name=op.f('ck_booking_patterns_specific_times')),
    sa.CheckConstraint("freq_kind IN ('PER_HOUR','PER_DAY','SPECIFIC_TIME')", name=op.f('ck_booking_patterns_freq_kind')),
    sa.CheckConstraint("holiday_mode IN ('INCLUDE')", name=op.f('ck_booking_patterns_holiday_mode')),
    sa.CheckConstraint("jsonb_typeof(time_bands) = 'array'", name=op.f('ck_booking_patterns_time_bands_array')),
    sa.CheckConstraint("priority IN ('STANDARD','PREMIUM')", name=op.f('ck_booking_patterns_priority')),
    sa.CheckConstraint('date_to >= date_from', name=op.f('ck_booking_patterns_date_range')),
    sa.CheckConstraint('days_mask BETWEEN 1 AND 127', name=op.f('ck_booking_patterns_days_mask')),
    sa.CheckConstraint('duration_units IN (1, 2, 4)', name=op.f('ck_booking_patterns_duration_units')),
    sa.CheckConstraint('freq_value > 0', name=op.f('ck_booking_patterns_freq_value')),
    sa.CheckConstraint('min_interval_seconds >= 0', name=op.f('ck_booking_patterns_min_interval')),
    sa.ForeignKeyConstraint(['advertiser_org_id'], ['organizations.id'], name=op.f('fk_booking_patterns_advertiser_org_id_organizations'), ondelete='RESTRICT'),
    sa.ForeignKeyConstraint(['campaign_id'], ['campaigns.id'], name=op.f('fk_booking_patterns_campaign_id_campaigns'), ondelete='RESTRICT'),
    sa.ForeignKeyConstraint(['hold_id'], ['inventory_holds.id'], name=op.f('fk_booking_patterns_hold_id_inventory_holds'), ondelete='RESTRICT'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_booking_patterns'))
    )
    op.create_index('ix_booking_patterns_campaign', 'booking_patterns', ['campaign_id'], unique=False)
    op.create_index('ix_booking_patterns_hold', 'booking_patterns', ['hold_id'], unique=False)
    op.create_table('maintenance_orders',
    sa.Column('id', sa.UUID(), nullable=False),
    sa.Column('establishment_id', sa.UUID(), nullable=False),
    sa.Column('screen_id', sa.UUID(), nullable=True),
    sa.Column('equipment_item_id', sa.UUID(), nullable=True),
    sa.Column('component', sa.String(length=10), nullable=False),
    sa.Column('status', sa.String(length=14), nullable=False),
    sa.Column('priority', sa.String(length=8), nullable=False),
    sa.Column('description', sa.Text(), nullable=False),
    sa.Column('resolution', sa.Text(), nullable=True),
    sa.Column('technician_user_id', sa.UUID(), nullable=True),
    sa.Column('support_ticket_id', sa.UUID(), nullable=True),
    sa.Column('opened_by', sa.UUID(), nullable=True),
    sa.Column('opened_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('closed_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.CheckConstraint("(status IN ('RESOLVED','CANCELLED')) = (closed_at IS NOT NULL)", name=op.f('ck_maintenance_orders_closed_consistent')),
    sa.CheckConstraint("component IN ('TV','TVBOX','CABLE','NETWORK','OTHER')", name=op.f('ck_maintenance_orders_component_valid')),
    sa.CheckConstraint("priority IN ('LOW','NORMAL','HIGH','URGENT')", name=op.f('ck_maintenance_orders_priority_valid')),
    sa.CheckConstraint("status IN ('OPEN','IN_PROGRESS','WAITING_PARTS','RESOLVED','CANCELLED')", name=op.f('ck_maintenance_orders_status_valid')),
    sa.ForeignKeyConstraint(['equipment_item_id'], ['equipment_items.id'], name=op.f('fk_maintenance_orders_equipment_item_id_equipment_items'), ondelete='RESTRICT'),
    sa.ForeignKeyConstraint(['establishment_id'], ['establishments.id'], name=op.f('fk_maintenance_orders_establishment_id_establishments'), ondelete='RESTRICT'),
    sa.ForeignKeyConstraint(['opened_by'], ['users.id'], name=op.f('fk_maintenance_orders_opened_by_users'), ondelete='SET NULL'),
    sa.ForeignKeyConstraint(['screen_id'], ['screens.id'], name=op.f('fk_maintenance_orders_screen_id_screens'), ondelete='RESTRICT'),
    sa.ForeignKeyConstraint(['technician_user_id'], ['users.id'], name=op.f('fk_maintenance_orders_technician_user_id_users'), ondelete='RESTRICT'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_maintenance_orders'))
    )
    op.create_index('ix_maintenance_orders_establishment_id', 'maintenance_orders', ['establishment_id'], unique=False)
    op.create_index('ix_maintenance_orders_screen_id', 'maintenance_orders', ['screen_id'], unique=False)
    op.create_index('ix_maintenance_orders_status', 'maintenance_orders', ['status'], unique=False)
    op.create_index('ix_maintenance_orders_technician_user_id', 'maintenance_orders', ['technician_user_id'], unique=False)
    op.create_table('order_items',
    sa.Column('id', sa.UUID(), nullable=False),
    sa.Column('order_id', sa.UUID(), nullable=False),
    sa.Column('campaign_id', sa.UUID(), nullable=False),
    sa.Column('revision_id', sa.UUID(), nullable=True),
    sa.Column('media_cents', sa.BigInteger(), nullable=False),
    sa.Column('fees_cents', sa.BigInteger(), nullable=False),
    sa.Column('discount_cents', sa.BigInteger(), nullable=False),
    sa.Column('amount_cents', sa.BigInteger(), nullable=False),
    sa.Column('quote', postgresql.JSONB(astext_type=Text()), nullable=False),
    sa.Column('quote_hash', sa.String(length=64), nullable=False),
    sa.Column('hold_id', sa.UUID(), nullable=True),
    sa.CheckConstraint('amount_cents >= 0', name=op.f('ck_order_items_amount_nonneg')),
    sa.ForeignKeyConstraint(['campaign_id'], ['campaigns.id'], name=op.f('fk_order_items_campaign_id_campaigns'), ondelete='RESTRICT'),
    sa.ForeignKeyConstraint(['order_id'], ['orders.id'], name=op.f('fk_order_items_order_id_orders'), ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['revision_id'], ['campaign_revisions.id'], name=op.f('fk_order_items_revision_id_campaign_revisions'), ondelete='RESTRICT'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_order_items')),
    sa.UniqueConstraint('order_id', 'campaign_id', name='uq_order_items_order_campaign')
    )
    op.create_index('ix_order_items_campaign_id', 'order_items', ['campaign_id'], unique=False)
    op.create_table('booking_screens',
    sa.Column('pattern_id', sa.UUID(), nullable=False),
    sa.Column('screen_id', sa.UUID(), nullable=False),
    sa.Column('hold_id', sa.UUID(), nullable=False),
    sa.Column('campaign_id', sa.UUID(), nullable=False),
    sa.Column('status', sa.String(length=16), nullable=False),
    sa.Column('hold_expires_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('conflict_decision_id', sa.UUID(), nullable=True),
    sa.Column('conflict_result', sa.String(length=16), nullable=False),
    sa.Column('conflict_reason', sa.String(length=64), nullable=True),
    sa.Column('tz_snapshot', sa.String(length=64), nullable=False),
    sa.Column('policy_id_snapshot', sa.UUID(), nullable=True),
    sa.Column('rotation_snapshot', postgresql.JSONB(astext_type=Text()), nullable=False),
    sa.Column('status_reason', sa.String(length=64), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.CheckConstraint("status <> 'HELD' OR hold_expires_at IS NOT NULL", name=op.f('ck_booking_screens_held_has_expiry')),
    sa.CheckConstraint("status IN ('HELD', 'BOOKED', 'EXPIRED', 'CANCELLED', 'SUSPENDED')", name=op.f('ck_booking_screens_status')),
    sa.ForeignKeyConstraint(['campaign_id'], ['campaigns.id'], name=op.f('fk_booking_screens_campaign_id_campaigns'), ondelete='RESTRICT'),
    sa.ForeignKeyConstraint(['hold_id'], ['inventory_holds.id'], name=op.f('fk_booking_screens_hold_id_inventory_holds'), ondelete='RESTRICT'),
    sa.ForeignKeyConstraint(['pattern_id'], ['booking_patterns.id'], name=op.f('fk_booking_screens_pattern_id_booking_patterns'), ondelete='RESTRICT'),
    sa.ForeignKeyConstraint(['policy_id_snapshot'], ['rotation_policies.id'], name=op.f('fk_booking_screens_policy_id_snapshot_rotation_policies'), ondelete='RESTRICT'),
    sa.ForeignKeyConstraint(['screen_id'], ['screens.id'], name=op.f('fk_booking_screens_screen_id_screens'), ondelete='RESTRICT'),
    sa.PrimaryKeyConstraint('pattern_id', 'screen_id', name='pk_booking_screens')
    )
    op.create_index('ix_booking_screens_active', 'booking_screens', ['screen_id'], unique=False, postgresql_where=sa.text("status IN ('HELD','BOOKED')"))
    op.create_index('ix_booking_screens_campaign', 'booking_screens', ['campaign_id'], unique=False)
    op.create_index('ix_booking_screens_hold', 'booking_screens', ['hold_id'], unique=False)
    op.create_table('maintenance_events',
    sa.Column('id', sa.UUID(), nullable=False),
    sa.Column('order_id', sa.UUID(), nullable=False),
    sa.Column('event', sa.String(length=16), nullable=False),
    sa.Column('from_status', sa.String(length=14), nullable=True),
    sa.Column('to_status', sa.String(length=14), nullable=True),
    sa.Column('technician_user_id', sa.UUID(), nullable=True),
    sa.Column('note', sa.Text(), nullable=True),
    sa.Column('actor_user_id', sa.UUID(), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('clock_timestamp()'), nullable=False),
    sa.CheckConstraint("event IN ('OPENED','ASSIGNED','STATUS_CHANGED','NOTE','RESOLVED','CANCELLED')", name=op.f('ck_maintenance_events_event_valid')),
    sa.ForeignKeyConstraint(['actor_user_id'], ['users.id'], name=op.f('fk_maintenance_events_actor_user_id_users'), ondelete='SET NULL'),
    sa.ForeignKeyConstraint(['order_id'], ['maintenance_orders.id'], name=op.f('fk_maintenance_events_order_id_maintenance_orders'), ondelete='RESTRICT'),
    sa.ForeignKeyConstraint(['technician_user_id'], ['users.id'], name=op.f('fk_maintenance_events_technician_user_id_users'), ondelete='SET NULL'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_maintenance_events'))
    )
    op.create_index('ix_maintenance_events_order_id', 'maintenance_events', ['order_id'], unique=False)


# ─── 3. tabelas existentes alteradas no lugar (com migração de dados) ────────

def _upgrade_organizations() -> None:
    op.add_column("organizations", sa.Column("business_roles", postgresql.ARRAY(String(length=20)), server_default=sa.text("'{}'::varchar[]"), nullable=False))
    for col in (
        sa.Column("responsible_name", sa.String(length=255), nullable=True),
        sa.Column("address", sa.String(length=255), nullable=True),
        sa.Column("number", sa.String(length=20), nullable=True),
        sa.Column("complement", sa.String(length=100), nullable=True),
        sa.Column("neighborhood", sa.String(length=100), nullable=True),
        sa.Column("city", sa.String(length=100), nullable=True),
        sa.Column("state", sa.String(length=2), nullable=True),
        sa.Column("postal_code", sa.String(length=8), nullable=True),
        sa.Column("city_key", sa.String(length=120), nullable=True),
        sa.Column("deactivated_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("deactivation_reason", sa.Text(), nullable=True),
        sa.Column("kyc_status", sa.String(length=10), server_default=sa.text("'PENDING'"), nullable=False),
        sa.Column("kyc_reviewed_by", sa.UUID(), nullable=True),
        sa.Column("kyc_reviewed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("kyc_notes", sa.Text(), nullable=True),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.Column("created_by", sa.UUID(), nullable=True),
    ):
        op.add_column("organizations", col)
    # bitmask legado: 1 → ADVERTISER; 2 ou 4 → PARTNER (shim OrganizationTypeFlag do W-ORG)
    for r in _rows("SELECT id, name, type FROM organizations ORDER BY created_at, id"):
        roles = (["ADVERTISER"] if r["type"] & 1 else []) + (["PARTNER"] if r["type"] & 6 else [])
        _exec("UPDATE organizations SET business_roles = CAST(:roles AS varchar[]) WHERE id = :id",
              roles="{" + ",".join(roles) + "}", id=r["id"])
        _audit("migration.0004.organization_business_roles", "organization", r["id"], org_id=r["id"],
               before={"type": r["type"]}, after={"business_roles": roles},
               reason="0004: bitmask organizations.type substituído por business_roles (W-ORG)")
    op.create_index("ix_organizations_business_roles", "organizations", ["business_roles"], unique=False, postgresql_using="gin")
    op.create_index("ix_organizations_city_key", "organizations", ["city_key"], unique=False)
    op.create_foreign_key(op.f("fk_organizations_created_by_users"), "organizations", "users", ["created_by"], ["id"], ondelete="SET NULL")
    op.create_foreign_key(op.f("fk_organizations_kyc_reviewed_by_users"), "organizations", "users", ["kyc_reviewed_by"], ["id"], ondelete="SET NULL")
    op.drop_column("organizations", "type")


def _upgrade_establishments() -> None:
    for col in (
        sa.Column("document_type", sa.String(length=4), nullable=True),
        sa.Column("neighborhood_key", sa.String(length=120), nullable=True),
        sa.Column("city_key", sa.String(length=120), nullable=True),
        sa.Column("city_ibge_code", sa.String(length=7), nullable=True),
        sa.Column("region", sa.String(length=12), nullable=True),
        sa.Column("geo_source", sa.String(length=10), nullable=True),
        sa.Column("geo_accuracy_m", sa.Integer(), nullable=True),
        sa.Column("geo_captured_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("timezone_source", sa.String(length=10), server_default=sa.text("'AUTO'"), nullable=False),
        sa.Column("venue_class", sa.String(length=10), server_default=sa.text("'STANDARD'"), nullable=False),
        sa.Column("weight_milli", sa.Integer(), server_default=sa.text("1000"), nullable=False),
        sa.Column("estimated_daily_visitors", sa.Integer(), nullable=True),
        sa.Column("audience_method", sa.String(length=15), nullable=True),
        sa.Column("audience_updated_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("partner_plan_id", sa.UUID(), nullable=True),
        sa.Column("receives_commission", sa.Boolean(), server_default=sa.text("true"), nullable=False),
        sa.Column("pays_monthly_fee", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("monthly_fee_cents", sa.Integer(), nullable=True),
        sa.Column("onboarding_status", sa.String(length=20), server_default=sa.text("'ACTIVE'"), nullable=False),
        sa.Column("responsible_technician_id", sa.UUID(), nullable=True),
        sa.Column("installation_date", sa.Date(), nullable=True),
        sa.Column("deactivated_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("deactivation_reason", sa.Text(), nullable=True),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.Column("created_by", sa.UUID(), nullable=True),
    ):
        op.add_column("establishments", col)
    # city_key (escopo CITY), região IBGE e CEP com 8 dígitos (inválido → NULL)
    for r in _rows("SELECT id, org_id, city, state, postal_code FROM establishments ORDER BY created_at, id"):
        uf = r["state"].strip().upper()
        digits = re.sub(r"\D", "", r["postal_code"] or "")
        cep = digits if len(digits) == 8 else None
        after = {"city_key": _city_key(uf, r["city"]), "region": UF_REGION[uf], "state": uf, "postal_code": cep}
        _exec("UPDATE establishments SET state = :uf, city_key = :ck, region = :rg, postal_code = :cep WHERE id = :id",
              uf=uf, ck=after["city_key"], rg=after["region"], cep=cep, id=r["id"])
        _audit("migration.0004.establishment_geo", "establishment", r["id"], org_id=r["org_id"],
               before={"state": r["state"], "postal_code": r["postal_code"]}, after=after,
               reason="0004: city_key/region derivados de cidade/UF; CEP normalizado (W-ORG)")
    op.alter_column("establishments", "city_key", existing_type=sa.String(length=120), nullable=False)
    op.alter_column("establishments", "region", existing_type=sa.String(length=12), nullable=False)
    op.alter_column("establishments", "city", existing_type=sa.VARCHAR(length=100), nullable=False)
    op.alter_column("establishments", "state", existing_type=sa.VARCHAR(length=2), nullable=False)
    op.alter_column("establishments", "country", existing_type=sa.VARCHAR(length=3), server_default=sa.text("'BRA'"), existing_nullable=False)
    op.alter_column("establishments", "postal_code", existing_type=sa.VARCHAR(length=10), type_=sa.String(length=8), existing_nullable=True)
    op.alter_column("establishments", "timezone", existing_type=sa.VARCHAR(length=50), type_=sa.String(length=64), existing_nullable=False)
    op.create_index("ix_establishments_city_key", "establishments", ["city_key"], unique=False)
    op.create_index("ix_establishments_partner_plan_id", "establishments", ["partner_plan_id"], unique=False)
    op.create_index("ix_establishments_region", "establishments", ["region"], unique=False)
    op.create_index("ix_establishments_state", "establishments", ["state"], unique=False)
    op.create_foreign_key(op.f("fk_establishments_partner_plan_id_partner_plans"), "establishments", "partner_plans", ["partner_plan_id"], ["id"], ondelete="RESTRICT")
    # category_id: establishment_categories estava vazia ⇒ todos NULL; FK passa para taxonomy_nodes
    op.create_foreign_key(op.f("fk_establishments_category_id_taxonomy_nodes"), "establishments", "taxonomy_nodes", ["category_id"], ["id"], ondelete="RESTRICT")
    op.create_foreign_key(op.f("fk_establishments_responsible_technician_id_users"), "establishments", "users", ["responsible_technician_id"], ["id"], ondelete="SET NULL")
    op.create_foreign_key(op.f("fk_establishments_created_by_users"), "establishments", "users", ["created_by"], ["id"], ondelete="SET NULL")


def _upgrade_screens() -> None:
    for col in (
        sa.Column("sellable", sa.Boolean(), server_default=sa.text("true"), nullable=False),
        sa.Column("decommissioned_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("decommission_reason", sa.Text(), nullable=True),
        sa.Column("display_type", sa.String(length=12), server_default=sa.text("'TV'"), nullable=False),
        sa.Column("player_type", sa.String(length=14), server_default=sa.text("'TVBOX'"), nullable=False),
        sa.Column("player_brand", sa.String(length=100), nullable=True),
        sa.Column("player_model", sa.String(length=100), nullable=True),
        sa.Column("player_serial_number", sa.String(length=100), nullable=True),
        sa.Column("installed_by", sa.UUID(), nullable=True),
        sa.Column("screen_class", sa.String(length=10), server_default=sa.text("'STANDARD'"), nullable=False),
        sa.Column("weight_milli", sa.Integer(), server_default=sa.text("1000"), nullable=False),
        sa.Column("programming_mode", sa.String(length=24), server_default=sa.text("'ESTABLISHMENT_DEFAULT'"), nullable=False),
        sa.Column("estimated_daily_audience", sa.Integer(), nullable=True),
        sa.Column("audience_method", sa.String(length=15), nullable=True),
        sa.Column("audience_updated_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_by", sa.UUID(), nullable=True),
    ):
        op.add_column("screens", col)
    rows = _rows("SELECT s.id, s.status, s.orientation, s.tv_owner, s.inches, s.estimated_audience, "
                 "s.comodato_config, s.updated_at, e.org_id FROM screens s JOIN establishments e "
                 "ON e.id = s.establishment_id ORDER BY s.created_at, s.id")
    # status de conectividade legado (PAIRED/UNPAIRED/ONLINE/OFFLINE...) → ciclo de vida ACTIVE
    _exec("UPDATE screens SET status = CASE WHEN upper(status) IN ('MAINTENANCE','INACTIVE','DECOMMISSIONED') "
          "THEN upper(status) ELSE 'ACTIVE' END, orientation = upper(orientation), tv_owner = upper(tv_owner)")
    _exec("UPDATE screens SET decommissioned_at = updated_at, sellable = false WHERE status = 'DECOMMISSIONED'")
    _exec("UPDATE screens SET estimated_daily_audience = estimated_audience, audience_method = 'MANUAL', "
          "audience_updated_at = now() WHERE estimated_audience IS NOT NULL")
    op.alter_column("screens", "status", existing_type=sa.VARCHAR(length=20), server_default=sa.text("'ACTIVE'"), existing_nullable=False)
    op.alter_column("screens", "inches", existing_type=sa.DOUBLE_PRECISION(precision=53), type_=sa.Integer(), existing_nullable=True,
                    postgresql_using="round(inches)::integer")
    for r in rows:
        after = dict(_rows("SELECT status, orientation, tv_owner, inches, estimated_daily_audience, sellable "
                           "FROM screens WHERE id = :id", id=r["id"])[0])
        _audit("migration.0004.screen", "screen", r["id"], org_id=r["org_id"],
               before={"status": r["status"], "orientation": r["orientation"], "tv_owner": r["tv_owner"],
                       "inches": r["inches"], "estimated_audience": r["estimated_audience"],
                       "comodato_config": r["comodato_config"]},
               after=after, reason="0004: status de conectividade legado → ciclo de vida (presença é do W-PLAY); "
                                   "orientação maiúscula; polegadas inteiras; comodato_config removido (W-ORG)")
    op.create_index("ix_screens_sellable", "screens", ["sellable"], unique=False)
    op.create_foreign_key(op.f("fk_screens_created_by_users"), "screens", "users", ["created_by"], ["id"], ondelete="SET NULL")
    op.create_foreign_key(op.f("fk_screens_installed_by_users"), "screens", "users", ["installed_by"], ["id"], ondelete="SET NULL")
    op.drop_column("screens", "comodato_config")
    op.drop_column("screens", "estimated_audience")


def _upgrade_pairing_and_devices() -> None:
    # pairing_requests legados: efêmeros (TTL de minutos), sem chave pública → descartados (W-PLAY)
    n_pairing = _count("pairing_requests")
    _exec("DELETE FROM pairing_requests")
    if n_pairing:
        _audit("migration.0004.pairing_requests_discarded", "pairing_request", None,
               after={"deleted": n_pairing},
               reason="0004: pedidos de pareamento do protocolo legado (sem chave pública) descartados (W-PLAY)")
    op.add_column("pairing_requests", sa.Column("public_key", sa.Text(), nullable=False))
    op.add_column("pairing_requests", sa.Column("key_alg", sa.String(length=10), nullable=False))
    op.add_column("pairing_requests", sa.Column("key_fingerprint", sa.String(length=64), nullable=False))
    op.add_column("pairing_requests", sa.Column("platform", sa.String(length=20), nullable=False))
    op.add_column("pairing_requests", sa.Column("app_version", sa.String(length=40), nullable=True))
    op.add_column("pairing_requests", sa.Column("device_info", postgresql.JSONB(astext_type=Text()), nullable=True))
    op.add_column("pairing_requests", sa.Column("status", sa.String(length=20), nullable=False))
    op.add_column("pairing_requests", sa.Column("claimed_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("pairing_requests", sa.Column("claim_method", sa.String(length=20), nullable=True))
    op.add_column("pairing_requests", sa.Column("claimed_by_user_id", sa.UUID(), nullable=True))
    op.add_column("pairing_requests", sa.Column("screen_id", sa.UUID(), nullable=True))
    op.add_column("pairing_requests", sa.Column("result_fetched_at", sa.DateTime(timezone=True), nullable=True))
    op.alter_column("pairing_requests", "pairing_code", existing_type=sa.VARCHAR(length=20), type_=sa.String(length=9), existing_nullable=False)
    op.alter_column("pairing_requests", "created_at", existing_type=postgresql.TIMESTAMP(timezone=True), server_default=sa.text("now()"), existing_nullable=False)
    op.drop_index("ix_pairing_requests_establishment_id", table_name="pairing_requests")
    op.drop_index("ix_pairing_requests_pairing_code", table_name="pairing_requests")
    op.drop_index("ix_pairing_requests_qr_token", table_name="pairing_requests")
    op.drop_constraint("uq_pairing_requests_qr_token", "pairing_requests", type_="unique")
    op.create_index("ix_pairing_requests_status_expires", "pairing_requests", ["status", "expires_at"], unique=False)
    op.create_unique_constraint(op.f("uq_pairing_requests_pairing_code"), "pairing_requests", ["pairing_code"])
    op.drop_constraint("fk_pairing_requests_device_id_devices", "pairing_requests", type_="foreignkey")
    op.drop_constraint("fk_pairing_requests_establishment_id_establishments", "pairing_requests", type_="foreignkey")
    op.create_foreign_key(op.f("fk_pairing_requests_screen_id_screens"), "pairing_requests", "screens", ["screen_id"], ["id"], ondelete="SET NULL")
    op.create_foreign_key(op.f("fk_pairing_requests_claimed_by_user_id_users"), "pairing_requests", "users", ["claimed_by_user_id"], ["id"], ondelete="SET NULL")
    op.create_foreign_key(op.f("fk_pairing_requests_device_id_devices"), "pairing_requests", "devices", ["device_id"], ["id"], ondelete="SET NULL")
    op.drop_column("pairing_requests", "used_at")
    op.drop_column("pairing_requests", "qr_token")
    op.drop_column("pairing_requests", "establishment_id")

    # devices legados (token/segredo em claro, sem chave pública): NÃO migráveis (ADR_007/W-PLAY).
    # Mantidos como REVOGADOS (histórico preservado; re-parear = novo device_id); segredos descartados.
    for col in (
        sa.Column("public_key", sa.Text(), nullable=True),
        sa.Column("key_alg", sa.String(length=10), nullable=True),
        sa.Column("key_fingerprint", sa.String(length=64), nullable=True),
        sa.Column("device_info", postgresql.JSONB(astext_type=Text()), nullable=True),
        sa.Column("update_group", sa.String(length=40), nullable=True),
        sa.Column("status", sa.String(length=20), nullable=True),
        sa.Column("pairing_method", sa.String(length=20), nullable=True),
        sa.Column("paired_by_user_id", sa.UUID(), nullable=True),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("revoked_reason", sa.String(length=255), nullable=True),
        sa.Column("revoked_by_user_id", sa.UUID(), nullable=True),
        sa.Column("presence", sa.String(length=20), nullable=True),
        sa.Column("player_state", sa.String(length=20), nullable=True),
        sa.Column("last_play_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("applied_manifest_version", sa.Integer(), nullable=True),
        sa.Column("pending_manifest_version", sa.Integer(), nullable=True),
        sa.Column("last_error", sa.String(length=255), nullable=True),
        sa.Column("telemetry_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("pop_last_seq", sa.BigInteger(), nullable=True),
        sa.Column("pop_last_hash", sa.String(length=64), nullable=True),
    ):
        op.add_column("devices", col)
    legacy = _rows("SELECT d.id, d.screen_id, d.platform, d.is_active, d.paired_at, d.created_at, e.org_id "
                   "FROM devices d LEFT JOIN screens s ON s.id = d.screen_id "
                   "LEFT JOIN establishments e ON e.id = s.establishment_id ORDER BY d.created_at, d.id")
    for r in legacy:
        before = {"screen_id": r["screen_id"], "platform": r["platform"], "is_active": r["is_active"],
                  "paired_at": r["paired_at"]}  # sem token/segredo
        if r["screen_id"] is None:
            # sem tela não há onde manter (screen_id passa a NOT NULL): removido
            _exec("UPDATE screens SET device_id = NULL WHERE device_id = :id", id=r["id"])
            _exec("DELETE FROM devices WHERE id = :id", id=r["id"])
            _audit("migration.0004.device_discarded", "device", r["id"], before=before,
                   reason="0004: dispositivo legado sem tela e sem chave pública descartado (W-PLAY)")
            continue
        _audit("migration.0004.device_revoked", "device", r["id"], org_id=r["org_id"], before=before,
               after={"status": "REVOKED", "secrets": "descartados"},
               reason="0004: dispositivo do protocolo legado (token/segredo em claro, sem chave pública) "
                      "revogado — re-parear pelo RDPP/1 (W-PLAY)")
    _exec(
        "UPDATE devices SET status = 'REVOKED', revoked_at = now(), "
        "revoked_reason = 'legacy_0004: protocolo legado (token/segredo em claro, sem chave pública); re-parear', "
        "public_key = '', key_alg = 'LEGACY', key_fingerprint = 'legacy0004-' || md5(id::text), "
        "pairing_method = 'PANEL', presence = 'offline', platform = lower(platform), "
        "paired_at = COALESCE(paired_at, created_at), updated_at = COALESCE(updated_at, now())"
    )
    _exec("UPDATE screens SET device_id = NULL WHERE device_id IN (SELECT id FROM devices WHERE status = 'REVOKED')")
    for c in ("public_key", "key_alg", "key_fingerprint", "status", "pairing_method", "presence"):
        op.alter_column("devices", c, nullable=False)
    op.alter_column("devices", "screen_id", existing_type=sa.UUID(), nullable=False)
    op.alter_column("devices", "app_version", existing_type=sa.VARCHAR(length=30), type_=sa.String(length=40), existing_nullable=True)
    op.alter_column("devices", "os_version", existing_type=sa.VARCHAR(length=50), type_=sa.String(length=80), existing_nullable=True)
    op.alter_column("devices", "paired_at", existing_type=postgresql.TIMESTAMP(timezone=True), nullable=False)
    op.alter_column("devices", "created_at", existing_type=postgresql.TIMESTAMP(timezone=True), server_default=sa.text("now()"), existing_nullable=False)
    op.alter_column("devices", "updated_at", existing_type=postgresql.TIMESTAMP(timezone=True), server_default=sa.text("now()"), nullable=False)
    op.drop_index("ix_devices_device_token", table_name="devices")
    op.drop_constraint("uq_devices_device_token", "devices", type_="unique")
    op.create_index("ix_devices_status", "devices", ["status"], unique=False)
    op.create_index("uq_devices_active_screen", "devices", ["screen_id"], unique=True, postgresql_where=sa.text("status = 'ACTIVE'"))
    op.create_unique_constraint(op.f("uq_devices_key_fingerprint"), "devices", ["key_fingerprint"])
    op.drop_constraint("fk_devices_screen_id_screens", "devices", type_="foreignkey")
    op.create_foreign_key(op.f("fk_devices_revoked_by_user_id_users"), "devices", "users", ["revoked_by_user_id"], ["id"], ondelete="SET NULL")
    op.create_foreign_key(op.f("fk_devices_paired_by_user_id_users"), "devices", "users", ["paired_by_user_id"], ["id"], ondelete="SET NULL")
    op.create_foreign_key(op.f("fk_devices_screen_id_screens"), "devices", "screens", ["screen_id"], ["id"], ondelete="RESTRICT")
    for c in ("device_secret", "device_token", "ip_address", "ssid", "is_active", "cycle_anchor"):
        op.drop_column("devices", c)


def _upgrade_media_and_equipment() -> None:
    """media_files / equipment_items / equipment_movements: VAZIAS (guarda) — ops do autogenerate."""
    op.add_column('equipment_items', sa.Column('tracking', sa.String(length=8), nullable=False))
    op.add_column('equipment_items', sa.Column('name', sa.String(length=160), nullable=False))
    op.add_column('equipment_items', sa.Column('inches', sa.Integer(), nullable=True))
    op.add_column('equipment_items', sa.Column('unit', sa.String(length=10), server_default=sa.text("'UN'"), nullable=False))
    op.add_column('equipment_items', sa.Column('condition', sa.String(length=10), server_default=sa.text("'NEW'"), nullable=False))
    op.add_column('equipment_items', sa.Column('supplier', sa.String(length=160), nullable=True))
    op.add_column('equipment_items', sa.Column('purchase_date', sa.Date(), nullable=True))
    op.add_column('equipment_items', sa.Column('purchase_cost_cents', sa.Integer(), nullable=True))
    op.add_column('equipment_items', sa.Column('location_kind', sa.String(length=14), nullable=True))
    op.add_column('equipment_items', sa.Column('location_city_key', sa.String(length=120), nullable=True))
    op.add_column('equipment_items', sa.Column('location_establishment_id', sa.UUID(), nullable=True))
    op.add_column('equipment_items', sa.Column('location_screen_id', sa.UUID(), nullable=True))
    op.add_column('equipment_items', sa.Column('location_user_id', sa.UUID(), nullable=True))
    op.add_column('equipment_items', sa.Column('scope_org_id', sa.UUID(), nullable=True))
    op.add_column('equipment_items', sa.Column('comodato_agreement_id', sa.UUID(), nullable=True))
    op.add_column('equipment_items', sa.Column('expected_return_at', sa.Date(), nullable=True))
    op.add_column('equipment_items', sa.Column('movement_count', sa.Integer(), server_default=sa.text('0'), nullable=False))
    op.add_column('equipment_items', sa.Column('last_movement_at', sa.DateTime(timezone=True), nullable=True))
    op.add_column('equipment_items', sa.Column('created_by', sa.UUID(), nullable=True))
    op.add_column('equipment_items', sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False))
    op.alter_column('equipment_items', 'type',
               existing_type=sa.VARCHAR(length=20),
               type_=sa.String(length=12),
               existing_nullable=False)
    op.alter_column('equipment_items', 'status',
               existing_type=sa.VARCHAR(length=20),
               type_=sa.String(length=16),
               existing_nullable=False)
    op.alter_column('equipment_items', 'created_at',
               existing_type=postgresql.TIMESTAMP(timezone=True),
               server_default=sa.text('now()'),
               existing_nullable=False)
    op.drop_index('ix_equipment_items_asset_tag', table_name='equipment_items')
    op.drop_index('ix_equipment_items_org_id', table_name='equipment_items')
    op.drop_index('ix_equipment_items_screen_id', table_name='equipment_items')
    op.drop_index('ix_equipment_items_serial_number', table_name='equipment_items')
    op.create_index('ix_equipment_items_location_city_key', 'equipment_items', ['location_city_key'], unique=False)
    op.create_index('ix_equipment_items_location_establishment_id', 'equipment_items', ['location_establishment_id'], unique=False)
    op.create_index('ix_equipment_items_location_screen_id', 'equipment_items', ['location_screen_id'], unique=False)
    op.create_index('ix_equipment_items_scope_org_id', 'equipment_items', ['scope_org_id'], unique=False)
    op.create_index('ix_equipment_items_type', 'equipment_items', ['type'], unique=False)
    op.create_index('uq_equipment_items_asset_tag', 'equipment_items', ['asset_tag'], unique=True, postgresql_where=sa.text('asset_tag IS NOT NULL'))
    op.create_index('uq_equipment_items_type_serial', 'equipment_items', ['type', 'serial_number'], unique=True, postgresql_where=sa.text('serial_number IS NOT NULL'))
    op.drop_constraint('fk_equipment_items_org_id_organizations', 'equipment_items', type_='foreignkey')
    op.drop_constraint('fk_equipment_items_screen_id_screens', 'equipment_items', type_='foreignkey')
    op.create_foreign_key(op.f('fk_equipment_items_scope_org_id_organizations'), 'equipment_items', 'organizations', ['scope_org_id'], ['id'], ondelete='RESTRICT')
    op.create_foreign_key(op.f('fk_equipment_items_comodato_agreement_id_comodato_agreements'), 'equipment_items', 'comodato_agreements', ['comodato_agreement_id'], ['id'], ondelete='RESTRICT')
    op.create_foreign_key(op.f('fk_equipment_items_location_screen_id_screens'), 'equipment_items', 'screens', ['location_screen_id'], ['id'], ondelete='RESTRICT')
    op.create_foreign_key(op.f('fk_equipment_items_created_by_users'), 'equipment_items', 'users', ['created_by'], ['id'], ondelete='SET NULL')
    op.create_foreign_key(op.f('fk_equipment_items_location_establishment_id_establishments'), 'equipment_items', 'establishments', ['location_establishment_id'], ['id'], ondelete='RESTRICT')
    op.create_foreign_key(op.f('fk_equipment_items_location_user_id_users'), 'equipment_items', 'users', ['location_user_id'], ['id'], ondelete='RESTRICT')
    op.drop_column('equipment_items', 'purchased_at')
    op.drop_column('equipment_items', 'screen_id')
    op.drop_column('equipment_items', 'org_id')
    op.add_column('equipment_movements', sa.Column('seq', sa.BigInteger(), sa.Identity(always=True), nullable=False))
    op.add_column('equipment_movements', sa.Column('group_id', sa.UUID(), nullable=True))
    op.add_column('equipment_movements', sa.Column('movement_type', sa.String(length=16), nullable=False))
    op.add_column('equipment_movements', sa.Column('quantity', sa.Integer(), nullable=False))
    op.add_column('equipment_movements', sa.Column('from_kind', sa.String(length=14), nullable=False))
    op.add_column('equipment_movements', sa.Column('from_ref_id', sa.UUID(), nullable=True))
    op.add_column('equipment_movements', sa.Column('from_city_key', sa.String(length=120), nullable=True))
    op.add_column('equipment_movements', sa.Column('to_kind', sa.String(length=14), nullable=False))
    op.add_column('equipment_movements', sa.Column('to_ref_id', sa.UUID(), nullable=True))
    op.add_column('equipment_movements', sa.Column('to_city_key', sa.String(length=120), nullable=True))
    op.add_column('equipment_movements', sa.Column('establishment_id', sa.UUID(), nullable=True))
    op.add_column('equipment_movements', sa.Column('screen_id', sa.UUID(), nullable=True))
    op.add_column('equipment_movements', sa.Column('org_id', sa.UUID(), nullable=True))
    op.add_column('equipment_movements', sa.Column('comodato_agreement_id', sa.UUID(), nullable=True))
    op.add_column('equipment_movements', sa.Column('replaced_item_id', sa.UUID(), nullable=True))
    op.add_column('equipment_movements', sa.Column('reference', sa.String(length=120), nullable=True))
    op.add_column('equipment_movements', sa.Column('expected_return_at', sa.Date(), nullable=True))
    op.alter_column('equipment_movements', 'from_status',
               existing_type=sa.VARCHAR(length=20),
               type_=sa.String(length=16),
               existing_nullable=True)
    op.alter_column('equipment_movements', 'to_status',
               existing_type=sa.VARCHAR(length=20),
               type_=sa.String(length=16),
               nullable=True)
    op.alter_column('equipment_movements', 'performed_by',
               existing_type=sa.UUID(),
               nullable=True)
    op.alter_column('equipment_movements', 'created_at',
               existing_type=postgresql.TIMESTAMP(timezone=True),
               server_default=sa.text('clock_timestamp()'),
               existing_nullable=False)
    op.drop_index('ix_equipment_movements_item_id', table_name='equipment_movements')
    op.create_index('ix_equipment_movements_establishment_id', 'equipment_movements', ['establishment_id'], unique=False)
    op.create_index('ix_equipment_movements_group_id', 'equipment_movements', ['group_id'], unique=False)
    op.create_index('ix_equipment_movements_item_id_seq', 'equipment_movements', ['item_id', 'seq'], unique=False)
    op.create_index('ix_equipment_movements_screen_id', 'equipment_movements', ['screen_id'], unique=False)
    op.create_unique_constraint(op.f('uq_equipment_movements_seq'), 'equipment_movements', ['seq'])
    op.drop_constraint('fk_equipment_movements_item_id_equipment_items', 'equipment_movements', type_='foreignkey')
    op.create_foreign_key(op.f('fk_equipment_movements_org_id_organizations'), 'equipment_movements', 'organizations', ['org_id'], ['id'], ondelete='RESTRICT')
    op.create_foreign_key(op.f('fk_equipment_movements_item_id_equipment_items'), 'equipment_movements', 'equipment_items', ['item_id'], ['id'], ondelete='RESTRICT')
    op.create_foreign_key(op.f('fk_equipment_movements_replaced_item_id_equipment_items'), 'equipment_movements', 'equipment_items', ['replaced_item_id'], ['id'], ondelete='RESTRICT')
    op.create_foreign_key(op.f('fk_equipment_movements_comodato_agreement_id_comodato_a_6462'), 'equipment_movements', 'comodato_agreements', ['comodato_agreement_id'], ['id'], ondelete='RESTRICT')
    op.create_foreign_key(op.f('fk_equipment_movements_establishment_id_establishments'), 'equipment_movements', 'establishments', ['establishment_id'], ['id'], ondelete='RESTRICT')
    op.create_foreign_key(op.f('fk_equipment_movements_screen_id_screens'), 'equipment_movements', 'screens', ['screen_id'], ['id'], ondelete='RESTRICT')
    op.add_column('media_files', sa.Column('origin', sa.String(length=30), server_default='UPLOAD', nullable=False))
    op.add_column('media_files', sa.Column('name', sa.String(length=255), nullable=False))
    op.add_column('media_files', sa.Column('original_extension', sa.String(length=16), nullable=True))
    op.add_column('media_files', sa.Column('declared_mime', sa.String(length=100), nullable=True))
    op.add_column('media_files', sa.Column('extension_mismatch', sa.Boolean(), server_default=sa.text('false'), nullable=False))
    op.add_column('media_files', sa.Column('kind', sa.String(length=10), nullable=False))
    op.add_column('media_files', sa.Column('source_format', sa.String(length=20), nullable=False))
    op.add_column('media_files', sa.Column('source_mime', sa.String(length=100), nullable=False))
    op.add_column('media_files', sa.Column('source_size_bytes', sa.BigInteger(), nullable=False))
    op.add_column('media_files', sa.Column('source_sha256', sa.String(length=64), nullable=False))
    op.add_column('media_files', sa.Column('source_storage_key', sa.String(length=512), nullable=True))
    op.add_column('media_files', sa.Column('source_width', sa.Integer(), nullable=True))
    op.add_column('media_files', sa.Column('source_height', sa.Integer(), nullable=True))
    op.add_column('media_files', sa.Column('source_duration_ms', sa.BigInteger(), nullable=True))
    op.add_column('media_files', sa.Column('source_codec', sa.String(length=32), nullable=True))
    op.add_column('media_files', sa.Column('source_fps_milli', sa.Integer(), nullable=True))
    op.add_column('media_files', sa.Column('source_has_audio', sa.Boolean(), nullable=True))
    op.add_column('media_files', sa.Column('source_metadata', postgresql.JSONB(astext_type=Text()), nullable=True))
    op.add_column('media_files', sa.Column('status_code', sa.String(length=64), nullable=True))
    op.add_column('media_files', sa.Column('status_message', sa.Text(), nullable=True))
    op.add_column('media_files', sa.Column('progress_pct', sa.SmallInteger(), server_default='0', nullable=False))
    op.add_column('media_files', sa.Column('attempts', sa.Integer(), server_default='0', nullable=False))
    op.add_column('media_files', sa.Column('lease_until', sa.DateTime(timezone=True), nullable=True))
    op.add_column('media_files', sa.Column('ready_at', sa.DateTime(timezone=True), nullable=True))
    op.add_column('media_files', sa.Column('storage_key', sa.String(length=512), nullable=True))
    op.add_column('media_files', sa.Column('sha256', sa.String(length=64), nullable=True))
    op.add_column('media_files', sa.Column('duration_ms', sa.BigInteger(), nullable=True))
    op.add_column('media_files', sa.Column('codec', sa.String(length=32), nullable=True))
    op.add_column('media_files', sa.Column('fps_milli', sa.Integer(), nullable=True))
    op.add_column('media_files', sa.Column('animated', sa.Boolean(), server_default=sa.text('false'), nullable=False))
    op.add_column('media_files', sa.Column('frame_count', sa.Integer(), nullable=True))
    op.add_column('media_files', sa.Column('preview_storage_key', sa.String(length=512), nullable=True))
    op.add_column('media_files', sa.Column('preview_mime', sa.String(length=100), nullable=True))
    op.add_column('media_files', sa.Column('preview_size_bytes', sa.BigInteger(), nullable=True))
    op.add_column('media_files', sa.Column('deleted_at', sa.DateTime(timezone=True), nullable=True))
    op.add_column('media_files', sa.Column('deleted_by', sa.UUID(), nullable=True))
    op.add_column('media_files', sa.Column('purged_at', sa.DateTime(timezone=True), nullable=True))
    op.alter_column('media_files', 'created_by',
               existing_type=sa.UUID(),
               nullable=True)
    op.alter_column('media_files', 'original_filename',
               existing_type=sa.VARCHAR(length=500),
               type_=sa.String(length=255),
               existing_nullable=False)
    op.alter_column('media_files', 'status',
               existing_type=sa.VARCHAR(length=20),
               server_default='UPLOADED',
               existing_nullable=False)
    op.alter_column('media_files', 'mime_type',
               existing_type=sa.VARCHAR(length=100),
               nullable=True)
    op.alter_column('media_files', 'size_bytes',
               existing_type=sa.INTEGER(),
               type_=sa.BigInteger(),
               nullable=True)
    op.alter_column('media_files', 'orientation',
               existing_type=sa.VARCHAR(length=20),
               type_=sa.String(length=12),
               existing_nullable=True)
    op.drop_index('ix_media_files_checksum', table_name='media_files')
    op.drop_index('ix_media_files_org_id', table_name='media_files')
    op.create_index('ix_media_files_org_id_created_at', 'media_files', ['org_id', 'created_at'], unique=False)
    op.create_index('ix_media_files_pending_purge', 'media_files', ['deleted_at'], unique=False, postgresql_where=sa.text('deleted_at IS NOT NULL AND purged_at IS NULL'))
    op.create_index('ix_media_files_sha256', 'media_files', ['sha256'], unique=False)
    op.create_index('uq_media_files_org_id_source_sha256', 'media_files', ['org_id', 'source_sha256'], unique=True, postgresql_where=sa.text('deleted_at IS NULL'))
    op.create_foreign_key(op.f('fk_media_files_deleted_by_users'), 'media_files', 'users', ['deleted_by'], ['id'], ondelete='RESTRICT')
    op.drop_column('media_files', 'has_audio_original')
    op.drop_column('media_files', 'error_message')
    op.drop_column('media_files', 'storage_provider')
    op.drop_column('media_files', 'content_type')
    op.drop_column('media_files', 'storage_path')
    op.drop_column('media_files', 'filename')
    op.drop_column('media_files', 'checksum_sha256')
    op.drop_column('media_files', 'duration_seconds')


def _upgrade_creatives() -> None:
    # código público RD-XXXXXX: RENOMEADO (dados preservados; formato validado na guarda)
    op.drop_index("ix_creatives_creative_id", table_name="creatives")
    op.drop_constraint("uq_creatives_creative_id", "creatives", type_="unique")
    op.alter_column("creatives", "creative_id", existing_type=sa.VARCHAR(length=20), type_=sa.String(length=9), existing_nullable=False)
    op.alter_column("creatives", "creative_id", new_column_name="code", existing_type=sa.String(length=9), existing_nullable=False)
    _add_not_null("creatives", sa.Column("kind", sa.String(length=20)), "'EXTERNAL'")
    _add_not_null("creatives", sa.Column("orientation", sa.String(length=12)), "'HORIZONTAL'")
    op.add_column("creatives", sa.Column("media_duration_ms", sa.Integer(), nullable=True))
    op.add_column("creatives", sa.Column("ad_text", sa.Text(), nullable=True))
    _add_not_null("creatives", sa.Column("technical_status", sa.String(length=10)), "'PENDENTE'")
    op.add_column("creatives", sa.Column("technical_error", sa.String(length=80), nullable=True))
    op.add_column("creatives", sa.Column("product_id", sa.UUID(), nullable=True))
    op.add_column("creatives", sa.Column("brand_id", sa.UUID(), nullable=True))
    _add_not_null("creatives", sa.Column("version", sa.Integer()), "1")
    op.add_column("creatives", sa.Column("supersedes_id", sa.UUID(), nullable=True))
    op.add_column("creatives", sa.Column("root_id", sa.UUID(), nullable=True))
    op.add_column("creatives", sa.Column("created_by", sa.UUID(), nullable=True))
    op.add_column("creatives", sa.Column("archived_at", sa.DateTime(timezone=True), nullable=True))
    n = _count("creatives")
    if n:
        _exec(f"UPDATE creatives SET status = {_case('status', CREATIVE_STATUS_EN_PT, 'upper(status)')}, root_id = id")
        _audit("migration.0004.creatives", "creative", None, after={"migrated": n},
               reason="0004: creative_id → code (preservado), status EN → PT (W-CAMP)")
    op.alter_column("creatives", "duration_seconds", existing_type=sa.INTEGER(), type_=sa.SmallInteger(), existing_nullable=False)
    op.create_index("ix_creatives_media_file_id", "creatives", ["media_file_id"], unique=False)
    op.create_index("ix_creatives_root_id", "creatives", ["root_id"], unique=False)
    op.create_unique_constraint("uq_creatives_code", "creatives", ["code"])
    op.create_foreign_key(op.f("fk_creatives_product_id_products"), "creatives", "products", ["product_id"], ["id"], ondelete="RESTRICT")
    op.create_foreign_key(op.f("fk_creatives_supersedes_id_creatives"), "creatives", "creatives", ["supersedes_id"], ["id"], ondelete="RESTRICT")
    op.create_foreign_key(op.f("fk_creatives_brand_id_brands"), "creatives", "brands", ["brand_id"], ["id"], ondelete="RESTRICT")
    op.create_foreign_key(op.f("fk_creatives_created_by_users"), "creatives", "users", ["created_by"], ["id"], ondelete="RESTRICT")
    op.create_foreign_key(op.f("fk_creatives_root_id_creatives"), "creatives", "creatives", ["root_id"], ["id"], ondelete="RESTRICT")


def _upgrade_campaigns() -> None:
    _add_not_null("campaign_creatives", sa.Column("pending", sa.Boolean()), "false")
    _add_not_null("campaign_creatives", sa.Column("pending_removal", sa.Boolean()), "false")
    op.add_column("campaign_creatives", sa.Column("replaces_creative_id", sa.UUID(), nullable=True))
    op.add_column("campaign_creatives", sa.Column("added_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False))
    op.add_column("campaign_creatives", sa.Column("removed_at", sa.DateTime(timezone=True), nullable=True))
    op.create_foreign_key(op.f("fk_campaign_creatives_replaces_creative_id_creatives"), "campaign_creatives", "creatives", ["replaces_creative_id"], ["id"], ondelete="RESTRICT")

    op.add_column("campaign_targets", sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False))
    _exec("UPDATE campaign_targets SET target_type = upper(target_type)")
    dup = _scalar("SELECT count(*) FROM (SELECT 1 FROM campaign_targets GROUP BY campaign_id, target_type, "
                  "target_value, is_exclude HAVING count(*) > 1) d")
    if dup:  # alvos idênticos repetidos: mantém o primeiro (sem efeito na segmentação)
        _exec("DELETE FROM campaign_targets t USING campaign_targets o WHERE t.campaign_id = o.campaign_id "
              "AND t.target_type = o.target_type AND t.target_value = o.target_value AND t.is_exclude = o.is_exclude "
              "AND t.id > o.id")
        _audit("migration.0004.campaign_targets_dedup", "campaign_target", None, after={"duplicate_groups": dup},
               reason="0004: alvos de campanha duplicados removidos para a UNIQUE nova (W-CAMP)")
    op.drop_index("ix_campaign_targets_target_type_value", table_name="campaign_targets")
    op.create_unique_constraint("uq_campaign_targets_unique", "campaign_targets", ["campaign_id", "target_type", "target_value", "is_exclude"])

    op.add_column("campaigns", sa.Column("description", sa.Text(), nullable=True))
    op.add_column("campaigns", sa.Column("created_by", sa.UUID(), nullable=True))
    op.add_column("campaigns", sa.Column("updated_by", sa.UUID(), nullable=True))
    op.add_column("campaigns", sa.Column("version", sa.Integer(), server_default="1", nullable=False))
    _add_not_null("campaigns", sa.Column("sales_mode", sa.String(length=20)), "'SELF_SERVICE'")
    op.add_column("campaigns", sa.Column("seller_user_id", sa.UUID(), nullable=True))
    op.add_column("campaigns", sa.Column("plan_id", sa.UUID(), nullable=True))
    _add_not_null("campaigns", sa.Column("period_type", sa.String(length=20)), "'AVULSO'")
    op.add_column("campaigns", sa.Column("start_date", sa.Date(), nullable=True))
    op.add_column("campaigns", sa.Column("end_date", sa.Date(), nullable=True))
    _add_not_null("campaigns", sa.Column("duration_s", sa.SmallInteger()), "30")
    _add_not_null("campaigns", sa.Column("freq_kind", sa.String(length=20)), "'PER_HOUR'")
    _add_not_null("campaigns", sa.Column("freq_value", sa.Integer()), "1")
    op.add_column("campaigns", sa.Column("specific_times", postgresql.JSONB(astext_type=Text()), nullable=True))
    _add_not_null("campaigns", sa.Column("min_interval_s", sa.Integer()), "0")
    _add_not_null("campaigns", sa.Column("days_mask", sa.SmallInteger()), "127")
    _add_not_null("campaigns", sa.Column("time_bands", postgresql.JSONB(astext_type=Text())), "'[[0, 1440]]'::jsonb")
    _add_not_null("campaigns", sa.Column("priority", sa.String(length=10)), "'STANDARD'")
    op.add_column("campaigns", sa.Column("exclusivity", sa.String(length=30), server_default="NONE", nullable=False))
    _add_not_null("campaigns", sa.Column("allow_same_advertiser_consecutive", sa.Boolean()), "false")
    _add_not_null("campaigns", sa.Column("request_art_service", sa.Boolean()), "false")
    op.add_column("campaigns", sa.Column("auto_renew", sa.Boolean(), server_default=sa.text("false"), nullable=False))
    op.add_column("campaigns", sa.Column("product_id", sa.UUID(), nullable=True))
    op.add_column("campaigns", sa.Column("brand_id", sa.UUID(), nullable=True))
    op.add_column("campaigns", sa.Column("approval_source", sa.String(length=30), nullable=True))
    op.add_column("campaigns", sa.Column("submitted_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("campaigns", sa.Column("review_due_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("campaigns", sa.Column("rejected_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("campaigns", sa.Column("rejected_by", sa.UUID(), nullable=True))
    op.add_column("campaigns", sa.Column("rejection_reason", sa.Text(), nullable=True))
    op.add_column("campaigns", sa.Column("rejection_count", sa.Integer(), server_default="0", nullable=False))
    op.add_column("campaigns", sa.Column("contracted_price_cents", sa.BigInteger(), server_default="0", nullable=False))
    op.add_column("campaigns", sa.Column("paid_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("campaigns", sa.Column("paid_order_id", sa.UUID(), nullable=True))
    op.add_column("campaigns", sa.Column("payment_order_id", sa.UUID(), nullable=True))
    _add_not_null("campaigns", sa.Column("requires_new_payment", sa.Boolean()), "false")
    op.add_column("campaigns", sa.Column("payment_issue", sa.String(length=40), nullable=True))
    op.add_column("campaigns", sa.Column("live_revision_id", sa.UUID(), nullable=True))
    op.add_column("campaigns", sa.Column("pending_revision_id", sa.UUID(), nullable=True))
    op.add_column("campaigns", sa.Column("status_before_review", sa.String(length=30), nullable=True))
    op.add_column("campaigns", sa.Column("paused_from", sa.String(length=30), nullable=True))
    op.add_column("campaigns", sa.Column("scheduled_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("campaigns", sa.Column("syncing_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("campaigns", sa.Column("activated_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("campaigns", sa.Column("paused_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("campaigns", sa.Column("finalized_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("campaigns", sa.Column("cancelled_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("campaigns", sa.Column("cancelled_by", sa.UUID(), nullable=True))
    op.add_column("campaigns", sa.Column("cancel_reason", sa.Text(), nullable=True))

    # dados (W-CAMP): status EN → PT; datas no fuso de São Paulo; motivo de rejeição
    for r in _rows("SELECT id, org_id, status, starts_at, ends_at, total_budget_cents, rejected_reason, "
                   "updated_at FROM campaigns ORDER BY created_at, id"):
        new_status = CAMPAIGN_STATUS_EN_PT.get((r["status"] or "").upper(), "RASCUNHO")
        _exec(
            "UPDATE campaigns SET status = :st, "
            "start_date = (starts_at AT TIME ZONE 'America/Sao_Paulo')::date, "
            "end_date = (ends_at AT TIME ZONE 'America/Sao_Paulo')::date, "
            "rejection_reason = rejected_reason, "
            "rejected_at = CASE WHEN :st = 'REJEITADA' THEN updated_at END, "
            "rejection_count = CASE WHEN :st = 'REJEITADA' THEN 1 ELSE 0 END, "
            "requires_new_payment = (:st = 'REJEITADA'), "
            "finalized_at = CASE WHEN :st = 'FINALIZADA' THEN updated_at END, "
            "cancelled_at = CASE WHEN :st = 'CANCELADA' THEN updated_at END "
            "WHERE id = :id", st=new_status, id=r["id"])
        after = dict(_rows("SELECT status, start_date, end_date, rejection_reason, requires_new_payment "
                           "FROM campaigns WHERE id = :id", id=r["id"])[0])
        _exec("INSERT INTO campaign_status_history (id, campaign_id, from_status, to_status, trigger, origin, reason, "
              "created_at) VALUES (gen_random_uuid(), :c, :f, :t, 'migration_0004', 'system', :r, now())",
              c=r["id"], f=r["status"], t=new_status,
              r="Migração 0004: status legado convertido para a máquina de estados oficial (sem pagamento/revisão "
                "válidos no modelo antigo).")
        _audit("migration.0004.campaign", "campaign", r["id"], org_id=r["org_id"],
               before={"status": r["status"], "starts_at": r["starts_at"], "ends_at": r["ends_at"],
                       "total_budget_cents": r["total_budget_cents"], "rejected_reason": r["rejected_reason"]},
               after=after,
               reason="0004: status EN → PT, datas em America/Sao_Paulo; total_budget_cents sem equivalente no "
                      "modelo novo (preço é calculado no servidor) — valor preservado só neste registro (W-CAMP)")

    op.drop_index("ix_campaigns_ends_at", table_name="campaigns")
    op.drop_index("ix_campaigns_starts_at", table_name="campaigns")
    op.create_index("ix_campaigns_end_date", "campaigns", ["end_date"], unique=False)
    op.create_index("ix_campaigns_review_due_at", "campaigns", ["review_due_at"], unique=False)
    op.create_foreign_key("fk_campaigns_pending_revision", "campaigns", "campaign_revisions", ["pending_revision_id"], ["id"], ondelete="RESTRICT")
    op.create_foreign_key(op.f("fk_campaigns_cancelled_by_users"), "campaigns", "users", ["cancelled_by"], ["id"], ondelete="RESTRICT")
    op.create_foreign_key(op.f("fk_campaigns_seller_user_id_users"), "campaigns", "users", ["seller_user_id"], ["id"], ondelete="RESTRICT")
    op.create_foreign_key(op.f("fk_campaigns_updated_by_users"), "campaigns", "users", ["updated_by"], ["id"], ondelete="RESTRICT")
    op.create_foreign_key("fk_campaigns_live_revision", "campaigns", "campaign_revisions", ["live_revision_id"], ["id"], ondelete="RESTRICT")
    op.create_foreign_key(op.f("fk_campaigns_product_id_products"), "campaigns", "products", ["product_id"], ["id"], ondelete="RESTRICT")
    op.create_foreign_key(op.f("fk_campaigns_created_by_users"), "campaigns", "users", ["created_by"], ["id"], ondelete="RESTRICT")
    op.create_foreign_key(op.f("fk_campaigns_brand_id_brands"), "campaigns", "brands", ["brand_id"], ["id"], ondelete="RESTRICT")
    op.create_foreign_key(op.f("fk_campaigns_rejected_by_users"), "campaigns", "users", ["rejected_by"], ["id"], ondelete="RESTRICT")
    op.create_foreign_key(op.f("fk_campaigns_plan_id_plans"), "campaigns", "plans", ["plan_id"], ["id"], ondelete="RESTRICT")
    for c in ("starts_at", "total_budget_cents", "rejected_reason", "ends_at"):
        op.drop_column("campaigns", c)


def _upgrade_biz() -> None:
    # ── notifications: legado sem emissor funcional → apagado (recomendação W-BIZ) ──
    n = _count("notifications")
    _exec("DELETE FROM notifications")
    if n:
        _audit("migration.0004.notifications_discarded", "notification", None, after={"deleted": n},
               reason="0004: notificações do formato legado (tipos fora do catálogo) descartadas (W-BIZ)")
    op.add_column("notification_preferences", sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False))
    _exec("UPDATE notification_preferences SET channel = upper(channel)")
    op.add_column("notifications", sa.Column("org_id", sa.UUID(), nullable=True))
    op.add_column("notifications", sa.Column("event_type", sa.String(length=100), nullable=False))
    op.add_column("notifications", sa.Column("entity_type", sa.String(length=50), nullable=True))
    op.add_column("notifications", sa.Column("entity_id", sa.String(length=100), nullable=True))
    op.add_column("notifications", sa.Column("link", sa.String(length=500), nullable=True))
    op.add_column("notifications", sa.Column("dedupe_key", sa.String(length=255), nullable=True))
    op.alter_column("notifications", "created_at", existing_type=postgresql.TIMESTAMP(timezone=True), server_default=sa.text("now()"), existing_nullable=False)
    op.drop_index("ix_notifications_channel", table_name="notifications")
    op.drop_index("ix_notifications_created_at", table_name="notifications")
    op.drop_index("ix_notifications_read_at", table_name="notifications")
    op.drop_index("ix_notifications_user_id", table_name="notifications")
    op.drop_constraint("uq_notifications_idempotency", "notifications", type_="unique")
    op.drop_constraint("uq_notifications_idempotency_key", "notifications", type_="unique")
    op.create_index("ix_notifications_user_created", "notifications", ["user_id", "created_at"], unique=False)
    op.create_index("ix_notifications_user_unread", "notifications", ["user_id"], unique=False, postgresql_where=sa.text("read_at IS NULL"))
    op.create_unique_constraint("uq_notifications_user_dedupe", "notifications", ["user_id", "dedupe_key"])
    op.create_foreign_key(op.f("fk_notifications_org_id_organizations"), "notifications", "organizations", ["org_id"], ["id"], ondelete="SET NULL")
    for c in ("channel", "sent_at", "idempotency_key", "type"):
        op.drop_column("notifications", c)

    # ── leads: status → stage (PT), assigned_to → owner_id ──
    op.add_column("leads", sa.Column("interest", sa.String(length=30), nullable=True))
    _add_not_null("leads", sa.Column("stage", sa.String(length=20)), "'NOVO'")
    op.add_column("leads", sa.Column("owner_id", sa.UUID(), nullable=True))
    op.add_column("leads", sa.Column("city", sa.String(length=100), nullable=True))
    op.add_column("leads", sa.Column("uf", sa.String(length=2), nullable=True))
    op.add_column("leads", sa.Column("city_key", sa.String(length=120), nullable=True))
    op.add_column("leads", sa.Column("org_id", sa.UUID(), nullable=True))
    op.add_column("leads", sa.Column("estimated_value_cents", sa.BigInteger(), nullable=True))
    op.add_column("leads", sa.Column("lost_reason", sa.String(length=500), nullable=True))
    op.add_column("leads", sa.Column("created_by", sa.UUID(), nullable=True))
    op.add_column("leads", sa.Column("stage_changed_at", sa.DateTime(timezone=True), nullable=True))
    n = _count("leads")
    if n:
        _exec(f"UPDATE leads SET stage = {_case('status', LEAD_STATUS_EN_PT, chr(39) + 'NOVO' + chr(39))}, "
              "owner_id = assigned_to, stage_changed_at = updated_at, "
              "source = left(COALESCE(NULLIF(btrim(source), ''), 'manual'), 50)")
        _audit("migration.0004.leads", "lead", None, after={"migrated": n},
               reason="0004: leads status → stage (PT), assigned_to → owner_id (W-BIZ)")
    op.alter_column("leads", "source", existing_type=sa.VARCHAR(length=100), type_=sa.String(length=50), nullable=False)
    op.drop_index("ix_leads_assigned_to", table_name="leads")
    op.drop_index("ix_leads_status", table_name="leads")
    op.create_index("ix_leads_city_key", "leads", ["city_key"], unique=False)
    op.create_index("ix_leads_org_id", "leads", ["org_id"], unique=False)
    op.create_index("ix_leads_owner_id", "leads", ["owner_id"], unique=False)
    op.create_index("ix_leads_stage", "leads", ["stage"], unique=False)
    op.drop_constraint("fk_leads_assigned_to_users", "leads", type_="foreignkey")
    op.create_foreign_key(op.f("fk_leads_created_by_users"), "leads", "users", ["created_by"], ["id"], ondelete="SET NULL")
    op.create_foreign_key(op.f("fk_leads_org_id_organizations"), "leads", "organizations", ["org_id"], ["id"], ondelete="SET NULL")
    op.create_foreign_key(op.f("fk_leads_owner_id_users"), "leads", "users", ["owner_id"], ["id"], ondelete="SET NULL")
    op.drop_column("leads", "status")
    op.drop_column("leads", "assigned_to")

    # ── proposals / proposal_items ──
    op.add_column("proposal_items", sa.Column("position", sa.Integer(), nullable=True))
    op.add_column("proposal_items", sa.Column("package_code", sa.String(length=60), nullable=True))
    op.add_column("proposal_items", sa.Column("line_total_cents", sa.BigInteger(), nullable=True))
    op.add_column("proposal_items", sa.Column("details", postgresql.JSONB(astext_type=Text()), nullable=True))
    op.alter_column("proposal_items", "unit_price_cents", existing_type=sa.INTEGER(), type_=sa.BigInteger(), existing_nullable=False)
    n_items = _count("proposal_items")
    if n_items:
        disc = _scalar("SELECT count(*) FROM proposal_items WHERE discount_percent <> 0")
        _exec("UPDATE proposal_items p SET position = s.rn, line_total_cents = p.quantity * p.unit_price_cents, "
              "details = CASE WHEN p.discount_percent <> 0 THEN jsonb_build_object('legacy_discount_percent', "
              "p.discount_percent) END FROM (SELECT id, row_number() OVER (PARTITION BY proposal_id ORDER BY id) - 1 "
              "AS rn FROM proposal_items) s WHERE s.id = p.id")
        _audit("migration.0004.proposal_items", "proposal_item", None,
               after={"migrated": n_items, "legacy_discount_percent_kept_in_details": disc},
               reason="0004: itens com total de linha; desconto % legado preservado em details (W-BIZ)")
    op.alter_column("proposal_items", "position", nullable=False)
    op.alter_column("proposal_items", "line_total_cents", nullable=False)
    op.drop_column("proposal_items", "discount_percent")

    for col in (
        sa.Column("number", sa.String(length=20), nullable=True),
        sa.Column("seller_id", sa.UUID(), nullable=True),
        sa.Column("city_key", sa.String(length=120), nullable=True),
        sa.Column("subtotal_cents", sa.BigInteger(), nullable=True),
        sa.Column("discount_bps", sa.Integer(), nullable=True),
        sa.Column("discount_cents", sa.BigInteger(), nullable=True),
        sa.Column("discount_limit_bps_snapshot", sa.Integer(), nullable=True),
        sa.Column("discount_approval_required", sa.Boolean(), nullable=True),
        sa.Column("discount_justification", sa.String(length=1000), nullable=True),
        sa.Column("discount_approved_by", sa.UUID(), nullable=True),
        sa.Column("discount_approved_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("discount_decision_reason", sa.String(length=1000), nullable=True),
        sa.Column("validity_days", sa.Integer(), nullable=True),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("sent_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("accepted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("decided_note", sa.String(length=1000), nullable=True),
        sa.Column("converted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("order_id", sa.UUID(), nullable=True),
        sa.Column("pdf_key", sa.String(length=512), nullable=True),
        sa.Column("pdf_sha256", sa.String(length=64), nullable=True),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.Column("version", sa.Integer(), nullable=True),
    ):
        op.add_column("proposals", col)
    op.alter_column("proposals", "status", existing_type=sa.VARCHAR(length=20), type_=sa.String(length=25), existing_nullable=False)
    op.alter_column("proposals", "total_cents", existing_type=sa.INTEGER(), type_=sa.BigInteger(), existing_nullable=False)
    n = _count("proposals")
    if n:
        _assign_codes("proposals", "'PROP-' || to_char(created_at AT TIME ZONE 'America/Sao_Paulo', 'YYYY') || '-'")
        _exec(
            "UPDATE proposals SET seller_id = created_by, discount_approved_by = approved_by, "
            "subtotal_cents = total_cents, discount_bps = 0, discount_cents = 0, discount_approval_required = false, "
            f"validity_days = 30, version = 1, status = {_case('status', PROPOSAL_STATUS_EN_PT, chr(39) + 'RASCUNHO' + chr(39))}")
        _audit("migration.0004.proposals", "proposal", None, after={"migrated": n},
               reason="0004: propostas numeradas PROP-AAAA-XXXXXX, vendedor = criador, status PT (W-BIZ)")
    for c in ("number", "seller_id", "subtotal_cents", "discount_bps", "discount_cents", "discount_approval_required",
              "validity_days", "version"):
        op.alter_column("proposals", c, nullable=False)
    op.drop_index("ix_proposals_created_by", table_name="proposals")
    op.create_index("ix_proposals_city_key", "proposals", ["city_key"], unique=False)
    op.create_index("ix_proposals_seller_id", "proposals", ["seller_id"], unique=False)
    op.create_unique_constraint("uq_proposals_number", "proposals", ["number"])
    op.drop_constraint("fk_proposals_created_by_users", "proposals", type_="foreignkey")
    op.drop_constraint("fk_proposals_approved_by_users", "proposals", type_="foreignkey")
    op.create_foreign_key(op.f("fk_proposals_seller_id_users"), "proposals", "users", ["seller_id"], ["id"], ondelete="RESTRICT")
    op.create_foreign_key(op.f("fk_proposals_discount_approved_by_users"), "proposals", "users", ["discount_approved_by"], ["id"], ondelete="RESTRICT")
    op.drop_column("proposals", "created_by")
    op.drop_column("proposals", "approved_by")

    # ── tickets / ticket_messages ──
    op.alter_column("ticket_messages", "content", new_column_name="body", existing_type=sa.TEXT(), existing_nullable=False)
    op.alter_column("ticket_messages", "created_at", existing_type=postgresql.TIMESTAMP(timezone=True), server_default=sa.text("now()"), existing_nullable=False)
    for col in (
        sa.Column("number", sa.String(length=12), nullable=True),
        sa.Column("org_id", sa.UUID(), nullable=True),
        sa.Column("screen_id", sa.UUID(), nullable=True),
        sa.Column("creative_code", sa.String(length=12), nullable=True),
        sa.Column("campaign_id", sa.UUID(), nullable=True),
        sa.Column("advertiser_org_id", sa.UUID(), nullable=True),
        sa.Column("city_key", sa.String(length=120), nullable=True),
        sa.Column("sla_due_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("first_response_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("closed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("version", sa.Integer(), nullable=True),
    ):
        op.add_column("tickets", col)
    n = _count("tickets")
    if n:
        cats = ",".join(f"'{c}'" for c in TICKET_CATEGORIES)
        _assign_codes("tickets", "'CH-'")
        _exec(
            "UPDATE tickets SET version = 1, description = COALESCE(description, ''), title = left(title, 200), "
            f"category = CASE WHEN category IS NULL THEN 'DUVIDA' WHEN upper(category) IN ({cats}) "
            "THEN upper(category) ELSE 'OUTRO' END, "
            f"status = {_case('status', TICKET_STATUS_EN_PT, 'upper(status)')}, "
            f"priority = {_case('priority', TICKET_PRIORITY_EN_PT, chr(39) + 'MEDIA' + chr(39))}")
        _exec("UPDATE tickets SET status = 'ABERTO' WHERE status NOT IN "
              "('ABERTO','EM_ATENDIMENTO','AGUARDANDO_CLIENTE','RESOLVIDO','FECHADO','CANCELADO')")
        _exec("UPDATE tickets SET closed_at = updated_at WHERE status = 'FECHADO'")
        _audit("migration.0004.tickets", "ticket", None, after={"migrated": n},
               reason="0004: chamados numerados CH-NNNNNN, status/prioridade/categoria PT, content → body (W-BIZ)")
    op.alter_column("tickets", "number", nullable=False)
    op.alter_column("tickets", "version", nullable=False)
    op.alter_column("tickets", "category", existing_type=sa.VARCHAR(length=100), type_=sa.String(length=20), nullable=False)
    op.alter_column("tickets", "title", existing_type=sa.VARCHAR(length=255), type_=sa.String(length=200), existing_nullable=False)
    op.alter_column("tickets", "description", existing_type=sa.TEXT(), nullable=False)
    op.alter_column("tickets", "priority", existing_type=sa.VARCHAR(length=20), type_=sa.String(length=10), existing_nullable=False)
    op.drop_index("ix_tickets_created_by", table_name="tickets")
    op.drop_index("ix_tickets_priority", table_name="tickets")
    op.create_index("ix_tickets_city_key", "tickets", ["city_key"], unique=False)
    op.create_index("ix_tickets_creative_code", "tickets", ["creative_code"], unique=False)
    op.create_index("ix_tickets_org_id", "tickets", ["org_id"], unique=False)
    op.create_index("ix_tickets_sla_due_at", "tickets", ["sla_due_at"], unique=False)
    op.create_unique_constraint("uq_tickets_number", "tickets", ["number"])
    op.drop_constraint("fk_tickets_creative_id_creatives", "tickets", type_="foreignkey")
    op.create_foreign_key(op.f("fk_tickets_org_id_organizations"), "tickets", "organizations", ["org_id"], ["id"], ondelete="RESTRICT")
    op.create_foreign_key(op.f("fk_tickets_advertiser_org_id_organizations"), "tickets", "organizations", ["advertiser_org_id"], ["id"], ondelete="SET NULL")


# ─── 4. CHECKs das tabelas existentes (autogenerate não compara CHECK) ────────

EXISTING_TABLE_CHECKS = [
    ('ck_campaign_targets_target_type_valid', 'campaign_targets', "target_type IN ('COUNTRY', 'REGION', 'STATE', 'CITY', 'NEIGHBORHOOD', 'POSTAL_CODE', 'CATEGORY', 'ESTABLISHMENT')"),
    ('ck_campaigns_contracted_price_nonneg', 'campaigns', 'contracted_price_cents >= 0'),
    ('ck_campaigns_dates_order', 'campaigns', 'end_date IS NULL OR start_date IS NULL OR end_date >= start_date'),
    ('ck_campaigns_days_mask_valid', 'campaigns', 'days_mask BETWEEN 1 AND 127'),
    ('ck_campaigns_duration_valid', 'campaigns', 'duration_s IN (30, 60, 120)'),
    ('ck_campaigns_exclusivity_valid', 'campaigns', "exclusivity IN ('NONE', 'COMPETITOR_EXCLUSIVE')"),
    ('ck_campaigns_freq_kind_valid', 'campaigns', "freq_kind IN ('PER_HOUR', 'PER_DAY', 'SPECIFIC_TIME')"),
    ('ck_campaigns_freq_value_positive', 'campaigns', 'freq_value > 0'),
    ('ck_campaigns_min_interval_nonneg', 'campaigns', 'min_interval_s >= 0'),
    ('ck_campaigns_period_type_valid', 'campaigns', "period_type IN ('AVULSO', 'MENSAL', 'SEMESTRAL', 'ANUAL')"),
    ('ck_campaigns_priority_valid', 'campaigns', "priority IN ('STANDARD', 'PREMIUM')"),
    ('ck_campaigns_sales_mode_valid', 'campaigns', "sales_mode IN ('SELF_SERVICE', 'COMMERCIAL')"),
    ('ck_campaigns_status_valid', 'campaigns', "status IN ('RASCUNHO', 'AGUARDANDO_PAGAMENTO', 'AGUARDANDO_MIDIA', 'EM_ANALISE', 'APROVADA', 'AGENDADA', 'SINCRONIZANDO', 'ATIVA', 'PAUSADA', 'FINALIZADA', 'REJEITADA', 'CANCELADA')"),
    ('ck_creatives_code_format', 'creatives', "code ~ '^RD-[ABCDEFGHJMNPQRSTUVYZ2-9]{6}$'"),
    ('ck_creatives_duration_valid', 'creatives', 'duration_seconds IN (30, 60, 120)'),
    ('ck_creatives_kind_valid', 'creatives', "kind IN ('EXTERNAL', 'INTERNAL', 'INSTITUTIONAL', 'FALLBACK')"),
    ('ck_creatives_orientation_valid', 'creatives', "orientation IN ('HORIZONTAL', 'VERTICAL')"),
    ('ck_creatives_status_valid', 'creatives', "status IN ('RASCUNHO', 'EM_ANALISE', 'APROVADO', 'REJEITADO', 'SUBSTITUIDO', 'ARQUIVADO')"),
    ('ck_creatives_technical_status_valid', 'creatives', "technical_status IN ('PENDENTE', 'VALIDO', 'INVALIDO')"),
    ('ck_creatives_version_positive', 'creatives', 'version >= 1'),
    ('ck_equipment_items_bulk_no_serial', 'equipment_items', "tracking = 'SERIAL' OR (serial_number IS NULL AND asset_tag IS NULL)"),
    ('ck_equipment_items_bulk_status_consistent', 'equipment_items', "(tracking = 'BULK') = (status = 'BULK')"),
    ('ck_equipment_items_condition_valid', 'equipment_items', "condition IN ('NEW','GOOD','DAMAGED','BROKEN')"),
    ('ck_equipment_items_cost_nonneg', 'equipment_items', 'purchase_cost_cents IS NULL OR purchase_cost_cents >= 0'),
    ('ck_equipment_items_inches_positive', 'equipment_items', 'inches IS NULL OR inches > 0'),
    ('ck_equipment_items_location_kind_valid', 'equipment_items', "location_kind IS NULL OR location_kind IN ('EXTERNAL','WAREHOUSE','TECHNICIAN','ESTABLISHMENT','SCREEN','MAINTENANCE','RMA','DISPOSED')"),
    ('ck_equipment_items_status_valid', 'equipment_items', "status IN ('IN_STOCK','WITH_TECHNICIAN','INSTALLED','LOANED','COMODATO','IN_MAINTENANCE','IN_RMA','WRITTEN_OFF','BULK')"),
    ('ck_equipment_items_tracking_valid', 'equipment_items', "tracking IN ('SERIAL','BULK')"),
    ('ck_equipment_items_type_valid', 'equipment_items', "type IN ('TV','TVBOX','CABLE','REMOTE','MOUNT','ACCESSORY')"),
    ('ck_equipment_movements_from_kind_valid', 'equipment_movements', "from_kind IN ('EXTERNAL','WAREHOUSE','TECHNICIAN','ESTABLISHMENT','SCREEN','MAINTENANCE','RMA','DISPOSED')"),
    ('ck_equipment_movements_from_to_distinct', 'equipment_movements', 'NOT (from_kind = to_kind AND from_ref_id IS NOT DISTINCT FROM to_ref_id AND from_city_key IS NOT DISTINCT FROM to_city_key)'),
    ('ck_equipment_movements_movement_type_valid', 'equipment_movements', "movement_type IN ('ENTRY','TRANSFER','INSTALL','LOAN','COMODATO','RETURN','MAINTENANCE_IN','MAINTENANCE_OUT','RMA_OUT','RMA_RETURN','WRITE_OFF','ADJUSTMENT')"),
    ('ck_equipment_movements_quantity_positive', 'equipment_movements', 'quantity > 0'),
    ('ck_equipment_movements_to_kind_valid', 'equipment_movements', "to_kind IN ('EXTERNAL','WAREHOUSE','TECHNICIAN','ESTABLISHMENT','SCREEN','MAINTENANCE','RMA','DISPOSED')"),
    ('ck_establishments_audience_method_valid', 'establishments', "audience_method IS NULL OR audience_method IN ('MANUAL','FLOW_ESTIMATE')"),
    ('ck_establishments_coords_pair', 'establishments', '(lat IS NULL) = (lng IS NULL)'),
    ('ck_establishments_document_type_valid', 'establishments', "document_type IS NULL OR document_type IN ('CPF','CNPJ')"),
    ('ck_establishments_geo_source_valid', 'establishments', "geo_source IS NULL OR geo_source IN ('MANUAL','GPS')"),
    ('ck_establishments_lat_range', 'establishments', 'lat IS NULL OR (lat >= -90 AND lat <= 90)'),
    ('ck_establishments_lng_range', 'establishments', 'lng IS NULL OR (lng >= -180 AND lng <= 180)'),
    ('ck_establishments_monthly_fee_nonneg', 'establishments', 'monthly_fee_cents IS NULL OR monthly_fee_cents >= 0'),
    ('ck_establishments_onboarding_status_valid', 'establishments', "onboarding_status IN ('AWAITING_CONTRACT','ACTIVE')"),
    ('ck_establishments_postal_code_digits', 'establishments', "postal_code IS NULL OR postal_code ~ '^[0-9]{8}$'"),
    ('ck_establishments_region_valid', 'establishments', "region IN ('NORTE','NORDESTE','CENTRO_OESTE','SUDESTE','SUL')"),
    ('ck_establishments_state_len', 'establishments', 'char_length(state) = 2'),
    ('ck_establishments_timezone_source_valid', 'establishments', "timezone_source IN ('AUTO','MANUAL')"),
    ('ck_establishments_venue_class_valid', 'establishments', "venue_class IN ('STANDARD','PREMIUM')"),
    ('ck_establishments_visitors_nonneg', 'establishments', 'estimated_daily_visitors IS NULL OR estimated_daily_visitors >= 0'),
    ('ck_establishments_weight_range', 'establishments', 'weight_milli > 0 AND weight_milli <= 100000'),
    ('ck_leads_stage_valid', 'leads', "stage IN ('NOVO','CONTATO','QUALIFICADO','PROPOSTA','NEGOCIACAO','GANHO','PERDIDO')"),
    ('ck_media_files_kind_valid', 'media_files', "kind IN ('IMAGE', 'VIDEO')"),
    ('ck_media_files_orientation_valid', 'media_files', "orientation IS NULL OR orientation IN ('HORIZONTAL', 'VERTICAL')"),
    ('ck_media_files_progress_range', 'media_files', 'progress_pct BETWEEN 0 AND 100'),
    ('ck_media_files_ready_complete', 'media_files', "status <> 'READY' OR (storage_key IS NOT NULL AND sha256 IS NOT NULL AND size_bytes IS NOT NULL AND width IS NOT NULL AND height IS NOT NULL)"),
    ('ck_media_files_sha256_hex', 'media_files', "sha256 IS NULL OR sha256 ~ '^[0-9a-f]{64}$'"),
    ('ck_media_files_size_positive', 'media_files', 'size_bytes IS NULL OR size_bytes > 0'),
    ('ck_media_files_source_sha256_hex', 'media_files', "source_sha256 ~ '^[0-9a-f]{64}$'"),
    ('ck_media_files_source_size_positive', 'media_files', 'source_size_bytes > 0'),
    ('ck_media_files_status_valid', 'media_files', "status IN ('UPLOADED', 'VALIDATING', 'PROCESSING', 'READY', 'REJECTED', 'FAILED')"),
    ('ck_notification_preferences_channel_valid', 'notification_preferences', "channel IN ('IN_APP','EMAIL','PUSH')"),
    ('ck_organizations_business_roles_valid', 'organizations', "business_roles <@ ARRAY['ADVERTISER','PARTNER','AGENCY','FRANCHISE']::varchar[]"),
    ('ck_organizations_document_type_valid', 'organizations', "document_type IN ('CPF','CNPJ')"),
    ('ck_organizations_kyc_status_valid', 'organizations', "kyc_status IN ('PENDING','APPROVED','REJECTED')"),
    ('ck_organizations_state_len', 'organizations', 'state IS NULL OR char_length(state) = 2'),
    ('ck_proposal_items_line_total_consistent', 'proposal_items', 'line_total_cents = quantity * unit_price_cents'),
    ('ck_proposal_items_quantity_positive', 'proposal_items', 'quantity > 0'),
    ('ck_proposal_items_unit_price_non_negative', 'proposal_items', 'unit_price_cents >= 0'),
    ('ck_proposals_amounts_non_negative', 'proposals', 'subtotal_cents >= 0 AND discount_cents >= 0 AND total_cents >= 0'),
    ('ck_proposals_discount_bps_range', 'proposals', 'discount_bps >= 0 AND discount_bps <= 10000'),
    ('ck_proposals_status_valid', 'proposals', "status IN ('RASCUNHO','AGUARDANDO_APROVACAO','APROVADA','ENVIADA','ACEITA','RECUSADA','EXPIRADA','CONVERTIDA','CANCELADA')"),
    ('ck_proposals_total_consistent', 'proposals', 'total_cents = subtotal_cents - discount_cents'),
    ('ck_screens_audience_method_valid', 'screens', "audience_method IS NULL OR audience_method IN ('MANUAL','FLOW_ESTIMATE')"),
    ('ck_screens_audience_nonneg', 'screens', 'estimated_daily_audience IS NULL OR estimated_daily_audience >= 0'),
    ('ck_screens_decommissioned_at_consistent', 'screens', "(status = 'DECOMMISSIONED') = (decommissioned_at IS NOT NULL)"),
    ('ck_screens_decommissioned_not_sellable', 'screens', "status <> 'DECOMMISSIONED' OR sellable = false"),
    ('ck_screens_display_type_valid', 'screens', "display_type IN ('TV','MONITOR','LED_PANEL','PROJECTOR','OTHER')"),
    ('ck_screens_inches_range', 'screens', 'inches IS NULL OR (inches > 0 AND inches <= 500)'),
    ('ck_screens_orientation_valid', 'screens', "orientation IN ('HORIZONTAL','VERTICAL')"),
    ('ck_screens_player_type_valid', 'screens', "player_type IN ('TVBOX','SMART_TV_APP','PC','BROWSER','OTHER')"),
    ('ck_screens_programming_mode_valid', 'screens', "programming_mode IN ('ESTABLISHMENT_DEFAULT','CUSTOM')"),
    ('ck_screens_resolution_positive', 'screens', '(resolution_w IS NULL OR resolution_w > 0) AND (resolution_h IS NULL OR resolution_h > 0)'),
    ('ck_screens_screen_class_valid', 'screens', "screen_class IN ('STANDARD','PREMIUM')"),
    ('ck_screens_status_valid', 'screens', "status IN ('ACTIVE','MAINTENANCE','INACTIVE','DECOMMISSIONED')"),
    ('ck_screens_tv_owner_valid', 'screens', "tv_owner IN ('RD','ESTABLISHMENT')"),
    ('ck_screens_weight_range', 'screens', 'weight_milli > 0 AND weight_milli <= 100000'),
    ('ck_tickets_category_valid', 'tickets', "category IN ('DUVIDA','PROBLEMA_TELA','RETIRADA_ANUNCIO','CAMPANHA','FINANCEIRO','INSTALACAO','MANUTENCAO','OUTRO')"),
    ('ck_tickets_priority_valid', 'tickets', "priority IN ('BAIXA','MEDIA','ALTA','CRITICA')"),
    ('ck_tickets_status_valid', 'tickets', "status IN ('ABERTO','EM_ATENDIMENTO','AGUARDANDO_CLIENTE','RESOLVIDO','FECHADO','CANCELADO')"),
]


def _create_existing_checks() -> None:
    for name, table, expr in EXISTING_TABLE_CHECKS:
        op.create_check_constraint(op.f(name), table, expr)


# ─── 5. DDL extra e privilégios ──────────────────────────────────────────────

def _extra_ddl() -> None:
    for t in APPEND_ONLY_0004:
        for stmt in ddl.append_only_trigger(t):
            op.execute(stmt)
    for t, cols in IMMUTABLE_0004.items():
        for stmt in ddl.immutable_columns_trigger(t, cols):
            op.execute(stmt)
    for stmt in ddl.domain_statements(strict=True):
        op.execute(stmt)


def _runtime_privileges() -> None:
    """Papel de runtime sem UPDATE/DELETE/TRUNCATE nas tabelas append-only (camada extra além dos
    triggers). ACL EXPLÍCITA de ``rd_app`` → o db-init (ops/db) nunca re-concede. Sem o papel no
    cluster (instalação nova antes do db-init) nada é feito: o db-init roda ANTES do migrate."""
    if not _role_exists(RUNTIME_ROLE):
        return
    for t in APPEND_ONLY_0003 + APPEND_ONLY_0004 + ("screen_anchor_history",):
        op.execute(f"GRANT SELECT, INSERT ON {t} TO {RUNTIME_ROLE}")
        op.execute(f"REVOKE UPDATE, DELETE, TRUNCATE ON {t} FROM {RUNTIME_ROLE}")
    for t in NO_DELETE_0004:
        op.execute(f"GRANT SELECT, INSERT, UPDATE ON {t} TO {RUNTIME_ROLE}")
        op.execute(f"REVOKE DELETE, TRUNCATE ON {t} FROM {RUNTIME_ROLE}")


def upgrade() -> None:
    _guards()
    _base_ddl()
    _drop_legacy_tables()
    _create_new_tables()
    _upgrade_organizations()
    _upgrade_establishments()
    _upgrade_screens()
    _upgrade_pairing_and_devices()
    _upgrade_media_and_equipment()
    _upgrade_creatives()
    _upgrade_campaigns()
    _upgrade_biz()
    _create_existing_checks()
    _extra_ddl()
    _runtime_privileges()


# ═════════════════════════════════════════════════════════════════════════════
# DOWNGRADE — volta ao formato da 0003 (aproximação documentada).
# PERDE: todas as tabelas novas e seus dados (financeiro, inventário, manifestos,
# PoP, CRM/design/relatórios/control plane, taxonomia, contratos, estoque...);
# payment_orders/webhook_events/playback_events voltam VAZIAS no formato legado;
# dispositivos do protocolo novo ficam inutilizáveis (sem token legado); pairing
# requests apagados; notificações apagadas. Mapeamentos reversos best-effort:
# business_roles → type, status PT → EN, start/end_date → starts/ends_at (SP),
# contracted_price_cents → total_budget_cents, code → creative_id, stage → status.
# Recusa (RuntimeError) dados que o formato legado não comporta (ex.: mídia gerada
# pelo servidor sem autor, movimentação de estoque sem responsável).
# ═════════════════════════════════════════════════════════════════════════════

def _downgrade_guards() -> None:
    problems = []
    if _table_exists("media_files") and _scalar("SELECT count(*) FROM media_files WHERE created_by IS NULL"):
        problems.append("media_files gerada pelo servidor sem autor (created_by NULL)")
    if _scalar("SELECT count(*) FROM equipment_movements WHERE performed_by IS NULL OR to_status IS NULL"):
        problems.append("equipment_movements sem responsável/estado de destino")
    if problems:
        raise RuntimeError("downgrade 0004: dados que o formato 0003 não comporta — " + "; ".join(problems))


def _downgrade_ddl() -> None:
    import importlib

    for mod_name in ("app.modules.financial.ddl", "app.modules.inventory.ddl", "app.modules.organizations.ddl"):
        for stmt in importlib.import_module(mod_name).drop_statements():
            op.execute(stmt)
    op.execute("DROP TRIGGER IF EXISTS trg_design_versions_guard ON design_versions")
    for t in IMMUTABLE_0004:
        for stmt in ddl.drop_immutable_columns_trigger(t):
            op.execute(stmt)
    for t in APPEND_ONLY_0004:
        for stmt in ddl.drop_append_only_trigger(t):
            op.execute(stmt)
    for name, table, _expr in reversed(EXISTING_TABLE_CHECKS):
        op.drop_constraint(op.f(name), table, type_="check")


def _downgrade_data_reverse() -> None:
    """Executado ANTES de remover as colunas novas (dados reversos best-effort)."""
    # establishments.category_id aponta para taxonomy_nodes (removida); a FK legada aponta para
    # establishment_categories (vazia) ⇒ categoria perdida no rollback
    _exec("UPDATE establishments SET category_id = NULL WHERE category_id IS NOT NULL")
    # organizations: business_roles → bitmask (PARTNER volta como 2)
    op.add_column("organizations", sa.Column("type", sa.INTEGER(), autoincrement=False, nullable=True))
    _exec("UPDATE organizations SET type = (CASE WHEN 'ADVERTISER' = ANY(business_roles) THEN 1 ELSE 0 END) "
          "| (CASE WHEN 'PARTNER' = ANY(business_roles) THEN 2 ELSE 0 END)")
    op.alter_column("organizations", "type", nullable=False)
    # campaigns
    op.add_column("campaigns", sa.Column("starts_at", postgresql.TIMESTAMP(timezone=True), autoincrement=False, nullable=True))
    op.add_column("campaigns", sa.Column("rejected_reason", sa.TEXT(), autoincrement=False, nullable=True))
    op.add_column("campaigns", sa.Column("ends_at", postgresql.TIMESTAMP(timezone=True), autoincrement=False, nullable=True))
    op.add_column("campaigns", sa.Column("total_budget_cents", sa.INTEGER(), autoincrement=False, nullable=True))
    _exec(f"UPDATE campaigns SET status = {_case('status', CAMPAIGN_STATUS_PT_EN, 'status')}, "
          "starts_at = (start_date::timestamp AT TIME ZONE 'America/Sao_Paulo'), "
          "ends_at = ((end_date + 1)::timestamp AT TIME ZONE 'America/Sao_Paulo') - interval '1 second', "
          "rejected_reason = rejection_reason, total_budget_cents = LEAST(contracted_price_cents, 2147483647)")
    op.alter_column("campaigns", "total_budget_cents", nullable=False)
    # creatives: status PT → EN; code → creative_id (renomeada de volta)
    _exec(f"UPDATE creatives SET status = {_case('status', {v: k for k, v in CREATIVE_STATUS_EN_PT.items() if k != 'IN_REVIEW'}, 'status')}")
    op.alter_column("creatives", "code", new_column_name="creative_id", existing_type=sa.String(length=9), existing_nullable=False)
    op.alter_column("creatives", "creative_id", existing_type=sa.String(length=9), type_=sa.VARCHAR(length=20), existing_nullable=False)
    # media_files: colunas legadas a partir das novas
    for col in (
        sa.Column("duration_seconds", sa.DOUBLE_PRECISION(precision=53), autoincrement=False, nullable=True),
        sa.Column("checksum_sha256", sa.VARCHAR(length=64), autoincrement=False, nullable=True),
        sa.Column("filename", sa.VARCHAR(length=500), autoincrement=False, nullable=True),
        sa.Column("storage_path", sa.VARCHAR(length=1000), autoincrement=False, nullable=True),
        sa.Column("content_type", sa.VARCHAR(length=10), autoincrement=False, nullable=True),
        sa.Column("storage_provider", sa.VARCHAR(length=20), autoincrement=False, nullable=True),
        sa.Column("error_message", sa.TEXT(), autoincrement=False, nullable=True),
        sa.Column("has_audio_original", sa.BOOLEAN(), autoincrement=False, nullable=True),
    ):
        op.add_column("media_files", col)
    _exec("UPDATE media_files SET filename = left(name, 500), storage_path = COALESCE(storage_key, source_storage_key, ''), "
          "content_type = lower(kind), storage_provider = 'local', duration_seconds = duration_ms / 1000.0, "
          "checksum_sha256 = sha256, error_message = status_message, has_audio_original = source_has_audio, "
          "size_bytes = COALESCE(size_bytes, source_size_bytes), mime_type = COALESCE(mime_type, source_mime)")
    for c in ("filename", "storage_path", "content_type", "storage_provider"):
        op.alter_column("media_files", c, nullable=False)
    # screens
    op.add_column("screens", sa.Column("estimated_audience", sa.INTEGER(), autoincrement=False, nullable=True))
    op.add_column("screens", sa.Column("comodato_config", postgresql.JSONB(astext_type=Text()), autoincrement=False, nullable=True))
    _exec("UPDATE screens SET estimated_audience = estimated_daily_audience, orientation = lower(orientation), "
          "status = CASE WHEN status = 'ACTIVE' THEN (CASE WHEN device_id IS NULL THEN 'UNPAIRED' ELSE 'PAIRED' END) "
          "ELSE status END")
    # devices: formato legado exige token/segredo — sintéticos e inativos (re-pareamento obrigatório)
    op.add_column("devices", sa.Column("ip_address", sa.VARCHAR(length=50), autoincrement=False, nullable=True))
    op.add_column("devices", sa.Column("cycle_anchor", postgresql.TIMESTAMP(timezone=True), autoincrement=False, nullable=True))
    op.add_column("devices", sa.Column("device_token", sa.VARCHAR(length=255), autoincrement=False, nullable=True))
    op.add_column("devices", sa.Column("device_secret", sa.VARCHAR(length=500), autoincrement=False, nullable=True))
    op.add_column("devices", sa.Column("is_active", sa.BOOLEAN(), autoincrement=False, nullable=True))
    op.add_column("devices", sa.Column("ssid", sa.VARCHAR(length=100), autoincrement=False, nullable=True))
    _exec("UPDATE devices SET device_token = 'downgraded-' || id::text, device_secret = '', is_active = false, "
          "platform = upper(platform)")
    for c in ("device_token", "device_secret", "is_active"):
        op.alter_column("devices", c, nullable=False)
    _exec("DELETE FROM pairing_requests")
    op.add_column("pairing_requests", sa.Column("establishment_id", sa.UUID(), autoincrement=False, nullable=True))
    op.add_column("pairing_requests", sa.Column("used_at", postgresql.TIMESTAMP(timezone=True), autoincrement=False, nullable=True))
    op.add_column("pairing_requests", sa.Column("qr_token", sa.VARCHAR(length=255), autoincrement=False, nullable=False))
    # notifications: formato legado (sem emissor) — apagadas
    _exec("DELETE FROM notifications")
    # leads
    op.add_column("leads", sa.Column("assigned_to", sa.UUID(), autoincrement=False, nullable=True))
    op.add_column("leads", sa.Column("status", sa.VARCHAR(length=20), autoincrement=False, nullable=True))
    _exec(f"UPDATE leads SET assigned_to = owner_id, status = {_case('stage', {v: k for k, v in LEAD_STATUS_EN_PT.items() if k not in ('WON',)}, 'stage')}")
    op.alter_column("leads", "status", nullable=False)
    # proposals
    op.add_column("proposals", sa.Column("approved_by", sa.UUID(), autoincrement=False, nullable=True))
    op.add_column("proposals", sa.Column("created_by", sa.UUID(), autoincrement=False, nullable=True))
    _exec(f"UPDATE proposals SET created_by = seller_id, approved_by = discount_approved_by, "
          f"status = {_case('status', {v: k for k, v in PROPOSAL_STATUS_EN_PT.items()}, 'status')}")
    op.alter_column("proposals", "created_by", nullable=False)
    op.add_column("proposal_items", sa.Column("discount_percent", sa.DOUBLE_PRECISION(precision=53), autoincrement=False, nullable=True))
    _exec("UPDATE proposal_items SET discount_percent = COALESCE((details ->> 'legacy_discount_percent')::double precision, 0)")
    op.alter_column("proposal_items", "discount_percent", nullable=False)
    # tickets
    _exec(f"UPDATE tickets SET status = {_case('status', {v: k for k, v in TICKET_STATUS_EN_PT.items() if k not in ('WAITING',)}, 'status')}, "
          f"priority = {_case('priority', {v: k for k, v in TICKET_PRIORITY_EN_PT.items() if k not in ('NORMAL', 'URGENT')}, 'priority')}")
    op.alter_column("ticket_messages", "body", new_column_name="content", existing_type=sa.TEXT(), existing_nullable=False)


def _downgrade_existing_tables() -> None:
    op.drop_constraint(op.f('fk_tickets_org_id_organizations'), 'tickets', type_='foreignkey')
    op.drop_constraint(op.f('fk_tickets_advertiser_org_id_organizations'), 'tickets', type_='foreignkey')
    op.create_foreign_key('fk_tickets_creative_id_creatives', 'tickets', 'creatives', ['creative_id'], ['id'], ondelete='SET NULL')
    op.drop_constraint('uq_tickets_number', 'tickets', type_='unique')
    op.drop_index('ix_tickets_sla_due_at', table_name='tickets')
    op.drop_index('ix_tickets_org_id', table_name='tickets')
    op.drop_index('ix_tickets_creative_code', table_name='tickets')
    op.drop_index('ix_tickets_city_key', table_name='tickets')
    op.create_index('ix_tickets_priority', 'tickets', ['priority'], unique=False)
    op.create_index('ix_tickets_created_by', 'tickets', ['created_by'], unique=False)
    op.alter_column('tickets', 'priority',
               existing_type=sa.String(length=10),
               type_=sa.VARCHAR(length=20),
               existing_nullable=False)
    op.alter_column('tickets', 'description',
               existing_type=sa.TEXT(),
               nullable=True)
    op.alter_column('tickets', 'title',
               existing_type=sa.String(length=200),
               type_=sa.VARCHAR(length=255),
               existing_nullable=False)
    op.alter_column('tickets', 'category',
               existing_type=sa.String(length=20),
               type_=sa.VARCHAR(length=100),
               nullable=True)
    op.drop_column('tickets', 'version')
    op.drop_column('tickets', 'closed_at')
    op.drop_column('tickets', 'first_response_at')
    op.drop_column('tickets', 'sla_due_at')
    op.drop_column('tickets', 'city_key')
    op.drop_column('tickets', 'advertiser_org_id')
    op.drop_column('tickets', 'campaign_id')
    op.drop_column('tickets', 'creative_code')
    op.drop_column('tickets', 'screen_id')
    op.drop_column('tickets', 'org_id')
    op.drop_column('tickets', 'number')
    op.alter_column('ticket_messages', 'created_at',
               existing_type=postgresql.TIMESTAMP(timezone=True),
               server_default=None,
               existing_nullable=False)
    op.drop_constraint(op.f('fk_screens_created_by_users'), 'screens', type_='foreignkey')
    op.drop_constraint(op.f('fk_screens_installed_by_users'), 'screens', type_='foreignkey')
    op.drop_index('ix_screens_sellable', table_name='screens')
    op.alter_column('screens', 'inches',
               existing_type=sa.Integer(),
               type_=sa.DOUBLE_PRECISION(precision=53),
               existing_nullable=True)
    op.alter_column('screens', 'status',
               existing_type=sa.VARCHAR(length=20),
               server_default=None,
               existing_nullable=False)
    op.drop_column('screens', 'created_by')
    op.drop_column('screens', 'audience_updated_at')
    op.drop_column('screens', 'audience_method')
    op.drop_column('screens', 'estimated_daily_audience')
    op.drop_column('screens', 'programming_mode')
    op.drop_column('screens', 'weight_milli')
    op.drop_column('screens', 'screen_class')
    op.drop_column('screens', 'installed_by')
    op.drop_column('screens', 'player_serial_number')
    op.drop_column('screens', 'player_model')
    op.drop_column('screens', 'player_brand')
    op.drop_column('screens', 'player_type')
    op.drop_column('screens', 'display_type')
    op.drop_column('screens', 'decommission_reason')
    op.drop_column('screens', 'decommissioned_at')
    op.drop_column('screens', 'sellable')
    op.drop_constraint(op.f('fk_proposals_discount_approved_by_users'), 'proposals', type_='foreignkey')
    op.drop_constraint(op.f('fk_proposals_seller_id_users'), 'proposals', type_='foreignkey')
    op.create_foreign_key('fk_proposals_created_by_users', 'proposals', 'users', ['created_by'], ['id'], ondelete='RESTRICT')
    op.create_foreign_key('fk_proposals_approved_by_users', 'proposals', 'users', ['approved_by'], ['id'], ondelete='RESTRICT')
    op.drop_constraint('uq_proposals_number', 'proposals', type_='unique')
    op.drop_index('ix_proposals_seller_id', table_name='proposals')
    op.drop_index('ix_proposals_city_key', table_name='proposals')
    op.create_index('ix_proposals_created_by', 'proposals', ['created_by'], unique=False)
    op.alter_column('proposals', 'total_cents',
               existing_type=sa.BigInteger(),
               type_=sa.INTEGER(),
               existing_nullable=False)
    op.alter_column('proposals', 'status',
               existing_type=sa.String(length=25),
               type_=sa.VARCHAR(length=20),
               existing_nullable=False)
    op.drop_column('proposals', 'version')
    op.drop_column('proposals', 'notes')
    op.drop_column('proposals', 'pdf_sha256')
    op.drop_column('proposals', 'pdf_key')
    op.drop_column('proposals', 'order_id')
    op.drop_column('proposals', 'converted_at')
    op.drop_column('proposals', 'decided_note')
    op.drop_column('proposals', 'accepted_at')
    op.drop_column('proposals', 'sent_at')
    op.drop_column('proposals', 'expires_at')
    op.drop_column('proposals', 'validity_days')
    op.drop_column('proposals', 'discount_decision_reason')
    op.drop_column('proposals', 'discount_approved_at')
    op.drop_column('proposals', 'discount_approved_by')
    op.drop_column('proposals', 'discount_justification')
    op.drop_column('proposals', 'discount_approval_required')
    op.drop_column('proposals', 'discount_limit_bps_snapshot')
    op.drop_column('proposals', 'discount_cents')
    op.drop_column('proposals', 'discount_bps')
    op.drop_column('proposals', 'subtotal_cents')
    op.drop_column('proposals', 'city_key')
    op.drop_column('proposals', 'seller_id')
    op.drop_column('proposals', 'number')
    op.alter_column('proposal_items', 'unit_price_cents',
               existing_type=sa.BigInteger(),
               type_=sa.INTEGER(),
               existing_nullable=False)
    op.drop_column('proposal_items', 'details')
    op.drop_column('proposal_items', 'line_total_cents')
    op.drop_column('proposal_items', 'package_code')
    op.drop_column('proposal_items', 'position')
    op.drop_constraint(op.f('fk_pairing_requests_screen_id_screens'), 'pairing_requests', type_='foreignkey')
    op.drop_constraint(op.f('fk_pairing_requests_claimed_by_user_id_users'), 'pairing_requests', type_='foreignkey')
    op.drop_constraint(op.f('fk_pairing_requests_device_id_devices'), 'pairing_requests', type_='foreignkey')
    op.create_foreign_key('fk_pairing_requests_establishment_id_establishments', 'pairing_requests', 'establishments', ['establishment_id'], ['id'], ondelete='RESTRICT')
    op.create_foreign_key('fk_pairing_requests_device_id_devices', 'pairing_requests', 'devices', ['device_id'], ['id'], ondelete='RESTRICT')
    op.drop_constraint(op.f('uq_pairing_requests_pairing_code'), 'pairing_requests', type_='unique')
    op.drop_index('ix_pairing_requests_status_expires', table_name='pairing_requests')
    op.create_unique_constraint('uq_pairing_requests_qr_token', 'pairing_requests', ['qr_token'])
    op.create_index('ix_pairing_requests_qr_token', 'pairing_requests', ['qr_token'], unique=False)
    op.create_index('ix_pairing_requests_pairing_code', 'pairing_requests', ['pairing_code'], unique=False)
    op.create_index('ix_pairing_requests_establishment_id', 'pairing_requests', ['establishment_id'], unique=False)
    op.alter_column('pairing_requests', 'created_at',
               existing_type=postgresql.TIMESTAMP(timezone=True),
               server_default=None,
               existing_nullable=False)
    op.alter_column('pairing_requests', 'pairing_code',
               existing_type=sa.String(length=9),
               type_=sa.VARCHAR(length=20),
               existing_nullable=False)
    op.drop_column('pairing_requests', 'result_fetched_at')
    op.drop_column('pairing_requests', 'screen_id')
    op.drop_column('pairing_requests', 'claimed_by_user_id')
    op.drop_column('pairing_requests', 'claim_method')
    op.drop_column('pairing_requests', 'claimed_at')
    op.drop_column('pairing_requests', 'status')
    op.drop_column('pairing_requests', 'device_info')
    op.drop_column('pairing_requests', 'app_version')
    op.drop_column('pairing_requests', 'platform')
    op.drop_column('pairing_requests', 'key_fingerprint')
    op.drop_column('pairing_requests', 'key_alg')
    op.drop_column('pairing_requests', 'public_key')
    op.drop_constraint(op.f('fk_organizations_created_by_users'), 'organizations', type_='foreignkey')
    op.drop_constraint(op.f('fk_organizations_kyc_reviewed_by_users'), 'organizations', type_='foreignkey')
    op.drop_index('ix_organizations_city_key', table_name='organizations')
    op.drop_index('ix_organizations_business_roles', table_name='organizations', postgresql_using='gin')
    op.drop_column('organizations', 'created_by')
    op.drop_column('organizations', 'notes')
    op.drop_column('organizations', 'kyc_notes')
    op.drop_column('organizations', 'kyc_reviewed_at')
    op.drop_column('organizations', 'kyc_reviewed_by')
    op.drop_column('organizations', 'kyc_status')
    op.drop_column('organizations', 'deactivation_reason')
    op.drop_column('organizations', 'deactivated_at')
    op.drop_column('organizations', 'city_key')
    op.drop_column('organizations', 'postal_code')
    op.drop_column('organizations', 'state')
    op.drop_column('organizations', 'city')
    op.drop_column('organizations', 'neighborhood')
    op.drop_column('organizations', 'complement')
    op.drop_column('organizations', 'number')
    op.drop_column('organizations', 'address')
    op.drop_column('organizations', 'responsible_name')
    op.drop_column('organizations', 'business_roles')
    op.add_column('notifications', sa.Column('type', sa.VARCHAR(length=100), autoincrement=False, nullable=False))
    op.add_column('notifications', sa.Column('sent_at', postgresql.TIMESTAMP(timezone=True), autoincrement=False, nullable=True))
    op.add_column('notifications', sa.Column('idempotency_key', sa.VARCHAR(length=255), autoincrement=False, nullable=False))
    op.add_column('notifications', sa.Column('channel', sa.VARCHAR(length=20), autoincrement=False, nullable=False))
    op.drop_constraint(op.f('fk_notifications_org_id_organizations'), 'notifications', type_='foreignkey')
    op.drop_constraint('uq_notifications_user_dedupe', 'notifications', type_='unique')
    op.drop_index('ix_notifications_user_unread', table_name='notifications', postgresql_where=sa.text('read_at IS NULL'))
    op.drop_index('ix_notifications_user_created', table_name='notifications')
    op.create_unique_constraint('uq_notifications_idempotency_key', 'notifications', ['idempotency_key'])
    op.create_unique_constraint('uq_notifications_idempotency', 'notifications', ['idempotency_key'])
    op.create_index('ix_notifications_user_id', 'notifications', ['user_id'], unique=False)
    op.create_index('ix_notifications_read_at', 'notifications', ['read_at'], unique=False)
    op.create_index('ix_notifications_created_at', 'notifications', ['created_at'], unique=False)
    op.create_index('ix_notifications_channel', 'notifications', ['channel'], unique=False)
    op.alter_column('notifications', 'created_at',
               existing_type=postgresql.TIMESTAMP(timezone=True),
               server_default=None,
               existing_nullable=False)
    op.drop_column('notifications', 'dedupe_key')
    op.drop_column('notifications', 'link')
    op.drop_column('notifications', 'entity_id')
    op.drop_column('notifications', 'entity_type')
    op.drop_column('notifications', 'event_type')
    op.drop_column('notifications', 'org_id')
    op.drop_column('notification_preferences', 'updated_at')
    op.drop_constraint(op.f('fk_media_files_deleted_by_users'), 'media_files', type_='foreignkey')
    op.drop_index('uq_media_files_org_id_source_sha256', table_name='media_files', postgresql_where=sa.text('deleted_at IS NULL'))
    op.drop_index('ix_media_files_sha256', table_name='media_files')
    op.drop_index('ix_media_files_pending_purge', table_name='media_files', postgresql_where=sa.text('deleted_at IS NOT NULL AND purged_at IS NULL'))
    op.drop_index('ix_media_files_org_id_created_at', table_name='media_files')
    op.create_index('ix_media_files_org_id', 'media_files', ['org_id'], unique=False)
    op.create_index('ix_media_files_checksum', 'media_files', ['checksum_sha256'], unique=False)
    op.alter_column('media_files', 'orientation',
               existing_type=sa.String(length=12),
               type_=sa.VARCHAR(length=20),
               existing_nullable=True)
    op.alter_column('media_files', 'size_bytes',
               existing_type=sa.BigInteger(),
               type_=sa.INTEGER(),
               nullable=False)
    op.alter_column('media_files', 'mime_type',
               existing_type=sa.VARCHAR(length=100),
               nullable=False)
    op.alter_column('media_files', 'status',
               existing_type=sa.VARCHAR(length=20),
               server_default=None,
               existing_nullable=False)
    op.alter_column('media_files', 'original_filename',
               existing_type=sa.String(length=255),
               type_=sa.VARCHAR(length=500),
               existing_nullable=False)
    op.alter_column('media_files', 'created_by',
               existing_type=sa.UUID(),
               nullable=False)
    op.drop_column('media_files', 'purged_at')
    op.drop_column('media_files', 'deleted_by')
    op.drop_column('media_files', 'deleted_at')
    op.drop_column('media_files', 'preview_size_bytes')
    op.drop_column('media_files', 'preview_mime')
    op.drop_column('media_files', 'preview_storage_key')
    op.drop_column('media_files', 'frame_count')
    op.drop_column('media_files', 'animated')
    op.drop_column('media_files', 'fps_milli')
    op.drop_column('media_files', 'codec')
    op.drop_column('media_files', 'duration_ms')
    op.drop_column('media_files', 'sha256')
    op.drop_column('media_files', 'storage_key')
    op.drop_column('media_files', 'ready_at')
    op.drop_column('media_files', 'lease_until')
    op.drop_column('media_files', 'attempts')
    op.drop_column('media_files', 'progress_pct')
    op.drop_column('media_files', 'status_message')
    op.drop_column('media_files', 'status_code')
    op.drop_column('media_files', 'source_metadata')
    op.drop_column('media_files', 'source_has_audio')
    op.drop_column('media_files', 'source_fps_milli')
    op.drop_column('media_files', 'source_codec')
    op.drop_column('media_files', 'source_duration_ms')
    op.drop_column('media_files', 'source_height')
    op.drop_column('media_files', 'source_width')
    op.drop_column('media_files', 'source_storage_key')
    op.drop_column('media_files', 'source_sha256')
    op.drop_column('media_files', 'source_size_bytes')
    op.drop_column('media_files', 'source_mime')
    op.drop_column('media_files', 'source_format')
    op.drop_column('media_files', 'kind')
    op.drop_column('media_files', 'extension_mismatch')
    op.drop_column('media_files', 'declared_mime')
    op.drop_column('media_files', 'original_extension')
    op.drop_column('media_files', 'name')
    op.drop_column('media_files', 'origin')
    op.drop_constraint(op.f('fk_leads_created_by_users'), 'leads', type_='foreignkey')
    op.drop_constraint(op.f('fk_leads_org_id_organizations'), 'leads', type_='foreignkey')
    op.drop_constraint(op.f('fk_leads_owner_id_users'), 'leads', type_='foreignkey')
    op.create_foreign_key('fk_leads_assigned_to_users', 'leads', 'users', ['assigned_to'], ['id'], ondelete='SET NULL')
    op.drop_index('ix_leads_stage', table_name='leads')
    op.drop_index('ix_leads_owner_id', table_name='leads')
    op.drop_index('ix_leads_org_id', table_name='leads')
    op.drop_index('ix_leads_city_key', table_name='leads')
    op.create_index('ix_leads_status', 'leads', ['status'], unique=False)
    op.create_index('ix_leads_assigned_to', 'leads', ['assigned_to'], unique=False)
    op.alter_column('leads', 'source',
               existing_type=sa.String(length=50),
               type_=sa.VARCHAR(length=100),
               nullable=True)
    op.drop_column('leads', 'stage_changed_at')
    op.drop_column('leads', 'created_by')
    op.drop_column('leads', 'lost_reason')
    op.drop_column('leads', 'estimated_value_cents')
    op.drop_column('leads', 'org_id')
    op.drop_column('leads', 'city_key')
    op.drop_column('leads', 'uf')
    op.drop_column('leads', 'city')
    op.drop_column('leads', 'owner_id')
    op.drop_column('leads', 'stage')
    op.drop_column('leads', 'interest')
    op.drop_constraint(op.f('fk_establishments_responsible_technician_id_users'), 'establishments', type_='foreignkey')
    op.drop_constraint(op.f('fk_establishments_created_by_users'), 'establishments', type_='foreignkey')
    op.drop_constraint(op.f('fk_establishments_partner_plan_id_partner_plans'), 'establishments', type_='foreignkey')
    op.drop_constraint(op.f('fk_establishments_category_id_taxonomy_nodes'), 'establishments', type_='foreignkey')
    op.drop_index('ix_establishments_state', table_name='establishments')
    op.drop_index('ix_establishments_region', table_name='establishments')
    op.drop_index('ix_establishments_partner_plan_id', table_name='establishments')
    op.drop_index('ix_establishments_city_key', table_name='establishments')
    op.alter_column('establishments', 'timezone',
               existing_type=sa.String(length=64),
               type_=sa.VARCHAR(length=50),
               existing_nullable=False)
    op.alter_column('establishments', 'postal_code',
               existing_type=sa.String(length=8),
               type_=sa.VARCHAR(length=10),
               existing_nullable=True)
    op.alter_column('establishments', 'country',
               existing_type=sa.VARCHAR(length=3),
               server_default=None,
               existing_nullable=False)
    op.alter_column('establishments', 'state',
               existing_type=sa.VARCHAR(length=2),
               nullable=True)
    op.alter_column('establishments', 'city',
               existing_type=sa.VARCHAR(length=100),
               nullable=True)
    op.drop_column('establishments', 'created_by')
    op.drop_column('establishments', 'notes')
    op.drop_column('establishments', 'deactivation_reason')
    op.drop_column('establishments', 'deactivated_at')
    op.drop_column('establishments', 'installation_date')
    op.drop_column('establishments', 'responsible_technician_id')
    op.drop_column('establishments', 'onboarding_status')
    op.drop_column('establishments', 'monthly_fee_cents')
    op.drop_column('establishments', 'pays_monthly_fee')
    op.drop_column('establishments', 'receives_commission')
    op.drop_column('establishments', 'partner_plan_id')
    op.drop_column('establishments', 'audience_updated_at')
    op.drop_column('establishments', 'audience_method')
    op.drop_column('establishments', 'estimated_daily_visitors')
    op.drop_column('establishments', 'weight_milli')
    op.drop_column('establishments', 'venue_class')
    op.drop_column('establishments', 'timezone_source')
    op.drop_column('establishments', 'geo_captured_at')
    op.drop_column('establishments', 'geo_accuracy_m')
    op.drop_column('establishments', 'geo_source')
    op.drop_column('establishments', 'region')
    op.drop_column('establishments', 'city_ibge_code')
    op.drop_column('establishments', 'city_key')
    op.drop_column('establishments', 'neighborhood_key')
    op.drop_column('establishments', 'document_type')
    op.drop_constraint(op.f('fk_equipment_movements_screen_id_screens'), 'equipment_movements', type_='foreignkey')
    op.drop_constraint(op.f('fk_equipment_movements_establishment_id_establishments'), 'equipment_movements', type_='foreignkey')
    op.drop_constraint(op.f('fk_equipment_movements_comodato_agreement_id_comodato_a_6462'), 'equipment_movements', type_='foreignkey')
    op.drop_constraint(op.f('fk_equipment_movements_replaced_item_id_equipment_items'), 'equipment_movements', type_='foreignkey')
    op.drop_constraint(op.f('fk_equipment_movements_item_id_equipment_items'), 'equipment_movements', type_='foreignkey')
    op.drop_constraint(op.f('fk_equipment_movements_org_id_organizations'), 'equipment_movements', type_='foreignkey')
    op.create_foreign_key('fk_equipment_movements_item_id_equipment_items', 'equipment_movements', 'equipment_items', ['item_id'], ['id'], ondelete='CASCADE')
    op.drop_constraint(op.f('uq_equipment_movements_seq'), 'equipment_movements', type_='unique')
    op.drop_index('ix_equipment_movements_screen_id', table_name='equipment_movements')
    op.drop_index('ix_equipment_movements_item_id_seq', table_name='equipment_movements')
    op.drop_index('ix_equipment_movements_group_id', table_name='equipment_movements')
    op.drop_index('ix_equipment_movements_establishment_id', table_name='equipment_movements')
    op.create_index('ix_equipment_movements_item_id', 'equipment_movements', ['item_id'], unique=False)
    op.alter_column('equipment_movements', 'created_at',
               existing_type=postgresql.TIMESTAMP(timezone=True),
               server_default=None,
               existing_nullable=False)
    op.alter_column('equipment_movements', 'performed_by',
               existing_type=sa.UUID(),
               nullable=False)
    op.alter_column('equipment_movements', 'to_status',
               existing_type=sa.String(length=16),
               type_=sa.VARCHAR(length=20),
               nullable=False)
    op.alter_column('equipment_movements', 'from_status',
               existing_type=sa.String(length=16),
               type_=sa.VARCHAR(length=20),
               existing_nullable=True)
    op.drop_column('equipment_movements', 'expected_return_at')
    op.drop_column('equipment_movements', 'reference')
    op.drop_column('equipment_movements', 'replaced_item_id')
    op.drop_column('equipment_movements', 'comodato_agreement_id')
    op.drop_column('equipment_movements', 'org_id')
    op.drop_column('equipment_movements', 'screen_id')
    op.drop_column('equipment_movements', 'establishment_id')
    op.drop_column('equipment_movements', 'to_city_key')
    op.drop_column('equipment_movements', 'to_ref_id')
    op.drop_column('equipment_movements', 'to_kind')
    op.drop_column('equipment_movements', 'from_city_key')
    op.drop_column('equipment_movements', 'from_ref_id')
    op.drop_column('equipment_movements', 'from_kind')
    op.drop_column('equipment_movements', 'quantity')
    op.drop_column('equipment_movements', 'movement_type')
    op.drop_column('equipment_movements', 'group_id')
    op.drop_column('equipment_movements', 'seq')
    op.add_column('equipment_items', sa.Column('org_id', sa.UUID(), autoincrement=False, nullable=True))
    op.add_column('equipment_items', sa.Column('screen_id', sa.UUID(), autoincrement=False, nullable=True))
    op.add_column('equipment_items', sa.Column('purchased_at', sa.DATE(), autoincrement=False, nullable=True))
    op.drop_constraint(op.f('fk_equipment_items_location_user_id_users'), 'equipment_items', type_='foreignkey')
    op.drop_constraint(op.f('fk_equipment_items_location_establishment_id_establishments'), 'equipment_items', type_='foreignkey')
    op.drop_constraint(op.f('fk_equipment_items_created_by_users'), 'equipment_items', type_='foreignkey')
    op.drop_constraint(op.f('fk_equipment_items_location_screen_id_screens'), 'equipment_items', type_='foreignkey')
    op.drop_constraint(op.f('fk_equipment_items_comodato_agreement_id_comodato_agreements'), 'equipment_items', type_='foreignkey')
    op.drop_constraint(op.f('fk_equipment_items_scope_org_id_organizations'), 'equipment_items', type_='foreignkey')
    op.create_foreign_key('fk_equipment_items_screen_id_screens', 'equipment_items', 'screens', ['screen_id'], ['id'], ondelete='SET NULL')
    op.create_foreign_key('fk_equipment_items_org_id_organizations', 'equipment_items', 'organizations', ['org_id'], ['id'], ondelete='SET NULL')
    op.drop_index('uq_equipment_items_type_serial', table_name='equipment_items', postgresql_where=sa.text('serial_number IS NOT NULL'))
    op.drop_index('uq_equipment_items_asset_tag', table_name='equipment_items', postgresql_where=sa.text('asset_tag IS NOT NULL'))
    op.drop_index('ix_equipment_items_type', table_name='equipment_items')
    op.drop_index('ix_equipment_items_scope_org_id', table_name='equipment_items')
    op.drop_index('ix_equipment_items_location_screen_id', table_name='equipment_items')
    op.drop_index('ix_equipment_items_location_establishment_id', table_name='equipment_items')
    op.drop_index('ix_equipment_items_location_city_key', table_name='equipment_items')
    op.create_index('ix_equipment_items_serial_number', 'equipment_items', ['serial_number'], unique=False)
    op.create_index('ix_equipment_items_screen_id', 'equipment_items', ['screen_id'], unique=False)
    op.create_index('ix_equipment_items_org_id', 'equipment_items', ['org_id'], unique=False)
    op.create_index('ix_equipment_items_asset_tag', 'equipment_items', ['asset_tag'], unique=False)
    op.alter_column('equipment_items', 'created_at',
               existing_type=postgresql.TIMESTAMP(timezone=True),
               server_default=None,
               existing_nullable=False)
    op.alter_column('equipment_items', 'status',
               existing_type=sa.String(length=16),
               type_=sa.VARCHAR(length=20),
               existing_nullable=False)
    op.alter_column('equipment_items', 'type',
               existing_type=sa.String(length=12),
               type_=sa.VARCHAR(length=20),
               existing_nullable=False)
    op.drop_column('equipment_items', 'updated_at')
    op.drop_column('equipment_items', 'created_by')
    op.drop_column('equipment_items', 'last_movement_at')
    op.drop_column('equipment_items', 'movement_count')
    op.drop_column('equipment_items', 'expected_return_at')
    op.drop_column('equipment_items', 'comodato_agreement_id')
    op.drop_column('equipment_items', 'scope_org_id')
    op.drop_column('equipment_items', 'location_user_id')
    op.drop_column('equipment_items', 'location_screen_id')
    op.drop_column('equipment_items', 'location_establishment_id')
    op.drop_column('equipment_items', 'location_city_key')
    op.drop_column('equipment_items', 'location_kind')
    op.drop_column('equipment_items', 'purchase_cost_cents')
    op.drop_column('equipment_items', 'purchase_date')
    op.drop_column('equipment_items', 'supplier')
    op.drop_column('equipment_items', 'condition')
    op.drop_column('equipment_items', 'unit')
    op.drop_column('equipment_items', 'inches')
    op.drop_column('equipment_items', 'name')
    op.drop_column('equipment_items', 'tracking')
    op.drop_constraint(op.f('fk_devices_screen_id_screens'), 'devices', type_='foreignkey')
    op.drop_constraint(op.f('fk_devices_paired_by_user_id_users'), 'devices', type_='foreignkey')
    op.drop_constraint(op.f('fk_devices_revoked_by_user_id_users'), 'devices', type_='foreignkey')
    op.create_foreign_key('fk_devices_screen_id_screens', 'devices', 'screens', ['screen_id'], ['id'], ondelete='SET NULL')
    op.drop_constraint(op.f('uq_devices_key_fingerprint'), 'devices', type_='unique')
    op.drop_index('uq_devices_active_screen', table_name='devices', postgresql_where=sa.text("status = 'ACTIVE'"))
    op.drop_index('ix_devices_status', table_name='devices')
    op.create_unique_constraint('uq_devices_device_token', 'devices', ['device_token'])
    op.create_index('ix_devices_device_token', 'devices', ['device_token'], unique=True)
    op.alter_column('devices', 'updated_at',
               existing_type=postgresql.TIMESTAMP(timezone=True),
               server_default=None,
               nullable=True)
    op.alter_column('devices', 'created_at',
               existing_type=postgresql.TIMESTAMP(timezone=True),
               server_default=None,
               existing_nullable=False)
    op.alter_column('devices', 'paired_at',
               existing_type=postgresql.TIMESTAMP(timezone=True),
               nullable=True)
    op.alter_column('devices', 'os_version',
               existing_type=sa.String(length=80),
               type_=sa.VARCHAR(length=50),
               existing_nullable=True)
    op.alter_column('devices', 'app_version',
               existing_type=sa.String(length=40),
               type_=sa.VARCHAR(length=30),
               existing_nullable=True)
    op.alter_column('devices', 'screen_id',
               existing_type=sa.UUID(),
               nullable=True)
    op.drop_column('devices', 'pop_last_hash')
    op.drop_column('devices', 'pop_last_seq')
    op.drop_column('devices', 'telemetry_at')
    op.drop_column('devices', 'last_error')
    op.drop_column('devices', 'pending_manifest_version')
    op.drop_column('devices', 'applied_manifest_version')
    op.drop_column('devices', 'last_play_at')
    op.drop_column('devices', 'player_state')
    op.drop_column('devices', 'presence')
    op.drop_column('devices', 'revoked_by_user_id')
    op.drop_column('devices', 'revoked_reason')
    op.drop_column('devices', 'revoked_at')
    op.drop_column('devices', 'paired_by_user_id')
    op.drop_column('devices', 'pairing_method')
    op.drop_column('devices', 'status')
    op.drop_column('devices', 'update_group')
    op.drop_column('devices', 'device_info')
    op.drop_column('devices', 'key_fingerprint')
    op.drop_column('devices', 'key_alg')
    op.drop_column('devices', 'public_key')
    op.drop_constraint(op.f('fk_creatives_created_by_users'), 'creatives', type_='foreignkey')
    op.drop_constraint(op.f('fk_creatives_brand_id_brands'), 'creatives', type_='foreignkey')
    op.drop_constraint(op.f('fk_creatives_root_id_creatives'), 'creatives', type_='foreignkey')
    op.drop_constraint(op.f('fk_creatives_product_id_products'), 'creatives', type_='foreignkey')
    op.drop_constraint(op.f('fk_creatives_supersedes_id_creatives'), 'creatives', type_='foreignkey')
    op.drop_constraint('uq_creatives_code', 'creatives', type_='unique')
    op.drop_index('ix_creatives_root_id', table_name='creatives')
    op.drop_index('ix_creatives_media_file_id', table_name='creatives')
    op.create_unique_constraint('uq_creatives_creative_id', 'creatives', ['creative_id'])
    op.create_index('ix_creatives_creative_id', 'creatives', ['creative_id'], unique=True)
    op.alter_column('creatives', 'duration_seconds',
               existing_type=sa.SmallInteger(),
               type_=sa.INTEGER(),
               existing_nullable=False)
    op.drop_column('creatives', 'archived_at')
    op.drop_column('creatives', 'created_by')
    op.drop_column('creatives', 'root_id')
    op.drop_column('creatives', 'supersedes_id')
    op.drop_column('creatives', 'version')
    op.drop_column('creatives', 'brand_id')
    op.drop_column('creatives', 'product_id')
    op.drop_column('creatives', 'technical_error')
    op.drop_column('creatives', 'technical_status')
    op.drop_column('creatives', 'ad_text')
    op.drop_column('creatives', 'media_duration_ms')
    op.drop_column('creatives', 'orientation')
    op.drop_column('creatives', 'kind')
    op.drop_constraint(op.f('fk_campaigns_cancelled_by_users'), 'campaigns', type_='foreignkey')
    op.drop_constraint(op.f('fk_campaigns_brand_id_brands'), 'campaigns', type_='foreignkey')
    op.drop_constraint('fk_campaigns_pending_revision', 'campaigns', type_='foreignkey')
    op.drop_constraint('fk_campaigns_live_revision', 'campaigns', type_='foreignkey')
    op.drop_constraint(op.f('fk_campaigns_rejected_by_users'), 'campaigns', type_='foreignkey')
    op.drop_constraint(op.f('fk_campaigns_created_by_users'), 'campaigns', type_='foreignkey')
    op.drop_constraint(op.f('fk_campaigns_seller_user_id_users'), 'campaigns', type_='foreignkey')
    op.drop_constraint(op.f('fk_campaigns_product_id_products'), 'campaigns', type_='foreignkey')
    op.drop_constraint(op.f('fk_campaigns_updated_by_users'), 'campaigns', type_='foreignkey')
    op.drop_constraint(op.f('fk_campaigns_plan_id_plans'), 'campaigns', type_='foreignkey')
    op.drop_index('ix_campaigns_review_due_at', table_name='campaigns')
    op.drop_index('ix_campaigns_end_date', table_name='campaigns')
    op.create_index('ix_campaigns_starts_at', 'campaigns', ['starts_at'], unique=False)
    op.create_index('ix_campaigns_ends_at', 'campaigns', ['ends_at'], unique=False)
    op.drop_column('campaigns', 'cancel_reason')
    op.drop_column('campaigns', 'cancelled_by')
    op.drop_column('campaigns', 'cancelled_at')
    op.drop_column('campaigns', 'finalized_at')
    op.drop_column('campaigns', 'paused_at')
    op.drop_column('campaigns', 'activated_at')
    op.drop_column('campaigns', 'syncing_at')
    op.drop_column('campaigns', 'scheduled_at')
    op.drop_column('campaigns', 'paused_from')
    op.drop_column('campaigns', 'status_before_review')
    op.drop_column('campaigns', 'pending_revision_id')
    op.drop_column('campaigns', 'live_revision_id')
    op.drop_column('campaigns', 'payment_issue')
    op.drop_column('campaigns', 'requires_new_payment')
    op.drop_column('campaigns', 'payment_order_id')
    op.drop_column('campaigns', 'paid_order_id')
    op.drop_column('campaigns', 'paid_at')
    op.drop_column('campaigns', 'contracted_price_cents')
    op.drop_column('campaigns', 'rejection_count')
    op.drop_column('campaigns', 'rejection_reason')
    op.drop_column('campaigns', 'rejected_by')
    op.drop_column('campaigns', 'rejected_at')
    op.drop_column('campaigns', 'review_due_at')
    op.drop_column('campaigns', 'submitted_at')
    op.drop_column('campaigns', 'approval_source')
    op.drop_column('campaigns', 'brand_id')
    op.drop_column('campaigns', 'product_id')
    op.drop_column('campaigns', 'auto_renew')
    op.drop_column('campaigns', 'request_art_service')
    op.drop_column('campaigns', 'allow_same_advertiser_consecutive')
    op.drop_column('campaigns', 'exclusivity')
    op.drop_column('campaigns', 'priority')
    op.drop_column('campaigns', 'time_bands')
    op.drop_column('campaigns', 'days_mask')
    op.drop_column('campaigns', 'min_interval_s')
    op.drop_column('campaigns', 'specific_times')
    op.drop_column('campaigns', 'freq_value')
    op.drop_column('campaigns', 'freq_kind')
    op.drop_column('campaigns', 'duration_s')
    op.drop_column('campaigns', 'end_date')
    op.drop_column('campaigns', 'start_date')
    op.drop_column('campaigns', 'period_type')
    op.drop_column('campaigns', 'plan_id')
    op.drop_column('campaigns', 'seller_user_id')
    op.drop_column('campaigns', 'sales_mode')
    op.drop_column('campaigns', 'version')
    op.drop_column('campaigns', 'updated_by')
    op.drop_column('campaigns', 'created_by')
    op.drop_column('campaigns', 'description')
    op.drop_constraint('uq_campaign_targets_unique', 'campaign_targets', type_='unique')
    op.create_index('ix_campaign_targets_target_type_value', 'campaign_targets', ['target_type', 'target_value'], unique=False)
    op.drop_column('campaign_targets', 'created_at')
    op.drop_constraint(op.f('fk_campaign_creatives_replaces_creative_id_creatives'), 'campaign_creatives', type_='foreignkey')
    op.drop_column('campaign_creatives', 'removed_at')
    op.drop_column('campaign_creatives', 'added_at')
    op.drop_column('campaign_creatives', 'replaces_creative_id')
    op.drop_column('campaign_creatives', 'pending_removal')
    op.drop_column('campaign_creatives', 'pending')


def _drop_new_tables() -> None:
    op.drop_index('ix_maintenance_events_order_id', table_name='maintenance_events')
    op.drop_table('maintenance_events')
    op.drop_index('ix_booking_screens_hold', table_name='booking_screens')
    op.drop_index('ix_booking_screens_campaign', table_name='booking_screens')
    op.drop_index('ix_booking_screens_active', table_name='booking_screens', postgresql_where=sa.text("status IN ('HELD','BOOKED')"))
    op.drop_table('booking_screens')
    op.drop_index('ix_order_items_campaign_id', table_name='order_items')
    op.drop_table('order_items')
    op.drop_index('ix_maintenance_orders_technician_user_id', table_name='maintenance_orders')
    op.drop_index('ix_maintenance_orders_status', table_name='maintenance_orders')
    op.drop_index('ix_maintenance_orders_screen_id', table_name='maintenance_orders')
    op.drop_index('ix_maintenance_orders_establishment_id', table_name='maintenance_orders')
    op.drop_table('maintenance_orders')
    op.drop_index('ix_booking_patterns_hold', table_name='booking_patterns')
    op.drop_index('ix_booking_patterns_campaign', table_name='booking_patterns')
    op.drop_table('booking_patterns')
    op.drop_index('ix_screen_inventory_state_sellable', table_name='screen_inventory_state')
    op.drop_table('screen_inventory_state')
    op.drop_index('ix_refunds_order', table_name='refunds')
    op.drop_table('refunds')
    op.drop_index('ix_playback_events_started_at_brin', table_name='playback_events', postgresql_using='brin')
    op.drop_index('ix_playback_events_screen_started_ms', table_name='playback_events')
    op.drop_index('ix_playback_events_screen_started', table_name='playback_events')
    op.drop_index('ix_playback_events_received_at', table_name='playback_events')
    op.drop_index('ix_playback_events_not_aggregated', table_name='playback_events', postgresql_where=sa.text('aggregated_at IS NULL'))
    op.drop_index('ix_playback_events_fin_pending', table_name='playback_events', postgresql_where=sa.text("financial_status = 'PENDING'"))
    op.drop_index('ix_playback_events_device_started_ms', table_name='playback_events')
    op.drop_index('ix_playback_events_device_nonce', table_name='playback_events')
    op.drop_index('ix_playback_events_campaign_started', table_name='playback_events')
    op.drop_table('playback_events')
    op.drop_index('ix_manifest_versions_screen_generated', table_name='manifest_versions')
    op.drop_index('ix_manifest_versions_manifest_id', table_name='manifest_versions')
    op.drop_table('manifest_versions')
    op.drop_index('ux_inventory_holds_one_held_per_campaign', table_name='inventory_holds', postgresql_where=sa.text("status = 'HELD'"))
    op.drop_index('ix_inventory_holds_order', table_name='inventory_holds')
    op.drop_index('ix_inventory_holds_expiring', table_name='inventory_holds', postgresql_where=sa.text("status = 'HELD'"))
    op.drop_index('ix_inventory_holds_campaign', table_name='inventory_holds')
    op.drop_table('inventory_holds')
    op.drop_index('ix_internal_content_scope', table_name='internal_content')
    op.drop_index('ix_internal_content_creative', table_name='internal_content')
    op.drop_table('internal_content')
    op.drop_index('ix_installation_photos_installation_id', table_name='installation_photos')
    op.drop_table('installation_photos')
    op.drop_index('ix_fallback_content_scope', table_name='fallback_content')
    op.drop_table('fallback_content')
    op.drop_index('ix_emergency_broadcasts_window', table_name='emergency_broadcasts')
    op.drop_table('emergency_broadcasts')
    op.drop_index('ix_device_telemetry_device_recorded', table_name='device_telemetry')
    op.drop_table('device_telemetry')
    op.drop_index('ix_device_sync_events_type_occurred', table_name='device_sync_events')
    op.drop_index('ix_device_sync_events_screen_occurred', table_name='device_sync_events')
    op.drop_index('ix_device_sync_events_org_occurred', table_name='device_sync_events')
    op.drop_table('device_sync_events')
    op.drop_index('ix_device_commands_screen_created', table_name='device_commands')
    op.drop_index('ix_device_commands_device_status', table_name='device_commands')
    op.drop_table('device_commands')
    op.drop_index('ix_design_comments_request_id', table_name='design_comments')
    op.drop_table('design_comments')
    op.drop_index('uq_design_approvals_version_approved', table_name='design_approvals', postgresql_where=sa.text("decision = 'APROVADA'"))
    op.drop_index('ix_design_approvals_request_id', table_name='design_approvals')
    op.drop_table('design_approvals')
    op.drop_table('creative_segments')
    op.drop_index('ix_contract_acceptances_template_id', table_name='contract_acceptances')
    op.drop_index('ix_contract_acceptances_org_id', table_name='contract_acceptances')
    op.drop_index('ix_contract_acceptances_establishment_id', table_name='contract_acceptances')
    op.drop_table('contract_acceptances')
    op.drop_index('ux_conflict_overrides_active', table_name='conflict_overrides', postgresql_where=sa.text('revoked_at IS NULL'))
    op.drop_table('conflict_overrides')
    op.drop_index('ix_campaign_status_history_campaign_id', table_name='campaign_status_history')
    op.drop_table('campaign_status_history')
    op.drop_table('campaign_segments')
    op.drop_index('ix_campaign_revisions_campaign_id', table_name='campaign_revisions')
    op.drop_table('campaign_revisions')
    op.drop_index('ix_withdrawals_status', table_name='withdrawals')
    op.drop_index('ix_withdrawals_establishment', table_name='withdrawals')
    op.drop_table('withdrawals')
    op.drop_index('ix_ticket_events_ticket_id', table_name='ticket_events')
    op.drop_table('ticket_events')
    op.drop_index('ix_screen_retirements_status', table_name='screen_retirements')
    op.drop_table('screen_retirements')
    op.drop_index('ix_screen_history_screen_id_created_at', table_name='screen_history')
    op.drop_index('ix_screen_history_event', table_name='screen_history')
    op.drop_table('screen_history')
    op.drop_index('ix_screen_anchor_history_screen', table_name='screen_anchor_history')
    op.drop_table('screen_anchor_history')
    op.drop_index('ix_sales_attributions_status', table_name='sales_attributions')
    op.drop_index('ix_sales_attributions_seller_id', table_name='sales_attributions')
    op.drop_table('sales_attributions')
    op.drop_index('uq_playback_daily_stats_key', table_name='playback_daily_stats')
    op.drop_index('ix_playback_daily_stats_screen_day', table_name='playback_daily_stats')
    op.drop_index('ix_playback_daily_stats_org_day', table_name='playback_daily_stats')
    op.drop_index('ix_playback_daily_stats_campaign_day', table_name='playback_daily_stats')
    op.drop_table('playback_daily_stats')
    op.drop_index('ix_onboarding_documents_application_id', table_name='onboarding_documents')
    op.drop_table('onboarding_documents')
    op.drop_index('ix_installations_technician_user_id', table_name='installations')
    op.drop_index('ix_installations_screen_id', table_name='installations')
    op.drop_index('ix_installations_establishment_id', table_name='installations')
    op.drop_table('installations')
    op.drop_index('ix_establishment_products_product_id', table_name='establishment_products')
    op.drop_table('establishment_products')
    op.drop_index('ix_design_versions_request_id', table_name='design_versions')
    op.drop_table('design_versions')
    op.drop_index('uq_design_extra_revisions_open', table_name='design_extra_revisions', postgresql_where=sa.text("status IN ('SOLICITADA','AGUARDANDO_PAGAMENTO')"))
    op.drop_index('ix_design_extra_revisions_request_id', table_name='design_extra_revisions')
    op.drop_table('design_extra_revisions')
    op.drop_index('ix_design_events_request_id', table_name='design_events')
    op.drop_table('design_events')
    op.drop_index('ix_design_attachments_request_id', table_name='design_attachments')
    op.drop_table('design_attachments')
    op.drop_index('ix_creative_kills_screen', table_name='creative_kills')
    op.drop_index('ix_creative_kills_creative', table_name='creative_kills')
    op.drop_table('creative_kills')
    op.drop_index('uq_cp_rollout_targets_instance_running', table_name='cp_rollout_targets', postgresql_where=sa.text("status = 'EM_EXECUCAO'"))
    op.drop_index('ix_cp_rollout_targets_rollout_status', table_name='cp_rollout_targets')
    op.drop_table('cp_rollout_targets')
    op.drop_index('ix_comodato_agreements_screen_id', table_name='comodato_agreements')
    op.drop_index('ix_comodato_agreements_org_id', table_name='comodato_agreements')
    op.drop_index('ix_comodato_agreements_establishment_id', table_name='comodato_agreements')
    op.drop_table('comodato_agreements')
    op.drop_table('commission_pool_releases')
    op.drop_table('commission_closings')
    op.drop_index('ix_commission_campaign_screens_est', table_name='commission_campaign_screens')
    op.drop_table('commission_campaign_screens')
    op.drop_index('ix_commission_accruals_est_comp', table_name='commission_accruals')
    op.drop_index('ix_commission_accruals_competence', table_name='commission_accruals')
    op.drop_table('commission_accruals')
    op.drop_index('ix_audience_estimates_screen_id', table_name='audience_estimates')
    op.drop_index('ix_audience_estimates_establishment_id', table_name='audience_estimates')
    op.drop_table('audience_estimates')
    op.drop_index('ix_webhook_events_status', table_name='webhook_events')
    op.drop_index('ix_webhook_events_external_ref', table_name='webhook_events')
    op.drop_table('webhook_events')
    op.drop_index('ix_template_renders_template_id', table_name='template_renders')
    op.drop_table('template_renders')
    op.drop_index('ix_revenue_schedules_status', table_name='revenue_schedules')
    op.drop_index('ix_revenue_schedules_campaign', table_name='revenue_schedules')
    op.drop_table('revenue_schedules')
    op.drop_index('uq_reconciliation_items_open_key', table_name='reconciliation_items', postgresql_where=sa.text("status = 'DIVERGENT'"))
    op.drop_index('ix_reconciliation_items_status', table_name='reconciliation_items')
    op.drop_table('reconciliation_items')
    op.drop_index('ix_products_segment_id', table_name='products')
    op.drop_index('ix_products_brand_id', table_name='products')
    op.drop_table('products')
    op.drop_table('payment_order_callbacks')
    op.drop_index('ix_payables_status_due', table_name='payables')
    op.drop_table('payables')
    op.drop_index('uq_onboarding_applications_token_hash', table_name='onboarding_applications', postgresql_where=sa.text('token_hash IS NOT NULL'))
    op.drop_index('ix_onboarding_applications_status', table_name='onboarding_applications')
    op.drop_index('ix_onboarding_applications_email', table_name='onboarding_applications')
    op.drop_table('onboarding_applications')
    op.drop_index('ix_notification_deliveries_user', table_name='notification_deliveries')
    op.drop_index('ix_notification_deliveries_pending', table_name='notification_deliveries', postgresql_where=sa.text("status IN ('PENDING','SENDING')"))
    op.drop_table('notification_deliveries')
    op.drop_index('ix_ledger_postings_transaction_id', table_name='ledger_postings')
    op.drop_index('ix_ledger_postings_account_competence', table_name='ledger_postings')
    op.drop_table('ledger_postings')
    op.drop_index('ix_lead_stage_events_lead_id', table_name='lead_stage_events')
    op.drop_table('lead_stage_events')
    op.drop_index('ix_lead_activities_lead_id', table_name='lead_activities')
    op.drop_table('lead_activities')
    op.drop_table('fiscal_document_requests')
    op.drop_index('ix_establishment_segments_segment_id', table_name='establishment_segments')
    op.drop_table('establishment_segments')
    op.drop_index('uq_establishment_pix_keys_current', table_name='establishment_pix_keys', postgresql_where=sa.text('revoked_at IS NULL'))
    op.drop_table('establishment_pix_keys')
    op.drop_index('ix_establishment_commission_settings_est', table_name='establishment_commission_settings')
    op.drop_table('establishment_commission_settings')
    op.drop_index('ix_design_requests_status', table_name='design_requests')
    op.drop_index('ix_design_requests_org_id', table_name='design_requests')
    op.drop_index('ix_design_requests_establishment_id', table_name='design_requests')
    op.drop_index('ix_design_requests_assigned_to', table_name='design_requests')
    op.drop_table('design_requests')
    op.drop_index('ix_cp_rollouts_status', table_name='cp_rollouts')
    op.drop_table('cp_rollouts')
    op.drop_index('ix_coupon_redemptions_coupon_org', table_name='coupon_redemptions')
    op.drop_table('coupon_redemptions')
    op.drop_index('ix_conflict_exceptions_establishment', table_name='conflict_exceptions')
    op.drop_table('conflict_exceptions')
    op.drop_table('commission_competence_runs')
    op.drop_table('commission_campaigns')
    op.drop_index('ix_agency_client_grants_user_id', table_name='agency_client_grants')
    op.drop_index('ix_agency_client_grants_link_id', table_name='agency_client_grants')
    op.drop_table('agency_client_grants')
    op.drop_index('ix_template_assets_org_id', table_name='template_assets')
    op.drop_table('template_assets')
    op.drop_table('report_runs')
    op.drop_index('ix_price_items_table_kind', table_name='price_items')
    op.drop_table('price_items')
    op.drop_index('ix_plans_status', table_name='plans')
    op.drop_index('ix_plans_org_id', table_name='plans')
    op.drop_table('plans')
    op.drop_index('ix_payment_orders_status', table_name='payment_orders')
    op.drop_index('ix_payment_orders_reference', table_name='payment_orders')
    op.drop_index('ix_payment_orders_org_id', table_name='payment_orders')
    op.drop_table('payment_orders')
    op.drop_table('organization_approval_policies')
    op.drop_index('ix_orders_org_status', table_name='orders')
    op.drop_index('ix_orders_expires_at', table_name='orders')
    op.drop_table('orders')
    op.drop_index('ix_ledger_transactions_ref', table_name='ledger_transactions')
    op.drop_index('ix_ledger_transactions_org', table_name='ledger_transactions')
    op.drop_index('ix_ledger_transactions_kind', table_name='ledger_transactions')
    op.drop_index('ix_ledger_transactions_external', table_name='ledger_transactions')
    op.drop_index('ix_ledger_transactions_competence', table_name='ledger_transactions')
    op.drop_table('ledger_transactions')
    op.drop_index('ix_financial_approval_events_request', table_name='financial_approval_events')
    op.drop_table('financial_approval_events')
    op.drop_index('ix_design_templates_status', table_name='design_templates')
    op.drop_index('ix_design_templates_org_id', table_name='design_templates')
    op.drop_table('design_templates')
    op.drop_index('ix_delivery_proofs_org_id', table_name='delivery_proofs')
    op.drop_index('ix_delivery_proofs_campaign_id', table_name='delivery_proofs')
    op.drop_table('delivery_proofs')
    op.drop_table('cp_releases')
    op.drop_index('ix_competitor_links_org_b_id', table_name='competitor_links')
    op.drop_table('competitor_links')
    op.drop_index('ix_brands_owner_org_id', table_name='brands')
    op.drop_table('brands')
    op.drop_index('uq_agency_clients_active_pair', table_name='agency_clients', postgresql_where=sa.text("status = 'ACTIVE'"))
    op.drop_index('ix_agency_clients_client_org_id', table_name='agency_clients')
    op.drop_index('ix_agency_clients_agency_org_id', table_name='agency_clients')
    op.drop_table('agency_clients')
    op.drop_index('ix_advertiser_segments_segment_id', table_name='advertiser_segments')
    op.drop_table('advertiser_segments')
    op.drop_index('ix_advertiser_profiles_category_id', table_name='advertiser_profiles')
    op.drop_table('advertiser_profiles')
    op.drop_table('sales_reps')
    op.drop_index('ix_rotation_policies_scope', table_name='rotation_policies')
    op.drop_table('rotation_policies')
    op.drop_index('ix_report_exports_requested_by', table_name='report_exports')
    op.drop_table('report_exports')
    op.drop_table('rd_price_floors')
    op.drop_index(op.f('ix_rd_price_floor_history_key'), table_name='rd_price_floor_history')
    op.drop_table('rd_price_floor_history')
    op.drop_table('promotions')
    op.drop_index('ux_price_tables_one_active', table_name='price_tables', postgresql_where=sa.text("status = 'ATIVA'"))
    op.drop_table('price_tables')
    op.drop_table('partner_plans')
    op.drop_table('notification_templates')
    op.drop_table('notification_cooldowns')
    op.drop_table('instance_profile')
    op.drop_table('financial_period_closings')
    op.drop_index('ix_financial_password_reset_codes_user', table_name='financial_password_reset_codes')
    op.drop_table('financial_password_reset_codes')
    op.drop_index('ix_financial_approval_requests_status', table_name='financial_approval_requests')
    op.drop_table('financial_approval_requests')
    op.drop_index('ix_cp_instances_group_id', table_name='cp_instances')
    op.drop_table('cp_instances')
    op.drop_table('coupons')
    op.drop_index('uq_contract_templates_one_published', table_name='contract_templates', postgresql_where=sa.text("status = 'PUBLISHED'"))
    op.drop_table('contract_templates')
    op.drop_table('content_policies')
    op.drop_index('ix_conflict_rules_subject', table_name='conflict_rules')
    op.drop_index('ix_conflict_rules_object', table_name='conflict_rules')
    op.drop_table('conflict_rules')
    op.drop_index('uq_app_releases_platform_version', table_name='app_releases')
    op.drop_index('ix_app_releases_platform_status', table_name='app_releases')
    op.drop_table('app_releases')
    op.drop_index('ix_taxonomy_nodes_path', table_name='taxonomy_nodes', postgresql_ops={'path': 'text_pattern_ops'})
    op.drop_index('ix_taxonomy_nodes_parent_id', table_name='taxonomy_nodes')
    op.drop_table('taxonomy_nodes')
    op.drop_index('ix_segments_path', table_name='segments', postgresql_ops={'path': 'text_pattern_ops'})
    op.drop_index('ix_segments_parent_id', table_name='segments')
    op.drop_table('segments')
    op.drop_table('reconciliation_runs')
    op.drop_index('ix_ledger_accounts_owner', table_name='ledger_accounts')
    op.drop_index(op.f('ix_ledger_accounts_org_id'), table_name='ledger_accounts')
    op.drop_index('ix_ledger_accounts_code', table_name='ledger_accounts')
    op.drop_table('ledger_accounts')
    op.drop_index('uq_instance_signing_keys_active', table_name='instance_signing_keys', postgresql_where=sa.text('is_active'))
    op.drop_table('instance_signing_keys')
    op.drop_table('cp_instance_groups')
    op.drop_index('ix_cp_events_rollout_id', table_name='cp_events')
    op.drop_index('ix_cp_events_instance_id', table_name='cp_events')
    op.drop_table('cp_events')
    op.drop_index('ix_conflict_decisions_creative', table_name='conflict_decisions')
    op.drop_index('ix_conflict_decisions_campaign_est', table_name='conflict_decisions')
    op.drop_table('conflict_decisions')
    op.drop_index('ix_commission_accrual_events_key', table_name='commission_accrual_events')
    op.drop_table('commission_accrual_events')


def _recreate_legacy_tables() -> None:
    op.create_table('establishment_categories',
    sa.Column('id', sa.UUID(), autoincrement=False, nullable=False),
    sa.Column('name', sa.VARCHAR(length=100), autoincrement=False, nullable=False),
    sa.Column('slug', sa.VARCHAR(length=100), autoincrement=False, nullable=False),
    sa.Column('parent_id', sa.UUID(), autoincrement=False, nullable=True),
    sa.ForeignKeyConstraint(['parent_id'], ['establishment_categories.id'], name='fk_establishment_categories_parent_id_establishment_categories', ondelete='RESTRICT'),
    sa.PrimaryKeyConstraint('id', name='pk_establishment_categories'),
    sa.UniqueConstraint('slug', name='uq_establishment_categories_slug')
    )
    op.create_index('ix_establishment_categories_parent_id', 'establishment_categories', ['parent_id'], unique=False)
    op.create_index('ix_establishment_categories_slug', 'establishment_categories', ['slug'], unique=True)
    op.create_table('ledger_entries',
    sa.Column('id', sa.UUID(), autoincrement=False, nullable=False),
    sa.Column('type', sa.VARCHAR(length=30), autoincrement=False, nullable=False),
    sa.Column('org_id', sa.UUID(), autoincrement=False, nullable=False),
    sa.Column('amount_cents', sa.INTEGER(), autoincrement=False, nullable=False),
    sa.Column('description', sa.VARCHAR(length=500), autoincrement=False, nullable=True),
    sa.Column('ref_type', sa.VARCHAR(length=100), autoincrement=False, nullable=True),
    sa.Column('ref_id', sa.VARCHAR(length=255), autoincrement=False, nullable=True),
    sa.Column('idempotency_key', sa.VARCHAR(length=255), autoincrement=False, nullable=False),
    sa.Column('created_at', postgresql.TIMESTAMP(timezone=True), autoincrement=False, nullable=False),
    sa.Column('created_by', sa.UUID(), autoincrement=False, nullable=True),
    sa.CheckConstraint('amount_cents > 0', name=op.f('ck_ledger_entries_ck_ledger_entries_amount_positive')),
    sa.ForeignKeyConstraint(['created_by'], ['users.id'], name='fk_ledger_entries_created_by_users', ondelete='RESTRICT'),
    sa.ForeignKeyConstraint(['org_id'], ['organizations.id'], name='fk_ledger_entries_org_id_organizations', ondelete='RESTRICT'),
    sa.PrimaryKeyConstraint('id', name='pk_ledger_entries'),
    sa.UniqueConstraint('idempotency_key', name='uq_ledger_entries_idempotency'),
    sa.UniqueConstraint('idempotency_key', name='uq_ledger_entries_idempotency_key')
    )
    op.create_index('ix_ledger_entries_created_at', 'ledger_entries', ['created_at'], unique=False)
    op.create_index('ix_ledger_entries_org_id_type', 'ledger_entries', ['org_id', 'type'], unique=False)
    op.create_index('ix_ledger_entries_ref', 'ledger_entries', ['ref_type', 'ref_id'], unique=False)
    op.create_table('player_manifests',
    sa.Column('id', sa.UUID(), autoincrement=False, nullable=False),
    sa.Column('screen_id', sa.UUID(), autoincrement=False, nullable=False),
    sa.Column('version', sa.INTEGER(), autoincrement=False, nullable=False),
    sa.Column('content', postgresql.JSONB(astext_type=Text()), autoincrement=False, nullable=False),
    sa.Column('activated_at', postgresql.TIMESTAMP(timezone=True), autoincrement=False, nullable=True),
    sa.Column('expires_at', postgresql.TIMESTAMP(timezone=True), autoincrement=False, nullable=True),
    sa.Column('checksum', sa.VARCHAR(length=64), autoincrement=False, nullable=True),
    sa.Column('created_at', postgresql.TIMESTAMP(timezone=True), autoincrement=False, nullable=False),
    sa.ForeignKeyConstraint(['screen_id'], ['screens.id'], name='fk_player_manifests_screen_id_screens', ondelete='RESTRICT'),
    sa.PrimaryKeyConstraint('id', name='pk_player_manifests'),
    sa.UniqueConstraint('screen_id', 'version', name='uq_player_manifests_screen_version')
    )
    op.create_index('ix_player_manifests_activated_at', 'player_manifests', ['activated_at'], unique=False)
    op.create_index('ix_player_manifests_expires_at', 'player_manifests', ['expires_at'], unique=False)
    op.create_index('ix_player_manifests_screen_id', 'player_manifests', ['screen_id'], unique=False)
    op.create_table('campaign_schedules',
    sa.Column('id', sa.UUID(), autoincrement=False, nullable=False),
    sa.Column('campaign_id', sa.UUID(), autoincrement=False, nullable=False),
    sa.Column('day_type', sa.VARCHAR(length=20), autoincrement=False, nullable=False),
    sa.Column('days_of_week', postgresql.JSONB(astext_type=Text()), autoincrement=False, nullable=True),
    sa.Column('time_from', postgresql.TIME(), autoincrement=False, nullable=True),
    sa.Column('time_to', postgresql.TIME(), autoincrement=False, nullable=True),
    sa.Column('timezone', sa.VARCHAR(length=50), autoincrement=False, nullable=False),
    sa.ForeignKeyConstraint(['campaign_id'], ['campaigns.id'], name='fk_campaign_schedules_campaign_id_campaigns', ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id', name='pk_campaign_schedules')
    )
    op.create_index('ix_campaign_schedules_campaign_id', 'campaign_schedules', ['campaign_id'], unique=False)
    op.create_table('payouts',
    sa.Column('id', sa.UUID(), autoincrement=False, nullable=False),
    sa.Column('establishment_id', sa.UUID(), autoincrement=False, nullable=False),
    sa.Column('amount_cents', sa.INTEGER(), autoincrement=False, nullable=False),
    sa.Column('pix_key', sa.VARCHAR(length=255), autoincrement=False, nullable=True),
    sa.Column('status', sa.VARCHAR(length=20), autoincrement=False, nullable=False),
    sa.Column('requested_by', sa.UUID(), autoincrement=False, nullable=False),
    sa.Column('approved_by', sa.UUID(), autoincrement=False, nullable=True),
    sa.Column('requested_at', postgresql.TIMESTAMP(timezone=True), autoincrement=False, nullable=False),
    sa.Column('approved_at', postgresql.TIMESTAMP(timezone=True), autoincrement=False, nullable=True),
    sa.Column('paid_at', postgresql.TIMESTAMP(timezone=True), autoincrement=False, nullable=True),
    sa.Column('gateway_ref', sa.VARCHAR(length=255), autoincrement=False, nullable=True),
    sa.ForeignKeyConstraint(['approved_by'], ['users.id'], name='fk_payouts_approved_by_users', ondelete='RESTRICT'),
    sa.ForeignKeyConstraint(['establishment_id'], ['establishments.id'], name='fk_payouts_establishment_id_establishments', ondelete='RESTRICT'),
    sa.ForeignKeyConstraint(['requested_by'], ['users.id'], name='fk_payouts_requested_by_users', ondelete='RESTRICT'),
    sa.PrimaryKeyConstraint('id', name='pk_payouts')
    )
    op.create_index('ix_payouts_establishment_id', 'payouts', ['establishment_id'], unique=False)
    op.create_index('ix_payouts_status', 'payouts', ['status'], unique=False)
    op.create_table('inventory_reservations',
    sa.Column('id', sa.UUID(), autoincrement=False, nullable=False),
    sa.Column('campaign_id', sa.UUID(), autoincrement=False, nullable=False),
    sa.Column('org_id', sa.UUID(), autoincrement=False, nullable=False),
    sa.Column('slots_count', sa.INTEGER(), autoincrement=False, nullable=False),
    sa.Column('total_duration_seconds', sa.INTEGER(), autoincrement=False, nullable=False),
    sa.Column('reserved_at', postgresql.TIMESTAMP(timezone=True), autoincrement=False, nullable=False),
    sa.Column('expires_at', postgresql.TIMESTAMP(timezone=True), autoincrement=False, nullable=False),
    sa.Column('confirmed_at', postgresql.TIMESTAMP(timezone=True), autoincrement=False, nullable=True),
    sa.Column('cancelled_at', postgresql.TIMESTAMP(timezone=True), autoincrement=False, nullable=True),
    sa.Column('idempotency_key', sa.VARCHAR(length=255), autoincrement=False, nullable=False),
    sa.ForeignKeyConstraint(['campaign_id'], ['campaigns.id'], name='fk_inventory_reservations_campaign_id_campaigns', ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['org_id'], ['organizations.id'], name='fk_inventory_reservations_org_id_organizations', ondelete='RESTRICT'),
    sa.PrimaryKeyConstraint('id', name='pk_inventory_reservations'),
    sa.UniqueConstraint('idempotency_key', name='uq_inventory_reservations_idempotency'),
    sa.UniqueConstraint('idempotency_key', name='uq_inventory_reservations_idempotency_key')
    )
    op.create_index('ix_inventory_reservations_campaign_id', 'inventory_reservations', ['campaign_id'], unique=False)
    op.create_index('ix_inventory_reservations_expires_at', 'inventory_reservations', ['expires_at'], unique=False)
    op.create_table('inventory_slots',
    sa.Column('id', sa.UUID(), autoincrement=False, nullable=False),
    sa.Column('screen_id', sa.UUID(), autoincrement=False, nullable=False),
    sa.Column('slot_type', sa.VARCHAR(length=20), autoincrement=False, nullable=False),
    sa.Column('date', sa.DATE(), autoincrement=False, nullable=False),
    sa.Column('hour', sa.INTEGER(), autoincrement=False, nullable=False),
    sa.Column('minute', sa.INTEGER(), autoincrement=False, nullable=False),
    sa.Column('duration_seconds', sa.INTEGER(), autoincrement=False, nullable=False),
    sa.Column('status', sa.VARCHAR(length=20), autoincrement=False, nullable=False),
    sa.Column('campaign_id', sa.UUID(), autoincrement=False, nullable=True),
    sa.Column('reservation_id', sa.UUID(), autoincrement=False, nullable=True),
    sa.ForeignKeyConstraint(['campaign_id'], ['campaigns.id'], name='fk_inventory_slots_campaign_id_campaigns', ondelete='SET NULL'),
    sa.ForeignKeyConstraint(['reservation_id'], ['inventory_reservations.id'], name='fk_inventory_slots_reservation_id_inventory_reservations', ondelete='SET NULL'),
    sa.ForeignKeyConstraint(['screen_id'], ['screens.id'], name='fk_inventory_slots_screen_id_screens', ondelete='RESTRICT'),
    sa.PrimaryKeyConstraint('id', name='pk_inventory_slots'),
    sa.UniqueConstraint('screen_id', 'date', 'hour', 'minute', 'duration_seconds', name='uq_inventory_slots_screen_time')
    )
    op.create_index('ix_inventory_slots_campaign_id', 'inventory_slots', ['campaign_id'], unique=False)
    op.create_index('ix_inventory_slots_screen_id_date', 'inventory_slots', ['screen_id', 'date'], unique=False)
    op.create_index('ix_inventory_slots_status', 'inventory_slots', ['status'], unique=False)
    op.create_table('payment_orders',
    sa.Column('id', sa.UUID(), autoincrement=False, nullable=False),
    sa.Column('org_id', sa.UUID(), autoincrement=False, nullable=False),
    sa.Column('campaign_id', sa.UUID(), autoincrement=False, nullable=True),
    sa.Column('amount_cents', sa.INTEGER(), autoincrement=False, nullable=False),
    sa.Column('status', sa.VARCHAR(length=20), autoincrement=False, nullable=False),
    sa.Column('gateway_ref', sa.VARCHAR(length=255), autoincrement=False, nullable=True),
    sa.Column('gateway_data', postgresql.JSONB(astext_type=Text()), autoincrement=False, nullable=True),
    sa.Column('method', sa.VARCHAR(length=50), autoincrement=False, nullable=True),
    sa.Column('created_at', postgresql.TIMESTAMP(timezone=True), autoincrement=False, nullable=False),
    sa.Column('paid_at', postgresql.TIMESTAMP(timezone=True), autoincrement=False, nullable=True),
    sa.Column('refunded_at', postgresql.TIMESTAMP(timezone=True), autoincrement=False, nullable=True),
    sa.ForeignKeyConstraint(['campaign_id'], ['campaigns.id'], name='fk_payment_orders_campaign_id_campaigns', ondelete='RESTRICT'),
    sa.ForeignKeyConstraint(['org_id'], ['organizations.id'], name='fk_payment_orders_org_id_organizations', ondelete='RESTRICT'),
    sa.PrimaryKeyConstraint('id', name='pk_payment_orders')
    )
    op.create_index('ix_payment_orders_campaign_id', 'payment_orders', ['campaign_id'], unique=False)
    op.create_index('ix_payment_orders_gateway_ref', 'payment_orders', ['gateway_ref'], unique=False)
    op.create_index('ix_payment_orders_org_id', 'payment_orders', ['org_id'], unique=False)
    op.create_index('ix_payment_orders_status', 'payment_orders', ['status'], unique=False)
    op.create_table('webhook_events',
    sa.Column('id', sa.UUID(), autoincrement=False, nullable=False),
    sa.Column('gateway_event_id', sa.VARCHAR(length=255), autoincrement=False, nullable=False),
    sa.Column('event_type', sa.VARCHAR(length=100), autoincrement=False, nullable=False),
    sa.Column('payload', postgresql.JSONB(astext_type=Text()), autoincrement=False, nullable=True),
    sa.Column('processed_at', postgresql.TIMESTAMP(timezone=True), autoincrement=False, nullable=False),
    sa.Column('created_at', postgresql.TIMESTAMP(timezone=True), autoincrement=False, nullable=False),
    sa.PrimaryKeyConstraint('id', name='pk_webhook_events'),
    sa.UniqueConstraint('gateway_event_id', name='uq_webhook_events_gateway_event_id')
    )
    op.create_index('ix_webhook_events_event_type', 'webhook_events', ['event_type'], unique=False)
    op.create_index('ix_webhook_events_processed_at', 'webhook_events', ['processed_at'], unique=False)
    op.create_table('playback_events',
    sa.Column('id', sa.UUID(), autoincrement=False, nullable=False),
    sa.Column('player_id', sa.UUID(), autoincrement=False, nullable=False),
    sa.Column('screen_id', sa.UUID(), autoincrement=False, nullable=False),
    sa.Column('creative_id', sa.UUID(), autoincrement=False, nullable=False),
    sa.Column('campaign_id', sa.UUID(), autoincrement=False, nullable=True),
    sa.Column('manifest_version', sa.INTEGER(), autoincrement=False, nullable=False),
    sa.Column('started_at', postgresql.TIMESTAMP(timezone=True), autoincrement=False, nullable=False),
    sa.Column('finished_at', postgresql.TIMESTAMP(timezone=True), autoincrement=False, nullable=True),
    sa.Column('expected_duration_s', sa.INTEGER(), autoincrement=False, nullable=False),
    sa.Column('actual_duration_s', sa.DOUBLE_PRECISION(precision=53), autoincrement=False, nullable=True),
    sa.Column('result', sa.VARCHAR(length=20), autoincrement=False, nullable=False),
    sa.Column('sequence_number', sa.BIGINT(), autoincrement=False, nullable=False),
    sa.Column('nonce', sa.VARCHAR(length=100), autoincrement=False, nullable=False),
    sa.Column('signature', sa.VARCHAR(length=500), autoincrement=False, nullable=False),
    sa.Column('is_valid', sa.BOOLEAN(), autoincrement=False, nullable=True),
    sa.Column('sync_id', sa.UUID(), autoincrement=False, nullable=True),
    sa.ForeignKeyConstraint(['campaign_id'], ['campaigns.id'], name='fk_playback_events_campaign_id_campaigns', ondelete='RESTRICT'),
    sa.ForeignKeyConstraint(['creative_id'], ['creatives.id'], name='fk_playback_events_creative_id_creatives', ondelete='RESTRICT'),
    sa.ForeignKeyConstraint(['player_id'], ['devices.id'], name='fk_playback_events_player_id_devices', ondelete='RESTRICT'),
    sa.ForeignKeyConstraint(['screen_id'], ['screens.id'], name='fk_playback_events_screen_id_screens', ondelete='RESTRICT'),
    sa.PrimaryKeyConstraint('id', name='pk_playback_events'),
    sa.UniqueConstraint('player_id', 'sequence_number', 'nonce', name='uq_playback_events_player_seq_nonce')
    )
    op.create_index('ix_playback_events_campaign_id', 'playback_events', ['campaign_id'], unique=False)
    op.create_index('ix_playback_events_creative_id', 'playback_events', ['creative_id'], unique=False)
    op.create_index('ix_playback_events_is_valid', 'playback_events', ['is_valid'], unique=False)
    op.create_index('ix_playback_events_player_id', 'playback_events', ['player_id'], unique=False)
    op.create_index('ix_playback_events_screen_started', 'playback_events', ['screen_id', 'started_at'], unique=False)
    op.create_index('ix_playback_events_sync_id', 'playback_events', ['sync_id'], unique=False)
    op.create_table('commissions',
    sa.Column('id', sa.UUID(), autoincrement=False, nullable=False),
    sa.Column('screen_id', sa.UUID(), autoincrement=False, nullable=False),
    sa.Column('establishment_id', sa.UUID(), autoincrement=False, nullable=False),
    sa.Column('campaign_id', sa.UUID(), autoincrement=False, nullable=False),
    sa.Column('playback_event_id', sa.UUID(), autoincrement=False, nullable=True),
    sa.Column('amount_cents', sa.INTEGER(), autoincrement=False, nullable=False),
    sa.Column('weight', sa.DOUBLE_PRECISION(precision=53), autoincrement=False, nullable=True),
    sa.Column('status', sa.VARCHAR(length=20), autoincrement=False, nullable=False),
    sa.Column('period_start', sa.DATE(), autoincrement=False, nullable=True),
    sa.Column('period_end', sa.DATE(), autoincrement=False, nullable=True),
    sa.Column('paid_at', postgresql.TIMESTAMP(timezone=True), autoincrement=False, nullable=True),
    sa.Column('created_at', postgresql.TIMESTAMP(timezone=True), autoincrement=False, nullable=False),
    sa.ForeignKeyConstraint(['campaign_id'], ['campaigns.id'], name='fk_commissions_campaign_id_campaigns', ondelete='RESTRICT'),
    sa.ForeignKeyConstraint(['establishment_id'], ['establishments.id'], name='fk_commissions_establishment_id_establishments', ondelete='RESTRICT'),
    sa.ForeignKeyConstraint(['playback_event_id'], ['playback_events.id'], name='fk_commissions_playback_event_id_playback_events', ondelete='RESTRICT'),
    sa.ForeignKeyConstraint(['screen_id'], ['screens.id'], name='fk_commissions_screen_id_screens', ondelete='RESTRICT'),
    sa.PrimaryKeyConstraint('id', name='pk_commissions')
    )
    op.create_index('ix_commissions_campaign_id', 'commissions', ['campaign_id'], unique=False)
    op.create_index('ix_commissions_establishment_id', 'commissions', ['establishment_id'], unique=False)
    op.create_index('ix_commissions_period', 'commissions', ['period_start', 'period_end'], unique=False)
    op.create_index('ix_commissions_screen_id', 'commissions', ['screen_id'], unique=False)
    op.create_index('ix_commissions_status', 'commissions', ['status'], unique=False)
    op.create_unique_constraint('uq_inventory_reservations_idempotency_key', 'inventory_reservations', ['idempotency_key'])
    op.create_unique_constraint('uq_ledger_entries_idempotency_key', 'ledger_entries', ['idempotency_key'])
    op.create_foreign_key("fk_establishments_category_id_establishment_categories", "establishments", "establishment_categories", ["category_id"], ["id"], ondelete="RESTRICT")


def downgrade() -> None:
    _downgrade_guards()
    _downgrade_ddl()
    _downgrade_data_reverse()
    _downgrade_existing_tables()
    _drop_new_tables()
    _recreate_legacy_tables()
    for fn in NEW_FUNCTIONS:
        op.execute(f"DROP FUNCTION IF EXISTS {fn}")
    op.execute("DROP EXTENSION IF EXISTS btree_gist")
