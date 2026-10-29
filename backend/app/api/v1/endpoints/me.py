"""
RD Mídia Indoor — Identidade do usuário autenticado (contrato §3) + LGPD do titular.

GET /me · PATCH /me · GET /me/consents · POST /me/consents · GET /me/data-export ·
POST /me/deletion-request
"""
from __future__ import annotations

import uuid
from typing import Literal, Optional

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.audit import audit
from app.core.authz import AuthContext, forbid_impersonation, get_auth_context_lenient
from app.core.config import settings
from app.core.database import get_db
from app.core.errors import Conflict, DomainError
from app.core.net import client_ip
from app.core.timeutils import utcnow
from app.core.permissions import (
    ROLE_TEMPLATES,
    SCOPE_CITY,
    SCOPE_ESTABLISHMENT,
    SCOPE_GLOBAL,
    SCOPE_ORGANIZATION,
)
from app.models.lgpd import CONSENT_DOCUMENTS, ConsentRecord, DataDeletionRequest
from app.models.users import User, UserSession
from app.modules.auth import service as svc
from app.modules.security.setup import setup_state

router = APIRouter()


async def build_me(db: AsyncSession, ctx: AuthContext) -> dict:
    from app.models.establishments import Establishment
    from app.models.organizations import Organization

    user: User = ctx.user
    org_ids = {g.scope_id for g in ctx.grants if g.scope_type == SCOPE_ORGANIZATION}
    est_ids = {g.scope_id for g in ctx.grants if g.scope_type == SCOPE_ESTABLISHMENT}
    ests = {}
    if est_ids:
        for e in (await db.execute(select(Establishment).where(Establishment.id.in_(est_ids)))).scalars():
            ests[e.id] = e
            org_ids.add(e.org_id)
    orgs = {}
    if org_ids:
        for o in (await db.execute(select(Organization).where(Organization.id.in_(org_ids)))).scalars():
            orgs[o.id] = o

    def org_label(oid) -> str:
        o = orgs.get(oid)
        return (o.trade_name or o.name) if o else "Organização"

    assignments = []
    contexts: list[dict] = []
    seen: set[str] = set()
    if any(g.scope_type in (SCOPE_GLOBAL, SCOPE_CITY) for g in ctx.grants):
        contexts.append({"key": "rd", "label": "RD Mídia Indoor", "kind": "PLATFORM"})
        seen.add("rd")
    for g in sorted(ctx.grants, key=lambda x: (x.role_slug, x.scope_type, str(x.scope_id or x.scope_value or ""))):
        if g.scope_type == SCOPE_GLOBAL:
            label = "Global"
        elif g.scope_type == SCOPE_ORGANIZATION:
            label = org_label(g.scope_id)
        elif g.scope_type == SCOPE_ESTABLISHMENT:
            e = ests.get(g.scope_id)
            label = (e.trade_name or e.name) if e else "Estabelecimento"
        else:
            label = g.scope_value or "Cidade"
        assignments.append(
            {
                "id": str(g.assignment_id),
                "role": g.role_slug,
                "scope_type": g.scope_type,
                "scope_id": str(g.scope_id) if g.scope_id else None,
                "scope_value": g.scope_value,
                "scope_label": label,
            }
        )
        tpl = ROLE_TEMPLATES.get(g.role_slug)
        kind = tpl.context_kind if tpl else None
        org_id = None
        if g.scope_type == SCOPE_ORGANIZATION:
            org_id = g.scope_id
        elif g.scope_type == SCOPE_ESTABLISHMENT and g.scope_id in ests:
            org_id = ests[g.scope_id].org_id
        if kind == "ESTABLISHMENT" and org_id:
            key = f"est:{org_id}"
            if key not in seen:
                seen.add(key)
                contexts.append({"key": key, "label": f"Minha unidade — {org_label(org_id)}", "kind": "ESTABLISHMENT",
                                 "org_id": str(org_id)})
        elif kind == "ADVERTISER" and org_id:
            key = f"adv:{org_id}"
            if key not in seen:
                seen.add(key)
                contexts.append({"key": key, "label": f"Minhas campanhas — {org_label(org_id)}", "kind": "ADVERTISER",
                                 "org_id": str(org_id)})

    imp = None
    if ctx.impersonation is not None:
        imp = {"id": str(ctx.impersonation.real_user_id), "name": ctx.impersonation.real_user_name}
    return {
        "user": svc.user_public(user),
        "permissions": sorted(ctx.permissions),
        "assignments": assignments,
        "contexts": contexts,
        "impersonation": {
            "active": ctx.impersonation is not None,
            "real_user": imp,
            "reason": ctx.impersonation.reason if ctx.impersonation else None,
        },
        "mfa": {"required": ctx.mfa_required, "configured": bool(user.totp_enabled)},
        "must_change_password": bool(user.must_change_password),
        # AÇÃO 003: estado do primeiro acesso (somente estados/booleans; nunca segredo)
        "security_setup": await setup_state(db, user, ctx.grants),
    }


@router.get("")
async def get_me(ctx: AuthContext = Depends(get_auth_context_lenient), db: AsyncSession = Depends(get_db)):
    return await build_me(db, ctx)


class MePatch(BaseModel):
    theme: Optional[Literal["dark", "light", "system"]] = None
    name: Optional[str] = Field(default=None, min_length=2, max_length=255)
    phone: Optional[str] = Field(default=None, max_length=30)


@router.patch("")
async def patch_me(body: MePatch, ctx: AuthContext = Depends(get_auth_context_lenient), db: AsyncSession = Depends(get_db)):
    data = body.model_dump(exclude_unset=True)
    user = (await db.execute(select(User).where(User.id == ctx.user_id).with_for_update())).scalar_one()
    if ctx.impersonation is not None and set(data) - {"theme"}:
        forbid_impersonation(ctx)
    before, after = {}, {}
    if "theme" in data and data["theme"] is not None:
        before["theme"], after["theme"] = user.theme, data["theme"]
        user.theme = data["theme"]
    if "name" in data and data["name"] is not None:
        before["name"], after["name"] = user.full_name, data["name"].strip()
        user.full_name = data["name"].strip()
    if "phone" in data:
        phone = svc.normalize_phone(data["phone"]) if data["phone"] else None
        if phone and phone != user.phone:
            taken = (await db.execute(select(User.id).where(User.phone == phone, User.id != user.id))).first()
            if taken:
                raise Conflict("PHONE_IN_USE", "Telefone já cadastrado em outra conta.")
        before["phone"], after["phone"] = user.phone, phone
        user.phone = phone
    if after:
        await audit(db, ctx, "user.profile_updated", "user", user.id, before=before, after=after)
    await db.flush()
    return {"user": svc.user_public(user)}


# ─── LGPD do titular ─────────────────────────────────────────────────────────

class ConsentIn(BaseModel):
    document: Literal["terms", "privacy", "cookies", "marketing"]
    version: Optional[str] = Field(default=None, max_length=40)
    action: Literal["accepted", "revoked"] = "accepted"


@router.get("/consents")
async def list_consents(ctx: AuthContext = Depends(get_auth_context_lenient), db: AsyncSession = Depends(get_db)):
    rows = (
        await db.execute(
            select(ConsentRecord).where(ConsentRecord.user_id == ctx.user_id).order_by(ConsentRecord.created_at)
        )
    ).scalars().all()
    current: dict[str, dict] = {}
    for r in rows:
        current[r.document] = {"document": r.document, "version": r.version, "action": r.action, "at": r.created_at}
    return {
        "current_versions": {"terms": settings.TERMS_VERSION, "privacy": settings.PRIVACY_VERSION},
        "consents": list(current.values()),
        "history": [{"document": r.document, "version": r.version, "action": r.action, "at": r.created_at} for r in rows],
    }


@router.post("/consents", status_code=201)
async def record_consent(body: ConsentIn, request: Request, ctx: AuthContext = Depends(get_auth_context_lenient),
                         db: AsyncSession = Depends(get_db)):
    forbid_impersonation(ctx)
    version = body.version or {"terms": settings.TERMS_VERSION, "privacy": settings.PRIVACY_VERSION}.get(
        body.document, "1"
    )
    from app.modules.auth.accounts import record_consent

    rec = await record_consent(
        db, user_id=ctx.user_id, document=body.document, version=version, action=body.action,
        ip=client_ip(request), user_agent=request.headers.get("user-agent"), ctx=ctx,
    )
    return {"id": str(rec.id), "document": rec.document, "version": rec.version, "action": rec.action}


@router.get("/data-export")
async def data_export(ctx: AuthContext = Depends(get_auth_context_lenient), db: AsyncSession = Depends(get_db)):
    """Exporta os dados pessoais do titular (LGPD art. 18, II e V)."""
    from app.models.audit import AuditLog

    forbid_impersonation(ctx)
    user: User = ctx.user
    sessions = (
        await db.execute(select(UserSession).where(UserSession.user_id == user.id).order_by(UserSession.created_at))
    ).scalars().all()
    consents = (
        await db.execute(select(ConsentRecord).where(ConsentRecord.user_id == user.id).order_by(ConsentRecord.created_at))
    ).scalars().all()
    acts = (
        await db.execute(
            select(AuditLog).where(AuditLog.actor_user_id == user.id).order_by(AuditLog.seq.desc()).limit(1000)
        )
    ).scalars().all()
    await audit(db, ctx, "lgpd.data_export", "user", user.id)
    return {
        "generated_for": str(user.id),
        "profile": {
            "id": str(user.id),
            "name": user.full_name,
            "email": user.email,
            "phone": user.phone,
            "created_at": user.created_at,
            "last_login": user.last_login,
            "theme": user.theme,
            "mfa_enabled": user.totp_enabled,
        },
        "assignments": [
            {"role": g.role_slug, "scope_type": g.scope_type, "scope_id": str(g.scope_id) if g.scope_id else None,
             "scope_value": g.scope_value}
            for g in ctx.grants
        ],
        "sessions": [
            {"id": str(s.id), "created_at": s.created_at, "last_seen_at": s.last_seen_at, "ip": s.ip_address,
             "user_agent": s.user_agent, "revoked_at": s.revoked_at}
            for s in sessions
        ],
        "consents": [
            {"document": c.document, "version": c.version, "action": c.action, "at": c.created_at,
             "ip": c.ip_address} for c in consents
        ],
        "activity": [
            {"at": a.created_at, "action": a.action, "entity_type": a.entity_type, "entity_id": a.entity_id,
             "ip": a.ip_address} for a in acts
        ],
    }


class DeletionIn(BaseModel):
    reason: Optional[str] = Field(default=None, max_length=2000)


@router.post("/deletion-request", status_code=202)
async def deletion_request(body: DeletionIn, ctx: AuthContext = Depends(get_auth_context_lenient),
                           db: AsyncSession = Depends(get_db)):
    forbid_impersonation(ctx)
    if ctx.user.is_admin_master:
        raise DomainError("ADMIN_MASTER_PROTECTED", "Admin Master não pode ser excluído por esta rota.", status=403)
    pending = (
        await db.execute(
            select(DataDeletionRequest).where(
                DataDeletionRequest.user_id == ctx.user_id, DataDeletionRequest.status == "PENDING"
            )
        )
    ).scalar_one_or_none()
    if pending is not None:
        return {"id": str(pending.id), "status": pending.status, "requested_at": pending.requested_at}
    req = DataDeletionRequest(id=uuid.uuid4(), user_id=ctx.user_id, reason=body.reason, status="PENDING",
                              requested_at=utcnow())
    db.add(req)
    await audit(db, ctx, "lgpd.deletion_requested", "data_deletion_request", req.id)
    await db.flush()
    return {"id": str(req.id), "status": req.status, "requested_at": req.requested_at}
