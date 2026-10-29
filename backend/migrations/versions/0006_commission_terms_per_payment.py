"""commission_terms_per_payment — AÇÃO 002 / W-FIN correção (h) + (i)

Comissão por PAGAMENTO (renovação e alteração paga entram no pool de 5 %; telas incluídas por
alteração geram comissão) e peso por TELA (peso efetivo = estabelecimento × tela / 1000).

Schema:
- ``commission_campaigns`` passa a ser o TERMO por cobrança: nova PK ``id``; ``campaign_id`` deixa
  de ser PK; UNIQUE (campaign_id, payment_order_id); colunas ``kind`` (INITIAL|RENEWAL|CHANGE),
  ``start_date``/``end_date`` (período do termo), ``daily_plan`` (exibições contratadas por dia,
  snapshot) e ``expected_total``; índice por campanha; CHECKs ``kind_valid``, ``period_valid``,
  ``expected_nonneg``.
- ``commission_campaign_screens``: PK (terms_id, screen_id) → FK ``commission_campaigns.id``;
  colunas ``establishment_weight_milli``, ``screen_weight_milli``, ``created_at``; removidas
  ``expected_total``, ``share_ucents``, ``plan`` (o plano passa a ser diário).
- NOVA ``commission_day_plans`` (terms_id, local_day) PK — plano do dia por tela, APPEND-ONLY
  (trigger ``rd_forbid_mutation``; runtime ``rd_app`` sem UPDATE/DELETE/TRUNCATE).
- ``commission_accruals``: ``terms_id`` (FK, NOT NULL); UNIQUE ``uq_commission_accruals_key`` passa
  a (terms_id, screen_id, local_day, competence); FK de ``campaign_id`` removida (coluna mantida
  para relatórios) + índice ``ix_commission_accruals_campaign``.
- ``commission_pool_releases``: PK (terms_id, competence) com FK; ``campaign_id`` removida.

Dados (linhas existentes, auditados ``migration.0006.*``): cada linha antiga vira o termo INITIAL
da sua cobrança (período = cronograma de receita da cobrança × campanha); telas e acúmulos são
ligados ao termo; o plano congelado antigo (``plan`` por tela) vira ``daily_plan`` do termo e um
``commission_day_plans`` por dia já apropriado (mesmos números ⇒ mesmos valores apropriados).

Downgrade: só se houver NO MÁXIMO um termo por campanha (senão recusa — renovações/alterações
pagas não têm representação no formato antigo). Recria as colunas antigas a partir do plano
diário (``share_ucents`` recalculado pelo algoritmo antigo NÃO é restaurado: fica 0 e a
reapropriação usa o novo cálculo após novo upgrade) e remove ``commission_day_plans``.

Revision ID: 0006
Revises: 0005
Create Date: 2026-10-08
"""
import json
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0006"
down_revision: Union[str, None] = "0005"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

RUNTIME_ROLE = "rd_app"


def _exec(sql: str, **params) -> None:
    op.get_bind().execute(sa.text(sql), params)


def _scalar(sql: str, **params):
    return op.get_bind().execute(sa.text(sql), params).scalar()


def _role_exists(role: str) -> bool:
    return bool(_scalar("SELECT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = :r)", r=role))


def _audit(action: str, after: dict) -> None:
    _exec("INSERT INTO audit_logs (id, action, entity_type, entity_id, after_data, origin, created_at) "
          "VALUES (gen_random_uuid(), :a, 'commission_terms', NULL, CAST(:af AS jsonb), 'system', now())",
          a=action, af=json.dumps(after))


def upgrade() -> None:
    counts = {t: _scalar(f"SELECT count(*) FROM {t}") for t in (
        "commission_campaigns", "commission_campaign_screens", "commission_accruals", "commission_pool_releases")}

    # ── commission_campaigns → termo por cobrança ────────────────────────────
    op.add_column("commission_campaigns", sa.Column("id", postgresql.UUID(as_uuid=True), nullable=True))
    op.add_column("commission_campaigns", sa.Column("kind", sa.String(length=10), server_default=sa.text("'INITIAL'"),
                                                    nullable=False))
    op.add_column("commission_campaigns", sa.Column("start_date", sa.Date(), nullable=True))
    op.add_column("commission_campaigns", sa.Column("end_date", sa.Date(), nullable=True))
    op.add_column("commission_campaigns", sa.Column("daily_plan", postgresql.JSONB(astext_type=sa.Text()), nullable=True))
    op.add_column("commission_campaigns", sa.Column("expected_total", sa.BigInteger(), nullable=True))
    _exec("UPDATE commission_campaigns SET id = gen_random_uuid()")
    _exec("UPDATE commission_campaigns cc SET start_date = rs.start_date, end_date = rs.end_date "
          "FROM revenue_schedules rs WHERE rs.payment_order_id = cc.payment_order_id AND rs.campaign_id = cc.campaign_id")
    _exec("""
        UPDATE commission_campaigns cc SET start_date = p.d0, end_date = p.d1
        FROM (SELECT s.campaign_id, min(e.key::date) AS d0, max(e.key::date) AS d1
              FROM commission_campaign_screens s, jsonb_each(s.plan) e GROUP BY s.campaign_id) p
        WHERE p.campaign_id = cc.campaign_id AND cc.start_date IS NULL
    """)
    _exec("UPDATE commission_campaigns SET start_date = (registered_at AT TIME ZONE 'America/Sao_Paulo')::date, "
          "end_date = (registered_at AT TIME ZONE 'America/Sao_Paulo')::date WHERE start_date IS NULL")
    # plano diário (snapshot) a partir do plano congelado antigo por tela
    _exec("""
        UPDATE commission_campaigns cc SET daily_plan = d.plan, expected_total = d.total
        FROM (
            SELECT x.campaign_id, jsonb_object_agg(x.day, x.n) AS plan, sum(x.n)::bigint AS total
            FROM (SELECT s.campaign_id, e.key AS day, sum((e.value)::text::bigint) AS n
                  FROM commission_campaign_screens s, jsonb_each(s.plan) e
                  GROUP BY s.campaign_id, e.key) x
            GROUP BY x.campaign_id
        ) d WHERE d.campaign_id = cc.campaign_id
    """)
    op.alter_column("commission_campaigns", "id", nullable=False)
    op.alter_column("commission_campaigns", "start_date", nullable=False)
    op.alter_column("commission_campaigns", "end_date", nullable=False)

    # ── tabelas dependentes ganham terms_id antes da troca de PK ─────────────
    op.add_column("commission_campaign_screens", sa.Column("terms_id", postgresql.UUID(as_uuid=True), nullable=True))
    op.add_column("commission_accruals", sa.Column("terms_id", postgresql.UUID(as_uuid=True), nullable=True))
    op.add_column("commission_pool_releases", sa.Column("terms_id", postgresql.UUID(as_uuid=True), nullable=True))
    for t in ("commission_campaign_screens", "commission_accruals", "commission_pool_releases"):
        _exec(f"UPDATE {t} x SET terms_id = cc.id FROM commission_campaigns cc WHERE cc.campaign_id = x.campaign_id")

    # plano do dia para cada dia já apropriado (mesmos números do plano congelado antigo)
    op.create_table(
        "commission_day_plans",
        sa.Column("terms_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("local_day", sa.Date(), nullable=False),
        sa.Column("plan", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("e_day", sa.BigInteger(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.CheckConstraint("e_day >= 0", name=op.f("ck_commission_day_plans_e_day_nonneg")),
        # FK para commission_campaigns.id só DEPOIS da troca de PK (abaixo)
        sa.PrimaryKeyConstraint("terms_id", "local_day", name=op.f("pk_commission_day_plans")),
    )

    _exec("""
        INSERT INTO commission_day_plans (terms_id, local_day, plan, e_day)
        SELECT s.terms_id, d.day::date,
               jsonb_object_agg(s.screen_id::text, (s.plan ->> d.day)::bigint),
               sum((s.plan ->> d.day)::bigint)
        FROM (SELECT DISTINCT terms_id, local_day::text AS day FROM commission_accruals) d
        JOIN commission_campaign_screens s ON s.terms_id = d.terms_id
        WHERE COALESCE((s.plan ->> d.day)::bigint, 0) > 0
        GROUP BY s.terms_id, d.day
    """)

    # ── troca de chaves ──────────────────────────────────────────────────────
    op.drop_constraint(op.f("fk_commission_campaign_screens_campaign_id_commission_campaigns"), "commission_campaign_screens",
                       type_="foreignkey")
    op.drop_constraint(op.f("fk_commission_accruals_campaign_id_commission_campaigns"), "commission_accruals",
                       type_="foreignkey")
    op.drop_constraint(op.f("fk_commission_pool_releases_campaign_id_commission_campaigns"), "commission_pool_releases",
                       type_="foreignkey")
    op.drop_constraint(op.f("pk_commission_campaigns"), "commission_campaigns", type_="primary")
    op.create_primary_key(op.f("pk_commission_campaigns"), "commission_campaigns", ["id"])
    op.create_unique_constraint(op.f("uq_commission_campaigns_campaign_payment"), "commission_campaigns",
                                ["campaign_id", "payment_order_id"])
    op.create_index(op.f("ix_commission_campaigns_campaign"), "commission_campaigns", ["campaign_id"], unique=False)
    op.create_check_constraint(op.f("ck_commission_campaigns_kind_valid"), "commission_campaigns",
                               "kind IN ('INITIAL','RENEWAL','CHANGE')")
    op.create_check_constraint(op.f("ck_commission_campaigns_period_valid"), "commission_campaigns", "end_date >= start_date")
    op.create_check_constraint(op.f("ck_commission_campaigns_expected_nonneg"), "commission_campaigns",
                               "expected_total IS NULL OR expected_total >= 0")
    # FK do plano diário só agora (commission_campaigns.id acabou de virar PK)
    op.create_foreign_key(op.f("fk_commission_day_plans_terms_id_commission_campaigns"), "commission_day_plans",
                          "commission_campaigns", ["terms_id"], ["id"], ondelete="RESTRICT")

    # telas
    op.alter_column("commission_campaign_screens", "terms_id", nullable=False)
    op.drop_constraint(op.f("pk_commission_campaign_screens"), "commission_campaign_screens", type_="primary")
    op.drop_constraint(op.f("ck_commission_campaign_screens_nonneg"), "commission_campaign_screens", type_="check")
    op.drop_constraint(op.f("ck_commission_campaign_screens_weight_nonneg"), "commission_campaign_screens", type_="check")
    op.drop_column("commission_campaign_screens", "expected_total")
    op.drop_column("commission_campaign_screens", "share_ucents")
    op.drop_column("commission_campaign_screens", "plan")
    op.add_column("commission_campaign_screens", sa.Column("establishment_weight_milli", sa.Integer(),
                                                           server_default=sa.text("1000"), nullable=False))
    op.add_column("commission_campaign_screens", sa.Column("screen_weight_milli", sa.Integer(),
                                                           server_default=sa.text("1000"), nullable=False))
    op.add_column("commission_campaign_screens", sa.Column("created_at", sa.DateTime(timezone=True),
                                                           server_default=sa.text("now()"), nullable=False))
    _exec("UPDATE commission_campaign_screens SET establishment_weight_milli = weight_milli")
    op.create_primary_key(op.f("pk_commission_campaign_screens"), "commission_campaign_screens", ["terms_id", "screen_id"])
    op.create_foreign_key(op.f("fk_commission_campaign_screens_terms_id_commission_campaigns"), "commission_campaign_screens",
                          "commission_campaigns", ["terms_id"], ["id"], ondelete="RESTRICT")
    op.create_check_constraint(op.f("ck_commission_campaign_screens_weight_nonneg"), "commission_campaign_screens",
                               "weight_milli >= 0 AND establishment_weight_milli >= 0 AND screen_weight_milli >= 0")
    op.create_check_constraint(op.f("ck_commission_campaign_screens_attributed_nonneg"), "commission_campaign_screens",
                               "attributed_revenue_cents >= 0")

    # acúmulos
    op.alter_column("commission_accruals", "terms_id", nullable=False)
    op.drop_constraint(op.f("uq_commission_accruals_key"), "commission_accruals", type_="unique")
    op.create_unique_constraint(op.f("uq_commission_accruals_key"), "commission_accruals",
                                ["terms_id", "screen_id", "local_day", "competence"])
    op.create_foreign_key(op.f("fk_commission_accruals_terms_id_commission_campaigns"), "commission_accruals",
                          "commission_campaigns", ["terms_id"], ["id"], ondelete="RESTRICT")
    op.create_index(op.f("ix_commission_accruals_campaign"), "commission_accruals", ["campaign_id"], unique=False)

    # liberações de pool
    op.alter_column("commission_pool_releases", "terms_id", nullable=False)
    op.drop_constraint(op.f("pk_commission_pool_releases"), "commission_pool_releases", type_="primary")
    op.drop_column("commission_pool_releases", "campaign_id")
    op.create_primary_key(op.f("pk_commission_pool_releases"), "commission_pool_releases", ["terms_id", "competence"])
    op.create_foreign_key(op.f("fk_commission_pool_releases_terms_id_commission_campaigns"), "commission_pool_releases",
                          "commission_campaigns", ["terms_id"], ["id"], ondelete="RESTRICT")

    # ── DDL append-only + privilégios do runtime ─────────────────────────────
    from app.db import ddl

    for stmt in ddl.append_only_trigger("commission_day_plans"):
        op.execute(stmt)
    if _role_exists(RUNTIME_ROLE):
        op.execute(f"GRANT SELECT, INSERT ON commission_day_plans TO {RUNTIME_ROLE}")
        op.execute(f"REVOKE UPDATE, DELETE, TRUNCATE ON commission_day_plans FROM {RUNTIME_ROLE}")

    day_plans = _scalar("SELECT count(*) FROM commission_day_plans")
    _audit("migration.0006.commission_terms_per_payment", {**counts, "commission_day_plans": day_plans})


def downgrade() -> None:
    multi = _scalar("SELECT count(*) FROM (SELECT campaign_id FROM commission_campaigns GROUP BY campaign_id "
                    "HAVING count(*) > 1) x")
    if multi:
        raise RuntimeError(f"downgrade 0006 recusado: {multi} campanha(s) com mais de um termo de comissão "
                           "(renovação/alteração paga) — o formato 0005 não representa esses pagamentos")
    from app.db import ddl

    for stmt in ddl.drop_append_only_trigger("commission_day_plans"):
        op.execute(stmt)

    # liberações de pool
    op.add_column("commission_pool_releases", sa.Column("campaign_id", postgresql.UUID(as_uuid=True), nullable=True))
    _exec("UPDATE commission_pool_releases x SET campaign_id = cc.campaign_id FROM commission_campaigns cc "
          "WHERE cc.id = x.terms_id")
    op.drop_constraint(op.f("fk_commission_pool_releases_terms_id_commission_campaigns"), "commission_pool_releases",
                       type_="foreignkey")
    op.drop_constraint(op.f("pk_commission_pool_releases"), "commission_pool_releases", type_="primary")
    op.alter_column("commission_pool_releases", "campaign_id", nullable=False)
    op.drop_column("commission_pool_releases", "terms_id")
    op.create_primary_key(op.f("pk_commission_pool_releases"), "commission_pool_releases", ["campaign_id", "competence"])

    # acúmulos
    op.drop_index(op.f("ix_commission_accruals_campaign"), table_name="commission_accruals")
    op.drop_constraint(op.f("fk_commission_accruals_terms_id_commission_campaigns"), "commission_accruals", type_="foreignkey")
    op.drop_constraint(op.f("uq_commission_accruals_key"), "commission_accruals", type_="unique")
    op.create_unique_constraint(op.f("uq_commission_accruals_key"), "commission_accruals",
                                ["campaign_id", "screen_id", "local_day", "competence"])
    op.drop_column("commission_accruals", "terms_id")

    # telas (plano por tela reconstruído dos planos diários existentes; share_ucents = 0)
    op.drop_constraint(op.f("ck_commission_campaign_screens_attributed_nonneg"), "commission_campaign_screens", type_="check")
    op.drop_constraint(op.f("ck_commission_campaign_screens_weight_nonneg"), "commission_campaign_screens", type_="check")
    op.drop_constraint(op.f("fk_commission_campaign_screens_terms_id_commission_campaigns"), "commission_campaign_screens",
                       type_="foreignkey")
    op.drop_constraint(op.f("pk_commission_campaign_screens"), "commission_campaign_screens", type_="primary")
    op.add_column("commission_campaign_screens", sa.Column("expected_total", sa.BigInteger(),
                                                           server_default=sa.text("0"), nullable=False))
    op.add_column("commission_campaign_screens", sa.Column("share_ucents", sa.BigInteger(),
                                                           server_default=sa.text("0"), nullable=False))
    op.add_column("commission_campaign_screens", sa.Column("plan", postgresql.JSONB(astext_type=sa.Text()),
                                                           server_default=sa.text("'{}'::jsonb"), nullable=False))
    _exec("""
        UPDATE commission_campaign_screens s SET plan = p.plan, expected_total = p.total
        FROM (SELECT dp.terms_id, k.key AS screen_id, jsonb_object_agg(dp.local_day::text, (k.value)::text::bigint) AS plan,
                     sum((k.value)::text::bigint) AS total
              FROM commission_day_plans dp, jsonb_each(dp.plan) k GROUP BY dp.terms_id, k.key) p
        WHERE p.terms_id = s.terms_id AND p.screen_id = s.screen_id::text
    """)
    for c in ("expected_total", "share_ucents", "plan"):
        op.alter_column("commission_campaign_screens", c, server_default=None)
    op.drop_column("commission_campaign_screens", "created_at")
    op.drop_column("commission_campaign_screens", "screen_weight_milli")
    op.drop_column("commission_campaign_screens", "establishment_weight_milli")
    op.drop_column("commission_campaign_screens", "terms_id")
    op.create_primary_key(op.f("pk_commission_campaign_screens"), "commission_campaign_screens", ["campaign_id", "screen_id"])
    op.create_check_constraint(op.f("ck_commission_campaign_screens_weight_nonneg"), "commission_campaign_screens",
                               "weight_milli >= 0")
    op.create_check_constraint(op.f("ck_commission_campaign_screens_nonneg"), "commission_campaign_screens",
                               "expected_total >= 0 AND share_ucents >= 0")

    op.drop_table("commission_day_plans")

    # termos → campanha
    op.drop_constraint(op.f("ck_commission_campaigns_expected_nonneg"), "commission_campaigns", type_="check")
    op.drop_constraint(op.f("ck_commission_campaigns_period_valid"), "commission_campaigns", type_="check")
    op.drop_constraint(op.f("ck_commission_campaigns_kind_valid"), "commission_campaigns", type_="check")
    op.drop_index(op.f("ix_commission_campaigns_campaign"), table_name="commission_campaigns")
    op.drop_constraint(op.f("uq_commission_campaigns_campaign_payment"), "commission_campaigns", type_="unique")
    op.drop_constraint(op.f("pk_commission_campaigns"), "commission_campaigns", type_="primary")
    op.create_primary_key(op.f("pk_commission_campaigns"), "commission_campaigns", ["campaign_id"])
    for c in ("expected_total", "daily_plan", "end_date", "start_date", "kind", "id"):
        op.drop_column("commission_campaigns", c)
    op.create_foreign_key(op.f("fk_commission_campaign_screens_campaign_id_commission_campaigns"),
                          "commission_campaign_screens", "commission_campaigns", ["campaign_id"], ["campaign_id"],
                          ondelete="RESTRICT")
    op.create_foreign_key(op.f("fk_commission_accruals_campaign_id_commission_campaigns"), "commission_accruals",
                          "commission_campaigns", ["campaign_id"], ["campaign_id"], ondelete="RESTRICT")
    op.create_foreign_key(op.f("fk_commission_pool_releases_campaign_id_commission_campaigns"), "commission_pool_releases",
                          "commission_campaigns", ["campaign_id"], ["campaign_id"], ondelete="RESTRICT")
    _audit("migration.0006.downgrade", {"terms": _scalar("SELECT count(*) FROM commission_campaigns")})
