"""
RD Mídia Indoor — Estoque/patrimônio (W-ORG; ETAPA2 #204–#210, #1146–#1151; P002 #35).

Rotas ``/api/v1/equipment``:
- ``GET ""`` / ``POST ""`` (entrada no estoque: item SERIAL com série/patrimônio, ou BULK
  com quantidade).
- ``GET /summary`` — contagem por tipo × situação (SERIAL) e saldo por tipo (BULK).
- ``GET/PATCH /{id}`` (detalhe com SALDO DERIVADO do ledger por local).
- ``GET /{id}/movements`` / ``POST /{id}/movements`` — entrada, transferência, instalação,
  empréstimo, comodato, devolução, manutenção, RMA, baixa, ajuste. Movimentações são
  append-only (trigger); nada é editado/apagado.
Escopo: GLOBAL vê tudo; CITY vê estoque/itens da sua cidade; ORGANIZATION/ESTABLISHMENT
vêem só itens instalados/emprestados nos seus estabelecimentos; técnico vê o que está com
ele. Fora do escopo ⇒ 404.
"""
from __future__ import annotations

import uuid
from datetime import date
from typing import Optional

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import case, false, func, or_, select, true
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.audit import audit
from app.core.authz import AuthContext, require
from app.core.database import get_db
from app.core.errors import Conflict, DomainError, NotFound
from app.models.equipment import EquipmentItem, EquipmentMovement, LocationKind, TrackingMode
from app.modules.assets import service as svc
from app.modules.organizations.common import paginate, publish_safe

router = APIRouter()

KINDS = "^(EXTERNAL|WAREHOUSE|TECHNICIAN|ESTABLISHMENT|SCREEN|MAINTENANCE|RMA|DISPOSED)$"


class LocationIn(BaseModel):
    kind: str = Field(pattern=KINDS)
    ref_id: Optional[uuid.UUID] = None
    city_key: Optional[str] = Field(default=None, max_length=120)


class ItemIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    type: str = Field(pattern="^(TV|TVBOX|CABLE|REMOTE|MOUNT|ACCESSORY)$")
    tracking: str = Field(default="SERIAL", pattern="^(SERIAL|BULK)$")
    name: str = Field(min_length=2, max_length=160)
    quantity: int = Field(default=1, ge=0, le=100000)
    warehouse_city_key: Optional[str] = Field(default=None, max_length=120)
    brand: Optional[str] = Field(default=None, max_length=100)
    model: Optional[str] = Field(default=None, max_length=100)
    serial_number: Optional[str] = Field(default=None, max_length=100)
    asset_tag: Optional[str] = Field(default=None, max_length=100)
    inches: Optional[int] = Field(default=None, gt=0, le=500)
    unit: str = Field(default="UN", max_length=10)
    condition: str = Field(default="NEW", pattern="^(NEW|GOOD|DAMAGED|BROKEN)$")
    supplier: Optional[str] = Field(default=None, max_length=160)
    purchase_date: Optional[date] = None
    purchase_cost_cents: Optional[int] = Field(default=None, ge=0)
    warranty_until: Optional[date] = None
    reference: Optional[str] = Field(default=None, max_length=120)
    notes: Optional[str] = Field(default=None, max_length=4000)


class ItemPatch(BaseModel):
    model_config = ConfigDict(extra="forbid")  # type/tracking são imutáveis

    name: Optional[str] = Field(default=None, min_length=2, max_length=160)
    brand: Optional[str] = Field(default=None, max_length=100)
    model: Optional[str] = Field(default=None, max_length=100)
    serial_number: Optional[str] = Field(default=None, max_length=100)
    asset_tag: Optional[str] = Field(default=None, max_length=100)
    inches: Optional[int] = Field(default=None, gt=0, le=500)
    condition: Optional[str] = Field(default=None, pattern="^(NEW|GOOD|DAMAGED|BROKEN)$")
    supplier: Optional[str] = Field(default=None, max_length=160)
    purchase_date: Optional[date] = None
    purchase_cost_cents: Optional[int] = Field(default=None, ge=0)
    warranty_until: Optional[date] = None
    notes: Optional[str] = Field(default=None, max_length=4000)


class MovementIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    movement_type: str = Field(pattern="^(ENTRY|TRANSFER|INSTALL|LOAN|COMODATO|RETURN|MAINTENANCE_IN|MAINTENANCE_OUT"
                                       "|RMA_OUT|RMA_RETURN|WRITE_OFF|ADJUSTMENT)$")
    to: LocationIn
    from_: Optional[LocationIn] = Field(default=None, alias="from")
    quantity: int = Field(default=1, gt=0, le=100000)
    reason: Optional[str] = Field(default=None, max_length=2000)
    reference: Optional[str] = Field(default=None, max_length=120)
    comodato_agreement_id: Optional[uuid.UUID] = None
    expected_return_at: Optional[date] = None


def item_filter(ctx: AuthContext, perm: str):
    is_global, orgs, ests, cities = ctx._scopes(perm)
    if is_global:
        return true()
    clauses = [EquipmentItem.tracking == TrackingMode.BULK.value]  # catálogo de itens de quantidade
    if orgs:
        clauses.append(EquipmentItem.scope_org_id.in_(list(orgs)))
    if ests:
        clauses.append(EquipmentItem.location_establishment_id.in_(list(ests)))
    if cities:
        clauses.append(EquipmentItem.location_city_key.in_(list(cities)))
    if ctx.user_id is not None:
        clauses.append(EquipmentItem.location_user_id == ctx.user_id)
    return or_(*clauses) if clauses else false()


def can_item(ctx: AuthContext, perm: str, it: EquipmentItem) -> bool:
    if ctx.can(perm, global_only=True):
        return True
    if it.tracking == TrackingMode.BULK.value:
        return ctx.has_any(perm)
    if it.location_user_id is not None and it.location_user_id == ctx.user_id and ctx.has_any(perm):
        return True
    return ctx.can(perm, org_id=it.scope_org_id, establishment_id=it.location_establishment_id,
                   city=it.location_city_key)


def can_location(ctx: AuthContext, perm: str, loc: svc.Location) -> bool:
    k = loc.kind
    if ctx.can(perm, global_only=True):
        return True
    if k in (LocationKind.EXTERNAL.value, LocationKind.RMA.value, LocationKind.DISPOSED.value):
        return True  # sem dono: a checagem relevante é a do outro lado
    if k in (LocationKind.ESTABLISHMENT.value, LocationKind.SCREEN.value):
        return ctx.can(perm, org_id=loc.org_id, establishment_id=loc.establishment_id, city=loc.city_key)
    if k in (LocationKind.WAREHOUSE.value, LocationKind.MAINTENANCE.value):
        return loc.city_key is not None and ctx.can(perm, city=loc.city_key)
    if k == LocationKind.TECHNICIAN.value:
        return loc.ref_id is not None and loc.ref_id == ctx.user_id
    return False


async def _item(db: AsyncSession, ctx: AuthContext, perm: str, item_id: uuid.UUID, *,
                lock: bool = False) -> EquipmentItem:
    it = await (svc.lock_item(db, item_id) if lock else db.get(EquipmentItem, item_id))
    if it is None or not can_item(ctx, perm, it):
        raise NotFound()
    return it


async def _publish(it: EquipmentItem, change: str) -> None:
    await publish_safe("equipment.updated", {"id": str(it.id), "status": it.status, "change": change},
                       org_id=it.scope_org_id, establishment_id=it.location_establishment_id,
                       city=it.location_city_key)


@router.get("")
async def list_items(
    page: int = Query(1, ge=1),
    page_size: int = Query(25, ge=1, le=100),
    type: Optional[str] = Query(None, pattern="^(TV|TVBOX|CABLE|REMOTE|MOUNT|ACCESSORY)$"),
    tracking: Optional[str] = Query(None, pattern="^(SERIAL|BULK)$"),
    status: Optional[str] = Query(None, max_length=16),
    location_kind: Optional[str] = Query(None, pattern=KINDS),
    establishment_id: Optional[uuid.UUID] = None,
    screen_id: Optional[uuid.UUID] = None,
    search: Optional[str] = Query(None, max_length=100),
    ctx: AuthContext = Depends(require("equipment.view")),
    db: AsyncSession = Depends(get_db),
):
    q = select(EquipmentItem).where(item_filter(ctx, "equipment.view"))
    if type:
        q = q.where(EquipmentItem.type == type)
    if tracking:
        q = q.where(EquipmentItem.tracking == tracking)
    if status:
        q = q.where(EquipmentItem.status == status)
    if location_kind:
        q = q.where(EquipmentItem.location_kind == location_kind)
    if establishment_id:
        q = q.where(EquipmentItem.location_establishment_id == establishment_id)
    if screen_id:
        q = q.where(EquipmentItem.location_screen_id == screen_id)
    if search:
        t = f"%{search.strip()}%"
        q = q.where(EquipmentItem.name.ilike(t) | EquipmentItem.serial_number.ilike(t)
                    | EquipmentItem.asset_tag.ilike(t) | EquipmentItem.model.ilike(t))
    q = q.order_by(EquipmentItem.type, EquipmentItem.name, EquipmentItem.id)
    return await paginate(db, q, page, page_size, svc.item_out)


@router.get("/summary")
async def summary(ctx: AuthContext = Depends(require("equipment.view")), db: AsyncSession = Depends(get_db)):
    rows = (await db.execute(
        select(EquipmentItem.type, EquipmentItem.status, func.count())
        .where(EquipmentItem.tracking == TrackingMode.SERIAL.value, item_filter(ctx, "equipment.view"))
        .group_by(EquipmentItem.type, EquipmentItem.status)
    )).all()
    serial: dict = {}
    for t, st, n in rows:
        serial.setdefault(t, {})[st] = int(n)
    M = EquipmentMovement
    bulk_ids = select(EquipmentItem.id).where(EquipmentItem.tracking == TrackingMode.BULK.value)
    if not ctx.can("equipment.view", global_only=True):
        cities = ctx.city_keys("equipment.view") or set()
        city_filter_in = M.to_city_key.in_(list(cities)) if cities else false()
        city_filter_out = M.from_city_key.in_(list(cities)) if cities else false()
    else:
        city_filter_in = city_filter_out = true()
    wh = LocationKind.WAREHOUSE.value
    stock = (await db.execute(
        select(EquipmentItem.type, EquipmentItem.name,
               func.coalesce(func.sum(case((M.to_kind == wh, M.quantity), else_=0)).filter(city_filter_in), 0)
               - func.coalesce(func.sum(case((M.from_kind == wh, M.quantity), else_=0)).filter(city_filter_out), 0))
        .join(M, M.item_id == EquipmentItem.id)
        .where(EquipmentItem.id.in_(bulk_ids))
        .group_by(EquipmentItem.type, EquipmentItem.name)
        .order_by(EquipmentItem.type, EquipmentItem.name)
    )).all()
    return {"serial_by_type_status": serial,
            "bulk_in_warehouse": [{"type": t, "name": n, "quantity": int(q or 0)} for t, n, q in stock]}


@router.post("", status_code=201)
async def create_item(body: ItemIn, ctx: AuthContext = Depends(require("equipment.manage")),
                      db: AsyncSession = Depends(get_db)):
    from app.core.authz import normalize_city_key

    wh_city = normalize_city_key(body.warehouse_city_key)
    if not can_location(ctx, "equipment.manage", svc.Location(LocationKind.WAREHOUSE.value, None, wh_city)):
        raise DomainError("WAREHOUSE_OUT_OF_SCOPE", "Informe o estoque da sua cidade de atuação.", status=403)
    data = body.model_dump(exclude={"type", "tracking", "name", "quantity", "warehouse_city_key", "reference"})
    it, mv = await svc.create_item(db, ctx, type=body.type, tracking=body.tracking, name=body.name,
                                   quantity=body.quantity, warehouse_city_key=wh_city, reference=body.reference,
                                   **data)
    bal = await svc.balances(db, it.id)
    await db.commit()
    await _publish(it, "created")
    return svc.item_out(it, bal)


@router.get("/{item_id}")
async def get_item(item_id: uuid.UUID, ctx: AuthContext = Depends(require("equipment.view")),
                   db: AsyncSession = Depends(get_db)):
    it = await _item(db, ctx, "equipment.view", item_id)
    bal = await svc.balances(db, it.id)
    if not ctx.can("equipment.view", global_only=True) and it.tracking == TrackingMode.BULK.value:
        cities = ctx.city_keys("equipment.view") or set()
        bal = [b for b in bal if b["city_key"] in cities]
    return svc.item_out(it, bal)


@router.patch("/{item_id}")
async def update_item(item_id: uuid.UUID, body: ItemPatch, ctx: AuthContext = Depends(require("equipment.manage")),
                      db: AsyncSession = Depends(get_db)):
    it = await _item(db, ctx, "equipment.manage", item_id, lock=True)
    data = body.model_dump(exclude_unset=True)
    if it.tracking == TrackingMode.BULK.value and (data.get("serial_number") or data.get("asset_tag")):
        raise DomainError("BULK_NO_SERIAL", "Item de quantidade não tem série/patrimônio individual.", status=400)
    for f in ("serial_number", "asset_tag"):
        v = data.get(f)
        if v and v != getattr(it, f):
            q = select(EquipmentItem.id).where(getattr(EquipmentItem, f) == v, EquipmentItem.id != it.id)
            if f == "serial_number":
                q = q.where(EquipmentItem.type == it.type)
            if (await db.execute(q)).scalar_one_or_none() is not None:
                raise Conflict("EQUIPMENT_DUPLICATE", "Já existe item com este número de série/patrimônio.",
                               details={"field": f})
    before, after = {}, {}
    for k, v in data.items():
        if getattr(it, k) != v:
            before[k], after[k] = getattr(it, k), v
            setattr(it, k, v)
    if after:
        await db.flush()
        await audit(db, ctx, "equipment.updated", "equipment_item", it.id, before=before, after=after,
                    org_id=it.scope_org_id)
    await db.commit()
    await db.refresh(it)
    return svc.item_out(it, await svc.balances(db, it.id))


@router.get("/{item_id}/movements")
async def list_movements(item_id: uuid.UUID, page: int = Query(1, ge=1), page_size: int = Query(50, ge=1, le=100),
                         ctx: AuthContext = Depends(require("equipment.view")), db: AsyncSession = Depends(get_db)):
    it = await _item(db, ctx, "equipment.view", item_id)
    q = select(EquipmentMovement).where(EquipmentMovement.item_id == it.id).order_by(EquipmentMovement.seq.desc())
    return await paginate(db, q, page, page_size, svc.movement_out)


@router.post("/{item_id}/movements", status_code=201)
async def create_movement(item_id: uuid.UUID, body: MovementIn,
                          ctx: AuthContext = Depends(require("equipment.manage")),
                          db: AsyncSession = Depends(get_db)):
    it = await _item(db, ctx, "equipment.manage", item_id, lock=True)
    to = await svc.resolve(db, svc.Location(body.to.kind, body.to.ref_id, body.to.city_key))
    if not can_location(ctx, "equipment.manage", to):
        raise NotFound()
    src = None
    if body.from_ is not None:
        src = await svc.resolve(db, svc.Location(body.from_.kind, body.from_.ref_id, body.from_.city_key))
    check_src = src if src is not None else (svc.current_location(it) if it.tracking == "SERIAL" else None)
    if check_src is not None and not can_location(ctx, "equipment.manage", check_src):
        raise NotFound()
    mv = await svc.move(db, ctx, it, body.movement_type, to, source=src, quantity=body.quantity, reason=body.reason,
                        reference=body.reference, comodato_agreement_id=body.comodato_agreement_id,
                        expected_return_at=body.expected_return_at)
    out = svc.movement_out(mv)
    bal = await svc.balances(db, it.id)
    await db.commit()
    await db.refresh(it)
    await _publish(it, f"movement:{body.movement_type}")
    return {"movement": out, "item": svc.item_out(it, bal)}

