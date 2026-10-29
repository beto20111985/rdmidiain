"""
RD Mídia Indoor — Política de rotação versionada (ADR_006 §1–§2; A6 §10.2 item 1). W-INV.

Escopos GLOBAL < PARTNER_PLAN < ESTABELECIMENTO < TELA (o mais específico vence).
``I``/``E`` múltiplos de 30 s (> 0) e ``(I+E)`` divisor de 3600 (CHECK no banco);
vigências da mesma chave de escopo nunca se sobrepõem (EXCLUDE USING gist, btree_gist).
A política vale para um dia local de uma tela quando sua vigência contém a meia-noite
local daquele dia (mudança só vale para o futuro — validada no serviço).
"""
from __future__ import annotations

import uuid
from datetime import datetime
from typing import Optional

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    Computed,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    event,
    func,
    literal_column,
    text,
)
from sqlalchemy.dialects.postgresql import UUID, ExcludeConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base

POLICY_SCOPES = ("GLOBAL", "PARTNER_PLAN", "ESTABLISHMENT", "SCREEN")
SCOPE_RANK = {"GLOBAL": 0, "PARTNER_PLAN": 1, "ESTABLISHMENT": 2, "SCREEN": 3}


class RotationPolicy(Base):
    __tablename__ = "rotation_policies"
    __table_args__ = (
        CheckConstraint("scope IN ('GLOBAL','PARTNER_PLAN','ESTABLISHMENT','SCREEN')", name="scope"),
        CheckConstraint("(scope = 'GLOBAL') = (scope_id IS NULL)", name="scope_id_matches_scope"),
        CheckConstraint("internal_seconds > 0 AND internal_seconds % 30 = 0", name="internal_multiple_30"),
        CheckConstraint("external_seconds > 0 AND external_seconds % 30 = 0", name="external_multiple_30"),
        CheckConstraint("3600 % (internal_seconds + external_seconds) = 0", name="cycle_divides_hour"),
        CheckConstraint(
            "overflow_max_seconds >= 0 AND overflow_max_seconds % 30 = 0 AND overflow_max_seconds <= internal_seconds",
            name="overflow_limit",
        ),
        CheckConstraint("allow_external_overflow OR overflow_max_seconds = 0", name="overflow_requires_flag"),
        CheckConstraint("effective_to IS NULL OR effective_to > effective_from", name="effective_range"),
        CheckConstraint("length(btrim(reason)) >= 5", name="reason_required"),
        Index("ix_rotation_policies_scope", "scope", "scope_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    scope: Mapped[str] = mapped_column(String(20), nullable=False)
    scope_id: Mapped[Optional[uuid.UUID]] = mapped_column(UUID(as_uuid=True), nullable=True)
    scope_key: Mapped[str] = mapped_column(
        Text, Computed("scope || ':' || coalesce(scope_id::text, '*')", persisted=True), nullable=False
    )
    internal_seconds: Mapped[int] = mapped_column(Integer, nullable=False)
    external_seconds: Mapped[int] = mapped_column(Integer, nullable=False)
    allow_external_overflow: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False,
                                                          server_default="false")
    overflow_max_seconds: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    effective_from: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    effective_to: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1, server_default="1")
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    created_by: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="RESTRICT"), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())


_t = RotationPolicy.__table__
_t.append_constraint(
    ExcludeConstraint(
        (_t.c.scope_key, "="),
        (func.tstzrange(_t.c.effective_from, _t.c.effective_to, literal_column("'[)'")), "&&"),
        using="gist",
        name="ex_rotation_policies_no_overlap",
    )
)


@event.listens_for(_t, "before_create")
def _btree_gist(target, connection, **kw):  # pragma: no cover - DDL
    # EXCLUDE com igualdade em texto exige btree_gist (extensão confiável no PG ≥ 13)
    connection.execute(text("CREATE EXTENSION IF NOT EXISTS btree_gist"))
