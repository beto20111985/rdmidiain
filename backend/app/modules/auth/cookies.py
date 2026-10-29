"""
RD Mídia Indoor — Cookie de refresh e CSRF (contrato §2).

Fonte ÚNICA usada pelos endpoints de auth e por outros módulos que emitem sessão
(ex.: auto-cadastro do anunciante, W-ORG):

    from app.modules.auth.cookies import set_refresh_cookie
    payload, refresh_raw = await issue_login(db, user, ip=..., user_agent=..., via="advertiser_signup")
    resp = JSONResponse(payload); set_refresh_cookie(resp, refresh_raw)

Cookie ``rd_refresh``: HttpOnly; SameSite=Strict; Path=/api/v1/auth; Secure conforme
``COOKIE_SECURE`` (obrigatório em produção).
"""
from __future__ import annotations

from fastapi import Request, Response

from app.core.config import settings
from app.core.errors import Forbidden


def set_refresh_cookie(response: Response, raw: str) -> None:
    response.set_cookie(
        settings.REFRESH_COOKIE_NAME,
        raw,
        max_age=settings.REFRESH_TOKEN_EXPIRE_DAYS * 86400,
        path=settings.REFRESH_COOKIE_PATH,
        secure=bool(settings.COOKIE_SECURE),
        httponly=True,
        samesite="strict",
    )


def clear_refresh_cookie(response: Response) -> None:
    response.delete_cookie(
        settings.REFRESH_COOKIE_NAME,
        path=settings.REFRESH_COOKIE_PATH,
        secure=bool(settings.COOKIE_SECURE),
        httponly=True,
        samesite="strict",
    )


def check_csrf(request: Request) -> None:
    """Rotas que usam o cookie exigem ``X-RD-CSRF: 1`` e ``Origin`` permitido (origens do
    PAINEL: ``CORS_ORIGINS`` + ``RD_DEV_ALLOWED_ORIGINS`` só em development/test)."""
    from app.core.origins import origin_in

    if request.headers.get("x-rd-csrf") != "1":
        raise Forbidden("CSRF_FAILED", "Requisição bloqueada (CSRF).")
    if not origin_in(request.headers.get("origin"), settings.panel_origins):
        raise Forbidden("CSRF_FAILED", "Origem não permitida.")
