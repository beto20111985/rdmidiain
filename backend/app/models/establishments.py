"""
RD Mídia Indoor — Estabelecimentos (domínio W-ORG; AÇÃO 002).

Estabelecimento = unidade física onde as telas ficam (ETAPA2 #74–#100; P001 §17–§18).
Hierarquia: Organização (empresa) → Estabelecimentos (unidades) → Telas.

Geografia normalizada (ETAPA2 #127–#137): ``country`` (ISO-3166 alfa-3, padrão BRA),
``region`` (macro-região derivada da UF), ``state`` (UF), ``city``, ``city_key``
(``UF:cidade-normalizada`` — mesma chave do escopo CITY do RBAC), ``city_ibge_code``
(opcional), ``neighborhood`` + ``neighborhood_key``, ``postal_code`` (8 dígitos),
``lat``/``lng`` com origem (MANUAL | GPS) e precisão.

``city_key``, ``region`` e ``neighborhood_key`` são DERIVADOS automaticamente (evento
ORM ``before_insert``/``before_update``) — nunca divergem de ``state``/``city``.

Timezone IANA obrigatório e validado (``app.core.timeutils.validate_timezone``);
sugestão automática por UF/cidade (``app.modules.establishments.geo``) e correção
pelo admin (``timezone_source = MANUAL``), sempre auditada.

``onboarding_status``: ``AWAITING_CONTRACT`` (aprovado, sem aceite do contrato de
parceria) | ``ACTIVE``. Telas de estabelecimento inativo ou sem contrato não são
vendáveis (``screens.sellable`` é recalculado pelo serviço de telas).
"""
from __future__ import annotations

import enum
import uuid
from datetime import date, datetime
from typing import Optional

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    Date,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    event,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.database import Base
from app.models.base import FullBaseModelMixin


class OnboardingStatus(str, enum.Enum):
    AWAITING_CONTRACT = "AWAITING_CONTRACT"
    ACTIVE = "ACTIVE"


class GeoSource(str, enum.Enum):
    MANUAL = "MANUAL"
    GPS = "GPS"


class TimezoneSource(str, enum.Enum):
    AUTO = "AUTO"
    MANUAL = "MANUAL"


class VenueClass(str, enum.Enum):
    STANDARD = "STANDARD"
    PREMIUM = "PREMIUM"


class AudienceMethod(str, enum.Enum):
    MANUAL = "MANUAL"
    FLOW_ESTIMATE = "FLOW_ESTIMATE"


class Establishment(FullBaseModelMixin, Base):
    """Local físico onde as telas são instaladas. Pertence a uma organização."""

    __tablename__ = "establishments"
    __mapper_args__ = {"eager_defaults": True}
    __table_args__ = (
        Index("ix_establishments_org_id", "org_id"),
        Index("ix_establishments_city_state", "city", "state"),
        Index("ix_establishments_city_key", "city_key"),
        Index("ix_establishments_state", "state"),
        Index("ix_establishments_region", "region"),
        Index("ix_establishments_is_active", "is_active"),
        Index("ix_establishments_category_id", "category_id"),
        Index("ix_establishments_partner_plan_id", "partner_plan_id"),
        CheckConstraint("document_type IS NULL OR document_type IN ('CPF','CNPJ')", name="document_type_valid"),
        CheckConstraint("char_length(state) = 2", name="state_len"),
        CheckConstraint(
            "region IN ('NORTE','NORDESTE','CENTRO_OESTE','SUDESTE','SUL')", name="region_valid"
        ),
        CheckConstraint("postal_code IS NULL OR postal_code ~ '^[0-9]{8}$'", name="postal_code_digits"),
        CheckConstraint("lat IS NULL OR (lat >= -90 AND lat <= 90)", name="lat_range"),
        CheckConstraint("lng IS NULL OR (lng >= -180 AND lng <= 180)", name="lng_range"),
        CheckConstraint("(lat IS NULL) = (lng IS NULL)", name="coords_pair"),
        CheckConstraint("geo_source IS NULL OR geo_source IN ('MANUAL','GPS')", name="geo_source_valid"),
        CheckConstraint("timezone_source IN ('AUTO','MANUAL')", name="timezone_source_valid"),
        CheckConstraint("venue_class IN ('STANDARD','PREMIUM')", name="venue_class_valid"),
        CheckConstraint("weight_milli > 0 AND weight_milli <= 100000", name="weight_range"),
        CheckConstraint("onboarding_status IN ('AWAITING_CONTRACT','ACTIVE')", name="onboarding_status_valid"),
        CheckConstraint("monthly_fee_cents IS NULL OR monthly_fee_cents >= 0", name="monthly_fee_nonneg"),
        CheckConstraint(
            "estimated_daily_visitors IS NULL OR estimated_daily_visitors >= 0", name="visitors_nonneg"
        ),
        CheckConstraint(
            "audience_method IS NULL OR audience_method IN ('MANUAL','FLOW_ESTIMATE')", name="audience_method_valid"
        ),
    )

    org_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("organizations.id", ondelete="RESTRICT"), nullable=False
    )
    # Nome da unidade exibido no painel (ex.: "Mercado X — Unidade Centro")
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    trade_name: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)  # nome fantasia
    legal_name: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)  # razão social
    document: Mapped[Optional[str]] = mapped_column(String(18), nullable=True)  # CPF/CNPJ normalizado
    document_type: Mapped[Optional[str]] = mapped_column(String(4), nullable=True)
    responsible_name: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    phone: Mapped[Optional[str]] = mapped_column(String(30), nullable=True)
    email: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)

    # Endereço / geografia
    address: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    number: Mapped[Optional[str]] = mapped_column(String(20), nullable=True)
    complement: Mapped[Optional[str]] = mapped_column(String(100), nullable=True)
    neighborhood: Mapped[Optional[str]] = mapped_column(String(100), nullable=True)
    neighborhood_key: Mapped[Optional[str]] = mapped_column(String(120), nullable=True)
    city: Mapped[str] = mapped_column(String(100), nullable=False)
    city_key: Mapped[str] = mapped_column(String(120), nullable=False)
    city_ibge_code: Mapped[Optional[str]] = mapped_column(String(7), nullable=True)
    state: Mapped[str] = mapped_column(String(2), nullable=False)
    region: Mapped[str] = mapped_column(String(12), nullable=False)
    country: Mapped[str] = mapped_column(String(3), nullable=False, default="BRA", server_default=text("'BRA'"))
    postal_code: Mapped[Optional[str]] = mapped_column(String(8), nullable=True)

    lat: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    lng: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    geo_source: Mapped[Optional[str]] = mapped_column(String(10), nullable=True)
    geo_accuracy_m: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    geo_captured_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)

    # Fuso IANA (UTC no banco; horários contratados em hora local — P001 §18)
    timezone: Mapped[str] = mapped_column(String(64), nullable=False, default="America/Sao_Paulo")
    timezone_source: Mapped[str] = mapped_column(
        String(10), nullable=False, default=TimezoneSource.AUTO.value, server_default=text("'AUTO'")
    )

    # Tipo/ramo (taxonomia SETOR > CATEGORIA > SUBCATEGORIA)
    category_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True), ForeignKey("taxonomy_nodes.id", ondelete="RESTRICT"), nullable=True
    )

    # Classe/peso do local (premium) e audiência ESTIMADA (nunca medida)
    venue_class: Mapped[str] = mapped_column(
        String(10), nullable=False, default=VenueClass.STANDARD.value, server_default=text("'STANDARD'")
    )
    weight_milli: Mapped[int] = mapped_column(Integer, nullable=False, default=1000, server_default=text("1000"))
    estimated_daily_visitors: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    audience_method: Mapped[Optional[str]] = mapped_column(String(15), nullable=True)
    audience_updated_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)

    # Configuração comercial definida pelo admin no cadastro (ETAPA2 #16, #1417–#1420)
    partner_plan_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True), ForeignKey("partner_plans.id", ondelete="RESTRICT"), nullable=True
    )
    receives_commission: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default=text("true")
    )
    pays_monthly_fee: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, server_default=text("false"))
    monthly_fee_cents: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)

    # Onboarding / instalação (ETAPA2 #999–#1016)
    onboarding_status: Mapped[str] = mapped_column(
        String(20), nullable=False, default=OnboardingStatus.ACTIVE.value, server_default=text("'ACTIVE'")
    )
    responsible_technician_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    installation_date: Mapped[Optional[date]] = mapped_column(Date, nullable=True)

    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    deactivated_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    deactivation_reason: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    # Configuração operacional livre (sem horário de funcionamento: não bloqueia veiculação — #87)
    operational_config: Mapped[Optional[dict]] = mapped_column(JSONB, nullable=True)
    notes: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    created_by: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )

    organization: Mapped["Organization"] = relationship(  # noqa: F821
        "Organization", back_populates="establishments"
    )
    screens: Mapped[list["Screen"]] = relationship("Screen", back_populates="establishment")  # noqa: F821


@event.listens_for(Establishment, "before_insert")
@event.listens_for(Establishment, "before_update")
def _derive_geo_keys(mapper, connection, target: Establishment) -> None:  # noqa: ANN001
    """Mantém city_key/region/neighborhood_key coerentes com UF/cidade/bairro."""
    from app.modules.establishments.geo import derive_keys

    keys = derive_keys(target.state, target.city, target.neighborhood)
    target.state = keys["state"]
    target.city_key = keys["city_key"]
    target.region = keys["region"]
    target.neighborhood_key = keys["neighborhood_key"]
