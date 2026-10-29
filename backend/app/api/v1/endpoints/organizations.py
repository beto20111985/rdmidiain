"""
RD Mídia Indoor — Organizações (W-ORG; AÇÃO 002). Contrato: ``docs/contracts`` §1/§3.

Rotas (prefixo ``/api/v1/organizations``):
- ``GET ""`` lista (``organizations.view``; escopo ORGANIZATION/CITY), filtros ``search``,
  ``role``, ``is_active``; paginação ``{items,total,page,page_size}``.
- ``POST ""`` cria (``organizations.manage``) — papéis explícitos + CPF/CNPJ validado.
- ``GET/PATCH /{id}``; ``POST /{id}/deactivate|activate`` (motivo; inativar cascata
  para os estabelecimentos); ``POST /{id}/kyc``.
- ``PUT /{id}/advertiser-profile`` (categoria + segmentos do anunciante).
- Agência: ``GET/POST /{agency_id}/clients``, ``POST /{agency_id}/clients/{link_id}/grants``,
  ``POST /{agency_id}/clients/{link_id}/end``.
"""
from __future__ import annotations

import uuid
from typing import Optional

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, EmailStr, Field
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.audit import audit
from app.core.authz import AuthContext, require
from app.core.database import get_db
from app.core.errors import DomainError, NotFound
from app.models.organizations import AgencyClient, BusinessRole, Organization
from app.models.taxonomy import AdvertiserProfile, AdvertiserSegment
from app.modules.organizations import service as svc
from app.modules.organizations.common import paginate

router = APIRouter()


class OrgCreateIn(BaseModel):
    name: str = Field(min_length=2, max_length=255)
    document: str = Field(min_length=11, max_length=20)
    document_type: Optional[str] = Field(default=None, pattern="^(CPF|CNPJ)$")
    business_roles: list[str] = Field(min_length=1)
    trade_name: Optional[str] = Field(default=None, max_length=255)
    responsible_name: Optional[str] = Field(default=None, max_length=255)
    email: Optional[EmailStr] = None
    phone: Optional[str] = Field(default=None, max_length=30)
    address: Optional[str] = Field(default=None, max_length=255)
    number: Optional[str] = Field(default=None, max_length=20)
    complement: Optional[str] = Field(default=None, max_length=100)
    neighborhood: Optional[str] = Field(default=None, max_length=100)
    city: Optional[str] = Field(default=None, max_length=100)
    state: Optional[str] = Field(default=None, min_length=2, max_length=2)
    postal_code: Optional[str] = Field(default=None, max_length=10)
    notes: Optional[str] = Field(default=None, max_length=4000)


class OrgPatchIn(BaseModel):
    name: Optional[str] = Field(default=None, min_length=2, max_length=255)
    document: Optional[str] = Field(default=None, min_length=11, max_length=20)
    document_type: Optional[str] = Field(default=None, pattern="^(CPF|CNPJ)$")
    business_roles: Optional[list[str]] = None
    trade_name: Optional[str] = Field(default=None, max_length=255)
    responsible_name: Optional[str] = Field(default=None, max_length=255)
    email: Optional[EmailStr] = None
    phone: Optional[str] = Field(default=None, max_length=30)
    address: Optional[str] = Field(default=None, max_length=255)
    number: Optional[str] = Field(default=None, max_length=20)
    complement: Optional[str] = Field(default=None, max_length=100)
    neighborhood: Optional[str] = Field(default=None, max_length=100)
    city: Optional[str] = Field(default=None, max_length=100)
    state: Optional[str] = Field(default=None, min_length=2, max_length=2)
    postal_code: Optional[str] = Field(default=None, max_length=10)
    notes: Optional[str] = Field(default=None, max_length=4000)


class ReasonIn(BaseModel):
    reason: str = Field(min_length=10, max_length=2000)


class KycIn(BaseModel):
    status: str = Field(pattern="^(PENDING|APPROVED|REJECTED)$")
    notes: Optional[str] = Field(default=None, max_length=2000)


class AdvertiserProfileIn(BaseModel):
    category_id: Optional[uuid.UUID] = None
    segment_ids: list[uuid.UUID] = Field(default_factory=list, max_length=200)


class AgencyLinkIn(BaseModel):
    client_org_id: uuid.UUID


class AgencyGrantIn(BaseModel):
    user_id: uuid.UUID


async def _org(db: AsyncSession, org_id: uuid.UUID, *, lock: bool = False) -> Organization:
    q = select(Organization).where(Organization.id == org_id)
    if lock:
        q = q.with_for_update()
    org = (await db.execute(q)).scalar_one_or_none()
    if org is None:
        raise NotFound()
    return org


async def _publish_org(org: Organization, change: str) -> None:
    from app.modules.organizations.common import publish_safe

    await publish_safe("organization.updated", {"id": str(org.id), "change": change}, org_id=org.id,
                       city=org.city_key)


@router.get("")
async def list_organizations(
    page: int = Query(1, ge=1),
    page_size: int = Query(25, ge=1, le=100),
    search: Optional[str] = Query(None, max_length=100),
    role: Optional[str] = Query(None, pattern="^(ADVERTISER|PARTNER|AGENCY|FRANCHISE)$"),
    is_active: Optional[bool] = None,
    sort: str = Query("name", pattern="^-?(name|created_at)$"),
    exclude_instance: bool = Query(False, description="Oculta a organização da própria instância (seletores de cliente)"),
    ctx: AuthContext = Depends(require("organizations.view")),
    db: AsyncSession = Depends(get_db),
):
    q = select(Organization).where(svc.org_filter(ctx, "organizations.view"))
    if exclude_instance:  # W-BIZ (autorizado pelo coordenador): instância nunca aparece como cliente
        from app.modules.instance.service import instance_org_ids

        own = await instance_org_ids(db)
        if own:
            q = q.where(Organization.id.not_in(own))
    if search:
        q = q.where(svc.search_clause(search))
    if role:
        q = q.where(Organization.business_roles.any(role))
    if is_active is not None:
        q = q.where(Organization.is_active.is_(is_active))
    col = Organization.created_at if sort.lstrip("-") == "created_at" else Organization.name
    q = q.order_by(col.desc() if sort.startswith("-") else col.asc(), Organization.id)
    return await paginate(db, q, page, page_size, lambda o: svc.org_out(o, full=False))


@router.post("", status_code=201)
async def create_organization(
    body: OrgCreateIn,
    ctx: AuthContext = Depends(require("organizations.manage")),
    db: AsyncSession = Depends(get_db),
):
    org = await svc.create_org(db, ctx, body.model_dump())
    await db.commit()
    await _publish_org(org, "created")
    return svc.org_out(org)


@router.get("/{org_id}")
async def get_organization(org_id: uuid.UUID, ctx: AuthContext = Depends(require("organizations.view")),
                           db: AsyncSession = Depends(get_db)):
    org = await _org(db, org_id)
    svc.ensure_org(ctx, "organizations.view", org)
    out = svc.org_out(org)
    prof = await db.get(AdvertiserProfile, org.id)
    out["advertiser_profile"] = {
        "category_id": str(prof.category_id) if prof and prof.category_id else None,
        "segment_ids": sorted(str(s) for s in (await db.execute(
            select(AdvertiserSegment.segment_id).where(AdvertiserSegment.org_id == org.id))).scalars()),
    }
    return out


@router.patch("/{org_id}")
async def update_organization(org_id: uuid.UUID, body: OrgPatchIn,
                              ctx: AuthContext = Depends(require("organizations.manage")),
                              db: AsyncSession = Depends(get_db)):
    org = await _org(db, org_id, lock=True)
    svc.ensure_org(ctx, "organizations.manage", org)
    changes = await svc.update_org(db, ctx, org, body.model_dump(exclude_unset=True))
    await db.commit()
    if changes:
        await _publish_org(org, "updated")
    return svc.org_out(org)


@router.post("/{org_id}/deactivate")
async def deactivate_organization(org_id: uuid.UUID, body: ReasonIn,
                                  ctx: AuthContext = Depends(require("organizations.manage",
                                                                     allow_impersonation=False)),
                                  db: AsyncSession = Depends(get_db)):
    org = await _org(db, org_id, lock=True)
    svc.ensure_org(ctx, "organizations.manage", org)
    affected = await svc.set_active(db, ctx, org, False, body.reason)
    await db.commit()
    await _publish_org(org, "deactivated")
    return {**svc.org_out(org), "establishments_deactivated": [str(e.id) for e in affected]}


@router.post("/{org_id}/activate")
async def activate_organization(org_id: uuid.UUID, body: ReasonIn,
                                ctx: AuthContext = Depends(require("organizations.manage",
                                                                   allow_impersonation=False)),
                                db: AsyncSession = Depends(get_db)):
    org = await _org(db, org_id, lock=True)
    svc.ensure_org(ctx, "organizations.manage", org)
    await svc.set_active(db, ctx, org, True, body.reason)
    await db.commit()
    await _publish_org(org, "activated")
    return svc.org_out(org)


@router.post("/{org_id}/kyc")
async def review_kyc(org_id: uuid.UUID, body: KycIn,
                     ctx: AuthContext = Depends(require("organizations.manage", global_only=True,
                                                        allow_impersonation=False)),
                     db: AsyncSession = Depends(get_db)):
    org = await _org(db, org_id, lock=True)
    await svc.set_kyc(db, ctx, org, body.status, body.notes)
    await db.commit()
    return svc.org_out(org)


@router.put("/{org_id}/advertiser-profile")
async def set_advertiser_profile(org_id: uuid.UUID, body: AdvertiserProfileIn,
                                 ctx: AuthContext = Depends(require("organizations.manage")),
                                 db: AsyncSession = Depends(get_db)):
    from app.modules.taxonomy.service import validate_category_id, validate_segment_ids

    org = await _org(db, org_id, lock=True)
    svc.ensure_org(ctx, "organizations.manage", org)
    if not org.has_role(BusinessRole.ADVERTISER):
        raise DomainError("ORG_NOT_ADVERTISER", "A organização não tem o papel de anunciante.", status=400)
    cat = await validate_category_id(db, body.category_id)
    segs = await validate_segment_ids(db, body.segment_ids)
    prof = await db.get(AdvertiserProfile, org.id)
    before = {"category_id": str(prof.category_id) if prof and prof.category_id else None,
              "segment_ids": sorted(str(s) for s in (await db.execute(
                  select(AdvertiserSegment.segment_id).where(AdvertiserSegment.org_id == org.id))).scalars())}
    if prof is None:
        prof = AdvertiserProfile(org_id=org.id, category_id=cat, updated_by=ctx.user_id)
        db.add(prof)
    else:
        prof.category_id = cat
        prof.updated_by = ctx.user_id
    await db.execute(delete(AdvertiserSegment).where(AdvertiserSegment.org_id == org.id))
    for s in segs:
        db.add(AdvertiserSegment(org_id=org.id, segment_id=s))
    await db.flush()
    after = {"category_id": str(cat) if cat else None, "segment_ids": sorted(str(s) for s in segs)}
    await audit(db, ctx, "organization.advertiser_profile_set", "organization", org.id, before=before, after=after,
                org_id=org.id)
    await db.commit()
    return after


# ─── Agência → clientes ──────────────────────────────────────────────────────

@router.get("/{agency_id}/clients")
async def list_agency_clients(agency_id: uuid.UUID, include_ended: bool = False,
                              ctx: AuthContext = Depends(require("organizations.view")),
                              db: AsyncSession = Depends(get_db)):
    agency = await _org(db, agency_id)
    svc.ensure_org(ctx, "organizations.view", agency)
    q = select(AgencyClient, Organization).join(Organization, Organization.id == AgencyClient.client_org_id).where(
        AgencyClient.agency_org_id == agency.id)
    if not include_ended:
        q = q.where(AgencyClient.status == "ACTIVE")
    rows = (await db.execute(q.order_by(Organization.name))).all()
    return {"items": [svc.agency_link_out(link, client) for link, client in rows], "total": len(rows),
            "page": 1, "page_size": max(len(rows), 1)}


@router.post("/{agency_id}/clients", status_code=201)
async def link_agency_client(agency_id: uuid.UUID, body: AgencyLinkIn,
                             ctx: AuthContext = Depends(require("organizations.manage", allow_impersonation=False)),
                             db: AsyncSession = Depends(get_db)):
    agency = await _org(db, agency_id, lock=True)
    svc.ensure_org(ctx, "organizations.manage", agency)
    client = await _org(db, body.client_org_id)
    svc.ensure_org(ctx, "organizations.manage", client)
    link = await svc.link_agency(db, ctx, agency, client)
    await db.commit()
    return svc.agency_link_out(link, client)


async def _link(db: AsyncSession, agency_id: uuid.UUID, link_id: uuid.UUID) -> AgencyClient:
    link = (await db.execute(select(AgencyClient).where(AgencyClient.id == link_id).with_for_update())).scalar_one_or_none()
    if link is None or link.agency_org_id != agency_id:
        raise NotFound()
    return link


@router.post("/{agency_id}/clients/{link_id}/grants", status_code=201)
async def grant_agency_access(agency_id: uuid.UUID, link_id: uuid.UUID, body: AgencyGrantIn,
                              ctx: AuthContext = Depends(require("users.manage", allow_impersonation=False)),
                              db: AsyncSession = Depends(get_db)):
    agency = await _org(db, agency_id)
    svc.ensure_org(ctx, "organizations.view", agency)
    link = await _link(db, agency_id, link_id)
    g = await svc.grant_agency_user(db, ctx, link, body.user_id)
    await db.commit()
    return {"id": str(g.id), "agency_client_id": str(link.id), "user_id": str(g.user_id),
            "role_assignment_id": str(g.role_assignment_id), "role": "agency",
            "scope_type": "ORGANIZATION", "scope_id": str(link.client_org_id)}


@router.post("/{agency_id}/clients/{link_id}/end")
async def end_agency_client(agency_id: uuid.UUID, link_id: uuid.UUID, body: ReasonIn,
                            ctx: AuthContext = Depends(require("organizations.manage", allow_impersonation=False)),
                            db: AsyncSession = Depends(get_db)):
    agency = await _org(db, agency_id)
    svc.ensure_org(ctx, "organizations.manage", agency)
    link = await _link(db, agency_id, link_id)
    revoked = await svc.end_agency_link(db, ctx, link, body.reason)
    await db.commit()
    return {**svc.agency_link_out(link), "assignments_revoked": revoked}
