"""
RD Mídia Indoor — Organizações (domínio W-ORG; AÇÃO 002).

Organização = conceito central (P001 §16; ETAPA2 #17–#18, #1267–#1270). Uma mesma
organização pode acumular PAPÉIS DE NEGÓCIO explícitos (``business_roles``):

- ``ADVERTISER``  — Anunciante (cliente pagante principal);
- ``PARTNER``     — Parceiro/Estabelecimento (dona de estabelecimentos com telas);
- ``AGENCY``      — Agência (gerencia várias organizações anunciantes — ``agency_clients``);
- ``FRANCHISE``   — Franquia/Representante (carteira própria dentro da instância RD).

O antigo bitmask ``type`` (1/2/4, rotulado de forma inconsistente no frontend —
REQ-NEG-018) foi SUBSTITUÍDO por ``business_roles`` (array validado por CHECK).
``OrganizationTypeFlag`` e o atributo ``type`` permanecem apenas como SHIM de
compatibilidade de escrita para código legado (factories/CLI) e convertem para os
papéis explícitos; não há mais coluna ``type`` no banco.

Usuários ↔ organizações: RBAC com escopo (``role_assignments`` scope ORGANIZATION —
fundação W1). Agência → clientes: ``agency_clients`` + ``agency_client_grants``
(atribuições ``agency`` com escopo ORGANIZATION do cliente).
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
    String,
    Text,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import ARRAY, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.database import Base
from app.models.base import BaseModelMixin


class DocumentType(str, enum.Enum):
    """Tipo de documento fiscal (PF = CPF; PJ = CNPJ, inclusive alfanumérico)."""

    CPF = "CPF"
    CNPJ = "CNPJ"


class BusinessRole(str, enum.Enum):
    """Papéis de negócio combináveis de uma organização."""

    ADVERTISER = "ADVERTISER"
    PARTNER = "PARTNER"
    AGENCY = "AGENCY"
    FRANCHISE = "FRANCHISE"


BUSINESS_ROLE_LABELS: dict[str, str] = {
    BusinessRole.ADVERTISER.value: "Anunciante",
    BusinessRole.PARTNER.value: "Parceiro (estabelecimento)",
    BusinessRole.AGENCY.value: "Agência",
    BusinessRole.FRANCHISE.value: "Franquia / Representante",
}
BUSINESS_ROLES: tuple[str, ...] = tuple(r.value for r in BusinessRole)
_ROLES_SQL = ",".join(f"'{r}'" for r in BUSINESS_ROLES)


class KycStatus(str, enum.Enum):
    PENDING = "PENDING"
    APPROVED = "APPROVED"
    REJECTED = "REJECTED"


class OrganizationTypeFlag(int, enum.Enum):
    """DEPRECADO — bitmask legado. Use ``BusinessRole``.

    Mantido só para código legado que ainda constrói ``Organization(type=...)``:
    1 → ADVERTISER; 2 e 4 → PARTNER.
    """

    IS_ADVERTISER = 1
    IS_PARTNER = 2
    IS_ESTABLISHMENT_OWNER = 4


def roles_from_legacy_bitmask(value: int) -> list[str]:
    roles: list[str] = []
    if value & OrganizationTypeFlag.IS_ADVERTISER.value:
        roles.append(BusinessRole.ADVERTISER.value)
    if value & (OrganizationTypeFlag.IS_PARTNER.value | OrganizationTypeFlag.IS_ESTABLISHMENT_OWNER.value):
        roles.append(BusinessRole.PARTNER.value)
    return roles


class Organization(BaseModelMixin, Base):
    """Organização (empresa ou pessoa física)."""

    __tablename__ = "organizations"
    __mapper_args__ = {"eager_defaults": True}
    __table_args__ = (
        Index("ix_organizations_document", "document", unique=True),
        Index("ix_organizations_is_active", "is_active"),
        Index("ix_organizations_city_key", "city_key"),
        Index("ix_organizations_business_roles", "business_roles", postgresql_using="gin"),
        CheckConstraint("document_type IN ('CPF','CNPJ')", name="document_type_valid"),
        CheckConstraint(
            f"business_roles <@ ARRAY[{_ROLES_SQL}]::varchar[]", name="business_roles_valid"
        ),
        CheckConstraint("kyc_status IN ('PENDING','APPROVED','REJECTED')", name="kyc_status_valid"),
        CheckConstraint("state IS NULL OR char_length(state) = 2", name="state_len"),
    )

    # Razão social (PJ) ou nome completo (PF)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    trade_name: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    # Documento normalizado (CPF 11 dígitos; CNPJ 14 caracteres — numérico ou alfanumérico)
    document: Mapped[str] = mapped_column(String(18), nullable=False, unique=True)
    document_type: Mapped[str] = mapped_column(String(4), nullable=False, default=DocumentType.CNPJ.value)

    business_roles: Mapped[list[str]] = mapped_column(
        ARRAY(String(20)), nullable=False, default=list, server_default=text("'{}'::varchar[]")
    )

    responsible_name: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    email: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    phone: Mapped[Optional[str]] = mapped_column(String(30), nullable=True)

    # Endereço fiscal/sede (opcional) — city_key habilita escopo CITY (comercial/franquia)
    address: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    number: Mapped[Optional[str]] = mapped_column(String(20), nullable=True)
    complement: Mapped[Optional[str]] = mapped_column(String(100), nullable=True)
    neighborhood: Mapped[Optional[str]] = mapped_column(String(100), nullable=True)
    city: Mapped[Optional[str]] = mapped_column(String(100), nullable=True)
    state: Mapped[Optional[str]] = mapped_column(String(2), nullable=True)
    postal_code: Mapped[Optional[str]] = mapped_column(String(8), nullable=True)
    city_key: Mapped[Optional[str]] = mapped_column(String(120), nullable=True)

    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    deactivated_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    deactivation_reason: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    # KYC mínimo (REQ-ORG-015 — campos detalhados dependem do proprietário, D-18)
    kyc_status: Mapped[str] = mapped_column(
        String(10), nullable=False, default=KycStatus.PENDING.value, server_default=text("'PENDING'")
    )
    kyc_reviewed_by: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    kyc_reviewed_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    kyc_notes: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    notes: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    created_by: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )

    establishments: Mapped[list["Establishment"]] = relationship(  # noqa: F821
        "Establishment", back_populates="organization"
    )

    # ── papéis ──
    def has_role(self, role: str | BusinessRole) -> bool:
        r = role.value if isinstance(role, BusinessRole) else str(role)
        return r in (self.business_roles or [])

    # ── SHIM legado (escrita) ──
    @property
    def type(self) -> int:  # noqa: A003 — nome legado
        v = 0
        roles = self.business_roles or []
        if BusinessRole.ADVERTISER.value in roles:
            v |= OrganizationTypeFlag.IS_ADVERTISER.value
        if BusinessRole.PARTNER.value in roles:
            v |= OrganizationTypeFlag.IS_PARTNER.value
        return v

    @type.setter
    def type(self, value: int) -> None:  # noqa: A003
        self.business_roles = roles_from_legacy_bitmask(int(value or 0))


class AgencyClientStatus(str, enum.Enum):
    ACTIVE = "ACTIVE"
    ENDED = "ENDED"


class AgencyClient(Base):
    """Vínculo agência → organização anunciante cliente (REQ-NEG-020/RBAC-008)."""

    __tablename__ = "agency_clients"
    __mapper_args__ = {"eager_defaults": True}
    __table_args__ = (
        Index("ix_agency_clients_agency_org_id", "agency_org_id"),
        Index("ix_agency_clients_client_org_id", "client_org_id"),
        Index(
            "uq_agency_clients_active_pair",
            "agency_org_id",
            "client_org_id",
            unique=True,
            postgresql_where=text("status = 'ACTIVE'"),
        ),
        CheckConstraint("agency_org_id <> client_org_id", name="distinct_orgs"),
        CheckConstraint("status IN ('ACTIVE','ENDED')", name="status_valid"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    agency_org_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("organizations.id", ondelete="RESTRICT"), nullable=False
    )
    client_org_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("organizations.id", ondelete="RESTRICT"), nullable=False
    )
    status: Mapped[str] = mapped_column(String(10), nullable=False, default=AgencyClientStatus.ACTIVE.value)
    created_by: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())
    ended_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    ended_by: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    end_reason: Mapped[Optional[str]] = mapped_column(Text, nullable=True)


class AgencyClientGrant(Base):
    """Atribuição RBAC (perfil ``agency`` no escopo ORGANIZATION do cliente) criada por um vínculo."""

    __tablename__ = "agency_client_grants"
    __mapper_args__ = {"eager_defaults": True}
    __table_args__ = (
        Index("ix_agency_client_grants_link_id", "agency_client_id"),
        Index("ix_agency_client_grants_user_id", "user_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    agency_client_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("agency_clients.id", ondelete="RESTRICT"), nullable=False
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    role_assignment_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True), ForeignKey("role_assignments.id", ondelete="SET NULL"), nullable=True
    )
    created_by: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())
    revoked_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
