"""
RD Mídia Indoor — Auto-cadastro do ANUNCIANTE (W-ORG; ETAPA2 #1017–#1023; REQ-ONB-004).

Fluxo público (mesmo padrão de segurança do auto-cadastro do parceiro):
1. ``request_signup`` — e-mail (+ CAPTCHA quando configurado). Resposta SEMPRE 202 (sem
   enumeração). E-mail novo → token de uso único (Redis, só o hash SHA-256 como chave,
   TTL ``TOKEN_TTL_S``) enviado pelo canal de e-mail real; e-mail já cadastrado → aviso
   "você já tem conta" para o PRÓPRIO endereço (nenhum token).
2. ``peek`` — valida o token (header ``X-RD-Signup-Token``; nunca query string).
3. ``complete`` — valida TUDO antes de consumir o token (documento CPF/CNPJ inclusive
   alfanumérico, segmentos, termos vigentes, versões de consentimento, senha pela
   política do W1, e-mail livre); consome o token (GETDEL — uso único); cria, na mesma
   transação: usuário (interface W1), organização com papel ADVERTISER, perfil/segmentos
   do anunciante, perfil ``advertiser`` no escopo ORGANIZATION (interface W1), aceite
   eletrônico dos TERMOS DO ANUNCIANTE (versão/hash/IP/data/PDF — ``contract_acceptances``)
   e consentimentos LGPD (interface W1). Falha depois do consumo ⇒ rollback + token
   restaurado (TTL restante). Opcional ``contact_sales`` → lead no CRM.
4. ``request_sales_contact`` — anunciante logado pede contato do comercial: lead no CRM
   (interface W-BIZ, origem ``AUTO_CADASTRO``) sem duplicar.

Interfaces de outros domínios ficam isoladas em ``_w1_*`` / ``_crm_*`` abaixo.
"""
from __future__ import annotations

import hashlib
import json
import secrets
import time
import uuid
from typing import Any, Optional

from pydantic import BaseModel, EmailStr, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.audit import audit
from app.core.config import settings
from app.core.email import OutgoingEmail, mask_email, send_safely
from app.core.errors import Conflict, DomainError, NotFound, Unauthorized
from app.core.redis import get_redis, rkey
from app.models.contracts import ContractKind
from app.models.organizations import BusinessRole, Organization
from app.models.taxonomy import AdvertiserProfile, AdvertiserSegment

TOKEN_TTL_S = 24 * 3600
TOKEN_HEADER = "X-RD-Signup-Token"
SOURCE = "AUTO_CADASTRO"


# ─── Schemas ─────────────────────────────────────────────────────────────────

class SignupStartIn(BaseModel):
    email: EmailStr
    captcha_token: Optional[str] = Field(default=None, max_length=4096)


class SignupOrgIn(BaseModel):
    name: str = Field(min_length=2, max_length=255)  # razão social (PJ) ou nome completo (PF)
    trade_name: Optional[str] = Field(default=None, max_length=255)
    document: str = Field(min_length=11, max_length=20)
    document_type: Optional[str] = Field(default=None, pattern="^(CPF|CNPJ)$")
    responsible_name: Optional[str] = Field(default=None, max_length=255)
    phone: str = Field(min_length=8, max_length=30)
    contact_email: Optional[EmailStr] = None
    address: Optional[str] = Field(default=None, max_length=255)
    number: Optional[str] = Field(default=None, max_length=20)
    complement: Optional[str] = Field(default=None, max_length=100)
    neighborhood: Optional[str] = Field(default=None, max_length=100)
    city: str = Field(min_length=2, max_length=100)
    state: str = Field(min_length=2, max_length=2)
    postal_code: Optional[str] = Field(default=None, max_length=10)
    category_id: Optional[uuid.UUID] = None
    segment_ids: list[uuid.UUID] = Field(min_length=1, max_length=50)


class SignupUserIn(BaseModel):
    full_name: str = Field(min_length=2, max_length=255)
    password: str = Field(min_length=1, max_length=256)


class SignupConsentsIn(BaseModel):
    terms_template_id: uuid.UUID  # versão vigente dos Termos do Anunciante (GET /advertiser-signup/terms)
    accept_terms: bool
    terms_version: str = Field(min_length=1, max_length=40)  # termos de uso da plataforma (LGPD)
    privacy_version: str = Field(min_length=1, max_length=40)
    accept_privacy: bool
    marketing_opt_in: bool = False


class SignupCompleteIn(BaseModel):
    organization: SignupOrgIn
    user: SignupUserIn
    consents: SignupConsentsIn
    contact_sales: bool = False
    sales_message: Optional[str] = Field(default=None, max_length=1000)


class SalesContactIn(BaseModel):
    org_id: uuid.UUID
    message: Optional[str] = Field(default=None, max_length=1000)


# ─── Token (Redis, uso único) ────────────────────────────────────────────────

def _hash(raw: str) -> str:
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _key(raw: str) -> str:
    return rkey("advsignup", _hash(raw))


def _invalid() -> Unauthorized:
    return Unauthorized("SIGNUP_TOKEN_INVALID", "Link de cadastro inválido, expirado ou já utilizado.")


def _check_shape(raw: Optional[str]) -> str:
    if not raw or len(raw) < 20 or len(raw) > 128:
        raise _invalid()
    return raw


def _decode(blob: Optional[str]) -> dict:
    if not blob:
        raise _invalid()
    try:
        data = json.loads(blob)
    except ValueError:
        raise _invalid() from None
    if not isinstance(data, dict) or int(data.get("exp", 0)) <= int(time.time()):
        raise _invalid()
    return data


async def peek(raw: Optional[str]) -> dict:
    raw = _check_shape(raw)
    r = await get_redis()
    return _decode(await r.get(_key(raw)))


async def consume(raw: str) -> dict:
    r = await get_redis()
    return _decode(await r.getdel(_key(raw)))


async def restore(raw: str, data: dict) -> None:
    """Devolve o token após falha posterior ao consumo (mantém a expiração original)."""
    left = int(data.get("exp", 0)) - int(time.time())
    if left > 0:
        r = await get_redis()
        await r.set(_key(raw), json.dumps(data), ex=left, nx=True)


async def _new_token(email: str) -> str:
    raw = secrets.token_urlsafe(32)
    r = await get_redis()
    data = {"email": email, "exp": int(time.time()) + TOKEN_TTL_S, "nonce": secrets.token_hex(8)}
    await r.set(_key(raw), json.dumps(data), ex=TOKEN_TTL_S, nx=True)
    return raw


# ─── Interfaces de outros domínios (isoladas) ────────────────────────────────

async def _w1_create_user(db: AsyncSession, *, email: str, full_name: str, password: str):
    from app.modules.auth.accounts import create_user_account

    return await create_user_account(db, email=email, full_name=full_name, password=password,
                                     origin="advertiser_self_signup")


async def _w1_grant_advertiser(db: AsyncSession, *, user_id: uuid.UUID, org_id: uuid.UUID):
    from app.modules.auth.accounts import grant_client_role

    return await grant_client_role(db, user_id=user_id, role_slug="advertiser", scope_type="ORGANIZATION",
                                   scope_id=org_id, granted_by=None,
                                   reason="Auto-cadastro do anunciante (dono da organização)")


async def _w1_record_consent(db: AsyncSession, ctx: Any, *, user_id: uuid.UUID, document: str, version: str,
                             ip: Optional[str], user_agent: Optional[str]):
    from app.modules.auth.accounts import record_consent

    return await record_consent(db, user_id=user_id, document=document, version=version, action="accepted",
                                ip=ip, user_agent=user_agent, ctx=ctx)


async def _w1_issue_login(db: AsyncSession, user: Any, *, ip: Optional[str], user_agent: Optional[str]):
    from app.modules.auth.accounts import issue_login

    return await issue_login(db, user, ip=ip, user_agent=user_agent, via="advertiser_self_signup")


async def _crm_upsert_lead(db: AsyncSession, *, org: Organization, contact_name: str, email: str,
                           notes: Optional[str]):
    from app.modules.crm.service import upsert_inbound_lead

    return await upsert_inbound_lead(db, source=SOURCE, name=contact_name, company=org.trade_name or org.name,
                                     email=email, phone=org.phone, org_id=org.id, city=org.city, uf=org.state,
                                     interest="ANUNCIANTE", notes=notes)


# ─── Passo 1: pedido de cadastro ─────────────────────────────────────────────

async def request_signup(db: AsyncSession, email: str) -> tuple[str, Optional[str]]:
    """Retorna (tipo_de_email, token_cru|None). Tipos: ``LINK`` | ``EXISTING_ACCOUNT``."""
    from app.models.users import User
    from app.modules.auth.service import normalize_email

    email = normalize_email(email)
    exists = (await db.execute(select(User.id).where(User.email == email))).first() is not None
    raw = None if exists else await _new_token(email)
    await audit(db, None, "advertiser_signup.requested", "advertiser_signup", _hash(email)[:16],
                after={"email": mask_email(email), "existing_account": exists}, origin="api")
    return ("EXISTING_ACCOUNT" if exists else "LINK"), raw


async def send_signup_email(kind: str, email: str, raw: Optional[str]) -> None:
    base = settings.FRONTEND_URL.rstrip("/")
    if kind == "LINK":
        text = ("Recebemos seu pedido de cadastro como anunciante na RD Mídia Indoor.\n\n"
                f"Para continuar, acesse: {base}/cadastro-anunciante?token={raw}\n\n"
                "O link é pessoal, vale por 24 horas e pode ser usado uma única vez.\n"
                "Se você não pediu este cadastro, ignore esta mensagem.")
    else:
        text = ("Recebemos um pedido de cadastro como anunciante com este e-mail, mas ele já possui conta "
                "na RD Mídia Indoor.\n\n"
                f"Entre em {base}/login ou recupere a senha em {base}/esqueci-senha.\n"
                "Se você não pediu este cadastro, ignore esta mensagem.")
    await send_safely(OutgoingEmail(to=email, subject="RD Mídia Indoor — cadastro de anunciante", text=text,
                                    tags={"kind": "advertiser_signup"}))


# ─── Passo 3: conclusão ──────────────────────────────────────────────────────

async def current_terms(db: AsyncSession):
    from app.modules.contracts.service import published_template

    return await published_template(db, ContractKind.ADVERTISER_TERMS.value)


async def _validate(db: AsyncSession, email: str, body: SignupCompleteIn) -> dict:
    """Validações que NÃO consomem o token. Retorna dados normalizados."""
    from app.core.passwords import password_policy_errors
    from app.models.users import User
    from app.modules.organizations.service import check_document, ensure_document_free
    from app.modules.taxonomy.service import validate_category_id, validate_segment_ids

    c = body.consents
    if not c.accept_terms or not c.accept_privacy:
        raise DomainError("CONSENT_REQUIRED", "É preciso aceitar os Termos do Anunciante e a Política de "
                                              "Privacidade para concluir o cadastro.", status=400)
    if c.terms_version != settings.TERMS_VERSION or c.privacy_version != settings.PRIVACY_VERSION:
        raise Conflict("CONSENT_VERSION_OUTDATED", "Os termos foram atualizados. Recarregue a página e aceite a "
                                                   "versão vigente.",
                       details={"terms_version": settings.TERMS_VERSION, "privacy_version": settings.PRIVACY_VERSION})
    tpl = await current_terms(db)
    if tpl is None:
        raise Conflict("TERMS_NOT_PUBLISHED", "Os Termos do Anunciante ainda não estão disponíveis. "
                                              "Tente novamente mais tarde.")
    if tpl.id != c.terms_template_id:
        raise Conflict("TERMS_VERSION_OUTDATED", "Os Termos do Anunciante foram atualizados. Leia e aceite a "
                                                 "versão vigente.",
                       details={"current_template_id": str(tpl.id), "current_version": tpl.version})
    errs = password_policy_errors(body.user.password, email)
    if errs:
        raise DomainError("PASSWORD_POLICY", errs[0], status=400, details={"errors": errs})
    o = body.organization
    doc, dtype = check_document(o.document, o.document_type)
    # mesmo telefone exigido no pagamento (DDD + número — ``payments.payer_missing``; AÇÃO 004). E-mail
    # (contato ou o do link) e documento já são obrigatórios aqui.
    from app.modules.financial.payments import payer_missing

    if payer_missing(email, o.phone, doc):
        raise DomainError("ADVERTISER_PAYER_DATA_REQUIRED",
                          "Informe o telefone com DDD — ele é exigido no pagamento das campanhas.", status=422,
                          details={"missing": ["phone"], "errors": [
                              {"loc": ["body", "organization", "phone"],
                               "msg": "Telefone com DDD obrigatório (exigido no pagamento)."}]})
    await ensure_document_free(db, doc)
    segs = await validate_segment_ids(db, o.segment_ids)
    cat = await validate_category_id(db, o.category_id)
    if (await db.execute(select(User.id).where(User.email == email))).first() is not None:
        raise Conflict("EMAIL_UNAVAILABLE", "Este e-mail já possui conta. Entre pelo login ou recupere a senha.")
    from app.modules.establishments.geo import normalize_postal_code, normalize_uf

    try:
        uf = normalize_uf(o.state)
        postal = normalize_postal_code(o.postal_code)
    except ValueError as e:
        raise DomainError("ADDRESS_INVALID", str(e), status=400) from None
    return {"document": doc, "document_type": dtype, "segments": segs, "category_id": cat, "uf": uf,
            "postal_code": postal, "template": tpl}


async def complete(db: AsyncSession, raw: Optional[str], body: SignupCompleteIn, *, ip: Optional[str],
                   user_agent: Optional[str]) -> dict:
    from app.core.authz import AuthContext, load_grants
    from app.modules.contracts.service import accept as accept_contract
    from app.modules.organizations.service import apply_address

    token_data = await peek(raw)
    email = token_data["email"]
    v = await _validate(db, email, body)
    token_data = await consume(raw)  # uso único (corrida: o segundo recebe 401)
    try:
        user = await _w1_create_user(db, email=email, full_name=body.user.full_name.strip(),
                                     password=body.user.password)
        o = body.organization
        org = Organization(
            id=uuid.uuid4(), name=o.name.strip(), trade_name=(o.trade_name or None), document=v["document"],
            document_type=v["document_type"], business_roles=[BusinessRole.ADVERTISER.value],
            responsible_name=(o.responsible_name or body.user.full_name).strip(),
            email=str(o.contact_email or email).lower(), phone=o.phone.strip(), is_active=True, created_by=user.id,
        )
        apply_address(org, {"address": o.address, "number": o.number, "complement": o.complement,
                            "neighborhood": o.neighborhood, "city": o.city, "state": v["uf"],
                            "postal_code": v["postal_code"]})
        db.add(org)
        await db.flush()
        db.add(AdvertiserProfile(org_id=org.id, category_id=v["category_id"], updated_by=user.id))
        for sid in v["segments"]:
            db.add(AdvertiserSegment(org_id=org.id, segment_id=sid))
        await db.flush()
        await _w1_grant_advertiser(db, user_id=user.id, org_id=org.id)
        await db.flush()
        ctx = AuthContext(kind="user", user=user, grants=await load_grants(db, user.id))
        await audit(db, ctx, "organization.created", "organization", org.id,
                    after={"name": org.name, "document_type": org.document_type, "business_roles": org.business_roles,
                           "city_key": org.city_key, "origin": "advertiser_self_signup"}, org_id=org.id)
        await audit(db, ctx, "organization.advertiser_profile_set", "organization", org.id,
                    after={"category_id": v["category_id"], "segment_ids": sorted(str(s) for s in v["segments"])},
                    org_id=org.id)
        acc, _, _ = await accept_contract(db, ctx, template_id=v["template"].id, org_id=org.id, establishment_id=None,
                                          comodato_agreement_id=None, ip=ip, user_agent=user_agent)
        consents = [("terms", settings.TERMS_VERSION), ("privacy", settings.PRIVACY_VERSION)]
        if body.consents.marketing_opt_in:
            consents.append(("marketing", settings.PRIVACY_VERSION))
        for doc_kind, version in consents:
            await _w1_record_consent(db, ctx, user_id=user.id, document=doc_kind, version=version, ip=ip,
                                     user_agent=user_agent)
        lead_created = None
        if body.contact_sales:
            _, lead_created = await _crm_upsert_lead(db, org=org, contact_name=user.full_name, email=email,
                                                     notes=body.sales_message)
        await audit(db, ctx, "advertiser_signup.completed", "organization", org.id,
                    after={"user_id": str(user.id), "acceptance_id": str(acc.id), "consents": [c[0] for c in consents],
                           "contact_sales": body.contact_sales, "lead_created": lead_created}, org_id=org.id)
        login_payload, refresh_raw = await _w1_issue_login(db, user, ip=ip, user_agent=user_agent)
        await db.flush()
    except BaseException:
        await restore(raw, token_data)
        raise
    return {
        "login": login_payload,
        "refresh_token": refresh_raw,
        "organization": {"id": str(org.id), "name": org.name, "business_roles": org.business_roles,
                         "document_type": org.document_type, "kyc_status": org.kyc_status},
        "terms_acceptance": {"id": str(acc.id), "version": acc.version, "pdf_sha256": acc.pdf_sha256},
        "sales_contact_requested": bool(body.contact_sales),
        "org": org,
        "_token_data": token_data,  # para restaurar o token se o COMMIT falhar (endpoint)
    }


# ─── Passo 4: falar com o comercial ──────────────────────────────────────────

async def request_sales_contact(db: AsyncSession, ctx: Any, org_id: uuid.UUID, message: Optional[str]) -> bool:
    org = await db.get(Organization, org_id)
    if org is None or not ctx.can("campaigns.view", org_id=org.id):
        raise NotFound()
    if not org.has_role(BusinessRole.ADVERTISER):
        raise DomainError("ORG_NOT_ADVERTISER", "A organização não é anunciante.", status=400)
    _, created = await _crm_upsert_lead(db, org=org, contact_name=ctx.user.full_name, email=ctx.user.email,
                                        notes=message)
    await audit(db, ctx, "advertiser_signup.sales_contact_requested", "organization", org.id,
                after={"lead_created": created}, org_id=org.id)
    return bool(created)
