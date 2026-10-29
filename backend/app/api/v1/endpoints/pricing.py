"""
RD Mídia Indoor — Preços, planos, promoções, cupons e margem (W-CAMP, AÇÃO 002).

``pricing.view`` (GLOBAL) lê tabelas/planos/margem; ``pricing.manage`` (GLOBAL) cria/edita/
ativa. Tabela ATIVA é imutável (nova versão por duplicação) — preços mudam sem deploy.
Plano: margem calculada ao ativar (NEGATIVA ⇒ 409; abaixo do mínimo ⇒ alerta). Plano
personalizado (por cliente) exige aprovação de outra pessoa com ``pricing.manage``.
Cotação pública (``POST /pricing/quote``): telas resolvidas no servidor; nunca aceita preço do cliente.
"""
from __future__ import annotations

import re
import uuid
from datetime import datetime
from typing import Literal, Optional

from fastapi import APIRouter, Depends, Query, Response
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.audit import audit
from app.core.authz import AuthContext, get_auth_context, require
from app.core.database import get_db
from app.core.errors import Conflict, DomainError, Forbidden, NotFound
from app.core.timeutils import utcnow
from app.models.pricing import (
    Coupon,
    Plan,
    PlanKind,
    PlanStatus,
    PriceItem,
    PriceItemKind,
    PriceItemMode,
    PriceTable,
    PriceTableStatus,
    Promotion,
)
from app.modules.campaigns import schemas as SC
from app.modules.campaigns import service as S
from app.modules.campaigns import targeting as TG
from app.modules.pricing import engine as PE
from app.modules.pricing import service as PS

router = APIRouter()


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


def _page(items, total, page, page_size):
    return {"items": items, "total": total, "page": page, "page_size": page_size}


# ─── tabelas ─────────────────────────────────────────────────────────────────

class TableIn(_Strict):
    name: str = Field(min_length=3, max_length=120)
    notes: Optional[str] = Field(default=None, max_length=2000)


class ItemIn(_Strict):
    kind: Literal[tuple(k.value for k in PriceItemKind)]  # type: ignore[valid-type]
    key: str = Field(default="*", min_length=1, max_length=100)
    mode: Literal[tuple(m.value for m in PriceItemMode)]  # type: ignore[valid-type]
    min_value: Optional[int] = Field(default=None, ge=0)
    max_value: Optional[int] = Field(default=None, ge=0)
    value: int = Field(ge=0, le=10**12)
    description: Optional[str] = Field(default=None, max_length=255)


class ItemPatch(_Strict):
    key: Optional[str] = Field(default=None, min_length=1, max_length=100)
    min_value: Optional[int] = Field(default=None, ge=0)
    max_value: Optional[int] = Field(default=None, ge=0)
    value: Optional[int] = Field(default=None, ge=0, le=10**12)
    description: Optional[str] = Field(default=None, max_length=255)


_KIND_MODES = {
    "DURATION_BASE": {"BASE_CENTS"}, "FREQUENCY": {"MULTIPLIER_BPS", "ADD_CENTS"},
    "SCREEN_COUNT": {"MULTIPLIER_BPS", "ADD_CENTS"}, "PERIOD": {"MULTIPLIER_BPS", "ADD_CENTS"},
    "REGION": {"MULTIPLIER_BPS", "ADD_CENTS"}, "TIME_BAND": {"MULTIPLIER_BPS"},
    "SCREEN_CLASS": {"MULTIPLIER_BPS", "ADD_CENTS"}, "VENUE_CLASS": {"MULTIPLIER_BPS", "ADD_CENTS"},
    "ESTABLISHMENT": {"MULTIPLIER_BPS"}, "EXCLUSIVITY": {"MULTIPLIER_BPS", "ADD_CENTS"},
    "PRIORITY": {"MULTIPLIER_BPS", "ADD_CENTS"}, "MIN_PRICE": {"FIXED_CENTS"}, "FEE": {"FIXED_CENTS"},
    "TAX": {"PERCENT_BPS"}, "COST_GATEWAY": {"PERCENT_BPS", "FIXED_CENTS"}, "COST_SELLER": {"PERCENT_BPS"},
    "COST_SERVER": {"PER_SCREEN_MONTH_CENTS"}, "COST_EQUIPMENT": {"PER_SCREEN_MONTH_CENTS"},
    "COST_DESIGNER": {"FIXED_CENTS"}, "COST_DIRECT": {"FIXED_CENTS", "PERCENT_BPS"},
}


def _validate_item(kind: str, mode: str, key: str, lo, hi, value: int) -> None:
    if mode not in _KIND_MODES[kind]:
        raise DomainError("PRICE_ITEM_INVALID", "Modo incompatível com o tipo de item.", status=400,
                          details={"kind": kind, "mode": mode, "allowed": sorted(_KIND_MODES[kind])})
    if kind == "DURATION_BASE" and key not in ("30", "60", "120"):
        raise DomainError("PRICE_ITEM_INVALID", "Preço-base só para 30, 60 ou 120 segundos.", status=400)
    if kind == "PERIOD" and key not in ("AVULSO", "MENSAL", "SEMESTRAL", "ANUAL", "*"):
        raise DomainError("PRICE_ITEM_INVALID", "Período inválido (sem trimestral).", status=400)
    if kind == "FEE" and key not in ("ART", "INSTALLATION", "UPGRADE"):
        raise DomainError("PRICE_ITEM_INVALID", "Taxa deve ser ART, INSTALLATION ou UPGRADE.", status=400)
    if kind == "TIME_BAND" and (lo is None or hi is None or not (0 <= lo < hi <= 1440)):
        raise DomainError("PRICE_ITEM_INVALID", "Faixa horária exige min/max em minutos (0–1440).", status=400)
    if mode in ("MULTIPLIER_BPS",) and value == 0:
        raise DomainError("PRICE_ITEM_INVALID", "Multiplicador zero não é permitido.", status=400)
    if mode == "PERCENT_BPS" and value > 10000:
        raise DomainError("PRICE_ITEM_INVALID", "Percentual acima de 100%.", status=400)
    if lo is not None and hi is not None and hi < lo:
        raise DomainError("PRICE_ITEM_INVALID", "Faixa inválida.", status=400)


def table_out(t: PriceTable, items: Optional[list] = None) -> dict:
    out = {"id": str(t.id), "name": t.name, "version": t.version, "status": t.status, "notes": t.notes,
           "cloned_from_id": str(t.cloned_from_id) if t.cloned_from_id else None, "created_at": t.created_at,
           "activated_at": t.activated_at, "archived_at": t.archived_at}
    if items is not None:
        out["items"] = [{"id": str(i.id), "kind": i.kind, "key": i.key, "mode": i.mode, "min_value": i.min_value,
                         "max_value": i.max_value, "value": int(i.value), "description": i.description}
                        for i in sorted(items, key=lambda i: (i.kind, i.key, i.min_value or 0, str(i.id)))]
    return out


async def _table(db: AsyncSession, table_id: uuid.UUID, *, lock: bool = False) -> PriceTable:
    q = select(PriceTable).where(PriceTable.id == table_id)
    if lock:
        q = q.with_for_update().execution_options(populate_existing=True)
    t = (await db.execute(q)).scalar_one_or_none()
    if t is None:
        raise NotFound()
    return t


def _draft_only(t: PriceTable) -> None:
    if t.status != PriceTableStatus.RASCUNHO.value:
        raise Conflict("PRICE_TABLE_IMMUTABLE", "Tabela ativa/arquivada não muda: duplique para criar nova versão.")


async def _items(db: AsyncSession, table_id: uuid.UUID) -> list[PriceItem]:
    return list((await db.execute(select(PriceItem).where(PriceItem.table_id == table_id))).scalars())


async def _next_version(db: AsyncSession) -> int:
    return int((await db.execute(select(func.max(PriceTable.version)))).scalar() or 0) + 1


@router.get("/tables")
async def list_tables(ctx: AuthContext = Depends(require("pricing.view", global_only=True)),
                      db: AsyncSession = Depends(get_db)):
    rows = (await db.execute(select(PriceTable).order_by(PriceTable.version.desc()))).scalars().all()
    return {"items": [table_out(t) for t in rows]}


@router.post("/tables", status_code=201)
async def create_table(body: TableIn, ctx: AuthContext = Depends(require("pricing.manage", global_only=True)),
                       db: AsyncSession = Depends(get_db)):
    t = PriceTable(id=uuid.uuid4(), name=body.name, notes=body.notes, version=await _next_version(db),
                   status=PriceTableStatus.RASCUNHO.value, created_by=ctx.user_id)
    db.add(t)
    await audit(db, ctx, "pricing.table_created", "price_table", t.id, after={"name": t.name, "version": t.version})
    await db.flush()
    return table_out(t, [])


@router.get("/tables/{table_id}")
async def get_table(table_id: uuid.UUID, ctx: AuthContext = Depends(require("pricing.view", global_only=True)),
                    db: AsyncSession = Depends(get_db)):
    t = await _table(db, table_id)
    return table_out(t, await _items(db, t.id))


@router.post("/tables/{table_id}/duplicate", status_code=201)
async def duplicate_table(table_id: uuid.UUID, ctx: AuthContext = Depends(require("pricing.manage", global_only=True)),
                          db: AsyncSession = Depends(get_db)):
    src = await _table(db, table_id)
    t = PriceTable(id=uuid.uuid4(), name=f"{src.name} (cópia)", notes=src.notes, version=await _next_version(db),
                   status=PriceTableStatus.RASCUNHO.value, cloned_from_id=src.id, created_by=ctx.user_id)
    db.add(t)
    await db.flush([t])
    items = await _items(db, src.id)
    for i in items:
        db.add(PriceItem(id=uuid.uuid4(), table_id=t.id, kind=i.kind, key=i.key, mode=i.mode, min_value=i.min_value,
                         max_value=i.max_value, value=i.value, description=i.description))
    await audit(db, ctx, "pricing.table_duplicated", "price_table", t.id,
                after={"from_version": src.version, "version": t.version, "items": len(items)})
    await db.flush()
    return table_out(t, await _items(db, t.id))


@router.post("/tables/{table_id}/items", status_code=201)
async def add_item(table_id: uuid.UUID, body: ItemIn,
                   ctx: AuthContext = Depends(require("pricing.manage", global_only=True)),
                   db: AsyncSession = Depends(get_db)):
    t = await _table(db, table_id, lock=True)
    _draft_only(t)
    _validate_item(body.kind, body.mode, body.key, body.min_value, body.max_value, body.value)
    i = PriceItem(id=uuid.uuid4(), table_id=t.id, **body.model_dump())
    db.add(i)
    await audit(db, ctx, "pricing.item_created", "price_item", i.id, after={**body.model_dump(), "table": t.version})
    await db.flush()
    return table_out(t, await _items(db, t.id))


@router.patch("/tables/{table_id}/items/{item_id}")
async def patch_item(table_id: uuid.UUID, item_id: uuid.UUID, body: ItemPatch,
                     ctx: AuthContext = Depends(require("pricing.manage", global_only=True)),
                     db: AsyncSession = Depends(get_db)):
    t = await _table(db, table_id, lock=True)
    _draft_only(t)
    i = await db.get(PriceItem, item_id)
    if i is None or i.table_id != t.id:
        raise NotFound()
    data = body.model_dump(exclude_unset=True)
    before = {k: getattr(i, k) for k in data}
    for k, v in data.items():
        setattr(i, k, v)
    _validate_item(i.kind, i.mode, i.key, i.min_value, i.max_value, int(i.value))
    await audit(db, ctx, "pricing.item_updated", "price_item", i.id, before=before, after=data)
    return table_out(t, await _items(db, t.id))


@router.delete("/tables/{table_id}/items/{item_id}", status_code=204)
async def delete_item(table_id: uuid.UUID, item_id: uuid.UUID,
                      ctx: AuthContext = Depends(require("pricing.manage", global_only=True)),
                      db: AsyncSession = Depends(get_db)):
    t = await _table(db, table_id, lock=True)
    _draft_only(t)
    i = await db.get(PriceItem, item_id)
    if i is None or i.table_id != t.id:
        raise NotFound()
    await audit(db, ctx, "pricing.item_deleted", "price_item", i.id,
                before={"kind": i.kind, "key": i.key, "value": int(i.value)})
    await db.delete(i)
    return Response(status_code=204)


@router.post("/tables/{table_id}/activate")
async def activate_table(table_id: uuid.UUID,
                         ctx: AuthContext = Depends(require("pricing.manage", global_only=True,
                                                            allow_impersonation=False)),
                         db: AsyncSession = Depends(get_db)):
    """Ativa a versão (arquiva a anterior). Bloqueia se algum plano ATIVO ficaria com margem negativa."""
    t = await _table(db, table_id, lock=True)
    _draft_only(t)
    items = await _items(db, t.id)
    if not any(i.kind == "DURATION_BASE" for i in items):
        raise Conflict("PRICE_TABLE_INCOMPLETE", "A tabela precisa de preço-base por duração.")
    from app.modules.pricing import floors as FL

    await FL.enforce_table_items(db, items)
    negatives, below = [], []
    for p in (await db.execute(select(Plan).where(Plan.status == PlanStatus.ATIVO.value))).scalars():
        q = await PS.plan_reference_quote(db, p, table=t)
        await PS.enforce_plan_floor(db, p, quote=q)
        if q["margin"]["status"] == "NEGATIVE":
            negatives.append({"plan_id": str(p.id), "name": p.name, "margin_bps": q["margin"]["margin_bps"]})
        elif q["margin"]["status"] == "BELOW_MIN":
            below.append({"plan_id": str(p.id), "name": p.name, "margin_bps": q["margin"]["margin_bps"]})
    if negatives:
        raise Conflict("TABLE_MAKES_PLAN_NEGATIVE", "A nova tabela deixaria plano(s) ativo(s) com margem negativa.",
                       details={"plans": negatives})
    prev = (await db.execute(select(PriceTable).where(PriceTable.status == PriceTableStatus.ATIVA.value)
                             .with_for_update().execution_options(populate_existing=True))).scalar_one_or_none()
    now = utcnow()
    if prev is not None:
        prev.status = PriceTableStatus.ARQUIVADA.value
        prev.archived_at = now
        await db.flush([prev])
    t.status = PriceTableStatus.ATIVA.value
    t.activated_at = now
    t.activated_by = ctx.user_id
    await audit(db, ctx, "pricing.table_activated", "price_table", t.id,
                before={"active_version": prev.version if prev else None},
                after={"active_version": t.version, "plans_below_min_margin": below})
    return {**table_out(t), "warnings": [{"code": "MARGIN_BELOW_MIN", **b} for b in below]}


# ─── planos ──────────────────────────────────────────────────────────────────

class PlanIn(_Strict):
    name: str = Field(min_length=3, max_length=120)
    description: Optional[str] = Field(default=None, max_length=2000)
    kind: Literal["PREDEFINED", "CUSTOM"] = "PREDEFINED"
    org_id: Optional[uuid.UUID] = None
    period_type: Literal["AVULSO", "MENSAL", "SEMESTRAL", "ANUAL"] = "MENSAL"
    period_days: Optional[int] = Field(default=None, ge=1, le=366)
    duration_s: int = 30
    freq_kind: Literal["PER_HOUR", "PER_DAY", "SPECIFIC_TIME"] = "PER_HOUR"
    freq_value: int = Field(default=1, ge=1, le=1440)
    specific_times: Optional[list[str]] = Field(default=None, max_length=48)
    min_interval_s: int = Field(default=0, ge=0, le=86400)
    days_mask: int = Field(default=127, ge=1, le=127)
    time_bands: list[list[int]] = Field(default_factory=lambda: [[0, 1440]], max_length=24)
    screens_min: int = Field(default=1, ge=1, le=100000)
    screens_max: int = Field(default=1, ge=1, le=100000)
    priority: Literal["STANDARD", "PREMIUM"] = "STANDARD"
    exclusivity: Literal["NONE", "COMPETITOR_EXCLUSIVE"] = "NONE"
    establishment_category_ids: Optional[list[uuid.UUID]] = Field(default=None, max_length=50)
    region_targets: Optional[list[SC.TargetIn]] = Field(default=None, max_length=100)
    discount_bps: int = Field(default=0, ge=0, le=10000)
    max_campaigns: Optional[int] = Field(default=None, ge=0)
    max_creatives: Optional[int] = Field(default=None, ge=0)
    max_screens: Optional[int] = Field(default=None, ge=0)
    max_upload_mb: Optional[int] = Field(default=None, ge=0)


class PlanPatch(_Strict):
    name: Optional[str] = Field(default=None, min_length=3, max_length=120)
    description: Optional[str] = Field(default=None, max_length=2000)
    discount_bps: Optional[int] = Field(default=None, ge=0, le=10000)
    screens_min: Optional[int] = Field(default=None, ge=1)
    screens_max: Optional[int] = Field(default=None, ge=1)
    max_campaigns: Optional[int] = Field(default=None, ge=0)
    max_creatives: Optional[int] = Field(default=None, ge=0)
    max_screens: Optional[int] = Field(default=None, ge=0)
    max_upload_mb: Optional[int] = Field(default=None, ge=0)


def _slugify(name: str) -> str:
    import unicodedata

    t = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode("ascii").lower()
    return re.sub(r"[^a-z0-9]+", "-", t).strip("-")[:100] or "plano"


def plan_out(p: Plan, *, staff: bool = True) -> dict:
    out = {"id": str(p.id), "name": p.name, "slug": p.slug, "description": p.description, "kind": p.kind,
           "org_id": str(p.org_id) if p.org_id else None, "status": p.status, "period_type": p.period_type,
           "period_days": p.period_days, "duration_s": p.duration_s, "freq_kind": p.freq_kind,
           "freq_value": p.freq_value, "specific_times": p.specific_times or [], "min_interval_s": p.min_interval_s,
           "days_mask": p.days_mask, "time_bands": p.time_bands, "screens_min": p.screens_min,
           "screens_max": p.screens_max, "priority": p.priority, "exclusivity": p.exclusivity,
           "establishment_category_ids": p.establishment_category_ids, "region_targets": p.region_targets,
           "discount_bps": p.discount_bps, "max_campaigns": p.max_campaigns, "max_creatives": p.max_creatives,
           "max_screens": p.max_screens, "max_upload_mb": p.max_upload_mb, "version": p.version}
    if staff:
        out.update({"margin_bps": p.margin_bps, "margin_status": p.margin_status,
                    "reference_quote": p.reference_quote, "approved_at": p.approved_at,
                    "approved_by": str(p.approved_by) if p.approved_by else None,
                    "created_by": str(p.created_by) if p.created_by else None,
                    "rejection_reason": p.rejection_reason})
    elif p.reference_quote:
        out["reference_total_cents"] = p.reference_quote.get("total_cents")
    return out


def _can_create_custom(ctx: AuthContext) -> bool:
    return ctx.can("pricing.manage", global_only=True) or ctx.has_any("crm.manage")


async def _unique_slug(db: AsyncSession, base: str) -> str:
    slug, n = base, 1
    while (await db.execute(select(Plan.id).where(Plan.slug == slug))).first() is not None:
        n += 1
        slug = f"{base}-{n}"
    return slug


@router.get("/plans")
async def list_plans(page: int = Query(1, ge=1), page_size: int = Query(25, ge=1, le=100),
                     status: Optional[str] = Query(None, max_length=60),
                     ctx: AuthContext = Depends(require("pricing.view", global_only=True)),
                     db: AsyncSession = Depends(get_db)):
    base = select(Plan)
    if status:
        base = base.where(Plan.status.in_([s.strip().upper() for s in status.split(",")]))
    total = (await db.execute(select(func.count()).select_from(base.subquery()))).scalar_one()
    rows = (await db.execute(base.order_by(Plan.created_at.desc(), Plan.id).offset((page - 1) * page_size)
                             .limit(page_size))).scalars().all()
    return _page([plan_out(p) for p in rows], total, page, page_size)


@router.get("/plans/catalog")
async def plan_catalog(org_id: Optional[uuid.UUID] = None, ctx: AuthContext = Depends(get_auth_context),
                       db: AsyncSession = Depends(get_db)):
    """Planos ATIVOS disponíveis ao cliente: pré-definidos + personalizados da própria organização."""
    if not (ctx.has_any("checkout.create") or ctx.has_any("campaigns.manage") or ctx.has_any("pricing.view")):
        raise Forbidden(details={"permission": "checkout.create"})
    conds = [Plan.kind == PlanKind.PREDEFINED.value]
    if org_id is not None:
        if not (ctx.can("checkout.create", org_id=org_id) or ctx.can("pricing.view", global_only=True)):
            raise NotFound()
        conds.append((Plan.kind == PlanKind.CUSTOM.value) & (Plan.org_id == org_id))
    from sqlalchemy import or_

    rows = (await db.execute(select(Plan).where(Plan.status == PlanStatus.ATIVO.value, or_(*conds))
                             .order_by(Plan.name))).scalars().all()
    return {"items": [plan_out(p, staff=False) for p in rows]}


@router.post("/plans", status_code=201)
async def create_plan(body: PlanIn, ctx: AuthContext = Depends(get_auth_context), db: AsyncSession = Depends(get_db)):
    if body.kind == "PREDEFINED":
        if not ctx.can("pricing.manage", global_only=True):
            raise Forbidden(details={"permission": "pricing.manage"})
        if body.org_id is not None:
            raise DomainError("PLAN_INVALID", "Plano pré-definido não pertence a um cliente.", status=400)
        status = PlanStatus.RASCUNHO.value
    else:
        if not _can_create_custom(ctx):
            raise Forbidden(details={"permission": "pricing.manage"})
        if body.org_id is None:
            raise DomainError("PLAN_INVALID", "Plano personalizado exige o cliente (org_id).", status=400)
        from app.models.organizations import Organization

        if await db.get(Organization, body.org_id) is None:
            raise NotFound()
        status = PlanStatus.PENDENTE_APROVACAO.value  # personalizado exige aprovação admin (#1023)
    if body.screens_max < body.screens_min:
        raise DomainError("PLAN_INVALID", "screens_max deve ser ≥ screens_min.", status=400)
    if body.period_type == "AVULSO" and not body.period_days:
        raise DomainError("PERIOD_DAYS_REQUIRED", "Plano avulso exige quantidade de dias.", status=400)
    S.validate_schedule(start_date=None, end_date=None, period_type=body.period_type, duration_s=body.duration_s,
                        freq_kind=body.freq_kind, freq_value=body.freq_value, specific_times=body.specific_times,
                        days_mask=body.days_mask, time_bands=body.time_bands, min_interval_s=body.min_interval_s)
    data = body.model_dump()
    data["region_targets"] = [t.model_dump() for t in body.region_targets] if body.region_targets else None
    data["establishment_category_ids"] = [str(x) for x in body.establishment_category_ids] \
        if body.establishment_category_ids else None
    p = Plan(id=uuid.uuid4(), slug=await _unique_slug(db, _slugify(body.name)), status=status,
             created_by=ctx.user_id, version=1, **data)
    db.add(p)
    await db.flush([p])
    await PS.enforce_plan_floor(db, p)
    await audit(db, ctx, "pricing.plan_created", "plan", p.id, after=plan_out(p),
                org_id=p.org_id)
    return plan_out(p)


@router.get("/plans/{plan_id}")
async def get_plan(plan_id: uuid.UUID, ctx: AuthContext = Depends(require("pricing.view", global_only=True)),
                   db: AsyncSession = Depends(get_db)):
    p = await db.get(Plan, plan_id)
    if p is None:
        raise NotFound()
    return plan_out(p)


@router.patch("/plans/{plan_id}")
async def patch_plan(plan_id: uuid.UUID, body: PlanPatch,
                     ctx: AuthContext = Depends(require("pricing.manage", global_only=True)),
                     db: AsyncSession = Depends(get_db)):
    p = (await db.execute(select(Plan).where(Plan.id == plan_id).with_for_update().execution_options(populate_existing=True))).scalar_one_or_none()
    if p is None:
        raise NotFound()
    if p.status == PlanStatus.ATIVO.value and set(body.model_dump(exclude_unset=True)) - {"name", "description"}:
        raise Conflict("PLAN_ACTIVE_IMMUTABLE", "Plano ativo: desative ou duplique para alterar preço/limites.")
    data = body.model_dump(exclude_unset=True)
    before = {k: getattr(p, k) for k in data}
    for k, v in data.items():
        setattr(p, k, v)
    if p.screens_max < p.screens_min:
        raise DomainError("PLAN_INVALID", "screens_max deve ser ≥ screens_min.", status=400)
    p.version += 1
    await db.flush([p])
    await PS.enforce_plan_floor(db, p)
    await audit(db, ctx, "pricing.plan_updated", "plan", p.id, before=before, after=data, org_id=p.org_id)
    return plan_out(p)


@router.post("/plans/{plan_id}/duplicate", status_code=201)
async def duplicate_plan(plan_id: uuid.UUID, ctx: AuthContext = Depends(require("pricing.manage", global_only=True)),
                         db: AsyncSession = Depends(get_db)):
    src = await db.get(Plan, plan_id)
    if src is None:
        raise NotFound()
    cols = {c.key: getattr(src, c.key) for c in Plan.__table__.columns
            if c.key not in ("id", "slug", "status", "created_at", "updated_at", "approved_by", "approved_at",
                             "reference_quote", "margin_bps", "margin_status", "rejection_reason", "version",
                             "cloned_from_id", "created_by", "name")}
    p = Plan(id=uuid.uuid4(), name=f"{src.name} (cópia)", slug=await _unique_slug(db, _slugify(src.name + "-copia")),
             status=PlanStatus.RASCUNHO.value if src.kind == "PREDEFINED" else PlanStatus.PENDENTE_APROVACAO.value,
             cloned_from_id=src.id, created_by=ctx.user_id, version=1, **cols)
    db.add(p)
    await db.flush([p])
    await audit(db, ctx, "pricing.plan_duplicated", "plan", p.id, after={"from": str(src.id)}, org_id=p.org_id)
    return plan_out(p)


async def _activate_plan(db: AsyncSession, ctx: AuthContext, p: Plan, *, action: str) -> dict:
    q = await PS.plan_reference_quote(db, p)
    await PS.enforce_plan_floor(db, p, quote=q)
    m = q["margin"]
    p.reference_quote = {**PE.public_view(q), "margin": m}
    p.margin_bps = m["margin_bps"]
    p.margin_status = m["status"]
    if m["status"] == "NEGATIVE":
        await audit(db, ctx, "pricing.plan_blocked_negative_margin", "plan", p.id, after={"margin": m},
                    org_id=p.org_id)
        raise Conflict("PLAN_MARGIN_NEGATIVE", "Plano bloqueado: margem negativa.",
                       details={"margin_cents": m["margin_cents"], "margin_bps": m["margin_bps"]})
    before = p.status
    p.status = PlanStatus.ATIVO.value
    warnings = []
    if m["status"] == "BELOW_MIN":
        warnings.append({"code": "MARGIN_BELOW_MIN", "margin_bps": m["margin_bps"],
                         "min_margin_bps": m["min_margin_bps"]})
    await audit(db, ctx, action, "plan", p.id, before={"status": before},
                after={"status": p.status, "margin_bps": m["margin_bps"], "margin_status": m["status"]},
                org_id=p.org_id)
    return {**plan_out(p), "warnings": warnings}


@router.post("/plans/{plan_id}/activate")
async def activate_plan(plan_id: uuid.UUID, ctx: AuthContext = Depends(require("pricing.manage", global_only=True)),
                        db: AsyncSession = Depends(get_db)):
    p = (await db.execute(select(Plan).where(Plan.id == plan_id).with_for_update().execution_options(populate_existing=True))).scalar_one_or_none()
    if p is None:
        raise NotFound()
    if p.kind == PlanKind.CUSTOM.value and p.approved_at is None:
        raise Conflict("PLAN_APPROVAL_REQUIRED", "Plano personalizado precisa ser aprovado.")
    if p.status not in (PlanStatus.RASCUNHO.value, PlanStatus.INATIVO.value):
        raise Conflict("PLAN_INVALID_STATUS", "Plano não pode ser ativado neste status.", details={"status": p.status})
    return await _activate_plan(db, ctx, p, action="pricing.plan_activated")


@router.post("/plans/{plan_id}/deactivate")
async def deactivate_plan(plan_id: uuid.UUID, ctx: AuthContext = Depends(require("pricing.manage", global_only=True)),
                          db: AsyncSession = Depends(get_db)):
    p = (await db.execute(select(Plan).where(Plan.id == plan_id).with_for_update().execution_options(populate_existing=True))).scalar_one_or_none()
    if p is None:
        raise NotFound()
    if p.status != PlanStatus.ATIVO.value:
        raise Conflict("PLAN_INVALID_STATUS", "Plano não está ativo.")
    p.status = PlanStatus.INATIVO.value
    await audit(db, ctx, "pricing.plan_deactivated", "plan", p.id, after={"status": p.status}, org_id=p.org_id)
    return plan_out(p)


@router.post("/plans/{plan_id}/approve")
async def approve_plan(plan_id: uuid.UUID,
                       ctx: AuthContext = Depends(require("pricing.manage", global_only=True,
                                                          allow_impersonation=False)),
                       db: AsyncSession = Depends(get_db)):
    p = (await db.execute(select(Plan).where(Plan.id == plan_id).with_for_update().execution_options(populate_existing=True))).scalar_one_or_none()
    if p is None:
        raise NotFound()
    if p.status != PlanStatus.PENDENTE_APROVACAO.value:
        raise Conflict("PLAN_INVALID_STATUS", "Plano não aguarda aprovação.")
    if p.created_by == ctx.user_id:
        raise Forbidden("PLAN_SELF_APPROVAL", "Quem criou o plano personalizado não pode aprová-lo.")
    p.approved_by, p.approved_at = ctx.user_id, utcnow()
    return await _activate_plan(db, ctx, p, action="pricing.plan_approved")


class PlanReject(_Strict):
    reason: str = Field(min_length=10, max_length=2000)


@router.post("/plans/{plan_id}/reject")
async def reject_plan(plan_id: uuid.UUID, body: PlanReject,
                      ctx: AuthContext = Depends(require("pricing.manage", global_only=True)),
                      db: AsyncSession = Depends(get_db)):
    p = (await db.execute(select(Plan).where(Plan.id == plan_id).with_for_update().execution_options(populate_existing=True))).scalar_one_or_none()
    if p is None:
        raise NotFound()
    if p.status != PlanStatus.PENDENTE_APROVACAO.value:
        raise Conflict("PLAN_INVALID_STATUS", "Plano não aguarda aprovação.")
    p.status = PlanStatus.REJEITADO.value
    p.rejection_reason = body.reason
    await audit(db, ctx, "pricing.plan_rejected", "plan", p.id, reason=body.reason, org_id=p.org_id)
    return plan_out(p)


@router.post("/plans/{plan_id}/margin")
async def plan_margin(plan_id: uuid.UUID, ctx: AuthContext = Depends(require("pricing.view", global_only=True)),
                      db: AsyncSession = Depends(get_db)):
    p = await db.get(Plan, plan_id)
    if p is None:
        raise NotFound()
    q = await PS.plan_reference_quote(db, p)
    return {"plan_id": str(p.id), "quote": q}


# ─── promoções ───────────────────────────────────────────────────────────────

class PromotionIn(_Strict):
    name: str = Field(min_length=3, max_length=120)
    discount_bps: int = Field(ge=1, le=10000)
    valid_from: datetime
    valid_until: datetime
    plan_ids: Optional[list[uuid.UUID]] = Field(default=None, max_length=100)
    active: bool = True


class PromotionPatch(_Strict):
    active: Optional[bool] = None
    valid_until: Optional[datetime] = None
    name: Optional[str] = Field(default=None, min_length=3, max_length=120)


async def _enforce_promotion_floor(db: AsyncSession, p: Promotion) -> None:
    """Preço final com a promoção não pode ficar abaixo do piso RD em nenhum plano ATIVO alcançado."""
    if not p.active:
        return
    for plan in await PS.plans_affected(db, p.plan_ids):
        await PS.enforce_plan_floor(db, plan, promotion=(str(p.id), p.name, p.discount_bps))


def promo_out(p: Promotion) -> dict:
    return {"id": str(p.id), "name": p.name, "discount_bps": p.discount_bps, "valid_from": p.valid_from,
            "valid_until": p.valid_until, "plan_ids": p.plan_ids, "active": p.active}


@router.get("/promotions")
async def list_promotions(ctx: AuthContext = Depends(require("pricing.view", global_only=True)),
                          db: AsyncSession = Depends(get_db)):
    rows = (await db.execute(select(Promotion).order_by(Promotion.valid_from.desc()))).scalars().all()
    return {"items": [promo_out(p) for p in rows]}


@router.post("/promotions", status_code=201)
async def create_promotion(body: PromotionIn, ctx: AuthContext = Depends(require("pricing.manage", global_only=True)),
                           db: AsyncSession = Depends(get_db)):
    if body.valid_until <= body.valid_from:
        raise DomainError("VALIDITY_INVALID", "Validade inválida.", status=400)
    p = Promotion(id=uuid.uuid4(), name=body.name, discount_bps=body.discount_bps, valid_from=body.valid_from,
                  valid_until=body.valid_until, plan_ids=[str(x) for x in body.plan_ids] if body.plan_ids else None,
                  active=body.active, created_by=ctx.user_id)
    db.add(p)
    await _enforce_promotion_floor(db, p)
    await audit(db, ctx, "pricing.promotion_created", "promotion", p.id, after=promo_out(p))
    return promo_out(p)


@router.patch("/promotions/{promotion_id}")
async def patch_promotion(promotion_id: uuid.UUID, body: PromotionPatch,
                          ctx: AuthContext = Depends(require("pricing.manage", global_only=True)),
                          db: AsyncSession = Depends(get_db)):
    p = await db.get(Promotion, promotion_id)
    if p is None:
        raise NotFound()
    data = body.model_dump(exclude_unset=True)
    before = {k: getattr(p, k) for k in data}
    for k, v in data.items():
        setattr(p, k, v)
    if p.valid_until <= p.valid_from:
        raise DomainError("VALIDITY_INVALID", "Validade inválida.", status=400)
    await _enforce_promotion_floor(db, p)
    await audit(db, ctx, "pricing.promotion_updated", "promotion", p.id, before=before, after=data)
    return promo_out(p)


# ─── cupons ──────────────────────────────────────────────────────────────────

class CouponIn(_Strict):
    code: str = Field(min_length=3, max_length=40, pattern=r"^[A-Za-z0-9_\-]+$")
    description: Optional[str] = Field(default=None, max_length=255)
    discount_type: Literal["PERCENT", "FIXED"]
    discount_bps: int = Field(default=0, ge=0, le=10000)
    discount_cents: int = Field(default=0, ge=0, le=10**10)
    valid_from: datetime
    valid_until: datetime
    max_uses: Optional[int] = Field(default=None, ge=1)
    max_uses_per_org: Optional[int] = Field(default=None, ge=1)
    min_order_cents: int = Field(default=0, ge=0)
    plan_ids: Optional[list[uuid.UUID]] = Field(default=None, max_length=100)
    active: bool = True


class CouponPatch(_Strict):
    active: Optional[bool] = None
    valid_until: Optional[datetime] = None
    max_uses: Optional[int] = Field(default=None, ge=1)
    description: Optional[str] = Field(default=None, max_length=255)


async def _enforce_coupon_floor(db: AsyncSession, c: Coupon) -> None:
    """Preço final com o cupom (planos ATIVOS alcançados, respeitando o pedido mínimo) não pode ficar abaixo do piso."""
    if not c.active:
        return
    fact = PE.CouponFact(str(c.id), c.code, c.discount_type, discount_bps=int(c.discount_bps),
                         discount_cents=int(c.discount_cents))
    for plan in await PS.plans_affected(db, c.plan_ids):
        table = await PS.active_table_or_none(db)
        if table is None:
            return
        base = await PS.plan_reference_quote(db, plan, table=table)
        if base["media_gross_cents"] < int(c.min_order_cents):
            continue
        # cumulativo com a promoção vigente do plano (o mesmo empilhamento da cotação real)
        promo = await PS.best_promotion(db, plan.id, utcnow())
        await PS.enforce_plan_floor(db, plan, table=table, coupon=fact,
                                    promotion=(str(promo.id), promo.name, promo.discount_bps) if promo else None)


def coupon_out(c: Coupon) -> dict:
    return {"id": str(c.id), "code": c.code, "description": c.description, "discount_type": c.discount_type,
            "discount_bps": c.discount_bps, "discount_cents": int(c.discount_cents), "valid_from": c.valid_from,
            "valid_until": c.valid_until, "max_uses": c.max_uses, "uses_count": c.uses_count,
            "max_uses_per_org": c.max_uses_per_org, "min_order_cents": int(c.min_order_cents),
            "plan_ids": c.plan_ids, "active": c.active}


@router.get("/coupons")
async def list_coupons(ctx: AuthContext = Depends(require("pricing.view", global_only=True)),
                       db: AsyncSession = Depends(get_db)):
    rows = (await db.execute(select(Coupon).order_by(Coupon.created_at.desc()))).scalars().all()
    return {"items": [coupon_out(c) for c in rows]}


@router.post("/coupons", status_code=201)
async def create_coupon(body: CouponIn, ctx: AuthContext = Depends(require("pricing.manage", global_only=True)),
                        db: AsyncSession = Depends(get_db)):
    if body.discount_type == "PERCENT" and not (1 <= body.discount_bps <= 10000 and body.discount_cents == 0):
        raise DomainError("COUPON_INVALID_DEFINITION", "Cupom percentual exige discount_bps (1–10000).", status=400)
    if body.discount_type == "FIXED" and not (body.discount_cents > 0 and body.discount_bps == 0):
        raise DomainError("COUPON_INVALID_DEFINITION", "Cupom de valor fixo exige discount_cents > 0.", status=400)
    if body.valid_until <= body.valid_from:
        raise DomainError("VALIDITY_INVALID", "Validade inválida.", status=400)
    code = body.code.upper()
    if (await db.execute(select(Coupon.id).where(Coupon.code == code))).first() is not None:
        raise Conflict("COUPON_CODE_TAKEN", "Já existe um cupom com este código.")
    c = Coupon(id=uuid.uuid4(), code=code, description=body.description, discount_type=body.discount_type,
               discount_bps=body.discount_bps, discount_cents=body.discount_cents, valid_from=body.valid_from,
               valid_until=body.valid_until, max_uses=body.max_uses, uses_count=0,
               max_uses_per_org=body.max_uses_per_org, min_order_cents=body.min_order_cents,
               plan_ids=[str(x) for x in body.plan_ids] if body.plan_ids else None, active=body.active,
               created_by=ctx.user_id)
    db.add(c)
    await _enforce_coupon_floor(db, c)
    await audit(db, ctx, "pricing.coupon_created", "coupon", c.id, after=coupon_out(c))
    return coupon_out(c)


@router.patch("/coupons/{coupon_id}")
async def patch_coupon(coupon_id: uuid.UUID, body: CouponPatch,
                       ctx: AuthContext = Depends(require("pricing.manage", global_only=True)),
                       db: AsyncSession = Depends(get_db)):
    c = (await db.execute(select(Coupon).where(Coupon.id == coupon_id).with_for_update().execution_options(populate_existing=True))).scalar_one_or_none()
    if c is None:
        raise NotFound()
    data = body.model_dump(exclude_unset=True)
    if data.get("max_uses") is not None and data["max_uses"] < c.uses_count:
        raise DomainError("COUPON_INVALID_DEFINITION", "max_uses menor que os usos já realizados.", status=400)
    before = {k: getattr(c, k) for k in data}
    for k, v in data.items():
        setattr(c, k, v)
    await _enforce_coupon_floor(db, c)
    await audit(db, ctx, "pricing.coupon_updated", "coupon", c.id, before=before, after=data)
    return coupon_out(c)


class CouponValidateIn(_Strict):
    code: str = Field(min_length=1, max_length=40)
    org_id: Optional[uuid.UUID] = None
    plan_id: Optional[uuid.UUID] = None


@router.post("/coupons/validate")
async def validate_coupon(body: CouponValidateIn, ctx: AuthContext = Depends(require("checkout.create")),
                          db: AsyncSession = Depends(get_db)):
    from app.api.v1.endpoints.campaigns import resolve_org

    org_id = resolve_org(ctx, body.org_id, "checkout.create")
    c = await PS.find_coupon(db, body.code, org_id=org_id, plan_id=body.plan_id, at=utcnow())
    return {"valid": True, "code": c.code, "discount_type": c.discount_type, "discount_bps": c.discount_bps,
            "discount_cents": int(c.discount_cents), "min_order_cents": int(c.min_order_cents),
            "valid_until": c.valid_until}


# ─── cotação (tempo real, antes de criar a campanha) ─────────────────────────

@router.post("/quote")
async def quote_preview(body: SC.QuotePreviewIn, ctx: AuthContext = Depends(get_auth_context),
                        db: AsyncSession = Depends(get_db)):
    from app.api.v1.endpoints.campaigns import resolve_org
    from app.models.campaigns import Campaign

    if not (ctx.has_any("checkout.create") or ctx.has_any("campaigns.manage") or ctx.has_any("pricing.view")):
        raise Forbidden(details={"permission": "checkout.create"})
    perm = "checkout.create" if ctx.has_any("checkout.create") else (
        "campaigns.manage" if ctx.has_any("campaigns.manage") else "pricing.view")
    camp = None
    if body.campaign_id is not None:
        # estimativa de rascunho/alteração: telas pela MESMA função do checkout/aprovação (conflitos reais)
        camp = await S.get_campaign(db, ctx, body.campaign_id, perm="campaigns.view")
        if body.org_id is not None and body.org_id != camp.org_id:
            raise DomainError("CAMPAIGN_ORG_MISMATCH", "A campanha não pertence à organização informada.", status=400)
        org_id = camp.org_id
    else:
        org_id = resolve_org(ctx, body.org_id, perm)
    end = body.end_date or PS.period_end(body.start_date, body.period_type, body.period_days)
    S.validate_schedule(start_date=body.start_date, end_date=end, period_type=body.period_type,
                        duration_s=body.duration_s, freq_kind=body.freq_kind, freq_value=body.freq_value,
                        specific_times=body.specific_times, days_mask=body.days_mask, time_bands=body.time_bands,
                        min_interval_s=0)
    specs = TG.validate_specs(TG.specs_from([t.model_dump() for t in body.targets]))
    await S._check_target_permissions(db, ctx, specs)
    if camp is not None:
        res = await S.eligible_resolution(db, camp, specs=specs, duration_s=body.duration_s)
    else:
        probe = Campaign(id=uuid.uuid4(), org_id=org_id, name="cotação", status="RASCUNHO", duration_s=body.duration_s)
        res = await TG.resolve(db, specs, duration_s=body.duration_s, campaign=probe)
    if not res.screens:
        raise Conflict("NO_SCREENS_AVAILABLE", "Nenhuma tela disponível para a segmentação escolhida.")
    req = PS.PriceQuoteRequest(
        org_id=org_id, duration_s=body.duration_s, freq_kind=body.freq_kind, freq_value=body.freq_value,
        start_date=body.start_date, end_date=end, period_type=body.period_type, screens=res.screens,
        days_mask=body.days_mask, time_bands=body.time_bands, specific_times=body.specific_times or [],
        priority=body.priority, exclusivity=body.exclusivity, fees=("ART",) if body.request_art_service else (),
        plan_id=body.plan_id, coupon_code=body.coupon_code, with_seller=False)
    q = await PS.price_quote(db, ctx, req)
    staff = ctx.can("pricing.view", global_only=True)
    return {"quote": q if staff else PE.public_view(q), "availability": TG.availability_summary(res)}


class MarginSimIn(_Strict):
    duration_s: int = 30
    freq_kind: Literal["PER_HOUR", "PER_DAY", "SPECIFIC_TIME"] = "PER_HOUR"
    freq_value: int = Field(default=1, ge=1, le=1440)
    period_type: Literal["AVULSO", "MENSAL", "SEMESTRAL", "ANUAL"] = "MENSAL"
    period_days: Optional[int] = Field(default=None, ge=1, le=366)
    screens: int = Field(default=1, ge=1, le=10000)
    discount_bps: int = Field(default=0, ge=0, le=10000)
    priority: Literal["STANDARD", "PREMIUM"] = "STANDARD"
    exclusivity: Literal["NONE", "COMPETITOR_EXCLUSIVE"] = "NONE"
    request_art_service: bool = False


@router.post("/margin-simulator")
async def margin_simulator(body: MarginSimIn, ctx: AuthContext = Depends(require("pricing.view", global_only=True)),
                           db: AsyncSession = Depends(get_db)):
    p = Plan(id=uuid.uuid4(), name="simulação", slug="simulacao", kind="PREDEFINED", status="RASCUNHO",
             period_type=body.period_type, period_days=body.period_days, duration_s=body.duration_s,
             freq_kind=body.freq_kind, freq_value=body.freq_value, days_mask=127, time_bands=[[0, 1440]],
             screens_min=1, screens_max=body.screens, priority=body.priority, exclusivity=body.exclusivity,
             discount_bps=body.discount_bps, min_interval_s=0)
    q = await PS.plan_reference_quote(db, p)
    return {"quote": q, "margin": q["margin"]}
