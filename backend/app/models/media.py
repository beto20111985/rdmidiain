"""
RD Mídia Indoor — Biblioteca de mídia (W-MEDIA, AÇÃO 002).

``MediaFile`` = um arquivo enviado (ou gerado no servidor) para uma organização e a
sua versão normalizada ("rendition") pronta para os players. ``Creative`` vive em
``app.models.campaigns`` (W-CAMP) e referencia ``media_files.id``.

Ciclo de vida (``status``)::

    UPLOADED ──► VALIDATING ──► PROCESSING ──► READY
                    │               │
                    ├──► REJECTED (motivo em status_code/status_message; definitivo)
                    └──► FAILED   (erro transitório; retentável — volta a VALIDATING)

- Valores de tamanho em bytes (BIGINT); durações em milissegundos inteiros (``*_ms``);
  fps em milésimos (``*_fps_milli``: 29970 = 29,97 fps). Nada de float.
- ``source_*`` descreve o arquivo ENVIADO (como chegou); as colunas sem prefixo
  descrevem a SAÍDA normalizada (o que o player baixa): ``storage_key``,
  ``mime_type``, ``size_bytes``, ``sha256``, ``width``, ``height``, ``duration_ms``…
- Chaves de armazenamento são geradas pelo servidor (``app.core.storage.build_key``);
  nenhum caminho local é persistido.
- Exclusão: somente ``deleted_at`` (soft delete) — e só sem referências; o arquivo
  físico é removido depois pelo GC (``purged_at``), que reconsulta as referências.
- Idempotência: (org_id, source_sha256) único entre mídias não excluídas.
"""
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
    SmallInteger,
    String,
    Text,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.database import Base
from app.models.base import BaseModelMixin


class MediaFileStatus(str, enum.Enum):
    """Estado do processamento de um arquivo de mídia."""

    UPLOADED = "UPLOADED"
    VALIDATING = "VALIDATING"
    PROCESSING = "PROCESSING"
    READY = "READY"
    REJECTED = "REJECTED"
    FAILED = "FAILED"


class MediaKind(str, enum.Enum):
    IMAGE = "IMAGE"
    VIDEO = "VIDEO"


class MediaOrientation(str, enum.Enum):
    HORIZONTAL = "HORIZONTAL"  # 1920×1080
    VERTICAL = "VERTICAL"  # 1080×1920


# CreativeStatus/Creative foram movidos para app.models.campaigns (W-CAMP, AÇÃO 002).

_STATUSES = ", ".join(f"'{s.value}'" for s in MediaFileStatus)
_KINDS = ", ".join(f"'{k.value}'" for k in MediaKind)
_ORIENTATIONS = ", ".join(f"'{o.value}'" for o in MediaOrientation)


class MediaFile(BaseModelMixin, Base):
    __tablename__ = "media_files"
    __table_args__ = (
        Index("ix_media_files_org_id_created_at", "org_id", "created_at"),
        Index("ix_media_files_status", "status"),
        Index("ix_media_files_created_by", "created_by"),
        Index("ix_media_files_sha256", "sha256"),
        Index(
            "uq_media_files_org_id_source_sha256",
            "org_id",
            "source_sha256",
            unique=True,
            postgresql_where=text("deleted_at IS NULL"),
        ),
        Index(
            "ix_media_files_pending_purge",
            "deleted_at",
            postgresql_where=text("deleted_at IS NOT NULL AND purged_at IS NULL"),
        ),
        CheckConstraint(f"status IN ({_STATUSES})", name="status_valid"),
        CheckConstraint(f"kind IN ({_KINDS})", name="kind_valid"),
        CheckConstraint(f"orientation IS NULL OR orientation IN ({_ORIENTATIONS})", name="orientation_valid"),
        CheckConstraint("progress_pct BETWEEN 0 AND 100", name="progress_range"),
        CheckConstraint("source_size_bytes > 0", name="source_size_positive"),
        CheckConstraint("size_bytes IS NULL OR size_bytes > 0", name="size_positive"),
        CheckConstraint("status <> 'READY' OR (storage_key IS NOT NULL AND sha256 IS NOT NULL "
                        "AND size_bytes IS NOT NULL AND width IS NOT NULL AND height IS NOT NULL)",
                        name="ready_complete"),
        CheckConstraint("source_sha256 ~ '^[0-9a-f]{64}$'", name="source_sha256_hex"),
        CheckConstraint("sha256 IS NULL OR sha256 ~ '^[0-9a-f]{64}$'", name="sha256_hex"),
    )

    org_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("organizations.id", ondelete="RESTRICT"), nullable=False
    )
    # Nulo quando gerado pelo servidor (ex.: arte aprovada do designer — origin != UPLOAD)
    created_by: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="RESTRICT"), nullable=True
    )
    origin: Mapped[str] = mapped_column(String(30), nullable=False, server_default="UPLOAD")

    # Identificação para humanos (NUNCA usados como caminho)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    original_filename: Mapped[str] = mapped_column(String(255), nullable=False)
    original_extension: Mapped[Optional[str]] = mapped_column(String(16), nullable=True)
    declared_mime: Mapped[Optional[str]] = mapped_column(String(100), nullable=True)
    extension_mismatch: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("false"))

    kind: Mapped[str] = mapped_column(String(10), nullable=False)

    # ── arquivo enviado (fonte) ──
    source_format: Mapped[str] = mapped_column(String(20), nullable=False)  # jpeg|png|gif|mp4|quicktime|webm|matroska|avi|mpegts
    source_mime: Mapped[str] = mapped_column(String(100), nullable=False)
    source_size_bytes: Mapped[int] = mapped_column(BigInteger, nullable=False)
    source_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    source_storage_key: Mapped[Optional[str]] = mapped_column(String(512), nullable=True)
    source_width: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    source_height: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    source_duration_ms: Mapped[Optional[int]] = mapped_column(BigInteger, nullable=True)
    source_codec: Mapped[Optional[str]] = mapped_column(String(32), nullable=True)
    source_fps_milli: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    source_has_audio: Mapped[Optional[bool]] = mapped_column(Boolean, nullable=True)
    source_metadata: Mapped[Optional[dict]] = mapped_column(JSONB, nullable=True)

    # ── processamento ──
    status: Mapped[str] = mapped_column(String(20), nullable=False, server_default=MediaFileStatus.UPLOADED.value)
    status_code: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    status_message: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    progress_pct: Mapped[int] = mapped_column(SmallInteger, nullable=False, server_default="0")
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    lease_until: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    processed_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    ready_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)

    # ── saída normalizada (o que o player baixa) ──
    storage_key: Mapped[Optional[str]] = mapped_column(String(512), nullable=True)
    mime_type: Mapped[Optional[str]] = mapped_column(String(100), nullable=True)
    size_bytes: Mapped[Optional[int]] = mapped_column(BigInteger, nullable=True)
    sha256: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    width: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    height: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    duration_ms: Mapped[Optional[int]] = mapped_column(BigInteger, nullable=True)  # nulo para imagem
    codec: Mapped[Optional[str]] = mapped_column(String(32), nullable=True)
    fps_milli: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    orientation: Mapped[Optional[str]] = mapped_column(String(12), nullable=True)
    animated: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("false"))
    frame_count: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)

    # ── preview (painel) ──
    preview_storage_key: Mapped[Optional[str]] = mapped_column(String(512), nullable=True)
    preview_mime: Mapped[Optional[str]] = mapped_column(String(100), nullable=True)
    preview_size_bytes: Mapped[Optional[int]] = mapped_column(BigInteger, nullable=True)

    # ── exclusão segura ──
    deleted_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    deleted_by: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="RESTRICT"), nullable=True
    )
    purged_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)

    # Relacionamentos
    organization: Mapped["Organization"] = relationship("Organization")  # noqa: F821
    creator: Mapped[Optional["User"]] = relationship("User", foreign_keys=[created_by])  # noqa: F821
    creatives: Mapped[list["Creative"]] = relationship("Creative", back_populates="media_file")  # noqa: F821

    @property
    def is_deleted(self) -> bool:
        return self.deleted_at is not None
