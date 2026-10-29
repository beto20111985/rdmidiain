"""
RD Mídia Indoor — API de inventário por capacidade (W-INV; ADR_006).

Rotas (prefixo ``/api/v1/inventory``):
- ``GET  /policies``                         políticas de rotação (RD: ``settings.view`` GLOBAL)
- ``POST /policies``                         nova versão de política (``settings.manage`` GLOBAL, sem impersonation)
- ``GET  /screens/{id}/state``               estado de inventário/âncora/política (``inventory.view`` no escopo)
- ``GET  /screens/{id}/anchor/history``      histórico de âncora (``inventory.view`` GLOBAL)
- ``POST /screens/{id}/anchor/reset``        reset auditado com justificativa (``screens.manage`` GLOBAL)
- ``GET  /availability``                     capacidade × ocupação por região/período — só números
                                             (``campaign_id`` opcional: só telas elegíveis da campanha, W-CAMP)
- ``POST /quote``                            cabimento de padrões nas telas (``checkout.create``; sem nomes)
- ``GET  /holds`` · ``GET /holds/{id}``      reservas (``inventory.view`` no escopo da organização)
- ``POST /holds/{id}/release``               desistência de reserva NÃO paga (``inventory.reserve``/``checkout.create``)
- ``GET  /retirements`` · ``POST /retirements/{id}/resolve``  relatório de redistribuição (#1116)

Removidas (A6 E06–E11): ``GET /availability`` por slot de relógio, ``POST /reservations``,
``POST /reservations/{id}/book`` (confirmava sem pagamento), ``DELETE /reservations/{id}``
(liberava inventário pago) e ``GET /reservations`` (sem escopo de tenant). A reserva
acontece SÓ no checkout (W-CAMP → ``inventory.service.hold``) e a conversão SÓ no
pagamento (W-FIN → W-CAMP → ``convert_hold``).
"""
from __future__ import annotations

import uuid
from datetime import date, datetime
from typing import Any, Optional

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.audit import audit
from app.core.authz import AuthContext, require
from app.core.database import get_db
from app.core.errors import Conflict, DomainError, Forbidden, NotFound
from app.core.timeutils import isoformat_z
from app.models.inventory import BookingScreen, InventoryHold, ScreenRetirement
from app.models.rotation import RotationPolicy
from app.modules.inventory import service as inv
from app.modules.inventory import sources as S
from app.modules.inventory.policies import create_policy, policy_dict

router = APIRouter()


def jz(v: Any) -> Any:
    """Serialização do contrato: datetimes em ISO-8601 UTC com ``Z``; UUID em texto."""
    if isinstance(v, datetime):
        return isoformat_z(v)
    if isinstance(v, date):
        return v.isoformat()
    if isinstance(v, uuid.UUID):
        return str(v)
    if isinstance(v, dict):
        return {k: jz(x) for k, x in v.items()}
    if isinstance(v, (list, tuple)):
        return [jz(x) for x in v]
    return v


async def _screen_scope(db: AsyncSession, ctx: AuthContext, perm: str, screen_id: uuid.UUID) -> S.ScreenInfo:
    info = (await S.load_screens(db, [screen_id])).get(screen_id)
    if info is None:
        raise NotFound()
    ctx.ensure(perm, org_id=info.org_id, establishment_id=info.establishment_id, city=info.city_key)
    return info


# ─── Políticas de rotação ────────────────────────────────────────────────────

class PolicyIn(BaseModel):
    scope: str = Field(pattern="^(GLOBAL|PARTNER_PLAN|ESTABLISHMENT|SCREEN)$")
    scope_id: Optional[uuid.UUID] = None
    internal_seconds: int = Field(ge=30, le=3600)
    external_seconds: int = Field(ge=30, le=3600)
    allow_external_overflow: bool = False
    overflow_max_seconds: int = Field(0, ge=0, le=3600)
    effective_from: datetime
    reason: str = Field(min_length=5, max_length=2000)


@router.get("/policies")
async def list_policies(
    scope: Optional[str] = Query(None, pattern="^(GLOBAL|PARTNER_PLAN|ESTABLISHMENT|SCREEN)$"),
    scope_id: Optional[uuid.UUID] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(25, ge=1, le=100),
    ctx: AuthContext = Depends(require("settings.view", global_only=True)),
    db: AsyncSession = Depends(get_db),
):
    q = select(RotationPolicy)
    if scope:
        q = q.where(RotationPolicy.scope == scope)
    if scope_id:
        q = q.where(RotationPolicy.scope_id == scope_id)
    total = (await db.execute(select(func.count()).select_from(q.subquery()))).scalar_one()
    rows = (await db.execute(q.order_by(RotationPolicy.scope, RotationPolicy.scope_id, RotationPolicy.effective_from.desc())
                             .offset((page - 1) * page_size).limit(page_size))).scalars()
    default = await inv.resolve_for_screens(db, [])
    return jz({"items": [policy_dict(r) for r in rows], "total": total, "page": page, "page_size": page_size,
               "default": default._default.summary() if default._default else None})


@router.post("/policies", status_code=201)
async def post_policy(
    body: PolicyIn,
    ctx: AuthContext = Depends(require("settings.manage", global_only=True, allow_impersonation=False)),
    db: AsyncSession = Depends(get_db),
):
    row = await create_policy(db, ctx, scope=body.scope, scope_id=body.scope_id,
                              internal_seconds=body.internal_seconds, external_seconds=body.external_seconds,
                              effective_from=body.effective_from, reason=body.reason,
                              allow_external_overflow=body.allow_external_overflow,
                              overflow_max_seconds=body.overflow_max_seconds)
    return jz(policy_dict(row))


# ─── Estado/âncora por tela ──────────────────────────────────────────────────

class AnchorResetIn(BaseModel):
    reason: str = Field(min_length=10, max_length=2000)
    anchor_ms: Optional[int] = Field(None, ge=0)


@router.get("/screens/{screen_id}/state")
async def screen_state(screen_id: uuid.UUID, ctx: AuthContext = Depends(require("inventory.view")),
                       db: AsyncSession = Depends(get_db)):
    await _screen_scope(db, ctx, "inventory.view", screen_id)
    return jz(await inv.screen_inventory_view(db, screen_id))


@router.get("/screens/{screen_id}/anchor/history")
async def screen_anchor_history(screen_id: uuid.UUID,
                                ctx: AuthContext = Depends(require("inventory.view", global_only=True)),
                                db: AsyncSession = Depends(get_db)):
    await _screen_scope(db, ctx, "inventory.view", screen_id)
    return jz({"items": await inv.anchor_history(db, screen_id)})


@router.post("/screens/{screen_id}/anchor/reset")
async def screen_anchor_reset(screen_id: uuid.UUID, body: AnchorResetIn,
                              ctx: AuthContext = Depends(require("screens.manage", global_only=True,
                                                                 allow_impersonation=False)),
                              db: AsyncSession = Depends(get_db)):
    await _screen_scope(db, ctx, "screens.manage", screen_id)
    new = await inv.reset_anchor(db, ctx, screen_id, body.reason, body.anchor_ms)
    return jz({"screen_id": screen_id, "anchor_ms": new})


# ─── Disponibilidade / cotação (somente números) ─────────────────────────────

@router.get("/availability")
async def get_availability(
    date_from: date,
    date_to: date,
    state: Optional[str] = Query(None, min_length=2, max_length=2),
    city_key: Optional[str] = Query(None, max_length=120),
    establishment_id: Optional[uuid.UUID] = None,
    campaign_id: Optional[uuid.UUID] = None,
    ctx: AuthContext = Depends(require("inventory.view")),
    db: AsyncSession = Depends(get_db),
):
    region = bool(state or city_key or establishment_id)
    if not (region or campaign_id):
        raise DomainError("REGION_REQUIRED", "Informe UF, cidade ou estabelecimento.", status=400)
    sids = []
    if region:
        sids = await S.screens_matching(db, states=[state] if state else [], city_keys=[city_key] if city_key else [],
                                        establishment_ids=[establishment_id] if establishment_id else [])
    if campaign_id is not None:
        # (edição W-CAMP autorizada) com ``campaign_id``: só as telas que a campanha REALMENTE compraria —
        # mesma função do checkout/aprovação (segmentação, duração, conflitos com os criativos reais);
        # sem filtro regional, as telas elegíveis da segmentação da campanha. Sem ``campaign_id``: inalterado.
        from app.modules.campaigns import service as CS

        if region:
            sids = await CS.eligible_screen_ids_among(db, ctx, campaign_id, sids)
        else:
            c = await CS.get_campaign(db, ctx, campaign_id, perm="campaigns.view")
            sids = (await CS.eligible_resolution(db, c)).screen_ids
    # resposta AGREGADA (contagens de unidades por dia; sem ids/nomes de telas, estabelecimentos ou
    # anunciantes) — o anunciante vê a disponibilidade da região sem identificar terceiros
    return jz(await inv.availability(db, screen_ids=sids, date_from=date_from, date_to=date_to))


class QuoteIn(BaseModel):
    screen_ids: list[uuid.UUID] = Field(min_length=1, max_length=5000)
    patterns: list[dict] = Field(min_length=1, max_length=20)
    campaign_id: Optional[uuid.UUID] = None


@router.post("/quote")
async def post_quote(body: QuoteIn, ctx: AuthContext = Depends(require("checkout.create")),
                     db: AsyncSession = Depends(get_db)):
    if body.campaign_id is not None:
        camp = (await S.load_campaigns(db, [body.campaign_id])).get(body.campaign_id)
        if camp is None:
            raise NotFound()
        ctx.ensure("checkout.create", org_id=camp.org_id)
    res = await inv.quote_capacity(db, ctx, body.screen_ids, body.patterns, campaign_id=body.campaign_id)
    return jz(res.to_dict(public=True))


# ─── Reservas ────────────────────────────────────────────────────────────────

class ReleaseIn(BaseModel):
    reason: str = Field(min_length=3, max_length=64)


async def _hold_scoped(db: AsyncSession, ctx: AuthContext, perm: str, hold_id: uuid.UUID) -> InventoryHold:
    h = await db.get(InventoryHold, hold_id)
    if h is None:
        raise NotFound()
    ctx.ensure(perm, org_id=h.org_id)
    return h


@router.get("/holds")
async def list_holds(
    campaign_id: Optional[uuid.UUID] = None,
    status: Optional[str] = Query(None, pattern="^(HELD|CONVERTED|EXPIRED|CANCELLED|PAID_WITHOUT_INVENTORY)$"),
    page: int = Query(1, ge=1),
    page_size: int = Query(25, ge=1, le=100),
    ctx: AuthContext = Depends(require("inventory.view")),
    db: AsyncSession = Depends(get_db),
):
    q = select(InventoryHold).where(ctx.scope_filter("inventory.view", org_col=InventoryHold.org_id))
    if campaign_id:
        q = q.where(InventoryHold.campaign_id == campaign_id)
    if status:
        q = q.where(InventoryHold.status == status)
    total = (await db.execute(select(func.count()).select_from(q.subquery()))).scalar_one()
    rows = (await db.execute(q.order_by(InventoryHold.created_at.desc()).offset((page - 1) * page_size)
                             .limit(page_size))).scalars().all()
    return jz({"items": [inv.hold_dict(h) for h in rows], "total": total, "page": page, "page_size": page_size})


@router.get("/holds/{hold_id}")
async def get_hold(hold_id: uuid.UUID, ctx: AuthContext = Depends(require("inventory.view")),
                   db: AsyncSession = Depends(get_db)):
    h = await _hold_scoped(db, ctx, "inventory.view", hold_id)
    sids = (await db.execute(select(BookingScreen.screen_id).where(BookingScreen.hold_id == h.id).distinct())).scalars()
    return jz(inv.hold_dict(h, sorted(set(sids), key=str)))


@router.post("/holds/{hold_id}/release")
async def post_release(hold_id: uuid.UUID, body: ReleaseIn, ctx: AuthContext = Depends(require("checkout.create")),
                       db: AsyncSession = Depends(get_db)):
    h = await _hold_scoped(db, ctx, "checkout.create", hold_id)
    if h.status != "HELD":
        raise Conflict("HOLD_NOT_HELD", "Somente reservas não pagas podem ser liberadas aqui.",
                       details={"status": h.status})
    res = await inv.release_hold(db, hold_id, "CUSTOMER_RELEASED")
    await audit(db, ctx, "inventory.hold_release_requested", "inventory_hold", hold_id, reason=body.reason,
                org_id=h.org_id)
    return jz(res.to_dict())


# ─── Redistribuição por remoção definitiva (#1116) ───────────────────────────

class ResolveIn(BaseModel):
    resolution: str = Field(min_length=10, max_length=4000)


def _ret(r: ScreenRetirement) -> dict:
    return {"id": str(r.id), "screen_id": str(r.screen_id),
            "establishment_id": str(r.establishment_id) if r.establishment_id else None, "reason": r.reason,
            "affected": r.affected, "status": r.status, "resolution": r.resolution,
            "resolved_by": str(r.resolved_by) if r.resolved_by else None, "resolved_at": r.resolved_at,
            "created_at": r.created_at}


@router.get("/retirements")
async def list_retirements(status: Optional[str] = Query(None, pattern="^(OPEN|RESOLVED)$"),
                           page: int = Query(1, ge=1), page_size: int = Query(25, ge=1, le=100),
                           ctx: AuthContext = Depends(require("inventory.view", global_only=True)),
                           db: AsyncSession = Depends(get_db)):
    q = select(ScreenRetirement)
    if status:
        q = q.where(ScreenRetirement.status == status)
    total = (await db.execute(select(func.count()).select_from(q.subquery()))).scalar_one()
    rows = (await db.execute(q.order_by(ScreenRetirement.created_at.desc()).offset((page - 1) * page_size)
                             .limit(page_size))).scalars()
    return jz({"items": [_ret(r) for r in rows], "total": total, "page": page, "page_size": page_size})


@router.post("/retirements/{report_id}/resolve")
async def resolve_retirement(report_id: uuid.UUID, body: ResolveIn,
                             ctx: AuthContext = Depends(require("screens.manage", global_only=True,
                                                                allow_impersonation=False)),
                             db: AsyncSession = Depends(get_db)):
    r = (await db.execute(select(ScreenRetirement).where(ScreenRetirement.id == report_id).with_for_update())
         ).scalar_one_or_none()
    if r is None:
        raise NotFound()
    if r.status != "OPEN":
        raise Conflict("RETIREMENT_ALREADY_RESOLVED", "Relatório já resolvido.")
    before = _ret(r)
    r.status, r.resolution, r.resolved_by, r.resolved_at = "RESOLVED", body.resolution, ctx.user_id, await inv.db_now(db)
    await db.flush()
    await audit(db, ctx, "inventory.retirement_resolved", "screen_retirement", r.id, before=before, after=_ret(r),
                reason=body.resolution)
    return jz(_ret(r))
