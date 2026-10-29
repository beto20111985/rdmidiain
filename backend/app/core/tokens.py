"""
RD Mídia Indoor — Tokens JWT (somente access/mfa/mfa_setup; refresh é opaco).

Claims comuns: ``iss``, ``aud``, ``sub``, ``iat``, ``exp``, ``jti``, ``typ``.
- ``typ=access``: + ``sid`` (sessão), ``amr`` (["pwd","otp"]), opcional
  ``imp_by``/``imp_id`` (impersonation). 15 min.
- ``typ=mfa``: desafio de 2º fator após senha correta. 5 min, uso único.
- ``typ=mfa_setup``: libera apenas as rotas de configuração do TOTP para perfis com
  2FA obrigatório ainda não configurado. 10 min.
- ``typ=device``: reservado ao agente de playback (``authz.register_device_authenticator``).

Nunca aceitar ``alg=none``; algoritmo fixo da configuração.
"""
from __future__ import annotations

import uuid
from datetime import timedelta
from typing import Any, Optional

from jose import JWTError, jwt

from app.core.config import settings
from app.core.timeutils import utcnow

TYP_ACCESS = "access"
TYP_MFA = "mfa"
TYP_MFA_SETUP = "mfa_setup"
TYP_DEVICE = "device"


class TokenError(Exception):
    pass


def encode(sub: str, typ: str, ttl: timedelta, extra: Optional[dict[str, Any]] = None) -> tuple[str, dict]:
    now = utcnow()
    claims: dict[str, Any] = {
        "iss": settings.JWT_ISSUER,
        "aud": settings.JWT_AUDIENCE,
        "sub": str(sub),
        "iat": int(now.timestamp()),
        "exp": int((now + ttl).timestamp()),
        "jti": uuid.uuid4().hex,
        "typ": typ,
    }
    if extra:
        claims.update(extra)
    return jwt.encode(claims, settings.JWT_SECRET_KEY, algorithm=settings.JWT_ALGORITHM), claims


def decode(token: str, *, expected_typ: Optional[str] = None) -> dict:
    try:
        claims = jwt.decode(
            token,
            settings.JWT_SECRET_KEY,
            algorithms=[settings.JWT_ALGORITHM],
            audience=settings.JWT_AUDIENCE,
            issuer=settings.JWT_ISSUER,
            options={"require_exp": True, "require_iat": True, "require_sub": True, "require_jti": True},
        )
    except JWTError as exc:
        raise TokenError(type(exc).__name__) from None
    if expected_typ is not None and claims.get("typ") != expected_typ:
        raise TokenError("wrong_typ")
    return claims


def access_ttl() -> timedelta:
    return timedelta(minutes=settings.ACCESS_TOKEN_EXPIRE_MINUTES)


def create_access_token(
    user_id: Any,
    session_id: Any,
    *,
    amr: list[str],
    impersonated_by: Any = None,
    ttl: Optional[timedelta] = None,
) -> tuple[str, dict]:
    extra: dict[str, Any] = {"sid": str(session_id), "amr": amr}
    if impersonated_by is not None:
        extra["imp_by"] = str(impersonated_by)
        extra["imp_id"] = str(session_id)
    return encode(str(user_id), TYP_ACCESS, ttl or access_ttl(), extra)
