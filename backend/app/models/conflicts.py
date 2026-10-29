"""
RD Mídia Indoor — Matriz de conflitos (W-CAMP, AÇÃO 002; A6 §10.7; ADR_006 #11).

Taxonomia (nós de categoria, segmentos, marcas, produtos, competidores explícitos e
política de conteúdo) pertence ao W-ORG (app/models/taxonomy.py) — aqui ficam as
REGRAS da matriz, exceções do estabelecimento (criadas pela RD), overrides admin
por campanha × estabelecimento e as DECISÕES gravadas (explicáveis).
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
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base


def _in(values) -> str:
    return "(" + ", ".join(f"'{v}'" for v in values) + ")"


class RuleSubjectType(str, enum.Enum):
    EST_CATEGORY = "EST_CATEGORY"  # nó de taxonomia do estabelecimento (setor/categoria/subcategoria)
    EST_SEGMENT = "EST_SEGMENT"  # segmento vendido pelo estabelecimento
    EST_ORG = "EST_ORG"  # rede (organização dona do estabelecimento)
    ESTABLISHMENT = "ESTABLISHMENT"  # estabelecimento específico


class RuleObjectType(str, enum.Enum):
    AD_CATEGORY = "AD_CATEGORY"  # categoria do anunciante
    AD_SEGMENT = "AD_SEGMENT"  # segmento do anúncio
    AD_PRODUCT = "AD_PRODUCT"
    AD_BRAND = "AD_BRAND"
    AD_ADVERTISER = "AD_ADVERTISER"


class Effect(str, enum.Enum):
    BLOCK = "BLOCK"
    ALLOW = "ALLOW"


class ExceptionObjectType(str, enum.Enum):
    ADVERTISER = "ADVERTISER"
    BRAND = "BRAND"
    CAMPAIGN = "CAMPAIGN"


class ConflictRule(Base):
    __tablename__ = "conflict_rules"
    __table_args__ = (
        CheckConstraint(f"subject_type IN {_in(s.value for s in RuleSubjectType)}", name="subject_type_valid"),
        CheckConstraint(f"object_type IN {_in(o.value for o in RuleObjectType)}", name="object_type_valid"),
        CheckConstraint("effect IN ('BLOCK', 'ALLOW')", name="effect_valid"),
        CheckConstraint("length(reason) >= 5", name="reason_len"),
        CheckConstraint("valid_until IS NULL OR valid_from IS NULL OR valid_until > valid_from", name="validity"),
        Index("ix_conflict_rules_subject", "subject_type", "subject_id"),
        Index("ix_conflict_rules_object", "object_type", "object_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    subject_type: Mapped[str] = mapped_column(String(20), nullable=False)
    subject_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    object_type: Mapped[str] = mapped_column(String(20), nullable=False)
    object_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    include_descendants: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    effect: Mapped[str] = mapped_column(String(5), nullable=False)
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    valid_from: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    valid_until: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    created_by: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="RESTRICT"), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())
    updated_by: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="RESTRICT"), nullable=True
    )
    updated_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)


class ConflictException(Base):
    """Exceção comercial do estabelecimento — criada SOMENTE pela RD (ETAPA2 #114; P001 §43)."""

    __tablename__ = "conflict_exceptions"
    __table_args__ = (
        CheckConstraint(f"object_type IN {_in(o.value for o in ExceptionObjectType)}", name="object_type_valid"),
        CheckConstraint("length(reason) >= 10", name="reason_len"),
        CheckConstraint("valid_until IS NULL OR valid_until > valid_from", name="validity"),
        Index("ix_conflict_exceptions_establishment", "establishment_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    establishment_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("establishments.id", ondelete="RESTRICT"), nullable=False
    )
    object_type: Mapped[str] = mapped_column(String(12), nullable=False)
    object_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    approved_by: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="RESTRICT"), nullable=False
    )
    valid_from: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())
    valid_until: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    revoked_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    revoked_by: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="RESTRICT"), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())


class ConflictOverride(Base):
    """Override admin campanha × estabelecimento (justificativa ≥ 20, validade, auditoria)."""

    __tablename__ = "conflict_overrides"
    __table_args__ = (
        CheckConstraint("length(btrim(justification)) >= 20", name="justification_len"),
        CheckConstraint("expires_at IS NULL OR expires_at > approved_at", name="validity"),
        Index(
            "ux_conflict_overrides_active",
            "campaign_id",
            "establishment_id",
            unique=True,
            postgresql_where=text("revoked_at IS NULL"),
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    campaign_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("campaigns.id", ondelete="RESTRICT"), nullable=False
    )
    establishment_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("establishments.id", ondelete="RESTRICT"), nullable=False
    )
    justification: Mapped[str] = mapped_column(Text, nullable=False)
    approved_by: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="RESTRICT"), nullable=False
    )
    approved_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())
    expires_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    revoked_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    revoked_by: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="RESTRICT"), nullable=True
    )
    revoke_reason: Mapped[Optional[str]] = mapped_column(Text, nullable=True)


class ConflictDecision(Base):
    """Decisão gravada e explicável (append-only)."""

    __tablename__ = "conflict_decisions"
    __table_args__ = (
        CheckConstraint("result IN ('ALLOW', 'BLOCK')", name="result_valid"),
        Index("ix_conflict_decisions_campaign_est", "campaign_id", "establishment_id", "evaluated_at"),
        Index("ix_conflict_decisions_creative", "creative_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    campaign_id: Mapped[Optional[uuid.UUID]] = mapped_column(UUID(as_uuid=True), nullable=True)
    creative_id: Mapped[Optional[uuid.UUID]] = mapped_column(UUID(as_uuid=True), nullable=True)
    establishment_id: Mapped[Optional[uuid.UUID]] = mapped_column(UUID(as_uuid=True), nullable=True)
    other_creative_id: Mapped[Optional[uuid.UUID]] = mapped_column(UUID(as_uuid=True), nullable=True)
    result: Mapped[str] = mapped_column(String(5), nullable=False)
    reason_code: Mapped[str] = mapped_column(String(40), nullable=False)
    rule_path: Mapped[list] = mapped_column(JSONB, nullable=False)
    engine_version: Mapped[str] = mapped_column(String(20), nullable=False)
    facts_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    evaluated_by: Mapped[Optional[uuid.UUID]] = mapped_column(UUID(as_uuid=True), nullable=True)
    evaluated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())


CONFLICT_APPEND_ONLY_TABLES = ("conflict_decisions",)
