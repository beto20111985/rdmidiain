"""
RD Mídia Indoor — Suporte / chamados (AÇÃO 002 / W-BIZ).

Requisitos: ETAPA2 #99, #1086–#1091, #1128–#1134; P001 §110; P002 item 36; achado A7-A10.

- ``tickets``         chamado de cliente (anunciante/estabelecimento), técnico ou admin;
                      vínculos: estabelecimento, tela, criativo (``RD-XXXXXX`` informado e
                      resolvido), campanha, anunciante (resolvido pelo criativo — nunca
                      exibido ao estabelecimento), técnico atribuído, SLA.
- ``ticket_messages`` comentários; ``is_internal`` só para a equipe RD (``support.manage``).
- ``ticket_events``   histórico append-only (status, atribuição, prioridade, SLA).
"""
from __future__ import annotations

import enum
import uuid
from datetime import datetime
from typing import Optional

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base
from app.models.base import BaseModelMixin


class TicketStatus(str, enum.Enum):
    ABERTO = "ABERTO"
    EM_ATENDIMENTO = "EM_ATENDIMENTO"
    AGUARDANDO_CLIENTE = "AGUARDANDO_CLIENTE"
    RESOLVIDO = "RESOLVIDO"
    FECHADO = "FECHADO"
    CANCELADO = "CANCELADO"


class TicketPriority(str, enum.Enum):
    BAIXA = "BAIXA"
    MEDIA = "MEDIA"
    ALTA = "ALTA"
    CRITICA = "CRITICA"


TICKET_CATEGORIES = ("DUVIDA", "PROBLEMA_TELA", "RETIRADA_ANUNCIO", "CAMPANHA", "FINANCEIRO", "INSTALACAO",
                     "MANUTENCAO", "OUTRO")


def _in(col: str, values) -> str:
    return f"{col} IN (" + ",".join(f"'{v}'" for v in values) + ")"


class Ticket(BaseModelMixin, Base):
    __tablename__ = "tickets"
    __mapper_args__ = {"eager_defaults": True}
    __table_args__ = (
        UniqueConstraint("number", name="uq_tickets_number"),
        CheckConstraint(_in("status", [s.value for s in TicketStatus]), name="status_valid"),
        CheckConstraint(_in("priority", [p.value for p in TicketPriority]), name="priority_valid"),
        CheckConstraint(_in("category", TICKET_CATEGORIES), name="category_valid"),
        Index("ix_tickets_status", "status"),
        Index("ix_tickets_org_id", "org_id"),
        Index("ix_tickets_establishment_id", "establishment_id"),
        Index("ix_tickets_assigned_to", "assigned_to"),
        Index("ix_tickets_creative_code", "creative_code"),
        Index("ix_tickets_city_key", "city_key"),
        Index("ix_tickets_sla_due_at", "sla_due_at"),
    )

    number: Mapped[str] = mapped_column(String(12), nullable=False)
    org_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True), ForeignKey("organizations.id", ondelete="RESTRICT"), nullable=True
    )
    establishment_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True), ForeignKey("establishments.id", ondelete="SET NULL"), nullable=True
    )
    screen_id: Mapped[Optional[uuid.UUID]] = mapped_column(UUID(as_uuid=True), nullable=True)
    creative_code: Mapped[Optional[str]] = mapped_column(String(12), nullable=True)
    creative_id: Mapped[Optional[uuid.UUID]] = mapped_column(UUID(as_uuid=True), nullable=True)
    campaign_id: Mapped[Optional[uuid.UUID]] = mapped_column(UUID(as_uuid=True), nullable=True)
    advertiser_org_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True), ForeignKey("organizations.id", ondelete="SET NULL"), nullable=True
    )
    category: Mapped[str] = mapped_column(String(20), nullable=False, default="DUVIDA")
    title: Mapped[str] = mapped_column(String(200), nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default=TicketStatus.ABERTO.value)
    priority: Mapped[str] = mapped_column(String(10), nullable=False, default=TicketPriority.MEDIA.value)
    created_by: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("users.id", ondelete="RESTRICT"), nullable=False)
    assigned_to: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    city_key: Mapped[Optional[str]] = mapped_column(String(120), nullable=True)
    sla_due_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    first_response_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    resolved_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    closed_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)


class TicketMessage(Base):
    __tablename__ = "ticket_messages"
    __table_args__ = (
        Index("ix_ticket_messages_ticket_id", "ticket_id"),
        Index("ix_ticket_messages_author_id", "author_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    ticket_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("tickets.id", ondelete="CASCADE"), nullable=False)
    author_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("users.id", ondelete="RESTRICT"), nullable=False)
    body: Mapped[str] = mapped_column(Text, nullable=False)
    is_internal: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())


class TicketEvent(Base):
    """Histórico do chamado (append-only)."""

    __tablename__ = "ticket_events"
    __table_args__ = (Index("ix_ticket_events_ticket_id", "ticket_id"),)

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    ticket_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("tickets.id", ondelete="RESTRICT"), nullable=False)
    actor_id: Mapped[Optional[uuid.UUID]] = mapped_column(UUID(as_uuid=True), nullable=True)
    type: Mapped[str] = mapped_column(String(30), nullable=False)
    from_value: Mapped[Optional[str]] = mapped_column(String(100), nullable=True)
    to_value: Mapped[Optional[str]] = mapped_column(String(100), nullable=True)
    internal: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())
