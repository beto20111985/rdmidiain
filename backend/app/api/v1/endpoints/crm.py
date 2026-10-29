"""RD Mídia Indoor — CRM / Comercial (AÇÃO 002 / W-BIZ). Prefixo ``/api/v1/crm``.

Leads (funil), atividades, propostas (desconto com aprovação, PDF, validade 30 dias,
conversão em pedido pelo checkout do W-CAMP), estrutura comercial (representantes) e
atribuições de venda (comissão do vendedor, lidas pelo W-FIN).
"""
from __future__ import annotations

from datetime import date, datetime
from typing import Literal, Optional
from uuid import UUID

from fastapi import APIRouter, Depends, Header, Query, Response
from pydantic import BaseModel, EmailStr, Field
from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.audit import audit
from app.core.authz import AuthContext, require
from app.core.database import get_db
from app.core.errors import DomainError, NotFound
from app.core.timeutils import isoformat_z
from app.models.crm import Lead, LeadActivity, LeadStageEvent, Proposal, SalesAttribution, SalesRep
from app.modules.crm import service as svc

router = APIRouter()


def _z(dt: Optional[datetime]) -> Optional[str]:
    return isoformat_z(dt) if dt else None


def _page(items, total, page, page_size) -> dict:
    return {"items": items, "total": total, "page": page, "page_size": page_size}


# Nomes exibidos (follow-up FE-FIN): para cada ``*_id`` de pessoa/organização da resposta, acrescenta
# o NOME correspondente (só o nome — sem e-mail/telefone/documento). O CRM é exclusivo da equipe RD e
# as respostas já estão filtradas pelo escopo do leitor (só ids que ele pode ver chegam aqui).
_USER_KEYS = {"owner_id": "owner_name", "seller_id": "seller_name", "user_id": "user_name",
              "actor_id": "actor_name", "created_by": "created_by_name",
              "discount_approved_by": "discount_approved_by_name"}
_ORG_KEYS = {"org_id": "org_name"}
_REP_KEYS = {"manager_id": "manager_name"}
_REMOVED = "Usuário removido"


def _walk(o, fn) -> None:
    if isinstance(o, dict):
        fn(o)
        for v in list(o.values()):
            if isinstance(v, (dict, list)):
                _walk(v, fn)
    elif isinstance(o, list):
        for v in o:
            _walk(v, fn)


async def _named(db: AsyncSession, payload):
    from app.models.organizations import Organization
    from app.models.users import User

    users, orgs, reps = set(), set(), set()

    def collect(d: dict) -> None:
        for keys, bucket in ((_USER_KEYS, users), (_ORG_KEYS, orgs), (_REP_KEYS, reps)):
            for k in keys:
                v = d.get(k)
                if v:
                    try:
                        bucket.add(UUID(str(v)))
                    except ValueError:
                        pass

    _walk(payload, collect)
    unames: dict = {}
    if users:
        for uid, name, anon in (await db.execute(
                select(User.id, User.full_name, User.anonymized_at).where(User.id.in_(users)))).all():
            unames[str(uid)] = _REMOVED if anon is not None else name
    onames = {str(i): n for i, n in (await db.execute(
        select(Organization.id, Organization.name).where(Organization.id.in_(orgs))))} if orgs else {}
    rnames = {str(i): (_REMOVED if anon is not None else n) for i, n, anon in (await db.execute(
        select(SalesRep.id, User.full_name, User.anonymized_at).join(User, User.id == SalesRep.user_id)
        .where(SalesRep.id.in_(reps))))} if reps else {}

    def fill(d: dict) -> None:
        for keys, names in ((_USER_KEYS, unames), (_ORG_KEYS, onames), (_REP_KEYS, rnames)):
            for k, label in keys.items():
                if k in d:
                    d[label] = names.get(str(d[k])) if d[k] else None

    _walk(payload, fill)
    return payload


# ─── Leads ───────────────────────────────────────────────────────────────────

def _lead_out(l: Lead) -> dict:
    return {
        "id": str(l.id), "name": l.name, "company": l.company, "email": l.email, "phone": l.phone,
        "source": l.source, "interest": l.interest, "stage": l.stage, "owner_id": str(l.owner_id) if l.owner_id else None,
        "city": l.city, "uf": l.uf, "city_key": l.city_key, "org_id": str(l.org_id) if l.org_id else None,
        "estimated_value_cents": l.estimated_value_cents, "lost_reason": l.lost_reason, "notes": l.notes,
        "stage_changed_at": _z(l.stage_changed_at), "created_at": _z(l.created_at), "updated_at": _z(l.updated_at),
        "created_by": str(l.created_by) if l.created_by else None,
    }


class LeadIn(BaseModel):
    name: str = Field(min_length=2, max_length=255)
    company: Optional[str] = Field(None, max_length=255)
    email: Optional[EmailStr] = None
    phone: Optional[str] = Field(None, max_length=30)
    source: str = Field("manual", max_length=50)
    interest: Optional[Literal["ANUNCIANTE", "PARCEIRO", "AMBOS"]] = None
    city: Optional[str] = Field(None, max_length=100)
    uf: Optional[str] = Field(None, min_length=2, max_length=2)
    owner_id: Optional[UUID] = None
    org_id: Optional[UUID] = None
    estimated_value_cents: Optional[int] = Field(None, ge=0)
    notes: Optional[str] = Field(None, max_length=5000)


class LeadPatch(BaseModel):
    name: Optional[str] = Field(None, min_length=2, max_length=255)
    company: Optional[str] = Field(None, max_length=255)
    email: Optional[EmailStr] = None
    phone: Optional[str] = Field(None, max_length=30)
    interest: Optional[Literal["ANUNCIANTE", "PARCEIRO", "AMBOS"]] = None
    owner_id: Optional[UUID] = None
    org_id: Optional[UUID] = None
    estimated_value_cents: Optional[int] = Field(None, ge=0)
    notes: Optional[str] = Field(None, max_length=5000)


@router.get("/leads")
async def list_leads(
    stage: Optional[str] = None, owner_id: Optional[UUID] = None, q: Optional[str] = Query(None, max_length=100),
    page: int = Query(1, ge=1), page_size: int = Query(25, ge=1, le=100),
    ctx: AuthContext = Depends(require("crm.view")), db: AsyncSession = Depends(get_db),
):
    cond = [svc.scope_clause(ctx, "crm.view", Lead.city_key, Lead.owner_id)]
    if stage:
        cond.append(Lead.stage == stage)
    if owner_id:
        cond.append(Lead.owner_id == owner_id)
    if q:
        like = f"%{q.replace('%', '').replace('_', '')}%"
        cond.append(or_(Lead.name.ilike(like), Lead.company.ilike(like), Lead.email.ilike(like)))
    total = (await db.execute(select(func.count()).select_from(Lead).where(*cond))).scalar_one()
    rows = (await db.execute(select(Lead).where(*cond).order_by(Lead.created_at.desc())
                             .offset((page - 1) * page_size).limit(page_size))).scalars()
    return await _named(db, _page([_lead_out(l) for l in rows], total, page, page_size))


@router.post("/leads", status_code=201)
async def create_lead(body: LeadIn, ctx: AuthContext = Depends(require("crm.manage")), db: AsyncSession = Depends(get_db)):
    lead = await svc.create_lead(db, ctx, body.model_dump())
    return await _named(db, _lead_out(lead))


@router.get("/leads/{lead_id}")
async def get_lead(lead_id: UUID, ctx: AuthContext = Depends(require("crm.view")), db: AsyncSession = Depends(get_db)):
    lead = await svc.get_lead(db, ctx, lead_id)
    acts = (await db.execute(select(LeadActivity).where(LeadActivity.lead_id == lead.id)
                             .order_by(LeadActivity.occurred_at.desc()))).scalars()
    hist = (await db.execute(select(LeadStageEvent).where(LeadStageEvent.lead_id == lead.id)
                             .order_by(LeadStageEvent.created_at))).scalars()
    props = (await db.execute(select(Proposal).where(Proposal.lead_id == lead.id).order_by(Proposal.created_at.desc()))).scalars()
    out = _lead_out(lead)
    out["activities"] = [{"id": str(a.id), "kind": a.kind, "description": a.description,
                          "occurred_at": _z(a.occurred_at), "actor_id": str(a.actor_id) if a.actor_id else None}
                         for a in acts]
    out["stage_history"] = [{"from": h.from_stage, "to": h.to_stage, "reason": h.reason, "at": _z(h.created_at),
                             "actor_id": str(h.actor_id) if h.actor_id else None} for h in hist]
    out["proposals"] = [{"id": str(p.id), "number": p.number, "status": svc.effective_status(p),
                         "total_cents": p.total_cents, "seller_id": str(p.seller_id)} for p in props]
    return await _named(db, out)


@router.patch("/leads/{lead_id}")
async def patch_lead(lead_id: UUID, body: LeadPatch, ctx: AuthContext = Depends(require("crm.manage")),
                     db: AsyncSession = Depends(get_db)):
    lead = await svc.get_lead(db, ctx, lead_id, "crm.manage", for_update=True)
    changes = body.model_dump(exclude_unset=True)
    if changes.get("org_id") is not None:
        await svc._assert_client_org(db, changes["org_id"])
    before = {k: getattr(lead, k) for k in changes}
    for k, v in changes.items():
        setattr(lead, k, str(v) if k == "email" and v is not None else v)
    await db.flush()
    await audit(db, ctx, "crm.lead_updated", "lead", lead.id, before=before, after=changes)
    return await _named(db, _lead_out(lead))


class StageIn(BaseModel):
    stage: Literal["NOVO", "CONTATO", "QUALIFICADO", "PROPOSTA", "NEGOCIACAO", "GANHO", "PERDIDO"]
    reason: Optional[str] = Field(None, max_length=500)


@router.post("/leads/{lead_id}/stage")
async def lead_stage(lead_id: UUID, body: StageIn, ctx: AuthContext = Depends(require("crm.manage")),
                     db: AsyncSession = Depends(get_db)):
    return await _named(db, _lead_out(await svc.change_lead_stage(db, ctx, lead_id, body.stage, body.reason)))


class ActivityIn(BaseModel):
    kind: Literal["NOTA", "LIGACAO", "REUNIAO", "EMAIL", "WHATSAPP", "VISITA"]
    description: str = Field(min_length=1, max_length=5000)
    occurred_at: Optional[datetime] = None


@router.post("/leads/{lead_id}/activities", status_code=201)
async def lead_activity(lead_id: UUID, body: ActivityIn, ctx: AuthContext = Depends(require("crm.manage")),
                        db: AsyncSession = Depends(get_db)):
    a = await svc.add_activity(db, ctx, lead_id, body.kind, body.description, body.occurred_at)
    return {"id": str(a.id), "kind": a.kind, "description": a.description, "occurred_at": _z(a.occurred_at)}


@router.get("/client-options")
async def client_options(search: Optional[str] = Query(None, max_length=100), limit: int = Query(20, ge=1, le=50),
                         ctx: AuthContext = Depends(require("crm.manage")), db: AsyncSession = Depends(get_db)):
    """Seletor de CLIENTE da proposta: anunciantes/agências ativos, sem a organização da própria instância."""
    return {"items": await svc.client_options(db, ctx, search=search, limit=limit)}


@router.get("/pipeline")
async def get_pipeline(ctx: AuthContext = Depends(require("crm.view")), db: AsyncSession = Depends(get_db)):
    return await svc.pipeline(db, ctx)


# ─── Propostas ───────────────────────────────────────────────────────────────

def _proposal_out(p: Proposal, items=None) -> dict:
    out = {
        "id": str(p.id), "number": p.number, "title": p.title, "status": svc.effective_status(p),
        "stored_status": p.status, "lead_id": str(p.lead_id) if p.lead_id else None,
        "org_id": str(p.org_id) if p.org_id else None, "seller_id": str(p.seller_id), "city_key": p.city_key,
        "subtotal_cents": p.subtotal_cents, "discount_bps": p.discount_bps, "discount_cents": p.discount_cents,
        "total_cents": p.total_cents, "discount_limit_bps": p.discount_limit_bps_snapshot,
        "discount_approval_required": p.discount_approval_required, "discount_justification": p.discount_justification,
        "discount_approved_by": str(p.discount_approved_by) if p.discount_approved_by else None,
        "discount_approved_at": _z(p.discount_approved_at), "discount_decision_reason": p.discount_decision_reason,
        "validity_days": p.validity_days, "valid_until": p.valid_until.isoformat() if p.valid_until else None,
        "expires_at": _z(p.expires_at), "sent_at": _z(p.sent_at), "accepted_at": _z(p.accepted_at),
        "converted_at": _z(p.converted_at), "order_id": str(p.order_id) if p.order_id else None,
        "pdf_sha256": p.pdf_sha256, "notes": p.notes, "version": p.version,
        "created_at": _z(p.created_at), "updated_at": _z(p.updated_at),
    }
    if items is not None:
        out["items"] = [{"id": str(i.id), "position": i.position, "package_code": i.package_code,
                         "description": i.description, "quantity": i.quantity, "unit_price_cents": i.unit_price_cents,
                         "line_total_cents": i.line_total_cents, "details": i.details or {}} for i in items]
    return out


class ItemIn(BaseModel):
    package_code: Optional[str] = Field(None, max_length=60)
    description: str = Field(min_length=1, max_length=500)
    quantity: int = Field(1, ge=1, le=100000)
    unit_price_cents: int = Field(ge=0)
    details: Optional[dict] = None


class ProposalIn(BaseModel):
    title: str = Field(min_length=3, max_length=255)
    lead_id: Optional[UUID] = None
    org_id: Optional[UUID] = None
    city: Optional[str] = Field(None, max_length=100)
    uf: Optional[str] = Field(None, min_length=2, max_length=2)
    discount_bps: int = Field(0, ge=0, le=10000)
    discount_justification: Optional[str] = Field(None, max_length=1000)
    notes: Optional[str] = Field(None, max_length=5000)
    items: list[ItemIn] = Field(default_factory=list, max_length=100)


class ProposalPatch(BaseModel):
    title: Optional[str] = Field(None, min_length=3, max_length=255)
    org_id: Optional[UUID] = None
    discount_bps: Optional[int] = Field(None, ge=0, le=10000)
    discount_justification: Optional[str] = Field(None, max_length=1000)
    notes: Optional[str] = Field(None, max_length=5000)
    items: Optional[list[ItemIn]] = Field(None, max_length=100)


@router.get("/proposals")
async def list_proposals(
    status: Optional[str] = None, lead_id: Optional[UUID] = None, seller_id: Optional[UUID] = None,
    page: int = Query(1, ge=1), page_size: int = Query(25, ge=1, le=100),
    ctx: AuthContext = Depends(require("crm.view")), db: AsyncSession = Depends(get_db),
):
    cond = [svc.scope_clause(ctx, "crm.view", Proposal.city_key, Proposal.seller_id)]
    if status:
        cond.append(Proposal.status == status)
    if lead_id:
        cond.append(Proposal.lead_id == lead_id)
    if seller_id:
        cond.append(Proposal.seller_id == seller_id)
    total = (await db.execute(select(func.count()).select_from(Proposal).where(*cond))).scalar_one()
    rows = (await db.execute(select(Proposal).where(*cond).order_by(Proposal.created_at.desc())
                             .offset((page - 1) * page_size).limit(page_size))).scalars()
    return await _named(db, _page([_proposal_out(p) for p in rows], total, page, page_size))


@router.post("/proposals", status_code=201)
async def create_proposal(body: ProposalIn, ctx: AuthContext = Depends(require("crm.manage")),
                          db: AsyncSession = Depends(get_db)):
    data = body.model_dump()
    data["items"] = [i.model_dump() for i in body.items]
    p = await svc.create_proposal(db, ctx, data)
    return await _named(db, _proposal_out(p, await svc.load_items(db, p.id)))


@router.get("/proposals/{proposal_id}")
async def get_proposal(proposal_id: UUID, ctx: AuthContext = Depends(require("crm.view")),
                       db: AsyncSession = Depends(get_db)):
    p = await svc.get_proposal(db, ctx, proposal_id)
    return await _named(db, _proposal_out(p, await svc.load_items(db, p.id)))


@router.patch("/proposals/{proposal_id}")
async def patch_proposal(proposal_id: UUID, body: ProposalPatch, ctx: AuthContext = Depends(require("crm.manage")),
                         db: AsyncSession = Depends(get_db)):
    changes = body.model_dump(exclude_unset=True)
    if body.items is not None:
        changes["items"] = [i.model_dump() for i in body.items]
    p = await svc.update_proposal(db, ctx, proposal_id, changes)
    return await _named(db, _proposal_out(p, await svc.load_items(db, p.id)))


@router.post("/proposals/{proposal_id}/submit")
async def submit(proposal_id: UUID, ctx: AuthContext = Depends(require("crm.manage")), db: AsyncSession = Depends(get_db)):
    p = await svc.submit_proposal(db, ctx, proposal_id)
    return await _named(db, _proposal_out(p, await svc.load_items(db, p.id)))


class DecisionIn(BaseModel):
    approve: bool
    reason: Optional[str] = Field(None, max_length=1000)


@router.post("/proposals/{proposal_id}/discount-decision")
async def discount_decision(proposal_id: UUID, body: DecisionIn,
                            ctx: AuthContext = Depends(require("crm.discount_approve")),
                            db: AsyncSession = Depends(get_db)):
    p = await svc.decide_discount(db, ctx, proposal_id, body.approve, body.reason)
    return await _named(db, _proposal_out(p))


class SendIn(BaseModel):
    email_to: Optional[EmailStr] = None


@router.post("/proposals/{proposal_id}/send")
async def send(proposal_id: UUID, body: SendIn, ctx: AuthContext = Depends(require("crm.manage")),
               db: AsyncSession = Depends(get_db)):
    p, _pdf, emailed = await svc.send_proposal(db, ctx, proposal_id, str(body.email_to) if body.email_to else None)
    out = _proposal_out(p)
    out["emailed"] = emailed
    return await _named(db, out)


@router.get("/proposals/{proposal_id}/pdf")
async def proposal_pdf(proposal_id: UUID, ctx: AuthContext = Depends(require("crm.view")),
                       db: AsyncSession = Depends(get_db)):
    from app.core.storage import get_storage

    p = await svc.get_proposal(db, ctx, proposal_id)
    if p.pdf_key:
        data = await get_storage().read_bytes(p.pdf_key, max_bytes=20 * 1024 * 1024)
    else:
        data = await svc.render_pdf(db, p)  # rascunho: gerado na hora, marcado como RASCUNHO
    return Response(content=data, media_type="application/pdf",
                    headers={"Content-Disposition": f'attachment; filename="proposta-{p.number}.pdf"',
                             "Cache-Control": "no-store"})


class ClientDecisionIn(BaseModel):
    accepted: bool
    note: Optional[str] = Field(None, max_length=1000)


@router.post("/proposals/{proposal_id}/client-decision")
async def client_decision(proposal_id: UUID, body: ClientDecisionIn, ctx: AuthContext = Depends(require("crm.manage")),
                          db: AsyncSession = Depends(get_db)):
    return await _named(db, _proposal_out(await svc.record_client_decision(db, ctx, proposal_id, body.accepted,
                                                                          body.note)))


class CancelIn(BaseModel):
    reason: str = Field(min_length=3, max_length=500)


@router.post("/proposals/{proposal_id}/cancel")
async def cancel(proposal_id: UUID, body: CancelIn, ctx: AuthContext = Depends(require("crm.manage")),
                 db: AsyncSession = Depends(get_db)):
    return await _named(db, _proposal_out(await svc.cancel_proposal(db, ctx, proposal_id, body.reason)))


@router.get("/proposals/{proposal_id}/convertible-campaigns")
async def convertible_campaigns(proposal_id: UUID, ctx: AuthContext = Depends(require("crm.manage")),
                                db: AsyncSession = Depends(get_db)):
    """Campanhas do CLIENTE da proposta elegíveis para a conversão (mesma regra do checkout do W-CAMP:
    RASCUNHO ou REJEITADA que exige novo pagamento). Basta ``crm.manage`` no escopo da proposta (não exige
    ``campaigns.view``); expõe só id, nome, status, período e pendências conhecidas."""
    return {"items": await svc.convertible_campaigns(db, ctx, proposal_id)}


class ConvertIn(BaseModel):
    campaign_ids: list[UUID] = Field(min_length=1, max_length=50)
    coupon_code: Optional[str] = Field(None, max_length=40)


@router.post("/proposals/{proposal_id}/convert")
async def convert(proposal_id: UUID, body: ConvertIn,
                  idempotency_key: str = Header(..., alias="Idempotency-Key", min_length=8, max_length=100),
                  ctx: AuthContext = Depends(require("crm.manage")), db: AsyncSession = Depends(get_db)):
    res = await svc.convert_proposal(db, ctx, proposal_id, campaign_ids=body.campaign_ids,
                                     idempotency_key=idempotency_key, coupon_code=body.coupon_code)
    out = _proposal_out(res["proposal"])
    out.update({"order_id": str(res["order_id"]), "order_total_cents": res["order_total_cents"],
                "proposal_total_cents": res["proposal_total_cents"], "checkout": res["checkout"]})
    return await _named(db, out)


# ─── Estrutura comercial ─────────────────────────────────────────────────────

def _rep_out(r: SalesRep) -> dict:
    return {"id": str(r.id), "user_id": str(r.user_id), "kind": r.kind, "region_label": r.region_label,
            "city_key": r.city_key, "uf": r.uf, "manager_id": str(r.manager_id) if r.manager_id else None,
            "compensation": r.compensation, "commission_bps": r.commission_bps, "is_active": r.is_active}


class RepIn(BaseModel):
    user_id: UUID
    kind: Literal["VENDEDOR", "REPRESENTANTE", "FRANQUIA", "GERENTE"] = "VENDEDOR"
    region_label: Optional[str] = Field(None, max_length=120)
    city: Optional[str] = Field(None, max_length=100)
    uf: Optional[str] = Field(None, min_length=2, max_length=2)
    manager_id: Optional[UUID] = None
    compensation: Literal["COMISSAO", "SALARIO_FIXO", "AMBOS"] = "COMISSAO"
    commission_bps: Optional[int] = Field(None, ge=0, le=10000)
    is_active: bool = True


@router.get("/sales-reps")
async def list_reps(ctx: AuthContext = Depends(require("crm.view", global_only=True)), db: AsyncSession = Depends(get_db)):
    rows = (await db.execute(select(SalesRep).order_by(SalesRep.created_at))).scalars()
    return await _named(db, {"items": [_rep_out(r) for r in rows]})


@router.put("/sales-reps")
async def upsert_rep(body: RepIn, ctx: AuthContext = Depends(require("crm.manage", global_only=True)),
                     db: AsyncSession = Depends(get_db)):
    from app.core.authz import city_key as ck
    from app.models.users import User

    if await db.get(User, body.user_id) is None:
        raise NotFound()
    rep = (await db.execute(select(SalesRep).where(SalesRep.user_id == body.user_id).with_for_update())).scalar_one_or_none()
    old_bps = rep.commission_bps if rep else None
    if body.commission_bps != old_bps and not ctx.can("commissions.adjust", global_only=True):
        raise DomainError("FORBIDDEN", "Alterar o percentual de comissão exige commissions.adjust.", status=403,
                          details={"permission": "commissions.adjust"})
    if rep is None:
        rep = SalesRep(user_id=body.user_id)
        db.add(rep)
    rep.kind, rep.region_label, rep.uf = body.kind, body.region_label, body.uf
    rep.city_key = ck(body.uf, body.city) if body.uf and body.city else None
    rep.manager_id, rep.compensation, rep.commission_bps, rep.is_active = (
        body.manager_id, body.compensation, body.commission_bps, body.is_active)
    await db.flush()
    await audit(db, ctx, "crm.sales_rep_saved", "sales_rep", rep.id, before={"commission_bps": old_bps},
                after=_rep_out(rep))
    return await _named(db, _rep_out(rep))


@router.get("/attributions")
async def list_attributions(
    seller_id: Optional[UUID] = None, status: Optional[str] = None,
    page: int = Query(1, ge=1), page_size: int = Query(25, ge=1, le=100),
    ctx: AuthContext = Depends(require("crm.view")), db: AsyncSession = Depends(get_db),
):
    cond = []
    if not ctx.can("crm.view", global_only=True):
        cond.append(SalesAttribution.seller_id == ctx.user_id)  # vendedor regional vê só as próprias
    if seller_id:
        cond.append(SalesAttribution.seller_id == seller_id)
    if status:
        cond.append(SalesAttribution.status == status)
    total = (await db.execute(select(func.count()).select_from(SalesAttribution).where(*cond))).scalar_one()
    rows = (await db.execute(select(SalesAttribution).where(*cond).order_by(SalesAttribution.created_at.desc())
                             .offset((page - 1) * page_size).limit(page_size))).scalars()
    return await _named(db, _page([{"id": str(a.id), "proposal_id": str(a.proposal_id), "order_id": str(a.order_id),
                                    "seller_id": str(a.seller_id), "org_id": str(a.org_id),
                                    "amount_cents": a.amount_cents, "commission_bps": a.commission_bps,
                                    "commission_cents": a.commission_cents, "status": a.status,
                                    "created_at": _z(a.created_at)} for a in rows], total, page, page_size))
