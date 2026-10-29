"""
RD Mídia Indoor — Trilha de auditoria (contrato §4).

Tabela ``audit_logs`` APPEND-ONLY (trigger ``rd_forbid_mutation`` bloqueia UPDATE,
DELETE e TRUNCATE) e encadeada por hash: o trigger ``rd_audit_chain`` atribui
``seq`` (sob advisory lock), ``prev_hash`` (hash da linha anterior) e ``row_hash``
(SHA-256 do conteúdo canônico + prev_hash). Ver ``app/db/ddl.py``.

Escreva SEMPRE via ``app.core.audit.audit(...)``.
"""
from __future__ import annotations

import uuid
from datetime import datetime
from typing import Optional

from sqlalchemy import BigInteger, CheckConstraint, DateTime, FetchedValue, ForeignKey, Index, String, Text, func
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base

AUDIT_ORIGINS = ("api", "worker", "player", "system", "cli")


class AuditLog(Base):
    __tablename__ = "audit_logs"
    __table_args__ = (
        Index("uq_audit_logs_seq", "seq", unique=True),
        Index("ix_audit_logs_created_at", "created_at"),
        Index("ix_audit_logs_entity", "entity_type", "entity_id"),
        Index("ix_audit_logs_actor_user_id", "actor_user_id"),
        Index("ix_audit_logs_action", "action"),
        Index("ix_audit_logs_org_id", "org_id"),
        CheckConstraint("origin IN ('api','worker','player','system','cli')", name="origin_valid"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    # Preenchidos pelo trigger rd_audit_chain (nunca pela aplicação)
    seq: Mapped[int] = mapped_column(BigInteger, nullable=False, server_default=FetchedValue())
    prev_hash: Mapped[str] = mapped_column(String(64), nullable=False, server_default=FetchedValue())
    row_hash: Mapped[str] = mapped_column(String(64), nullable=False, server_default=FetchedValue())

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())
    actor_user_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="RESTRICT"), nullable=True
    )
    # Usuário real quando a ação ocorre durante impersonation
    real_user_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="RESTRICT"), nullable=True
    )
    actor_device_id: Mapped[Optional[uuid.UUID]] = mapped_column(UUID(as_uuid=True), nullable=True)
    org_id: Mapped[Optional[uuid.UUID]] = mapped_column(UUID(as_uuid=True), nullable=True)
    action: Mapped[str] = mapped_column(String(100), nullable=False)
    entity_type: Mapped[Optional[str]] = mapped_column(String(100), nullable=True)
    entity_id: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    before_data: Mapped[Optional[dict]] = mapped_column(JSONB, nullable=True)
    after_data: Mapped[Optional[dict]] = mapped_column(JSONB, nullable=True)
    reason: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    ip_address: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    user_agent: Mapped[Optional[str]] = mapped_column(String(500), nullable=True)
    origin: Mapped[str] = mapped_column(String(20), nullable=False, default="api")
    request_id: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
