"""
RD Mídia Indoor — Editor inicial de templates (AÇÃO 002 / W-BIZ).

Requisitos: ETAPA2 #388–#389; RESPOSTAS RODRIGO #389 (editor já na 1ª versão);
P001 §109 (seguro, útil, evolutivo — sem editor gráfico impossível de manter).

Modelo JSON simples e validado (``app/modules/templates/spec.py``): fundo de cor sólida,
camadas de TEXTO (fontes permitidas, variáveis ``{{nome}}``), IMAGEM (asset enviado
à biblioteca de templates) e RETÂNGULO, posicionadas numa GRADE 24×24 sobre telas
1920×1080 (horizontal) ou 1080×1920 (vertical). Renderização SOMENTE no servidor
(Pillow) para PNG/JPG; nenhum HTML/JS é aceito ou executado.
"""
from __future__ import annotations

import uuid
from datetime import datetime
from typing import Optional

from sqlalchemy import BigInteger, CheckConstraint, DateTime, ForeignKey, Index, Integer, String, Text, func
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base


class DesignTemplate(Base):
    __tablename__ = "design_templates"
    __mapper_args__ = {"eager_defaults": True}
    __table_args__ = (
        CheckConstraint("orientation IN ('HORIZONTAL','VERTICAL')", name="orientation_valid"),
        CheckConstraint("status IN ('RASCUNHO','ATIVO','ARQUIVADO')", name="status_valid"),
        CheckConstraint("(orientation = 'HORIZONTAL' AND width = 1920 AND height = 1080) OR "
                        "(orientation = 'VERTICAL' AND width = 1080 AND height = 1920)", name="canvas_fhd"),
        Index("ix_design_templates_org_id", "org_id"),
        Index("ix_design_templates_status", "status"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    org_id: Mapped[Optional[uuid.UUID]] = mapped_column(  # NULL = template global da RD
        UUID(as_uuid=True), ForeignKey("organizations.id", ondelete="RESTRICT"), nullable=True
    )
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    description: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    orientation: Mapped[str] = mapped_column(String(12), nullable=False)
    width: Mapped[int] = mapped_column(Integer, nullable=False)
    height: Mapped[int] = mapped_column(Integer, nullable=False)
    spec: Mapped[dict] = mapped_column(JSONB, nullable=False)
    status: Mapped[str] = mapped_column(String(12), nullable=False, default="RASCUNHO")
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    created_by: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    updated_by: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now(),
                                                 onupdate=func.now())


class TemplateAsset(Base):
    __tablename__ = "template_assets"
    __table_args__ = (Index("ix_template_assets_org_id", "org_id"),)

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    org_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True), ForeignKey("organizations.id", ondelete="RESTRICT"), nullable=True
    )
    storage_key: Mapped[str] = mapped_column(String(512), nullable=False)
    filename: Mapped[str] = mapped_column(String(255), nullable=False)
    content_type: Mapped[str] = mapped_column(String(50), nullable=False)
    width: Mapped[int] = mapped_column(Integer, nullable=False)
    height: Mapped[int] = mapped_column(Integer, nullable=False)
    size_bytes: Mapped[int] = mapped_column(BigInteger, nullable=False)
    sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    uploaded_by: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())


class TemplateRender(Base):
    __tablename__ = "template_renders"
    __table_args__ = (
        CheckConstraint("format IN ('PNG','JPEG')", name="format_valid"),
        Index("ix_template_renders_template_id", "template_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    template_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("design_templates.id", ondelete="RESTRICT"), nullable=False
    )
    template_version: Mapped[int] = mapped_column(Integer, nullable=False)
    org_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True), ForeignKey("organizations.id", ondelete="RESTRICT"), nullable=True
    )
    format: Mapped[str] = mapped_column(String(5), nullable=False)
    variables: Mapped[Optional[dict]] = mapped_column(JSONB, nullable=True)
    storage_key: Mapped[str] = mapped_column(String(512), nullable=False)
    sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    size_bytes: Mapped[int] = mapped_column(BigInteger, nullable=False)
    width: Mapped[int] = mapped_column(Integer, nullable=False)
    height: Mapped[int] = mapped_column(Integer, nullable=False)
    media_id: Mapped[Optional[uuid.UUID]] = mapped_column(UUID(as_uuid=True), nullable=True)
    created_by: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())
