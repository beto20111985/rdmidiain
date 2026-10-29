"""
RD Mídia Indoor — Auto-cadastro do anunciante (W-ORG; REQ-ONB-004; ETAPA2 #1017–#1023).

Prefixo ``/api/v1/advertiser-signup``. Contrato completo para o frontend em
``stack/logs/002/W-ORG_relatorio.md`` (seção "Pós-janela").

Públicas (sem login; token SOMENTE no header ``X-RD-Signup-Token``):
- ``GET  /terms``     — Termos do Anunciante vigentes + versões de consentimento + política de senha.
- ``GET  /options``   — segmentos e categorias ativos (formulário público).
- ``POST /start``     — ``{email, captcha_token?}`` → 202 sempre (sem enumeração); rate limit.
- ``GET  /application`` — valida o token (não consome): ``{email, expires_at}``.
- ``POST /complete``  — cria conta + organização anunciante + aceite + consentimentos e já
  devolve a sessão (payload igual ao login + cookie de refresh).
Autenticada (anunciante):
- ``POST /sales-contact`` — "falar com o comercial" (lead no CRM, sem duplicar).
"""
from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, BackgroundTasks, Depends, Header, Request, Response
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import ratelimit
from app.core.authz import AuthContext, require
from app.core.captcha import get_captcha_verifier
from app.core.config import settings
from app.core.database import get_db
from app.core.errors import DomainError
from app.core.net import client_ip
from app.core.passwords import PASSWORD_MAX_LENGTH, PASSWORD_MIN_LENGTH
from app.modules.onboarding import advertiser as svc
from app.modules.organizations.common import publish_safe

router = APIRouter()


def _ua(request: Request) -> Optional[str]:
    return (request.headers.get("user-agent") or "")[:500] or None


@router.get("/terms")
async def terms(request: Request, db: AsyncSession = Depends(get_db)):
    from app.modules.contracts.service import template_out

    await ratelimit.enforce("adv_signup_terms_ip", client_ip(request), limit=120, window_s=3600)
    tpl = await svc.current_terms(db)
    return {
        "advertiser_terms": template_out(tpl) if tpl else None,
        "terms_version": settings.TERMS_VERSION,
        "privacy_version": settings.PRIVACY_VERSION,
        "password_policy": {"min_length": PASSWORD_MIN_LENGTH, "max_length": PASSWORD_MAX_LENGTH,
                            "requires_letters_and_digits": True, "must_not_contain_email": True},
        "captcha_required": bool(get_captcha_verifier().enabled),
        "token_header": svc.TOKEN_HEADER,
    }


@router.get("/options")
async def options(request: Request, db: AsyncSession = Depends(get_db)):
    """Taxonomia ativa para o formulário público (somente nomes/hierarquia — dado não sensível)."""
    from sqlalchemy import select

    from app.models.taxonomy import Segment, TaxonomyNode

    await ratelimit.enforce("adv_signup_terms_ip", client_ip(request), limit=120, window_s=3600)
    segs = (await db.execute(select(Segment).where(Segment.is_active.is_(True))
                             .order_by(Segment.depth, Segment.name))).scalars().all()
    nodes = (await db.execute(select(TaxonomyNode).where(TaxonomyNode.is_active.is_(True))
                              .order_by(TaxonomyNode.depth, TaxonomyNode.name))).scalars().all()

    def _o(x, kind=None):
        d = {"id": str(x.id), "parent_id": str(x.parent_id) if x.parent_id else None, "name": x.name,
             "depth": x.depth}
        if kind:
            d["kind"] = kind
        return d

    return {"segments": [_o(x) for x in segs], "categories": [_o(n, n.kind) for n in nodes]}


@router.post("/start", status_code=202)
async def start(body: svc.SignupStartIn, request: Request, background: BackgroundTasks,
                db: AsyncSession = Depends(get_db)):
    ip = client_ip(request)
    await ratelimit.enforce("adv_signup_ip", ip, limit=10, window_s=3600)
    cap = get_captcha_verifier()
    if cap.enabled and not await cap.verify(body.captcha_token, ip):
        raise DomainError("CAPTCHA_REQUIRED", "Confirme que você não é um robô.", status=400)
    email = str(body.email).strip().lower()
    ok, _ = await ratelimit.hit("adv_signup_email", email, limit=3, window_s=86400)
    if ok:
        kind, raw = await svc.request_signup(db, email)
        await db.commit()
        background.add_task(svc.send_signup_email, kind, email, raw)
    # resposta idêntica em qualquer caso (e-mail novo, existente ou limitado)
    return {"status": "accepted", "message": "Se o e-mail for válido, você receberá as instruções em instantes."}


@router.get("/application")
async def application(request: Request, x_rd_signup_token: Optional[str] = Header(None, alias=svc.TOKEN_HEADER)):
    from datetime import datetime, timezone

    from app.core.timeutils import isoformat_z

    await ratelimit.enforce("adv_signup_peek_ip", client_ip(request), limit=60, window_s=3600)
    data = await svc.peek(x_rd_signup_token)
    return {"email": data["email"],
            "expires_at": isoformat_z(datetime.fromtimestamp(int(data["exp"]), tz=timezone.utc))}


@router.post("/complete", status_code=201)
async def complete(body: svc.SignupCompleteIn, request: Request, response: Response,
                   x_rd_signup_token: Optional[str] = Header(None, alias=svc.TOKEN_HEADER),
                   db: AsyncSession = Depends(get_db)):
    from app.modules.auth.cookies import set_refresh_cookie

    ip = client_ip(request)
    await ratelimit.enforce("adv_signup_complete_ip", ip, limit=20, window_s=3600)
    out = await svc.complete(db, x_rd_signup_token, body, ip=ip, user_agent=_ua(request))
    token_data = out.pop("_token_data")
    try:
        await db.commit()
    except BaseException:
        await svc.restore(x_rd_signup_token, token_data)  # nada foi gravado: o link volta a valer
        raise
    org = out.pop("org")
    set_refresh_cookie(response, out.pop("refresh_token"))
    await publish_safe("organization.updated", {"id": str(org.id), "change": "advertiser_self_signup"},
                       org_id=org.id, city=org.city_key)
    return {**out["login"], "organization": out["organization"], "terms_acceptance": out["terms_acceptance"],
            "sales_contact_requested": out["sales_contact_requested"],
            "next_steps": ["Escolher pacote e região e contratar (self-service)",
                           "Ou falar com o comercial: POST /api/v1/advertiser-signup/sales-contact"]}


@router.post("/sales-contact")
async def sales_contact(body: svc.SalesContactIn, request: Request,
                        ctx: AuthContext = Depends(require("campaigns.view", allow_impersonation=False)),
                        db: AsyncSession = Depends(get_db)):
    await ratelimit.enforce("adv_sales_contact_user", str(ctx.user_id), limit=10, window_s=3600)
    created = await svc.request_sales_contact(db, ctx, body.org_id, body.message)
    await db.commit()
    return {"status": "requested", "already_requested": not created,
            "message": "O comercial da RD entrará em contato."}
