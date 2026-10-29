"""
RD Mídia Indoor — Versões de manifesto RDPP/1 por tela (gerador ÚNICO — W-INV).

Substitui ``player_manifests`` (lista plana; 3 geradores divergentes — A6 E18–E20).
- ``version`` monotônica por tela, obtida sob o lock de ``screen_inventory_state``
  (``manifest_version = manifest_version + 1``).
- ``content_gz``: bytes RDJ1 EXATOS do objeto ``manifest`` (gzip, mtime=0) — a
  assinatura ES256 é verificável a qualquer momento; pode ser podado (NULL) em
  versões antigas, preservando hashes e as listas de criativos/campanhas/mídias
  (validação de PoP por versão e referência de mídia para rollback).
- ``content_sha256`` = SHA-256 dos bytes RDJ1; ``plan_sha256`` = SHA-256 do RDJ1 sem
  campos voláteis (``manifest_id``, ``version``, ``generated_at_ms``) — base de
  "nada mudou ⇒ não gera versão" e do teste de determinismo.
"""
from __future__ import annotations

import uuid
from datetime import datetime
from typing import Optional

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    LargeBinary,
    PrimaryKeyConstraint,
    String,
    Text,
    func,
)
from sqlalchemy.dialects.postgresql import ARRAY, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base


class ManifestVersion(Base):
    __tablename__ = "manifest_versions"
    __table_args__ = (
        PrimaryKeyConstraint("screen_id", "version", name="pk_manifest_versions"),
        CheckConstraint("version > 0", name="version_positive"),
        CheckConstraint("char_length(content_sha256) = 64 AND char_length(plan_sha256) = 64", name="sha_len"),
        Index("ix_manifest_versions_manifest_id", "manifest_id", unique=True),
        Index("ix_manifest_versions_screen_generated", "screen_id", "generated_at"),
    )

    screen_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("screens.id", ondelete="RESTRICT"), nullable=False
    )
    version: Mapped[int] = mapped_column(BigInteger, nullable=False)
    manifest_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    content_gz: Mapped[Optional[bytes]] = mapped_column(LargeBinary, nullable=True)
    content_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    plan_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    size_bytes: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    signature: Mapped[str] = mapped_column(Text, nullable=False)
    key_id: Mapped[str] = mapped_column(String(64), nullable=False)
    reason: Mapped[str] = mapped_column(String(64), nullable=False, default="on_demand")
    generated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())
    horizon_from: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    horizon_to: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    media_ids: Mapped[list] = mapped_column(ARRAY(UUID(as_uuid=True)), nullable=False, default=list)
    creative_ids: Mapped[list] = mapped_column(ARRAY(UUID(as_uuid=True)), nullable=False, default=list)
    campaign_ids: Mapped[list] = mapped_column(ARRAY(UUID(as_uuid=True)), nullable=False, default=list)
    unplaced_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    applied_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    applied_by_device: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True), ForeignKey("devices.id", ondelete="SET NULL"), nullable=True
    )
