"""
RD Mídia Indoor — Modelos financeiros (AÇÃO 002 / W-FIN; ADR_009; A5 D0–D9).

Regras invioláveis:
- Dinheiro SEMPRE ``BIGINT`` em centavos (``*_cents``); comissão acumulada em
  micro-centavos (``*_ucents`` = centavo × 10⁶). Percentuais em basis points
  (``*_bps``), pesos em milésimos (``*_milli``). Nenhum ``Float``/``Numeric``.
- Ledger em PARTIDAS DOBRADAS: ``ledger_transactions`` + ``ledger_postings``
  (sinal: + débito, − crédito). Σ por transação = 0 garantido por constraint trigger
  DEFERRABLE INITIALLY DEFERRED (``app.modules.financial.ddl``). Ledger é
  append-only (trigger anti UPDATE/DELETE/TRUNCATE). Correção = transação de
  reversão ligada (``reverses_transaction_id`` UNIQUE). Saldo = Σ postings — não
  existe coluna de saldo.
- Período fechado (``financial_period_closings``) rejeita lançamentos (trigger).
- Toda saída de dinheiro da RD passa por ``financial_approval_requests``. AÇÃO 003
  (ADR_011): o pedido nasce com o modo da instância (``approval_mode`` SINGLE|DUAL,
  snapshot imutável). DUAL = dupla aprovação dos dois Admin Master (CHECK aprovador ≠
  solicitante); SINGLE = autorização única do próprio solicitante (``self_authorized_by``,
  ``approved_by`` sempre NULL). CHECK ``mode_consistent`` impede misturar os fluxos.
"""
from __future__ import annotations

import enum
import uuid
from datetime import date, datetime
from typing import Optional

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    Date,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    SmallInteger,
    String,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base


def _uuid_pk() -> Mapped[uuid.UUID]:
    return mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)


def _created() -> Mapped[datetime]:
    return mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())


def _in(values) -> str:
    return "(" + ",".join(f"'{v}'" for v in values) + ")"


# ═══════════════════════════════════════════════════════════════════════════
# LEDGER (partidas dobradas)
# ═══════════════════════════════════════════════════════════════════════════

class AccountKind(str, enum.Enum):
    ASSET = "ASSET"
    LIABILITY = "LIABILITY"
    EQUITY = "EQUITY"
    INCOME = "INCOME"
    EXPENSE = "EXPENSE"


class OwnerType(str, enum.Enum):
    RD = "RD"
    ESTABLISHMENT = "ESTABLISHMENT"
    ADVERTISER = "ADVERTISER"
    SELLER = "SELLER"


class LedgerAccount(Base):
    """Conta contábil. ``account_key`` = code|owner_type|owner_id (único)."""

    __tablename__ = "ledger_accounts"
    __table_args__ = (
        UniqueConstraint("account_key", name="uq_ledger_accounts_account_key"),
        CheckConstraint(f"kind IN {_in(k.value for k in AccountKind)}", name="kind_valid"),
        CheckConstraint(f"owner_type IN {_in(o.value for o in OwnerType)}", name="owner_type_valid"),
        CheckConstraint("(owner_type = 'RD') = (owner_id IS NULL)", name="owner_id_matches_type"),
        Index("ix_ledger_accounts_owner", "owner_type", "owner_id"),
        Index("ix_ledger_accounts_code", "code"),
    )

    id: Mapped[uuid.UUID] = _uuid_pk()
    account_key: Mapped[str] = mapped_column(String(200), nullable=False)
    code: Mapped[str] = mapped_column(String(120), nullable=False)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    kind: Mapped[str] = mapped_column(String(12), nullable=False)
    owner_type: Mapped[str] = mapped_column(String(20), nullable=False)
    owner_id: Mapped[Optional[uuid.UUID]] = mapped_column(UUID(as_uuid=True), nullable=True)
    # organização dona (para extratos com escopo): org do anunciante ou do estabelecimento
    org_id: Mapped[Optional[uuid.UUID]] = mapped_column(UUID(as_uuid=True), nullable=True, index=True)
    is_cash: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, server_default=text("false"))
    dre_group: Mapped[Optional[str]] = mapped_column(String(40), nullable=True)
    created_at: Mapped[datetime] = _created()


class LedgerTransaction(Base):
    """Cabeçalho de lançamento (append-only). Σ postings = 0 (constraint trigger diferida)."""

    __tablename__ = "ledger_transactions"
    __table_args__ = (
        UniqueConstraint("idempotency_key", name="uq_ledger_transactions_idempotency_key"),
        UniqueConstraint("reverses_transaction_id", name="uq_ledger_transactions_reverses"),
        Index("ix_ledger_transactions_competence", "competence_date"),
        Index("ix_ledger_transactions_kind", "kind"),
        Index("ix_ledger_transactions_ref", "ref_type", "ref_id"),
        Index("ix_ledger_transactions_org", "org_id"),
        Index("ix_ledger_transactions_external", "external_provider", "external_ref"),
    )

    id: Mapped[uuid.UUID] = _uuid_pk()
    kind: Mapped[str] = mapped_column(String(40), nullable=False)
    idempotency_key: Mapped[str] = mapped_column(String(200), nullable=False)
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())
    competence_date: Mapped[date] = mapped_column(Date, nullable=False)
    description: Mapped[str] = mapped_column(String(500), nullable=False)
    created_by: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="RESTRICT"), nullable=True
    )
    approval_request_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True), ForeignKey("financial_approval_requests.id", ondelete="RESTRICT"), nullable=True
    )
    reverses_transaction_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True), ForeignKey("ledger_transactions.id", ondelete="RESTRICT"), nullable=True
    )
    external_provider: Mapped[Optional[str]] = mapped_column(String(30), nullable=True)
    external_ref: Mapped[Optional[str]] = mapped_column(String(120), nullable=True)
    ref_type: Mapped[Optional[str]] = mapped_column(String(40), nullable=True)
    ref_id: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    org_id: Mapped[Optional[uuid.UUID]] = mapped_column(UUID(as_uuid=True), nullable=True)
    meta: Mapped[Optional[dict]] = mapped_column("metadata", JSONB, nullable=True)
    created_at: Mapped[datetime] = _created()


class LedgerPosting(Base):
    """Partida (append-only). ``amount_cents`` ≠ 0: + débito / − crédito."""

    __tablename__ = "ledger_postings"
    __table_args__ = (
        CheckConstraint("amount_cents <> 0", name="amount_nonzero"),
        Index("ix_ledger_postings_transaction_id", "transaction_id"),
        Index("ix_ledger_postings_account_competence", "account_id", "competence_date"),
    )

    id: Mapped[uuid.UUID] = _uuid_pk()
    transaction_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("ledger_transactions.id", ondelete="RESTRICT"), nullable=False
    )
    account_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("ledger_accounts.id", ondelete="RESTRICT"), nullable=False
    )
    amount_cents: Mapped[int] = mapped_column(BigInteger, nullable=False)
    competence_date: Mapped[date] = mapped_column(Date, nullable=False)
    memo: Mapped[Optional[str]] = mapped_column(String(300), nullable=True)
    created_at: Mapped[datetime] = _created()


class FinancialPeriodClosing(Base):
    """Fechamento mensal (append-only): período fechado é imutável (trigger no ledger)."""

    __tablename__ = "financial_period_closings"
    __table_args__ = (
        UniqueConstraint("period_start", name="uq_financial_period_closings_period"),
        CheckConstraint("period_end >= period_start", name="period_valid"),
        CheckConstraint("extract(day FROM period_start) = 1", name="period_starts_day_1"),
    )

    id: Mapped[uuid.UUID] = _uuid_pk()
    period_start: Mapped[date] = mapped_column(Date, nullable=False)
    period_end: Mapped[date] = mapped_column(Date, nullable=False)
    closed_by: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="RESTRICT"), nullable=True
    )
    snapshot: Mapped[dict] = mapped_column(JSONB, nullable=False)
    snapshot_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    transaction_count: Mapped[int] = mapped_column(BigInteger, nullable=False)
    closed_at: Mapped[datetime] = _created()


# ═══════════════════════════════════════════════════════════════════════════
# COBRANÇAS / PAGAMENTOS / WEBHOOKS
# ═══════════════════════════════════════════════════════════════════════════

class PaymentOrderStatus(str, enum.Enum):
    CREATED = "CREATED"  # gravado; chamada ao provedor ainda não concluída
    PENDING = "PENDING"  # cobrança criada; aguardando pagamento
    CREATION_FAILED = "CREATION_FAILED"  # provedor recusou (4xx) — nenhuma cobrança existe
    CREATION_UNKNOWN = "CREATION_UNKNOWN"  # timeout/5xx — cobrança pode existir (conciliação)
    PAID = "PAID"
    PARTIALLY_REFUNDED = "PARTIALLY_REFUNDED"
    REFUNDED = "REFUNDED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"
    EXPIRED = "EXPIRED"


class PaymentOrder(Base):
    """Cobrança CloudPayPag de um pedido (o pedido comercial é do W-CAMP: ``orders``).

    ``items``/``amount_cents`` são o snapshot de preço calculado no servidor (nunca do
    cliente). ``id`` é enviado como ``order_id`` à CloudPayPag (conciliação).
    """

    __tablename__ = "payment_orders"
    __table_args__ = (
        UniqueConstraint("provider_external_ref", name="uq_payment_orders_provider_external_ref"),
        UniqueConstraint("idempotency_key", name="uq_payment_orders_idempotency_key"),
        CheckConstraint("amount_cents > 0", name="amount_positive"),
        CheckConstraint("eligible_revenue_cents >= 0 AND services_cents >= 0", name="parts_nonneg"),
        CheckConstraint("eligible_revenue_cents + services_cents = amount_cents", name="parts_sum"),
        CheckConstraint("refunded_cents >= 0 AND refunded_cents <= amount_cents", name="refunded_range"),
        CheckConstraint(f"status IN {_in(s.value for s in PaymentOrderStatus)}", name="status_valid"),
        CheckConstraint("method IN ('pix','credit_card')", name="method_valid"),
        CheckConstraint("installments BETWEEN 1 AND 12", name="installments_range"),
        Index("ix_payment_orders_org_id", "org_id"),
        Index("ix_payment_orders_status", "status"),
        Index("ix_payment_orders_reference", "reference_type", "reference_id"),
    )

    id: Mapped[uuid.UUID] = _uuid_pk()
    org_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("organizations.id", ondelete="RESTRICT"), nullable=False
    )
    reference_type: Mapped[str] = mapped_column(String(40), nullable=False)
    reference_id: Mapped[str] = mapped_column(String(64), nullable=False)
    reference: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    items: Mapped[list] = mapped_column(JSONB, nullable=False)
    amount_cents: Mapped[int] = mapped_column(BigInteger, nullable=False)
    eligible_revenue_cents: Mapped[int] = mapped_column(BigInteger, nullable=False)
    services_cents: Mapped[int] = mapped_column(BigInteger, nullable=False)
    currency: Mapped[str] = mapped_column(String(3), nullable=False, default="BRL", server_default=text("'BRL'"))
    method: Mapped[str] = mapped_column(String(20), nullable=False, default="pix")
    installments: Mapped[int] = mapped_column(SmallInteger, nullable=False, default=1, server_default=text("1"))
    status: Mapped[str] = mapped_column(String(24), nullable=False, default=PaymentOrderStatus.CREATED.value)
    dispute_status: Mapped[Optional[str]] = mapped_column(String(20), nullable=True)
    provider: Mapped[str] = mapped_column(String(30), nullable=False, default="cloudpaypag")
    instance_slug: Mapped[str] = mapped_column(String(64), nullable=False)
    provider_transaction_id: Mapped[Optional[int]] = mapped_column(BigInteger, nullable=True)
    provider_external_ref: Mapped[Optional[str]] = mapped_column(String(80), nullable=True)
    provider_processor_ref: Mapped[Optional[str]] = mapped_column(String(120), nullable=True)
    provider_status: Mapped[Optional[str]] = mapped_column(String(30), nullable=True)
    provider_error: Mapped[Optional[str]] = mapped_column(String(300), nullable=True)
    pix_qr_code: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    pix_end_to_end_id: Mapped[Optional[str]] = mapped_column(String(80), nullable=True)
    paid_amount_cents: Mapped[Optional[int]] = mapped_column(BigInteger, nullable=True)
    fee_cents: Mapped[Optional[int]] = mapped_column(BigInteger, nullable=True)
    net_cents: Mapped[Optional[int]] = mapped_column(BigInteger, nullable=True)
    refunded_cents: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0, server_default=text("0"))
    seller_user_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="RESTRICT"), nullable=True
    )
    idempotency_key: Mapped[Optional[str]] = mapped_column(String(120), nullable=True)
    expires_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    paid_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    created_by: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="RESTRICT"), nullable=True
    )
    created_at: Mapped[datetime] = _created()
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )


class PaymentOrderCallback(Base):
    """Execução de callbacks de negócio após ``paid`` (ex.: W-CAMP ``on_order_paid``).

    FAILED = erro transitório (reprocessado pelo sweeper); REJECTED = recusa de negócio
    determinística (ex.: 409 ``ORDER_MISMATCH`` de pedido superado) — nunca reprocessada:
    vira item de conciliação ``PAID_WITHOUT_SERVICE`` (estorno/análise).
    """

    __tablename__ = "payment_order_callbacks"
    __table_args__ = (
        UniqueConstraint("payment_order_id", "name", name="uq_payment_order_callbacks_order_name"),
        CheckConstraint("status IN ('PENDING','DONE','FAILED','REJECTED')", name="status_valid"),
    )

    id: Mapped[uuid.UUID] = _uuid_pk()
    payment_order_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("payment_orders.id", ondelete="RESTRICT"), nullable=False
    )
    name: Mapped[str] = mapped_column(String(80), nullable=False)
    status: Mapped[str] = mapped_column(String(10), nullable=False, default="PENDING")
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default=text("0"))
    last_error: Mapped[Optional[str]] = mapped_column(String(500), nullable=True)
    done_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = _created()
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )


class WebhookEventStatus(str, enum.Enum):
    RECEIVED = "RECEIVED"
    PROCESSING = "PROCESSING"
    PROCESSED = "PROCESSED"
    IGNORED = "IGNORED"  # fora de ordem / sem efeito
    DIVERGENT = "DIVERGENT"  # valor/moeda divergente → conciliação
    REJECTED = "REJECTED"  # subconta divergente
    FAILED = "FAILED"  # erro de processamento (reprocessável)


class WebhookEvent(Base):
    """Webhook recebido. Corpo bruto e headers são imutáveis (trigger); só o estado de
    processamento muda. Idempotência: UNIQUE (provider, instance_slug, webhook_event_id)."""

    __tablename__ = "webhook_events"
    __table_args__ = (
        UniqueConstraint("provider", "instance_slug", "webhook_event_id", name="uq_webhook_events_provider_event"),
        CheckConstraint(f"status IN {_in(s.value for s in WebhookEventStatus)}", name="status_valid"),
        Index("ix_webhook_events_status", "status"),
        Index("ix_webhook_events_external_ref", "external_ref"),
    )

    id: Mapped[uuid.UUID] = _uuid_pk()
    provider: Mapped[str] = mapped_column(String(30), nullable=False)
    instance_slug: Mapped[str] = mapped_column(String(64), nullable=False)
    webhook_event_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    raw_body: Mapped[str] = mapped_column(Text, nullable=False)
    headers: Mapped[dict] = mapped_column(JSONB, nullable=False)
    signature_valid: Mapped[bool] = mapped_column(Boolean, nullable=False)
    external_ref: Mapped[Optional[str]] = mapped_column(String(80), nullable=True)
    order_ref: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    provider_status: Mapped[Optional[str]] = mapped_column(String(30), nullable=True)
    subconta_id: Mapped[Optional[int]] = mapped_column(BigInteger, nullable=True)
    received_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())
    status: Mapped[str] = mapped_column(String(12), nullable=False, default=WebhookEventStatus.RECEIVED.value)
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default=text("0"))
    last_error: Mapped[Optional[str]] = mapped_column(String(500), nullable=True)
    payment_order_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True), ForeignKey("payment_orders.id", ondelete="RESTRICT"), nullable=True
    )
    processed_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)


class RevenueSchedule(Base):
    """Receita diferida de mídia por (cobrança × campanha), reconhecida diariamente.

    O valor reconhecido é DERIVADO do ledger (Σ dos lançamentos REVENUE_RECOGNITION
    com ``ref_id`` = id deste cronograma). Último dia fecha o restante.
    """

    __tablename__ = "revenue_schedules"
    __table_args__ = (
        UniqueConstraint("payment_order_id", "campaign_id", name="uq_revenue_schedules_order_campaign"),
        CheckConstraint("eligible_cents >= 0", name="eligible_nonneg"),
        CheckConstraint("refunded_cents >= 0 AND refunded_cents <= eligible_cents", name="refunded_range"),
        CheckConstraint("end_date >= start_date", name="dates_valid"),
        CheckConstraint("status IN ('ACTIVE','CANCELLED','COMPLETED')", name="status_valid"),
        Index("ix_revenue_schedules_status", "status"),
        Index("ix_revenue_schedules_campaign", "campaign_id"),
    )

    id: Mapped[uuid.UUID] = _uuid_pk()
    payment_order_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("payment_orders.id", ondelete="RESTRICT"), nullable=False
    )
    campaign_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    org_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    eligible_cents: Mapped[int] = mapped_column(BigInteger, nullable=False)
    refunded_cents: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0, server_default=text("0"))
    start_date: Mapped[date] = mapped_column(Date, nullable=False)
    end_date: Mapped[date] = mapped_column(Date, nullable=False)
    status: Mapped[str] = mapped_column(String(12), nullable=False, default="ACTIVE")
    cancelled_on: Mapped[Optional[date]] = mapped_column(Date, nullable=True)
    created_at: Mapped[datetime] = _created()
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )


# ═══════════════════════════════════════════════════════════════════════════
# COMISSÃO (ADR_009 item 3; A5 D2)
# ═══════════════════════════════════════════════════════════════════════════

class EstablishmentCommissionSetting(Base):
    """Exceções por estabelecimento (com vigência). Alteração só por dupla aprovação.

    ``None`` = herda do cadastro do estabelecimento (W-ORG: ``establishments.receives_commission``
    e ``establishments.weight_milli``) / do pool da instância (``commission.pool_bps``).
    """

    __tablename__ = "establishment_commission_settings"
    __table_args__ = (
        CheckConstraint("rate_bps IS NULL OR rate_bps BETWEEN 0 AND 10000", name="rate_range"),
        CheckConstraint("weight_milli IS NULL OR weight_milli BETWEEN 0 AND 100000", name="weight_range"),
        CheckConstraint("valid_to IS NULL OR valid_to > valid_from", name="validity"),
        Index("ix_establishment_commission_settings_est", "establishment_id", "valid_from"),
    )

    id: Mapped[uuid.UUID] = _uuid_pk()
    establishment_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("establishments.id", ondelete="RESTRICT"), nullable=False
    )
    receives_commission: Mapped[Optional[bool]] = mapped_column(Boolean, nullable=True)
    rate_bps: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)  # None = pool padrão
    weight_milli: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    auto_payout: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True, server_default=text("true"))
    valid_from: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    valid_to: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    approved_by: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="RESTRICT"), nullable=True
    )
    approval_request_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True), ForeignKey("financial_approval_requests.id", ondelete="RESTRICT"), nullable=True
    )
    reason: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = _created()


class CommissionCampaign(Base):
    """TERMOS de comissão POR PAGAMENTO (cobrança × campanha) — AÇÃO 002 correção (h).

    Cada cobrança paga (contratação inicial, renovação ou alteração paga) gera um termo com o
    seu próprio pool = floor(receita elegível DESTA cobrança × pool_bps) e o seu período
    (renovação = período novo; alteração paga = do pagamento até o fim). Pools nunca se somam
    sobre a mesma receita; estorno de uma cobrança afeta só o seu termo.
    ``daily_plan``/``expected_total``: exibições contratadas por dia no período (snapshot do
    BOOKING do W-INV na 1ª apropriação) — orçamento diário = pool × E_dia / E_total.
    """

    __tablename__ = "commission_campaigns"
    __table_args__ = (
        UniqueConstraint("campaign_id", "payment_order_id", name="uq_commission_campaigns_campaign_payment"),
        CheckConstraint("model IN ('A_POOL','B_ATTRIBUTED')", name="model_valid"),
        CheckConstraint("pool_bps BETWEEN 0 AND 10000", name="pool_bps_range"),
        CheckConstraint("eligible_revenue_cents >= 0 AND pool_cents >= 0", name="amounts_nonneg"),
        CheckConstraint("status IN ('ACTIVE','CANCELLED')", name="status_valid"),
        CheckConstraint("kind IN ('INITIAL','RENEWAL','CHANGE')", name="kind_valid"),
        CheckConstraint("end_date >= start_date", name="period_valid"),
        CheckConstraint("expected_total IS NULL OR expected_total >= 0", name="expected_nonneg"),
        Index("ix_commission_campaigns_campaign", "campaign_id"),
    )

    id: Mapped[uuid.UUID] = _uuid_pk()
    campaign_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    payment_order_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("payment_orders.id", ondelete="RESTRICT"), nullable=False
    )
    org_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    kind: Mapped[str] = mapped_column(String(10), nullable=False, default="INITIAL", server_default=text("'INITIAL'"))
    model: Mapped[str] = mapped_column(String(20), nullable=False)
    pool_bps: Mapped[int] = mapped_column(Integer, nullable=False)
    eligible_revenue_cents: Mapped[int] = mapped_column(BigInteger, nullable=False)
    pool_cents: Mapped[int] = mapped_column(BigInteger, nullable=False)
    start_date: Mapped[date] = mapped_column(Date, nullable=False)
    end_date: Mapped[date] = mapped_column(Date, nullable=False)
    daily_plan: Mapped[Optional[dict]] = mapped_column(JSONB, nullable=True)  # {"YYYY-MM-DD": E_dia}
    expected_total: Mapped[Optional[int]] = mapped_column(BigInteger, nullable=True)
    status: Mapped[str] = mapped_column(String(12), nullable=False, default="ACTIVE")
    cancelled_on: Mapped[Optional[date]] = mapped_column(Date, nullable=True)
    snapshot: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    registered_at: Mapped[datetime] = _created()
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )


class CommissionCampaignScreen(Base):
    """Tela participante de um termo: peso/taxa/atribuição congelados na 1ª vez em que aparece
    num plano diário do termo (peso efetivo = peso do estabelecimento × peso da tela / 1000)."""

    __tablename__ = "commission_campaign_screens"
    __table_args__ = (
        CheckConstraint("weight_milli >= 0 AND establishment_weight_milli >= 0 AND screen_weight_milli >= 0",
                        name="weight_nonneg"),
        CheckConstraint("rate_bps BETWEEN 0 AND 10000", name="rate_range"),
        CheckConstraint("attributed_revenue_cents >= 0", name="attributed_nonneg"),
        Index("ix_commission_campaign_screens_est", "establishment_id"),
    )

    terms_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("commission_campaigns.id", ondelete="RESTRICT"), primary_key=True
    )
    screen_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    campaign_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    establishment_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    establishment_org_id: Mapped[Optional[uuid.UUID]] = mapped_column(UUID(as_uuid=True), nullable=True)
    receives_commission: Mapped[bool] = mapped_column(Boolean, nullable=False)
    weight_milli: Mapped[int] = mapped_column(Integer, nullable=False)
    establishment_weight_milli: Mapped[int] = mapped_column(Integer, nullable=False, default=1000,
                                                            server_default=text("1000"))
    screen_weight_milli: Mapped[int] = mapped_column(Integer, nullable=False, default=1000, server_default=text("1000"))
    rate_bps: Mapped[int] = mapped_column(Integer, nullable=False)
    attributed_revenue_cents: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    created_at: Mapped[datetime] = _created()


class CommissionDayPlan(Base):
    """Plano do DIA de um termo (append-only): exibições contratadas por tela naquele dia local,
    congelado na 1ª apropriação do dia (fonte: BOOKING vigente do W-INV)."""

    __tablename__ = "commission_day_plans"
    __table_args__ = (CheckConstraint("e_day >= 0", name="e_day_nonneg"),)

    terms_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("commission_campaigns.id", ondelete="RESTRICT"), primary_key=True
    )
    local_day: Mapped[date] = mapped_column(Date, primary_key=True)
    plan: Mapped[dict] = mapped_column(JSONB, nullable=False)  # {"<screen_id>": E}
    e_day: Mapped[int] = mapped_column(BigInteger, nullable=False)  # peso do dia no orçamento do termo
    created_at: Mapped[datetime] = _created()


class CommissionAccrualEvent(Base):
    """Dedupe de PoP já apropriado (idempotência por playback_uuid)."""

    __tablename__ = "commission_accrual_events"
    __table_args__ = (Index("ix_commission_accrual_events_key", "campaign_id", "screen_id", "local_day"),)

    playback_uuid: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    campaign_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    screen_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    local_day: Mapped[date] = mapped_column(Date, nullable=False)
    counted: Mapped[bool] = mapped_column(Boolean, nullable=False)
    created_at: Mapped[datetime] = _created()


class CommissionAccrual(Base):
    """Acúmulo diário provisório (µc) por (termo, tela, dia, competência).

    Dado de trabalho até o fechamento: depois do fechamento da competência a linha é
    congelada (``closed_at``); eventos tardios geram linha na competência aberta.
    """

    __tablename__ = "commission_accruals"
    __table_args__ = (
        UniqueConstraint("terms_id", "screen_id", "local_day", "competence", name="uq_commission_accruals_key"),
        CheckConstraint("valid_count >= 0 AND expected_count >= 0 AND amount_ucents >= 0", name="nonneg"),
        CheckConstraint("extract(day FROM competence) = 1", name="competence_day_1"),
        Index("ix_commission_accruals_est_comp", "establishment_id", "competence"),
        Index("ix_commission_accruals_competence", "competence"),
        Index("ix_commission_accruals_campaign", "campaign_id"),
    )

    id: Mapped[uuid.UUID] = _uuid_pk()
    terms_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("commission_campaigns.id", ondelete="RESTRICT"), nullable=False
    )
    campaign_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    screen_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    establishment_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    local_day: Mapped[date] = mapped_column(Date, nullable=False)
    competence: Mapped[date] = mapped_column(Date, nullable=False)
    valid_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    expected_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    amount_ucents: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    reversed_ucents: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0, server_default=text("0"))
    closed_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )


class CommissionCompetenceRun(Base):
    """Fechamento de competência de comissão (último dia 23:59:59 America/Sao_Paulo)."""

    __tablename__ = "commission_competence_runs"
    __table_args__ = (
        CheckConstraint("extract(day FROM competence) = 1", name="competence_day_1"),
        CheckConstraint("status IN ('CLOSED','RELEASED')", name="status_valid"),
    )

    competence: Mapped[date] = mapped_column(Date, primary_key=True)
    status: Mapped[str] = mapped_column(String(10), nullable=False)
    pool_released_cents: Mapped[int] = mapped_column(BigInteger, nullable=False)
    commission_cents: Mapped[int] = mapped_column(BigInteger, nullable=False)
    residue_cents: Mapped[int] = mapped_column(BigInteger, nullable=False)
    carry_cents: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    rounding_residue_ucents: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    close_transaction_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True), ForeignKey("ledger_transactions.id", ondelete="RESTRICT"), nullable=True
    )
    release_transaction_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True), ForeignKey("ledger_transactions.id", ondelete="RESTRICT"), nullable=True
    )
    closed_at: Mapped[datetime] = _created()
    released_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)


class CommissionClosing(Base):
    """Resultado do fechamento por estabelecimento × competência (floor de µc → centavos)."""

    __tablename__ = "commission_closings"
    __table_args__ = (
        CheckConstraint("accrued_ucents >= 0 AND amount_cents >= 0 AND residue_ucents >= 0", name="nonneg"),
    )

    competence: Mapped[date] = mapped_column(
        Date, ForeignKey("commission_competence_runs.competence", ondelete="RESTRICT"), primary_key=True
    )
    establishment_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    accrued_ucents: Mapped[int] = mapped_column(BigInteger, nullable=False)
    amount_cents: Mapped[int] = mapped_column(BigInteger, nullable=False)
    residue_ucents: Mapped[int] = mapped_column(BigInteger, nullable=False)
    created_at: Mapped[datetime] = _created()


class CommissionPoolRelease(Base):
    """Parcela do pool de cada TERMO liberada por competência (técnica do piso acumulado)."""

    __tablename__ = "commission_pool_releases"

    terms_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("commission_campaigns.id", ondelete="RESTRICT"), primary_key=True
    )
    competence: Mapped[date] = mapped_column(Date, primary_key=True)
    released_cents: Mapped[int] = mapped_column(BigInteger, nullable=False)
    created_at: Mapped[datetime] = _created()


# ═══════════════════════════════════════════════════════════════════════════
# DUPLA APROVAÇÃO (ADR_009 item 4; A5 D3)
# ═══════════════════════════════════════════════════════════════════════════

class ApprovalAction(str, enum.Enum):
    RD_WITHDRAWAL = "RD_WITHDRAWAL"  # PIX OUT da RD (sócios)
    REFUND_PIX_OUT = "REFUND_PIX_OUT"  # reembolso (contabiliza + PIX OUT ao pagador ou aguarda estorno do provedor)
    EXPENSE_CREATE = "EXPENSE_CREATE"
    EXPENSE_UPDATE = "EXPENSE_UPDATE"
    PAYABLE_SETTLEMENT = "PAYABLE_SETTLEMENT"
    LEDGER_ADJUSTMENT = "LEDGER_ADJUSTMENT"  # ajuste/crédito manual
    COMMISSION_ADJUSTMENT = "COMMISSION_ADJUSTMENT"
    COMMISSION_EXCEPTION = "COMMISSION_EXCEPTION"  # exceção de comissão por estabelecimento
    SETTING_CHANGE = "SETTING_CHANGE"  # configuração financeira
    RECONCILIATION_ADJUSTMENT = "RECONCILIATION_ADJUSTMENT"
    DEPOSIT_REFUND = "DEPOSIT_REFUND"  # devolução de caução do parceiro (parcial permitida)
    APPROVAL_MODE_CHANGE = "APPROVAL_MODE_CHANGE"  # AÇÃO 003: mudança do modo de aprovação (SINGLE↔DUAL)


class ApprovalMode(str, enum.Enum):
    SINGLE = "SINGLE"  # autorização única do próprio solicitante (Master)
    DUAL = "DUAL"  # A solicita → B aprova


# CHECK mode_consistent (SPEC §2.2) — DUAL e SINGLE não se misturam no banco.
APPROVAL_MODE_CONSISTENT_CHECK = (
    "(approval_mode = 'DUAL' AND self_authorized_by IS NULL AND self_authorized_at IS NULL"
    " AND ((status = 'APPROVED') = (approved_by IS NOT NULL)))"
    " OR (approval_mode = 'SINGLE' AND approved_by IS NULL AND rejected_by IS NULL"
    " AND ((status = 'APPROVED') = (self_authorized_by IS NOT NULL))"
    " AND (self_authorized_by IS NULL OR self_authorized_by = requested_by)"
    " AND ((self_authorized_by IS NULL) = (self_authorized_at IS NULL)))"
)


PIX_OUT_ACTIONS = frozenset({ApprovalAction.RD_WITHDRAWAL.value, ApprovalAction.REFUND_PIX_OUT.value,
                             ApprovalAction.PAYABLE_SETTLEMENT.value, ApprovalAction.DEPOSIT_REFUND.value})


class ApprovalStatus(str, enum.Enum):
    PENDING = "PENDING"
    APPROVED = "APPROVED"
    REJECTED = "REJECTED"
    CANCELLED = "CANCELLED"
    EXPIRED = "EXPIRED"


class ExecutionState(str, enum.Enum):
    NOT_STARTED = "NOT_STARTED"
    PENDING = "PENDING"  # aprovado, aguardando worker
    EXECUTING = "EXECUTING"  # gravado ANTES da chamada externa
    EXECUTED = "EXECUTED"
    FAILED = "FAILED"
    UNKNOWN = "UNKNOWN"  # timeout/5xx: conciliação manual, sem retry automático
    BLOCKED_EXTERNAL = "BLOCKED_EXTERNAL"


class FinancialApprovalRequest(Base):
    """Pedido de aprovação financeira (DUAL: dupla aprovação; SINGLE: autorização única).

    payload/valor/solicitante/modo imutáveis; INSERT só com o modo atual da instância e
    solicitante Master ativo; aprovação/autorização conferidas no banco (triggers em
    ``app.modules.financial.ddl``)."""

    __tablename__ = "financial_approval_requests"
    __table_args__ = (
        UniqueConstraint("idempotency_key", name="uq_financial_approval_requests_idempotency_key"),
        CheckConstraint(f"action_type IN {_in(a.value for a in ApprovalAction)}", name="action_valid"),
        CheckConstraint(f"status IN {_in(s.value for s in ApprovalStatus)}", name="status_valid"),
        CheckConstraint(f"execution_state IN {_in(s.value for s in ExecutionState)}", name="execution_valid"),
        CheckConstraint(f"approval_mode IN {_in(m.value for m in ApprovalMode)}", name="mode_valid"),
        CheckConstraint("approved_by IS NULL OR approved_by <> requested_by", name="approver_not_requester"),
        CheckConstraint("rejected_by IS NULL OR rejected_by <> requested_by", name="rejecter_not_requester"),
        CheckConstraint("amount_cents IS NULL OR amount_cents >= 0", name="amount_nonneg"),
        CheckConstraint(APPROVAL_MODE_CONSISTENT_CHECK, name="mode_consistent"),
        Index("ix_financial_approval_requests_status", "status"),
    )

    id: Mapped[uuid.UUID] = _uuid_pk()
    action_type: Mapped[str] = mapped_column(String(40), nullable=False)
    payload: Mapped[dict] = mapped_column(JSONB, nullable=False)
    payload_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    amount_cents: Mapped[Optional[int]] = mapped_column(BigInteger, nullable=True)
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    requested_by: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="RESTRICT"), nullable=False
    )
    requested_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())
    status: Mapped[str] = mapped_column(String(10), nullable=False, default=ApprovalStatus.PENDING.value)
    # Snapshot do modo da instância na criação (sem default: o código SEMPRE informa).
    approval_mode: Mapped[str] = mapped_column(String(6), nullable=False)
    # SINGLE: o próprio solicitante autoriza (approved_by fica NULL).
    self_authorized_by: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="RESTRICT"), nullable=True
    )
    self_authorized_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    approved_by: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="RESTRICT"), nullable=True
    )
    approved_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    rejected_by: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="RESTRICT"), nullable=True
    )
    rejected_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    rejection_reason: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    cancelled_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    execution_state: Mapped[str] = mapped_column(String(18), nullable=False, default=ExecutionState.NOT_STARTED.value)
    execution_ref: Mapped[Optional[str]] = mapped_column(String(120), nullable=True)
    execution_error: Mapped[Optional[str]] = mapped_column(String(500), nullable=True)
    executed_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    idempotency_key: Mapped[str] = mapped_column(String(120), nullable=False)
    email_code_hash: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    email_code_expires_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    email_code_attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default=text("0"))
    email_code_user_id: Mapped[Optional[uuid.UUID]] = mapped_column(UUID(as_uuid=True), nullable=True)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )


class FinancialApprovalEvent(Base):
    """Trilha append-only do pedido de aprovação."""

    __tablename__ = "financial_approval_events"
    __table_args__ = (Index("ix_financial_approval_events_request", "request_id", "created_at"),)

    id: Mapped[uuid.UUID] = _uuid_pk()
    request_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("financial_approval_requests.id", ondelete="RESTRICT"), nullable=False
    )
    event: Mapped[str] = mapped_column(String(30), nullable=False)
    actor_user_id: Mapped[Optional[uuid.UUID]] = mapped_column(UUID(as_uuid=True), nullable=True)
    data: Mapped[Optional[dict]] = mapped_column(JSONB, nullable=True)
    created_at: Mapped[datetime] = _created()


# ═══════════════════════════════════════════════════════════════════════════
# CHAVES PIX / SAQUES (PIX OUT)
# ═══════════════════════════════════════════════════════════════════════════

PIX_KEY_TYPES = ("cpf", "cnpj", "email", "phone", "random")


class EstablishmentPixKey(Base):
    """Chave PIX de recebimento do estabelecimento (valor cifrado AES-GCM).

    Só ``pix_keys.manage`` cria/troca; vira utilizável em ``active_from`` (12 h).
    Uma chave não revogada por estabelecimento (índice parcial).
    """

    __tablename__ = "establishment_pix_keys"
    __table_args__ = (
        CheckConstraint(f"key_type IN {_in(PIX_KEY_TYPES)}", name="key_type_valid"),
        Index("uq_establishment_pix_keys_current", "establishment_id", unique=True,
              postgresql_where=text("revoked_at IS NULL")),
    )

    id: Mapped[uuid.UUID] = _uuid_pk()
    establishment_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("establishments.id", ondelete="RESTRICT"), nullable=False
    )
    key_type: Mapped[str] = mapped_column(String(10), nullable=False)
    key_value_enc: Mapped[str] = mapped_column(Text, nullable=False)
    key_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    key_masked: Mapped[str] = mapped_column(String(80), nullable=False)
    holder_name: Mapped[str] = mapped_column(String(200), nullable=False)
    holder_document_enc: Mapped[str] = mapped_column(Text, nullable=False)
    holder_document_masked: Mapped[str] = mapped_column(String(30), nullable=False)
    active_from: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    created_by: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="RESTRICT"), nullable=False
    )
    created_at: Mapped[datetime] = _created()
    revoked_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    revoked_by: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="RESTRICT"), nullable=True
    )
    revoke_reason: Mapped[Optional[str]] = mapped_column(String(300), nullable=True)


class WithdrawalKind(str, enum.Enum):
    ESTABLISHMENT = "ESTABLISHMENT"  # comissão do estabelecimento (sem dupla aprovação)
    RD = "RD"  # retirada da RD (dupla aprovação)
    REFUND = "REFUND"  # reembolso ao anunciante (dupla aprovação)
    PAYABLE = "PAYABLE"  # pagamento de conta a pagar (dupla aprovação)
    DEPOSIT = "DEPOSIT"  # devolução de caução ao parceiro (dupla aprovação; chave PIX ativa do estabelecimento)


class WithdrawalStatus(str, enum.Enum):
    REQUESTED = "REQUESTED"
    EXECUTING = "EXECUTING"  # gravado ANTES do POST /withdrawals
    PROCESSING = "PROCESSING"  # provedor aceitou
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"
    UNKNOWN = "UNKNOWN"  # timeout/5xx — sem retry automático


class Withdrawal(Base):
    """PIX OUT executado pela CloudPayPag (``POST /withdrawals``, sem idempotência no provedor)."""

    __tablename__ = "withdrawals"
    __table_args__ = (
        UniqueConstraint("idempotency_key", name="uq_withdrawals_idempotency_key"),
        UniqueConstraint("provider_external_id", name="uq_withdrawals_provider_external_id"),
        CheckConstraint(f"kind IN {_in(k.value for k in WithdrawalKind)}", name="kind_valid"),
        CheckConstraint(f"status IN {_in(s.value for s in WithdrawalStatus)}", name="status_valid"),
        CheckConstraint("origin IN ('MANUAL','AUTOMATIC')", name="origin_valid"),
        CheckConstraint("amount_cents > 0", name="amount_positive"),
        CheckConstraint("(kind IN ('ESTABLISHMENT','DEPOSIT')) = (establishment_id IS NOT NULL)",
                        name="establishment_matches_kind"),
        CheckConstraint("kind = 'ESTABLISHMENT' OR approval_request_id IS NOT NULL", name="rd_money_needs_approval"),
        CheckConstraint(f"pix_key_type IN {_in(PIX_KEY_TYPES)}", name="pix_key_type_valid"),
        Index("ix_withdrawals_establishment", "establishment_id", "status"),
        Index("ix_withdrawals_status", "status"),
    )

    id: Mapped[uuid.UUID] = _uuid_pk()
    kind: Mapped[str] = mapped_column(String(16), nullable=False)
    origin: Mapped[str] = mapped_column(String(10), nullable=False, default="MANUAL")
    establishment_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True), ForeignKey("establishments.id", ondelete="RESTRICT"), nullable=True
    )
    org_id: Mapped[Optional[uuid.UUID]] = mapped_column(UUID(as_uuid=True), nullable=True)
    pix_key_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True), ForeignKey("establishment_pix_keys.id", ondelete="RESTRICT"), nullable=True
    )
    pix_key_type: Mapped[str] = mapped_column(String(10), nullable=False)
    destination_enc: Mapped[str] = mapped_column(Text, nullable=False)  # chave PIX cifrada
    destination_masked: Mapped[str] = mapped_column(String(80), nullable=False)
    # ESTABLISHMENT/RD: ``amount`` = valor enviado (taxa descontada do destinatário — F4).
    # REFUND/PAYABLE: ``amount`` = valor devido ao destinatário; a RD arca com a taxa
    # (``sent_cents`` = amount + taxa simulada).
    amount_cents: Mapped[int] = mapped_column(BigInteger, nullable=False)
    sent_cents: Mapped[Optional[int]] = mapped_column(BigInteger, nullable=True)
    fee_cents: Mapped[Optional[int]] = mapped_column(BigInteger, nullable=True)
    net_cents: Mapped[Optional[int]] = mapped_column(BigInteger, nullable=True)
    status: Mapped[str] = mapped_column(String(12), nullable=False, default=WithdrawalStatus.REQUESTED.value)
    approval_request_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True), ForeignKey("financial_approval_requests.id", ondelete="RESTRICT"), nullable=True
    )
    ref_type: Mapped[Optional[str]] = mapped_column(String(40), nullable=True)
    ref_id: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    requested_by: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="RESTRICT"), nullable=True
    )
    idempotency_key: Mapped[str] = mapped_column(String(120), nullable=False)
    provider_withdrawal_id: Mapped[Optional[int]] = mapped_column(BigInteger, nullable=True)
    provider_external_id: Mapped[Optional[str]] = mapped_column(String(80), nullable=True)
    provider_status: Mapped[Optional[str]] = mapped_column(String(20), nullable=True)
    failure_reason: Mapped[Optional[str]] = mapped_column(String(300), nullable=True)
    requested_at: Mapped[datetime] = _created()
    executing_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    completed_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )


# ═══════════════════════════════════════════════════════════════════════════
# REEMBOLSOS (A5 D7)
# ═══════════════════════════════════════════════════════════════════════════

class RefundStatus(str, enum.Enum):
    AWAITING_APPROVAL = "AWAITING_APPROVAL"
    APPROVED = "APPROVED"  # contabilizado; aguardando pagamento
    PAYING = "PAYING"
    PAID = "PAID"
    BLOCKED_EXTERNAL_DEPENDENCY = "BLOCKED_EXTERNAL_DEPENDENCY"  # sem API de estorno
    REJECTED = "REJECTED"
    FAILED = "FAILED"
    UNKNOWN = "UNKNOWN"


class Refund(Base):
    __tablename__ = "refunds"
    __table_args__ = (
        CheckConstraint("policy IN ('FULL_BEFORE_START','PROPORTIONAL','ADMIN_OVERRIDE')", name="policy_valid"),
        CheckConstraint(f"status IN {_in(s.value for s in RefundStatus)}", name="status_valid"),
        CheckConstraint("refund_cents > 0", name="amount_positive"),
        Index("ix_refunds_order", "payment_order_id"),
    )

    id: Mapped[uuid.UUID] = _uuid_pk()
    payment_order_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("payment_orders.id", ondelete="RESTRICT"), nullable=False
    )
    campaign_id: Mapped[Optional[uuid.UUID]] = mapped_column(UUID(as_uuid=True), nullable=True)
    org_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    policy: Mapped[str] = mapped_column(String(20), nullable=False)
    as_of: Mapped[date] = mapped_column(Date, nullable=False)
    refund_cents: Mapped[int] = mapped_column(BigInteger, nullable=False)
    media_refund_cents: Mapped[int] = mapped_column(BigInteger, nullable=False)
    services_refund_cents: Mapped[int] = mapped_column(BigInteger, nullable=False)
    executed_valid: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    executed_expected: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    clawback_fraction_ppm: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    calculation: Mapped[dict] = mapped_column(JSONB, nullable=False)
    status: Mapped[str] = mapped_column(String(28), nullable=False)
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    requested_by: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="RESTRICT"), nullable=True
    )
    approval_request_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True), ForeignKey("financial_approval_requests.id", ondelete="RESTRICT"), nullable=True
    )
    ledger_transaction_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True), ForeignKey("ledger_transactions.id", ondelete="RESTRICT"), nullable=True
    )
    withdrawal_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True), ForeignKey("withdrawals.id", ondelete="RESTRICT"), nullable=True
    )
    created_at: Mapped[datetime] = _created()
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )


# ═══════════════════════════════════════════════════════════════════════════
# FINANCEIRO COMPLETO: CONTAS A PAGAR / DESPESAS / CONCILIAÇÃO / NOTA FISCAL
# ═══════════════════════════════════════════════════════════════════════════

class Payable(Base):
    """Conta a pagar / despesa manual (criação e baixa só por dupla aprovação)."""

    __tablename__ = "payables"
    __table_args__ = (
        CheckConstraint("amount_cents > 0", name="amount_positive"),
        CheckConstraint("status IN ('OPEN','PAID','CANCELLED')", name="status_valid"),
        Index("ix_payables_status_due", "status", "due_date"),
    )

    id: Mapped[uuid.UUID] = _uuid_pk()
    description: Mapped[str] = mapped_column(String(300), nullable=False)
    category: Mapped[str] = mapped_column(String(60), nullable=False)
    supplier_name: Mapped[Optional[str]] = mapped_column(String(200), nullable=True)
    supplier_document: Mapped[Optional[str]] = mapped_column(String(20), nullable=True)
    amount_cents: Mapped[int] = mapped_column(BigInteger, nullable=False)
    competence_date: Mapped[date] = mapped_column(Date, nullable=False)
    due_date: Mapped[date] = mapped_column(Date, nullable=False)
    status: Mapped[str] = mapped_column(String(10), nullable=False, default="OPEN")
    approval_request_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("financial_approval_requests.id", ondelete="RESTRICT"), nullable=False
    )
    ledger_transaction_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True), ForeignKey("ledger_transactions.id", ondelete="RESTRICT"), nullable=True
    )
    settlement_approval_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True), ForeignKey("financial_approval_requests.id", ondelete="RESTRICT"), nullable=True
    )
    settled_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = _created()
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )


class ReconciliationRun(Base):
    __tablename__ = "reconciliation_runs"
    __table_args__ = (
        CheckConstraint("status IN ('RUNNING','COMPLETED','FAILED','PROVIDER_UNAVAILABLE')", name="status_valid"),
    )

    id: Mapped[uuid.UUID] = _uuid_pk()
    kind: Mapped[str] = mapped_column(String(12), nullable=False, default="DAILY")
    status: Mapped[str] = mapped_column(String(22), nullable=False, default="RUNNING")
    provider_balance_cents: Mapped[Optional[int]] = mapped_column(BigInteger, nullable=True)
    internal_cash_cents: Mapped[Optional[int]] = mapped_column(BigInteger, nullable=True)
    summary: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    created_by: Mapped[Optional[uuid.UUID]] = mapped_column(UUID(as_uuid=True), nullable=True)
    started_at: Mapped[datetime] = _created()
    finished_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)


class ReconciliationItem(Base):
    """Divergência entre ledger e provedor. Resolução SÓ por ajuste aprovado (dupla aprovação)."""

    __tablename__ = "reconciliation_items"
    __table_args__ = (
        CheckConstraint("status IN ('DIVERGENT','RESOLVED')", name="status_valid"),
        Index("ix_reconciliation_items_status", "status"),
        Index("uq_reconciliation_items_open_key", "kind", "dedupe_key", unique=True,
              postgresql_where=text("status = 'DIVERGENT'")),
    )

    id: Mapped[uuid.UUID] = _uuid_pk()
    run_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True), ForeignKey("reconciliation_runs.id", ondelete="RESTRICT"), nullable=True
    )
    kind: Mapped[str] = mapped_column(String(40), nullable=False)
    dedupe_key: Mapped[str] = mapped_column(String(200), nullable=False)
    internal_ref: Mapped[Optional[str]] = mapped_column(String(120), nullable=True)
    external_ref: Mapped[Optional[str]] = mapped_column(String(120), nullable=True)
    internal_amount_cents: Mapped[Optional[int]] = mapped_column(BigInteger, nullable=True)
    external_amount_cents: Mapped[Optional[int]] = mapped_column(BigInteger, nullable=True)
    detail: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    status: Mapped[str] = mapped_column(String(10), nullable=False, default="DIVERGENT")
    resolution_approval_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True), ForeignKey("financial_approval_requests.id", ondelete="RESTRICT"), nullable=True
    )
    resolution_transaction_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True), ForeignKey("ledger_transactions.id", ondelete="RESTRICT"), nullable=True
    )
    resolved_by: Mapped[Optional[uuid.UUID]] = mapped_column(UUID(as_uuid=True), nullable=True)
    resolved_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = _created()


class FiscalDocumentRequest(Base):
    """Pedido de nota fiscal (porta ``InvoiceProvider`` sem fornecedor — P001 §92)."""

    __tablename__ = "fiscal_document_requests"
    __table_args__ = (
        CheckConstraint("status IN ('REQUESTED','BLOCKED_EXTERNAL_DEPENDENCY','ISSUED','FAILED')", name="status_valid"),
        UniqueConstraint("payment_order_id", name="uq_fiscal_document_requests_order"),
    )

    id: Mapped[uuid.UUID] = _uuid_pk()
    payment_order_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("payment_orders.id", ondelete="RESTRICT"), nullable=False
    )
    org_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    status: Mapped[str] = mapped_column(String(30), nullable=False)
    provider: Mapped[Optional[str]] = mapped_column(String(40), nullable=True)
    provider_ref: Mapped[Optional[str]] = mapped_column(String(120), nullable=True)
    detail: Mapped[Optional[str]] = mapped_column(String(300), nullable=True)
    requested_by: Mapped[Optional[uuid.UUID]] = mapped_column(UUID(as_uuid=True), nullable=True)
    created_at: Mapped[datetime] = _created()


class FinancialPasswordResetCode(Base):
    """Recuperação da senha financeira esquecida: código de 6 dígitos (HMAC), 15 min, uso único.

    Só o PRÓPRIO usuário (inclusive Admin Master — independência P001 §84) redefine, com o
    código + TOTP válido. ``used_at`` marca a redefinição e abre o bloqueio temporário de
    saques/aprovações (``finance.password_reset_cooldown_hours``).
    """

    __tablename__ = "financial_password_reset_codes"
    __table_args__ = (
        CheckConstraint("attempts >= 0", name="attempts_nonneg"),
        Index("ix_financial_password_reset_codes_user", "user_id", "created_at"),
    )

    id: Mapped[uuid.UUID] = _uuid_pk()
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    code_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = _created()
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    used_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    invalidated_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default=text("0"))
    request_ip: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
