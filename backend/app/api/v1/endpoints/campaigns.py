"""
RD Mídia Indoor — API de campanhas e criativos (W-CAMP, AÇÃO 002).

Permissões (contrato §3.1): campaigns.view|manage|approve|kill · creatives.view|manage ·
checkout.create. Escopo por organização (fora do escopo → 404). Aprovação/rejeição e
fila de análise: só equipe RD (``campaigns.approve`` em escopo GLOBAL).
O anunciante NUNCA recebe nomes/ids de estabelecimentos (disponibilidade só por contagem).
"""
from __future__ import annotations

import uuid
from typing import Optional

from fastapi import APIRouter, Depends, Header, Query, Response
from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.audit import audit
from app.core.authz import AuthContext, get_auth_context, require
from app.core.database import get_db
from app.core.errors import Conflict, DomainError, Forbidden, NotFound
from app.core.timeutils import utcnow
from app.models.campaigns import (
    Campaign,
    CampaignCreative,
    CampaignRevision,
    CampaignStatusHistory,
    Creative,
    CreativeStatus,
    OrganizationApprovalPolicy,
)
from app.modules.campaigns import changes as CH
from app.modules.campaigns import checkout as CK
from app.modules.campaigns import creatives as CR
from app.modules.campaigns import schemas as SC
from app.modules.campaigns import service as S
from app.modules.campaigns import states as ST
from app.modules.campaigns import targeting as TG
from app.modules.campaigns.notify import mark, rollback_to
from app.modules.pricing import engine as PE

router = APIRouter()
creatives_router = APIRouter()


# ─── helpers ─────────────────────────────────────────────────────────────────

def resolve_org(ctx: AuthContext, org_id: Optional[uuid.UUID], perm: str) -> uuid.UUID:
    if org_id is not None:
        ctx.ensure(perm, org_id=org_id)
        return org_id
    ac = ctx.active_context or ""
    if ":" in ac:
        try:
            cand = uuid.UUID(ac.partition(":")[2])
        except ValueError:
            cand = None
        if cand is not None and ctx.can(perm, org_id=cand):
            return cand
    orgs = ctx.org_ids(perm)
    if orgs is not None and len(orgs) == 1:
        return next(iter(orgs))
    raise DomainError("ORG_REQUIRED", "Informe a organização (org_id).", status=400)


def is_staff(ctx: AuthContext) -> bool:
    return ctx.can("campaigns.manage", global_only=True) or ctx.can("campaigns.approve", global_only=True)


def sees_exclusions(ctx: AuthContext) -> bool:
    """Diagnóstico de exclusão da disponibilidade (AÇÃO 004): só equipe RD em escopo GLOBAL — Admin/Master/
    Funcionário (``is_staff``) e Comercial (``pricing.view`` GLOBAL, a mesma regra da cotação RD)."""
    return is_staff(ctx) or ctx.can("pricing.view", global_only=True)


def page_out(items, total, page, page_size) -> dict:
    return {"items": items, "total": total, "page": page, "page_size": page_size}


async def campaign_out(db: AsyncSession, ctx: AuthContext, c: Campaign, *, detail: bool = False,
                       reproducing: Optional[bool] = None) -> dict:
    await S.fresh(db, c)
    out = {
        "id": str(c.id), "org_id": str(c.org_id), "name": c.name, "description": c.description, "status": c.status,
        "version": c.version, "sales_mode": c.sales_mode, "plan_id": str(c.plan_id) if c.plan_id else None,
        "period_type": c.period_type, "start_date": c.start_date, "end_date": c.end_date, "duration_s": c.duration_s,
        "freq_kind": c.freq_kind, "freq_value": c.freq_value, "specific_times": c.specific_times or [],
        "min_interval_s": c.min_interval_s, "days_mask": c.days_mask, "time_bands": c.time_bands,
        "priority": c.priority, "exclusivity": c.exclusivity,
        "allow_same_advertiser_consecutive": c.allow_same_advertiser_consecutive,
        "request_art_service": c.request_art_service, "auto_renew": c.auto_renew,
        "product_id": str(c.product_id) if c.product_id else None, "brand_id": str(c.brand_id) if c.brand_id else None,
        "contracted_price_cents": c.contracted_price_cents, "paid_at": c.paid_at,
        "paid_payment_order_id": str(c.payment_order_id) if c.paid_at and c.payment_order_id else None,
        "approval_source": c.approval_source, "submitted_at": c.submitted_at, "review_due_at": c.review_due_at,
        "approved_at": c.approved_at, "rejected_at": c.rejected_at, "rejection_reason": c.rejection_reason,
        "has_live_version": c.live_revision_id is not None, "has_pending_change": c.pending_revision_id is not None
        and c.live_revision_id is not None,
        "playable": S.is_campaign_playable(c), "payment_issue": c.payment_issue,
        "created_at": c.created_at, "updated_at": c.updated_at,
    }
    if detail:
        out["targets"] = [t.as_dict() for t in TG.specs_from(await S.targets_of(db, c.id))]
        out["segment_ids"] = [str(s) for s in await S.segment_ids_of(db, c.id)]
        links = await S.links_of(db, c.id)
        crs = {x.id: x for x in (await db.execute(select(Creative).where(
            Creative.id.in_([ln.creative_id for ln in links] or [uuid.uuid4()])))).scalars()}
        out["creatives"] = [{
            "creative_id": str(ln.creative_id), "code": crs[ln.creative_id].code if ln.creative_id in crs else None,
            "name": crs[ln.creative_id].name if ln.creative_id in crs else None,
            "status": crs[ln.creative_id].status if ln.creative_id in crs else None,
            "technical_status": crs[ln.creative_id].technical_status if ln.creative_id in crs else None,
            "is_active": ln.is_active, "pending": ln.pending, "pending_removal": ln.pending_removal,
            "replaces_creative_id": str(ln.replaces_creative_id) if ln.replaces_creative_id else None,
            "order": ln.order} for ln in links]
        out["allowed_transitions"] = ST.allowed_targets(c.status)
        out["reproducing"] = await S.playing_state(db, c)
        reproducing = out["reproducing"].get("playing")
        out["paid_payment_order_ids"] = [str(x) for x in await S.paid_payment_order_ids(db, c)]
        for key, rid in (("live_revision", c.live_revision_id), ("pending_revision", c.pending_revision_id)):
            rev = await db.get(CampaignRevision, rid) if rid else None
            out[key] = revision_out(rev, staff=is_staff(ctx)) if rev else None
    out["is_reproducing"] = bool(reproducing) if reproducing is not None else None
    out["display_status"] = S.display_status(c.status, reproducing)
    return out


def revision_out(r: CampaignRevision, *, staff: bool) -> dict:
    out = {"id": str(r.id), "number": r.number, "kind": r.kind, "status": r.status, "spec": r.spec,
           "screens_count": len(r.screen_ids or []), "creative_ids": r.creative_ids,
           "price_cents": r.price_cents, "price_diff_cents": r.price_diff_cents, "change_summary": r.change_summary,
           "quote": (r.quote if staff else PE.public_view(r.quote)) if r.quote else None,
           "created_at": r.created_at, "decided_at": r.decided_at, "decision_reason": r.decision_reason}
    if staff:
        out["screen_ids"] = r.screen_ids
        out["hold_id"] = str(r.hold_id) if r.hold_id else None
    return out


# ─── campanhas ───────────────────────────────────────────────────────────────

@router.get("")
async def list_campaigns(
    page: int = Query(1, ge=1),
    page_size: int = Query(25, ge=1, le=100),
    status: Optional[str] = Query(None, max_length=30),
    org_id: Optional[uuid.UUID] = None,
    q: Optional[str] = Query(None, max_length=100),
    sort: str = Query("-created_at", pattern=r"^-?(created_at|updated_at|name|start_date|end_date)$"),
    ctx: AuthContext = Depends(require("campaigns.view")),
    db: AsyncSession = Depends(get_db),
):
    base = select(Campaign).where(ctx.scope_filter("campaigns.view", org_col=Campaign.org_id))
    if status:
        base = base.where(Campaign.status.in_([s.strip().upper() for s in status.split(",")]))
    if org_id:
        base = base.where(Campaign.org_id == org_id)
    if q:
        base = base.where(Campaign.name.ilike(f"%{q.strip()}%"))
    total = (await db.execute(select(func.count()).select_from(base.subquery()))).scalar_one()
    col = getattr(Campaign, sort.lstrip("-"))
    rows = (await db.execute(base.order_by(col.desc() if sort.startswith("-") else col.asc(), Campaign.id)
                             .offset((page - 1) * page_size).limit(page_size))).scalars().all()
    playing = await S.playing_states(db, rows)  # uma consulta para a página (sem N+1)
    return page_out([await campaign_out(db, ctx, c, reproducing=playing.get(c.id)) for c in rows], total, page,
                    page_size)


@router.post("", status_code=201)
async def create_campaign(body: SC.CampaignCreate, ctx: AuthContext = Depends(require("campaigns.manage")),
                          db: AsyncSession = Depends(get_db)):
    org_id = resolve_org(ctx, body.org_id, "campaigns.manage")
    data = body.model_dump(exclude={"org_id", "targets", "segment_ids"}, exclude_none=True)
    c = await S.create_campaign(db, ctx, org_id=org_id, data=data,
                                targets=[t.model_dump() for t in body.targets] if body.targets else None,
                                segment_ids=body.segment_ids)
    await db.flush()
    return await campaign_out(db, ctx, c, detail=True)


@router.get("/review-queue")
async def review_queue(
    page: int = Query(1, ge=1),
    page_size: int = Query(25, ge=1, le=100),
    overdue_only: bool = False,
    ctx: AuthContext = Depends(require("campaigns.approve", global_only=True)),
    db: AsyncSession = Depends(get_db),
):
    now = utcnow()
    base = select(Campaign).where(Campaign.status == "EM_ANALISE")
    if overdue_only:
        base = base.where(Campaign.review_due_at <= now)
    total = (await db.execute(select(func.count()).select_from(base.subquery()))).scalar_one()
    rows = (await db.execute(base.order_by(Campaign.review_due_at.asc().nullslast(), Campaign.submitted_at, Campaign.id)
                             .offset((page - 1) * page_size).limit(page_size))).scalars().all()
    items = []
    for c in rows:
        d = await campaign_out(db, ctx, c, detail=True)
        d["sla_overdue"] = bool(c.review_due_at and c.review_due_at <= now)
        d["is_change_request"] = c.live_revision_id is not None
        items.append(d)
    return page_out(items, total, page, page_size)


@router.post("/approvals/bulk")
async def bulk_review(body: SC.BulkReviewIn,
                      ctx: AuthContext = Depends(require("campaigns.approve", global_only=True,
                                                         allow_impersonation=False)),
                      db: AsyncSession = Depends(get_db)):
    if body.action == "reject" and len((body.reason or "").strip()) < S.REJECTION_MIN_LEN:
        raise DomainError("REJECTION_REASON_REQUIRED", "Informe o motivo da rejeição.", status=400)
    done, failed = [], []
    for cid in body.campaign_ids:
        pos = mark(db)
        try:
            async with db.begin_nested():
                if body.action == "approve":
                    out = await S.approve(db, ctx, cid, note=body.reason)
                    if out.get("error"):
                        failed.append({"campaign_id": str(cid), "code": out["error"]["code"],
                                       "message": out["error"]["message"]})
                        continue
                else:
                    await S.reject(db, ctx, cid, reason=body.reason or "")
            done.append(str(cid))
        except DomainError as exc:
            rollback_to(db, pos)
            failed.append({"campaign_id": str(cid), "code": exc.code, "message": exc.message})
    await audit(db, ctx, f"campaign.bulk_{body.action}", "campaign", None,
                after={"done": done, "failed": [f["campaign_id"] for f in failed]}, reason=body.reason)
    return {"action": body.action, "done": done, "failed": failed}


@router.get("/approval-policies")
async def list_policies(ctx: AuthContext = Depends(require("campaigns.approve", global_only=True)),
                        db: AsyncSession = Depends(get_db)):
    from app.core.settings_service import get_setting

    rows = (await db.execute(select(OrganizationApprovalPolicy).order_by(OrganizationApprovalPolicy.updated_at.desc())
                             )).scalars().all()
    return {"global_mode": await get_setting(db, "campaign.approval_mode"),
            "items": [{"org_id": str(r.org_id), "mode": r.mode, "reason": r.reason,
                       "updated_by": str(r.updated_by) if r.updated_by else None, "updated_at": r.updated_at}
                      for r in rows]}


@router.put("/approval-policies/{org_id}")
async def put_policy(org_id: uuid.UUID, body: SC.ApprovalPolicyIn,
                     ctx: AuthContext = Depends(require("campaigns.approve", global_only=True,
                                                        allow_impersonation=False)),
                     db: AsyncSession = Depends(get_db)):
    from app.models.organizations import Organization

    if await db.get(Organization, org_id) is None:
        raise NotFound()
    row = (await db.execute(select(OrganizationApprovalPolicy).where(OrganizationApprovalPolicy.org_id == org_id)
                            .with_for_update().execution_options(populate_existing=True))).scalar_one_or_none()
    before = {"mode": row.mode} if row else None
    if row is None:
        row = OrganizationApprovalPolicy(org_id=org_id, mode=body.mode, reason=body.reason, updated_by=ctx.user_id,
                                         updated_at=utcnow())
        db.add(row)
    else:
        row.mode, row.reason, row.updated_by, row.updated_at = body.mode, body.reason, ctx.user_id, utcnow()
    await audit(db, ctx, "campaign.approval_policy_set", "organization", org_id, before=before,
                after={"mode": body.mode}, reason=body.reason, org_id=org_id)
    return {"org_id": str(org_id), "mode": row.mode, "reason": row.reason}


@router.delete("/approval-policies/{org_id}", status_code=204)
async def delete_policy(org_id: uuid.UUID,
                        ctx: AuthContext = Depends(require("campaigns.approve", global_only=True,
                                                           allow_impersonation=False)),
                        db: AsyncSession = Depends(get_db)):
    row = await db.get(OrganizationApprovalPolicy, org_id)
    if row is None:
        raise NotFound()
    await audit(db, ctx, "campaign.approval_policy_removed", "organization", org_id, before={"mode": row.mode},
                org_id=org_id)
    await db.delete(row)
    return Response(status_code=204)


@router.post("/availability")
async def availability(body: SC.AvailabilityIn, ctx: AuthContext = Depends(get_auth_context),
                       db: AsyncSession = Depends(get_db)):
    if not (ctx.has_any("campaigns.manage") or ctx.has_any("inventory.view") or ctx.has_any("pricing.view")):
        raise Forbidden(details={"permission": "campaigns.manage"})
    perm = "campaigns.manage" if ctx.has_any("campaigns.manage") else (
        "inventory.view" if ctx.has_any("inventory.view") else "pricing.view")
    camp = None
    if body.campaign_id is not None:
        camp = await S.get_campaign(db, ctx, body.campaign_id, perm="campaigns.view")
        if body.org_id is not None and body.org_id != camp.org_id:
            raise DomainError("CAMPAIGN_ORG_MISMATCH", "A campanha não pertence à organização informada.", status=400)
        org_id = camp.org_id
    else:
        org_id = resolve_org(ctx, body.org_id, perm) if not ctx.can(perm, global_only=True) or body.org_id else None
    if org_id is None:
        raise DomainError("ORG_REQUIRED", "Informe a organização anunciante (org_id).", status=400)
    # equipe RD: + ``exclusions`` (contagens por motivo, sem nomes); anunciante: resposta inalterada
    return await S.estimate_availability(db, ctx, org_id=org_id, targets=[t.model_dump() for t in body.targets],
                                         duration_s=body.duration_s, campaign=camp, diagnose=sees_exclusions(ctx))


@router.get("/by-code/{code}")
async def creative_by_code_lookup(code: str, ctx: AuthContext = Depends(require("creatives.view")),
                                  db: AsyncSession = Depends(get_db)):
    c = (await db.execute(select(Creative).where(Creative.code == code.strip().upper()))).scalar_one_or_none()
    if c is None:
        raise NotFound()
    ctx.ensure("creatives.view", org_id=c.org_id)
    camps = (await db.execute(select(Campaign).join(CampaignCreative, CampaignCreative.campaign_id == Campaign.id)
                              .where(CampaignCreative.creative_id == c.id,
                                     ctx.scope_filter("campaigns.view", org_col=Campaign.org_id)))).scalars().all()
    return {"creative": CR.creative_out(c, await CR.segment_ids_of(db, c.id)),
            "campaigns": [{"id": str(x.id), "name": x.name, "status": x.status} for x in camps]}


@router.get("/{campaign_id}")
async def get_campaign(campaign_id: uuid.UUID, ctx: AuthContext = Depends(require("campaigns.view")),
                       db: AsyncSession = Depends(get_db)):
    c = await S.get_campaign(db, ctx, campaign_id)
    return await campaign_out(db, ctx, c, detail=True)


@router.patch("/{campaign_id}")
async def patch_campaign(campaign_id: uuid.UUID, body: SC.CampaignPatch,
                         ctx: AuthContext = Depends(require("campaigns.manage")), db: AsyncSession = Depends(get_db)):
    data = body.model_dump(exclude={"expected_version"}, exclude_unset=True)
    c = await S.update_campaign(db, ctx, campaign_id, data, body.expected_version)
    return await campaign_out(db, ctx, c, detail=True)


@router.put("/{campaign_id}/targets")
async def put_targets(campaign_id: uuid.UUID, body: SC.TargetsPut,
                      ctx: AuthContext = Depends(require("campaigns.manage")), db: AsyncSession = Depends(get_db)):
    rows = await S.set_targets(db, ctx, campaign_id, [t.model_dump() for t in body.targets])
    return {"targets": [t.as_dict() for t in TG.specs_from(rows)]}


@router.put("/{campaign_id}/segments")
async def put_segments(campaign_id: uuid.UUID, body: SC.SegmentsPut,
                       ctx: AuthContext = Depends(require("campaigns.manage")), db: AsyncSession = Depends(get_db)):
    ids = await S.update_segments(db, ctx, campaign_id, body.segment_ids)
    return {"segment_ids": [str(x) for x in ids]}


@router.post("/{campaign_id}/creatives", status_code=201)
async def link_creative(campaign_id: uuid.UUID, body: SC.CreativeLink,
                        ctx: AuthContext = Depends(require("campaigns.manage")), db: AsyncSession = Depends(get_db)):
    ln = await S.add_creative(db, ctx, campaign_id, body.creative_id, body.order)
    return {"campaign_id": str(campaign_id), "creative_id": str(ln.creative_id), "is_active": ln.is_active}


@router.delete("/{campaign_id}/creatives/{creative_id}", status_code=204)
async def unlink_creative(campaign_id: uuid.UUID, creative_id: uuid.UUID,
                          ctx: AuthContext = Depends(require("campaigns.manage")), db: AsyncSession = Depends(get_db)):
    await S.remove_creative(db, ctx, campaign_id, creative_id)
    return Response(status_code=204)


@router.post("/{campaign_id}/quote")
async def quote_campaign(campaign_id: uuid.UUID, body: SC.SingleCheckoutIn,
                         ctx: AuthContext = Depends(require("campaigns.view")), db: AsyncSession = Depends(get_db)):
    c = await S.get_campaign(db, ctx, campaign_id)
    if c.start_date is None or c.end_date is None:
        raise DomainError("CAMPAIGN_INCOMPLETE", "Defina as datas de início e fim.", status=400)
    res = await S.resolve_campaign(db, c)
    if not res.screens:
        raise Conflict("NO_SCREENS_AVAILABLE", "Nenhuma tela disponível para a segmentação desta campanha.")
    q = await CK._quote_for(db, ctx, c, res.screens, coupon_code=body.coupon_code)
    staff = ctx.can("pricing.view", global_only=True)
    return {"quote": q if staff else PE.public_view(q), "availability": TG.availability_summary(res)}


@router.post("/{campaign_id}/checkout", status_code=201)
async def checkout_single(campaign_id: uuid.UUID, body: SC.SingleCheckoutIn, response: Response,
                          idempotency_key: Optional[str] = Header(default=None, alias="Idempotency-Key"),
                          ctx: AuthContext = Depends(require("checkout.create")), db: AsyncSession = Depends(get_db)):
    res = await CK.checkout(db, ctx, campaign_ids=[campaign_id], idempotency_key=idempotency_key,
                            coupon_code=body.coupon_code, method=body.method,
                            payment_source_token=body.payment_source_token, installments=body.installments)
    if res.failed:
        # resposta de erro SEM exceção: a transação confirma o cancelamento e o registro da tentativa
        from fastapi.responses import JSONResponse

        return JSONResponse(status_code=502, content={"error": res.response["error"]})
    if res.replayed:
        response.status_code = 200
    return res.response


@router.post("/{campaign_id}/renew", status_code=201)
async def renew(campaign_id: uuid.UUID, body: SC.RenewIn, response: Response,
                ctx: AuthContext = Depends(require("checkout.create")), db: AsyncSession = Depends(get_db)):
    """Proposta de renovação (cotação do período adicional). NÃO reserva inventário nem gera cobrança."""
    from app.modules.campaigns import renewals as RN

    out = await RN.renew(db, ctx, campaign_id, periods=body.periods)
    if out.pop("replayed", False):
        response.status_code = 200
    return out


@router.get("/{campaign_id}/renewal")
async def get_renewal(campaign_id: uuid.UUID, ctx: AuthContext = Depends(require("campaigns.view")),
                      db: AsyncSession = Depends(get_db)):
    from app.modules.campaigns import renewals as RN

    out = await RN.get_renewal(db, ctx, campaign_id)
    if out is None:
        raise NotFound("RENEWAL_NOT_FOUND", "Não há renovação aberta para esta campanha.")
    return out


@router.post("/{campaign_id}/renew/checkout", status_code=201)
async def renew_checkout(campaign_id: uuid.UUID, body: SC.SingleCheckoutIn, response: Response,
                         idempotency_key: Optional[str] = Header(default=None, alias="Idempotency-Key"),
                         ctx: AuthContext = Depends(require("checkout.create")), db: AsyncSession = Depends(get_db)):
    """Checkout da renovação: reserva de 2 h (W-INV) + pedido + cobrança (W-FIN)."""
    from app.modules.campaigns import renewals as RN

    if body.coupon_code:
        raise DomainError("COUPON_NOT_APPLICABLE", "Cupom não se aplica à renovação.", status=400)
    res = await RN.renewal_checkout(db, ctx, campaign_id, idempotency_key=idempotency_key, method=body.method,
                                    payment_source_token=body.payment_source_token, installments=body.installments)
    if res.failed:
        from fastapi.responses import JSONResponse

        return JSONResponse(status_code=502, content={"error": res.response["error"]})
    if res.replayed:
        response.status_code = 200
    return res.response


@router.put("/{campaign_id}/auto-renew")
async def put_auto_renew(campaign_id: uuid.UUID, body: SC.AutoRenewIn,
                         ctx: AuthContext = Depends(require("campaigns.manage")), db: AsyncSession = Depends(get_db)):
    from app.modules.campaigns import renewals as RN

    c = await RN.set_auto_renew(db, ctx, campaign_id, body.auto_renew)
    return await campaign_out(db, ctx, c)


@router.post("/{campaign_id}/duplicate", status_code=201)
async def duplicate(campaign_id: uuid.UUID, ctx: AuthContext = Depends(require("campaigns.manage")),
                    db: AsyncSession = Depends(get_db)):
    c = await S.duplicate_campaign(db, ctx, campaign_id)
    return await campaign_out(db, ctx, c, detail=True)


@router.post("/{campaign_id}/submit-media")
async def submit_media(campaign_id: uuid.UUID, ctx: AuthContext = Depends(require("campaigns.manage")),
                       db: AsyncSession = Depends(get_db)):
    c = await S.submit_media(db, ctx, campaign_id)
    return await campaign_out(db, ctx, c, detail=True)


@router.post("/{campaign_id}/resubmit")
async def resubmit(campaign_id: uuid.UUID, ctx: AuthContext = Depends(require("campaigns.manage")),
                   db: AsyncSession = Depends(get_db)):
    c = await S.resubmit(db, ctx, campaign_id)
    return await campaign_out(db, ctx, c, detail=True)


@router.post("/{campaign_id}/approve")
async def approve(campaign_id: uuid.UUID, body: SC.ApproveIn,
                  ctx: AuthContext = Depends(require("campaigns.approve", global_only=True, allow_impersonation=False)),
                  db: AsyncSession = Depends(get_db)):
    out = await S.approve(db, ctx, campaign_id, note=body.note)
    if out.get("error"):
        from fastapi.responses import JSONResponse

        return JSONResponse(status_code=409, content={"error": out["error"]})
    d = await campaign_out(db, ctx, out["campaign"], detail=True)
    d["conflicts_blocked_establishments"] = out["conflicts_blocked_establishments"]
    return d


@router.post("/{campaign_id}/reject")
async def reject(campaign_id: uuid.UUID, body: SC.RejectIn,
                 ctx: AuthContext = Depends(require("campaigns.approve", global_only=True, allow_impersonation=False)),
                 db: AsyncSession = Depends(get_db)):
    c = await S.reject(db, ctx, campaign_id, reason=body.reason, creative_ids=body.creative_ids)
    return await campaign_out(db, ctx, c, detail=True)


@router.post("/{campaign_id}/pause")
async def pause(campaign_id: uuid.UUID, body: SC.OptionalReason,
                ctx: AuthContext = Depends(require("campaigns.manage")), db: AsyncSession = Depends(get_db)):
    c = await S.pause(db, ctx, campaign_id, reason=body.reason)
    return await campaign_out(db, ctx, c, detail=True)


@router.post("/{campaign_id}/resume")
async def resume(campaign_id: uuid.UUID, ctx: AuthContext = Depends(require("campaigns.manage")),
                 db: AsyncSession = Depends(get_db)):
    c = await S.resume(db, ctx, campaign_id)
    return await campaign_out(db, ctx, c, detail=True)


@router.post("/{campaign_id}/kill")
async def kill(campaign_id: uuid.UUID, body: SC.Reason,
               ctx: AuthContext = Depends(require("campaigns.kill", global_only=True, allow_impersonation=False)),
               db: AsyncSession = Depends(get_db)):
    c = await S.pause(db, ctx, campaign_id, reason=body.reason, kill=True)
    return await campaign_out(db, ctx, c, detail=True)


@router.post("/{campaign_id}/cancel")
async def cancel(campaign_id: uuid.UUID, body: SC.Reason, ctx: AuthContext = Depends(require("campaigns.manage")),
                 db: AsyncSession = Depends(get_db)):
    c = await S.cancel(db, ctx, campaign_id, reason=body.reason)
    return await campaign_out(db, ctx, c, detail=True)


@router.post("/{campaign_id}/changes", status_code=201)
async def request_change(campaign_id: uuid.UUID, body: SC.ChangeIn,
                         idempotency_key: Optional[str] = Header(default=None, alias="Idempotency-Key"),
                         ctx: AuthContext = Depends(require("campaigns.manage")), db: AsyncSession = Depends(get_db)):
    data = body.model_dump(exclude_unset=True)
    if body.creative_replacements is not None:
        data["creative_replacements"] = [r.model_dump() for r in body.creative_replacements]
    if body.targets is not None:
        data["targets"] = [t.model_dump() for t in body.targets]
    return await CH.request_change(db, ctx, campaign_id, changes=data, idempotency_key=idempotency_key)


@router.get("/{campaign_id}/revisions")
async def list_revisions(campaign_id: uuid.UUID, ctx: AuthContext = Depends(require("campaigns.view")),
                         db: AsyncSession = Depends(get_db)):
    c = await S.get_campaign(db, ctx, campaign_id)
    rows = (await db.execute(select(CampaignRevision).where(CampaignRevision.campaign_id == c.id)
                             .order_by(CampaignRevision.number))).scalars().all()
    return {"items": [revision_out(r, staff=is_staff(ctx)) for r in rows]}


@router.get("/{campaign_id}/history")
async def history(campaign_id: uuid.UUID, ctx: AuthContext = Depends(require("campaigns.view")),
                  db: AsyncSession = Depends(get_db)):
    c = await S.get_campaign(db, ctx, campaign_id)
    rows = (await db.execute(select(CampaignStatusHistory).where(CampaignStatusHistory.campaign_id == c.id)
                             .order_by(CampaignStatusHistory.created_at, CampaignStatusHistory.id))).scalars().all()
    staff = is_staff(ctx)
    return {"items": [{"from": h.from_status, "to": h.to_status, "trigger": h.trigger, "reason": h.reason,
                       "origin": h.origin, "at": h.created_at,
                       **({"actor_user_id": str(h.actor_user_id) if h.actor_user_id else None} if staff else {})}
                      for h in rows]}


@router.get("/{campaign_id}/conflicts")
async def campaign_conflicts(campaign_id: uuid.UUID, ctx: AuthContext = Depends(require("campaigns.view")),
                             db: AsyncSession = Depends(get_db)):
    """RD: decisões por estabelecimento com explicação; anunciante: apenas contagens (sem nomes)."""
    c = await S.get_campaign(db, ctx, campaign_id)
    res = await S.resolve_campaign(db, c, check_conflicts=True)
    staff = is_staff(ctx) or ctx.can("conflicts.override", global_only=True)
    out = {"allowed_screens": len(res.screens), "blocked_establishments": len(res.excluded_conflict_establishments)}
    if staff:
        out["blocked"] = [{"establishment_id": str(e), **d.as_dict()}
                          for e, d in sorted(res.excluded_conflict_establishments.items(), key=lambda kv: str(kv[0]))]
    return out


@router.get("/{campaign_id}/screens")
async def campaign_screens(campaign_id: uuid.UUID, ctx: AuthContext = Depends(require("campaigns.view")),
                           db: AsyncSession = Depends(get_db)):
    """Telas da campanha. Contratada: telas congeladas da versão vigente; antes da compra: previsão.

    Cliente: visão PÚBLICA ``{friendly_id, state, orientation, letterbox}`` (sem estabelecimento,
    nome ou endereço). Equipe RD: inclui ids técnicos, estabelecimento e cidade."""
    from app.models.establishments import Establishment as E
    from app.models.screens import Screen

    c = await S.get_campaign(db, ctx, campaign_id)
    staff = is_staff(ctx)
    live = await db.get(CampaignRevision, c.live_revision_id) if c.live_revision_id else None
    if live is not None:
        source = "CONTRATADAS"
        ids = [uuid.UUID(x) for x in (live.screen_ids or [])]
        rows = (await db.execute(select(Screen.id, Screen.friendly_id, Screen.orientation, Screen.screen_class,
                                        Screen.establishment_id, E.state, E.city_key)
                                 .join(E, E.id == Screen.establishment_id).where(Screen.id.in_(ids))
                                 .order_by(Screen.friendly_id))).all() if ids else []
        orients = {cr.orientation for cr in await S.creatives_of(db, c.id, active_only=True)}
        items = [{"screen_id": r.id, "friendly_id": r.friendly_id, "orientation": r.orientation,
                  "screen_class": r.screen_class, "establishment_id": r.establishment_id, "state": r.state,
                  "city_key": r.city_key, "letterbox": any(o != r.orientation for o in orients)} for r in rows]
        summary = {"screens_available": len(items)}
    else:
        source = "PREVISAO"
        res = await S.resolve_campaign(db, c)
        fids = dict((await db.execute(select(Screen.id, Screen.friendly_id).where(
            Screen.id.in_(res.screen_ids)))).all()) if res.screens else {}
        items = sorted([{"screen_id": s.screen_id, "friendly_id": fids.get(s.screen_id), "orientation": s.orientation,
                         "screen_class": s.screen_class, "establishment_id": s.establishment_id, "state": s.state,
                         "city_key": s.city_key, "letterbox": s.letterbox} for s in res.screens],
                       key=lambda i: i["friendly_id"] or "")
        summary = TG.availability_summary(res)
    public_keys = ("friendly_id", "state", "orientation", "letterbox")
    if staff:
        out_items = [{k: (str(v) if isinstance(v, uuid.UUID) else v) for k, v in i.items()} for i in items]
    else:
        out_items = [{k: i[k] for k in public_keys} for i in items]
    return {"source": source, "items": out_items, "total": len(out_items), "summary": summary}


@router.get("/{campaign_id}/cancel-quote")
async def cancel_quote(campaign_id: uuid.UUID, ctx: AuthContext = Depends(require("campaigns.manage")),
                       db: AsyncSession = Depends(get_db)):
    """Prévia do reembolso para o diálogo de cancelamento (cálculo do financeiro — ADR_009 #8)."""
    return await S.cancel_quote(db, ctx, campaign_id)


# ─── criativos ───────────────────────────────────────────────────────────────

@creatives_router.get("")
async def list_creatives(
    page: int = Query(1, ge=1),
    page_size: int = Query(25, ge=1, le=100),
    org_id: Optional[uuid.UUID] = None,
    status: Optional[str] = Query(None, max_length=60),
    kind: Optional[str] = Query(None, max_length=20),
    q: Optional[str] = Query(None, max_length=100),
    ctx: AuthContext = Depends(require("creatives.view")),
    db: AsyncSession = Depends(get_db),
):
    base = select(Creative).where(ctx.scope_filter("creatives.view", org_col=Creative.org_id))
    if org_id:
        base = base.where(Creative.org_id == org_id)
    if status:
        base = base.where(Creative.status.in_([s.strip().upper() for s in status.split(",")]))
    if kind:
        base = base.where(Creative.kind == kind.upper())
    if q:
        like = f"%{q.strip()}%"
        base = base.where(or_(Creative.name.ilike(like), Creative.code.ilike(like.upper())))
    total = (await db.execute(select(func.count()).select_from(base.subquery()))).scalar_one()
    rows = (await db.execute(base.order_by(Creative.created_at.desc(), Creative.id)
                             .offset((page - 1) * page_size).limit(page_size))).scalars().all()
    return page_out([CR.creative_out(c, await CR.segment_ids_of(db, c.id)) for c in rows], total, page, page_size)


@creatives_router.post("", status_code=201)
async def create_creative(body: SC.CreativeCreate, ctx: AuthContext = Depends(require("creatives.manage")),
                          db: AsyncSession = Depends(get_db)):
    org_id = resolve_org(ctx, body.org_id, "creatives.manage")
    c = await CR.create_creative(db, ctx, org_id=org_id, name=body.name, media_file_id=body.media_file_id,
                                 duration_seconds=body.duration_seconds, kind=body.kind, ad_text=body.ad_text,
                                 product_id=body.product_id, brand_id=body.brand_id, segment_ids=body.segment_ids)
    return CR.creative_out(await S.fresh(db, c), await CR.segment_ids_of(db, c.id))


@creatives_router.get("/{creative_id}")
async def get_creative(creative_id: uuid.UUID, ctx: AuthContext = Depends(require("creatives.view")),
                       db: AsyncSession = Depends(get_db)):
    c = await CR.get_creative(db, ctx, creative_id)
    versions = (await db.execute(select(Creative).where(
        or_(Creative.root_id == (c.root_id or c.id), Creative.id == (c.root_id or c.id)))
        .order_by(Creative.version))).scalars().all()
    out = CR.creative_out(await S.fresh(db, c), await CR.segment_ids_of(db, c.id))
    out["versions"] = [{"id": str(v.id), "code": v.code, "version": v.version, "status": v.status} for v in versions]
    return out


@creatives_router.patch("/{creative_id}")
async def patch_creative(creative_id: uuid.UUID, body: SC.CreativePatch,
                         ctx: AuthContext = Depends(require("creatives.manage")), db: AsyncSession = Depends(get_db)):
    c = await CR.get_creative(db, ctx, creative_id, perm="creatives.manage", for_update=True)
    data = body.model_dump(exclude_unset=True)
    relevant = {k for k in data if k != "name"}
    if relevant and c.status != CreativeStatus.RASCUNHO.value:
        raise Conflict("CREATIVE_IMMUTABLE",
                       "Texto/classificação de criativo já enviado não mudam: crie uma nova versão (novo código).")
    if relevant:
        await CR.validate_classification(db, data.get("product_id", c.product_id), data.get("brand_id", c.brand_id))
    before = {k: getattr(c, k) for k in data}
    for k, v in data.items():
        setattr(c, k, v)
    await audit(db, ctx, "creative.updated", "creative", c.id, before=before, after=data, org_id=c.org_id)
    return CR.creative_out(await S.fresh(db, c), await CR.segment_ids_of(db, c.id))


@creatives_router.put("/{creative_id}/segments")
async def put_creative_segments(creative_id: uuid.UUID, body: SC.SegmentsPut,
                                ctx: AuthContext = Depends(require("creatives.manage")),
                                db: AsyncSession = Depends(get_db)):
    c = await CR.get_creative(db, ctx, creative_id, perm="creatives.manage", for_update=True)
    if c.status != CreativeStatus.RASCUNHO.value:
        raise Conflict("CREATIVE_IMMUTABLE", "Classificação de criativo já enviado exige nova versão.")
    before = [str(s) for s in await CR.segment_ids_of(db, c.id)]
    ids = await CR.set_segments(db, c.id, body.segment_ids)
    await audit(db, ctx, "creative.segments_updated", "creative", c.id, before={"segment_ids": before},
                after={"segment_ids": [str(s) for s in ids]}, org_id=c.org_id)
    return {"segment_ids": [str(s) for s in ids]}


@creatives_router.post("/{creative_id}/versions", status_code=201)
async def new_version(creative_id: uuid.UUID, body: SC.CreativeVersionIn,
                      ctx: AuthContext = Depends(require("creatives.manage")), db: AsyncSession = Depends(get_db)):
    old = await CR.get_creative(db, ctx, creative_id, perm="creatives.manage", for_update=True)
    data = body.model_dump(exclude_unset=True)
    kw = {k: data[k] for k in ("ad_text", "product_id", "brand_id") if k in data}
    c = await CR.create_version(db, ctx, old, name=data.get("name"), media_file_id=data.get("media_file_id"),
                                duration_seconds=data.get("duration_seconds"), segment_ids=data.get("segment_ids"),
                                **kw)
    return CR.creative_out(await S.fresh(db, c), await CR.segment_ids_of(db, c.id))


@creatives_router.post("/{creative_id}/review")
async def review_creative(creative_id: uuid.UUID, body: SC.CreativeReviewIn,
                          ctx: AuthContext = Depends(require("campaigns.approve", global_only=True,
                                                             allow_impersonation=False)),
                          db: AsyncSession = Depends(get_db)):
    """Revisão de conteúdo RD de criativos INTERNAL/INSTITUTIONAL/FALLBACK (EXTERNAL: via campanha)."""
    c = await CR.review_creative(db, ctx, creative_id, approve=body.decision == "APPROVE", reason=body.reason)
    return CR.creative_out(await S.fresh(db, c), await CR.segment_ids_of(db, c.id))


@creatives_router.post("/{creative_id}/revalidate")
async def revalidate(creative_id: uuid.UUID, ctx: AuthContext = Depends(require("creatives.manage")),
                     db: AsyncSession = Depends(get_db)):
    c = await CR.get_creative(db, ctx, creative_id, perm="creatives.manage", for_update=True)
    await CR.refresh_technical(db, [c])
    return CR.creative_out(await S.fresh(db, c), await CR.segment_ids_of(db, c.id))


@creatives_router.post("/{creative_id}/archive")
async def archive(creative_id: uuid.UUID, ctx: AuthContext = Depends(require("creatives.manage")),
                  db: AsyncSession = Depends(get_db)):
    c = await CR.get_creative(db, ctx, creative_id, perm="creatives.manage", for_update=True)
    in_use = (await db.execute(select(CampaignCreative.id).join(Campaign, Campaign.id == CampaignCreative.campaign_id)
                               .where(CampaignCreative.creative_id == c.id, CampaignCreative.removed_at.is_(None),
                                      Campaign.status.notin_(["FINALIZADA", "CANCELADA"])))).first()
    if in_use is not None:
        raise Conflict("CREATIVE_IN_USE", "Criativo em uso por campanha vigente.")
    before = c.status
    c.status = CreativeStatus.ARQUIVADO.value
    c.archived_at = utcnow()
    await audit(db, ctx, "creative.archived", "creative", c.id, before={"status": before},
                after={"status": c.status}, org_id=c.org_id)
    return CR.creative_out(await S.fresh(db, c), await CR.segment_ids_of(db, c.id))
