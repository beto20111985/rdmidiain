"""
RD Mídia Indoor — Telas (domínio W-ORG; AÇÃO 002).

Tela = UNIDADE DE INVENTÁRIO (ETAPA2 #159–#165, #1097–#1098; P001 §19, §21, §48):
TV + TV Box, Smart TV com aplicativo, computador, display, navegador ou outro.
LOCAL (estabelecimento) → TELA → DISPOSITIVO (player, W-PLAY em ``app/models/devices.py``).

- ``friendly_id`` = ``TEL-<UF>-XXXX`` (``app.core.ids.new_screen_code``): único e
  IMUTÁVEL (a tela pode trocar de estabelecimento/UF e o ID não muda).
- ``status`` (ciclo de vida, NÃO conectividade): ACTIVE | MAINTENANCE | INACTIVE |
  DECOMMISSIONED. Conectividade/pareamento/reprodução são do W-PLAY (presença).
- ``sellable``: participa do inventário vendável. Tela em MANUTENÇÃO continua vendável
  (ETAPA2 #1115); DECOMMISSIONED, estabelecimento inativo ou sem contrato ⇒ false.
  Remoção definitiva dispara ``screen_permanently_removed`` (listeners do W-INV).
- Hardware atual (snapshot) nas colunas; toda troca (TV, TV Box, hardware) e toda
  mudança de estabelecimento vão para ``screen_history`` (append-only) — o histórico
  de reprodução continua ligado ao mesmo ``screens.id``.
- Audiência: SEMPRE ESTIMADA (``estimated_daily_audience`` + ``audience_method``);
  histórico em ``audience_estimates`` (append-only). Nunca derivada de exibições.
"""
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
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.database import Base
from app.models.base import BaseModelMixin


class ScreenOrientation(str, enum.Enum):
    HORIZONTAL = "HORIZONTAL"
    VERTICAL = "VERTICAL"


class ScreenStatus(str, enum.Enum):
    """Ciclo de vida operacional da tela (não é conectividade)."""

    ACTIVE = "ACTIVE"
    MAINTENANCE = "MAINTENANCE"
    INACTIVE = "INACTIVE"
    DECOMMISSIONED = "DECOMMISSIONED"


class TVOwner(str, enum.Enum):
    """TV própria do estabelecimento × TV da RD em comodato (P001 §23)."""

    RD = "RD"
    ESTABLISHMENT = "ESTABLISHMENT"


class DisplayType(str, enum.Enum):
    TV = "TV"
    MONITOR = "MONITOR"
    LED_PANEL = "LED_PANEL"
    PROJECTOR = "PROJECTOR"
    OTHER = "OTHER"


class PlayerType(str, enum.Enum):
    TVBOX = "TVBOX"
    SMART_TV_APP = "SMART_TV_APP"
    PC = "PC"
    BROWSER = "BROWSER"
    OTHER = "OTHER"


class ScreenClass(str, enum.Enum):
    STANDARD = "STANDARD"
    PREMIUM = "PREMIUM"


class ProgrammingMode(str, enum.Enum):
    """Grade padrão comum do estabelecimento × programação própria da tela (#77–#79)."""

    ESTABLISHMENT_DEFAULT = "ESTABLISHMENT_DEFAULT"
    CUSTOM = "CUSTOM"


class ScreenHistoryEvent(str, enum.Enum):
    CREATED = "CREATED"
    UPDATED = "UPDATED"
    HARDWARE_CHANGED = "HARDWARE_CHANGED"
    TV_REPLACED = "TV_REPLACED"
    PLAYER_REPLACED = "PLAYER_REPLACED"
    MOVED = "MOVED"
    STATUS_CHANGED = "STATUS_CHANGED"
    SELLABLE_CHANGED = "SELLABLE_CHANGED"
    DECOMMISSIONED = "DECOMMISSIONED"
    AUDIENCE_UPDATED = "AUDIENCE_UPDATED"


def _in(values) -> str:
    return "(" + ",".join(f"'{v.value}'" for v in values) + ")"


class Screen(BaseModelMixin, Base):
    __tablename__ = "screens"
    __mapper_args__ = {"eager_defaults": True}
    __table_args__ = (
        Index("ix_screens_friendly_id", "friendly_id", unique=True),
        Index("ix_screens_establishment_id", "establishment_id"),
        Index("ix_screens_status", "status"),
        Index("ix_screens_device_id", "device_id"),
        Index("ix_screens_sellable", "sellable"),
        CheckConstraint(f"status IN {_in(ScreenStatus)}", name="status_valid"),
        CheckConstraint(f"orientation IN {_in(ScreenOrientation)}", name="orientation_valid"),
        CheckConstraint(f"tv_owner IN {_in(TVOwner)}", name="tv_owner_valid"),
        CheckConstraint(f"display_type IN {_in(DisplayType)}", name="display_type_valid"),
        CheckConstraint(f"player_type IN {_in(PlayerType)}", name="player_type_valid"),
        CheckConstraint(f"screen_class IN {_in(ScreenClass)}", name="screen_class_valid"),
        CheckConstraint(f"programming_mode IN {_in(ProgrammingMode)}", name="programming_mode_valid"),
        CheckConstraint("weight_milli > 0 AND weight_milli <= 100000", name="weight_range"),
        CheckConstraint("inches IS NULL OR (inches > 0 AND inches <= 500)", name="inches_range"),
        CheckConstraint(
            "(resolution_w IS NULL OR resolution_w > 0) AND (resolution_h IS NULL OR resolution_h > 0)",
            name="resolution_positive",
        ),
        CheckConstraint(
            "estimated_daily_audience IS NULL OR estimated_daily_audience >= 0", name="audience_nonneg"
        ),
        CheckConstraint(
            "audience_method IS NULL OR audience_method IN ('MANUAL','FLOW_ESTIMATE')", name="audience_method_valid"
        ),
        CheckConstraint("status <> 'DECOMMISSIONED' OR sellable = false", name="decommissioned_not_sellable"),
        CheckConstraint(
            "(status = 'DECOMMISSIONED') = (decommissioned_at IS NOT NULL)", name="decommissioned_at_consistent"
        ),
    )

    establishment_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("establishments.id", ondelete="RESTRICT"), nullable=False
    )
    # TEL-<UF>-XXXX — único e imutável
    friendly_id: Mapped[str] = mapped_column(String(30), nullable=False, unique=True)
    name: Mapped[str] = mapped_column(String(255), nullable=False)

    status: Mapped[str] = mapped_column(
        String(20), nullable=False, default=ScreenStatus.ACTIVE.value, server_default=text("'ACTIVE'")
    )
    sellable: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True, server_default=text("true"))
    decommissioned_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    decommission_reason: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    # Tipo de exibição e de player
    display_type: Mapped[str] = mapped_column(
        String(12), nullable=False, default=DisplayType.TV.value, server_default=text("'TV'")
    )
    player_type: Mapped[str] = mapped_column(
        String(14), nullable=False, default=PlayerType.TVBOX.value, server_default=text("'TVBOX'")
    )

    # Hardware atual (snapshot; histórico em screen_history)
    orientation: Mapped[str] = mapped_column(
        String(20), nullable=False, default=ScreenOrientation.HORIZONTAL.value
    )
    hardware_brand: Mapped[Optional[str]] = mapped_column(String(100), nullable=True)
    hardware_model: Mapped[Optional[str]] = mapped_column(String(100), nullable=True)
    inches: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    resolution_w: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    resolution_h: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    serial_number: Mapped[Optional[str]] = mapped_column(String(100), nullable=True)
    asset_tag: Mapped[Optional[str]] = mapped_column(String(100), nullable=True)  # patrimônio
    player_brand: Mapped[Optional[str]] = mapped_column(String(100), nullable=True)
    player_model: Mapped[Optional[str]] = mapped_column(String(100), nullable=True)
    player_serial_number: Mapped[Optional[str]] = mapped_column(String(100), nullable=True)
    installed_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    installed_by: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )

    # Propriedade da TV (comodato: ver comodato_agreements.screen_id)
    tv_owner: Mapped[str] = mapped_column(String(20), nullable=False, default=TVOwner.ESTABLISHMENT.value)

    # Dispositivo/player pareado (W-PLAY). use_alter: quebra o ciclo devices.screen_id ↔
    # screens.device_id na ordenação do create_all/autogenerate (W-DB, 0004).
    device_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True), ForeignKey("devices.id", ondelete="SET NULL", use_alter=True), nullable=True
    )

    # Classe/peso (premium) e programação
    screen_class: Mapped[str] = mapped_column(
        String(10), nullable=False, default=ScreenClass.STANDARD.value, server_default=text("'STANDARD'")
    )
    weight_milli: Mapped[int] = mapped_column(Integer, nullable=False, default=1000, server_default=text("1000"))
    programming_mode: Mapped[str] = mapped_column(
        String(24),
        nullable=False,
        default=ProgrammingMode.ESTABLISHMENT_DEFAULT.value,
        server_default=text("'ESTABLISHMENT_DEFAULT'"),
    )

    # Audiência ESTIMADA (pessoas/dia) — nunca medida
    estimated_daily_audience: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    audience_method: Mapped[Optional[str]] = mapped_column(String(15), nullable=True)
    audience_updated_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)

    notes: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    created_by: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )

    establishment: Mapped["Establishment"] = relationship(  # noqa: F821
        "Establishment", back_populates="screens"
    )
    device: Mapped[Optional["Device"]] = relationship(  # noqa: F821
        "Device", foreign_keys=[device_id], overlaps="screen"
    )


class ScreenHistory(Base):
    """Linha do tempo imutável da tela (append-only, trigger)."""

    __tablename__ = "screen_history"
    __mapper_args__ = {"eager_defaults": True}
    __table_args__ = (
        Index("ix_screen_history_screen_id_created_at", "screen_id", "created_at"),
        Index("ix_screen_history_event", "event"),
        CheckConstraint(f"event IN {_in(ScreenHistoryEvent)}", name="event_valid"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    screen_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("screens.id", ondelete="RESTRICT"), nullable=False
    )
    event: Mapped[str] = mapped_column(String(20), nullable=False)
    # estabelecimento em que a tela estava APÓS o evento
    establishment_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("establishments.id", ondelete="RESTRICT"), nullable=False
    )
    from_establishment_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True), ForeignKey("establishments.id", ondelete="RESTRICT"), nullable=True
    )
    before_data: Mapped[Optional[dict]] = mapped_column(JSONB, nullable=True)
    after_data: Mapped[Optional[dict]] = mapped_column(JSONB, nullable=True)
    reason: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    actor_user_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.clock_timestamp()
    )


class AudienceEstimate(Base):
    """Histórico (append-only) de audiência ESTIMADA de tela ou estabelecimento."""

    __tablename__ = "audience_estimates"
    __mapper_args__ = {"eager_defaults": True}
    __table_args__ = (
        Index("ix_audience_estimates_screen_id", "screen_id"),
        Index("ix_audience_estimates_establishment_id", "establishment_id"),
        CheckConstraint("subject_type IN ('SCREEN','ESTABLISHMENT')", name="subject_type_valid"),
        CheckConstraint("(subject_type = 'SCREEN') = (screen_id IS NOT NULL)", name="subject_consistent"),
        CheckConstraint("method IN ('MANUAL','FLOW_ESTIMATE')", name="method_valid"),
        CheckConstraint("daily_people >= 0", name="daily_people_nonneg"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    subject_type: Mapped[str] = mapped_column(String(15), nullable=False)
    screen_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True), ForeignKey("screens.id", ondelete="RESTRICT"), nullable=True
    )
    establishment_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("establishments.id", ondelete="RESTRICT"), nullable=False
    )
    daily_people: Mapped[int] = mapped_column(Integer, nullable=False)
    method: Mapped[str] = mapped_column(String(15), nullable=False)
    # FLOW_ESTIMATE: {"daily_visitors": int, "visibility_bps": int}; MANUAL: {"source": "..."}
    basis: Mapped[Optional[dict]] = mapped_column(JSONB, nullable=True)
    note: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    created_by: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.clock_timestamp()
    )


# Device / PairingRequest / DevicePlatform foram movidos para app/models/devices.py
# (W-PLAY, AÇÃO 002). Import mantido por compatibilidade.
from app.models.devices import Device, DevicePlatform, PairingRequest  # noqa: E402,F401
