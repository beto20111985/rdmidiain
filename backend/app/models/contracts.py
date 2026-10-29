"""
RD Mídia Indoor — Planos do parceiro, comodato e contratos/aceites (domínio W-ORG).

Requisitos: ETAPA2 #183–#203, #1152–#1158, #1417–#1424; P001 §23, §113.

- ``partner_plans``: planos oferecidos ao estabelecimento parceiro (gratuito ou com
  mensalidade; TV própria × TV RD em comodato; caução, instalação e upgrade
  configuráveis — nenhuma regra comercial única fixa no código). O W-INV usa os
  campos de rotação sugerida no escopo PARTNER_PLAN da política de rotação.
- ``comodato_agreements``: termos de comodato fechados com o estabelecimento (TV RD
  continua da RD; caução sim/não; instalação paga/gratuita; upgrade pago/gratuito/
  negociado; manutenção paga pela RD; TV quebrada paga pelo estabelecimento — todos
  editáveis no fechamento, ETAPA2 #191–#197).
- ``contract_templates``: textos versionados (PARCERIA, TERMOS DO ANUNCIANTE,
  COMODATO). Publicado ⇒ texto imutável (nova versão para alterar).
- ``contract_acceptances`` (append-only): aceite eletrônico com versão, hash do texto,
  usuário, IP, user-agent, data (servidor) e PDF gerado (StorageProvider). O login no
  sistema vale como assinatura (#1157); não é assinatura digital certificada (P001 §113).
  Registro manual de contrato em papel (#1158) é ``MANUAL_RECORD`` (identificado como tal).
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
    String,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base


class TvProvision(str, enum.Enum):
    OWN_TV = "OWN_TV"  # TV do estabelecimento; RD fornece player/TV Box se necessário
    RD_TV_COMODATO = "RD_TV_COMODATO"  # TV + player da RD em comodato


class UpgradePolicy(str, enum.Enum):
    FREE = "FREE"
    PAID = "PAID"
    NEGOTIATED = "NEGOTIATED"


class PayingParty(str, enum.Enum):
    RD = "RD"
    ESTABLISHMENT = "ESTABLISHMENT"


class ContractKind(str, enum.Enum):
    PARTNERSHIP = "PARTNERSHIP"
    ADVERTISER_TERMS = "ADVERTISER_TERMS"
    COMODATO = "COMODATO"


class ContractTemplateStatus(str, enum.Enum):
    DRAFT = "DRAFT"
    PUBLISHED = "PUBLISHED"
    RETIRED = "RETIRED"


class AcceptanceMethod(str, enum.Enum):
    ELECTRONIC = "ELECTRONIC"
    MANUAL_RECORD = "MANUAL_RECORD"


class _Ts:
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )


class PartnerPlan(_Ts, Base):
    __tablename__ = "partner_plans"
    __mapper_args__ = {"eager_defaults": True}
    __table_args__ = (
        CheckConstraint("monthly_fee_cents >= 0", name="monthly_fee_nonneg"),
        CheckConstraint("tv_provision IN ('OWN_TV','RD_TV_COMODATO')", name="tv_provision_valid"),
        CheckConstraint("deposit_cents >= 0 AND (deposit_required OR deposit_cents = 0)", name="deposit_consistent"),
        CheckConstraint(
            "installation_fee_cents >= 0 AND (installation_fee_required OR installation_fee_cents = 0)",
            name="installation_fee_consistent",
        ),
        CheckConstraint("upgrade_policy IN ('FREE','PAID','NEGOTIATED')", name="upgrade_policy_valid"),
        CheckConstraint("upgrade_fee_cents IS NULL OR upgrade_fee_cents >= 0", name="upgrade_fee_nonneg"),
        CheckConstraint("default_tv_inches IS NULL OR default_tv_inches > 0", name="tv_inches_positive"),
        CheckConstraint(
            "(default_internal_s IS NULL) = (default_external_s IS NULL)", name="rotation_pair"
        ),
        CheckConstraint(
            "default_internal_s IS NULL OR (default_internal_s >= 0 AND default_external_s >= 30"
            " AND default_internal_s % 30 = 0 AND default_external_s % 30 = 0"
            " AND 3600 % (default_internal_s + default_external_s) = 0)",
            name="rotation_valid",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    slug: Mapped[str] = mapped_column(String(140), nullable=False, unique=True)
    description: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    monthly_fee_cents: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default=text("0"))
    tv_provision: Mapped[str] = mapped_column(String(16), nullable=False)
    default_tv_inches: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    deposit_required: Mapped[bool] = mapped_column(Boolean, nullable=False)
    deposit_cents: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default=text("0"))
    installation_fee_required: Mapped[bool] = mapped_column(Boolean, nullable=False)
    installation_fee_cents: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default=text("0"))
    upgrade_policy: Mapped[str] = mapped_column(String(12), nullable=False)
    upgrade_fee_cents: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    receives_commission: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True, server_default=text("true"))
    # Rotação sugerida (W-INV: escopo PARTNER_PLAN). Ambos nulos = usa o padrão global.
    default_internal_s: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    default_external_s: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    allow_external_overflow: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=text("false")
    )
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True, server_default=text("true"))
    created_by: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )


class ComodatoAgreement(_Ts, Base):
    __tablename__ = "comodato_agreements"
    __mapper_args__ = {"eager_defaults": True}
    __table_args__ = (
        Index("ix_comodato_agreements_establishment_id", "establishment_id"),
        Index("ix_comodato_agreements_screen_id", "screen_id"),
        Index("ix_comodato_agreements_org_id", "org_id"),
        CheckConstraint("deposit_cents >= 0 AND (deposit_required OR deposit_cents = 0)", name="deposit_consistent"),
        CheckConstraint(
            "installation_fee_cents >= 0 AND (installation_fee_required OR installation_fee_cents = 0)",
            name="installation_fee_consistent",
        ),
        CheckConstraint("upgrade_policy IN ('FREE','PAID','NEGOTIATED')", name="upgrade_policy_valid"),
        CheckConstraint("upgrade_fee_cents IS NULL OR upgrade_fee_cents >= 0", name="upgrade_fee_nonneg"),
        CheckConstraint("maintenance_paid_by IN ('RD','ESTABLISHMENT')", name="maintenance_paid_by_valid"),
        CheckConstraint("breakage_paid_by IN ('RD','ESTABLISHMENT')", name="breakage_paid_by_valid"),
        CheckConstraint("status IN ('ACTIVE','ENDED')", name="status_valid"),
        CheckConstraint("(status = 'ENDED') = (ended_at IS NOT NULL)", name="ended_consistent"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    org_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("organizations.id", ondelete="RESTRICT"), nullable=False
    )
    establishment_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("establishments.id", ondelete="RESTRICT"), nullable=False
    )
    screen_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True), ForeignKey("screens.id", ondelete="RESTRICT"), nullable=True
    )
    partner_plan_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True), ForeignKey("partner_plans.id", ondelete="RESTRICT"), nullable=True
    )
    deposit_required: Mapped[bool] = mapped_column(Boolean, nullable=False)
    deposit_cents: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    installation_fee_required: Mapped[bool] = mapped_column(Boolean, nullable=False)
    installation_fee_cents: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    upgrade_policy: Mapped[str] = mapped_column(String(12), nullable=False)
    upgrade_fee_cents: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    maintenance_paid_by: Mapped[str] = mapped_column(String(15), nullable=False, default=PayingParty.RD.value)
    breakage_paid_by: Mapped[str] = mapped_column(
        String(15), nullable=False, default=PayingParty.ESTABLISHMENT.value
    )
    status: Mapped[str] = mapped_column(String(10), nullable=False, default="ACTIVE")
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())
    ended_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    end_reason: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    notes: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    created_by: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )


class ContractTemplate(_Ts, Base):
    __tablename__ = "contract_templates"
    __mapper_args__ = {"eager_defaults": True}
    __table_args__ = (
        UniqueConstraint("kind", "version", name="uq_contract_templates_kind_version"),
        Index(
            "uq_contract_templates_one_published",
            "kind",
            unique=True,
            postgresql_where=text("status = 'PUBLISHED'"),
        ),
        CheckConstraint("kind IN ('PARTNERSHIP','ADVERTISER_TERMS','COMODATO')", name="kind_valid"),
        CheckConstraint("status IN ('DRAFT','PUBLISHED','RETIRED')", name="status_valid"),
        CheckConstraint("version >= 1", name="version_positive"),
        CheckConstraint("char_length(body_sha256) = 64", name="body_sha256_len"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    kind: Mapped[str] = mapped_column(String(20), nullable=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    title: Mapped[str] = mapped_column(String(200), nullable=False)
    body: Mapped[str] = mapped_column(Text, nullable=False)
    body_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(String(10), nullable=False, default=ContractTemplateStatus.DRAFT.value)
    published_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    retired_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    created_by: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )


class ContractAcceptance(Base):
    """Aceite de contrato (append-only — trigger)."""

    __tablename__ = "contract_acceptances"
    __mapper_args__ = {"eager_defaults": True}
    __table_args__ = (
        Index("ix_contract_acceptances_org_id", "org_id"),
        Index("ix_contract_acceptances_establishment_id", "establishment_id"),
        Index("ix_contract_acceptances_template_id", "template_id"),
        UniqueConstraint(
            "template_id",
            "org_id",
            "establishment_id",
            "comodato_agreement_id",
            name="uq_contract_acceptances_subject",
            postgresql_nulls_not_distinct=True,
        ),
        CheckConstraint("method IN ('ELECTRONIC','MANUAL_RECORD')", name="method_valid"),
        CheckConstraint(
            "(method = 'ELECTRONIC' AND accepted_by_user_id IS NOT NULL AND ip_address IS NOT NULL)"
            " OR (method = 'MANUAL_RECORD' AND recorded_by_user_id IS NOT NULL AND notes IS NOT NULL)",
            name="method_consistent",
        ),
        CheckConstraint("char_length(body_sha256) = 64", name="body_sha256_len"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    template_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("contract_templates.id", ondelete="RESTRICT"), nullable=False
    )
    kind: Mapped[str] = mapped_column(String(20), nullable=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    body_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    org_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("organizations.id", ondelete="RESTRICT"), nullable=False
    )
    establishment_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True), ForeignKey("establishments.id", ondelete="RESTRICT"), nullable=True
    )
    comodato_agreement_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True), ForeignKey("comodato_agreements.id", ondelete="RESTRICT"), nullable=True
    )
    method: Mapped[str] = mapped_column(String(15), nullable=False)
    accepted_by_user_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="RESTRICT"), nullable=True
    )
    recorded_by_user_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="RESTRICT"), nullable=True
    )
    signer_name: Mapped[str] = mapped_column(String(255), nullable=False)
    ip_address: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    user_agent: Mapped[Optional[str]] = mapped_column(String(500), nullable=True)
    accepted_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    pdf_storage_key: Mapped[Optional[str]] = mapped_column(String(500), nullable=True)
    pdf_sha256: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    pdf_size: Mapped[Optional[int]] = mapped_column(BigInteger, nullable=True)
    notes: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.clock_timestamp()
    )
