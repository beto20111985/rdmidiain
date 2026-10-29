"""
RD Mídia Indoor — LGPD básico (P001 §97).

- ``ConsentRecord``: aceite/revogação de termos/privacidade/cookies com versão, IP,
  user-agent e data. APPEND-ONLY (prova de consentimento; retenção por base legal).
- ``DataDeletionRequest``: pedido de exclusão do titular; processado por quem tem
  ``lgpd.manage`` → ANONIMIZA dados pessoais preservando registros financeiros e de
  auditoria obrigatórios.
"""
from __future__ import annotations

import uuid
from datetime import datetime
from typing import Optional

from sqlalchemy import CheckConstraint, DateTime, ForeignKey, Index, String, Text, func, text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base

CONSENT_DOCUMENTS = ("terms", "privacy", "cookies", "marketing")


class ConsentRecord(Base):
    __tablename__ = "consent_records"
    __table_args__ = (
        Index("ix_consent_records_user_id", "user_id", "document"),
        CheckConstraint("document IN ('terms','privacy','cookies','marketing')", name="document_valid"),
        CheckConstraint("action IN ('accepted','revoked')", name="action_valid"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="RESTRICT"), nullable=False
    )
    document: Mapped[str] = mapped_column(String(20), nullable=False)
    version: Mapped[str] = mapped_column(String(40), nullable=False)
    action: Mapped[str] = mapped_column(String(10), nullable=False, default="accepted")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())
    ip_address: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    user_agent: Mapped[Optional[str]] = mapped_column(String(500), nullable=True)


class DataDeletionRequest(Base):
    __tablename__ = "data_deletion_requests"
    __table_args__ = (
        Index("ix_data_deletion_requests_status", "status"),
        Index(
            "uq_data_deletion_requests_pending_user",
            "user_id",
            unique=True,
            postgresql_where=text("status = 'PENDING'"),
        ),
        CheckConstraint("status IN ('PENDING','COMPLETED','REJECTED','CANCELLED')", name="status_valid"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="RESTRICT"), nullable=False
    )
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="PENDING", server_default=text("'PENDING'"))
    reason: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    requested_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())
    processed_by: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="RESTRICT"), nullable=True
    )
    processed_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    resolution_notes: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
