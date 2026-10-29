"""
RD Mídia Indoor — Preços, planos, promoções, cupons e pedidos (W-CAMP, AÇÃO 002).

Requisitos: ETAPA2 #486–#542, #1023, #1028, #1035–#1039; P001 §52–§54; P002 #29;
ADR_009 #7 (preço SEMPRE calculado no servidor e congelado no pedido).

- ``PriceTable`` versionada: só uma ATIVA por vez (índice único parcial); tabela
  ATIVA/ARQUIVADA é imutável (alteração = nova versão por duplicação).
- ``PriceItem``: itens de preço e de custo (margem) — ver ``app.modules.pricing.engine``
  para a semântica de cada ``kind``/``mode``. Valores: centavos (``*_CENTS``) ou bps.
- ``Plan``: pré-definido ou personalizado (personalizado exige aprovação admin);
  margem calculada na ativação (negativa bloqueia; abaixo do mínimo alerta).
- ``Coupon``/``CouponRedemption``: uso atômico (``uses_count < max_uses``) e
  idempotente por pedido (UNIQUE coupon×pedido).
- ``Order``/``OrderItem``: pedido comercial (carrinho com várias campanhas numa
  cobrança). A cobrança (PaymentOrder) é do W-FIN com reference=("campaign_checkout", order.id).
"""
from __future__ import annotations

import enum
import uuid
from datetime import datetime
from typing import Optional

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
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


def _in(values) -> str:
    return "(" + ", ".join(f"'{v}'" for v in values) + ")"


class PriceTableStatus(str, enum.Enum):
    RASCUNHO = "RASCUNHO"
    ATIVA = "ATIVA"
    ARQUIVADA = "ARQUIVADA"


class PriceItemKind(str, enum.Enum):
    # ── formação do preço de mídia ──
    DURATION_BASE = "DURATION_BASE"  # key 30|60|120 · BASE_CENTS (centavos por inserção por tela)
    FREQUENCY = "FREQUENCY"  # key PER_HOUR|PER_DAY|SPECIFIC_TIME|* · faixa min/max de freq_value · MULTIPLIER_BPS
    SCREEN_COUNT = "SCREEN_COUNT"  # faixa min/max de telas · MULTIPLIER_BPS
    PERIOD = "PERIOD"  # key AVULSO|MENSAL|SEMESTRAL|ANUAL · MULTIPLIER_BPS
    REGION = "REGION"  # key região (SUDESTE) | UF (GO) | city_key (GO:goiania) · MULTIPLIER_BPS|ADD_CENTS (mais específico vence)
    TIME_BAND = "TIME_BAND"  # faixa [min,max) em minutos locais · MULTIPLIER_BPS
    SCREEN_CLASS = "SCREEN_CLASS"  # key STANDARD|PREMIUM · MULTIPLIER_BPS
    VENUE_CLASS = "VENUE_CLASS"  # key STANDARD|PREMIUM (estabelecimento premium) · MULTIPLIER_BPS
    ESTABLISHMENT = "ESTABLISHMENT"  # key <uuid> (preço específico de estabelecimento) · MULTIPLIER_BPS
    EXCLUSIVITY = "EXCLUSIVITY"  # key COMPETITOR_EXCLUSIVE · MULTIPLIER_BPS
    PRIORITY = "PRIORITY"  # key PREMIUM · MULTIPLIER_BPS
    MIN_PRICE = "MIN_PRICE"  # FIXED_CENTS — preço mínimo de mídia por campanha
    FEE = "FEE"  # key ART|INSTALLATION|UPGRADE · FIXED_CENTS (cobradas separadas)
    # ── custos para margem (não alteram o preço) ──
    TAX = "TAX"  # PERCENT_BPS sobre a receita
    COST_GATEWAY = "COST_GATEWAY"  # PERCENT_BPS e/ou FIXED_CENTS (por cobrança)
    COST_SELLER = "COST_SELLER"  # PERCENT_BPS (comissão do vendedor)
    COST_SERVER = "COST_SERVER"  # PER_SCREEN_MONTH_CENTS
    COST_EQUIPMENT = "COST_EQUIPMENT"  # PER_SCREEN_MONTH_CENTS
    COST_DESIGNER = "COST_DESIGNER"  # FIXED_CENTS (quando há arte)
    COST_DIRECT = "COST_DIRECT"  # FIXED_CENTS | PERCENT_BPS


class PriceItemMode(str, enum.Enum):
    BASE_CENTS = "BASE_CENTS"
    MULTIPLIER_BPS = "MULTIPLIER_BPS"  # 10000 = ×1,0
    ADD_CENTS = "ADD_CENTS"  # soma ao preço unitário da inserção
    FIXED_CENTS = "FIXED_CENTS"
    PERCENT_BPS = "PERCENT_BPS"
    PER_SCREEN_MONTH_CENTS = "PER_SCREEN_MONTH_CENTS"  # rateado por dia (dias/30)


class PlanKind(str, enum.Enum):
    PREDEFINED = "PREDEFINED"
    CUSTOM = "CUSTOM"


class PlanStatus(str, enum.Enum):
    RASCUNHO = "RASCUNHO"
    PENDENTE_APROVACAO = "PENDENTE_APROVACAO"
    ATIVO = "ATIVO"
    INATIVO = "INATIVO"
    REJEITADO = "REJEITADO"


class OrderStatus(str, enum.Enum):
    AGUARDANDO_PAGAMENTO = "AGUARDANDO_PAGAMENTO"
    PAGO = "PAGO"
    EXPIRADO = "EXPIRADO"
    CANCELADO = "CANCELADO"


class OrderKind(str, enum.Enum):
    CHECKOUT = "CHECKOUT"
    CHANGE = "CHANGE"
    PROPOSAL = "PROPOSAL"
    RENEWAL = "RENEWAL"


class PriceTable(Base):
    __tablename__ = "price_tables"
    __table_args__ = (
        CheckConstraint(f"status IN {_in(s.value for s in PriceTableStatus)}", name="status_valid"),
        UniqueConstraint("version", name="uq_price_tables_version"),
        Index("ux_price_tables_one_active", "status", unique=True, postgresql_where=text("status = 'ATIVA'")),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[str] = mapped_column(String(12), nullable=False, default=PriceTableStatus.RASCUNHO.value)
    notes: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    cloned_from_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True), ForeignKey("price_tables.id", ondelete="RESTRICT"), nullable=True
    )
    created_by: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="RESTRICT"), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())
    activated_by: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="RESTRICT"), nullable=True
    )
    activated_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    archived_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)


class PriceItem(Base):
    __tablename__ = "price_items"
    __table_args__ = (
        CheckConstraint(f"kind IN {_in(k.value for k in PriceItemKind)}", name="kind_valid"),
        CheckConstraint(f"mode IN {_in(m.value for m in PriceItemMode)}", name="mode_valid"),
        CheckConstraint("value >= 0", name="value_nonneg"),
        CheckConstraint("min_value IS NULL OR max_value IS NULL OR max_value >= min_value", name="range_valid"),
        UniqueConstraint("table_id", "kind", "key", "mode", "min_value", "max_value", name="uq_price_items_identity"),
        Index("ix_price_items_table_kind", "table_id", "kind"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    table_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("price_tables.id", ondelete="CASCADE"), nullable=False
    )
    kind: Mapped[str] = mapped_column(String(30), nullable=False)
    key: Mapped[str] = mapped_column(String(100), nullable=False, default="*")
    mode: Mapped[str] = mapped_column(String(30), nullable=False)
    min_value: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    max_value: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    value: Mapped[int] = mapped_column(BigInteger, nullable=False)
    description: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)


class Plan(Base):
    __tablename__ = "plans"
    __table_args__ = (
        CheckConstraint(f"kind IN {_in(k.value for k in PlanKind)}", name="kind_valid"),
        CheckConstraint(f"status IN {_in(s.value for s in PlanStatus)}", name="status_valid"),
        CheckConstraint("period_type IN ('AVULSO', 'MENSAL', 'SEMESTRAL', 'ANUAL')", name="period_type_valid"),
        CheckConstraint("period_type <> 'AVULSO' OR period_days IS NOT NULL", name="avulso_days"),
        CheckConstraint("period_days IS NULL OR period_days BETWEEN 1 AND 366", name="period_days_range"),
        CheckConstraint("duration_s IN (30, 60, 120)", name="duration_valid"),
        CheckConstraint("freq_value > 0", name="freq_value_positive"),
        CheckConstraint("days_mask BETWEEN 1 AND 127", name="days_mask_valid"),
        CheckConstraint("screens_min >= 1 AND screens_max >= screens_min", name="screens_range"),
        CheckConstraint("discount_bps BETWEEN 0 AND 10000", name="discount_range"),
        CheckConstraint("kind <> 'CUSTOM' OR org_id IS NOT NULL", name="custom_has_org"),
        UniqueConstraint("slug", name="uq_plans_slug"),
        Index("ix_plans_status", "status"),
        Index("ix_plans_org_id", "org_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    slug: Mapped[str] = mapped_column(String(140), nullable=False)
    description: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    kind: Mapped[str] = mapped_column(String(12), nullable=False, default=PlanKind.PREDEFINED.value)
    org_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True), ForeignKey("organizations.id", ondelete="RESTRICT"), nullable=True
    )
    status: Mapped[str] = mapped_column(String(20), nullable=False, default=PlanStatus.RASCUNHO.value)
    # parâmetros do pacote
    period_type: Mapped[str] = mapped_column(String(12), nullable=False, default="MENSAL")
    period_days: Mapped[Optional[int]] = mapped_column(SmallInteger, nullable=True)
    duration_s: Mapped[int] = mapped_column(SmallInteger, nullable=False, default=30)
    freq_kind: Mapped[str] = mapped_column(String(20), nullable=False, default="PER_HOUR")
    freq_value: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    specific_times: Mapped[Optional[list]] = mapped_column(JSONB, nullable=True)
    min_interval_s: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    days_mask: Mapped[int] = mapped_column(SmallInteger, nullable=False, default=127)
    time_bands: Mapped[list] = mapped_column(JSONB, nullable=False, default=lambda: [[0, 1440]])
    screens_min: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    screens_max: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    priority: Mapped[str] = mapped_column(String(10), nullable=False, default="STANDARD")
    exclusivity: Mapped[str] = mapped_column(String(30), nullable=False, default="NONE")
    establishment_category_ids: Mapped[Optional[list]] = mapped_column(JSONB, nullable=True)
    region_targets: Mapped[Optional[list]] = mapped_column(JSONB, nullable=True)  # [{type,value}] sugestão
    discount_bps: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    # limites (ETAPA2 #518–#521)
    max_campaigns: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    max_creatives: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    max_screens: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    max_upload_mb: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    # margem calculada na ativação
    reference_quote: Mapped[Optional[dict]] = mapped_column(JSONB, nullable=True)
    margin_bps: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    margin_status: Mapped[Optional[str]] = mapped_column(String(12), nullable=True)
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    cloned_from_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True), ForeignKey("plans.id", ondelete="RESTRICT"), nullable=True
    )
    created_by: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="RESTRICT"), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )
    approved_by: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="RESTRICT"), nullable=True
    )
    approved_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    rejection_reason: Mapped[Optional[str]] = mapped_column(Text, nullable=True)


class Promotion(Base):
    __tablename__ = "promotions"
    __table_args__ = (
        CheckConstraint("discount_bps BETWEEN 1 AND 10000", name="discount_range"),
        CheckConstraint("valid_until > valid_from", name="validity"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    discount_bps: Mapped[int] = mapped_column(Integer, nullable=False)
    valid_from: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    valid_until: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    plan_ids: Mapped[Optional[list]] = mapped_column(JSONB, nullable=True)  # None = todos
    active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    created_by: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="RESTRICT"), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())


class Coupon(Base):
    __tablename__ = "coupons"
    __table_args__ = (
        UniqueConstraint("code", name="uq_coupons_code"),
        CheckConstraint("discount_type IN ('PERCENT', 'FIXED')", name="discount_type_valid"),
        CheckConstraint(
            "(discount_type = 'PERCENT' AND discount_bps BETWEEN 1 AND 10000 AND discount_cents = 0) OR "
            "(discount_type = 'FIXED' AND discount_cents > 0 AND discount_bps = 0)",
            name="discount_consistent",
        ),
        CheckConstraint("uses_count >= 0", name="uses_nonneg"),
        CheckConstraint("max_uses IS NULL OR uses_count <= max_uses", name="uses_within_limit"),
        CheckConstraint("code = upper(code)", name="code_upper"),
        CheckConstraint("valid_until > valid_from", name="validity"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    code: Mapped[str] = mapped_column(String(40), nullable=False)
    description: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    discount_type: Mapped[str] = mapped_column(String(10), nullable=False)
    discount_bps: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    discount_cents: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    valid_from: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    valid_until: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    max_uses: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    uses_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    max_uses_per_org: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    min_order_cents: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    plan_ids: Mapped[Optional[list]] = mapped_column(JSONB, nullable=True)
    active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    created_by: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="RESTRICT"), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())


class CouponRedemption(Base):
    __tablename__ = "coupon_redemptions"
    __table_args__ = (
        UniqueConstraint("coupon_id", "order_id", name="uq_coupon_redemptions_coupon_order"),
        Index("ix_coupon_redemptions_coupon_org", "coupon_id", "org_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    coupon_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("coupons.id", ondelete="RESTRICT"), nullable=False
    )
    org_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("organizations.id", ondelete="RESTRICT"), nullable=False
    )
    order_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("orders.id", ondelete="RESTRICT"), nullable=False
    )
    discount_cents: Mapped[int] = mapped_column(BigInteger, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())
    released_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)


class Order(Base):
    __tablename__ = "orders"
    __table_args__ = (
        CheckConstraint(f"status IN {_in(s.value for s in OrderStatus)}", name="status_valid"),
        CheckConstraint(f"kind IN {_in(k.value for k in OrderKind)}", name="kind_valid"),
        CheckConstraint("total_cents >= 0 AND media_cents >= 0 AND fees_cents >= 0 AND discount_cents >= 0",
                        name="amounts_nonneg"),
        CheckConstraint("total_cents = media_cents + fees_cents - discount_cents - credit_applied_cents",
                        name="total_consistent"),
        UniqueConstraint("org_id", "idempotency_key", name="uq_orders_org_idempotency"),
        Index("ix_orders_org_status", "org_id", "status"),
        Index("ix_orders_expires_at", "expires_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    org_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("organizations.id", ondelete="RESTRICT"), nullable=False
    )
    kind: Mapped[str] = mapped_column(String(12), nullable=False, default=OrderKind.CHECKOUT.value)
    status: Mapped[str] = mapped_column(String(24), nullable=False, default=OrderStatus.AGUARDANDO_PAGAMENTO.value)
    idempotency_key: Mapped[str] = mapped_column(String(100), nullable=False)
    request_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    media_cents: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    fees_cents: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    discount_cents: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    credit_applied_cents: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)  # saldo pré-pago (preparado)
    total_cents: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    coupon_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True), ForeignKey("coupons.id", ondelete="RESTRICT"), nullable=True
    )
    proposal_id: Mapped[Optional[uuid.UUID]] = mapped_column(UUID(as_uuid=True), nullable=True)
    commercial_discount_bps: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    commercial_discount_approved_by: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="RESTRICT"), nullable=True
    )
    payment_order_id: Mapped[Optional[uuid.UUID]] = mapped_column(UUID(as_uuid=True), nullable=True)
    response: Mapped[Optional[dict]] = mapped_column(JSONB, nullable=True)  # replay idempotente
    expires_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    paid_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    cancelled_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    created_by: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="RESTRICT"), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())


class OrderItem(Base):
    __tablename__ = "order_items"
    __table_args__ = (
        CheckConstraint("amount_cents >= 0", name="amount_nonneg"),
        UniqueConstraint("order_id", "campaign_id", name="uq_order_items_order_campaign"),
        Index("ix_order_items_campaign_id", "campaign_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    order_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("orders.id", ondelete="CASCADE"), nullable=False
    )
    campaign_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("campaigns.id", ondelete="RESTRICT"), nullable=False
    )
    revision_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True), ForeignKey("campaign_revisions.id", ondelete="RESTRICT"), nullable=True
    )
    media_cents: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    fees_cents: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    discount_cents: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    amount_cents: Mapped[int] = mapped_column(BigInteger, nullable=False)
    quote: Mapped[dict] = mapped_column(JSONB, nullable=False)
    quote_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    hold_id: Mapped[Optional[uuid.UUID]] = mapped_column(UUID(as_uuid=True), nullable=True)
