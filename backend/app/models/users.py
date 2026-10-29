"""
RD Mídia Indoor — Usuários, sessões e artefatos de autenticação.

User, UserSession (sessão = família de refresh tokens), RefreshToken (opaco, rotação
+ detecção de reuso), UserRecoveryCode (2FA), PasswordResetCode (código de 6 dígitos),
UserInvite (convite com perfil/escopo).

A trilha de auditoria fica em ``app.models.audit.AuditLog`` (append-only).
"""
from __future__ import annotations

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
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.database import Base
from app.models.base import BaseModelMixin

SCOPE_TYPES_SQL = "('GLOBAL','ORGANIZATION','ESTABLISHMENT','CITY')"


class User(BaseModelMixin, Base):
    """Usuário humano. Pode ter várias atribuições (perfil × escopo)."""

    __tablename__ = "users"
    __table_args__ = (
        Index("ix_users_email", "email", unique=True),
        Index("ix_users_is_active", "is_active"),
        Index("uq_users_phone", "phone", unique=True, postgresql_where=text("phone IS NOT NULL")),
        UniqueConstraint("admin_master_seat", name="uq_users_admin_master_seat"),
        CheckConstraint("admin_master_seat IN (1, 2)", name="admin_master_seat_valid"),
        CheckConstraint("theme IN ('light', 'dark', 'system')", name="theme_valid"),
    )

    email: Mapped[str] = mapped_column(String(255), nullable=False)
    hashed_password: Mapped[str] = mapped_column(String(255), nullable=False)
    full_name: Mapped[str] = mapped_column(String(255), nullable=False)
    # Telefone normalizado (somente dígitos, com DDI; ex.: 5562999998888)
    phone: Mapped[Optional[str]] = mapped_column(String(30), nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True, server_default=text("true"))
    is_verified: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, server_default=text("false"))
    must_change_password: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=text("false")
    )
    password_changed_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)

    # TOTP — segredos cifrados (AES-GCM, app.core.crypto); nunca em claro.
    totp_enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, server_default=text("false"))
    totp_secret_enc: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    totp_pending_secret_enc: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    totp_last_step: Mapped[Optional[int]] = mapped_column(BigInteger, nullable=True)
    totp_enabled_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)

    # Lockout por conta (persistido em transação separada)
    failed_login_attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default=text("0"))
    locked_until: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    last_login: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)

    # Admin Master: no máximo dois assentos (1 e 2). AÇÃO 003 (ADR_011): atribuídos SOMENTE pelas
    # funções SECURITY DEFINER do banco (bootstrap, convite do 2º Master, CLI restrita); o papel de
    # runtime não altera assento/ativação/e-mail/TOTP/senha financeira de titular (trigger).
    admin_master_seat: Mapped[Optional[int]] = mapped_column(SmallInteger, nullable=True)
    # Recuperação controlada (``recover-master``): bloqueio financeiro temporário a partir daqui.
    master_recovered_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    # AÇÃO 003 / 0009 (D1): selo HMAC (chave da aplicação) das credenciais do titular de assento — calculado e
    # CONFERIDO pela aplicação; o banco só exige que mude junto com hash/fatores (rastro) e nunca volte a NULL.
    master_credential_seal: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    theme: Mapped[str] = mapped_column(String(10), nullable=False, default="system", server_default=text("'system'"))

    # Senha financeira (P001 §88) — lógica implementada pelo agente financeiro.
    financial_password_hash: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    financial_password_changed_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)

    # LGPD: titular anonimizado (dados pessoais removidos; registros legais mantidos)
    anonymized_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)

    sessions: Mapped[list["UserSession"]] = relationship(
        "UserSession",
        back_populates="user",
        foreign_keys="UserSession.user_id",
        cascade="all, delete-orphan",
        passive_deletes=True,
    )

    @property
    def is_admin_master(self) -> bool:
        return self.admin_master_seat in (1, 2)


class UserSession(Base):
    """Sessão autenticada (= família de refresh tokens). Access tokens levam ``sid``."""

    __tablename__ = "user_sessions"
    __table_args__ = (
        Index("ix_user_sessions_user_id", "user_id"),
        Index("ix_user_sessions_expires_at", "expires_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())
    last_seen_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    revoked_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    revoked_reason: Mapped[Optional[str]] = mapped_column(String(50), nullable=True)
    ip_address: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    user_agent: Mapped[Optional[str]] = mapped_column(String(500), nullable=True)
    device_label: Mapped[Optional[str]] = mapped_column(String(120), nullable=True)
    amr: Mapped[list] = mapped_column(JSONB, nullable=False, default=list, server_default=text("'[]'::jsonb"))
    mfa_verified_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    # Impersonation: sessão do usuário-alvo criada por outro usuário
    impersonator_user_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="RESTRICT"), nullable=True
    )
    impersonation_reason: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    parent_session_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True), ForeignKey("user_sessions.id", ondelete="SET NULL"), nullable=True
    )

    user: Mapped["User"] = relationship("User", back_populates="sessions", foreign_keys=[user_id])

    @property
    def is_impersonation(self) -> bool:
        return self.impersonator_user_id is not None


class RefreshToken(Base):
    """Refresh token opaco (somente hash SHA-256). Usado uma única vez (rotação)."""

    __tablename__ = "refresh_tokens"
    __table_args__ = (
        UniqueConstraint("token_hash", name="uq_refresh_tokens_token_hash"),
        Index("ix_refresh_tokens_session_id", "session_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    session_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("user_sessions.id", ondelete="CASCADE"), nullable=False
    )
    token_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    used_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    replaced_by_id: Mapped[Optional[uuid.UUID]] = mapped_column(UUID(as_uuid=True), nullable=True)


class UserRecoveryCode(Base):
    """Código de recuperação do 2FA (HMAC; uso único)."""

    __tablename__ = "user_recovery_codes"
    __table_args__ = (
        UniqueConstraint("user_id", "code_hash", name="uq_user_recovery_codes_user_code"),
        Index("ix_user_recovery_codes_user_id", "user_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    code_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())
    used_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)


class PasswordResetCode(Base):
    """Código de 6 dígitos por e-mail (HMAC; 15 min; uso único; tentativas limitadas)."""

    __tablename__ = "password_reset_codes"
    __table_args__ = (Index("ix_password_reset_codes_user_id", "user_id"),)

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    code_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    used_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default=text("0"))
    request_ip: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)


class UserInvite(Base):
    """Convite por e-mail com perfil e escopo. Token guardado só como hash."""

    __tablename__ = "user_invites"
    __table_args__ = (
        UniqueConstraint("token_hash", name="uq_user_invites_token_hash"),
        Index("ix_user_invites_email", "email"),
        CheckConstraint(f"scope_type IN {SCOPE_TYPES_SQL}", name="scope_type_valid"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    email: Mapped[str] = mapped_column(String(255), nullable=False)
    token_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    invited_by: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="RESTRICT"), nullable=False
    )
    role_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("roles.id", ondelete="RESTRICT"), nullable=False
    )
    scope_type: Mapped[str] = mapped_column(String(20), nullable=False)
    scope_id: Mapped[Optional[uuid.UUID]] = mapped_column(UUID(as_uuid=True), nullable=True)
    scope_value: Mapped[Optional[str]] = mapped_column(String(120), nullable=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    accepted_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    accepted_user_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="RESTRICT"), nullable=True
    )
    revoked_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())
