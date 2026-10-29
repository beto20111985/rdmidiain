"""
RD Mídia Indoor — RBAC com escopos (contrato §3).

- ``Permission``: código do catálogo (``recurso.ação``), sincronizado de
  ``app.core.permissions.CATALOG``.
- ``Role``: perfil (``slug``). ``is_system=True`` = template padrão sincronizado do
  código; ``False`` = perfil personalizado (roles.manage).
- ``RolePermission``: N×N.
- ``RoleAssignment``: (usuário, perfil, scope_type, scope_id|scope_value).
  ``scope_type ∈ GLOBAL | ORGANIZATION | ESTABLISHMENT | CITY``.
  CITY usa ``scope_value`` (código IBGE ou ``UF:cidade-normalizada``).
"""
from __future__ import annotations

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
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.database import Base


class Permission(Base):
    __tablename__ = "permissions"
    __table_args__ = (
        UniqueConstraint("code", name="uq_permissions_code"),
        Index("ix_permissions_module", "module"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    code: Mapped[str] = mapped_column(String(100), nullable=False)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    description: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    module: Mapped[Optional[str]] = mapped_column(String(100), nullable=True)


class Role(Base):
    __tablename__ = "roles"
    __table_args__ = (
        UniqueConstraint("slug", name="uq_roles_slug"),
        Index("ix_roles_is_system", "is_system"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    slug: Mapped[str] = mapped_column(String(50), nullable=False)
    name: Mapped[str] = mapped_column(String(100), nullable=False)
    description: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    is_system: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, server_default=text("false"))
    mfa_required: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, server_default=text("false"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )

    permissions: Mapped[list["RolePermission"]] = relationship(
        "RolePermission", back_populates="role", cascade="all, delete-orphan", passive_deletes=True
    )


class RolePermission(Base):
    __tablename__ = "role_permissions"
    __table_args__ = (Index("ix_role_permissions_permission_id", "permission_id"),)

    role_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("roles.id", ondelete="CASCADE"), primary_key=True
    )
    permission_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("permissions.id", ondelete="CASCADE"), primary_key=True
    )

    role: Mapped["Role"] = relationship("Role", back_populates="permissions")
    permission: Mapped["Permission"] = relationship("Permission")


class RoleAssignment(Base):
    __tablename__ = "role_assignments"
    __table_args__ = (
        UniqueConstraint(
            "user_id",
            "role_id",
            "scope_type",
            "scope_id",
            "scope_value",
            name="uq_role_assignments_user_role_scope",
            postgresql_nulls_not_distinct=True,
        ),
        Index("ix_role_assignments_user_id", "user_id"),
        Index("ix_role_assignments_scope", "scope_type", "scope_id"),
        CheckConstraint(
            "scope_type IN ('GLOBAL','ORGANIZATION','ESTABLISHMENT','CITY')", name="scope_type_valid"
        ),
        CheckConstraint(
            "(scope_type = 'GLOBAL' AND scope_id IS NULL AND scope_value IS NULL)"
            " OR (scope_type IN ('ORGANIZATION','ESTABLISHMENT') AND scope_id IS NOT NULL AND scope_value IS NULL)"
            " OR (scope_type = 'CITY' AND scope_id IS NULL AND scope_value IS NOT NULL)",
            name="scope_consistent",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    role_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("roles.id", ondelete="RESTRICT"), nullable=False
    )
    scope_type: Mapped[str] = mapped_column(String(20), nullable=False)
    scope_id: Mapped[Optional[uuid.UUID]] = mapped_column(UUID(as_uuid=True), nullable=True)
    scope_value: Mapped[Optional[str]] = mapped_column(String(120), nullable=True)
    granted_by: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())
    expires_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)

    role: Mapped["Role"] = relationship("Role")
