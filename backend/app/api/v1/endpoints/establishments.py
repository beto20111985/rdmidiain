"""
RD Mídia Indoor — Estabelecimentos (W-ORG; AÇÃO 002).

Rotas (prefixo ``/api/v1/establishments``):
- ``GET ""`` (``establishments.view``; escopo ORGANIZATION/ESTABLISHMENT/CITY) com filtros
  ``org_id,state,city_key,region,category_id,is_active,onboarding_status,search``.
- ``POST ""`` (``establishments.manage``) — timezone validado ou sugerido por UF/cidade.
- ``GET /timezone-suggestion?state=&city=`` — sugestão de fuso IANA.
- ``GET/PATCH /{id}``; ``PUT /{id}/geo`` (coordenadas MANUAL|GPS);
  ``PUT /{id}/segments``, ``PUT /{id}/products``; ``POST /{id}/deactivate|reactivate``;
  ``POST /{id}/audience-estimates`` / ``GET`` (audiência ESTIMADA, nunca medida).
O anunciante NÃO tem ``establishments.view`` — para ele há só ``/catalog/availability``
(contagens agregadas, sem nomes/endereços).
"""
from __future__ import annotations

import uuid
from datetime import date
from typing import Optional

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, EmailStr, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.authz import AuthContext, require
from app.core.database import get_db
from app.core.errors import DomainError
from app.models.establishments import Establishment
from app.models.screens import AudienceEstimate
from app.modules.establishments import service as svc
from app.modules.establishments.geo import suggest_timezone
from app.modules.organizations.common import iso, paginate, publish_safe
from app.modules.screens.service import screens_of

router = APIRouter()


class EstablishmentIn(BaseModel):
    org_id: uuid.UUID
    name: str = Field(min_length=2, max_length=255)
    trade_name: Optional[str] = Field(default=None, max_length=255)
    legal_name: Optional[str] = Field(default=None, max_length=255)
    document: Optional[str] = Field(default=None, max_length=20)
    document_type: Optional[str] = Field(default=None, pattern="^(CPF|CNPJ)$")
    responsible_name: Optional[str] = Field(default=None, max_length=255)
    phone: Optional[str] = Field(default=None, max_length=30)
    email: Optional[EmailStr] = None
    address: Optional[str] = Field(default=None, max_length=255)
    number: Optional[str] = Field(default=None, max_length=20)
    complement: Optional[str] = Field(default=None, max_length=100)
    neighborhood: Optional[str] = Field(default=None, max_length=100)
    city: str = Field(min_length=2, max_length=100)
    state: str = Field(min_length=2, max_length=2)
    city_ibge_code: Optional[str] = Field(default=None, pattern=r"^\d{7}$")
    postal_code: Optional[str] = Field(default=None, max_length=10)
    lat: Optional[float] = None
    lng: Optional[float] = None
    geo_source: Optional[str] = Field(default=None, pattern="^(MANUAL|GPS)$")
    geo_accuracy_m: Optional[int] = Field(default=None, ge=0, le=100000)
    timezone: Optional[str] = Field(default=None, max_length=64)
    category_id: Optional[uuid.UUID] = None
    segment_ids: Optional[list[uuid.UUID]] = Field(default=None, max_length=200)
    venue_class: Optional[str] = Field(default=None, pattern="^(STANDARD|PREMIUM)$")
    weight_milli: Optional[int] = Field(default=None, gt=0, le=100000)
    partner_plan_id: Optional[uuid.UUID] = None
    receives_commission: bool = True
    pays_monthly_fee: bool = False
    monthly_fee_cents: Optional[int] = Field(default=None, ge=0)
    responsible_technician_id: Optional[uuid.UUID] = None
    installation_date: Optional[date] = None
    operational_config: Optional[dict] = None
    notes: Optional[str] = Field(default=None, max_length=4000)


class EstablishmentPatch(BaseModel):
    name: Optional[str] = Field(default=None, min_length=2, max_length=255)
    trade_name: Optional[str] = Field(default=None, max_length=255)
    legal_name: Optional[str] = Field(default=None, max_length=255)
    document: Optional[str] = Field(default=None, max_length=20)
    document_type: Optional[str] = Field(default=None, pattern="^(CPF|CNPJ)$")
    responsible_name: Optional[str] = Field(default=None, max_length=255)
    phone: Optional[str] = Field(default=None, max_length=30)
    email: Optional[EmailStr] = None
    address: Optional[str] = Field(default=None, max_length=255)
    number: Optional[str] = Field(default=None, max_length=20)
    complement: Optional[str] = Field(default=None, max_length=100)
    neighborhood: Optional[str] = Field(default=None, max_length=100)
    city: Optional[str] = Field(default=None, min_length=2, max_length=100)
    state: Optional[str] = Field(default=None, min_length=2, max_length=2)
    city_ibge_code: Optional[str] = Field(default=None, pattern=r"^\d{7}$")
    postal_code: Optional[str] = Field(default=None, max_length=10)
    timezone: Optional[str] = Field(default=None, max_length=64)
    category_id: Optional[uuid.UUID] = None
    venue_class: Optional[str] = Field(default=None, pattern="^(STANDARD|PREMIUM)$")
    weight_milli: Optional[int] = Field(default=None, gt=0, le=100000)
    partner_plan_id: Optional[uuid.UUID] = None
    receives_commission: Optional[bool] = None
    pays_monthly_fee: Optional[bool] = None
    monthly_fee_cents: Optional[int] = Field(default=None, ge=0)
    responsible_technician_id: Optional[uuid.UUID] = None
    installation_date: Optional[date] = None
    operational_config: Optional[dict] = None
    notes: Optional[str] = Field(default=None, max_length=4000)
    reason: Optional[str] = Field(default=None, max_length=2000)


class GeoIn(BaseModel):
    lat: float = Field(ge=-90, le=90)
    lng: float = Field(ge=-180, le=180)
    source: str = Field(pattern="^(MANUAL|GPS)$")
    accuracy_m: Optional[int] = Field(default=None, ge=0, le=100000)


class IdsIn(BaseModel):
    ids: list[uuid.UUID] = Field(default_factory=list, max_length=200)


class ReasonIn(BaseModel):
    reason: str = Field(min_length=10, max_length=2000)


class AudienceIn(BaseModel):
    method: str = Field(pattern="^(MANUAL|FLOW_ESTIMATE)$")
    daily_people: Optional[int] = Field(default=None, ge=0, le=10_000_000)
    daily_visitors: Optional[int] = Field(default=None, ge=0, le=10_000_000)
    visibility_bps: Optional[int] = Field(default=None, gt=0, le=10000)
    note: Optional[str] = Field(default=None, max_length=1000)


async def _publish(est: Establishment, change: str, extra: Optional[dict] = None) -> None:
    await publish_safe("establishment.updated", {"id": str(est.id), "org_id": str(est.org_id), "change": change,
                                                 **(extra or {})}, **svc.est_scope(est))


async def _publish_screens(est: Establishment, screens: list, change: str) -> None:
    for s in screens:
        await publish_safe("screen.updated", {"id": str(s.id), "friendly_id": s.friendly_id, "status": s.status,
                                              "sellable": s.sellable, "change": change}, **svc.est_scope(est))


@router.get("/timezone-suggestion")
async def timezone_suggestion(state: str = Query(min_length=2, max_length=2), city: Optional[str] = Query(None),
                              ctx: AuthContext = Depends(require("establishments.view"))):
    try:
        return suggest_timezone(state, city)
    except ValueError:
        raise DomainError("STATE_INVALID", "UF inválida.", status=400) from None


@router.get("")
async def list_establishments(
    page: int = Query(1, ge=1),
    page_size: int = Query(25, ge=1, le=100),
    org_id: Optional[uuid.UUID] = None,
    state: Optional[str] = Query(None, min_length=2, max_length=2),
    city_key: Optional[str] = Query(None, max_length=120),
    region: Optional[str] = Query(None, pattern="^(NORTE|NORDESTE|CENTRO_OESTE|SUDESTE|SUL)$"),
    category_id: Optional[uuid.UUID] = None,
    is_active: Optional[bool] = None,
    onboarding_status: Optional[str] = Query(None, pattern="^(AWAITING_CONTRACT|ACTIVE)$"),
    partner_plan_id: Optional[uuid.UUID] = None,
    search: Optional[str] = Query(None, max_length=100),
    sort: str = Query("name", pattern="^-?(name|created_at|city)$"),
    ctx: AuthContext = Depends(require("establishments.view")),
    db: AsyncSession = Depends(get_db),
):
    from app.core.authz import normalize_city_key

    q = select(Establishment).where(Establishment.deleted_at.is_(None), svc.est_filter(ctx, "establishments.view"))
    if org_id:
        q = q.where(Establishment.org_id == org_id)
    if state:
        q = q.where(Establishment.state == state.upper())
    if city_key:
        q = q.where(Establishment.city_key == normalize_city_key(city_key))
    if region:
        q = q.where(Establishment.region == region)
    if category_id:
        q = q.where(Establishment.category_id == category_id)
    if is_active is not None:
        q = q.where(Establishment.is_active.is_(is_active))
    if onboarding_status:
        q = q.where(Establishment.onboarding_status == onboarding_status)
    if partner_plan_id:
        q = q.where(Establishment.partner_plan_id == partner_plan_id)
    if search:
        t = f"%{search.strip()}%"
        q = q.where(Establishment.name.ilike(t) | Establishment.trade_name.ilike(t) | Establishment.city.ilike(t))
    key = sort.lstrip("-")
    col = {"name": Establishment.name, "created_at": Establishment.created_at, "city": Establishment.city}[key]
    q = q.order_by(col.desc() if sort.startswith("-") else col.asc(), Establishment.id)
    return await paginate(db, q, page, page_size, svc.establishment_out)


@router.post("", status_code=201)
async def create_establishment(body: EstablishmentIn,
                               ctx: AuthContext = Depends(require("establishments.manage")),
                               db: AsyncSession = Depends(get_db)):
    est = await svc.create_establishment(db, ctx, body.model_dump())
    seg = await svc.segment_ids_of(db, est.id)
    await db.commit()
    await _publish(est, "created")
    return svc.establishment_out(est, segment_ids=seg, product_ids=[])


@router.get("/{est_id}")
async def get_establishment(est_id: uuid.UUID, ctx: AuthContext = Depends(require("establishments.view")),
                            db: AsyncSession = Depends(get_db)):
    est = await svc.get_est(db, est_id)
    svc.ensure_est(ctx, "establishments.view", est)
    screens = await screens_of(db, est.id)
    out = svc.establishment_out(est, segment_ids=await svc.segment_ids_of(db, est.id),
                                product_ids=await svc.product_ids_of(db, est.id))
    out["screens_count"] = len([s for s in screens if s.status != "DECOMMISSIONED"])
    out["screens_sellable"] = len([s for s in screens if s.sellable])
    return out


@router.patch("/{est_id}")
async def update_establishment(est_id: uuid.UUID, body: EstablishmentPatch,
                               ctx: AuthContext = Depends(require("establishments.manage")),
                               db: AsyncSession = Depends(get_db)):
    est = await svc.get_est(db, est_id, lock=True)
    svc.ensure_est(ctx, "establishments.manage", est)
    changes = await svc.update_establishment(db, ctx, est, body.model_dump(exclude_unset=True))
    if {"timezone", "city_key", "state"} & set(changes):
        from app.modules.screens.service import request_manifest_refresh

        live = [s.id for s in await screens_of(db, est.id) if s.status != "DECOMMISSIONED"]
        await request_manifest_refresh(db, live, "establishment_timezone_or_city")
    await db.commit()
    if changes:
        await _publish(est, "updated", {"fields": sorted(changes)})
    return svc.establishment_out(est, segment_ids=await svc.segment_ids_of(db, est.id),
                                 product_ids=await svc.product_ids_of(db, est.id))


@router.put("/{est_id}/geo")
async def set_geo(est_id: uuid.UUID, body: GeoIn, ctx: AuthContext = Depends(require("establishments.manage")),
                  db: AsyncSession = Depends(get_db)):
    est = await svc.get_est(db, est_id, lock=True)
    svc.ensure_est(ctx, "establishments.manage", est)
    await svc.update_establishment(db, ctx, est, {"lat": body.lat, "lng": body.lng, "geo_source": body.source,
                                                  "geo_accuracy_m": body.accuracy_m})
    await db.commit()
    await _publish(est, "geo")
    return svc.establishment_out(est)


@router.put("/{est_id}/segments")
async def set_segments(est_id: uuid.UUID, body: IdsIn, ctx: AuthContext = Depends(require("establishments.manage")),
                       db: AsyncSession = Depends(get_db)):
    est = await svc.get_est(db, est_id, lock=True)
    svc.ensure_est(ctx, "establishments.manage", est)
    ids = await svc.set_segments(db, ctx, est, body.ids)
    await db.commit()
    await _publish(est, "segments")
    return {"segment_ids": sorted(str(i) for i in ids)}


@router.put("/{est_id}/products")
async def set_products(est_id: uuid.UUID, body: IdsIn, ctx: AuthContext = Depends(require("establishments.manage")),
                       db: AsyncSession = Depends(get_db)):
    est = await svc.get_est(db, est_id, lock=True)
    svc.ensure_est(ctx, "establishments.manage", est)
    ids = await svc.set_products(db, ctx, est, body.ids)
    await db.commit()
    await _publish(est, "products")
    return {"product_ids": sorted(str(i) for i in ids)}


@router.post("/{est_id}/deactivate")
async def deactivate(est_id: uuid.UUID, body: ReasonIn,
                     ctx: AuthContext = Depends(require("establishments.manage", allow_impersonation=False)),
                     db: AsyncSession = Depends(get_db)):
    from app.modules.screens.service import deactivate_establishment

    est = await svc.get_est(db, est_id, lock=True)
    svc.ensure_est(ctx, "establishments.manage", est)
    screens = await deactivate_establishment(db, ctx, est, body.reason)
    await db.commit()
    await _publish(est, "deactivated")
    await _publish_screens(est, screens, "establishment_deactivated")
    return svc.establishment_out(est)


@router.post("/{est_id}/reactivate")
async def reactivate(est_id: uuid.UUID, body: ReasonIn,
                     ctx: AuthContext = Depends(require("establishments.manage", allow_impersonation=False)),
                     db: AsyncSession = Depends(get_db)):
    from app.modules.screens.service import reactivate_establishment

    est = await svc.get_est(db, est_id, lock=True)
    svc.ensure_est(ctx, "establishments.manage", est)
    screens = await reactivate_establishment(db, ctx, est, body.reason)
    await db.commit()
    await _publish(est, "reactivated")
    await _publish_screens(est, screens, "establishment_reactivated")
    return svc.establishment_out(est)


def _audience_out(a: AudienceEstimate) -> dict:
    return {
        "id": str(a.id), "subject_type": a.subject_type, "screen_id": str(a.screen_id) if a.screen_id else None,
        "establishment_id": str(a.establishment_id), "daily_people": a.daily_people, "is_estimate": True,
        "method": a.method, "basis": a.basis, "note": a.note, "created_at": iso(a.created_at),
        "label": "Audiência estimada (pessoas/dia) — não é medição",
    }


@router.post("/{est_id}/audience-estimates", status_code=201)
async def add_audience(est_id: uuid.UUID, body: AudienceIn,
                       ctx: AuthContext = Depends(require("establishments.manage")),
                       db: AsyncSession = Depends(get_db)):
    est = await svc.get_est(db, est_id, lock=True)
    svc.ensure_est(ctx, "establishments.manage", est)
    ae = await svc.record_audience(db, ctx, est, method=body.method, daily_people=body.daily_people,
                                   daily_visitors=body.daily_visitors, visibility_bps=body.visibility_bps,
                                   note=body.note)
    await db.commit()
    await _publish(est, "audience")
    return _audience_out(ae)


@router.get("/{est_id}/audience-estimates")
async def list_audience(est_id: uuid.UUID, page: int = Query(1, ge=1), page_size: int = Query(25, ge=1, le=100),
                        ctx: AuthContext = Depends(require("establishments.view")),
                        db: AsyncSession = Depends(get_db)):
    est = await svc.get_est(db, est_id)
    svc.ensure_est(ctx, "establishments.view", est)
    q = select(AudienceEstimate).where(AudienceEstimate.establishment_id == est.id,
                                       AudienceEstimate.subject_type == "ESTABLISHMENT").order_by(
        AudienceEstimate.created_at.desc())
    return await paginate(db, q, page, page_size, _audience_out)

