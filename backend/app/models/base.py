"""
RD Mídia Indoor — Base Mixin para Modelos SQLAlchemy
Campos comuns: id (UUID4), created_at, updated_at, deleted_at
"""
import uuid
from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import DateTime, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column


def utcnow() -> datetime:
    """Retorna datetime atual em UTC."""
    return datetime.now(timezone.utc)


class UUIDMixin:
    """Mixin com UUID como PK principal."""

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
        nullable=False,
    )


class TimestampMixin:
    """Mixin com campos de auditoria de tempo."""

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
    )


class SoftDeleteMixin:
    """Mixin para soft delete — registros nunca são deletados fisicamente."""

    deleted_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
        default=None,
    )

    @property
    def is_deleted(self) -> bool:
        return self.deleted_at is not None


class BaseModelMixin(UUIDMixin, TimestampMixin):
    """Mixin base com UUID + timestamps. Herde para modelos completos."""
    pass


class FullBaseModelMixin(UUIDMixin, TimestampMixin, SoftDeleteMixin):
    """Mixin base completo: UUID + timestamps + soft delete."""
    pass
