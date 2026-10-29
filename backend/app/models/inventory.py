"""
RD Mídia Indoor — Inventário por CAPACIDADE em ciclo ancorado (ADR_006; A6 §10.2). W-INV.

Substitui ``InventorySlot``/``InventoryReservation`` (slots de relógio; 0 linhas no
banco — removidos; a migration 0004 dropa ``inventory_slots`` e
``inventory_reservations``).

Tabelas:
- ``screen_inventory_state``: uma linha por TELA — linha de LOCK (checkout, manifesto),
  fuso (snapshot do estabelecimento), política vigente, âncora WRITE-ONCE (trigger;
  reset só por ``rd_reset_screen_anchor`` com justificativa), vendável, versões.
- ``screen_anchor_history``: histórico append-only da âncora (1º start e resets).
- ``inventory_holds``: reserva de 2 h (``inventory.reservation_hours``) pelo relógio
  do BANCO; HELD → CONVERTED | EXPIRED | CANCELLED | PAID_WITHOUT_INVENTORY;
  idempotência por (org, Idempotency-Key); no máximo um HELD por campanha.
- ``booking_patterns``: padrão contratado (datas/dias/faixas LOCAIS, duração em
  unidades de 30 s, frequência, intervalo, prioridade, adjacência, exclusividade).
- ``booking_screens``: telas do padrão (HELD|BOOKED|EXPIRED|CANCELLED|SUSPENDED) com
  decisão de conflito e snapshots (fuso/política) congelados no checkout.
- ``screen_retirements``: relatório de redistribuição quando a tela/estabelecimento
  é removido definitivamente (#1116) — ação do admin, sem redistribuição automática.
"""
from __future__ import annotations

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
    PrimaryKeyConstraint,
    SmallInteger,
    String,
    Text,
    UniqueConstraint,
    event,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base

HOLD_STATUSES = ("HELD", "CONVERTED", "EXPIRED", "CANCELLED", "PAID_WITHOUT_INVENTORY")
BOOKING_STATUSES = ("HELD", "BOOKED", "EXPIRED", "CANCELLED", "SUSPENDED")
ANCHOR_SOURCES = ("PLAYER", "SERVER", "ADMIN_RESET")


def _in(values) -> str:
    return "(" + ", ".join(f"'{v}'" for v in values) + ")"


class ScreenInventoryState(Base):
    __tablename__ = "screen_inventory_state"
    __table_args__ = (
        CheckConstraint(f"anchor_source IS NULL OR anchor_source IN {_in(ANCHOR_SOURCES)}", name="anchor_source"),
        CheckConstraint("(anchor_utc IS NULL) = (anchor_source IS NULL)", name="anchor_source_present"),
        CheckConstraint("inventory_version >= 0 AND manifest_version >= 0", name="versions"),
        Index("ix_screen_inventory_state_sellable", "sellable"),
    )

    screen_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("screens.id", ondelete="RESTRICT"), primary_key=True
    )
    timezone: Mapped[str] = mapped_column(String(64), nullable=False)
    policy_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True), ForeignKey("rotation_policies.id", ondelete="RESTRICT"), nullable=True
    )
    anchor_utc: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    anchor_source: Mapped[Optional[str]] = mapped_column(String(16), nullable=True)
    anchor_device_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True), ForeignKey("devices.id", ondelete="SET NULL"), nullable=True
    )
    anchor_reported_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    sellable: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True, server_default="true")
    unsellable_reason: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    inventory_version: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0, server_default="0")
    manifest_version: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0, server_default="0")
    last_manifest_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now(),
                                                 onupdate=func.now())


class ScreenAnchorHistory(Base):
    __tablename__ = "screen_anchor_history"
    __table_args__ = (
        CheckConstraint(f"source IN {_in(ANCHOR_SOURCES)}", name="source"),
        CheckConstraint("length(btrim(reason)) >= 3", name="reason_required"),
        Index("ix_screen_anchor_history_screen", "screen_id", "created_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    screen_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("screens.id", ondelete="RESTRICT"), nullable=False
    )
    old_anchor: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    new_anchor: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    source: Mapped[str] = mapped_column(String(16), nullable=False)
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    actor_user_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="RESTRICT"), nullable=True
    )
    device_id: Mapped[Optional[uuid.UUID]] = mapped_column(UUID(as_uuid=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())


class InventoryHold(Base):
    __tablename__ = "inventory_holds"
    __table_args__ = (
        UniqueConstraint("org_id", "idempotency_key", name="uq_inventory_holds_org_idempotency"),
        CheckConstraint(f"status IN {_in(HOLD_STATUSES)}", name="status"),
        CheckConstraint("mode IN ('ALL_OR_NOTHING','PARTIAL')", name="mode"),
        CheckConstraint("hold_expires_at > created_at", name="expires_after_created"),
        CheckConstraint("length(idempotency_key) BETWEEN 1 AND 255", name="idempotency_key_len"),
        Index("ux_inventory_holds_one_held_per_campaign", "campaign_id", unique=True,
              postgresql_where=text("status = 'HELD'")),
        Index("ix_inventory_holds_expiring", "hold_expires_at", postgresql_where=text("status = 'HELD'")),
        Index("ix_inventory_holds_campaign", "campaign_id"),
        Index("ix_inventory_holds_order", "order_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    org_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("organizations.id", ondelete="RESTRICT"), nullable=False
    )
    campaign_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("campaigns.id", ondelete="RESTRICT"), nullable=False
    )
    order_id: Mapped[Optional[uuid.UUID]] = mapped_column(UUID(as_uuid=True), nullable=True)
    replaces_hold_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True), ForeignKey("inventory_holds.id", ondelete="RESTRICT"), nullable=True
    )
    status: Mapped[str] = mapped_column(String(30), nullable=False, default="HELD")
    mode: Mapped[str] = mapped_column(String(16), nullable=False, default="ALL_OR_NOTHING")
    idempotency_key: Mapped[str] = mapped_column(String(255), nullable=False)
    request_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())
    hold_expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    converted_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    released_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    release_reason: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    screens_requested: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    screens_held: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    details: Mapped[Optional[dict]] = mapped_column(JSONB, nullable=True)
    created_by: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="RESTRICT"), nullable=True
    )
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now(),
                                                 onupdate=func.now())


class BookingPattern(Base):
    __tablename__ = "booking_patterns"
    __table_args__ = (
        CheckConstraint("date_to >= date_from", name="date_range"),
        CheckConstraint("days_mask BETWEEN 1 AND 127", name="days_mask"),
        CheckConstraint("holiday_mode IN ('INCLUDE')", name="holiday_mode"),
        CheckConstraint("duration_units IN (1, 2, 4)", name="duration_units"),
        CheckConstraint("freq_kind IN ('PER_HOUR','PER_DAY','SPECIFIC_TIME')", name="freq_kind"),
        CheckConstraint("freq_value > 0", name="freq_value"),
        CheckConstraint("freq_kind <> 'SPECIFIC_TIME' OR specific_times IS NOT NULL", name="specific_times"),
        CheckConstraint("freq_kind <> 'PER_DAY' OR per_day_distribution IS NOT NULL", name="per_day_distribution"),
        CheckConstraint("min_interval_seconds >= 0", name="min_interval"),
        CheckConstraint("priority IN ('STANDARD','PREMIUM')", name="priority"),
        CheckConstraint("exclusivity IN ('NONE','COMPETITOR_EXCLUSIVE')", name="exclusivity"),
        CheckConstraint("jsonb_typeof(time_bands) = 'array'", name="time_bands_array"),
        Index("ix_booking_patterns_hold", "hold_id"),
        Index("ix_booking_patterns_campaign", "campaign_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    hold_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("inventory_holds.id", ondelete="RESTRICT"), nullable=False
    )
    campaign_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("campaigns.id", ondelete="RESTRICT"), nullable=False
    )
    advertiser_org_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("organizations.id", ondelete="RESTRICT"), nullable=False
    )
    date_from: Mapped[date] = mapped_column(Date, nullable=False)
    date_to: Mapped[date] = mapped_column(Date, nullable=False)
    days_mask: Mapped[int] = mapped_column(SmallInteger, nullable=False, default=127)
    holiday_mode: Mapped[str] = mapped_column(String(10), nullable=False, default="INCLUDE",
                                              server_default="INCLUDE")
    time_bands: Mapped[list] = mapped_column(JSONB, nullable=False)
    duration_units: Mapped[int] = mapped_column(SmallInteger, nullable=False)
    freq_kind: Mapped[str] = mapped_column(String(16), nullable=False)
    freq_value: Mapped[int] = mapped_column(Integer, nullable=False)
    specific_times: Mapped[Optional[list]] = mapped_column(JSONB, nullable=True)
    min_interval_seconds: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    priority: Mapped[str] = mapped_column(String(10), nullable=False, default="STANDARD")
    allow_same_advertiser_consecutive: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    exclusivity: Mapped[str] = mapped_column(String(32), nullable=False, default="NONE", server_default="NONE")
    per_day_distribution: Mapped[Optional[dict]] = mapped_column(JSONB, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())


class BookingScreen(Base):
    __tablename__ = "booking_screens"
    __table_args__ = (
        PrimaryKeyConstraint("pattern_id", "screen_id", name="pk_booking_screens"),
        CheckConstraint(f"status IN {_in(BOOKING_STATUSES)}", name="status"),
        CheckConstraint("status <> 'HELD' OR hold_expires_at IS NOT NULL", name="held_has_expiry"),
        Index("ix_booking_screens_active", "screen_id", postgresql_where=text("status IN ('HELD','BOOKED')")),
        Index("ix_booking_screens_hold", "hold_id"),
        Index("ix_booking_screens_campaign", "campaign_id"),
    )

    pattern_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("booking_patterns.id", ondelete="RESTRICT"), nullable=False
    )
    screen_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("screens.id", ondelete="RESTRICT"), nullable=False
    )
    hold_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("inventory_holds.id", ondelete="RESTRICT"), nullable=False
    )
    campaign_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("campaigns.id", ondelete="RESTRICT"), nullable=False
    )
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    hold_expires_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    conflict_decision_id: Mapped[Optional[uuid.UUID]] = mapped_column(UUID(as_uuid=True), nullable=True)
    conflict_result: Mapped[str] = mapped_column(String(16), nullable=False, default="ALLOW")
    conflict_reason: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    tz_snapshot: Mapped[str] = mapped_column(String(64), nullable=False)
    policy_id_snapshot: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True), ForeignKey("rotation_policies.id", ondelete="RESTRICT"), nullable=True
    )
    rotation_snapshot: Mapped[dict] = mapped_column(JSONB, nullable=False)
    status_reason: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now(),
                                                 onupdate=func.now())


class ScreenRetirement(Base):
    """Relatório de redistribuição (#1116): tela/estabelecimento removido em definitivo."""

    __tablename__ = "screen_retirements"
    __table_args__ = (
        CheckConstraint("status IN ('OPEN','RESOLVED')", name="status"),
        Index("ix_screen_retirements_status", "status", "created_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    screen_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("screens.id", ondelete="RESTRICT"), nullable=False
    )
    establishment_id: Mapped[Optional[uuid.UUID]] = mapped_column(UUID(as_uuid=True), nullable=True)
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    affected: Mapped[list] = mapped_column(JSONB, nullable=False, default=list)
    status: Mapped[str] = mapped_column(String(10), nullable=False, default="OPEN", server_default="OPEN")
    resolution: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    resolved_by: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="RESTRICT"), nullable=True
    )
    resolved_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())


# ─── DDL extra (harness create_all; a 0004 usa app.modules.inventory.ddl.statements) ──

@event.listens_for(ScreenInventoryState.__table__, "after_create")
def _anchor_ddl(target, connection, **kw):  # pragma: no cover - DDL
    from app.modules.inventory import ddl

    for stmt in ddl.anchor_statements():
        connection.execute(text(stmt))


@event.listens_for(ScreenAnchorHistory.__table__, "after_create")
def _history_ddl(target, connection, **kw):  # pragma: no cover - DDL
    from app.modules.inventory import ddl

    for stmt in ddl.history_statements():
        connection.execute(text(stmt))
