"""
RD Mídia Indoor — Matriz de conflitos: regras, exceções, overrides e decisões (W-CAMP).

- Regras e exceções do estabelecimento: ``taxonomy.manage`` (GLOBAL — só a RD; o
  estabelecimento solicita por ticket, ETAPA2 #98/#114).
- Override campanha × estabelecimento: ``conflicts.override`` (GLOBAL), justificativa
  ≥ 20 caracteres, validade, auditoria imutável; NÃO sobrescreve a política de conteúdo
  proibido (apostas/adulto/armas).
- Avaliação/decisões explicáveis: equipe RD (taxonomy.manage | conflicts.override | campaigns.approve).
"""
from __future__ import annotations

import uuid
from datetime import datetime
from typing import Literal, Optional

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.audit import audit
from app.core.authz import AuthContext, get_auth_context, require
from app.core.database import get_db
from app.core.errors import Conflict, DomainError, Forbidden, NotFound
from app.core.timeutils import utcnow
from app.models.conflicts import ConflictDecision, ConflictException, ConflictOverride, ConflictRule
from app.modules.conflicts import service as CS

router = APIRouter()

OVERRIDE_MIN_LEN = 20


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


def _staff(ctx: AuthContext) -> None:
    if not any(ctx.can(p, global_only=True) for p in ("taxonomy.manage", "conflicts.override", "campaigns.approve")):
        raise Forbidden(details={"permission": "taxonomy.manage"})


# ─── regras ──────────────────────────────────────────────────────────────────

class RuleIn(_Strict):
    subject_type: Literal["EST_CATEGORY", "EST_SEGMENT", "EST_ORG", "ESTABLISHMENT"]
    subject_id: uuid.UUID
    object_type: Literal["AD_CATEGORY", "AD_SEGMENT", "AD_PRODUCT", "AD_BRAND", "AD_ADVERTISER"]
    object_id: uuid.UUID
    include_descendants: bool = True
    effect: Literal["BLOCK", "ALLOW"]
    reason: str = Field(min_length=5, max_length=2000)
    valid_from: Optional[datetime] = None
    valid_until: Optional[datetime] = None


class RulePatch(_Strict):
    effect: Optional[Literal["BLOCK", "ALLOW"]] = None
    include_descendants: Optional[bool] = None
    reason: str = Field(min_length=5, max_length=2000)
    valid_until: Optional[datetime] = None
    active: Optional[bool] = None


def rule_out(r: ConflictRule) -> dict:
    return {"id": str(r.id), "subject_type": r.subject_type, "subject_id": str(r.subject_id),
            "object_type": r.object_type, "object_id": str(r.object_id), "include_descendants": r.include_descendants,
            "effect": r.effect, "reason": r.reason, "active": r.active, "valid_from": r.valid_from,
            "valid_until": r.valid_until, "version": r.version, "created_at": r.created_at, "updated_at": r.updated_at}


async def _check_ref(db: AsyncSession, kind: str, ref: uuid.UUID) -> None:
    from app.models.establishments import Establishment
    from app.models.organizations import Organization
    from app.models.taxonomy import Brand, Product, Segment, TaxonomyNode

    model = {"EST_CATEGORY": TaxonomyNode, "AD_CATEGORY": TaxonomyNode, "EST_SEGMENT": Segment,
             "AD_SEGMENT": Segment, "EST_ORG": Organization, "AD_ADVERTISER": Organization,
             "ESTABLISHMENT": Establishment, "AD_PRODUCT": Product, "AD_BRAND": Brand}[kind]
    if await db.get(model, ref) is None:
        raise DomainError("CONFLICT_RULE_REF_NOT_FOUND", "Referência da regra inexistente.", status=400,
                          details={"type": kind, "id": str(ref)})


@router.get("/rules")
async def list_rules(page: int = Query(1, ge=1), page_size: int = Query(50, ge=1, le=100),
                     active: Optional[bool] = None,
                     ctx: AuthContext = Depends(require("taxonomy.manage", global_only=True)),
                     db: AsyncSession = Depends(get_db)):
    base = select(ConflictRule)
    if active is not None:
        base = base.where(ConflictRule.active.is_(active))
    total = (await db.execute(select(func.count()).select_from(base.subquery()))).scalar_one()
    rows = (await db.execute(base.order_by(ConflictRule.created_at.desc(), ConflictRule.id)
                             .offset((page - 1) * page_size).limit(page_size))).scalars().all()
    return {"items": [rule_out(r) for r in rows], "total": total, "page": page, "page_size": page_size}


@router.post("/rules", status_code=201)
async def create_rule(body: RuleIn, ctx: AuthContext = Depends(require("taxonomy.manage", global_only=True)),
                      db: AsyncSession = Depends(get_db)):
    await _check_ref(db, body.subject_type, body.subject_id)
    await _check_ref(db, body.object_type, body.object_id)
    if body.valid_from and body.valid_until and body.valid_until <= body.valid_from:
        raise DomainError("VALIDITY_INVALID", "Validade inválida.", status=400)
    r = ConflictRule(id=uuid.uuid4(), created_by=ctx.user_id, active=True, version=1, **body.model_dump())
    db.add(r)
    await audit(db, ctx, "conflicts.rule_created", "conflict_rule", r.id, after=rule_out(r), reason=body.reason)
    await db.flush()
    return rule_out(r)


@router.patch("/rules/{rule_id}")
async def patch_rule(rule_id: uuid.UUID, body: RulePatch,
                     ctx: AuthContext = Depends(require("taxonomy.manage", global_only=True)),
                     db: AsyncSession = Depends(get_db)):
    r = (await db.execute(select(ConflictRule).where(ConflictRule.id == rule_id).with_for_update().execution_options(populate_existing=True))).scalar_one_or_none()
    if r is None:
        raise NotFound()
    # ``reason`` do PATCH é o MOTIVO DA ALTERAÇÃO (só auditoria); a justificativa da regra (``r.reason``) é mantida
    data = body.model_dump(exclude_unset=True, exclude={"reason"})
    before = rule_out(r)
    for k, v in data.items():
        setattr(r, k, v)
    if r.valid_from and r.valid_until and r.valid_until <= r.valid_from:
        raise DomainError("VALIDITY_INVALID", "Validade inválida.", status=400)
    r.version += 1
    r.updated_by, r.updated_at = ctx.user_id, utcnow()
    await audit(db, ctx, "conflicts.rule_updated", "conflict_rule", r.id, before=before, after=rule_out(r),
                reason=body.reason)
    return rule_out(r)


# ─── exceções do estabelecimento ─────────────────────────────────────────────

class ExceptionIn(_Strict):
    establishment_id: uuid.UUID
    object_type: Literal["ADVERTISER", "BRAND", "CAMPAIGN"]
    object_id: uuid.UUID
    reason: str = Field(min_length=10, max_length=2000)
    valid_from: Optional[datetime] = None
    valid_until: Optional[datetime] = None


def exc_out(x: ConflictException) -> dict:
    return {"id": str(x.id), "establishment_id": str(x.establishment_id), "object_type": x.object_type,
            "object_id": str(x.object_id), "reason": x.reason, "approved_by": str(x.approved_by),
            "valid_from": x.valid_from, "valid_until": x.valid_until, "revoked_at": x.revoked_at}


@router.get("/exceptions")
async def list_exceptions(establishment_id: Optional[uuid.UUID] = None,
                          ctx: AuthContext = Depends(require("taxonomy.manage", global_only=True)),
                          db: AsyncSession = Depends(get_db)):
    q = select(ConflictException)
    if establishment_id:
        q = q.where(ConflictException.establishment_id == establishment_id)
    rows = (await db.execute(q.order_by(ConflictException.created_at.desc()))).scalars().all()
    return {"items": [exc_out(x) for x in rows]}


@router.post("/exceptions", status_code=201)
async def create_exception(body: ExceptionIn,
                           ctx: AuthContext = Depends(require("taxonomy.manage", global_only=True,
                                                              allow_impersonation=False)),
                           db: AsyncSession = Depends(get_db)):
    from app.models.campaigns import Campaign
    from app.models.establishments import Establishment
    from app.models.organizations import Organization
    from app.models.taxonomy import Brand

    if await db.get(Establishment, body.establishment_id) is None:
        raise NotFound()
    model = {"ADVERTISER": Organization, "BRAND": Brand, "CAMPAIGN": Campaign}[body.object_type]
    if await db.get(model, body.object_id) is None:
        raise DomainError("CONFLICT_EXCEPTION_REF_NOT_FOUND", "Objeto da exceção inexistente.", status=400)
    now = utcnow()
    x = ConflictException(id=uuid.uuid4(), establishment_id=body.establishment_id, object_type=body.object_type,
                          object_id=body.object_id, reason=body.reason, approved_by=ctx.user_id,
                          valid_from=body.valid_from or now, valid_until=body.valid_until)
    if x.valid_until is not None and x.valid_until <= x.valid_from:
        raise DomainError("VALIDITY_INVALID", "Validade inválida.", status=400)
    db.add(x)
    await audit(db, ctx, "conflicts.exception_created", "conflict_exception", x.id, after=exc_out(x),
                reason=body.reason)
    return exc_out(x)


class RevokeIn(_Strict):
    reason: str = Field(min_length=5, max_length=2000)


@router.post("/exceptions/{exception_id}/revoke")
async def revoke_exception(exception_id: uuid.UUID, body: RevokeIn,
                           ctx: AuthContext = Depends(require("taxonomy.manage", global_only=True)),
                           db: AsyncSession = Depends(get_db)):
    x = (await db.execute(select(ConflictException).where(ConflictException.id == exception_id).with_for_update().execution_options(populate_existing=True))
         ).scalar_one_or_none()
    if x is None:
        raise NotFound()
    if x.revoked_at is not None:
        raise Conflict("ALREADY_REVOKED", "Exceção já revogada.")
    x.revoked_at, x.revoked_by = utcnow(), ctx.user_id
    await audit(db, ctx, "conflicts.exception_revoked", "conflict_exception", x.id, reason=body.reason)
    return exc_out(x)


# ─── overrides admin ─────────────────────────────────────────────────────────

class OverrideIn(_Strict):
    campaign_id: uuid.UUID
    establishment_id: uuid.UUID
    justification: str = Field(min_length=1, max_length=4000)
    expires_at: Optional[datetime] = None


def ov_out(o: ConflictOverride) -> dict:
    return {"id": str(o.id), "campaign_id": str(o.campaign_id), "establishment_id": str(o.establishment_id),
            "justification": o.justification, "approved_by": str(o.approved_by), "approved_at": o.approved_at,
            "expires_at": o.expires_at, "revoked_at": o.revoked_at, "revoke_reason": o.revoke_reason}


@router.get("/overrides")
async def list_overrides(campaign_id: Optional[uuid.UUID] = None,
                         ctx: AuthContext = Depends(require("conflicts.override", global_only=True)),
                         db: AsyncSession = Depends(get_db)):
    q = select(ConflictOverride)
    if campaign_id:
        q = q.where(ConflictOverride.campaign_id == campaign_id)
    rows = (await db.execute(q.order_by(ConflictOverride.approved_at.desc()))).scalars().all()
    return {"items": [ov_out(o) for o in rows]}


@router.post("/overrides", status_code=201)
async def create_override(body: OverrideIn,
                          ctx: AuthContext = Depends(require("conflicts.override", global_only=True,
                                                             allow_impersonation=False)),
                          db: AsyncSession = Depends(get_db)):
    from app.models.campaigns import Campaign
    from app.models.establishments import Establishment

    just = body.justification.strip()
    if len(just) < OVERRIDE_MIN_LEN:
        raise DomainError("OVERRIDE_JUSTIFICATION_REQUIRED",
                          f"Justificativa obrigatória com ao menos {OVERRIDE_MIN_LEN} caracteres.", status=400)
    c = await db.get(Campaign, body.campaign_id)
    if c is None or await db.get(Establishment, body.establishment_id) is None:
        raise NotFound()
    now = utcnow()
    if body.expires_at is not None and body.expires_at <= now:
        raise DomainError("VALIDITY_INVALID", "Validade do override já passou.", status=400)
    before = await CS.evaluate_conflict(db, c, body.establishment_id, record=True, ctx=ctx)
    active = (await db.execute(select(ConflictOverride).where(
        ConflictOverride.campaign_id == c.id, ConflictOverride.establishment_id == body.establishment_id,
        ConflictOverride.revoked_at.is_(None)).with_for_update().execution_options(populate_existing=True))).scalar_one_or_none()
    if active is not None:
        raise Conflict("OVERRIDE_EXISTS", "Já existe override ativo para esta campanha neste estabelecimento.")
    o = ConflictOverride(id=uuid.uuid4(), campaign_id=c.id, establishment_id=body.establishment_id,
                         justification=just, approved_by=ctx.user_id, approved_at=now, expires_at=body.expires_at)
    db.add(o)
    await db.flush([o])
    after = await CS.evaluate_conflict(db, c, body.establishment_id, record=True, ctx=ctx)
    await audit(db, ctx, "conflicts.override_created", "conflict_override", o.id,
                before={"decision": before.result, "reason_code": before.reason_code},
                after={**ov_out(o), "decision": after.result, "reason_code": after.reason_code},
                reason=just, org_id=c.org_id)
    out = ov_out(o)
    out["decision_before"] = before.as_dict()
    out["decision_after"] = after.as_dict()
    if after.reason_code == "PROIBIDO_UNIVERSAL":
        out["warning"] = "Conteúdo proibido pela política universal: o override não libera este anúncio."
    return out


@router.post("/overrides/{override_id}/revoke")
async def revoke_override(override_id: uuid.UUID, body: RevokeIn,
                          ctx: AuthContext = Depends(require("conflicts.override", global_only=True,
                                                             allow_impersonation=False)),
                          db: AsyncSession = Depends(get_db)):
    o = (await db.execute(select(ConflictOverride).where(ConflictOverride.id == override_id).with_for_update().execution_options(populate_existing=True))
         ).scalar_one_or_none()
    if o is None:
        raise NotFound()
    if o.revoked_at is not None:
        raise Conflict("ALREADY_REVOKED", "Override já revogado.")
    o.revoked_at, o.revoked_by, o.revoke_reason = utcnow(), ctx.user_id, body.reason
    await audit(db, ctx, "conflicts.override_revoked", "conflict_override", o.id, reason=body.reason)
    return ov_out(o)


# ─── avaliação / decisões ────────────────────────────────────────────────────

class EvaluateIn(_Strict):
    campaign_id: Optional[uuid.UUID] = None
    creative_id: Optional[uuid.UUID] = None
    other_creative_id: Optional[uuid.UUID] = None
    establishment_id: Optional[uuid.UUID] = None
    record: bool = False


@router.post("/evaluate")
async def evaluate(body: EvaluateIn, ctx: AuthContext = Depends(get_auth_context), db: AsyncSession = Depends(get_db)):
    _staff(ctx)
    if body.creative_id and body.other_creative_id:
        d = await CS.are_competitors(db, body.creative_id, body.other_creative_id, record=body.record, ctx=ctx)
        return {"mode": "AD_VS_AD", "competitors": d.result == "BLOCK", **d.as_dict()}
    subject = body.campaign_id or body.creative_id
    if subject is None or body.establishment_id is None:
        raise DomainError("EVALUATE_INVALID", "Informe campanha/criativo e estabelecimento.", status=400)
    d = await CS.evaluate_conflict(db, subject, body.establishment_id, record=body.record, ctx=ctx)
    return {"mode": "AD_VS_ESTABLISHMENT", **d.as_dict()}


@router.get("/decisions")
async def list_decisions(campaign_id: Optional[uuid.UUID] = None, establishment_id: Optional[uuid.UUID] = None,
                         page: int = Query(1, ge=1), page_size: int = Query(50, ge=1, le=100),
                         ctx: AuthContext = Depends(get_auth_context), db: AsyncSession = Depends(get_db)):
    _staff(ctx)
    base = select(ConflictDecision)
    if campaign_id:
        base = base.where(ConflictDecision.campaign_id == campaign_id)
    if establishment_id:
        base = base.where(ConflictDecision.establishment_id == establishment_id)
    total = (await db.execute(select(func.count()).select_from(base.subquery()))).scalar_one()
    rows = (await db.execute(base.order_by(ConflictDecision.evaluated_at.desc(), ConflictDecision.id)
                             .offset((page - 1) * page_size).limit(page_size))).scalars().all()
    return {"items": [{"id": str(d.id), "campaign_id": str(d.campaign_id) if d.campaign_id else None,
                       "creative_id": str(d.creative_id) if d.creative_id else None,
                       "establishment_id": str(d.establishment_id) if d.establishment_id else None,
                       "other_creative_id": str(d.other_creative_id) if d.other_creative_id else None,
                       "result": d.result, "reason_code": d.reason_code, "rule_path": d.rule_path,
                       "engine_version": d.engine_version, "evaluated_at": d.evaluated_at} for d in rows],
            "total": total, "page": page, "page_size": page_size}
