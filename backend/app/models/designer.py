"""
RD Mídia Indoor — Produção de arte / Designer (AÇÃO 002 / W-BIZ).

Requisitos: ETAPA2 #369–#389, #743, #1425–#1433; RESPOSTAS RODRIGO §14; P001 §108;
P002 item 34.

Fluxo (status em PT): BRIEFING (aguardando orçamento da RD) → AGUARDANDO_PAGAMENTO →
NA_FILA → EM_DESIGN → EM_REVISAO (cliente revisa) ⇄ ALTERACAO_SOLICITADA → APROVADA →
PUBLICADA; CANCELADA. 3 revisões incluídas (configurável); a 4ª exige revisão
extra aprovada/cobrada.

Imutabilidade (DDL extra em ``app/modules/designer/ddl.py``):
- ``design_versions``: conteúdo (arquivo/sha256/número) nunca muda; versão APROVADA
  (``locked``) não pode ser alterada nem apagada — mudança = nova versão + nova aprovação.
- ``design_approvals`` e ``design_events``: append-only (aprovação formal com data/hora,
  IP, user-agent, usuário e sha256 da versão aprovada).
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
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base
from app.models.base import BaseModelMixin


class DesignStatus(str, enum.Enum):
    BRIEFING = "BRIEFING"
    AGUARDANDO_PAGAMENTO = "AGUARDANDO_PAGAMENTO"
    NA_FILA = "NA_FILA"
    EM_DESIGN = "EM_DESIGN"
    EM_REVISAO = "EM_REVISAO"
    ALTERACAO_SOLICITADA = "ALTERACAO_SOLICITADA"
    APROVADA = "APROVADA"
    PUBLICADA = "PUBLICADA"
    CANCELADA = "CANCELADA"


class DesignVersionStatus(str, enum.Enum):
    EM_REVISAO = "EM_REVISAO"
    ALTERACAO_SOLICITADA = "ALTERACAO_SOLICITADA"
    APROVADA = "APROVADA"
    SUBSTITUIDA = "SUBSTITUIDA"


def _in(col: str, values) -> str:
    return f"{col} IN (" + ",".join(f"'{v}'" for v in values) + ")"


class DesignRequest(BaseModelMixin, Base):
    __mapper_args__ = {"eager_defaults": True}  # onupdate server-side sem lazy-load (async)
    __tablename__ = "design_requests"
    __table_args__ = (
        UniqueConstraint("code", name="uq_design_requests_code"),
        CheckConstraint(_in("status", [s.value for s in DesignStatus]), name="status_valid"),
        CheckConstraint(_in("kind", ["ANUNCIO_EXTERNO", "CONTEUDO_INTERNO"]), name="kind_valid"),
        CheckConstraint(_in("orientation", ["HORIZONTAL", "VERTICAL"]), name="orientation_valid"),
        CheckConstraint(_in("media_type", ["IMAGEM", "VIDEO"]), name="media_type_valid"),
        CheckConstraint(_in("billing", ["A_DEFINIR", "COBRAR", "NEGOCIADO", "ISENTO"]), name="billing_valid"),
        CheckConstraint(_in("payment_status", ["PENDENTE", "PAGO", "NAO_APLICAVEL"]), name="payment_status_valid"),
        CheckConstraint("price_cents >= 0", name="price_non_negative"),
        CheckConstraint("revisions_used >= 0 AND revisions_included >= 0 AND extra_revisions >= 0",
                        name="revisions_non_negative"),
        CheckConstraint("revisions_used <= revisions_included + extra_revisions", name="revisions_within_limit"),
        Index("ix_design_requests_org_id", "org_id"),
        Index("ix_design_requests_establishment_id", "establishment_id"),
        Index("ix_design_requests_status", "status"),
        Index("ix_design_requests_assigned_to", "assigned_to"),
    )

    code: Mapped[str] = mapped_column(String(12), nullable=False)
    org_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("organizations.id", ondelete="RESTRICT"), nullable=False)
    establishment_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True), ForeignKey("establishments.id", ondelete="SET NULL"), nullable=True
    )
    kind: Mapped[str] = mapped_column(String(20), nullable=False, default="ANUNCIO_EXTERNO")
    title: Mapped[str] = mapped_column(String(200), nullable=False)
    briefing: Mapped[str] = mapped_column(Text, nullable=False)
    texts: Mapped[Optional[list]] = mapped_column(JSONB, nullable=True)
    references: Mapped[Optional[list]] = mapped_column(JSONB, nullable=True)  # links/descrições de referência
    orientation: Mapped[str] = mapped_column(String(12), nullable=False, default="HORIZONTAL")
    media_type: Mapped[str] = mapped_column(String(8), nullable=False, default="IMAGEM")
    duration_s: Mapped[int] = mapped_column(Integer, nullable=False, default=30)
    status: Mapped[str] = mapped_column(String(25), nullable=False, default=DesignStatus.BRIEFING.value)
    requested_by: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("users.id", ondelete="RESTRICT"), nullable=False)
    assigned_to: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    billing: Mapped[str] = mapped_column(String(12), nullable=False, default="A_DEFINIR")
    price_cents: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    payment_status: Mapped[str] = mapped_column(String(15), nullable=False, default="NAO_APLICAVEL")
    payment_order_id: Mapped[Optional[uuid.UUID]] = mapped_column(UUID(as_uuid=True), nullable=True)
    revisions_included: Mapped[int] = mapped_column(Integer, nullable=False, default=3)
    extra_revisions: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    revisions_used: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    approved_version_id: Mapped[Optional[uuid.UUID]] = mapped_column(UUID(as_uuid=True), nullable=True)
    approved_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    published_media_id: Mapped[Optional[uuid.UUID]] = mapped_column(UUID(as_uuid=True), nullable=True)
    published_version_id: Mapped[Optional[uuid.UUID]] = mapped_column(UUID(as_uuid=True), nullable=True)
    published_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    due_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    cancel_reason: Mapped[Optional[str]] = mapped_column(String(500), nullable=True)
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)


class DesignAttachment(Base):
    __tablename__ = "design_attachments"
    __table_args__ = (
        CheckConstraint(_in("kind", ["LOGOTIPO", "IMAGEM", "VIDEO", "REFERENCIA", "TEXTO", "OUTRO"]), name="kind_valid"),
        Index("ix_design_attachments_request_id", "request_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    request_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("design_requests.id", ondelete="CASCADE"), nullable=False)
    kind: Mapped[str] = mapped_column(String(12), nullable=False)
    storage_key: Mapped[str] = mapped_column(String(512), nullable=False)
    filename: Mapped[str] = mapped_column(String(255), nullable=False)
    content_type: Mapped[str] = mapped_column(String(100), nullable=False)
    size_bytes: Mapped[int] = mapped_column(BigInteger, nullable=False)
    sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    uploaded_by: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("users.id", ondelete="RESTRICT"), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())


class DesignVersion(Base):
    __tablename__ = "design_versions"
    __table_args__ = (
        UniqueConstraint("request_id", "number", name="uq_design_versions_request_number"),
        CheckConstraint(_in("status", [s.value for s in DesignVersionStatus]), name="status_valid"),
        CheckConstraint(_in("source", ["UPLOAD", "TEMPLATE"]), name="source_valid"),
        CheckConstraint("NOT locked OR status = 'APROVADA'", name="locked_only_when_approved"),
        Index("ix_design_versions_request_id", "request_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    request_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("design_requests.id", ondelete="RESTRICT"), nullable=False)
    number: Mapped[int] = mapped_column(Integer, nullable=False)
    source: Mapped[str] = mapped_column(String(10), nullable=False, default="UPLOAD")
    template_id: Mapped[Optional[uuid.UUID]] = mapped_column(UUID(as_uuid=True), nullable=True)
    storage_key: Mapped[str] = mapped_column(String(512), nullable=False)
    filename: Mapped[str] = mapped_column(String(255), nullable=False)
    content_type: Mapped[str] = mapped_column(String(100), nullable=False)
    size_bytes: Mapped[int] = mapped_column(BigInteger, nullable=False)
    sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    width: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    height: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    notes: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    status: Mapped[str] = mapped_column(String(25), nullable=False, default=DesignVersionStatus.EM_REVISAO.value)
    locked: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, server_default=text("false"))
    created_by: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("users.id", ondelete="RESTRICT"), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())


class DesignApproval(Base):
    """Decisão FORMAL do cliente sobre uma versão (append-only)."""

    __tablename__ = "design_approvals"
    __table_args__ = (
        CheckConstraint(_in("decision", ["APROVADA", "ALTERACAO_SOLICITADA"]), name="decision_valid"),
        Index("ix_design_approvals_request_id", "request_id"),
        Index("uq_design_approvals_version_approved", "version_id", unique=True,
              postgresql_where=text("decision = 'APROVADA'")),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    request_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("design_requests.id", ondelete="RESTRICT"), nullable=False)
    version_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("design_versions.id", ondelete="RESTRICT"), nullable=False)
    decision: Mapped[str] = mapped_column(String(25), nullable=False)
    comment: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    user_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("users.id", ondelete="RESTRICT"), nullable=False)
    real_user_id: Mapped[Optional[uuid.UUID]] = mapped_column(UUID(as_uuid=True), nullable=True)  # impersonation
    ip_address: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    user_agent: Mapped[Optional[str]] = mapped_column(String(500), nullable=True)
    version_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    revision_number: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)  # nº da revisão consumida (alteração)
    decided_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())


class DesignComment(Base):
    __tablename__ = "design_comments"
    __table_args__ = (Index("ix_design_comments_request_id", "request_id"),)

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    request_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("design_requests.id", ondelete="CASCADE"), nullable=False)
    version_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True), ForeignKey("design_versions.id", ondelete="SET NULL"), nullable=True
    )
    author_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("users.id", ondelete="RESTRICT"), nullable=False)
    body: Mapped[str] = mapped_column(Text, nullable=False)
    is_internal: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())


class DesignEvent(Base):
    """Histórico de status do pedido (append-only)."""

    __tablename__ = "design_events"
    __table_args__ = (Index("ix_design_events_request_id", "request_id"),)

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    request_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("design_requests.id", ondelete="RESTRICT"), nullable=False)
    actor_id: Mapped[Optional[uuid.UUID]] = mapped_column(UUID(as_uuid=True), nullable=True)
    from_status: Mapped[Optional[str]] = mapped_column(String(25), nullable=True)
    to_status: Mapped[str] = mapped_column(String(25), nullable=False)
    note: Mapped[Optional[str]] = mapped_column(String(1000), nullable=True)
    data: Mapped[Optional[dict]] = mapped_column(JSONB, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())


class DesignExtraRevision(Base):
    """Revisão além das incluídas: precisa de aprovação da RD e, se cobrada, pagamento."""

    __tablename__ = "design_extra_revisions"
    __table_args__ = (
        CheckConstraint(_in("status", ["SOLICITADA", "AGUARDANDO_PAGAMENTO", "LIBERADA", "RECUSADA", "CANCELADA"]),
                        name="status_valid"),
        CheckConstraint("price_cents >= 0", name="price_non_negative"),
        Index("ix_design_extra_revisions_request_id", "request_id"),
        Index("uq_design_extra_revisions_open", "request_id", unique=True,
              postgresql_where=text("status IN ('SOLICITADA','AGUARDANDO_PAGAMENTO')")),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    request_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("design_requests.id", ondelete="RESTRICT"), nullable=False)
    status: Mapped[str] = mapped_column(String(25), nullable=False, default="SOLICITADA")
    price_cents: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    requested_by: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("users.id", ondelete="RESTRICT"), nullable=False)
    reason: Mapped[Optional[str]] = mapped_column(String(1000), nullable=True)
    decided_by: Mapped[Optional[uuid.UUID]] = mapped_column(UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    decided_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    decision_note: Mapped[Optional[str]] = mapped_column(String(1000), nullable=True)
    payment_order_id: Mapped[Optional[uuid.UUID]] = mapped_column(UUID(as_uuid=True), nullable=True)
    released_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())
