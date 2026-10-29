"""
RD Mídia Indoor — Auditoria (contrato §4; P001 §99).

    from app.core.audit import audit
    await audit(db, ctx, "campaign.approved", "campaign", c.id,
                before={"status": "IN_REVIEW"}, after={"status": "APPROVED"},
                reason="ok", org_id=c.org_id)

- Grava na MESMA transação da operação (``db``): se a operação falhar, a auditoria
  não fica órfã. Para registrar algo que deve persistir mesmo se a request falhar
  (ex.: tentativa de login negada), use ``audit_independent(...)``.
- O ``AuditLog`` é adicionado à sessão sem flush imediato: a inserção acontece no
  flush/commit — minimizando o tempo em que o lock da cadeia de hash fica retido.
- ``ctx``: ``AuthContext`` (usuário/dispositivo/sistema) ou ``None`` (sistema).
  Durante impersonation, ``actor_user_id`` = usuário impersonado e
  ``real_user_id`` = usuário real.
- IP, user-agent e request_id vêm do contexto da request (middleware de logging).
- ``before``/``after`` são sanitizados: chaves sensíveis (senha, hash, token,
  segredo, código...) viram ``"[REDACTED]"``.
- ``verify_chain(db)`` recalcula a cadeia de hash e aponta a primeira divergência.
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from enum import Enum
from typing import Any, Optional

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.logging import SENSITIVE_KEY_RE, current_request_meta

_MAX_DEPTH = 6


def sanitize(value: Any, _depth: int = 0) -> Any:
    """Converte para JSON e remove valores de chaves sensíveis."""
    if _depth > _MAX_DEPTH:
        return "[...]"
    if value is None or isinstance(value, (bool, int, str)):
        return value
    if isinstance(value, float):
        return value
    if isinstance(value, (uuid.UUID, Decimal)):
        return str(value)
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, dict):
        out = {}
        for k, v in value.items():
            ks = str(k)
            out[ks] = "[REDACTED]" if SENSITIVE_KEY_RE.search(ks) else sanitize(v, _depth + 1)
        return out
    if isinstance(value, (list, tuple, set, frozenset)):
        return [sanitize(v, _depth + 1) for v in value]
    return str(value)


def _actor_fields(ctx: Any) -> dict:
    if ctx is None:
        return {"actor_user_id": None, "real_user_id": None, "actor_device_id": None, "origin": "system"}
    kind = getattr(ctx, "kind", "user")
    if kind == "device":
        return {
            "actor_user_id": None,
            "real_user_id": None,
            "actor_device_id": getattr(ctx, "device_id", None),
            "origin": "player",
        }
    if kind == "system":
        return {"actor_user_id": None, "real_user_id": None, "actor_device_id": None, "origin": getattr(ctx, "origin", "system")}
    return {
        "actor_user_id": getattr(ctx, "user_id", None),
        "real_user_id": getattr(ctx, "real_user_id", None),
        "actor_device_id": None,
        "origin": "api",
    }


def build_audit_row(
    ctx: Any,
    action: str,
    entity_type: Optional[str] = None,
    entity_id: Any = None,
    before: Any = None,
    after: Any = None,
    reason: Optional[str] = None,
    origin: Optional[str] = None,
    org_id: Any = None,
    actor_user_id: Any = None,
):
    from app.models.audit import AuditLog

    meta = current_request_meta()
    fields = _actor_fields(ctx)
    if actor_user_id is not None and fields["actor_user_id"] is None:
        fields["actor_user_id"] = actor_user_id
    if origin:
        fields["origin"] = origin
    return AuditLog(
        id=uuid.uuid4(),
        action=action[:100],
        entity_type=(entity_type[:100] if entity_type else None),
        entity_id=(str(entity_id)[:255] if entity_id is not None else None),
        before_data=sanitize(before) if before is not None else None,
        after_data=sanitize(after) if after is not None else None,
        reason=reason,
        org_id=org_id,
        ip_address=(meta.ip[:64] if meta.ip else None),
        user_agent=(meta.user_agent[:500] if meta.user_agent else None),
        request_id=meta.request_id,
        **fields,
    )


async def audit(
    db: AsyncSession,
    ctx: Any,
    action: str,
    entity_type: Optional[str] = None,
    entity_id: Any = None,
    before: Any = None,
    after: Any = None,
    reason: Optional[str] = None,
    origin: Optional[str] = None,
    *,
    org_id: Any = None,
    actor_user_id: Any = None,
):
    """Adiciona um registro de auditoria à transação corrente."""
    row = build_audit_row(ctx, action, entity_type, entity_id, before, after, reason, origin, org_id, actor_user_id)
    db.add(row)
    return row


async def audit_independent(
    ctx: Any,
    action: str,
    entity_type: Optional[str] = None,
    entity_id: Any = None,
    before: Any = None,
    after: Any = None,
    reason: Optional[str] = None,
    origin: Optional[str] = None,
    *,
    org_id: Any = None,
    actor_user_id: Any = None,
) -> None:
    """Grava auditoria em transação separada (persiste mesmo se a request falhar).

    Não chame enquanto a transação da request já tiver inserido auditoria não
    confirmada (o lock da cadeia seria disputado; ``lock_timeout`` evita travar).
    """
    from app.core.database import independent_session

    async with independent_session() as s:
        s.add(build_audit_row(ctx, action, entity_type, entity_id, before, after, reason, origin, org_id, actor_user_id))


@dataclass
class ChainReport:
    ok: bool
    rows_checked: int
    first_bad_seq: Optional[int] = None
    problem: Optional[str] = None


async def verify_chain(db: AsyncSession, limit: Optional[int] = None) -> ChainReport:
    from app.db.ddl import GENESIS_HASH

    sql = (
        "SELECT a.seq, a.prev_hash, a.row_hash, "
        "encode(sha256(convert_to(rd_audit_hash_material(a, a.prev_hash), 'UTF8')), 'hex') AS computed "
        "FROM audit_logs a ORDER BY a.seq"
    )
    if limit:
        sql += f" LIMIT {int(limit)}"
    rows = (await db.execute(text(sql))).all()
    prev = GENESIS_HASH
    for i, (seq, prev_hash, row_hash, computed) in enumerate(rows):
        if prev_hash != prev:
            return ChainReport(False, i, seq, "prev_hash não corresponde à linha anterior")
        if computed != row_hash:
            return ChainReport(False, i, seq, "row_hash não corresponde ao conteúdo")
        prev = row_hash
    return ChainReport(True, len(rows))
