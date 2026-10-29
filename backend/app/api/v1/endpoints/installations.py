"""
RD Mídia Indoor — Instalações (técnico) e manutenção de hardware (W-ORG).

``/api/v1/installations`` (``equipment.view`` para ler, ``equipment.manage`` para operar;
escopo pelo estabelecimento — técnico com escopo CITY só vê a sua cidade):
- ``GET ""`` · ``POST ""`` · ``GET /{id}``
- ``POST /{id}/start`` · ``PUT /{id}/checklist`` · ``PUT /{id}/geolocation``
- ``POST /{id}/connection-test`` (presença real do player)
- ``POST /{id}/photos`` (multipart: ``kind`` + ``file`` JPEG/PNG) · ``GET /{id}/photos/{photo_id}``
- ``POST /{id}/signature`` (multipart: ``signer_name``, ``signer_role``, ``file``)
- ``POST /{id}/complete`` · ``POST /{id}/cancel``

``/api/v1/maintenance`` (``maintenance.manage``):
- ``GET ""`` · ``POST ""`` · ``GET /{id}`` · ``POST /{id}/assign`` · ``POST /{id}/status`` · ``POST /{id}/notes``
"""
from __future__ import annotations

import uuid
from datetime import date
from typing import Optional

from fastapi import APIRouter, Depends, File, Form, Query, UploadFile
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import or_, select
from sqlalchemy.orm import aliased
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.authz import AuthContext, require
from app.core.database import get_db
from app.core.errors import NotFound
from app.models.establishments import Establishment
from app.models.installations import Installation, InstallationPhoto, MaintenanceEvent, MaintenanceOrder
from app.models.screens import Screen
from app.models.users import User
from app.modules.establishments.service import ensure_est, est_filter, get_est
from app.modules.installations import service as svc
from app.modules.organizations import files
from app.modules.organizations.common import paginate, publish_safe

router = APIRouter()
maintenance_router = APIRouter()


class InstallationIn(BaseModel):
    establishment_id: uuid.UUID
    screen_id: Optional[uuid.UUID] = None
    technician_user_id: uuid.UUID
    scheduled_date: Optional[date] = None
    extra_checklist: list[dict] = Field(default_factory=list, max_length=30)


class ChecklistItemIn(BaseModel):
    key: str = Field(min_length=1, max_length=40)
    done: bool
    note: Optional[str] = Field(default=None, max_length=500)


class ChecklistIn(BaseModel):
    items: list[ChecklistItemIn] = Field(min_length=1, max_length=50)


class GeoIn(BaseModel):
    lat: float = Field(ge=-90, le=90)
    lng: float = Field(ge=-180, le=180)
    accuracy_m: Optional[int] = Field(default=None, ge=0, le=100000)


class InstallationPatch(BaseModel):
    model_config = ConfigDict(extra="forbid")

    screen_id: Optional[uuid.UUID] = None  # null explícito = desvincular
    technician_user_id: Optional[uuid.UUID] = None
    scheduled_date: Optional[date] = None
    notes: Optional[str] = Field(default=None, max_length=4000)


class CompleteIn(BaseModel):
    connection_test_waiver: Optional[str] = Field(default=None, max_length=2000)


class ReasonIn(BaseModel):
    reason: str = Field(min_length=10, max_length=2000)


_Tech = aliased(User, name="technician")
_Author = aliased(User, name="author")


def _author_col(model):
    """Quem abriu a ordem (manutenção) / quem agendou (instalação)."""
    return model.opened_by if model is MaintenanceOrder else model.created_by


def _author_key(model) -> str:
    return "opened_by_name" if model is MaintenanceOrder else "created_by_name"


def _with_names(model):
    """SELECT ``model`` + estabelecimento, código da tela, técnico e autor (1 query, LEFT JOINs — sem N+1)."""
    return (
        select(model, Establishment.name.label("establishment_name"), Screen.friendly_id.label("screen_code"),
               _Tech.full_name.label("technician_name"), _Author.full_name.label("author_name"))
        .join(Establishment, Establishment.id == model.establishment_id)
        .outerjoin(Screen, Screen.id == model.screen_id)
        .outerjoin(_Tech, _Tech.id == model.technician_user_id)
        .outerjoin(_Author, _Author.id == _author_col(model))
    )


def _named(out: dict, row, model) -> dict:
    out.update({"establishment_name": row.establishment_name, "screen_code": row.screen_code,
                "technician_name": row.technician_name, _author_key(model): row.author_name})
    return out


async def _names_for(db: AsyncSession, model, obj_id: uuid.UUID) -> dict:
    row = (await db.execute(_with_names(model).where(model.id == obj_id))).one()
    return _named({}, row, model)


async def user_names(db: AsyncSession, ids) -> dict:
    """Nomes de usuários em LOTE (1 query): ``{uuid_str: nome}``."""
    wanted = {uuid.UUID(str(i)) for i in ids if i}
    if not wanted:
        return {}
    rows = (await db.execute(select(User.id, User.full_name).where(User.id.in_(wanted)))).all()
    return {str(i): n for i, n in rows}


async def _inst(db: AsyncSession, ctx: AuthContext, perm: str, inst_id: uuid.UUID, *, lock: bool = False):
    inst = await svc.get_installation(db, inst_id, lock=lock)
    est = await db.get(Establishment, inst.establishment_id)
    ensure_est(ctx, perm, est)
    return inst, est


async def _out(db: AsyncSession, inst: Installation) -> dict:
    photos = (await db.execute(select(InstallationPhoto).where(InstallationPhoto.installation_id == inst.id)
                               .order_by(InstallationPhoto.created_at))).scalars().all()
    out = {**svc.installation_out(inst, list(photos)), **await _names_for(db, Installation, inst.id)}
    names = await user_names(db, [p["uploaded_by"] for p in out.get("photos", [])])
    for p in out.get("photos", []):
        p["uploaded_by_name"] = names.get(p["uploaded_by"])
    return out


async def _done(db: AsyncSession, inst: Installation, est: Establishment, change: str) -> dict:
    await db.commit()
    await db.refresh(inst)
    await publish_safe("establishment.updated", {"id": str(est.id), "org_id": str(est.org_id),
                                                 "change": f"installation:{change}", "installation_id": str(inst.id)},
                       org_id=est.org_id, establishment_id=est.id, city=est.city_key)
    return await _out(db, inst)


@router.get("")
async def list_installations(page: int = Query(1, ge=1), page_size: int = Query(25, ge=1, le=100),
                             establishment_id: Optional[uuid.UUID] = None,
                             status: Optional[str] = Query(None, pattern="^(SCHEDULED|IN_PROGRESS|COMPLETED|CANCELLED)$"),
                             mine: bool = False,
                             ctx: AuthContext = Depends(require("equipment.view")), db: AsyncSession = Depends(get_db)):
    q = _with_names(Installation).where(est_filter(ctx, "equipment.view"))
    if establishment_id:
        q = q.where(Installation.establishment_id == establishment_id)
    if status:
        q = q.where(Installation.status == status)
    if mine:
        q = q.where(Installation.technician_user_id == ctx.user_id)
    q = q.order_by(Installation.scheduled_date.desc().nullslast(), Installation.created_at.desc(), Installation.id)
    return await paginate(db, q, page, page_size,
                          lambda row: _named(svc.installation_out(row[0]), row, Installation), scalars=False)


@router.post("", status_code=201)
async def create_installation(body: InstallationIn, ctx: AuthContext = Depends(require("equipment.manage")),
                              db: AsyncSession = Depends(get_db)):
    est = await get_est(db, body.establishment_id)
    ensure_est(ctx, "equipment.manage", est)
    screen = None
    if body.screen_id:
        screen = await db.get(Screen, body.screen_id)
        if screen is None or screen.establishment_id != est.id:
            raise NotFound()
    inst = await svc.create_installation(db, ctx, est, screen, body.technician_user_id, body.scheduled_date,
                                         body.extra_checklist)
    return await _done(db, inst, est, "scheduled")


@router.get("/{inst_id}")
async def get_installation(inst_id: uuid.UUID, ctx: AuthContext = Depends(require("equipment.view")),
                           db: AsyncSession = Depends(get_db)):
    inst, _ = await _inst(db, ctx, "equipment.view", inst_id)
    return await _out(db, inst)


@router.patch("/{inst_id}")
async def update_installation(inst_id: uuid.UUID, body: InstallationPatch,
                              ctx: AuthContext = Depends(require("equipment.manage")),
                              db: AsyncSession = Depends(get_db)):
    from app.core.errors import DomainError

    inst, est = await _inst(db, ctx, "equipment.manage", inst_id, lock=True)
    changes = body.model_dump(exclude_unset=True)
    if "technician_user_id" in changes and changes["technician_user_id"] is None:
        raise DomainError("TECHNICIAN_REQUIRED", "A instalação precisa de um técnico responsável.", status=400)
    await svc.update_installation(db, ctx, inst, est, changes)
    return await _done(db, inst, est, "updated")


@router.post("/{inst_id}/start")
async def start_installation(inst_id: uuid.UUID, ctx: AuthContext = Depends(require("equipment.manage")),
                             db: AsyncSession = Depends(get_db)):
    inst, est = await _inst(db, ctx, "equipment.manage", inst_id, lock=True)
    await svc.start(db, ctx, inst, est)
    return await _done(db, inst, est, "started")


@router.put("/{inst_id}/checklist")
async def update_checklist(inst_id: uuid.UUID, body: ChecklistIn, ctx: AuthContext = Depends(require("equipment.manage")),
                           db: AsyncSession = Depends(get_db)):
    inst, est = await _inst(db, ctx, "equipment.manage", inst_id, lock=True)
    await svc.update_checklist(db, ctx, inst, est, [i.model_dump() for i in body.items])
    return await _done(db, inst, est, "checklist")


@router.put("/{inst_id}/geolocation")
async def set_geolocation(inst_id: uuid.UUID, body: GeoIn, ctx: AuthContext = Depends(require("equipment.manage")),
                          db: AsyncSession = Depends(get_db)):
    inst, est = await _inst(db, ctx, "equipment.manage", inst_id, lock=True)
    await svc.set_geolocation(db, ctx, inst, est, body.lat, body.lng, body.accuracy_m)
    return await _done(db, inst, est, "geolocation")


@router.post("/{inst_id}/connection-test")
async def connection_test(inst_id: uuid.UUID, ctx: AuthContext = Depends(require("equipment.manage")),
                          db: AsyncSession = Depends(get_db)):
    inst, est = await _inst(db, ctx, "equipment.manage", inst_id, lock=True)
    res = await svc.connection_test(db, ctx, inst, est)
    out = await _done(db, inst, est, "connection_test")
    return {"result": res, "installation": out}


@router.post("/{inst_id}/photos", status_code=201)
async def upload_photo(inst_id: uuid.UUID, kind: str = Form(..., pattern="^(TV_WORKING|EQUIPMENT|SITE|OTHER)$"),
                       file: UploadFile = File(...), ctx: AuthContext = Depends(require("equipment.manage")),
                       db: AsyncSession = Depends(get_db)):
    inst, est = await _inst(db, ctx, "equipment.manage", inst_id, lock=True)
    data = await files.read_limited(file, files.MAX_IMAGE_BYTES)
    ph = await svc.add_photo(db, ctx, inst, est, kind, data)
    await db.commit()
    return {"id": str(ph.id), "kind": ph.kind, "content_type": ph.content_type, "size_bytes": ph.size_bytes,
            "sha256": ph.sha256, "width": ph.width, "height": ph.height}


@router.get("/{inst_id}/photos/{photo_id}")
async def download_photo(inst_id: uuid.UUID, photo_id: uuid.UUID, ctx: AuthContext = Depends(require("equipment.view")),
                         db: AsyncSession = Depends(get_db)):
    from app.core.storage import ObjectNotFound, get_storage

    inst, _ = await _inst(db, ctx, "equipment.view", inst_id)
    ph = await db.get(InstallationPhoto, photo_id)
    if ph is None or ph.installation_id != inst.id:
        raise NotFound()
    st = get_storage()
    try:
        body = await st.open_range(ph.storage_key, 0, ph.size_bytes - 1)
    except ObjectNotFound:
        raise NotFound() from None
    return StreamingResponse(body, media_type=ph.content_type, headers={
        "Content-Length": str(ph.size_bytes), "Cache-Control": "private, no-store",
        "X-Content-Type-Options": "nosniff"})


@router.post("/{inst_id}/signature")
async def upload_signature(inst_id: uuid.UUID, signer_name: str = Form(..., min_length=3, max_length=255),
                           signer_role: Optional[str] = Form(None, max_length=120), file: UploadFile = File(...),
                           ctx: AuthContext = Depends(require("equipment.manage")), db: AsyncSession = Depends(get_db)):
    inst, est = await _inst(db, ctx, "equipment.manage", inst_id, lock=True)
    data = await files.read_limited(file, files.MAX_SIGNATURE_BYTES)
    await svc.set_signer(db, ctx, inst, est, signer_name, signer_role, data)
    return await _done(db, inst, est, "signed")


@router.post("/{inst_id}/complete")
async def complete_installation(inst_id: uuid.UUID, body: CompleteIn,
                                ctx: AuthContext = Depends(require("equipment.manage")),
                                db: AsyncSession = Depends(get_db)):
    inst, est = await _inst(db, ctx, "equipment.manage", inst_id, lock=True)
    await svc.complete(db, ctx, inst, est, body.connection_test_waiver)
    return await _done(db, inst, est, "completed")


@router.post("/{inst_id}/cancel")
async def cancel_installation(inst_id: uuid.UUID, body: ReasonIn,
                              ctx: AuthContext = Depends(require("equipment.manage")),
                              db: AsyncSession = Depends(get_db)):
    inst, est = await _inst(db, ctx, "equipment.manage", inst_id, lock=True)
    await svc.cancel(db, ctx, inst, est, body.reason)
    return await _done(db, inst, est, "cancelled")


# ─── Manutenção ──────────────────────────────────────────────────────────────

class OrderIn(BaseModel):
    establishment_id: Optional[uuid.UUID] = None
    screen_id: Optional[uuid.UUID] = None
    equipment_item_id: Optional[uuid.UUID] = None
    component: str = Field(pattern="^(TV|TVBOX|CABLE|NETWORK|OTHER)$")
    priority: str = Field(default="NORMAL", pattern="^(LOW|NORMAL|HIGH|URGENT)$")
    description: str = Field(min_length=10, max_length=4000)
    technician_user_id: Optional[uuid.UUID] = None
    put_screen_in_maintenance: bool = True


class AssignIn(BaseModel):
    technician_user_id: uuid.UUID


class OrderStatusIn(BaseModel):
    status: str = Field(pattern="^(IN_PROGRESS|WAITING_PARTS|RESOLVED|CANCELLED)$")
    note: Optional[str] = Field(default=None, max_length=4000)


class NoteIn(BaseModel):
    note: str = Field(min_length=2, max_length=4000)


async def _order(db: AsyncSession, ctx: AuthContext, order_id: uuid.UUID, *, lock: bool = False):
    q = select(MaintenanceOrder).where(MaintenanceOrder.id == order_id)
    if lock:
        q = q.with_for_update()
    o = (await db.execute(q)).scalar_one_or_none()
    if o is None:
        raise NotFound()
    est = await db.get(Establishment, o.establishment_id)
    if not (ctx.can("maintenance.manage", org_id=est.org_id, establishment_id=est.id, city=est.city_key)
            or (o.technician_user_id == ctx.user_id and ctx.has_any("maintenance.manage"))):
        raise NotFound()
    return o, est


async def _order_out(db: AsyncSession, o: MaintenanceOrder) -> dict:
    evs = (await db.execute(select(MaintenanceEvent).where(MaintenanceEvent.order_id == o.id)
                            .order_by(MaintenanceEvent.created_at, MaintenanceEvent.id))).scalars().all()
    out = {**svc.order_out(o, list(evs)), **await _names_for(db, MaintenanceOrder, o.id)}
    hist = out.get("history", [])
    names = await user_names(db, [h["actor_user_id"] for h in hist] + [h["technician_user_id"] for h in hist])
    for h in hist:
        h["actor_name"] = names.get(h["actor_user_id"])
        h["technician_name"] = names.get(h["technician_user_id"])
    return out


async def _order_done(db: AsyncSession, o: MaintenanceOrder, est: Establishment, change: str) -> dict:
    await db.commit()
    await db.refresh(o)
    await publish_safe("maintenance.updated", {"id": str(o.id), "status": o.status, "change": change,
                                               "screen_id": str(o.screen_id) if o.screen_id else None},
                       org_id=est.org_id, establishment_id=est.id, city=est.city_key)
    if o.screen_id:
        s = await db.get(Screen, o.screen_id)
        await publish_safe("screen.updated", {"id": str(s.id), "friendly_id": s.friendly_id, "status": s.status,
                                              "sellable": s.sellable, "change": f"maintenance:{change}"},
                           org_id=est.org_id, establishment_id=est.id, city=est.city_key)
    return await _order_out(db, o)


@maintenance_router.get("")
async def list_orders(page: int = Query(1, ge=1), page_size: int = Query(25, ge=1, le=100),
                      status: Optional[str] = Query(None, pattern="^(OPEN|IN_PROGRESS|WAITING_PARTS|RESOLVED|CANCELLED)$"),
                      open_only: bool = False, screen_id: Optional[uuid.UUID] = None, mine: bool = False,
                      ctx: AuthContext = Depends(require("maintenance.manage")), db: AsyncSession = Depends(get_db)):
    q = _with_names(MaintenanceOrder).where(
        or_(est_filter(ctx, "maintenance.manage"), MaintenanceOrder.technician_user_id == ctx.user_id))
    if status:
        q = q.where(MaintenanceOrder.status == status)
    if open_only:
        q = q.where(MaintenanceOrder.status.in_(svc.OPEN_STATES))
    if screen_id:
        q = q.where(MaintenanceOrder.screen_id == screen_id)
    if mine:
        q = q.where(MaintenanceOrder.technician_user_id == ctx.user_id)
    q = q.order_by(MaintenanceOrder.opened_at.desc(), MaintenanceOrder.id)
    return await paginate(db, q, page, page_size,
                          lambda row: _named(svc.order_out(row[0]), row, MaintenanceOrder), scalars=False)


@maintenance_router.post("", status_code=201)
async def open_order(body: OrderIn, ctx: AuthContext = Depends(require("maintenance.manage")),
                     db: AsyncSession = Depends(get_db)):
    from app.modules.screens.service import lock_screen

    screen = None
    if body.screen_id:
        screen = await lock_screen(db, body.screen_id)
        est = await get_est(db, screen.establishment_id)
        if body.establishment_id and body.establishment_id != est.id:
            raise NotFound()
    elif body.establishment_id:
        est = await get_est(db, body.establishment_id)
    else:
        from app.core.errors import DomainError

        raise DomainError("TARGET_REQUIRED", "Informe a tela ou o estabelecimento.", status=400)
    ensure_est(ctx, "maintenance.manage", est)
    if body.equipment_item_id:
        from app.models.equipment import EquipmentItem

        it = await db.get(EquipmentItem, body.equipment_item_id)
        if it is None or (it.location_establishment_id not in (None, est.id)):
            raise NotFound()
    o = await svc.open_order(db, ctx, est, screen, body.equipment_item_id, body.component, body.priority,
                             body.description, body.technician_user_id, body.put_screen_in_maintenance)
    return await _order_done(db, o, est, "opened")


@maintenance_router.get("/{order_id}")
async def get_order(order_id: uuid.UUID, ctx: AuthContext = Depends(require("maintenance.manage")),
                    db: AsyncSession = Depends(get_db)):
    o, _ = await _order(db, ctx, order_id)
    return await _order_out(db, o)


@maintenance_router.post("/{order_id}/assign")
async def assign_order(order_id: uuid.UUID, body: AssignIn, ctx: AuthContext = Depends(require("maintenance.manage")),
                       db: AsyncSession = Depends(get_db)):
    o, est = await _order(db, ctx, order_id, lock=True)
    ensure_est(ctx, "maintenance.manage", est)
    await svc.assign(db, ctx, o, est, body.technician_user_id)
    return await _order_done(db, o, est, "assigned")


@maintenance_router.post("/{order_id}/status")
async def order_status(order_id: uuid.UUID, body: OrderStatusIn,
                       ctx: AuthContext = Depends(require("maintenance.manage")), db: AsyncSession = Depends(get_db)):
    o, est = await _order(db, ctx, order_id, lock=True)
    await svc.change_status(db, ctx, o, est, body.status, body.note)
    return await _order_done(db, o, est, body.status.lower())


@maintenance_router.post("/{order_id}/notes", status_code=201)
async def order_note(order_id: uuid.UUID, body: NoteIn, ctx: AuthContext = Depends(require("maintenance.manage")),
                     db: AsyncSession = Depends(get_db)):
    o, est = await _order(db, ctx, order_id, lock=True)
    await svc.add_note(db, ctx, o, est, body.note)
    return await _order_done(db, o, est, "note")
