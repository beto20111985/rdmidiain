"""
RD Mídia Indoor — Conteúdo interno, fallback e emergência (P001 §24, §34–§35; ETAPA2
#1052–#1075, #746–#748, #1401). W-INV.

- ``internal_content``: lista ORDENADA por estabelecimento (``screen_id`` NULL = todas
  as telas) ou por tela (sobrepõe a do estabelecimento), SEM datas/horários
  (#1053–#1056). Gerenciada pela RD (``playlists.manage``); o estabelecimento só
  reordena com ``playlists.reorder``. Preenche o bloco interno e as sobras do bloco
  externo; não é cobrada.
- ``fallback_content``: escopo GLOBAL (conteúdo padrão/divulgação RD) e
  ESTABELECIMENTO (logo do estabelecimento) — usado quando falta conteúdo/validade.
- ``emergency_broadcasts``: comunicado urgente com janela e alvo (todas as telas,
  telas, estabelecimentos, UFs, cidades) — ÚNICO conteúdo que preempta (#1401);
  criação/cancelamento auditados (``campaigns.kill``).
"""
from __future__ import annotations

import uuid
from datetime import datetime
from typing import Optional

from sqlalchemy import Boolean, CheckConstraint, DateTime, ForeignKey, Index, Integer, String, Text, func
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base

FALLBACK_PURPOSES = ("ESTABLISHMENT_LOGO", "RD_DEFAULT", "RD_PROMO", "INSTITUTIONAL_MESSAGE")
EMERGENCY_TARGETS = ("ALL", "SCREENS", "ESTABLISHMENTS", "STATES", "CITIES")


def _in(values) -> str:
    return "(" + ", ".join(f"'{v}'" for v in values) + ")"


class InternalContent(Base):
    __tablename__ = "internal_content"
    __table_args__ = (
        CheckConstraint("position >= 0", name="position"),
        Index("ix_internal_content_scope", "establishment_id", "screen_id", "position"),
        Index("ix_internal_content_creative", "creative_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    establishment_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("establishments.id", ondelete="RESTRICT"), nullable=False
    )
    screen_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True), ForeignKey("screens.id", ondelete="RESTRICT"), nullable=True
    )
    creative_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("creatives.id", ondelete="RESTRICT"), nullable=False
    )
    position: Mapped[int] = mapped_column(Integer, nullable=False)
    active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True, server_default="true")
    created_by: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="RESTRICT"), nullable=True
    )
    updated_by: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="RESTRICT"), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now(),
                                                 onupdate=func.now())


class FallbackContent(Base):
    __tablename__ = "fallback_content"
    __table_args__ = (
        CheckConstraint("scope IN ('GLOBAL','ESTABLISHMENT')", name="scope"),
        CheckConstraint("(scope = 'GLOBAL') = (establishment_id IS NULL)", name="scope_matches_establishment"),
        CheckConstraint(f"purpose IN {_in(FALLBACK_PURPOSES)}", name="purpose"),
        CheckConstraint("position >= 0", name="position"),
        Index("ix_fallback_content_scope", "scope", "establishment_id", "position"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    scope: Mapped[str] = mapped_column(String(16), nullable=False)
    establishment_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True), ForeignKey("establishments.id", ondelete="RESTRICT"), nullable=True
    )
    creative_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("creatives.id", ondelete="RESTRICT"), nullable=False
    )
    purpose: Mapped[str] = mapped_column(String(32), nullable=False, default="RD_DEFAULT")
    position: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True, server_default="true")
    created_by: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="RESTRICT"), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now(),
                                                 onupdate=func.now())


class EmergencyBroadcast(Base):
    __tablename__ = "emergency_broadcasts"
    __table_args__ = (
        CheckConstraint("ends_at > starts_at", name="window"),
        CheckConstraint(f"target_kind IN {_in(EMERGENCY_TARGETS)}", name="target_kind"),
        CheckConstraint("target_kind = 'ALL' OR jsonb_array_length(target_ids) > 0", name="target_ids"),
        CheckConstraint("status IN ('ACTIVE','CANCELLED')", name="status"),
        CheckConstraint("length(btrim(reason)) >= 10", name="reason_required"),
        Index("ix_emergency_broadcasts_window", "status", "starts_at", "ends_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    title: Mapped[str] = mapped_column(String(200), nullable=False)
    creative_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("creatives.id", ondelete="RESTRICT"), nullable=False
    )
    starts_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    ends_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    target_kind: Mapped[str] = mapped_column(String(16), nullable=False)
    target_ids: Mapped[list] = mapped_column(JSONB, nullable=False, default=list)
    status: Mapped[str] = mapped_column(String(12), nullable=False, default="ACTIVE", server_default="ACTIVE")
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    created_by: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="RESTRICT"), nullable=False
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())
    cancelled_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    cancelled_by: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="RESTRICT"), nullable=True
    )
    cancel_reason: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
