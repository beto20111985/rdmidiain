"""
RD Mídia Indoor — Onboarding do estabelecimento parceiro (W-ORG; ETAPA2 #999–#1016).

Equipe RD (``/api/v1/onboarding``):
- ``POST ""`` (``establishments.manage``) cadastro pelo admin · ``POST /invites``
  (``organizations.manage``) convite do comercial por e-mail
- ``GET ""`` · ``GET /{id}`` · ``PUT /{id}/steps/{step}`` · ``POST /{id}/documents`` ·
  ``POST /{id}/submit`` · ``POST /{id}/approve`` · ``POST /{id}/reject``
  (aprovar/recusar: ``establishments.manage`` GLOBAL)

Público, por token no header ``X-RD-Onboarding-Token`` (nunca em query string):
- ``POST /public/signup`` (auto-cadastro; 202 sempre; rate limit; CAPTCHA quando configurado)
- ``GET /public/application`` · ``PUT /public/application/steps/{step}`` ·
  ``POST /public/application/documents`` · ``POST /public/application/submit``
"""
from __future__ import annotations

import uuid
from typing import Optional

from fastapi import APIRouter, BackgroundTasks, Depends, File, Form, Header, Query, Request, UploadFile
from pydantic import BaseModel, EmailStr, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import ratelimit
from app.core.authz import AuthContext, require
from app.core.captcha import get_captcha_verifier
from app.core.database import get_db
from app.core.errors import DomainError, NotFound
from app.core.net import client_ip
from app.models.onboarding import OnboardingApplication, OnboardingDocument
from app.modules.onboarding import service as svc
from app.modules.organizations import files
from app.modules.organizations.common import paginate, publish_safe

router = APIRouter()

DOC_KINDS = "^(COMPANY_REGISTRATION|ID_DOCUMENT|PROOF_OF_ADDRESS|OTHER)$"
TOKEN_HEADER = "X-RD-Onboarding-Token"


class AdminCreateIn(BaseModel):
    email: EmailStr
    org_id: Optional[uuid.UUID] = None


class InviteIn(BaseModel):
    email: EmailStr


class SignupIn(BaseModel):
    email: EmailStr
    captcha_token: Optional[str] = Field(default=None, max_length=4096)


class ReasonIn(BaseModel):
    reason: str = Field(min_length=10, max_length=2000)


async def _app(db: AsyncSession, app_id: uuid.UUID, *, lock: bool = False) -> OnboardingApplication:
    q = select(OnboardingApplication).where(OnboardingApplication.id == app_id)
    if lock:
        q = q.with_for_update()
    a = (await db.execute(q)).scalar_one_or_none()
    if a is None:
        raise NotFound()
    return a


def _ensure_visible(ctx: AuthContext, a: OnboardingApplication) -> None:
    """Equipe com escopo CITY só vê fichas da sua cidade (ou que ela mesma criou)."""
    if ctx.can("establishments.manage", global_only=True) or ctx.can("organizations.manage", global_only=True):
        return
    if a.created_by == ctx.user_id or a.invited_by == ctx.user_id:
        return
    addr = (a.data or {}).get("address") or {}
    from app.core.authz import city_key

    if addr.get("state") and addr.get("city") and (
        ctx.can("establishments.manage", city=city_key(addr["state"], addr["city"]))
        or ctx.can("organizations.manage", city=city_key(addr["state"], addr["city"]))
    ):
        return
    raise NotFound()


async def _docs(db: AsyncSession, a: OnboardingApplication) -> list:
    return list((await db.execute(select(OnboardingDocument).where(OnboardingDocument.application_id == a.id)
                                  .order_by(OnboardingDocument.created_at))).scalars().all())


async def _publish(a: OnboardingApplication, change: str) -> None:
    await publish_safe("onboarding.updated", {"id": str(a.id), "status": a.status, "change": change})


# ─── Equipe RD ───────────────────────────────────────────────────────────────

@router.post("", status_code=201)
async def create_application(body: AdminCreateIn, ctx: AuthContext = Depends(require("establishments.manage")),
                             db: AsyncSession = Depends(get_db)):
    a = await svc.create_admin(db, ctx, str(body.email), body.org_id)
    await db.commit()
    await _publish(a, "created")
    return svc.application_out(a, staff=True, documents=[])


@router.post("/invites", status_code=201)
async def invite(body: InviteIn, background: BackgroundTasks,
                 ctx: AuthContext = Depends(require("organizations.manage", allow_impersonation=False)),
                 db: AsyncSession = Depends(get_db)):
    a, raw = await svc.create_invite(db, ctx, str(body.email))
    await db.commit()
    background.add_task(svc.send_link, a.email, raw, "RD Mídia Indoor — convite para cadastrar seu estabelecimento",
                        "Você foi convidado a cadastrar seu estabelecimento como parceiro da RD Mídia Indoor.")
    await _publish(a, "invited")
    return svc.application_out(a, staff=True, documents=[])


@router.get("")
async def list_applications(page: int = Query(1, ge=1), page_size: int = Query(25, ge=1, le=100),
                            status: Optional[str] = Query(
                                None, pattern="^(INVITED|IN_PROGRESS|SUBMITTED|APPROVED|REJECTED|CANCELLED)$"),
                            ctx: AuthContext = Depends(require("establishments.manage")),
                            db: AsyncSession = Depends(get_db)):
    q = select(OnboardingApplication)
    if not ctx.can("establishments.manage", global_only=True):
        q = q.where((OnboardingApplication.created_by == ctx.user_id) | (OnboardingApplication.invited_by == ctx.user_id))
    if status:
        q = q.where(OnboardingApplication.status == status)
    q = q.order_by(OnboardingApplication.created_at.desc())
    return await paginate(db, q, page, page_size, lambda a: svc.application_out(a, staff=True))


@router.get("/{app_id}")
async def get_application(app_id: uuid.UUID, ctx: AuthContext = Depends(require("establishments.manage")),
                          db: AsyncSession = Depends(get_db)):
    a = await _app(db, app_id)
    _ensure_visible(ctx, a)
    return svc.application_out(a, staff=True, documents=await _docs(db, a))


@router.put("/{app_id}/steps/{step}")
async def save_step(app_id: uuid.UUID, step: str, body: dict,
                    ctx: AuthContext = Depends(require("establishments.manage")), db: AsyncSession = Depends(get_db)):
    a = await _app(db, app_id, lock=True)
    _ensure_visible(ctx, a)
    await svc.save_step(db, ctx, a, step, body, staff=True)
    await db.commit()
    await _publish(a, f"step:{step}")
    return svc.application_out(a, staff=True)


@router.post("/{app_id}/documents", status_code=201)
async def add_document(app_id: uuid.UUID, kind: str = Form(..., pattern=DOC_KINDS), file: UploadFile = File(...),
                       ctx: AuthContext = Depends(require("establishments.manage")),
                       db: AsyncSession = Depends(get_db)):
    a = await _app(db, app_id, lock=True)
    _ensure_visible(ctx, a)
    doc = await svc.add_document(db, ctx, a, kind, await files.read_limited(file, files.MAX_DOCUMENT_BYTES))
    await db.commit()
    return {"id": str(doc.id), "kind": doc.kind, "content_type": doc.content_type, "size_bytes": doc.size_bytes,
            "sha256": doc.sha256}


@router.post("/{app_id}/submit")
async def submit(app_id: uuid.UUID, ctx: AuthContext = Depends(require("establishments.manage")),
                 db: AsyncSession = Depends(get_db)):
    a = await _app(db, app_id, lock=True)
    _ensure_visible(ctx, a)
    await svc.submit(db, ctx, a)
    await db.commit()
    await _publish(a, "submitted")
    return svc.application_out(a, staff=True)


@router.post("/{app_id}/approve")
async def approve(app_id: uuid.UUID, ctx: AuthContext = Depends(require("establishments.manage", global_only=True,
                                                                        allow_impersonation=False)),
                  db: AsyncSession = Depends(get_db)):
    a = await _app(db, app_id, lock=True)
    org, est = await svc.approve(db, ctx, a)
    await db.commit()
    await _publish(a, "approved")
    await publish_safe("establishment.updated", {"id": str(est.id), "org_id": str(org.id), "change": "created"},
                       org_id=org.id, establishment_id=est.id, city=est.city_key)
    return {**svc.application_out(a, staff=True),
            "next_steps": ["Convidar o responsável: POST /api/v1/auth/invites (perfil establishment_owner, escopo "
                           "ORGANIZATION da organização)", "Aceite do contrato de parceria pelo responsável",
                           "Cadastrar telas e parear dispositivos"]}


@router.post("/{app_id}/reject")
async def reject(app_id: uuid.UUID, body: ReasonIn, background: BackgroundTasks,
                 ctx: AuthContext = Depends(require("establishments.manage", global_only=True,
                                                    allow_impersonation=False)),
                 db: AsyncSession = Depends(get_db)):
    from app.core.email import OutgoingEmail, send_safely

    a = await _app(db, app_id, lock=True)
    await svc.reject(db, ctx, a, body.reason)
    await db.commit()
    background.add_task(send_safely, OutgoingEmail(
        to=a.email, subject="RD Mídia Indoor — cadastro não aprovado",
        text=f"Seu cadastro de estabelecimento não foi aprovado.\n\nMotivo: {body.reason}", tags={"kind": "onboarding"}))
    await _publish(a, "rejected")
    return svc.application_out(a, staff=True)


# ─── Público (token) ─────────────────────────────────────────────────────────

@router.post("/public/signup", status_code=202)
async def public_signup(body: SignupIn, request: Request, background: BackgroundTasks,
                        db: AsyncSession = Depends(get_db)):
    ip = client_ip(request)
    await ratelimit.enforce("onb_signup_ip", ip, limit=10, window_s=3600)
    email = svc.normalize_email(str(body.email))
    cap = get_captcha_verifier()
    if cap.enabled and not await cap.verify(body.captcha_token, ip):
        raise DomainError("CAPTCHA_REQUIRED", "Confirme que você não é um robô.", status=400)
    ok, _ = await ratelimit.hit("onb_signup_email", email, limit=3, window_s=86400)
    if ok:
        a, raw = await svc.create_self_signup(db, email)
        await db.commit()
        background.add_task(svc.send_link, a.email, raw, "RD Mídia Indoor — cadastro de estabelecimento parceiro",
                            "Recebemos seu pedido de cadastro como estabelecimento parceiro da RD Mídia Indoor.")
    # resposta idêntica em qualquer caso (sem enumeração de e-mails)
    return {"status": "accepted", "message": "Se o e-mail for válido, você receberá o link de cadastro."}


@router.get("/public/application")
async def public_get(x_rd_onboarding_token: Optional[str] = Header(None, alias=TOKEN_HEADER),
                     db: AsyncSession = Depends(get_db)):
    a = await svc.by_token(db, x_rd_onboarding_token)
    return svc.application_out(a, staff=False, documents=await _docs(db, a))


@router.put("/public/application/steps/{step}")
async def public_step(step: str, body: dict, request: Request,
                      x_rd_onboarding_token: Optional[str] = Header(None, alias=TOKEN_HEADER),
                      db: AsyncSession = Depends(get_db)):
    await ratelimit.enforce("onb_public_ip", client_ip(request), limit=120, window_s=3600)
    a = await svc.by_token(db, x_rd_onboarding_token, lock=True)
    await svc.save_step(db, None, a, step, body, staff=False)
    await db.commit()
    await _publish(a, f"step:{step}")
    return svc.application_out(a, staff=False)


@router.post("/public/application/documents", status_code=201)
async def public_document(request: Request, kind: str = Form(..., pattern=DOC_KINDS), file: UploadFile = File(...),
                          x_rd_onboarding_token: Optional[str] = Header(None, alias=TOKEN_HEADER),
                          db: AsyncSession = Depends(get_db)):
    await ratelimit.enforce("onb_public_ip", client_ip(request), limit=120, window_s=3600)
    a = await svc.by_token(db, x_rd_onboarding_token, lock=True)
    doc = await svc.add_document(db, None, a, kind, await files.read_limited(file, files.MAX_DOCUMENT_BYTES))
    await db.commit()
    return {"id": str(doc.id), "kind": doc.kind, "content_type": doc.content_type, "size_bytes": doc.size_bytes,
            "sha256": doc.sha256}


@router.post("/public/application/submit")
async def public_submit(x_rd_onboarding_token: Optional[str] = Header(None, alias=TOKEN_HEADER),
                        db: AsyncSession = Depends(get_db)):
    a = await svc.by_token(db, x_rd_onboarding_token, lock=True)
    await svc.submit(db, None, a)
    await db.commit()
    await _publish(a, "submitted")
    return svc.application_out(a, staff=False)
