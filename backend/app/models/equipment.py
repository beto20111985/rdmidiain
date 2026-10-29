"""
RD Mídia Indoor — Estoque e patrimônio (domínio W-ORG; AÇÃO 002).

Requisitos: ETAPA2 #204–#210, #1146–#1151; P001 §22; P002 #35. Menu "Estoque"
(equipamentos) — distinto do "Inventário publicitário" (W-INV).

Modelo de LEDGER DE ESTOQUE:
- ``equipment_items``: TVs, TV Box, cabos, controles, suportes, acessórios.
  ``tracking = SERIAL`` (unidade com série/patrimônio; quantidade sempre 1) ou
  ``BULK`` (item de quantidade, ex.: cabos HDMI).
- ``equipment_movements`` (APPEND-ONLY, trigger): toda entrada, saída, instalação,
  empréstimo, comodato, devolução, manutenção, RMA, substituição e baixa é uma
  movimentação ``from_kind → to_kind`` com quantidade. Nada é editado ou apagado.
- Saldo DERIVADO: saldo(local) = Σ quantidade que entrou − Σ quantidade que saiu
  (``app.modules.assets.service.balances``). Para itens SERIAL o local atual é o único
  com saldo 1; as colunas ``status``/``location_*`` de ``equipment_items`` são uma
  PROJEÇÃO mantida na mesma transação, sob ``SELECT … FOR UPDATE`` do item, e
  conferida contra a derivação nos testes.
- Locais: EXTERNAL (fornecedor/compra), WAREHOUSE (estoque RD, opcionalmente por
  cidade), TECHNICIAN (com o técnico), ESTABLISHMENT (empréstimo/comodato no
  estabelecimento), SCREEN (instalado numa tela), MAINTENANCE, RMA (fabricante),
  DISPOSED (baixado).
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
    ForeignKey,
    Identity,
    Index,
    Integer,
    String,
    Text,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base


class EquipmentType(str, enum.Enum):
    TV = "TV"
    TVBOX = "TVBOX"
    CABLE = "CABLE"
    REMOTE = "REMOTE"
    MOUNT = "MOUNT"
    ACCESSORY = "ACCESSORY"


class TrackingMode(str, enum.Enum):
    SERIAL = "SERIAL"
    BULK = "BULK"


class EquipmentStatus(str, enum.Enum):
    """Situação DERIVADA do local atual (itens SERIAL)."""

    IN_STOCK = "IN_STOCK"
    WITH_TECHNICIAN = "WITH_TECHNICIAN"
    INSTALLED = "INSTALLED"
    LOANED = "LOANED"
    COMODATO = "COMODATO"
    IN_MAINTENANCE = "IN_MAINTENANCE"
    IN_RMA = "IN_RMA"
    WRITTEN_OFF = "WRITTEN_OFF"
    BULK = "BULK"  # itens de quantidade: ver saldos por local


class LocationKind(str, enum.Enum):
    EXTERNAL = "EXTERNAL"
    WAREHOUSE = "WAREHOUSE"
    TECHNICIAN = "TECHNICIAN"
    ESTABLISHMENT = "ESTABLISHMENT"
    SCREEN = "SCREEN"
    MAINTENANCE = "MAINTENANCE"
    RMA = "RMA"
    DISPOSED = "DISPOSED"


class MovementType(str, enum.Enum):
    ENTRY = "ENTRY"  # entrada (compra/recebimento)
    TRANSFER = "TRANSFER"  # entre estoques / técnico
    INSTALL = "INSTALL"  # saída para instalação numa tela
    LOAN = "LOAN"  # empréstimo ao estabelecimento
    COMODATO = "COMODATO"  # saída em comodato (exige acordo)
    RETURN = "RETURN"  # devolução ao estoque
    MAINTENANCE_IN = "MAINTENANCE_IN"
    MAINTENANCE_OUT = "MAINTENANCE_OUT"
    RMA_OUT = "RMA_OUT"
    RMA_RETURN = "RMA_RETURN"
    WRITE_OFF = "WRITE_OFF"  # baixa
    ADJUSTMENT = "ADJUSTMENT"  # ajuste de inventário (itens BULK)


class ItemCondition(str, enum.Enum):
    NEW = "NEW"
    GOOD = "GOOD"
    DAMAGED = "DAMAGED"
    BROKEN = "BROKEN"


def _in(values) -> str:
    return "(" + ",".join(f"'{v.value}'" for v in values) + ")"


class EquipmentItem(Base):
    __tablename__ = "equipment_items"
    __mapper_args__ = {"eager_defaults": True}
    __table_args__ = (
        Index("ix_equipment_items_type", "type"),
        Index("ix_equipment_items_status", "status"),
        Index("ix_equipment_items_location_screen_id", "location_screen_id"),
        Index("ix_equipment_items_location_establishment_id", "location_establishment_id"),
        Index("ix_equipment_items_scope_org_id", "scope_org_id"),
        Index("ix_equipment_items_location_city_key", "location_city_key"),
        Index("uq_equipment_items_asset_tag", "asset_tag", unique=True, postgresql_where=text("asset_tag IS NOT NULL")),
        Index(
            "uq_equipment_items_type_serial",
            "type",
            "serial_number",
            unique=True,
            postgresql_where=text("serial_number IS NOT NULL"),
        ),
        CheckConstraint(f"type IN {_in(EquipmentType)}", name="type_valid"),
        CheckConstraint(f"tracking IN {_in(TrackingMode)}", name="tracking_valid"),
        CheckConstraint(f"status IN {_in(EquipmentStatus)}", name="status_valid"),
        CheckConstraint(f"location_kind IS NULL OR location_kind IN {_in(LocationKind)}", name="location_kind_valid"),
        CheckConstraint(f"condition IN {_in(ItemCondition)}", name="condition_valid"),
        CheckConstraint("(tracking = 'BULK') = (status = 'BULK')", name="bulk_status_consistent"),
        CheckConstraint("tracking = 'SERIAL' OR (serial_number IS NULL AND asset_tag IS NULL)", name="bulk_no_serial"),
        CheckConstraint("purchase_cost_cents IS NULL OR purchase_cost_cents >= 0", name="cost_nonneg"),
        CheckConstraint("inches IS NULL OR inches > 0", name="inches_positive"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    type: Mapped[str] = mapped_column(String(12), nullable=False)
    tracking: Mapped[str] = mapped_column(String(8), nullable=False, default=TrackingMode.SERIAL.value)
    name: Mapped[str] = mapped_column(String(160), nullable=False)
    brand: Mapped[Optional[str]] = mapped_column(String(100), nullable=True)
    model: Mapped[Optional[str]] = mapped_column(String(100), nullable=True)
    serial_number: Mapped[Optional[str]] = mapped_column(String(100), nullable=True)
    asset_tag: Mapped[Optional[str]] = mapped_column(String(100), nullable=True)  # nº de patrimônio
    inches: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    unit: Mapped[str] = mapped_column(String(10), nullable=False, default="UN", server_default=text("'UN'"))
    condition: Mapped[str] = mapped_column(
        String(10), nullable=False, default=ItemCondition.NEW.value, server_default=text("'NEW'")
    )
    supplier: Mapped[Optional[str]] = mapped_column(String(160), nullable=True)
    purchase_date: Mapped[Optional[date]] = mapped_column(Date, nullable=True)
    purchase_cost_cents: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    warranty_until: Mapped[Optional[date]] = mapped_column(Date, nullable=True)
    notes: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    # ── projeção do local atual (SERIAL); mantida pelo serviço sob lock do item ──
    status: Mapped[str] = mapped_column(String(16), nullable=False, default=EquipmentStatus.IN_STOCK.value)
    location_kind: Mapped[Optional[str]] = mapped_column(String(14), nullable=True)
    location_city_key: Mapped[Optional[str]] = mapped_column(String(120), nullable=True)
    location_establishment_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True), ForeignKey("establishments.id", ondelete="RESTRICT"), nullable=True
    )
    location_screen_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True), ForeignKey("screens.id", ondelete="RESTRICT"), nullable=True
    )
    location_user_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="RESTRICT"), nullable=True
    )
    scope_org_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True), ForeignKey("organizations.id", ondelete="RESTRICT"), nullable=True
    )
    comodato_agreement_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True), ForeignKey("comodato_agreements.id", ondelete="RESTRICT"), nullable=True
    )
    expected_return_at: Mapped[Optional[date]] = mapped_column(Date, nullable=True)
    movement_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default=text("0"))
    last_movement_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)

    created_by: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )


class EquipmentMovement(Base):
    """Movimentação de estoque — IMUTÁVEL (append-only por trigger)."""

    __tablename__ = "equipment_movements"
    __mapper_args__ = {"eager_defaults": True}
    __table_args__ = (
        Index("ix_equipment_movements_item_id_seq", "item_id", "seq"),
        Index("ix_equipment_movements_group_id", "group_id"),
        Index("ix_equipment_movements_establishment_id", "establishment_id"),
        Index("ix_equipment_movements_screen_id", "screen_id"),
        Index("ix_equipment_movements_created_at", "created_at"),
        CheckConstraint(f"movement_type IN {_in(MovementType)}", name="movement_type_valid"),
        CheckConstraint(f"from_kind IN {_in(LocationKind)}", name="from_kind_valid"),
        CheckConstraint(f"to_kind IN {_in(LocationKind)}", name="to_kind_valid"),
        CheckConstraint("quantity > 0", name="quantity_positive"),
        CheckConstraint(
            "NOT (from_kind = to_kind AND from_ref_id IS NOT DISTINCT FROM to_ref_id"
            " AND from_city_key IS NOT DISTINCT FROM to_city_key)",
            name="from_to_distinct",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    seq: Mapped[int] = mapped_column(BigInteger, Identity(always=True), nullable=False, unique=True)
    item_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("equipment_items.id", ondelete="RESTRICT"), nullable=False
    )
    group_id: Mapped[Optional[uuid.UUID]] = mapped_column(UUID(as_uuid=True), nullable=True)
    movement_type: Mapped[str] = mapped_column(String(16), nullable=False)
    quantity: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    from_kind: Mapped[str] = mapped_column(String(14), nullable=False)
    from_ref_id: Mapped[Optional[uuid.UUID]] = mapped_column(UUID(as_uuid=True), nullable=True)
    from_city_key: Mapped[Optional[str]] = mapped_column(String(120), nullable=True)
    to_kind: Mapped[str] = mapped_column(String(14), nullable=False)
    to_ref_id: Mapped[Optional[uuid.UUID]] = mapped_column(UUID(as_uuid=True), nullable=True)
    to_city_key: Mapped[Optional[str]] = mapped_column(String(120), nullable=True)
    from_status: Mapped[Optional[str]] = mapped_column(String(16), nullable=True)
    to_status: Mapped[Optional[str]] = mapped_column(String(16), nullable=True)
    # escopo (para listagem/visibilidade): estabelecimento/tela/organização envolvidos
    establishment_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True), ForeignKey("establishments.id", ondelete="RESTRICT"), nullable=True
    )
    screen_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True), ForeignKey("screens.id", ondelete="RESTRICT"), nullable=True
    )
    org_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True), ForeignKey("organizations.id", ondelete="RESTRICT"), nullable=True
    )
    comodato_agreement_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True), ForeignKey("comodato_agreements.id", ondelete="RESTRICT"), nullable=True
    )
    replaced_item_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True), ForeignKey("equipment_items.id", ondelete="RESTRICT"), nullable=True
    )
    reference: Mapped[Optional[str]] = mapped_column(String(120), nullable=True)  # NF, nº RMA, etc.
    expected_return_at: Mapped[Optional[date]] = mapped_column(Date, nullable=True)
    reason: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    performed_by: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="RESTRICT"), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.clock_timestamp()
    )
