"""
RD Mídia Indoor — Contratos/aceites, planos do parceiro e comodato (W-ORG).

``/api/v1/contracts``:
- ``GET /templates`` (qualquer autenticado com organização: ``establishments.view`` ou
  ``campaigns.view`` ou ``organizations.manage``) · ``GET /templates/current?kind=``
- ``POST /templates`` · ``PATCH /templates/{id}`` · ``POST /templates/{id}/publish``
  (``organizations.manage`` GLOBAL)
- ``POST /accept`` (aceite eletrônico do cliente logado; sem impersonation)
- ``POST /acceptances/manual`` (registro de contrato em papel — equipe RD)
- ``GET /acceptances`` · ``GET /acceptances/{id}`` · ``GET /acceptances/{id}/pdf``
``/api/v1/partner-plans`` (planos do parceiro) e ``/api/v1/comodato-agreements``.
"""
from __future__ import annotations

import uuid
from typing import Optional

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.audit import audit
from app.core.authz import AuthContext, require
from app.core.database import get_db
from app.core.errors import Conflict, DomainError, NotFound
from app.core.net import client_ip
from app.core.timeutils import utcnow
from app.models.contracts import (
    ComodatoAgreement,
    ContractAcceptance,
    ContractTemplate,
    PartnerPlan,
)
from app.models.equipment import EquipmentItem
from app.models.establishments import Establishment
from app.modules.contracts import service as svc
from app.modules.establishments.service import ensure_est, est_filter, get_est
from app.modules.organizations.common import iso, paginate, publish_safe, require_any

router = APIRouter()
plans_router = APIRouter()
comodato_router = APIRouter()

READ_TPL = require_any("establishments.view", "campaigns.view", "organizations.manage")
MANAGE_TPL = require("organizations.manage", global_only=True, allow_impersonation=False)


class TemplateIn(BaseModel):
    kind: str = Field(pattern="^(PARTNERSHIP|ADVERTISER_TERMS|COMODATO)$")
    title: str = Field(min_length=5, max_length=200)
    body: str = Field(min_length=50, max_length=200_000)


class TemplatePatch(BaseModel):
    title: Optional[str] = Field(default=None, min_length=5, max_length=200)
    body: Optional[str] = Field(default=None, min_length=50, max_length=200_000)


class AcceptIn(BaseModel):
    template_id: uuid.UUID
    org_id: uuid.UUID
    establishment_id: Optional[uuid.UUID] = None
    comodato_agreement_id: Optional[uuid.UUID] = None


class ManualIn(AcceptIn):
    signer_name: str = Field(min_length=3, max_length=255)
    notes: str = Field(min_length=20, max_length=2000)


# ─── Modelos ─────────────────────────────────────────────────────────────────

@router.get("/templates")
async def list_templates(kind: Optional[str] = Query(None, pattern="^(PARTNERSHIP|ADVERTISER_TERMS|COMODATO)$"),
                         status: Optional[str] = Query(None, pattern="^(DRAFT|PUBLISHED|RETIRED)$"),
                         ctx: AuthContext = Depends(READ_TPL), db: AsyncSession = Depends(get_db)):
    q = select(ContractTemplate)
    staff = ctx.can("organizations.manage", global_only=True)
    if not staff:
        q = q.where(ContractTemplate.status != "DRAFT")
    if kind:
        q = q.where(ContractTemplate.kind == kind)
    if status:
        q = q.where(ContractTemplate.status == status)
    rows = (await db.execute(q.order_by(ContractTemplate.kind, ContractTemplate.version.desc()))).scalars().all()
    return {"items": [svc.template_out(t, include_body=False) for t in rows], "total": len(rows), "page": 1,
            "page_size": max(len(rows), 1)}


@router.get("/templates/current")
async def current_template(kind: str = Query(pattern="^(PARTNERSHIP|ADVERTISER_TERMS|COMODATO)$"),
                           ctx: AuthContext = Depends(READ_TPL), db: AsyncSession = Depends(get_db)):
    t = await svc.published_template(db, kind)
    if t is None:
        raise NotFound("CONTRACT_NOT_PUBLISHED", "Não há versão publicada deste contrato.")
    return svc.template_out(t)


@router.get("/templates/{template_id}")
async def get_template(template_id: uuid.UUID, ctx: AuthContext = Depends(READ_TPL),
                       db: AsyncSession = Depends(get_db)):
    t = await db.get(ContractTemplate, template_id)
    if t is None or (t.status == "DRAFT" and not ctx.can("organizations.manage", global_only=True)):
        raise NotFound()
    return svc.template_out(t)


@router.post("/templates", status_code=201)
async def create_template(body: TemplateIn, ctx: AuthContext = Depends(MANAGE_TPL),
                          db: AsyncSession = Depends(get_db)):
    t = await svc.create_template(db, ctx, body.kind, body.title, body.body)
    await db.commit()
    return svc.template_out(t)


@router.patch("/templates/{template_id}")
async def patch_template(template_id: uuid.UUID, body: TemplatePatch, ctx: AuthContext = Depends(MANAGE_TPL),
                         db: AsyncSession = Depends(get_db)):
    t = (await db.execute(select(ContractTemplate).where(ContractTemplate.id == template_id).with_for_update())
         ).scalar_one_or_none()
    if t is None:
        raise NotFound()
    await svc.update_template(db, ctx, t, body.title, body.body)
    await db.commit()
    await db.refresh(t)
    return svc.template_out(t)


@router.post("/templates/{template_id}/publish")
async def publish_template(template_id: uuid.UUID, ctx: AuthContext = Depends(MANAGE_TPL),
                           db: AsyncSession = Depends(get_db)):
    t = (await db.execute(select(ContractTemplate).where(ContractTemplate.id == template_id).with_for_update())
         ).scalar_one_or_none()
    if t is None:
        raise NotFound()
    await svc.publish_template(db, ctx, t)
    await db.commit()
    await db.refresh(t)
    return svc.template_out(t)


# ─── Aceites ─────────────────────────────────────────────────────────────────

async def _after_activation(est: Optional[Establishment]) -> None:
    if est is None:
        return
    await publish_safe("establishment.updated", {"id": str(est.id), "org_id": str(est.org_id),
                                                 "change": "contract_activated"},
                       org_id=est.org_id, establishment_id=est.id, city=est.city_key)


@router.post("/accept")
async def accept_contract(body: AcceptIn, request: Request,
                          ctx: AuthContext = Depends(require_any("establishments.view", "campaigns.view",
                                                                 allow_impersonation=False)),
                          db: AsyncSession = Depends(get_db)):
    acc, created, activated = await svc.accept(
        db, ctx, template_id=body.template_id, org_id=body.org_id, establishment_id=body.establishment_id,
        comodato_agreement_id=body.comodato_agreement_id, ip=client_ip(request),
        user_agent=request.headers.get("user-agent"),
    )
    await db.commit()
    await _after_activation(activated)
    return {**svc.acceptance_out(acc), "created": created,
            "establishment_activated": str(activated.id) if activated else None}


@router.post("/acceptances/manual", status_code=201)
async def record_manual(body: ManualIn, ctx: AuthContext = Depends(MANAGE_TPL), db: AsyncSession = Depends(get_db)):
    acc, activated = await svc.record_manual(
        db, ctx, template_id=body.template_id, org_id=body.org_id, establishment_id=body.establishment_id,
        comodato_agreement_id=body.comodato_agreement_id, signer_name=body.signer_name, notes=body.notes,
    )
    await db.commit()
    await _after_activation(activated)
    return {**svc.acceptance_out(acc), "establishment_activated": str(activated.id) if activated else None}


def _acc_visible(ctx: AuthContext, a: ContractAcceptance) -> bool:
    if ctx.can("organizations.view", org_id=a.org_id):
        return True
    if a.establishment_id is not None and ctx.can("establishments.view", org_id=a.org_id,
                                                  establishment_id=a.establishment_id):
        return True
    return a.kind == "ADVERTISER_TERMS" and ctx.can("campaigns.view", org_id=a.org_id)


@router.get("/acceptances")
async def list_acceptances(org_id: uuid.UUID, establishment_id: Optional[uuid.UUID] = None,
                           page: int = Query(1, ge=1), page_size: int = Query(25, ge=1, le=100),
                           ctx: AuthContext = Depends(require_any("organizations.view", "establishments.view",
                                                                  "campaigns.view")),
                           db: AsyncSession = Depends(get_db)):
    if not (ctx.can("organizations.view", org_id=org_id) or ctx.can("establishments.view", org_id=org_id)
            or ctx.can("campaigns.view", org_id=org_id)
            or (establishment_id and ctx.can("establishments.view", establishment_id=establishment_id))):
        raise NotFound()
    q = select(ContractAcceptance).where(ContractAcceptance.org_id == org_id)
    if establishment_id:
        q = q.where(ContractAcceptance.establishment_id == establishment_id)
    q = q.order_by(ContractAcceptance.accepted_at.desc())
    page_data = await paginate(db, q, page, page_size, lambda a: a)
    page_data["items"] = [svc.acceptance_out(a) for a in page_data["items"] if _acc_visible(ctx, a)]
    return page_data


async def _acceptance(db: AsyncSession, ctx: AuthContext, acc_id: uuid.UUID) -> ContractAcceptance:
    a = await db.get(ContractAcceptance, acc_id)
    if a is None or not _acc_visible(ctx, a):
        raise NotFound()
    return a


@router.get("/acceptances/{acc_id}")
async def get_acceptance(acc_id: uuid.UUID,
                         ctx: AuthContext = Depends(require_any("organizations.view", "establishments.view",
                                                                "campaigns.view")),
                         db: AsyncSession = Depends(get_db)):
    return svc.acceptance_out(await _acceptance(db, ctx, acc_id))


@router.get("/acceptances/{acc_id}/pdf")
async def acceptance_pdf(acc_id: uuid.UUID,
                         ctx: AuthContext = Depends(require_any("organizations.view", "establishments.view",
                                                                "campaigns.view")),
                         db: AsyncSession = Depends(get_db)):
    from app.core.storage import ObjectNotFound, get_storage

    a = await _acceptance(db, ctx, acc_id)
    if not a.pdf_storage_key:
        raise NotFound()
    st = get_storage()
    try:
        info = await st.stat(a.pdf_storage_key)
        body = await st.open_range(a.pdf_storage_key, 0, info.size - 1)
    except ObjectNotFound:
        raise NotFound("CONTRACT_PDF_MISSING", "Arquivo do contrato não encontrado.") from None
    return StreamingResponse(body, media_type="application/pdf", headers={
        "Content-Disposition": f'attachment; filename="contrato-{a.kind.lower()}-v{a.version}.pdf"',
        "Content-Length": str(info.size), "X-Content-SHA256": a.pdf_sha256 or "", "Cache-Control": "no-store"})


# ─── Planos do parceiro ──────────────────────────────────────────────────────

class PlanIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=2, max_length=120)
    description: Optional[str] = Field(default=None, max_length=4000)
    monthly_fee_cents: int = Field(default=0, ge=0)
    tv_provision: str = Field(pattern="^(OWN_TV|RD_TV_COMODATO)$")
    default_tv_inches: Optional[int] = Field(default=None, gt=0, le=200)
    deposit_required: bool
    deposit_cents: int = Field(default=0, ge=0)
    installation_fee_required: bool
    installation_fee_cents: int = Field(default=0, ge=0)
    upgrade_policy: str = Field(pattern="^(FREE|PAID|NEGOTIATED)$")
    upgrade_fee_cents: Optional[int] = Field(default=None, ge=0)
    receives_commission: bool = True
    default_internal_s: Optional[int] = Field(default=None, ge=0, le=3600)
    default_external_s: Optional[int] = Field(default=None, ge=30, le=3600)
    allow_external_overflow: bool = False


class PlanPatch(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: Optional[str] = Field(default=None, min_length=2, max_length=120)
    description: Optional[str] = Field(default=None, max_length=4000)
    monthly_fee_cents: Optional[int] = Field(default=None, ge=0)
    default_tv_inches: Optional[int] = Field(default=None, gt=0, le=200)
    deposit_required: Optional[bool] = None
    deposit_cents: Optional[int] = Field(default=None, ge=0)
    installation_fee_required: Optional[bool] = None
    installation_fee_cents: Optional[int] = Field(default=None, ge=0)
    upgrade_policy: Optional[str] = Field(default=None, pattern="^(FREE|PAID|NEGOTIATED)$")
    upgrade_fee_cents: Optional[int] = Field(default=None, ge=0)
    receives_commission: Optional[bool] = None
    default_internal_s: Optional[int] = Field(default=None, ge=0, le=3600)
    default_external_s: Optional[int] = Field(default=None, ge=30, le=3600)
    allow_external_overflow: Optional[bool] = None
    is_active: Optional[bool] = None


def _plan_out(p: PartnerPlan) -> dict:
    return {
        "id": str(p.id), "name": p.name, "slug": p.slug, "description": p.description,
        "monthly_fee_cents": p.monthly_fee_cents, "is_free": p.monthly_fee_cents == 0, "tv_provision": p.tv_provision,
        "default_tv_inches": p.default_tv_inches, "deposit_required": p.deposit_required,
        "deposit_cents": p.deposit_cents, "installation_fee_required": p.installation_fee_required,
        "installation_fee_cents": p.installation_fee_cents, "upgrade_policy": p.upgrade_policy,
        "upgrade_fee_cents": p.upgrade_fee_cents, "receives_commission": p.receives_commission,
        "default_internal_s": p.default_internal_s, "default_external_s": p.default_external_s,
        "allow_external_overflow": p.allow_external_overflow, "is_active": p.is_active,
        "created_at": iso(p.created_at), "updated_at": iso(p.updated_at),
    }


def _validate_plan(p: PartnerPlan) -> None:
    if (p.default_internal_s is None) != (p.default_external_s is None):
        raise DomainError("ROTATION_INCOMPLETE", "Informe tempo interno e externo juntos.", status=400)
    if p.default_internal_s is not None:
        i, e = p.default_internal_s, p.default_external_s
        if i % 30 or e % 30 or 3600 % (i + e):
            raise DomainError("ROTATION_INVALID",
                              "Tempos em múltiplos de 30 s e ciclo (interno + externo) divisor de 3600 s.", status=400)
    if not p.deposit_required and p.deposit_cents:
        raise DomainError("DEPOSIT_INCONSISTENT", "Caução com valor exige 'caução: sim'.", status=400)
    if not p.installation_fee_required and p.installation_fee_cents:
        raise DomainError("INSTALLATION_FEE_INCONSISTENT", "Taxa de instalação com valor exige 'instalação paga'.",
                          status=400)


@plans_router.get("")
async def list_plans(include_inactive: bool = False,
                     ctx: AuthContext = Depends(require_any("establishments.view", "organizations.view")),
                     db: AsyncSession = Depends(get_db)):
    q = select(PartnerPlan)
    if not include_inactive:
        q = q.where(PartnerPlan.is_active.is_(True))
    rows = (await db.execute(q.order_by(PartnerPlan.monthly_fee_cents, PartnerPlan.name))).scalars().all()
    return {"items": [_plan_out(p) for p in rows], "total": len(rows), "page": 1, "page_size": max(len(rows), 1)}


@plans_router.post("", status_code=201)
async def create_plan(body: PlanIn, ctx: AuthContext = Depends(require("establishments.manage", global_only=True)),
                      db: AsyncSession = Depends(get_db)):
    from app.modules.taxonomy.service import _unique_slug

    p = PartnerPlan(id=uuid.uuid4(), slug=await _unique_slug(db, PartnerPlan, body.name), created_by=ctx.user_id,
                    **body.model_dump())
    _validate_plan(p)
    db.add(p)
    await db.flush()
    await audit(db, ctx, "partner_plan.created", "partner_plan", p.id, after=_plan_out(p))
    await db.commit()
    return _plan_out(p)


@plans_router.patch("/{plan_id}")
async def patch_plan(plan_id: uuid.UUID, body: PlanPatch,
                     ctx: AuthContext = Depends(require("establishments.manage", global_only=True)),
                     db: AsyncSession = Depends(get_db)):
    p = (await db.execute(select(PartnerPlan).where(PartnerPlan.id == plan_id).with_for_update())).scalar_one_or_none()
    if p is None:
        raise NotFound()
    before = _plan_out(p)
    for k, v in body.model_dump(exclude_unset=True).items():
        setattr(p, k, v)
    _validate_plan(p)
    await db.flush()
    await db.refresh(p)
    after = _plan_out(p)
    await audit(db, ctx, "partner_plan.updated", "partner_plan", p.id,
                before={k: before[k] for k in after if before[k] != after[k]},
                after={k: after[k] for k in after if before[k] != after[k]})
    await db.commit()
    return after


# ─── Acordos de comodato ─────────────────────────────────────────────────────

class ComodatoIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    establishment_id: uuid.UUID
    screen_id: Optional[uuid.UUID] = None
    partner_plan_id: Optional[uuid.UUID] = None
    deposit_required: bool
    deposit_cents: int = Field(default=0, ge=0)
    installation_fee_required: bool
    installation_fee_cents: int = Field(default=0, ge=0)
    upgrade_policy: str = Field(pattern="^(FREE|PAID|NEGOTIATED)$")
    upgrade_fee_cents: Optional[int] = Field(default=None, ge=0)
    maintenance_paid_by: str = Field(default="RD", pattern="^(RD|ESTABLISHMENT)$")
    breakage_paid_by: str = Field(default="ESTABLISHMENT", pattern="^(RD|ESTABLISHMENT)$")
    notes: Optional[str] = Field(default=None, max_length=4000)


class EndIn(BaseModel):
    reason: str = Field(min_length=10, max_length=2000)


def _ag_out(a: ComodatoAgreement) -> dict:
    return {
        "id": str(a.id), "org_id": str(a.org_id), "establishment_id": str(a.establishment_id),
        "screen_id": str(a.screen_id) if a.screen_id else None,
        "partner_plan_id": str(a.partner_plan_id) if a.partner_plan_id else None,
        "deposit_required": a.deposit_required, "deposit_cents": a.deposit_cents,
        "installation_fee_required": a.installation_fee_required, "installation_fee_cents": a.installation_fee_cents,
        "upgrade_policy": a.upgrade_policy, "upgrade_fee_cents": a.upgrade_fee_cents,
        "maintenance_paid_by": a.maintenance_paid_by, "breakage_paid_by": a.breakage_paid_by,
        "status": a.status, "started_at": iso(a.started_at), "ended_at": iso(a.ended_at),
        "end_reason": a.end_reason, "notes": a.notes,
    }


@comodato_router.get("")
async def list_comodato(establishment_id: Optional[uuid.UUID] = None, status: Optional[str] = Query(
        None, pattern="^(ACTIVE|ENDED)$"), page: int = Query(1, ge=1), page_size: int = Query(25, ge=1, le=100),
        ctx: AuthContext = Depends(require("establishments.view")), db: AsyncSession = Depends(get_db)):
    q = (select(ComodatoAgreement, Establishment.name.label("establishment_name"))
         .join(Establishment, Establishment.id == ComodatoAgreement.establishment_id)
         .where(est_filter(ctx, "establishments.view")))
    if establishment_id:
        q = q.where(ComodatoAgreement.establishment_id == establishment_id)
    if status:
        q = q.where(ComodatoAgreement.status == status)
    q = q.order_by(ComodatoAgreement.started_at.desc(), ComodatoAgreement.id)
    return await paginate(db, q, page, page_size,
                          lambda row: {**_ag_out(row[0]), "establishment_name": row.establishment_name},
                          scalars=False)


@comodato_router.post("", status_code=201)
async def create_comodato(body: ComodatoIn, ctx: AuthContext = Depends(require("establishments.manage")),
                          db: AsyncSession = Depends(get_db)):
    from app.models.screens import Screen

    est = await get_est(db, body.establishment_id)
    ensure_est(ctx, "establishments.manage", est)
    if body.screen_id is not None:
        s = await db.get(Screen, body.screen_id)
        if s is None or s.establishment_id != est.id:
            raise NotFound()
    if body.partner_plan_id is not None and await db.get(PartnerPlan, body.partner_plan_id) is None:
        raise DomainError("PARTNER_PLAN_INVALID", "Plano do parceiro inexistente.", status=400)
    if not body.deposit_required and body.deposit_cents:
        raise DomainError("DEPOSIT_INCONSISTENT", "Caução com valor exige 'caução: sim'.", status=400)
    if not body.installation_fee_required and body.installation_fee_cents:
        raise DomainError("INSTALLATION_FEE_INCONSISTENT", "Taxa de instalação com valor exige 'instalação paga'.",
                          status=400)
    a = ComodatoAgreement(id=uuid.uuid4(), org_id=est.org_id, status="ACTIVE", created_by=ctx.user_id,
                          **body.model_dump())
    db.add(a)
    await db.flush()
    await audit(db, ctx, "comodato.created", "comodato_agreement", a.id, after=_ag_out(a), org_id=est.org_id)
    await db.commit()
    return _ag_out(a)


@comodato_router.get("/{agreement_id}")
async def get_comodato(agreement_id: uuid.UUID, ctx: AuthContext = Depends(require("establishments.view")),
                       db: AsyncSession = Depends(get_db)):
    a = await db.get(ComodatoAgreement, agreement_id)
    if a is None:
        raise NotFound()
    est = await db.get(Establishment, a.establishment_id)
    ensure_est(ctx, "establishments.view", est)
    items = (await db.execute(select(EquipmentItem).where(EquipmentItem.comodato_agreement_id == a.id))).scalars().all()
    from app.modules.assets.service import item_out

    return {**_ag_out(a), "establishment_name": est.name, "items": [item_out(i) for i in items]}


@comodato_router.post("/{agreement_id}/end")
async def end_comodato(agreement_id: uuid.UUID, body: EndIn,
                       ctx: AuthContext = Depends(require("establishments.manage", allow_impersonation=False)),
                       db: AsyncSession = Depends(get_db)):
    a = (await db.execute(select(ComodatoAgreement).where(ComodatoAgreement.id == agreement_id).with_for_update())
         ).scalar_one_or_none()
    if a is None:
        raise NotFound()
    est = await db.get(Establishment, a.establishment_id)
    ensure_est(ctx, "establishments.manage", est)
    if a.status != "ACTIVE":
        raise Conflict("COMODATO_ALREADY_ENDED", "Acordo já encerrado.")
    pending = (await db.execute(select(EquipmentItem.id).where(EquipmentItem.comodato_agreement_id == a.id))).all()
    if pending:
        raise Conflict("COMODATO_ITEMS_PENDING", "Há equipamentos em comodato ainda não devolvidos.",
                       details={"item_ids": [str(r[0]) for r in pending]})
    a.status = "ENDED"
    a.ended_at = utcnow()
    a.end_reason = body.reason
    await db.flush()
    await audit(db, ctx, "comodato.ended", "comodato_agreement", a.id, before={"status": "ACTIVE"},
                after={"status": "ENDED"}, reason=body.reason, org_id=a.org_id)
    await db.commit()
    await db.refresh(a)
    return _ag_out(a)

