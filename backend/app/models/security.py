"""
RD Mídia Indoor — Segurança da instância (AÇÃO 003 / ADR_011; SPEC §2.1).

- ``admin_master_invites``            convite de uso único do 2º Admin Master (token só como
                                       SHA-256; um convite PENDENTE por assento — índice único
                                       parcial + trigger ``rd_admin_master_invite_guard``).
- ``admin_master_seat_events``        trilha APPEND-ONLY e SELADA (HMAC da aplicação, cadeia
                                       ``prev_seal`` → ``app_seal``) das atribuições/recuperações
                                       de assento. Gravável só pelas funções ``SECURITY DEFINER``
                                       ``rd_security_assign_master_seat``/``rd_security_recover_master``.
- ``instance_approval_mode_history``  trilha APPEND-ONLY e SELADA do modo de aprovação financeira
                                       (``SINGLE``|``DUAL``). **Modo efetivo = linha de maior
                                       ``mode_version``.** Gravável só por
                                       ``rd_security_init_approval_mode``/``rd_security_apply_mode_transition``.

DDL (funções, triggers, privilégios): ``app.modules.security.ddl``. Selos: ``app.modules.security.seal``.
O papel de runtime (``rd_app``) NÃO tem INSERT/UPDATE/DELETE/TRUNCATE nas duas trilhas.
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
    Identity,
    Index,
    Integer,
    SmallInteger,
    String,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base

APPROVAL_MODES = ("SINGLE", "DUAL")
INVITE_STATUSES = ("PENDING", "ACCEPTED", "REVOKED", "EXPIRED", "LOCKED")
SEAT_EVENTS = ("ASSIGNED", "RECOVERED")
SEAT_SOURCES = ("BOOTSTRAP_ENV", "MASTER_INVITE", "CLI_BOOTSTRAP", "CLI_RECOVERY", "LEGACY_ADOPTED")
MODE_KINDS = ("INITIALIZED", "SINGLE_TO_DUAL", "DUAL_TO_SINGLE")
MODE_SOURCES = ("BOOTSTRAP_ENV", "LEGACY_DUAL_ADOPTED", "APPROVAL_REQUEST")


def _in(values) -> str:
    return "(" + ",".join(f"'{v}'" for v in values) + ")"


class AdminMasterInvite(Base):
    """Convite de uso único para o 2º Admin Master (link só no e-mail do convidado)."""

    __tablename__ = "admin_master_invites"
    __table_args__ = (
        UniqueConstraint("token_hash", name="uq_admin_master_invites_token_hash"),
        UniqueConstraint("activation_user_id", name="uq_admin_master_invites_activation_user_id"),
        Index("uq_admin_master_invites_pending_seat", "seat", unique=True,
              postgresql_where=text("status = 'PENDING'")),
        Index("ix_admin_master_invites_status", "status"),
        CheckConstraint("seat IN (1, 2)", name="seat_valid"),
        CheckConstraint(f"status IN {_in(INVITE_STATUSES)}", name="status_valid"),
        CheckConstraint("expires_at > created_at AND expires_at <= created_at + interval '7 days'",
                        name="expires_window"),
        CheckConstraint("failed_attempts >= 0", name="failed_attempts_nonneg"),
        CheckConstraint("sent_count >= 1", name="sent_count_positive"),
        CheckConstraint("(status = 'ACCEPTED') = (accepted_at IS NOT NULL AND accepted_user_id IS NOT NULL)",
                        name="accepted_consistent"),
        CheckConstraint("accepted_user_id IS NULL OR accepted_user_id = activation_user_id",
                        name="accepted_user_matches"),
        CheckConstraint("status <> 'REVOKED' OR revoked_at IS NOT NULL", name="revoked_consistent"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    seat: Mapped[int] = mapped_column(SmallInteger, nullable=False)
    email: Mapped[str] = mapped_column(String(255), nullable=False)  # normalizado (minúsculas)
    full_name: Mapped[str] = mapped_column(String(255), nullable=False)
    token_hash: Mapped[str] = mapped_column(String(64), nullable=False)  # sha256_hex(token)
    # id RESERVADO do usuário B (AAD do segredo TOTP pendente); sem FK (o usuário só nasce na ativação)
    activation_user_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    invited_by: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="RESTRICT"), nullable=False
    )
    status: Mapped[str] = mapped_column(String(10), nullable=False, default="PENDING")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    # encrypt_str(secret, purpose="totp", aad=<aad do usuário activation_user_id>)
    totp_pending_secret_enc: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    failed_attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default=text("0"))
    sent_count: Mapped[int] = mapped_column(Integer, nullable=False, default=1, server_default=text("1"))
    last_sent_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())
    accepted_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    accepted_user_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="RESTRICT"), nullable=True
    )
    revoked_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    revoked_by: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="RESTRICT"), nullable=True
    )
    revoke_reason: Mapped[Optional[str]] = mapped_column(String(300), nullable=True)
    # 0009 (D1): selo HMAC (chave da aplicação) do convite — calculado e CONFERIDO pela aplicação na ativação;
    # o banco exige não nulo no INSERT do papel de runtime e que mude junto com ``token_hash`` (reenvio).
    seal: Mapped[Optional[str]] = mapped_column(Text, nullable=True)


class AdminMasterSeatEvent(Base):
    """Evento selado de assento (ASSIGNED | RECOVERED). APPEND-ONLY; ordem da cadeia = ``id``."""

    __tablename__ = "admin_master_seat_events"
    __table_args__ = (
        UniqueConstraint("app_seal", name="uq_admin_master_seat_events_app_seal"),
        Index("uq_admin_master_seat_events_invite_id", "invite_id", unique=True,
              postgresql_where=text("invite_id IS NOT NULL")),
        CheckConstraint("seat IN (1, 2)", name="seat_valid"),
        CheckConstraint(f"event IN {_in(SEAT_EVENTS)}", name="event_valid"),
        CheckConstraint(f"source IN {_in(SEAT_SOURCES)}", name="source_valid"),
        CheckConstraint("(event = 'RECOVERED') = (source = 'CLI_RECOVERY')", name="recovered_source"),
        CheckConstraint("(source = 'MASTER_INVITE') = (invite_id IS NOT NULL)", name="invite_source"),
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(always=False), primary_key=True)
    seat: Mapped[int] = mapped_column(SmallInteger, nullable=False)
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="RESTRICT"), nullable=False
    )
    event: Mapped[str] = mapped_column(String(10), nullable=False)
    source: Mapped[str] = mapped_column(String(20), nullable=False)
    invite_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True), ForeignKey("admin_master_invites.id", ondelete="RESTRICT"), nullable=True
    )
    actor_user_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="RESTRICT"), nullable=True
    )
    prev_seal: Mapped[str] = mapped_column(String(64), nullable=False)
    app_seal: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())


MODE_SHAPE_CHECK = (
    "(kind = 'INITIALIZED' AND mode_version = 1 AND from_mode IS NULL AND approval_request_id IS NULL"
    " AND requested_by IS NULL AND authorized_by IS NULL AND source IN ('BOOTSTRAP_ENV','LEGACY_DUAL_ADOPTED')"
    " AND (source <> 'LEGACY_DUAL_ADOPTED' OR to_mode = 'DUAL'))"
    " OR (kind = 'SINGLE_TO_DUAL' AND mode_version > 1 AND from_mode = 'SINGLE' AND to_mode = 'DUAL'"
    " AND source = 'APPROVAL_REQUEST' AND approval_request_id IS NOT NULL AND requested_by IS NOT NULL"
    " AND authorized_by = requested_by AND length(btrim(reason)) >= 20)"
    " OR (kind = 'DUAL_TO_SINGLE' AND mode_version > 1 AND from_mode = 'DUAL' AND to_mode = 'SINGLE'"
    " AND source = 'APPROVAL_REQUEST' AND approval_request_id IS NOT NULL AND requested_by IS NOT NULL"
    " AND authorized_by IS NOT NULL AND authorized_by <> requested_by AND length(btrim(reason)) >= 20)"
)


class InstanceApprovalModeHistory(Base):
    """Fonte da verdade do modo de aprovação financeira. APPEND-ONLY e selada."""

    __tablename__ = "instance_approval_mode_history"
    __table_args__ = (
        UniqueConstraint("mode_version", name="uq_instance_approval_mode_history_mode_version"),
        UniqueConstraint("approval_request_id", name="uq_instance_approval_mode_history_approval_request_id"),
        UniqueConstraint("app_seal", name="uq_instance_approval_mode_history_app_seal"),
        CheckConstraint("mode_version >= 1", name="mode_version_positive"),
        CheckConstraint(f"from_mode IS NULL OR from_mode IN {_in(APPROVAL_MODES)}", name="from_mode_valid"),
        CheckConstraint(f"to_mode IN {_in(APPROVAL_MODES)}", name="to_mode_valid"),
        CheckConstraint(f"kind IN {_in(MODE_KINDS)}", name="kind_valid"),
        CheckConstraint(f"source IN {_in(MODE_SOURCES)}", name="source_valid"),
        CheckConstraint(MODE_SHAPE_CHECK, name="shape"),
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(always=False), primary_key=True)
    mode_version: Mapped[int] = mapped_column(Integer, nullable=False)
    from_mode: Mapped[Optional[str]] = mapped_column(String(6), nullable=True)
    to_mode: Mapped[str] = mapped_column(String(6), nullable=False)
    kind: Mapped[str] = mapped_column(String(16), nullable=False)
    source: Mapped[str] = mapped_column(String(24), nullable=False)
    # nomes explícitos: o nome da convenção passaria de 63 caracteres
    approval_request_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("financial_approval_requests.id", ondelete="RESTRICT",
                   name="fk_instance_approval_mode_history_approval_request_id"),
        nullable=True,
    )
    requested_by: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="RESTRICT"), nullable=True
    )
    authorized_by: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="RESTRICT"), nullable=True
    )
    reason: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    env_value: Mapped[Optional[str]] = mapped_column(String(16), nullable=True)  # 'true' | 'false' | NULL
    prev_seal: Mapped[str] = mapped_column(String(64), nullable=False)
    app_seal: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())
