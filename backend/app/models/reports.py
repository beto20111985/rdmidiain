"""
RD Mídia Indoor — Relatórios, exportações e comprovante de veiculação (AÇÃO 002 / W-BIZ).

Requisitos: ETAPA2 #785–#809; P001 §103–§104; P002 item 38.

- ``report_exports``  arquivo gerado (CSV/XLSX/PDF) por um usuário, no escopo dele;
                      guardado no StorageProvider; download só pelo solicitante.
- ``report_runs``     execuções do relatório MENSAL automático (idempotência por
                      tipo × organização × período).
- ``delivery_proofs`` comprovante de veiculação (append-only): contagem SOMENTE de
                      exibições VÁLIDAS (proof-of-play), payload canônico assinado
                      (ES256, chave da instância) + hash/assinatura do PDF final.
"""
from __future__ import annotations

import uuid
from datetime import date, datetime
from typing import Optional

from sqlalchemy import BigInteger, CheckConstraint, Date, DateTime, ForeignKey, Index, Integer, String, Text, \
    UniqueConstraint, func
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base


class ReportExport(Base):
    __tablename__ = "report_exports"
    __table_args__ = (
        CheckConstraint("format IN ('csv','xlsx','pdf')", name="format_valid"),
        CheckConstraint("status IN ('CONCLUIDO','FALHOU')", name="status_valid"),
        Index("ix_report_exports_requested_by", "requested_by", "created_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    report_key: Mapped[str] = mapped_column(String(60), nullable=False)
    params: Mapped[dict] = mapped_column(JSONB, nullable=False)
    format: Mapped[str] = mapped_column(String(5), nullable=False)
    status: Mapped[str] = mapped_column(String(10), nullable=False)
    storage_key: Mapped[Optional[str]] = mapped_column(String(512), nullable=True)
    filename: Mapped[Optional[str]] = mapped_column(String(200), nullable=True)
    content_type: Mapped[Optional[str]] = mapped_column(String(100), nullable=True)
    size_bytes: Mapped[Optional[int]] = mapped_column(BigInteger, nullable=True)
    sha256: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    row_count: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    emailed_to: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    error: Mapped[Optional[str]] = mapped_column(String(500), nullable=True)
    requested_by: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    org_id: Mapped[Optional[uuid.UUID]] = mapped_column(UUID(as_uuid=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())
    expires_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)


class ReportRun(Base):
    __tablename__ = "report_runs"
    __table_args__ = (
        UniqueConstraint("kind", "org_id", "period_start", name="uq_report_runs_kind_org_period"),
        CheckConstraint("kind IN ('MENSAL_ANUNCIANTE','MENSAL_ESTABELECIMENTO')", name="kind_valid"),
        CheckConstraint("status IN ('ENVIADO','SEM_DESTINATARIOS','FALHOU')", name="status_valid"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    kind: Mapped[str] = mapped_column(String(30), nullable=False)
    org_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("organizations.id", ondelete="CASCADE"),
                                              nullable=False)
    period_start: Mapped[date] = mapped_column(Date, nullable=False)
    period_end: Mapped[date] = mapped_column(Date, nullable=False)
    status: Mapped[str] = mapped_column(String(20), nullable=False)
    recipients: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    pdf_sha256: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    detail: Mapped[Optional[str]] = mapped_column(String(500), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())


class DeliveryProof(Base):
    """Comprovante de veiculação (append-only)."""

    __tablename__ = "delivery_proofs"
    __table_args__ = (
        UniqueConstraint("code", name="uq_delivery_proofs_code"),
        UniqueConstraint("pdf_sha256", name="uq_delivery_proofs_pdf_sha256"),
        Index("ix_delivery_proofs_campaign_id", "campaign_id"),
        Index("ix_delivery_proofs_org_id", "org_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    code: Mapped[str] = mapped_column(String(16), nullable=False)
    campaign_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    org_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("organizations.id", ondelete="RESTRICT"),
                                              nullable=False)
    period_start: Mapped[date] = mapped_column(Date, nullable=False)
    period_end: Mapped[date] = mapped_column(Date, nullable=False)
    source: Mapped[str] = mapped_column(String(80), nullable=False)
    total_valid_plays: Mapped[int] = mapped_column(BigInteger, nullable=False)
    payload: Mapped[dict] = mapped_column(JSONB, nullable=False)
    payload_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    payload_signature: Mapped[str] = mapped_column(Text, nullable=False)
    kid: Mapped[str] = mapped_column(String(40), nullable=False)
    pdf_key: Mapped[str] = mapped_column(String(512), nullable=False)
    pdf_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    pdf_signature: Mapped[str] = mapped_column(Text, nullable=False)
    created_by: Mapped[Optional[uuid.UUID]] = mapped_column(UUID(as_uuid=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())
