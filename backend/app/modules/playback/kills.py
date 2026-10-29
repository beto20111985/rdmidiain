"""
RD Mídia Indoor — Kill de criativos (RDPP/1 §8; ETAPA2 #1237–#1239; P001 §75).

Interface para o gerador de manifesto (W-INV):

    from app.modules.playback.kills import revoked_creative_ids
    manifest["revoked"] = sorted(str(x) for x in await revoked_creative_ids(db, screen_id))

Kill ativo = ``lifted_at IS NULL`` e (``screen_id`` nulo — todas as telas — ou igual à tela).
"""
from __future__ import annotations

import uuid
from typing import Any, Iterable, Optional

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.audit import audit
from app.core.authz import AuthContext
from app.core.errors import DomainError, NotFound
from app.core.timeutils import utcnow
from app.models.devices import CreativeKill


async def revoked_creative_ids(db: AsyncSession, screen_id: uuid.UUID) -> list[uuid.UUID]:
    # Sem flush aqui de propósito: quem altera kills (create_kill/lift_kill) faz flush; um flush neste
    # ponto (dentro do gerador do W-INV) anteciparia o INSERT da auditoria pendente e o lock da cadeia.
    rows = (
        await db.execute(
            select(CreativeKill.creative_id)
            .where(CreativeKill.lifted_at.is_(None), or_(CreativeKill.screen_id.is_(None), CreativeKill.screen_id == screen_id))
            .distinct()
        )
    ).scalars().all()
    return sorted(set(rows), key=str)


async def create_kill(
    db: AsyncSession,
    ctx: AuthContext,
    creative_id: uuid.UUID,
    screen_ids: Optional[Iterable[uuid.UUID]],
    reason: str,
) -> list[CreativeKill]:
    if not reason or len(reason.strip()) < 3:
        raise DomainError("REASON_REQUIRED", "Informe o motivo do kill.", status=422)
    targets: list[Optional[uuid.UUID]] = list(dict.fromkeys(screen_ids)) if screen_ids else [None]
    kills = []
    now = utcnow()
    for sid in targets:
        k = CreativeKill(id=uuid.uuid4(), creative_id=creative_id, screen_id=sid, reason=reason.strip()[:2000],
                         issued_by_user_id=ctx.user_id, created_at=now)
        db.add(k)
        kills.append(k)
    await db.flush()
    await audit(
        db,
        ctx,
        "creative.killed",
        "creative",
        creative_id,
        after={"screens": [str(s) if s else "ALL" for s in targets], "kill_ids": [str(k.id) for k in kills]},
        reason=reason,
    )
    return kills


async def lift_kill(db: AsyncSession, ctx: AuthContext, kill_id: uuid.UUID, reason: str) -> CreativeKill:
    k = await db.get(CreativeKill, kill_id)
    if k is None:
        raise NotFound()
    if k.lifted_at is not None:
        return k
    k.lifted_at = utcnow()
    k.lifted_by_user_id = ctx.user_id
    # autoflush=False (core/database.py): sem flush, a regeneração do manifesto na MESMA transação
    # (invalidate_screens → revoked_creative_ids) ainda veria o kill ativo e não geraria versão nova.
    await db.flush()
    await audit(db, ctx, "creative.kill_lifted", "creative", k.creative_id, after={"kill_id": str(k.id)}, reason=reason)
    return k


def kill_public(k: Any) -> dict:
    return {
        "id": str(k.id),
        "creative_id": str(k.creative_id),
        "screen_id": str(k.screen_id) if k.screen_id else None,
        "reason": k.reason,
        "issued_by_user_id": str(k.issued_by_user_id) if k.issued_by_user_id else None,
        "created_at": k.created_at.isoformat().replace("+00:00", "Z") if k.created_at else None,
        "lifted_at": k.lifted_at.isoformat().replace("+00:00", "Z") if k.lifted_at else None,
    }
