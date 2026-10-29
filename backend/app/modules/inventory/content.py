"""
RD Mídia Indoor — Conteúdo interno, fallback e emergência: regras e auditoria (W-INV).

- Conteúdo interno (#1052–#1064): RD cadastra/remove (``playlists.manage`` GLOBAL);
  o estabelecimento só REORDENA (``playlists.reorder`` no seu escopo). Lista ordenada,
  sem datas/horários; criativo ``kind = INTERNAL``; peça > bloco interno é ignorada
  pelo gerador (nunca cortar). Qualquer alteração gera nova versão de manifesto.
- Fallback (P001 §35): GLOBAL (padrão/divulgação RD) e por ESTABELECIMENTO (logo);
  criativo ``kind`` FALLBACK ou INSTITUTIONAL.
- Emergência (#1073–#1075, #1401): janela + alvo; único conteúdo que preempta;
  ``campaigns.kill`` GLOBAL, justificativa obrigatória, auditado; publica nova versão
  de manifesto nas telas-alvo (lotes grandes via worker).
"""
from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any, Optional, Sequence

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.audit import audit
from app.core.errors import Conflict, DomainError, NotFound
from app.models.content import EMERGENCY_TARGETS, FALLBACK_PURPOSES, EmergencyBroadcast, FallbackContent, InternalContent
from app.modules.inventory import notify as N
from app.modules.inventory import sources as S
from app.modules.inventory.manifests import invalidate_screens
from app.modules.inventory.state import db_now, ms_of


def _u(v: Any) -> uuid.UUID:
    return v if isinstance(v, uuid.UUID) else uuid.UUID(str(v))


async def _establishment(db: AsyncSession, establishment_id: Any):
    from app.models.establishments import Establishment

    est = await db.get(Establishment, _u(establishment_id))
    if est is None or getattr(est, "deleted_at", None) is not None:
        raise NotFound()
    return est


async def _ready_creative(db: AsyncSession, creative_id: Any, kinds: Sequence[str]):
    from app.models.campaigns import Creative

    c = await db.get(Creative, _u(creative_id))
    if c is None:
        raise NotFound("CREATIVE_NOT_FOUND", "Criativo não encontrado.")
    if c.kind not in kinds:
        raise DomainError("CREATIVE_KIND_INVALID", f"Criativo deve ser do tipo {' ou '.join(kinds)}.", status=400,
                          details={"kind": c.kind})
    if c.status != S.CREATIVE_APPROVED:
        raise DomainError("CREATIVE_NOT_APPROVED", "Criativo precisa estar APROVADO.", status=400)
    media = await S.media_refs(db, [c.media_file_id])
    if c.media_file_id not in media:
        raise DomainError("MEDIA_NOT_READY", "Mídia do criativo ainda não está pronta (READY).", status=400)
    return c


async def _screens_of_establishment(db: AsyncSession, establishment_id: uuid.UUID) -> list[uuid.UUID]:
    return await S.screens_matching(db, establishment_ids=[establishment_id])


def internal_dict(r: InternalContent, creative=None) -> dict:
    d = {"id": str(r.id), "establishment_id": str(r.establishment_id),
         "screen_id": str(r.screen_id) if r.screen_id else None, "creative_id": str(r.creative_id),
         "position": r.position, "active": r.active, "created_at": r.created_at, "updated_at": r.updated_at}
    if creative is not None:
        d.update({"code": creative.code, "name": creative.name, "duration_seconds": creative.duration_seconds})
    return d


async def list_internal(db: AsyncSession, establishment_id: Any, screen_id: Any = None) -> list[dict]:
    from app.models.campaigns import Creative

    q = (select(InternalContent, Creative).join(Creative, Creative.id == InternalContent.creative_id)
         .where(InternalContent.establishment_id == _u(establishment_id)))
    q = q.where(InternalContent.screen_id == _u(screen_id)) if screen_id else q.where(InternalContent.screen_id.is_(None))
    rows = (await db.execute(q.order_by(InternalContent.position, InternalContent.id))).all()
    return [internal_dict(r, c) for r, c in rows]


async def add_internal(db: AsyncSession, ctx: Any, *, establishment_id: Any, creative_id: Any,
                       screen_id: Any = None, position: Optional[int] = None) -> InternalContent:
    est = await _establishment(db, establishment_id)
    sid = _u(screen_id) if screen_id else None
    if sid is not None:
        info = (await S.load_screens(db, [sid])).get(sid)
        if info is None or info.establishment_id != est.id:
            raise DomainError("SCREEN_NOT_IN_ESTABLISHMENT", "A tela não pertence ao estabelecimento.", status=400)
    c = await _ready_creative(db, creative_id, ("INTERNAL",))
    q = select(func.coalesce(func.max(InternalContent.position), -1)).where(
        InternalContent.establishment_id == est.id,
        InternalContent.screen_id == sid if sid else InternalContent.screen_id.is_(None))
    last = (await db.execute(q)).scalar_one()
    pos = int(last) + 1 if position is None else int(position)
    if pos < 0:
        raise DomainError("POSITION_INVALID", "Posição inválida.", status=400)
    row = InternalContent(id=uuid.uuid4(), establishment_id=est.id, screen_id=sid, creative_id=c.id, position=pos,
                          active=True, created_by=getattr(ctx, "user_id", None), updated_by=getattr(ctx, "user_id", None))
    db.add(row)
    await db.flush()
    await audit(db, ctx, "internal_content.added", "internal_content", row.id, after=internal_dict(row),
                org_id=est.org_id)
    await invalidate_screens(db, [sid] if sid else await _screens_of_establishment(db, est.id), "internal_content")
    return row


async def update_internal(db: AsyncSession, ctx: Any, item_id: Any, *, active: Optional[bool]) -> InternalContent:
    row = await db.get(InternalContent, _u(item_id))
    if row is None:
        raise NotFound()
    est = await _establishment(db, row.establishment_id)
    before = internal_dict(row)
    if active is not None:
        row.active = bool(active)
    row.updated_by = getattr(ctx, "user_id", None)
    await db.flush()
    await audit(db, ctx, "internal_content.updated", "internal_content", row.id, before=before,
                after=internal_dict(row), org_id=est.org_id)
    await invalidate_screens(db, [row.screen_id] if row.screen_id else await _screens_of_establishment(db, est.id),
                             "internal_content")
    return row


async def remove_internal(db: AsyncSession, ctx: Any, item_id: Any) -> None:
    row = await db.get(InternalContent, _u(item_id))
    if row is None:
        raise NotFound()
    est = await _establishment(db, row.establishment_id)
    before = internal_dict(row)
    sid = row.screen_id
    await db.delete(row)
    await db.flush()
    await audit(db, ctx, "internal_content.removed", "internal_content", before["id"], before=before, org_id=est.org_id)
    await invalidate_screens(db, [sid] if sid else await _screens_of_establishment(db, est.id), "internal_content")


async def reorder_internal(db: AsyncSession, ctx: Any, *, establishment_id: Any, item_ids: Sequence[Any],
                           screen_id: Any = None) -> list[dict]:
    """Reordenação (estabelecimento com ``playlists.reorder`` ou RD). Exige o conjunto COMPLETO."""
    est = await _establishment(db, establishment_id)
    sid = _u(screen_id) if screen_id else None
    q = select(InternalContent).where(InternalContent.establishment_id == est.id)
    q = q.where(InternalContent.screen_id == sid) if sid else q.where(InternalContent.screen_id.is_(None))
    rows = {r.id: r for r in (await db.execute(q.with_for_update())).scalars().all()}
    ids = [_u(i) for i in item_ids]
    if len(set(ids)) != len(ids) or set(ids) != set(rows):
        raise DomainError("REORDER_SET_MISMATCH", "Envie todos os itens da lista exatamente uma vez.", status=400,
                          details={"expected": len(rows)})
    before = [str(r.id) for r in sorted(rows.values(), key=lambda r: (r.position, str(r.id)))]
    for pos, i in enumerate(ids):
        rows[i].position = pos
        rows[i].updated_by = getattr(ctx, "user_id", None)
    await db.flush()
    await audit(db, ctx, "internal_content.reordered", "establishment", est.id, before={"order": before},
                after={"order": [str(i) for i in ids], "screen_id": str(sid) if sid else None}, org_id=est.org_id)
    await invalidate_screens(db, [sid] if sid else await _screens_of_establishment(db, est.id), "internal_reorder")
    return await list_internal(db, est.id, sid)


# ─── Fallback ────────────────────────────────────────────────────────────────

def fallback_dict(r: FallbackContent) -> dict:
    return {"id": str(r.id), "scope": r.scope, "establishment_id": str(r.establishment_id) if r.establishment_id else None,
            "creative_id": str(r.creative_id), "purpose": r.purpose, "position": r.position, "active": r.active,
            "created_at": r.created_at}


async def list_fallback(db: AsyncSession, *, scope: Optional[str] = None, establishment_id: Any = None) -> list[dict]:
    q = select(FallbackContent)
    if scope:
        q = q.where(FallbackContent.scope == scope)
    if establishment_id:
        q = q.where(FallbackContent.establishment_id == _u(establishment_id))
    rows = (await db.execute(q.order_by(FallbackContent.scope, FallbackContent.position, FallbackContent.id))).scalars()
    return [fallback_dict(r) for r in rows]


async def add_fallback(db: AsyncSession, ctx: Any, *, scope: str, creative_id: Any, purpose: str,
                       establishment_id: Any = None, position: int = 0) -> FallbackContent:
    if scope not in ("GLOBAL", "ESTABLISHMENT"):
        raise DomainError("FALLBACK_SCOPE_INVALID", "Escopo deve ser GLOBAL ou ESTABLISHMENT.", status=400)
    if purpose not in FALLBACK_PURPOSES:
        raise DomainError("FALLBACK_PURPOSE_INVALID", "Finalidade inválida.", status=400,
                          details={"allowed": list(FALLBACK_PURPOSES)})
    est = await _establishment(db, establishment_id) if scope == "ESTABLISHMENT" else None
    if scope == "GLOBAL" and establishment_id:
        raise DomainError("FALLBACK_SCOPE_INVALID", "Fallback GLOBAL não tem estabelecimento.", status=400)
    c = await _ready_creative(db, creative_id, ("FALLBACK", "INSTITUTIONAL"))
    row = FallbackContent(id=uuid.uuid4(), scope=scope, establishment_id=est.id if est else None, creative_id=c.id,
                          purpose=purpose, position=int(position), active=True, created_by=getattr(ctx, "user_id", None))
    db.add(row)
    await db.flush()
    await audit(db, ctx, "fallback_content.added", "fallback_content", row.id, after=fallback_dict(row),
                org_id=est.org_id if est else None)
    sids = await _screens_of_establishment(db, est.id) if est else await S.screens_matching(db, all_screens=True)
    await invalidate_screens(db, sids, "fallback_content")
    return row


async def remove_fallback(db: AsyncSession, ctx: Any, item_id: Any) -> None:
    row = await db.get(FallbackContent, _u(item_id))
    if row is None:
        raise NotFound()
    before = fallback_dict(row)
    est_id = row.establishment_id
    await db.delete(row)
    await db.flush()
    await audit(db, ctx, "fallback_content.removed", "fallback_content", before["id"], before=before)
    sids = await _screens_of_establishment(db, est_id) if est_id else await S.screens_matching(db, all_screens=True)
    await invalidate_screens(db, sids, "fallback_content")


# ─── Emergência ──────────────────────────────────────────────────────────────

def emergency_dict(e: EmergencyBroadcast) -> dict:
    return {"id": str(e.id), "title": e.title, "creative_id": str(e.creative_id), "starts_at": e.starts_at,
            "ends_at": e.ends_at, "target_kind": e.target_kind, "target_ids": list(e.target_ids or []),
            "status": e.status, "reason": e.reason, "created_by": str(e.created_by), "created_at": e.created_at,
            "cancelled_at": e.cancelled_at, "cancel_reason": e.cancel_reason}


async def _emergency_screens(db: AsyncSession, e: EmergencyBroadcast) -> list[uuid.UUID]:
    t = e.target_kind
    ids = list(e.target_ids or [])
    if t == "ALL":
        return await S.screens_matching(db, all_screens=True)
    if t == "SCREENS":
        return await S.screens_matching(db, screen_ids=ids)
    if t == "ESTABLISHMENTS":
        return await S.screens_matching(db, establishment_ids=ids)
    if t == "STATES":
        return await S.screens_matching(db, states=ids)
    return await S.screens_matching(db, city_keys=ids)


async def create_emergency(db: AsyncSession, ctx: Any, *, title: str, creative_id: Any, starts_at: datetime,
                           ends_at: datetime, target_kind: str, target_ids: Sequence[Any], reason: str
                           ) -> tuple[EmergencyBroadcast, int]:
    if target_kind not in EMERGENCY_TARGETS:
        raise DomainError("EMERGENCY_TARGET_INVALID", "Alvo inválido.", status=400,
                          details={"allowed": list(EMERGENCY_TARGETS)})
    if target_kind != "ALL" and not target_ids:
        raise DomainError("EMERGENCY_TARGET_INVALID", "Informe os alvos.", status=400)
    if not reason or len(reason.strip()) < 10:
        raise DomainError("REASON_REQUIRED", "Justificativa obrigatória (mínimo 10 caracteres).", status=400)
    if starts_at.tzinfo is None or ends_at.tzinfo is None or ends_at <= starts_at:
        raise DomainError("EMERGENCY_WINDOW_INVALID", "Janela inválida (início < fim, com fuso).", status=400)
    now = await db_now(db)
    if ends_at <= now:
        raise DomainError("EMERGENCY_WINDOW_INVALID", "A janela já terminou.", status=400)
    c = await _ready_creative(db, creative_id, ("INSTITUTIONAL", "FALLBACK", "INTERNAL", "EXTERNAL"))
    norm = [str(x).upper() if target_kind == "STATES" else str(x) for x in (target_ids or [])]
    e = EmergencyBroadcast(id=uuid.uuid4(), title=title.strip()[:200], creative_id=c.id, starts_at=starts_at,
                           ends_at=ends_at, target_kind=target_kind, target_ids=norm, status="ACTIVE",
                           reason=reason.strip(), created_by=ctx.user_id)
    db.add(e)
    await db.flush()
    sids = await _emergency_screens(db, e)
    await audit(db, ctx, "emergency.created", "emergency_broadcast", e.id, after={**emergency_dict(e),
                                                                                    "screens": len(sids)},
                reason=reason)
    await invalidate_screens(db, sids, "emergency")
    await N.notify_safe(db, N.staff("campaigns.kill"), "inventory.emergency_published",
                        {"title": e.title, "screens": len(sids), "emergency_id": str(e.id)},
                        dedupe_key=f"inventory.emergency:{e.id}")
    return e, len(sids)


async def cancel_emergency(db: AsyncSession, ctx: Any, emergency_id: Any, reason: str) -> EmergencyBroadcast:
    e = (await db.execute(select(EmergencyBroadcast).where(EmergencyBroadcast.id == _u(emergency_id))
                          .with_for_update())).scalar_one_or_none()
    if e is None:
        raise NotFound()
    if e.status != "ACTIVE":
        raise Conflict("EMERGENCY_NOT_ACTIVE", "Comunicado já cancelado.")
    if not reason or len(reason.strip()) < 10:
        raise DomainError("REASON_REQUIRED", "Justificativa obrigatória (mínimo 10 caracteres).", status=400)
    before = emergency_dict(e)
    e.status, e.cancelled_at, e.cancelled_by, e.cancel_reason = "CANCELLED", await db_now(db), ctx.user_id, reason.strip()
    await db.flush()
    await audit(db, ctx, "emergency.cancelled", "emergency_broadcast", e.id, before=before, after=emergency_dict(e),
                reason=reason)
    await invalidate_screens(db, await _emergency_screens(db, e), "emergency_cancelled")
    return e


async def list_emergencies(db: AsyncSession, *, active_only: bool = False) -> list[dict]:
    q = select(EmergencyBroadcast)
    if active_only:
        q = q.where(EmergencyBroadcast.status == "ACTIVE", EmergencyBroadcast.ends_at > func.now())
    rows = (await db.execute(q.order_by(EmergencyBroadcast.starts_at.desc()).limit(200))).scalars()
    return [emergency_dict(e) for e in rows]


__all__ = [
    "add_internal", "update_internal", "remove_internal", "reorder_internal", "list_internal",
    "add_fallback", "remove_fallback", "list_fallback", "create_emergency", "cancel_emergency", "list_emergencies",
    "ms_of",
]
