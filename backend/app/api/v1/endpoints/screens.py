"""
RD Mídia Indoor — Telas (cadastro/hardware/inventário) e catálogo de disponibilidade (W-ORG).

Rotas ``/api/v1/screens`` (escopo pelo estabelecimento da tela: ORGANIZATION/ESTABLISHMENT/CITY):
- ``GET ""`` | ``POST ""`` (gera ``TEL-<UF>-XXXX`` único e imutável)
- ``GET/PATCH /{id}`` (``friendly_id`` NÃO é editável; campo desconhecido ⇒ 422)
- ``POST /{id}/status`` (ACTIVE | MAINTENANCE | INACTIVE), ``POST /{id}/move``,
  ``POST /{id}/replace-hardware``, ``POST /{id}/decommission`` (retirada definitiva),
  ``GET /{id}/history``, ``GET /{id}/equipment``, ``POST|GET /{id}/audience-estimates``.

Rotas ``/api/v1/catalog``:
- ``GET /availability`` — disponibilidade AGREGADA (contagem de telas/estabelecimentos por
  estado/cidade/região/categoria), SEM nomes nem endereços (ETAPA2 #139–#143; P001 §46).
  Permissão ``campaigns.view`` ou ``checkout.create``.

As rotas antigas de manifesto/sync/comando/status de player (``/screens/{id}/manifest``,
``/manifest/generate``, ``/sync``, ``/command``, ``/status``) foram REMOVIDAS daqui:
substituídas pela API do player ``/api/v1/player/*`` e pelos comandos/presença do W-PLAY.
"""
from __future__ import annotations

import uuid
from datetime import datetime
from typing import Optional

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import Integer, and_, case, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.audit import audit
from app.core.authz import AuthContext, normalize_city_key, require
from app.core.database import get_db
from app.core.errors import DomainError, NotFound
from app.models.establishments import Establishment
from app.models.screens import AudienceEstimate, Screen, ScreenHistory, ScreenHistoryEvent
from app.models.taxonomy import TaxonomyNode
from app.modules.establishments import service as est_svc
from app.modules.establishments.geo import REGION_LABELS
from app.modules.organizations.common import iso, paginate, publish_safe, require_any
from app.modules.screens import hardware as hw
from app.modules.screens import service as svc

router = APIRouter()
catalog_router = APIRouter()

ORIENTATION = "^(HORIZONTAL|VERTICAL)$"
DISPLAY = "^(TV|MONITOR|LED_PANEL|PROJECTOR|OTHER)$"
PLAYER = "^(TVBOX|SMART_TV_APP|PC|BROWSER|OTHER)$"


class ScreenIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    establishment_id: uuid.UUID
    name: str = Field(min_length=2, max_length=255)
    orientation: str = Field(default="HORIZONTAL", pattern=ORIENTATION)
    display_type: str = Field(default="TV", pattern=DISPLAY)
    player_type: str = Field(default="TVBOX", pattern=PLAYER)
    hardware_brand: Optional[str] = Field(default=None, max_length=100)
    hardware_model: Optional[str] = Field(default=None, max_length=100)
    inches: Optional[int] = Field(default=None, gt=0, le=500)
    resolution_w: Optional[int] = Field(default=1920, gt=0, le=16384)
    resolution_h: Optional[int] = Field(default=1080, gt=0, le=16384)
    serial_number: Optional[str] = Field(default=None, max_length=100)
    asset_tag: Optional[str] = Field(default=None, max_length=100)
    player_brand: Optional[str] = Field(default=None, max_length=100)
    player_model: Optional[str] = Field(default=None, max_length=100)
    player_serial_number: Optional[str] = Field(default=None, max_length=100)
    installed_at: Optional[datetime] = None
    tv_owner: str = Field(default="ESTABLISHMENT", pattern="^(RD|ESTABLISHMENT)$")
    screen_class: str = Field(default="STANDARD", pattern="^(STANDARD|PREMIUM)$")
    weight_milli: int = Field(default=1000, gt=0, le=100000)
    programming_mode: str = Field(default="ESTABLISHMENT_DEFAULT", pattern="^(ESTABLISHMENT_DEFAULT|CUSTOM)$")
    notes: Optional[str] = Field(default=None, max_length=4000)


class ScreenPatch(BaseModel):
    model_config = ConfigDict(extra="forbid")  # friendly_id/establishment_id/status têm rotas próprias

    name: Optional[str] = Field(default=None, min_length=2, max_length=255)
    orientation: Optional[str] = Field(default=None, pattern=ORIENTATION)
    display_type: Optional[str] = Field(default=None, pattern=DISPLAY)
    player_type: Optional[str] = Field(default=None, pattern=PLAYER)
    hardware_brand: Optional[str] = Field(default=None, max_length=100)
    hardware_model: Optional[str] = Field(default=None, max_length=100)
    inches: Optional[int] = Field(default=None, gt=0, le=500)
    resolution_w: Optional[int] = Field(default=None, gt=0, le=16384)
    resolution_h: Optional[int] = Field(default=None, gt=0, le=16384)
    serial_number: Optional[str] = Field(default=None, max_length=100)
    asset_tag: Optional[str] = Field(default=None, max_length=100)
    player_brand: Optional[str] = Field(default=None, max_length=100)
    player_model: Optional[str] = Field(default=None, max_length=100)
    player_serial_number: Optional[str] = Field(default=None, max_length=100)
    installed_at: Optional[datetime] = None
    tv_owner: Optional[str] = Field(default=None, pattern="^(RD|ESTABLISHMENT)$")
    screen_class: Optional[str] = Field(default=None, pattern="^(STANDARD|PREMIUM)$")
    weight_milli: Optional[int] = Field(default=None, gt=0, le=100000)
    programming_mode: Optional[str] = Field(default=None, pattern="^(ESTABLISHMENT_DEFAULT|CUSTOM)$")
    notes: Optional[str] = Field(default=None, max_length=4000)
    reason: Optional[str] = Field(default=None, max_length=2000)


class StatusIn(BaseModel):
    status: str = Field(pattern="^(ACTIVE|MAINTENANCE|INACTIVE)$")
    reason: str = Field(min_length=10, max_length=2000)


class MoveIn(BaseModel):
    establishment_id: uuid.UUID
    reason: str = Field(min_length=10, max_length=2000)


class ReplaceIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    component: str = Field(pattern="^(TV|PLAYER)$")
    new_equipment_item_id: Optional[uuid.UUID] = None
    old_item_destination: Optional[str] = Field(default=None, pattern="^(WAREHOUSE|MAINTENANCE)$")
    comodato_agreement_id: Optional[uuid.UUID] = None
    hardware: dict = Field(default_factory=dict)
    reason: str = Field(min_length=10, max_length=2000)


class ReasonIn(BaseModel):
    reason: str = Field(min_length=10, max_length=2000)


class AudienceIn(BaseModel):
    method: str = Field(pattern="^(MANUAL|FLOW_ESTIMATE)$")
    daily_people: Optional[int] = Field(default=None, ge=0, le=10_000_000)
    daily_visitors: Optional[int] = Field(default=None, ge=0, le=10_000_000)
    visibility_bps: Optional[int] = Field(default=None, gt=0, le=10000)
    note: Optional[str] = Field(default=None, max_length=1000)


def screen_out(s: Screen, est: Optional[Establishment] = None) -> dict:
    d = {
        "id": str(s.id),
        "friendly_id": s.friendly_id,
        "name": s.name,
        "establishment_id": str(s.establishment_id),
        "status": s.status,
        "sellable": s.sellable,
        "decommissioned_at": iso(s.decommissioned_at),
        "decommission_reason": s.decommission_reason,
        "display_type": s.display_type,
        "player_type": s.player_type,
        "orientation": s.orientation,
        "hardware": {
            "brand": s.hardware_brand, "model": s.hardware_model, "inches": s.inches,
            "resolution_w": s.resolution_w, "resolution_h": s.resolution_h, "serial_number": s.serial_number,
            "asset_tag": s.asset_tag, "player_brand": s.player_brand, "player_model": s.player_model,
            "player_serial_number": s.player_serial_number, "installed_at": iso(s.installed_at),
            "installed_by": str(s.installed_by) if s.installed_by else None,
        },
        "tv_owner": s.tv_owner,
        "device_id": str(s.device_id) if s.device_id else None,
        "screen_class": s.screen_class,
        "weight_milli": s.weight_milli,
        "programming_mode": s.programming_mode,
        "estimated_daily_audience": svc.audience_label(s.estimated_daily_audience, s.audience_method),
        "notes": s.notes,
        "created_at": iso(s.created_at),
        "updated_at": iso(s.updated_at),
    }
    if est is not None:
        d["establishment"] = {"id": str(est.id), "name": est.name, "org_id": str(est.org_id), "city": est.city,
                              "state": est.state, "city_key": est.city_key, "timezone": est.timezone,
                              "is_active": est.is_active, "onboarding_status": est.onboarding_status}
    return d


async def _load(db: AsyncSession, ctx: AuthContext, perm: str, screen_id: uuid.UUID, *,
                lock: bool = False) -> tuple[Screen, Establishment]:
    s = await (svc.lock_screen(db, screen_id) if lock else db.get(Screen, screen_id))
    if s is None:
        raise NotFound()
    est = await db.get(Establishment, s.establishment_id)
    est_svc.ensure_est(ctx, perm, est)
    return s, est


async def _publish(s: Screen, est: Establishment, change: str) -> None:
    await publish_safe("screen.updated", {"id": str(s.id), "friendly_id": s.friendly_id, "status": s.status,
                                          "sellable": s.sellable, "establishment_id": str(est.id), "change": change},
                       **svc.est_scope(est))


@router.get("")
async def list_screens(
    page: int = Query(1, ge=1),
    page_size: int = Query(25, ge=1, le=100),
    establishment_id: Optional[uuid.UUID] = None,
    org_id: Optional[uuid.UUID] = None,
    status: Optional[str] = Query(None, pattern="^(ACTIVE|MAINTENANCE|INACTIVE|DECOMMISSIONED)$"),
    sellable: Optional[bool] = None,
    state: Optional[str] = Query(None, min_length=2, max_length=2),
    city_key: Optional[str] = Query(None, max_length=120),
    screen_class: Optional[str] = Query(None, pattern="^(STANDARD|PREMIUM)$"),
    search: Optional[str] = Query(None, max_length=100),
    ctx: AuthContext = Depends(require("screens.view")),
    db: AsyncSession = Depends(get_db),
):
    q = (
        select(Screen, Establishment)
        .join(Establishment, Establishment.id == Screen.establishment_id)
        .where(ctx.scope_filter("screens.view", org_col=Establishment.org_id, establishment_col=Establishment.id,
                                city_col=Establishment.city_key))
    )
    if establishment_id:
        q = q.where(Screen.establishment_id == establishment_id)
    if org_id:
        q = q.where(Establishment.org_id == org_id)
    if status:
        q = q.where(Screen.status == status)
    if sellable is not None:
        q = q.where(Screen.sellable.is_(sellable))
    if state:
        q = q.where(Establishment.state == state.upper())
    if city_key:
        q = q.where(Establishment.city_key == normalize_city_key(city_key))
    if screen_class:
        q = q.where(Screen.screen_class == screen_class)
    if search:
        t = f"%{search.strip()}%"
        q = q.where(Screen.friendly_id.ilike(t) | Screen.name.ilike(t))
    q = q.order_by(Screen.friendly_id)
    return await paginate(db, q, page, page_size, lambda row: screen_out(row[0], row[1]), scalars=False)


@router.post("", status_code=201)
async def create_screen(body: ScreenIn, ctx: AuthContext = Depends(require("screens.manage")),
                        db: AsyncSession = Depends(get_db)):
    est = await est_svc.get_est(db, body.establishment_id, lock=True)
    est_svc.ensure_est(ctx, "screens.manage", est)
    if not est.is_active:
        raise DomainError("ESTABLISHMENT_INACTIVE", "Estabelecimento inativo.", status=400)
    data = body.model_dump(exclude={"establishment_id"})
    s = Screen(id=uuid.uuid4(), establishment_id=est.id, friendly_id=await svc.new_friendly_id(db, est.state),
               status="ACTIVE", created_by=ctx.user_id, **data)
    s.sellable = svc.compute_sellable(s, est)
    db.add(s)
    await db.flush()
    await svc.add_history(db, s, ScreenHistoryEvent.CREATED, actor_user_id=ctx.user_id,
                          after={"friendly_id": s.friendly_id, "establishment_id": str(est.id),
                                 **svc.snap(s, svc.HARDWARE_FIELDS)})
    await audit(db, ctx, "screen.created", "screen", s.id, after=screen_out(s), org_id=est.org_id)
    await db.commit()
    await _publish(s, est, "created")
    return screen_out(s, est)


@router.get("/{screen_id}")
async def get_screen(screen_id: uuid.UUID, ctx: AuthContext = Depends(require("screens.view")),
                     db: AsyncSession = Depends(get_db)):
    from app.modules.assets.service import installed_items, item_out

    s, est = await _load(db, ctx, "screens.view", screen_id)
    out = screen_out(s, est)
    out["equipment"] = [item_out(i) for i in await installed_items(db, s.id)] if ctx.can(
        "equipment.view", org_id=est.org_id, establishment_id=est.id, city=est.city_key) else None
    return out


@router.patch("/{screen_id}")
async def update_screen(screen_id: uuid.UUID, body: ScreenPatch, ctx: AuthContext = Depends(require("screens.manage")),
                        db: AsyncSession = Depends(get_db)):
    s, est = await _load(db, ctx, "screens.manage", screen_id, lock=True)
    if s.status == "DECOMMISSIONED":
        raise DomainError("SCREEN_DECOMMISSIONED", "Tela retirada definitivamente.", status=409)
    data = body.model_dump(exclude_unset=True)
    reason = data.pop("reason", None)
    hw_changes = {k: v for k, v in data.items() if k in svc.HARDWARE_FIELDS}
    other = {k: v for k, v in data.items() if k not in svc.HARDWARE_FIELDS}
    await hw.change_hardware(db, ctx, s, est, hw_changes, reason)
    before, after = {}, {}
    for k, v in other.items():
        if getattr(s, k) != v:
            before[k], after[k] = getattr(s, k), v
            setattr(s, k, v)
    if after:
        await svc.add_history(db, s, ScreenHistoryEvent.UPDATED, actor_user_id=ctx.user_id, before=before,
                              after=after, reason=reason)
        await audit(db, ctx, "screen.updated", "screen", s.id, before=before, after=after, reason=reason,
                    org_id=est.org_id)
    await db.flush()
    if {"orientation", "programming_mode", "resolution_w", "resolution_h"} & (set(hw_changes) | set(after)):
        await svc.request_manifest_refresh(db, [s.id], "screen_layout_changed")
    await db.commit()
    await db.refresh(s)
    await _publish(s, est, "updated")
    return screen_out(s, est)


@router.post("/{screen_id}/status")
async def set_screen_status(screen_id: uuid.UUID, body: StatusIn, ctx: AuthContext = Depends(require("screens.manage")),
                            db: AsyncSession = Depends(get_db)):
    s, est = await _load(db, ctx, "screens.manage", screen_id, lock=True)
    changed = await svc.set_status(db, ctx, s, est, body.status, body.reason)
    await db.commit()
    await db.refresh(s)
    if changed:
        await _publish(s, est, "status")
    return screen_out(s, est)


@router.post("/{screen_id}/move")
async def move_screen(screen_id: uuid.UUID, body: MoveIn, ctx: AuthContext = Depends(require("screens.manage")),
                      db: AsyncSession = Depends(get_db)):
    s, old_est = await _load(db, ctx, "screens.manage", screen_id, lock=True)
    new_est = await est_svc.get_est(db, body.establishment_id)
    est_svc.ensure_est(ctx, "screens.manage", new_est)
    await svc.move_screen(db, ctx, s, old_est, new_est, body.reason)
    await db.commit()
    await db.refresh(s)
    await _publish(s, old_est, "moved_out")
    await _publish(s, new_est, "moved_in")
    return screen_out(s, new_est)


@router.post("/{screen_id}/replace-hardware")
async def replace_hardware(screen_id: uuid.UUID, body: ReplaceIn,
                           ctx: AuthContext = Depends(require("screens.manage")),
                           db: AsyncSession = Depends(get_db)):
    s, est = await _load(db, ctx, "screens.manage", screen_id, lock=True)
    if body.new_equipment_item_id or body.old_item_destination:
        if not ctx.can("equipment.manage", org_id=est.org_id, establishment_id=est.id, city=est.city_key):
            raise NotFound()
    bad = [k for k in body.hardware if k not in (svc.TV_FIELDS if body.component == "TV" else svc.PLAYER_FIELDS)]
    if bad:
        raise DomainError("HARDWARE_FIELD_INVALID", "Campo de hardware não pertence ao componente.", status=400,
                          details={"fields": bad})
    result = await hw.replace_component(
        db, ctx, s, est, component=body.component, new_item_id=body.new_equipment_item_id,
        old_item_destination=body.old_item_destination, hardware=body.hardware, reason=body.reason,
        comodato_agreement_id=body.comodato_agreement_id,
    )
    await db.commit()
    await db.refresh(s)
    await _publish(s, est, "hardware_replaced")
    return {"screen": screen_out(s, est), **result}


@router.post("/{screen_id}/decommission")
async def decommission_screen(screen_id: uuid.UUID, body: ReasonIn,
                              ctx: AuthContext = Depends(require("screens.manage", allow_impersonation=False)),
                              db: AsyncSession = Depends(get_db)):
    s, est = await _load(db, ctx, "screens.manage", screen_id, lock=True)
    await svc.decommission(db, ctx, s, est, body.reason)
    await db.commit()
    await db.refresh(s)
    await _publish(s, est, "decommissioned")
    return screen_out(s, est)


def _history_out(h: ScreenHistory) -> dict:
    return {
        "id": str(h.id), "event": h.event, "establishment_id": str(h.establishment_id),
        "from_establishment_id": str(h.from_establishment_id) if h.from_establishment_id else None,
        "before": h.before_data, "after": h.after_data, "reason": h.reason,
        "actor_user_id": str(h.actor_user_id) if h.actor_user_id else None, "created_at": iso(h.created_at),
    }


@router.get("/{screen_id}/history")
async def screen_history(screen_id: uuid.UUID, page: int = Query(1, ge=1), page_size: int = Query(50, ge=1, le=100),
                         ctx: AuthContext = Depends(require("screens.view")), db: AsyncSession = Depends(get_db)):
    s, est = await _load(db, ctx, "screens.view", screen_id)
    q = select(ScreenHistory).where(ScreenHistory.screen_id == s.id).order_by(ScreenHistory.created_at.desc(),
                                                                            ScreenHistory.id)
    return await paginate(db, q, page, page_size, _history_out)


@router.get("/{screen_id}/equipment")
async def screen_equipment(screen_id: uuid.UUID, ctx: AuthContext = Depends(require("equipment.view")),
                           db: AsyncSession = Depends(get_db)):
    from app.modules.assets.service import installed_items, item_out

    s, est = await _load(db, ctx, "equipment.view", screen_id)
    items = await installed_items(db, s.id)
    return {"items": [item_out(i) for i in items], "total": len(items), "page": 1, "page_size": max(len(items), 1)}


@router.post("/{screen_id}/audience-estimates", status_code=201)
async def add_screen_audience(screen_id: uuid.UUID, body: AudienceIn,
                              ctx: AuthContext = Depends(require("screens.manage")),
                              db: AsyncSession = Depends(get_db)):
    s, est = await _load(db, ctx, "screens.manage", screen_id, lock=True)
    ae = await est_svc.record_audience(db, ctx, est, method=body.method, daily_people=body.daily_people,
                                       daily_visitors=body.daily_visitors, visibility_bps=body.visibility_bps,
                                       note=body.note, screen=s)
    await db.commit()
    await db.refresh(s)
    await _publish(s, est, "audience")
    return {"id": str(ae.id), "screen_id": str(s.id), "daily_people": ae.daily_people, "is_estimate": True,
            "method": ae.method, "basis": ae.basis, "created_at": iso(ae.created_at),
            "label": "Audiência estimada (pessoas/dia) — não é medição"}


@router.get("/{screen_id}/audience-estimates")
async def list_screen_audience(screen_id: uuid.UUID, page: int = Query(1, ge=1),
                               page_size: int = Query(25, ge=1, le=100),
                               ctx: AuthContext = Depends(require("screens.view")), db: AsyncSession = Depends(get_db)):
    s, est = await _load(db, ctx, "screens.view", screen_id)
    q = select(AudienceEstimate).where(AudienceEstimate.screen_id == s.id).order_by(AudienceEstimate.created_at.desc())
    return await paginate(db, q, page, page_size, lambda a: {
        "id": str(a.id), "daily_people": a.daily_people, "is_estimate": True, "method": a.method, "basis": a.basis,
        "note": a.note, "created_at": iso(a.created_at)})


# ─── Catálogo de disponibilidade (anunciante/checkout) ───────────────────────

GROUPS = {
    "state": ("state",),
    "city": ("state", "city_key"),
    "region": ("region",),
    "category": ("category_id",),
    "city_category": ("state", "city_key", "category_id"),
}


@catalog_router.get("/availability")
async def availability(
    group_by: str = Query("city", pattern="^(state|city|region|category|city_category)$"),
    country: str = Query("BRA", pattern="^BRA$"),
    region: Optional[str] = Query(None, pattern="^(NORTE|NORDESTE|CENTRO_OESTE|SUDESTE|SUL)$"),
    state: Optional[str] = Query(None, min_length=2, max_length=2),
    city_key: Optional[list[str]] = Query(None),
    neighborhood: Optional[str] = Query(None, max_length=100),
    postal_code_prefix: Optional[str] = Query(None, pattern=r"^\d{2,8}$"),
    category_id: Optional[uuid.UUID] = None,
    screen_class: Optional[str] = Query(None, pattern="^(STANDARD|PREMIUM)$"),
    orientation: Optional[str] = Query(None, pattern=ORIENTATION),
    ctx: AuthContext = Depends(require_any("campaigns.view", "checkout.create")),
    db: AsyncSession = Depends(get_db),
):
    """Contagens agregadas de telas VENDÁVEIS (sellable) — nunca nomes/endereços/ids de estabelecimento."""
    from app.modules.establishments.geo import slug
    from app.modules.taxonomy.service import node_descendant_ids

    E, S = Establishment, Screen
    conds = [S.sellable.is_(True), E.is_active.is_(True), E.deleted_at.is_(None), E.country == country]
    if region:
        conds.append(E.region == region)
    if state:
        conds.append(E.state == state.upper())
    if city_key:
        conds.append(E.city_key.in_([normalize_city_key(c) for c in city_key][:50]))
    if neighborhood:
        conds.append(E.neighborhood_key == slug(neighborhood))
    if postal_code_prefix:
        conds.append(E.postal_code.like(postal_code_prefix + "%"))
    if category_id:
        conds.append(E.category_id.in_(await node_descendant_ids(db, [category_id]) or {category_id}))
    if screen_class:
        conds.append(S.screen_class == screen_class)
    if orientation:
        conds.append(S.orientation == orientation)
    cols = {"state": E.state, "city_key": E.city_key, "region": E.region, "category_id": E.category_id}
    keys = GROUPS[group_by]
    gcols = [cols[k].label(k) for k in keys]
    extra = []
    if "city_key" in keys:
        # centro aproximado (1 casa decimal ≈ 11 km) — nunca a coordenada de um estabelecimento
        extra = [func.min(E.city).label("city"),
                 func.round(func.avg(E.lat).cast(_numeric()), 1).label("approx_lat"),
                 func.round(func.avg(E.lng).cast(_numeric()), 1).label("approx_lng")]
    q = (
        select(*gcols,
               func.count(func.distinct(S.id)).label("screens"),
               func.count(func.distinct(E.id)).label("establishments"),
               func.sum(case((S.screen_class == "PREMIUM", 1), else_=0)).cast(Integer).label("premium_screens"),
               func.sum(case((S.orientation == "VERTICAL", 1), else_=0)).cast(Integer).label("vertical_screens"),
               *extra)
        .select_from(S).join(E, E.id == S.establishment_id)
        .where(and_(*conds))
        .group_by(*[cols[k] for k in keys])
        .order_by(*[cols[k] for k in keys])
    )
    rows = (await db.execute(q)).mappings().all()
    cat_names = {}
    if "category_id" in keys:
        ids = {r["category_id"] for r in rows if r["category_id"]}
        if ids:
            cat_names = {i: n for i, n in (await db.execute(
                select(TaxonomyNode.id, TaxonomyNode.name).where(TaxonomyNode.id.in_(ids)))).all()}
    items = []
    for r in rows:
        it = {"screens": int(r["screens"]), "establishments": int(r["establishments"]),
              "premium_screens": int(r["premium_screens"] or 0), "vertical_screens": int(r["vertical_screens"] or 0)}
        for k in keys:
            v = r[k]
            it[k] = str(v) if isinstance(v, uuid.UUID) else v
        if "region" in keys:
            it["region_label"] = REGION_LABELS.get(r["region"])
        if "category_id" in keys:
            it["category_name"] = cat_names.get(r["category_id"]) if r["category_id"] else None
        if "city_key" in keys:
            it["city"] = r["city"]
            it["approx_center"] = ({"lat": float(r["approx_lat"]), "lng": float(r["approx_lng"])}
                                   if r["approx_lat"] is not None else None)
        items.append(it)
    return {
        "items": items,
        "totals": {"screens": sum(i["screens"] for i in items),
                   "establishments": sum(i["establishments"] for i in items)},
        "group_by": group_by,
        "establishment_names_visible": False,
        "note": "Somente contagens agregadas; nomes e endereços dos estabelecimentos não são revelados.",
    }


def _numeric():
    from sqlalchemy import Numeric

    return Numeric(9, 6)
