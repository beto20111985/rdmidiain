"""
RD Mídia Indoor — Onboarding do estabelecimento parceiro (domínio W-ORG; AÇÃO 002).

Requisitos: ETAPA2 #999–#1016; REQ-ONB-001..003.

Entradas: cadastro pelo admin (``ADMIN``), convite do comercial (``INVITE``, token
enviado por e-mail) e auto-cadastro (``SELF_SIGNUP``, token enviado por e-mail para
confirmar a posse do endereço). Todas passam por APROVAÇÃO da RD; a aprovação cria a
organização (papel PARTNER) e o estabelecimento (``onboarding_status =
AWAITING_CONTRACT``). O aceite eletrônico do contrato de parceria (usuário logado)
ativa o estabelecimento. Wizard por etapas no backend: ``data`` guarda cada etapa
validada; ``completed_steps`` registra as concluídas.

Tokens: guardados SOMENTE como hash SHA-256 e enviados ao cliente pelo header
``X-RD-Onboarding-Token`` (nunca em query string).
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
    Index,
    String,
    Text,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import ARRAY, JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base


class OnboardingOrigin(str, enum.Enum):
    ADMIN = "ADMIN"
    INVITE = "INVITE"
    SELF_SIGNUP = "SELF_SIGNUP"


class OnboardingAppStatus(str, enum.Enum):
    INVITED = "INVITED"
    IN_PROGRESS = "IN_PROGRESS"
    SUBMITTED = "SUBMITTED"
    APPROVED = "APPROVED"
    REJECTED = "REJECTED"
    CANCELLED = "CANCELLED"


def _in(values) -> str:
    return "(" + ",".join(f"'{v.value}'" for v in values) + ")"


class OnboardingApplication(Base):
    __tablename__ = "onboarding_applications"
    __mapper_args__ = {"eager_defaults": True}
    __table_args__ = (
        Index("ix_onboarding_applications_status", "status"),
        Index("ix_onboarding_applications_email", "email"),
        Index(
            "uq_onboarding_applications_token_hash",
            "token_hash",
            unique=True,
            postgresql_where=text("token_hash IS NOT NULL"),
        ),
        CheckConstraint(f"origin IN {_in(OnboardingOrigin)}", name="origin_valid"),
        CheckConstraint(f"status IN {_in(OnboardingAppStatus)}", name="status_valid"),
        CheckConstraint("origin = 'ADMIN' OR token_hash IS NOT NULL", name="token_for_public"),
        CheckConstraint(
            "(status = 'APPROVED') = (establishment_id IS NOT NULL AND org_id IS NOT NULL)",
            name="approved_has_entities",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    origin: Mapped[str] = mapped_column(String(12), nullable=False)
    status: Mapped[str] = mapped_column(String(12), nullable=False)
    email: Mapped[str] = mapped_column(String(255), nullable=False)
    token_hash: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    token_expires_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    invited_by: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    data: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    completed_steps: Mapped[list[str]] = mapped_column(
        ARRAY(String(20)), nullable=False, default=list, server_default=text("'{}'::varchar[]")
    )
    submitted_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    decided_by: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    decided_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    decision_reason: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    # organização existente (ADMIN pode anexar a uma org já cadastrada) ou criada na aprovação
    org_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True), ForeignKey("organizations.id", ondelete="RESTRICT"), nullable=True
    )
    establishment_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True), ForeignKey("establishments.id", ondelete="RESTRICT"), nullable=True
    )
    partner_plan_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True), ForeignKey("partner_plans.id", ondelete="RESTRICT"), nullable=True
    )
    technician_user_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    installation_date: Mapped[Optional[date]] = mapped_column(Date, nullable=True)
    created_by: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )


class OnboardingDocument(Base):
    """Documento opcional anexado ao onboarding (#1006)."""

    __tablename__ = "onboarding_documents"
    __mapper_args__ = {"eager_defaults": True}
    __table_args__ = (
        Index("ix_onboarding_documents_application_id", "application_id"),
        CheckConstraint(
            "kind IN ('COMPANY_REGISTRATION','ID_DOCUMENT','PROOF_OF_ADDRESS','OTHER')", name="kind_valid"
        ),
        CheckConstraint("content_type IN ('application/pdf','image/jpeg','image/png')", name="content_type_valid"),
        CheckConstraint("size_bytes > 0", name="size_positive"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    application_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("onboarding_applications.id", ondelete="RESTRICT"), nullable=False
    )
    kind: Mapped[str] = mapped_column(String(24), nullable=False)
    storage_key: Mapped[str] = mapped_column(String(500), nullable=False, unique=True)
    sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    content_type: Mapped[str] = mapped_column(String(32), nullable=False)
    size_bytes: Mapped[int] = mapped_column(BigInteger, nullable=False)
    uploaded_by: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())
