"""
RD Mídia Indoor — Administração de prova de exibição e kill de criativos (W-PLAY).

As rotas antigas ``POST /playback/event`` e ``POST /playback/sync`` (JWT de USUÁRIO,
inalcançáveis por dispositivos — A4 A12) e ``GET /playback/stats`` foram REMOVIDAS:
a ingestão é ``POST /api/v1/player/events:batch`` (Bearer de dispositivo).

Rotas (``/api/v1/playback``):
- ``GET  /events``                 eventos brutos (PoP) — ``reports.view`` no escopo da tela
- ``GET  /daily-stats``            agregados diários — ``reports.view`` no escopo da tela
- ``GET  /kills`` · ``POST /kills`` · ``POST /kills/{id}/lift`` — ``campaigns.kill``
"""
from __future__ import annotations

import uuid
from datetime import date, datetime
from typing import Optional

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.authz import AuthContext, require
from app.core.database import get_db
from app.core.errors import DomainError, NotFound
from app.models.devices import CreativeKill, Device, DeviceStatus
from app.models.playback import PlaybackDailyStat, PlaybackEvent
from app.modules.playback import integration as integ
from app.modules.playback import kills as kill_svc
from app.modules.websocket import bus

router = APIRouter()


def _iso(d) -> Optional[str]:
    return d.isoformat().replace("+00:00", "Z") if d else None


def event_public(e: PlaybackEvent) -> dict:
    return {
        "playback_uuid": str(e.playback_uuid),
        "device_id": str(e.device_id),
        "screen_id": str(e.screen_id),
        "establishment_id": str(e.establishment_id) if e.establishment_id else None,
        "creative_id": str(e.creative_id),
        "creative_code": e.creative_code,
        "campaign_id": str(e.campaign_id) if e.campaign_id else None,
        "insertion_id": str(e.insertion_id) if e.insertion_id else None,
        "kind": e.kind,
        "manifest_version": e.manifest_version,
        "started_at": _iso(e.started_at),
        "finished_at": _iso(e.finished_at),
        "expected_ms": e.expected_ms,
        "actual_ms": e.actual_ms,
        "result": e.result,
        "seq": e.seq,
        "offline": e.offline,
        "status": e.status,
        "valid": e.valid,
        "reason": e.reason,
        "reasons": e.reasons,
        "flags": e.flags,
        "local_day": e.local_day.isoformat(),
        "received_at": _iso(e.received_at),
        "financial_status": e.financial_status,
    }


def stat_public(s: PlaybackDailyStat) -> dict:
    return {
        "screen_id": str(s.screen_id),
        "creative_id": str(s.creative_id),
        "creative_code": s.creative_code,
        "campaign_id": str(s.campaign_id) if s.campaign_id else None,
        "kind": s.kind,
        "local_day": s.local_day.isoformat(),
        "establishment_id": str(s.establishment_id) if s.establishment_id else None,
        "plays_accepted": s.plays_accepted,
        "plays_valid": s.plays_valid,
        "plays_invalid": s.plays_invalid,
        "plays_rejected": s.plays_rejected,
        "valid_ms": s.valid_ms,
        "accepted_ms": s.accepted_ms,
    }


@router.get("/events")
async def list_events(
    screen_id: Optional[uuid.UUID] = None,
    campaign_id: Optional[uuid.UUID] = None,
    creative_id: Optional[uuid.UUID] = None,
    valid: Optional[bool] = None,
    started_from: Optional[datetime] = None,
    started_to: Optional[datetime] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(25, ge=1, le=100),
    ctx: AuthContext = Depends(require("reports.view")),
    db: AsyncSession = Depends(get_db),
):
    q = select(PlaybackEvent).where(
        ctx.scope_filter("reports.view", org_col=PlaybackEvent.org_id, establishment_col=PlaybackEvent.establishment_id)
    )
    if screen_id:
        q = q.where(PlaybackEvent.screen_id == screen_id)
    if campaign_id:
        q = q.where(PlaybackEvent.campaign_id == campaign_id)
    if creative_id:
        q = q.where(PlaybackEvent.creative_id == creative_id)
    if valid is not None:
        q = q.where(PlaybackEvent.valid.is_(valid))
    if started_from:
        q = q.where(PlaybackEvent.started_at >= started_from)
    if started_to:
        q = q.where(PlaybackEvent.started_at < started_to)
    total = (await db.execute(select(func.count()).select_from(q.subquery()))).scalar_one()
    rows = (
        await db.execute(q.order_by(PlaybackEvent.started_at.desc()).offset((page - 1) * page_size).limit(page_size))
    ).scalars().all()
    return {"items": [event_public(e) for e in rows], "total": int(total), "page": page, "page_size": page_size}


@router.get("/daily-stats")
async def list_daily_stats(
    screen_id: Optional[uuid.UUID] = None,
    campaign_id: Optional[uuid.UUID] = None,
    day_from: Optional[date] = None,
    day_to: Optional[date] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(50, ge=1, le=100),
    ctx: AuthContext = Depends(require("reports.view")),
    db: AsyncSession = Depends(get_db),
):
    q = select(PlaybackDailyStat).where(
        ctx.scope_filter("reports.view", org_col=PlaybackDailyStat.org_id, establishment_col=PlaybackDailyStat.establishment_id)
    )
    if screen_id:
        q = q.where(PlaybackDailyStat.screen_id == screen_id)
    if campaign_id:
        q = q.where(PlaybackDailyStat.campaign_id == campaign_id)
    if day_from:
        q = q.where(PlaybackDailyStat.local_day >= day_from)
    if day_to:
        q = q.where(PlaybackDailyStat.local_day <= day_to)
    total = (await db.execute(select(func.count()).select_from(q.subquery()))).scalar_one()
    rows = (
        await db.execute(q.order_by(PlaybackDailyStat.local_day.desc()).offset((page - 1) * page_size).limit(page_size))
    ).scalars().all()
    return {"items": [stat_public(s) for s in rows], "total": int(total), "page": page, "page_size": page_size}


# ─── Kill ────────────────────────────────────────────────────────────────────

class KillIn(BaseModel):
    creative_id: uuid.UUID
    screen_ids: Optional[list[uuid.UUID]] = Field(None, max_length=10000)
    reason: str = Field(..., min_length=3, max_length=2000)


class LiftIn(BaseModel):
    reason: str = Field(..., min_length=3, max_length=2000)


@router.get("/kills")
async def list_kills(
    active: Optional[bool] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(25, ge=1, le=100),
    ctx: AuthContext = Depends(require("campaigns.kill")),
    db: AsyncSession = Depends(get_db),
):
    q = select(CreativeKill)
    if not ctx.can("campaigns.kill", global_only=True):
        from app.models.establishments import Establishment
        from app.models.screens import Screen

        q = q.join(Screen, Screen.id == CreativeKill.screen_id).join(Establishment, Establishment.id == Screen.establishment_id).where(
            ctx.scope_filter("campaigns.kill", org_col=Establishment.org_id, establishment_col=Establishment.id)
        )
    if active is True:
        q = q.where(CreativeKill.lifted_at.is_(None))
    elif active is False:
        q = q.where(CreativeKill.lifted_at.is_not(None))
    total = (await db.execute(select(func.count()).select_from(q.subquery()))).scalar_one()
    rows = (await db.execute(q.order_by(CreativeKill.created_at.desc()).offset((page - 1) * page_size).limit(page_size))).scalars().all()
    return {"items": [kill_svc.kill_public(k) for k in rows], "total": int(total), "page": page, "page_size": page_size}


@router.post("/kills", status_code=201)
async def create_kill(
    body: KillIn,
    ctx: AuthContext = Depends(require("campaigns.kill")),
    db: AsyncSession = Depends(get_db),
):
    if body.screen_ids:
        for sid in set(body.screen_ids):
            info = await integ.screen_info(db, sid)
            if info is None:
                raise NotFound()
            ctx.ensure("campaigns.kill", org_id=info.org_id, establishment_id=info.establishment_id, city=info.city_key)
        targets = list(dict.fromkeys(body.screen_ids))
    else:
        if not ctx.can("campaigns.kill", global_only=True):
            raise DomainError("KILL_SCOPE_REQUIRED", "Kill em todas as telas exige permissão global; informe as telas.", status=403)
        targets = await integ.screens_with_creative(db, body.creative_id)
        if targets is None:  # sem índice no inventário: todas as telas com dispositivo ativo
            targets = list((await db.execute(select(Device.screen_id).where(Device.status == DeviceStatus.ACTIVE.value))).scalars().all())
    kills = await kill_svc.create_kill(db, ctx, body.creative_id, body.screen_ids or None, body.reason)
    if targets:
        await integ.invalidate_screens(db, targets, reason="kill")
    await db.commit()
    data = {"creative_ids": [str(body.creative_id)], "reason": body.reason}
    if body.screen_ids:
        await bus.to_screens(targets, "kill", data)
    else:
        await bus.to_all("kill", data)
    return {"items": [kill_svc.kill_public(k) for k in kills], "screens_invalidated": len(targets)}


@router.post("/kills/{kill_id}/lift")
async def lift_kill(
    kill_id: uuid.UUID,
    body: LiftIn,
    ctx: AuthContext = Depends(require("campaigns.kill")),
    db: AsyncSession = Depends(get_db),
):
    k = await db.get(CreativeKill, kill_id)
    if k is None:
        raise NotFound()
    if k.screen_id is not None:
        info = await integ.screen_info(db, k.screen_id)
        if info is None:
            raise NotFound()
        ctx.ensure("campaigns.kill", org_id=info.org_id, establishment_id=info.establishment_id, city=info.city_key)
        targets = [k.screen_id]
    else:
        ctx.ensure("campaigns.kill", global_only=True)
        targets = await integ.screens_with_creative(db, k.creative_id) or list(
            (await db.execute(select(Device.screen_id).where(Device.status == DeviceStatus.ACTIVE.value))).scalars().all()
        )
    k = await kill_svc.lift_kill(db, ctx, kill_id, body.reason)
    if targets:
        await integ.invalidate_screens(db, targets, reason="kill_lifted")
    return kill_svc.kill_public(k)
