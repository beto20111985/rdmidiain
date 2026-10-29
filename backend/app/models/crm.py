"""
RD Mídia Indoor — CRM / Comercial (AÇÃO 002 / W-BIZ).

Requisitos: ETAPA2 #19–#23, #1017–#1027, #1434–#1442; P001 §106–§107; P002 item 33.

- ``sales_reps``            estrutura comercial (vendedor, representante regional,
                            franquia, gerente) com região e % de comissão própria.
- ``leads``                 funil (estágio PT), vendedor responsável, cidade (escopo CITY).
- ``lead_stage_events``     histórico de estágio (append-only).
- ``lead_activities``       interações (ligação, reunião, e-mail, nota).
- ``proposals``             proposta comercial: desconto em bps; acima do limite exige
                            aprovação (``crm.discount_approve``); validade de 30 dias;
                            PDF; conversão proposta → pedido (checkout do W-CAMP).
- ``proposal_items``        itens/pacotes (valores em centavos inteiros).
- ``sales_attributions``    atribuição de venda ao vendedor (pedido, valor, bps
                            congelado) — lida pelo financeiro (W-FIN) para pagar comissão.
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
    String,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base
from app.models.base import BaseModelMixin


class LeadStage(str, enum.Enum):
    NOVO = "NOVO"
    CONTATO = "CONTATO"
    QUALIFICADO = "QUALIFICADO"
    PROPOSTA = "PROPOSTA"
    NEGOCIACAO = "NEGOCIACAO"
    GANHO = "GANHO"
    PERDIDO = "PERDIDO"


class ProposalStatus(str, enum.Enum):
    RASCUNHO = "RASCUNHO"
    AGUARDANDO_APROVACAO = "AGUARDANDO_APROVACAO"
    APROVADA = "APROVADA"
    ENVIADA = "ENVIADA"
    ACEITA = "ACEITA"
    RECUSADA = "RECUSADA"
    EXPIRADA = "EXPIRADA"
    CONVERTIDA = "CONVERTIDA"
    CANCELADA = "CANCELADA"


class SalesRepKind(str, enum.Enum):
    VENDEDOR = "VENDEDOR"
    REPRESENTANTE = "REPRESENTANTE"
    FRANQUIA = "FRANQUIA"
    GERENTE = "GERENTE"


def _in(col: str, enum_cls) -> str:
    return f"{col} IN (" + ",".join(f"'{e.value}'" for e in enum_cls) + ")"


# Compatibilidade de import (nomes antigos). LeadStatus == LeadStage.
LeadStatus = LeadStage


class SalesRep(BaseModelMixin, Base):
    __mapper_args__ = {"eager_defaults": True}  # onupdate server-side sem lazy-load (async)
    __tablename__ = "sales_reps"
    __table_args__ = (
        UniqueConstraint("user_id", name="uq_sales_reps_user_id"),
        CheckConstraint(_in("kind", SalesRepKind), name="kind_valid"),
        CheckConstraint("compensation IN ('COMISSAO','SALARIO_FIXO','AMBOS')", name="compensation_valid"),
        CheckConstraint("commission_bps IS NULL OR (commission_bps >= 0 AND commission_bps <= 10000)",
                        name="commission_bps_range"),
    )

    user_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("users.id", ondelete="RESTRICT"), nullable=False)
    kind: Mapped[str] = mapped_column(String(20), nullable=False, default=SalesRepKind.VENDEDOR.value)
    region_label: Mapped[Optional[str]] = mapped_column(String(120), nullable=True)
    city_key: Mapped[Optional[str]] = mapped_column(String(120), nullable=True)
    uf: Mapped[Optional[str]] = mapped_column(String(2), nullable=True)
    manager_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True), ForeignKey("sales_reps.id", ondelete="SET NULL"), nullable=True
    )
    compensation: Mapped[str] = mapped_column(String(20), nullable=False, default="COMISSAO")
    # NULL = usa o padrão da instância (setting crm.seller_commission_bps)
    commission_bps: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)


class Lead(BaseModelMixin, Base):
    __mapper_args__ = {"eager_defaults": True}  # onupdate server-side sem lazy-load (async)
    __tablename__ = "leads"
    __table_args__ = (
        CheckConstraint(_in("stage", LeadStage), name="stage_valid"),
        Index("ix_leads_stage", "stage"),
        Index("ix_leads_owner_id", "owner_id"),
        Index("ix_leads_city_key", "city_key"),
        Index("ix_leads_email", "email"),
        Index("ix_leads_org_id", "org_id"),
    )

    name: Mapped[str] = mapped_column(String(255), nullable=False)
    company: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    email: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    phone: Mapped[Optional[str]] = mapped_column(String(30), nullable=True)
    source: Mapped[str] = mapped_column(String(50), nullable=False, default="manual")
    interest: Mapped[Optional[str]] = mapped_column(String(30), nullable=True)  # ANUNCIANTE | PARCEIRO | AMBOS
    stage: Mapped[str] = mapped_column(String(20), nullable=False, default=LeadStage.NOVO.value)
    owner_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    city: Mapped[Optional[str]] = mapped_column(String(100), nullable=True)
    uf: Mapped[Optional[str]] = mapped_column(String(2), nullable=True)
    city_key: Mapped[Optional[str]] = mapped_column(String(120), nullable=True)
    org_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True), ForeignKey("organizations.id", ondelete="SET NULL"), nullable=True
    )
    estimated_value_cents: Mapped[Optional[int]] = mapped_column(BigInteger, nullable=True)
    lost_reason: Mapped[Optional[str]] = mapped_column(String(500), nullable=True)
    notes: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    created_by: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    stage_changed_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)


class LeadStageEvent(Base):
    __tablename__ = "lead_stage_events"
    __table_args__ = (Index("ix_lead_stage_events_lead_id", "lead_id"),)

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    lead_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("leads.id", ondelete="CASCADE"), nullable=False)
    from_stage: Mapped[Optional[str]] = mapped_column(String(20), nullable=True)
    to_stage: Mapped[str] = mapped_column(String(20), nullable=False)
    reason: Mapped[Optional[str]] = mapped_column(String(500), nullable=True)
    actor_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())


class LeadActivity(Base):
    __tablename__ = "lead_activities"
    __table_args__ = (
        CheckConstraint("kind IN ('NOTA','LIGACAO','REUNIAO','EMAIL','WHATSAPP','VISITA')", name="kind_valid"),
        Index("ix_lead_activities_lead_id", "lead_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    lead_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("leads.id", ondelete="CASCADE"), nullable=False)
    kind: Mapped[str] = mapped_column(String(20), nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=False)
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())
    actor_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())


class Proposal(BaseModelMixin, Base):
    __mapper_args__ = {"eager_defaults": True}  # onupdate server-side sem lazy-load (async)
    __tablename__ = "proposals"
    __table_args__ = (
        UniqueConstraint("number", name="uq_proposals_number"),
        CheckConstraint(_in("status", ProposalStatus), name="status_valid"),
        CheckConstraint("discount_bps >= 0 AND discount_bps <= 10000", name="discount_bps_range"),
        CheckConstraint("subtotal_cents >= 0 AND discount_cents >= 0 AND total_cents >= 0", name="amounts_non_negative"),
        CheckConstraint("total_cents = subtotal_cents - discount_cents", name="total_consistent"),
        Index("ix_proposals_lead_id", "lead_id"),
        Index("ix_proposals_org_id", "org_id"),
        Index("ix_proposals_status", "status"),
        Index("ix_proposals_seller_id", "seller_id"),
        Index("ix_proposals_city_key", "city_key"),
    )

    number: Mapped[str] = mapped_column(String(20), nullable=False)
    lead_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True), ForeignKey("leads.id", ondelete="RESTRICT"), nullable=True
    )
    org_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True), ForeignKey("organizations.id", ondelete="RESTRICT"), nullable=True
    )
    seller_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("users.id", ondelete="RESTRICT"), nullable=False)
    city_key: Mapped[Optional[str]] = mapped_column(String(120), nullable=True)
    title: Mapped[str] = mapped_column(String(255), nullable=False)
    status: Mapped[str] = mapped_column(String(25), nullable=False, default=ProposalStatus.RASCUNHO.value)
    subtotal_cents: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    discount_bps: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    discount_cents: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    total_cents: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    discount_limit_bps_snapshot: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    discount_approval_required: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    discount_justification: Mapped[Optional[str]] = mapped_column(String(1000), nullable=True)
    discount_approved_by: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="RESTRICT"), nullable=True
    )
    discount_approved_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    discount_decision_reason: Mapped[Optional[str]] = mapped_column(String(1000), nullable=True)
    validity_days: Mapped[int] = mapped_column(Integer, nullable=False, default=30)
    valid_until: Mapped[Optional[date]] = mapped_column(Date, nullable=True)
    expires_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    sent_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    accepted_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    decided_note: Mapped[Optional[str]] = mapped_column(String(1000), nullable=True)
    converted_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    order_id: Mapped[Optional[uuid.UUID]] = mapped_column(UUID(as_uuid=True), nullable=True)
    pdf_key: Mapped[Optional[str]] = mapped_column(String(512), nullable=True)
    pdf_sha256: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    notes: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)


class ProposalItem(Base):
    __tablename__ = "proposal_items"
    __table_args__ = (
        CheckConstraint("quantity > 0", name="quantity_positive"),
        CheckConstraint("unit_price_cents >= 0", name="unit_price_non_negative"),
        CheckConstraint("line_total_cents = quantity * unit_price_cents", name="line_total_consistent"),
        Index("ix_proposal_items_proposal_id", "proposal_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    proposal_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("proposals.id", ondelete="CASCADE"), nullable=False
    )
    position: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    package_code: Mapped[Optional[str]] = mapped_column(String(60), nullable=True)
    description: Mapped[str] = mapped_column(String(500), nullable=False)
    quantity: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    unit_price_cents: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    line_total_cents: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    details: Mapped[Optional[dict]] = mapped_column(JSONB, nullable=True)


class SalesAttribution(Base):
    """Venda atribuída ao vendedor (comissão comercial). O W-FIN apura/paga."""

    __tablename__ = "sales_attributions"
    __table_args__ = (
        UniqueConstraint("order_id", name="uq_sales_attributions_order_id"),
        CheckConstraint("amount_cents >= 0 AND commission_cents >= 0", name="amounts_non_negative"),
        CheckConstraint("commission_bps >= 0 AND commission_bps <= 10000", name="commission_bps_range"),
        CheckConstraint("status IN ('PENDENTE','APURADA','PAGA','CANCELADA')", name="status_valid"),
        Index("ix_sales_attributions_seller_id", "seller_id"),
        Index("ix_sales_attributions_status", "status"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    proposal_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("proposals.id", ondelete="RESTRICT"), nullable=False)
    order_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    seller_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("users.id", ondelete="RESTRICT"), nullable=False)
    sales_rep_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True), ForeignKey("sales_reps.id", ondelete="SET NULL"), nullable=True
    )
    org_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("organizations.id", ondelete="RESTRICT"), nullable=False)
    amount_cents: Mapped[int] = mapped_column(BigInteger, nullable=False)
    commission_bps: Mapped[int] = mapped_column(Integer, nullable=False)
    commission_cents: Mapped[int] = mapped_column(BigInteger, nullable=False)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="PENDENTE", server_default=text("'PENDENTE'"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())
