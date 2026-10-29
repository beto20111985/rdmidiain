"""
RD Mídia Indoor — Instalações (técnicos) e manutenção de hardware (domínio W-ORG).

Requisitos: ETAPA2 #1010–#1011, #1135–#1145, #1336–#1341, #1108–#1116; P001 §111–§112.

- ``installations``: registro de instalação feito pelo técnico (painel web): checklist,
  geolocalização do técnico (com distância até o estabelecimento), teste de conexão da
  tela (presença real fornecida pelo W-PLAY), fotos (``installation_photos`` no
  StorageProvider) e assinatura do responsável do estabelecimento (imagem).
- ``maintenance_orders`` + ``maintenance_events`` (append-only): status, técnico
  designado, histórico. Tela em manutenção NÃO sai do inventário (continua
  ``sellable``); redistribuição só com retirada definitiva (``screen_permanently_removed``).
"""
from __future__ import annotations

import enum
import uuid
from datetime import date, datetime
from typing import Optional

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    Date,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base


class InstallationStatus(str, enum.Enum):
    SCHEDULED = "SCHEDULED"
    IN_PROGRESS = "IN_PROGRESS"
    COMPLETED = "COMPLETED"
    CANCELLED = "CANCELLED"


class PhotoKind(str, enum.Enum):
    TV_WORKING = "TV_WORKING"
    EQUIPMENT = "EQUIPMENT"
    SITE = "SITE"
    OTHER = "OTHER"


class MaintenanceStatus(str, enum.Enum):
    OPEN = "OPEN"
    IN_PROGRESS = "IN_PROGRESS"
    WAITING_PARTS = "WAITING_PARTS"
    RESOLVED = "RESOLVED"
    CANCELLED = "CANCELLED"


class MaintenanceComponent(str, enum.Enum):
    TV = "TV"
    TVBOX = "TVBOX"
    CABLE = "CABLE"
    NETWORK = "NETWORK"
    OTHER = "OTHER"


def _in(values) -> str:
    return "(" + ",".join(f"'{v.value}'" for v in values) + ")"


class Installation(Base):
    __tablename__ = "installations"
    __mapper_args__ = {"eager_defaults": True}
    __table_args__ = (
        Index("ix_installations_establishment_id", "establishment_id"),
        Index("ix_installations_screen_id", "screen_id"),
        Index("ix_installations_technician_user_id", "technician_user_id"),
        CheckConstraint(f"status IN {_in(InstallationStatus)}", name="status_valid"),
        CheckConstraint("(tech_lat IS NULL) = (tech_lng IS NULL)", name="tech_coords_pair"),
        CheckConstraint("tech_lat IS NULL OR (tech_lat >= -90 AND tech_lat <= 90)", name="tech_lat_range"),
        CheckConstraint("tech_lng IS NULL OR (tech_lng >= -180 AND tech_lng <= 180)", name="tech_lng_range"),
        CheckConstraint("(status = 'COMPLETED') = (completed_at IS NOT NULL)", name="completed_consistent"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    establishment_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("establishments.id", ondelete="RESTRICT"), nullable=False
    )
    screen_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True), ForeignKey("screens.id", ondelete="RESTRICT"), nullable=True
    )
    technician_user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="RESTRICT"), nullable=False
    )
    status: Mapped[str] = mapped_column(String(12), nullable=False, default=InstallationStatus.SCHEDULED.value)
    scheduled_date: Mapped[Optional[date]] = mapped_column(Date, nullable=True)
    started_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    completed_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    # [{"key": "...", "label": "...", "required": bool, "done": bool, "note": str|null}]
    checklist: Mapped[list] = mapped_column(JSONB, nullable=False, default=list)
    # geolocalização do técnico (navegador/GPS do celular)
    tech_lat: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    tech_lng: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    tech_accuracy_m: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    tech_geo_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    distance_to_establishment_m: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    # último teste de conexão: {"status": "ONLINE|OFFLINE|UNAVAILABLE", "checked_at", "source", ...}
    connection_test: Mapped[Optional[dict]] = mapped_column(JSONB, nullable=True)
    connection_test_waiver: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    # assinatura do responsável (imagem PNG/JPEG no StorageProvider)
    signer_name: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    signer_role: Mapped[Optional[str]] = mapped_column(String(120), nullable=True)
    signer_image_key: Mapped[Optional[str]] = mapped_column(String(500), nullable=True)
    signer_image_sha256: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    signed_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    notes: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    created_by: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )


class InstallationPhoto(Base):
    __tablename__ = "installation_photos"
    __mapper_args__ = {"eager_defaults": True}
    __table_args__ = (
        Index("ix_installation_photos_installation_id", "installation_id"),
        CheckConstraint(f"kind IN {_in(PhotoKind)}", name="kind_valid"),
        CheckConstraint("content_type IN ('image/jpeg','image/png')", name="content_type_valid"),
        CheckConstraint("size_bytes > 0", name="size_positive"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    installation_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("installations.id", ondelete="RESTRICT"), nullable=False
    )
    kind: Mapped[str] = mapped_column(String(12), nullable=False)
    storage_key: Mapped[str] = mapped_column(String(500), nullable=False, unique=True)
    sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    content_type: Mapped[str] = mapped_column(String(20), nullable=False)
    size_bytes: Mapped[int] = mapped_column(BigInteger, nullable=False)
    width: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    height: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    uploaded_by: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())


class MaintenanceOrder(Base):
    __tablename__ = "maintenance_orders"
    __mapper_args__ = {"eager_defaults": True}
    __table_args__ = (
        Index("ix_maintenance_orders_screen_id", "screen_id"),
        Index("ix_maintenance_orders_establishment_id", "establishment_id"),
        Index("ix_maintenance_orders_status", "status"),
        Index("ix_maintenance_orders_technician_user_id", "technician_user_id"),
        CheckConstraint(f"status IN {_in(MaintenanceStatus)}", name="status_valid"),
        CheckConstraint(f"component IN {_in(MaintenanceComponent)}", name="component_valid"),
        CheckConstraint("priority IN ('LOW','NORMAL','HIGH','URGENT')", name="priority_valid"),
        CheckConstraint(
            "(status IN ('RESOLVED','CANCELLED')) = (closed_at IS NOT NULL)", name="closed_consistent"
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    establishment_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("establishments.id", ondelete="RESTRICT"), nullable=False
    )
    screen_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True), ForeignKey("screens.id", ondelete="RESTRICT"), nullable=True
    )
    equipment_item_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True), ForeignKey("equipment_items.id", ondelete="RESTRICT"), nullable=True
    )
    component: Mapped[str] = mapped_column(String(10), nullable=False)
    status: Mapped[str] = mapped_column(String(14), nullable=False, default=MaintenanceStatus.OPEN.value)
    priority: Mapped[str] = mapped_column(String(8), nullable=False, default="NORMAL")
    description: Mapped[str] = mapped_column(Text, nullable=False)
    resolution: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    technician_user_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="RESTRICT"), nullable=True
    )
    support_ticket_id: Mapped[Optional[uuid.UUID]] = mapped_column(UUID(as_uuid=True), nullable=True)
    opened_by: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    opened_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())
    closed_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )


class MaintenanceEvent(Base):
    """Histórico da ordem de manutenção — append-only."""

    __tablename__ = "maintenance_events"
    __mapper_args__ = {"eager_defaults": True}
    __table_args__ = (
        Index("ix_maintenance_events_order_id", "order_id"),
        CheckConstraint(
            "event IN ('OPENED','ASSIGNED','STATUS_CHANGED','NOTE','RESOLVED','CANCELLED')", name="event_valid"
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    order_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("maintenance_orders.id", ondelete="RESTRICT"), nullable=False
    )
    event: Mapped[str] = mapped_column(String(16), nullable=False)
    from_status: Mapped[Optional[str]] = mapped_column(String(14), nullable=True)
    to_status: Mapped[Optional[str]] = mapped_column(String(14), nullable=True)
    technician_user_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    note: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    actor_user_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.clock_timestamp()
    )
