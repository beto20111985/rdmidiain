"""
RD Mídia Indoor — Configuração da INSTÂNCIA / white-label (AÇÃO 002 / W-BIZ).

Requisitos: ETAPA2 #2–#12, #26–#34; P001 §14, §77; P002 item 40.

Isolamento: cada instância (RD ou white-label) é uma STACK própria (PostgreSQL,
Redis, volumes, configuração) — estas tabelas descrevem SOMENTE a instância em que
vivem (linha única em ``instance_profile``). Nada aqui permite acesso a outra
instância; o control plane (``cp_*``) é quem enxerga várias instâncias.

- ``instance_profile``       marca (nome, logo, cores restritas aos tokens do DS),
                             subdomínio (futuro), contato, referência ao segredo
                             CloudPayPag da instância (nunca o valor).
- ``rd_price_floors``        tabela base de PREÇOS MÍNIMOS da RD: a instância pode
                             cobrar igual ou mais, nunca menos (ETAPA2 #7).
- ``rd_price_floor_history`` histórico append-only das alterações de piso.
- ``instance_signing_keys``  chaves ES256 da instância (privada cifrada em repouso)
                             usadas para assinar comprovantes de veiculação.
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
    func,
    text,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base


class InstanceProfile(Base):
    __tablename__ = "instance_profile"
    __table_args__ = (
        CheckConstraint("id = 1", name="singleton"),
        CheckConstraint("payment_provider = 'CLOUDPAYPAG'", name="payment_provider_cloudpaypag"),
    )

    id: Mapped[int] = mapped_column(SmallInteger, primary_key=True, default=1)
    instance_slug: Mapped[str] = mapped_column(String(60), nullable=False, default="rd")
    display_name: Mapped[str] = mapped_column(String(120), nullable=False, default="RD Mídia Indoor")
    legal_name: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    document: Mapped[Optional[str]] = mapped_column(String(20), nullable=True)
    is_white_label: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    support_email: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    support_phone: Mapped[Optional[str]] = mapped_column(String(30), nullable=True)
    primary_color: Mapped[Optional[str]] = mapped_column(String(7), nullable=True)
    accent_color: Mapped[Optional[str]] = mapped_column(String(7), nullable=True)
    logo_key: Mapped[Optional[str]] = mapped_column(String(512), nullable=True)
    logo_content_type: Mapped[Optional[str]] = mapped_column(String(50), nullable=True)
    logo_sha256: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    subdomain: Mapped[Optional[str]] = mapped_column(String(63), nullable=True)
    payment_provider: Mapped[str] = mapped_column(String(20), nullable=False, default="CLOUDPAYPAG",
                                                  server_default=text("'CLOUDPAYPAG'"))
    # Referência ao segredo (ex.: "env:CLOUDPAYPAG_API_KEY" ou "file:/run/secrets/cloudpaypag") — NUNCA o valor.
    payment_credentials_ref: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    payment_account_label: Mapped[Optional[str]] = mapped_column(String(120), nullable=True)
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    updated_by: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())


class RdPriceFloor(Base):
    __tablename__ = "rd_price_floors"
    __table_args__ = (CheckConstraint("min_price_cents >= 0", name="min_price_non_negative"),)

    key: Mapped[str] = mapped_column(String(80), primary_key=True)
    label: Mapped[str] = mapped_column(String(200), nullable=False)
    unit: Mapped[str] = mapped_column(String(80), nullable=False)
    min_price_cents: Mapped[int] = mapped_column(BigInteger, nullable=False)
    note: Mapped[Optional[str]] = mapped_column(String(500), nullable=True)
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    updated_by: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())


class RdPriceFloorHistory(Base):
    __tablename__ = "rd_price_floor_history"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    key: Mapped[str] = mapped_column(String(80), nullable=False, index=True)
    old_min_price_cents: Mapped[Optional[int]] = mapped_column(BigInteger, nullable=True)
    new_min_price_cents: Mapped[int] = mapped_column(BigInteger, nullable=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    changed_by: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    reason: Mapped[Optional[str]] = mapped_column(String(500), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())


class InstanceSigningKey(Base):
    __tablename__ = "instance_signing_keys"
    __table_args__ = (
        CheckConstraint("algorithm = 'ES256'", name="algorithm_es256"),
        Index("uq_instance_signing_keys_active", "is_active", unique=True, postgresql_where=text("is_active")),
    )

    kid: Mapped[str] = mapped_column(String(40), primary_key=True)
    algorithm: Mapped[str] = mapped_column(String(10), nullable=False, default="ES256")
    public_pem: Mapped[str] = mapped_column(Text, nullable=False)
    private_enc: Mapped[str] = mapped_column(Text, nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())
    retired_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
