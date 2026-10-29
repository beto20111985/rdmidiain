"""
RD Mídia Indoor — Onboarding do estabelecimento (W-ORG; ETAPA2 #999–#1016).

Fluxos:
1. ADMIN   — equipe RD cria a ficha (``establishments.manage``), preenche as etapas e envia.
2. INVITE  — comercial (``organizations.manage``) convida por e-mail; o convidado preenche
             o wizard com o token (header ``X-RD-Onboarding-Token``) e envia.
3. SELF_SIGNUP — auto-cadastro público (rate limit por IP/e-mail); o token chega por e-mail
             (prova de posse do endereço; resposta sempre 202 — sem enumeração).
Todos exigem APROVAÇÃO da RD (``establishments.manage`` GLOBAL). A aprovação cria/usa a
organização (papel PARTNER) e cria o estabelecimento em ``AWAITING_CONTRACT``; o aceite do
contrato de parceria por um usuário da organização (login) ativa as telas.

Etapas do wizard (``STEPS``): company → establishment → address → screens →
commercial*/installation* (*só equipe RD). Obrigatórias para enviar: company,
establishment, address. Documentos são opcionais (#1006).
"""
from __future__ import annotations

import hashlib
import secrets
import uuid
from datetime import date, timedelta
from typing import Any, Optional

from pydantic import BaseModel, EmailStr, Field, ValidationError
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.audit import audit
from app.core.config import settings
from app.core.email import OutgoingEmail, send_safely
from app.core.errors import Conflict, DomainError, NotFound
from app.core.timeutils import utcnow, validate_timezone
from app.models.onboarding import OnboardingApplication, OnboardingAppStatus, OnboardingDocument, OnboardingOrigin
from app.models.organizations import BusinessRole, Organization
from app.modules.organizations import notifications as _notification_types  # noqa: F401  (registra tipos)

TOKEN_TTL = timedelta(days=7)
PUBLIC_STEPS = ("company", "establishment", "address", "screens")
STAFF_STEPS = ("commercial", "installation")
STEPS = PUBLIC_STEPS + STAFF_STEPS
REQUIRED_STEPS = ("company", "establishment", "address")
EDITABLE_STATUSES = (OnboardingAppStatus.INVITED.value, OnboardingAppStatus.IN_PROGRESS.value)


# ─── Schemas das etapas ──────────────────────────────────────────────────────

class CompanyStep(BaseModel):
    existing_org_id: Optional[uuid.UUID] = None  # só equipe RD
    name: Optional[str] = Field(default=None, min_length=2, max_length=255)
    trade_name: Optional[str] = Field(default=None, max_length=255)
    document: Optional[str] = Field(default=None, max_length=20)
    document_type: Optional[str] = Field(default=None, pattern="^(CPF|CNPJ)$")
    responsible_name: Optional[str] = Field(default=None, max_length=255)
    email: Optional[EmailStr] = None
    phone: Optional[str] = Field(default=None, max_length=30)


class EstablishmentStep(BaseModel):
    name: str = Field(min_length=2, max_length=255)
    trade_name: Optional[str] = Field(default=None, max_length=255)
    legal_name: Optional[str] = Field(default=None, max_length=255)
    document: Optional[str] = Field(default=None, max_length=20)
    document_type: Optional[str] = Field(default=None, pattern="^(CPF|CNPJ)$")
    responsible_name: str = Field(min_length=2, max_length=255)
    phone: str = Field(min_length=8, max_length=30)
    email: Optional[EmailStr] = None
    category_id: Optional[uuid.UUID] = None
    segment_ids: list[uuid.UUID] = Field(default_factory=list, max_length=200)


class AddressStep(BaseModel):
    address: str = Field(min_length=2, max_length=255)
    number: Optional[str] = Field(default=None, max_length=20)
    complement: Optional[str] = Field(default=None, max_length=100)
    neighborhood: Optional[str] = Field(default=None, max_length=100)
    city: str = Field(min_length=2, max_length=100)
    state: str = Field(min_length=2, max_length=2)
    postal_code: Optional[str] = Field(default=None, max_length=10)
    lat: Optional[float] = Field(default=None, ge=-90, le=90)
    lng: Optional[float] = Field(default=None, ge=-180, le=180)
    geo_source: Optional[str] = Field(default=None, pattern="^(MANUAL|GPS)$")
    timezone: Optional[str] = Field(default=None, max_length=64)


class ScreensStep(BaseModel):
    planned_screens: int = Field(ge=1, le=500)
    tv_provision: str = Field(pattern="^(OWN_TV|RD_TV_COMODATO)$")
    notes: Optional[str] = Field(default=None, max_length=2000)


class CommercialStep(BaseModel):
    partner_plan_id: Optional[uuid.UUID] = None
    receives_commission: bool = True
    pays_monthly_fee: bool = False
    monthly_fee_cents: Optional[int] = Field(default=None, ge=0)


class InstallationStep(BaseModel):
    technician_user_id: Optional[uuid.UUID] = None
    installation_date: Optional[date] = None


STEP_SCHEMAS: dict[str, type[BaseModel]] = {
    "company": CompanyStep,
    "establishment": EstablishmentStep,
    "address": AddressStep,
    "screens": ScreensStep,
    "commercial": CommercialStep,
    "installation": InstallationStep,
}


def hash_token(raw: str) -> str:
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def new_token() -> tuple[str, str]:
    raw = secrets.token_urlsafe(32)
    return raw, hash_token(raw)


def normalize_email(email: str) -> str:
    return (email or "").strip().lower()


async def send_link(email: str, raw: str, subject: str, intro: str) -> None:
    link = f"{settings.FRONTEND_URL.rstrip('/')}/cadastro-parceiro?token={raw}"
    await send_safely(OutgoingEmail(
        to=email, subject=subject,
        text=f"{intro}\n\nAcesse para preencher o cadastro: {link}\n\nO link vale por 7 dias.",
        tags={"kind": "onboarding"},
    ))


async def create_admin(db: AsyncSession, ctx: Any, email: str, org_id: Optional[uuid.UUID]) -> OnboardingApplication:
    if org_id is not None and await db.get(Organization, org_id) is None:
        raise NotFound()
    app_ = OnboardingApplication(id=uuid.uuid4(), origin=OnboardingOrigin.ADMIN.value,
                                 status=OnboardingAppStatus.IN_PROGRESS.value, email=normalize_email(email),
                                 org_id=org_id, data={}, created_by=ctx.user_id)
    if org_id is not None:
        app_.data = {"company": {"existing_org_id": str(org_id)}}
        app_.completed_steps = ["company"]
    db.add(app_)
    await db.flush()
    await audit(db, ctx, "onboarding.created", "onboarding_application", app_.id,
                after={"origin": "ADMIN", "email": app_.email, "org_id": str(org_id) if org_id else None})
    return app_


async def create_invite(db: AsyncSession, ctx: Any, email: str) -> tuple[OnboardingApplication, str]:
    raw, h = new_token()
    app_ = OnboardingApplication(id=uuid.uuid4(), origin=OnboardingOrigin.INVITE.value,
                                 status=OnboardingAppStatus.INVITED.value, email=normalize_email(email),
                                 token_hash=h, token_expires_at=utcnow() + TOKEN_TTL, invited_by=ctx.user_id,
                                 data={}, created_by=ctx.user_id)
    db.add(app_)
    await db.flush()
    await audit(db, ctx, "onboarding.invited", "onboarding_application", app_.id,
                after={"origin": "INVITE", "email": app_.email})
    return app_, raw


async def create_self_signup(db: AsyncSession, email: str) -> tuple[OnboardingApplication, str]:
    raw, h = new_token()
    app_ = OnboardingApplication(id=uuid.uuid4(), origin=OnboardingOrigin.SELF_SIGNUP.value,
                                 status=OnboardingAppStatus.INVITED.value, email=normalize_email(email),
                                 token_hash=h, token_expires_at=utcnow() + TOKEN_TTL, data={})
    db.add(app_)
    await db.flush()
    await audit(db, None, "onboarding.self_signup", "onboarding_application", app_.id,
                after={"origin": "SELF_SIGNUP", "email": app_.email}, origin="api")
    return app_, raw


async def by_token(db: AsyncSession, raw: Optional[str], *, lock: bool = False) -> OnboardingApplication:
    from app.core.errors import Unauthorized

    if not raw or len(raw) > 128:
        raise Unauthorized("ONBOARDING_TOKEN_INVALID", "Link de cadastro inválido ou expirado.")
    q = select(OnboardingApplication).where(OnboardingApplication.token_hash == hash_token(raw))
    if lock:
        q = q.with_for_update()
    app_ = (await db.execute(q)).scalar_one_or_none()
    if app_ is None or app_.token_expires_at is None or app_.token_expires_at <= utcnow():
        raise Unauthorized("ONBOARDING_TOKEN_INVALID", "Link de cadastro inválido ou expirado.")
    return app_


async def save_step(db: AsyncSession, ctx: Any, app_: OnboardingApplication, step: str, payload: dict,
                    *, staff: bool) -> dict:
    from app.modules.establishments.geo import normalize_postal_code, normalize_uf
    from app.modules.organizations.documents import DocumentError, normalize_document

    if step not in STEP_SCHEMAS:
        raise DomainError("ONBOARDING_STEP_INVALID", "Etapa inválida.", status=400, details={"steps": list(STEPS)})
    if step in STAFF_STEPS and not staff:
        raise DomainError("ONBOARDING_STEP_STAFF_ONLY", "Etapa preenchida pela equipe RD.", status=403)
    if app_.status not in EDITABLE_STATUSES:
        raise Conflict("ONBOARDING_NOT_EDITABLE", "Cadastro já enviado ou encerrado.")
    try:
        data = STEP_SCHEMAS[step].model_validate(payload)
    except ValidationError as e:
        raise DomainError("VALIDATION_ERROR", "Dados inválidos.", status=422,
                          details={"errors": [{"loc": [str(x) for x in err["loc"]], "msg": err["msg"]}
                                              for err in e.errors()]}) from None
    d = data.model_dump(mode="json")
    if step == "company":
        if d.get("existing_org_id"):
            if not staff:
                raise DomainError("ONBOARDING_STEP_STAFF_ONLY", "Somente a equipe RD vincula organização existente.",
                                  status=403)
            if await db.get(Organization, uuid.UUID(d["existing_org_id"])) is None:
                raise NotFound()
        else:
            if not d.get("name") or not d.get("document"):
                raise DomainError("COMPANY_REQUIRED", "Informe razão social/nome e CPF/CNPJ.", status=400)
            try:
                d["document"], d["document_type"] = normalize_document(d["document"], d.get("document_type"))
            except DocumentError as e:
                raise DomainError("DOCUMENT_INVALID", str(e), status=400) from None
    if step == "establishment" and d.get("document"):
        try:
            d["document"], d["document_type"] = normalize_document(d["document"], d.get("document_type"))
        except DocumentError as e:
            raise DomainError("DOCUMENT_INVALID", str(e), status=400) from None
    if step == "establishment":
        from app.modules.taxonomy.service import validate_category_id, validate_segment_ids

        await validate_category_id(db, d.get("category_id"))
        await validate_segment_ids(db, d.get("segment_ids") or [])
    if step == "address":
        try:
            d["state"] = normalize_uf(d["state"])
            d["postal_code"] = normalize_postal_code(d.get("postal_code"))
            if d.get("timezone"):
                validate_timezone(d["timezone"])
        except ValueError as e:
            raise DomainError("ADDRESS_INVALID", str(e), status=400) from None
        if (d.get("lat") is None) != (d.get("lng") is None):
            raise DomainError("COORDINATES_INCOMPLETE", "Informe latitude e longitude.", status=400)
    if step == "commercial" and d.get("pays_monthly_fee") and not d.get("monthly_fee_cents"):
        raise DomainError("MONTHLY_FEE_REQUIRED", "Informe o valor da mensalidade (centavos).", status=400)
    if step == "installation" and d.get("technician_user_id"):
        from app.models.users import User

        if await db.get(User, uuid.UUID(d["technician_user_id"])) is None:
            raise DomainError("TECHNICIAN_NOT_FOUND", "Técnico inexistente.", status=400)
    newdata = dict(app_.data or {})
    newdata[step] = d
    app_.data = newdata
    if step not in (app_.completed_steps or []):
        app_.completed_steps = list(app_.completed_steps or []) + [step]
    if app_.status == OnboardingAppStatus.INVITED.value:
        app_.status = OnboardingAppStatus.IN_PROGRESS.value
    await db.flush()
    await audit(db, ctx, "onboarding.step_saved", "onboarding_application", app_.id, after={"step": step},
                origin=None if ctx is not None else "api")
    return d


async def add_document(db: AsyncSession, ctx: Any, app_: OnboardingApplication, kind: str, data: bytes) -> OnboardingDocument:
    from app.modules.organizations import files

    if app_.status not in EDITABLE_STATUSES + (OnboardingAppStatus.SUBMITTED.value,):
        raise Conflict("ONBOARDING_NOT_EDITABLE", "Cadastro encerrado.")
    count = (await db.execute(select(func.count()).select_from(OnboardingDocument).where(
        OnboardingDocument.application_id == app_.id))).scalar_one()
    if count >= 10:
        raise DomainError("DOCUMENT_LIMIT", "Limite de documentos atingido.", status=400)
    ctype = files.sniff_document(data)
    did = uuid.uuid4()
    ext = {"application/pdf": "pdf", "image/jpeg": "jpg", "image/png": "png"}[ctype]
    stored = await files.store(db, files.key_for("onboarding", app_.id, f"{did}.{ext}"), data, ctype,
                               max_bytes=files.MAX_DOCUMENT_BYTES)
    doc = OnboardingDocument(id=did, application_id=app_.id, kind=kind, storage_key=stored.key, sha256=stored.sha256,
                             content_type=ctype, size_bytes=stored.size, uploaded_by=getattr(ctx, "user_id", None))
    db.add(doc)
    await db.flush()
    await audit(db, ctx, "onboarding.document_added", "onboarding_application", app_.id,
                after={"document_id": str(did), "kind": kind, "sha256": stored.sha256},
                origin=None if ctx is not None else "api")
    return doc


async def submit(db: AsyncSession, ctx: Any, app_: OnboardingApplication) -> None:
    from app.modules.notifications.service import StaffRecipients, notify

    if app_.status not in EDITABLE_STATUSES:
        raise Conflict("ONBOARDING_NOT_EDITABLE", "Cadastro já enviado ou encerrado.")
    missing = [s for s in REQUIRED_STEPS if s not in (app_.completed_steps or [])]
    if missing:
        raise DomainError("ONBOARDING_INCOMPLETE", "Preencha as etapas obrigatórias.", status=400,
                          details={"missing_steps": missing})
    app_.status = OnboardingAppStatus.SUBMITTED.value
    app_.submitted_at = utcnow()
    await db.flush()
    await audit(db, ctx, "onboarding.submitted", "onboarding_application", app_.id, after={"status": app_.status},
                origin=None if ctx is not None else "api")
    addr = (app_.data or {}).get("address", {})
    est = (app_.data or {}).get("establishment", {})
    from app.core.authz import city_key

    await notify(db, [StaffRecipients("establishments.manage", city_key(addr.get("state"), addr.get("city")))],
                 "onboarding.submitted",
                 {"establishment_name": est.get("name", ""), "city": addr.get("city", ""),
                  "state": addr.get("state", ""), "application_id": str(app_.id)},
                 dedupe_key=f"onboarding.submitted:{app_.id}", entity=("onboarding_application", app_.id))


async def approve(db: AsyncSession, ctx: Any, app_: OnboardingApplication) -> tuple[Organization, Any]:
    from app.modules.establishments.service import create_establishment
    from app.modules.notifications.service import OrgRecipients, notify
    from app.modules.organizations.service import create_org

    if app_.status != OnboardingAppStatus.SUBMITTED.value:
        raise Conflict("ONBOARDING_NOT_SUBMITTED", "Somente cadastros enviados podem ser aprovados.",
                       details={"status": app_.status})
    data = app_.data or {}
    comp, estd, addr = data["company"], data["establishment"], data["address"]
    commercial = data.get("commercial", {})
    inst = data.get("installation", {})
    if comp.get("existing_org_id"):
        org = await db.get(Organization, uuid.UUID(comp["existing_org_id"]))
        if org is None:
            raise NotFound()
        if not org.has_role(BusinessRole.PARTNER):
            org.business_roles = list(org.business_roles or []) + [BusinessRole.PARTNER.value]
    else:
        existing = (await db.execute(select(Organization).where(Organization.document == comp["document"]))
                    ).scalar_one_or_none()
        if existing is not None:
            raise Conflict("DOCUMENT_ALREADY_REGISTERED",
                           "CPF/CNPJ já cadastrado: vincule a organização existente na etapa 'company'.",
                           details={"org_id": str(existing.id)})
        org = await create_org(db, ctx, {
            "name": comp["name"], "document": comp["document"], "document_type": comp["document_type"],
            "business_roles": [BusinessRole.PARTNER.value], "trade_name": comp.get("trade_name"),
            "responsible_name": comp.get("responsible_name"), "email": comp.get("email") or app_.email,
            "phone": comp.get("phone"), "address": addr.get("address"), "number": addr.get("number"),
            "complement": addr.get("complement"), "neighborhood": addr.get("neighborhood"),
            "city": addr.get("city"), "state": addr.get("state"), "postal_code": addr.get("postal_code"),
        })
    est = await create_establishment(db, ctx, {
        "org_id": org.id, **{k: estd.get(k) for k in ("name", "trade_name", "legal_name", "document",
                                                     "document_type", "responsible_name", "phone", "email")},
        "category_id": estd.get("category_id"), "segment_ids": estd.get("segment_ids") or [],
        **{k: addr.get(k) for k in ("address", "number", "complement", "neighborhood", "city", "state",
                                    "postal_code", "lat", "lng", "geo_source", "timezone")},
        "partner_plan_id": commercial.get("partner_plan_id"),
        "receives_commission": commercial.get("receives_commission", True),
        "pays_monthly_fee": commercial.get("pays_monthly_fee", False),
        "monthly_fee_cents": commercial.get("monthly_fee_cents"),
        "responsible_technician_id": inst.get("technician_user_id"),
        "installation_date": date.fromisoformat(inst["installation_date"]) if inst.get("installation_date") else None,
    })
    now = utcnow()
    app_.status = OnboardingAppStatus.APPROVED.value
    app_.org_id, app_.establishment_id = org.id, est.id
    app_.partner_plan_id = est.partner_plan_id
    app_.technician_user_id = est.responsible_technician_id
    app_.installation_date = est.installation_date
    app_.decided_by, app_.decided_at = ctx.user_id, now
    await db.flush()
    created_installation = None
    if est.responsible_technician_id is not None:
        from app.modules.installations.service import create_installation

        created_installation = await create_installation(db, ctx, est, None, est.responsible_technician_id,
                                                         est.installation_date, [])
    await audit(db, ctx, "onboarding.approved", "onboarding_application", app_.id,
                after={"org_id": str(org.id), "establishment_id": str(est.id),
                       "installation_id": str(created_installation.id) if created_installation else None},
                org_id=org.id)
    await notify(db, [OrgRecipients(org.id, "establishments.view")], "contract.acceptance_required",
                 {"establishment_name": est.name, "establishment_id": str(est.id)},
                 dedupe_key=f"contract.acceptance_required:{est.id}", entity=("establishment", est.id), org_id=org.id)
    return org, est


async def reject(db: AsyncSession, ctx: Any, app_: OnboardingApplication, reason: str) -> None:
    if app_.status not in (OnboardingAppStatus.SUBMITTED.value,) + EDITABLE_STATUSES:
        raise Conflict("ONBOARDING_ALREADY_DECIDED", "Cadastro já decidido.")
    before = app_.status
    app_.status = OnboardingAppStatus.REJECTED.value
    app_.decided_by, app_.decided_at, app_.decision_reason = ctx.user_id, utcnow(), reason
    await db.flush()
    await audit(db, ctx, "onboarding.rejected", "onboarding_application", app_.id, before={"status": before},
                after={"status": app_.status}, reason=reason)


def application_out(app_: OnboardingApplication, *, staff: bool, documents: Optional[list] = None) -> dict:
    from app.modules.organizations.common import iso

    data = dict(app_.data or {})
    if not staff:
        data = {k: v for k, v in data.items() if k in PUBLIC_STEPS}
    d = {
        "id": str(app_.id), "origin": app_.origin, "status": app_.status, "email": app_.email,
        "steps": [{"key": s, "required": s in REQUIRED_STEPS, "completed": s in (app_.completed_steps or []),
                   "staff_only": s in STAFF_STEPS} for s in (STEPS if staff else PUBLIC_STEPS)],
        "data": data, "submitted_at": iso(app_.submitted_at), "decided_at": iso(app_.decided_at),
        "decision_reason": app_.decision_reason,
        "org_id": str(app_.org_id) if app_.org_id else None,
        "establishment_id": str(app_.establishment_id) if app_.establishment_id else None,
        "created_at": iso(app_.created_at),
    }
    if staff:
        d.update({"token_expires_at": iso(app_.token_expires_at),
                  "invited_by": str(app_.invited_by) if app_.invited_by else None,
                  "decided_by": str(app_.decided_by) if app_.decided_by else None})
    if documents is not None:
        d["documents"] = [{"id": str(x.id), "kind": x.kind, "content_type": x.content_type, "size_bytes": x.size_bytes,
                           "sha256": x.sha256, "created_at": iso(x.created_at)} for x in documents]
    return d

