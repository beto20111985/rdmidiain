"""
RD Mídia Indoor — Autenticação (contrato §2 / §2.1).

Access token JWT 15 min (memória do frontend) + refresh opaco no cookie HttpOnly
``rd_refresh`` (rotação + detecção de reuso). Rotas que usam o cookie
(``/auth/refresh``, ``/auth/logout``) exigem ``X-RD-CSRF: 1`` e ``Origin`` permitido.
"""
from __future__ import annotations

import secrets
import uuid
from datetime import timedelta
from typing import Optional

from fastapi import APIRouter, BackgroundTasks, Depends, Request, Response
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import ratelimit, tokens, totp
from app.core.audit import audit
from app.core.authz import (
    AuthContext,
    Grant,
    _resolve,
    can_grant_role_in_scope,
    forbid_impersonation,
    get_auth_context_lenient,
    get_mfa_setup_context,
    get_setup_context,
    get_setup_status_context,
    load_grants,
    normalize_city_key,
    require,
    role_reserved,
)
from app.core.captcha import get_captcha_verifier
from app.core.config import settings
from app.core.crypto import hmac_hex, sha256_hex
from app.core.database import get_db, independent_session
from app.core.email import OutgoingEmail, send_safely
from app.core.errors import (
    BadRequest,
    Conflict,
    DomainError,
    Forbidden,
    NotFound,
    Unauthorized,
    error_body,
)
from app.core.net import client_ip
from app.core.redis import ensure_redis_available
from app.core.passwords import dummy_verify, validate_password_policy
from app.core.permissions import SCOPE_CITY, SCOPE_ESTABLISHMENT, SCOPE_GLOBAL, SCOPE_ORGANIZATION, SCOPE_TYPES
from app.core.timeutils import utcnow
from app.models.rbac import Permission, Role, RoleAssignment, RolePermission
from app.models.users import PasswordResetCode, User, UserInvite, UserSession
from app.modules.auth import service as svc
from app.modules.security.credentials import credential_ok as master_credential_ok
from app.modules.security.credentials import ensure_master_credential, schedule_violation_alert
from app.modules.security.setup import setup_state

router = APIRouter()

INVALID_CREDENTIALS = ("INVALID_CREDENTIALS", "E-mail/telefone ou senha inválidos.")


# ─── Cookie / CSRF ───────────────────────────────────────────────────────────

# Cookie de refresh e CSRF: fonte única em app.modules.auth.cookies (importável por outros módulos)
from app.modules.auth.cookies import check_csrf, clear_refresh_cookie, set_refresh_cookie  # noqa: E402,F401


def _meta(request: Request) -> tuple[str, Optional[str]]:
    return client_ip(request), (request.headers.get("user-agent") or "")[:500] or None


def _user_ctx(user: User) -> AuthContext:
    return AuthContext(kind="user", user=user)


def _ok_payload(user: User, issued: svc.IssuedSession) -> dict:
    from app.modules.auth.accounts import login_payload

    return login_payload(user, issued)


async def _count_failure(db: AsyncSession, user_id: uuid.UUID, kind: str) -> None:
    """Registra falha (lockout) em transação própria.

    Antes, desfaz a transação da request — ela pode estar segurando ``FOR UPDATE`` no
    usuário, o que bloquearia a transação independente.
    """
    await db.rollback()
    fr = await svc.register_failure(user_id, kind)
    if fr.just_locked:
        svc.raise_locked(settings.LOGIN_LOCKOUT_MINUTES * 60)


# ─── Schemas ─────────────────────────────────────────────────────────────────

class LoginIn(BaseModel):
    login: str = Field(min_length=3, max_length=255)
    password: str = Field(min_length=1, max_length=256)
    captcha_token: Optional[str] = Field(default=None, max_length=4096)


class MfaVerifyIn(BaseModel):
    mfa_token: str = Field(min_length=10, max_length=4096)
    code: str = Field(min_length=6, max_length=20)


class ForgotIn(BaseModel):
    email: str = Field(min_length=3, max_length=255)


class ResetIn(BaseModel):
    email: str = Field(min_length=3, max_length=255)
    code: str = Field(min_length=6, max_length=6, pattern=r"^\d{6}$")
    new_password: str = Field(min_length=1, max_length=256)


class ChangePasswordIn(BaseModel):
    current_password: str = Field(min_length=1, max_length=256)
    new_password: str = Field(min_length=1, max_length=256)


class TotpSetupIn(BaseModel):
    password: str = Field(min_length=1, max_length=256)


class TotpEnableIn(BaseModel):
    code: str = Field(min_length=6, max_length=6, pattern=r"^\d{6}$")


class TotpDisableIn(BaseModel):
    password: str = Field(min_length=1, max_length=256)
    code: str = Field(min_length=6, max_length=6, pattern=r"^\d{6}$")


class InviteIn(BaseModel):
    email: str = Field(min_length=3, max_length=255)
    role: str = Field(min_length=2, max_length=50)
    scope_type: str = Field(default=SCOPE_GLOBAL)
    scope_id: Optional[uuid.UUID] = None
    scope_value: Optional[str] = Field(default=None, max_length=120)
    name: Optional[str] = Field(default=None, max_length=255)


class InviteAcceptIn(BaseModel):
    token: str = Field(min_length=20, max_length=256)
    name: str = Field(min_length=2, max_length=255)
    password: str = Field(min_length=1, max_length=256)


class ImpersonationIn(BaseModel):
    user_id: uuid.UUID
    reason: str = Field(min_length=10, max_length=1000)


# ─── LOGIN ───────────────────────────────────────────────────────────────────

@router.post("/login")
async def login(body: LoginIn, request: Request, response: Response):
    ip, ua = _meta(request)
    await ensure_redis_available()  # Redis fora → 503 claro, ANTES de qualquer contagem de lockout
    await ratelimit.enforce("login_ip", ip, limit=30, window_s=60)
    _, ident = svc.login_identifier(body.login)

    async with independent_session() as s:
        user = await svc.find_user_by_login(s, body.login)
        if user is None:
            dummy_verify()
            retry = await svc.unknown_locked(ident or body.login)
            if retry:
                svc.raise_locked(retry)
            retry = await svc.register_unknown_failure(ident or body.login)
            if retry:
                svc.raise_locked(retry)
            raise Unauthorized(*INVALID_CREDENTIALS)
        if user.admin_master_seat is not None and not master_credential_ok(user):
            # D1: titular com selo de credencial inválido ⇒ recusa ANTES de conferir a senha (resposta idêntica
            # à de credenciais inválidas; o motivo vai só para o alerta de integridade)
            dummy_verify()
            schedule_violation_alert("credential", origin="login", user_id=user.id, seat=user.admin_master_seat)
            raise Unauthorized(*INVALID_CREDENTIALS)
        remaining = svc.lock_remaining_s(user)
        if remaining > 0:
            svc.raise_locked(remaining)
        verifier = get_captcha_verifier()
        if verifier.enabled and (user.failed_login_attempts or 0) >= settings.CAPTCHA_AFTER_FAILURES:
            if not await verifier.verify(body.captcha_token, ip):
                raise DomainError("CAPTCHA_REQUIRED", "Confirme que você não é um robô.", status=400)
        ok, rehash = svc.check_password(user, body.password)
        user_id = user.id
        active = user.is_active and user.anonymized_at is None

    if not ok:
        fr = await svc.register_failure(user_id, "password")
        if fr.just_locked:
            svc.raise_locked(settings.LOGIN_LOCKOUT_MINUTES * 60)
        raise Unauthorized(*INVALID_CREDENTIALS)
    if not active:
        raise Unauthorized(*INVALID_CREDENTIALS)

    async with independent_session() as s:
        user = (await s.execute(select(User).where(User.id == user_id).with_for_update())).scalar_one()
        if rehash:
            user.hashed_password = svc.hash_password(body.password)
        grants = await load_grants(s, user.id)
        if user.totp_enabled:
            mfa_token, exp = svc.create_mfa_token(user)
            await audit(s, _user_ctx(user), "auth.password_verified_mfa_pending", "user", user.id)
            return {"status": "mfa_required", "mfa_token": mfa_token, "expires_in": exp}
        await svc.reset_failures(s, user)
        if svc.grants_require_mfa(grants):
            setup_token, exp = svc.create_mfa_setup_token(user)
            await audit(s, _user_ctx(user), "auth.login_mfa_setup_required", "user", user.id)
            return {
                "status": "mfa_setup_required",
                "mfa_setup_token": setup_token,
                "expires_in": exp,
                "user": svc.user_public(user),
                "setup": await setup_state(s, user, grants),
            }
        user.last_login = utcnow()
        issued = await svc.create_session(s, user, ip=ip, user_agent=ua, amr=["pwd"])
        await audit(s, _user_ctx(user), "auth.login", "user_session", issued.session.id, after={"amr": ["pwd"]})
        payload = _ok_payload(user, issued)
    set_refresh_cookie(response, issued.refresh_token)
    return payload


@router.post("/mfa/verify")
async def mfa_verify(body: MfaVerifyIn, request: Request, response: Response):
    ip, ua = _meta(request)
    await ensure_redis_available()
    await ratelimit.enforce("mfa_ip", ip, limit=30, window_s=60)
    try:
        claims = tokens.decode(body.mfa_token, expected_typ=tokens.TYP_MFA)
        user_id = uuid.UUID(claims["sub"])
    except (tokens.TokenError, KeyError, ValueError):
        raise Unauthorized("INVALID_MFA_TOKEN", "Sessão de verificação expirada. Entre novamente.") from None
    jti = claims["jti"]
    if await svc.mfa_jti_used(jti):
        raise Unauthorized("INVALID_MFA_TOKEN", "Sessão de verificação expirada. Entre novamente.")
    if await svc.mfa_token_attempt(jti) > 5:
        raise Unauthorized("MFA_TOO_MANY_ATTEMPTS", "Muitas tentativas. Entre novamente.")

    failed = False
    async with independent_session() as s:
        user = (await s.execute(select(User).where(User.id == user_id).with_for_update())).scalar_one_or_none()
        if (
            user is None
            or not user.is_active
            or user.anonymized_at is not None
            or not user.totp_enabled
            or int(claims.get("pwd_ts", -1)) != svc.pwd_ts(user)
        ):
            raise Unauthorized("INVALID_MFA_TOKEN", "Sessão de verificação expirada. Entre novamente.")
        await ensure_master_credential(user, origin="mfa_verify",
                                       error=Unauthorized("INVALID_MFA_TOKEN",
                                                          "Sessão de verificação expirada. Entre novamente."))
        remaining = svc.lock_remaining_s(user)
        if remaining > 0:
            svc.raise_locked(remaining)
        method = await svc.verify_second_factor(s, user, body.code)
        if method is None:
            failed = True
        else:
            fresh = await svc.consume_mfa_jti(jti)  # Redis fora → ServiceUnavailable (503)
            if not fresh:
                raise Unauthorized("INVALID_MFA_TOKEN", "Sessão de verificação expirada. Entre novamente.")
            await svc.reset_failures(s, user)
            user.last_login = utcnow()
            amr = ["pwd", "otp"] if method == "totp" else ["pwd", "otp", "recovery"]
            issued = await svc.create_session(s, user, ip=ip, user_agent=ua, amr=amr)
            await audit(s, _user_ctx(user), "auth.login", "user_session", issued.session.id,
                        after={"amr": amr, "second_factor": method})
            payload = _ok_payload(user, issued)
    if failed:
        fr = await svc.register_failure(user_id, "totp")
        if fr.just_locked:
            svc.raise_locked(settings.LOGIN_LOCKOUT_MINUTES * 60)
        raise Unauthorized("INVALID_MFA_CODE", "Código inválido.")
    set_refresh_cookie(response, issued.refresh_token)
    return payload


# ─── REFRESH / LOGOUT / SESSÕES ──────────────────────────────────────────────

@router.post("/refresh")
async def refresh(request: Request):
    check_csrf(request)
    # fora do try: indisponibilidade NÃO apaga o cookie (a sessão continua válida)
    await ensure_redis_available()
    raw = request.cookies.get(settings.REFRESH_COOKIE_NAME)
    try:
        out = await svc.rotate_refresh(raw)
    except DomainError as exc:
        resp = JSONResponse(status_code=exc.status, content=error_body(exc.code, exc.message, exc.details))
        clear_refresh_cookie(resp)
        return resp
    resp = JSONResponse({"access_token": out.access_token, "token_type": "bearer", "expires_in": out.expires_in})
    set_refresh_cookie(resp, out.refresh_token)
    return resp


async def _optional_ctx(request: Request, db: AsyncSession) -> Optional[AuthContext]:
    if not (request.headers.get("authorization") or "").lower().startswith("bearer "):
        return None
    try:
        return await _resolve(request, db, allowed_typs=(tokens.TYP_ACCESS,))
    except DomainError:
        return None


@router.post("/logout", status_code=204)
async def logout(request: Request, db: AsyncSession = Depends(get_db)):
    check_csrf(request)
    ctx = await _optional_ctx(request, db)
    targets: list[UserSession] = []
    if ctx is not None and ctx.session is not None:
        targets.append(ctx.session)
    cookie_session = await svc.session_from_refresh(db, request.cookies.get(settings.REFRESH_COOKIE_NAME))
    if cookie_session is not None and all(cookie_session.id != t.id for t in targets):
        if ctx is None or cookie_session.user_id == ctx.user_id:
            targets.append(cookie_session)
    revoked = await svc.revoke_sessions(db, targets, "logout")
    for t in targets:
        actor = ctx if ctx is not None else _user_ctx(await db.get(User, t.user_id))
        await audit(db, actor, "auth.logout", "user_session", t.id)
    await svc.after_commit_revocations(revoked)
    resp = Response(status_code=204)
    clear_refresh_cookie(resp)
    return resp


@router.post("/logout-all", status_code=204)
async def logout_all(ctx: AuthContext = Depends(get_auth_context_lenient), db: AsyncSession = Depends(get_db)):
    forbid_impersonation(ctx)
    revoked = await svc.revoke_all_user_sessions(db, ctx.user_id, "logout_all")
    await audit(db, ctx, "auth.logout_all", "user", ctx.user_id, after={"sessions_revoked": len(revoked)})
    await svc.after_commit_revocations(revoked)
    resp = Response(status_code=204)
    clear_refresh_cookie(resp)
    return resp


@router.get("/sessions")
async def list_sessions(ctx: AuthContext = Depends(get_auth_context_lenient), db: AsyncSession = Depends(get_db)):
    forbid_impersonation(ctx)
    now = utcnow()
    rows = (
        await db.execute(
            select(UserSession)
            .where(
                UserSession.user_id == ctx.user_id,
                UserSession.revoked_at.is_(None),
                UserSession.expires_at > now,
                UserSession.impersonator_user_id.is_(None),
            )
            .order_by(UserSession.created_at.desc())
        )
    ).scalars().all()
    return {
        "items": [
            {
                "id": str(s.id),
                "created_at": s.created_at,
                "last_seen_at": s.last_seen_at,
                "ip": s.ip_address,
                "user_agent": s.user_agent,
                "device_label": s.device_label,
                "current": s.id == ctx.session_id,
            }
            for s in rows
        ]
    }


@router.delete("/sessions/{session_id}", status_code=204)
async def revoke_session(
    session_id: uuid.UUID, ctx: AuthContext = Depends(get_auth_context_lenient), db: AsyncSession = Depends(get_db)
):
    forbid_impersonation(ctx)
    s = await db.get(UserSession, session_id)
    if s is None or s.user_id != ctx.user_id or s.impersonator_user_id is not None:
        raise NotFound()
    revoked = await svc.revoke_sessions(db, [s], "user_revoked")
    await audit(db, ctx, "auth.session_revoked", "user_session", s.id)
    await svc.after_commit_revocations(revoked)
    return Response(status_code=204)


# ─── SENHA ───────────────────────────────────────────────────────────────────

RESET_CODE_TTL = timedelta(minutes=15)
RESET_MAX_ATTEMPTS = 5


def _reset_hash(user_id: uuid.UUID, code: str) -> str:
    return hmac_hex(f"{user_id}:{code}", purpose="password_reset")


@router.post("/password/forgot", status_code=202)
async def password_forgot(body: ForgotIn, request: Request, background: BackgroundTasks):
    ip, _ = _meta(request)
    email = svc.normalize_email(body.email)
    await ensure_redis_available()
    await ratelimit.enforce("forgot_ip", ip, limit=10, window_s=900)
    allowed, _ = await ratelimit.hit("forgot_email", email, limit=3, window_s=900)
    code = f"{secrets.randbelow(10**6):06d}"
    async with independent_session() as s:
        user = (await s.execute(select(User).where(User.email == email))).scalar_one_or_none()
        if user is not None and user.is_active and user.anonymized_at is None and allowed:
            now = utcnow()
            # invalida códigos anteriores ainda válidos
            for old in (
                await s.execute(
                    select(PasswordResetCode).where(
                        PasswordResetCode.user_id == user.id, PasswordResetCode.used_at.is_(None)
                    )
                )
            ).scalars():
                old.used_at = now
            s.add(
                PasswordResetCode(
                    user_id=user.id, code_hash=_reset_hash(user.id, code), expires_at=now + RESET_CODE_TTL,
                    request_ip=ip[:64],
                )
            )
            await audit(s, None, "auth.password_reset_requested", "user", user.id, origin="api")
            background.add_task(
                send_safely,
                OutgoingEmail(
                    to=user.email,
                    subject="RD Mídia Indoor — código para redefinir sua senha",
                    text=(
                        f"Olá, {user.full_name}.\n\nSeu código para redefinir a senha é: {code}\n"
                        "Ele vale por 15 minutos e só pode ser usado uma vez.\n\n"
                        "Se você não pediu, ignore este e-mail — sua senha continua a mesma."
                    ),
                    tags={"kind": "password_reset"},
                ),
            )
        else:
            _reset_hash(uuid.uuid4(), code)  # trabalho equivalente (tempo aproximadamente constante)
    return {"status": "accepted"}


@router.post("/password/reset", status_code=204)
async def password_reset(body: ResetIn, request: Request):
    ip, _ = _meta(request)
    await ensure_redis_available()
    await ratelimit.enforce("reset_ip", ip, limit=20, window_s=900)
    email = svc.normalize_email(body.email)
    invalid = DomainError("INVALID_RESET_CODE", "Código inválido ou expirado.", status=400)
    revoked: list = []
    failed = False
    async with independent_session() as s:
        user = (await s.execute(select(User).where(User.email == email).with_for_update())).scalar_one_or_none()
        if user is None or not user.is_active or user.anonymized_at is not None:
            raise invalid
        await ensure_master_credential(user, origin="password_reset", error=invalid)  # D1
        now = utcnow()
        rc = (
            await s.execute(
                select(PasswordResetCode)
                .where(
                    PasswordResetCode.user_id == user.id,
                    PasswordResetCode.used_at.is_(None),
                    PasswordResetCode.expires_at > now,
                )
                .order_by(PasswordResetCode.created_at.desc())
                .limit(1)
                .with_for_update()
            )
        ).scalar_one_or_none()
        if rc is None:
            raise invalid
        if rc.code_hash != _reset_hash(user.id, body.code):
            rc.attempts += 1
            if rc.attempts >= RESET_MAX_ATTEMPTS:
                rc.used_at = now  # invalida após tentativas demais
            failed = True
        else:
            validate_password_policy(body.new_password, user.email)
            rc.used_at = now
            await svc.apply_password(s, user, body.new_password)
            await svc.reset_failures(s, user)
            revoked = await svc.revoke_all_user_sessions(s, user.id, "password_reset")
            await audit(s, _user_ctx(user), "auth.password_reset", "user", user.id,
                        after={"sessions_revoked": len(revoked)})
    if failed:
        raise invalid
    await svc.after_commit_revocations(revoked)
    return Response(status_code=204)


@router.post("/password/change", status_code=204)
async def password_change(
    body: ChangePasswordIn,
    request: Request,
    ctx: AuthContext = Depends(get_auth_context_lenient),
    db: AsyncSession = Depends(get_db),
):
    forbid_impersonation(ctx)
    user = (await db.execute(select(User).where(User.id == ctx.user_id).with_for_update())).scalar_one()
    ok, _ = svc.check_password(user, body.current_password)
    if not ok:
        await _count_failure(db, ctx.user_id, "password_change")
        raise DomainError("INVALID_PASSWORD", "Senha atual incorreta.", status=400)
    validate_password_policy(body.new_password, user.email)
    same, _ = svc.check_password(user, body.new_password)
    if same:
        raise DomainError("PASSWORD_REUSED", "A nova senha deve ser diferente da atual.", status=400)
    await svc.apply_password(db, user, body.new_password)
    revoked = await svc.revoke_all_user_sessions(db, user.id, "password_changed", except_session_id=ctx.session_id)
    await audit(db, ctx, "auth.password_changed", "user", user.id, after={"other_sessions_revoked": len(revoked)})
    await svc.after_commit_revocations(revoked)
    return Response(status_code=204)


# ─── TOTP ────────────────────────────────────────────────────────────────────

@router.post("/mfa/totp/setup")
async def totp_setup(body: TotpSetupIn, ctx: AuthContext = Depends(get_mfa_setup_context),
                     db: AsyncSession = Depends(get_db)):
    forbid_impersonation(ctx)
    user = (await db.execute(select(User).where(User.id == ctx.user_id).with_for_update())).scalar_one()
    if svc.lock_remaining_s(user) > 0:
        svc.raise_locked(svc.lock_remaining_s(user))
    ok, _ = svc.check_password(user, body.password)
    if not ok:
        await _count_failure(db, ctx.user_id, "mfa_setup_password")
        raise DomainError("INVALID_PASSWORD", "Senha incorreta.", status=400)
    if user.totp_enabled:
        raise Conflict("MFA_ALREADY_ENABLED", "A verificação em duas etapas já está ativa.")
    secret = totp.new_secret()
    user.totp_pending_secret_enc = svc.encrypt_totp_secret(user.id, secret)
    await audit(db, ctx, "auth.mfa_setup_started", "user", user.id)
    return {"otpauth_uri": totp.otpauth_uri(secret, user.email), "secret": secret}


@router.post("/mfa/totp/enable")
async def totp_enable(body: TotpEnableIn, request: Request, ctx: AuthContext = Depends(get_mfa_setup_context),
                      db: AsyncSession = Depends(get_db)):
    forbid_impersonation(ctx)
    ip, ua = _meta(request)
    user = (await db.execute(select(User).where(User.id == ctx.user_id).with_for_update())).scalar_one()
    if user.totp_enabled:
        raise Conflict("MFA_ALREADY_ENABLED", "A verificação em duas etapas já está ativa.")
    if svc.lock_remaining_s(user) > 0:
        svc.raise_locked(svc.lock_remaining_s(user))
    if not user.totp_pending_secret_enc:
        raise DomainError("MFA_SETUP_NOT_STARTED", "Inicie a configuração primeiro.", status=400)
    secret = svc.decrypt_totp_secret(user.id, user.totp_pending_secret_enc)
    step = totp.match_step(secret, body.code)
    if step is None:
        await _count_failure(db, ctx.user_id, "mfa_enable")
        raise DomainError("INVALID_MFA_CODE", "Código inválido.", status=400)
    user.totp_secret_enc = user.totp_pending_secret_enc
    user.totp_pending_secret_enc = None
    user.totp_enabled = True
    user.totp_enabled_at = utcnow()
    user.totp_last_step = step
    codes = await svc.replace_recovery_codes(db, user)
    await audit(db, ctx, "auth.mfa_enabled", "user", user.id)
    result: dict = {"recovery_codes": codes}
    if ctx.token_typ == tokens.TYP_MFA_SETUP:
        # concluiu o 2FA obrigatório: emite a sessão completa (senha + TOTP comprovados)
        await svc.reset_failures(db, user)
        user.last_login = utcnow()
        issued = await svc.create_session(db, user, ip=ip, user_agent=ua, amr=["pwd", "otp"])
        await audit(db, ctx, "auth.login", "user_session", issued.session.id, after={"amr": ["pwd", "otp"]})
        result.update(_ok_payload(user, issued))
        resp = JSONResponse(jsonable(result))
        set_refresh_cookie(resp, issued.refresh_token)
        return resp
    return result


def jsonable(v):
    from fastapi.encoders import jsonable_encoder

    return jsonable_encoder(v)


@router.post("/mfa/totp/disable", status_code=204)
async def totp_disable(body: TotpDisableIn, ctx: AuthContext = Depends(get_auth_context_lenient),
                       db: AsyncSession = Depends(get_db)):
    forbid_impersonation(ctx)
    if ctx.mfa_required:
        raise DomainError("MFA_REQUIRED_FOR_ROLE", "Seu perfil exige verificação em duas etapas.", status=403)
    user = (await db.execute(select(User).where(User.id == ctx.user_id).with_for_update())).scalar_one()
    if not user.totp_enabled:
        raise DomainError("MFA_NOT_ENABLED", "A verificação em duas etapas não está ativa.", status=400)
    ok, _ = svc.check_password(user, body.password)
    method = await svc.verify_second_factor(db, user, body.code, allow_recovery=False) if ok else None
    if not ok or method is None:
        await _count_failure(db, ctx.user_id, "mfa_disable")
        raise DomainError("INVALID_CREDENTIALS", "Senha ou código inválido.", status=400)
    from sqlalchemy import delete

    from app.models.users import UserRecoveryCode

    user.totp_enabled = False
    user.totp_secret_enc = None
    user.totp_pending_secret_enc = None
    user.totp_enabled_at = None
    await db.execute(delete(UserRecoveryCode).where(UserRecoveryCode.user_id == user.id))
    await audit(db, ctx, "auth.mfa_disabled", "user", user.id)
    return Response(status_code=204)


# ─── PRIMEIRO ACESSO (AÇÃO 003 — ADR_011 D6; SPEC §3, §5.1) ────────────────────
# Ordem imposta no servidor: PASSWORD_CHANGE → TOTP → FINANCIAL_PASSWORD (guard em authz._check_unlocked).

class SetupPasswordIn(BaseModel):
    current_password: str = Field(min_length=1, max_length=256)
    new_password: str = Field(min_length=1, max_length=256)


class SetupFinancialPasswordIn(BaseModel):
    login_password: str = Field(min_length=1, max_length=256)
    totp_code: str = Field(min_length=6, max_length=6, pattern=r"^\d{6}$")
    financial_password: str = Field(min_length=1, max_length=256)


@router.get("/setup/status")
async def setup_status(ctx: AuthContext = Depends(get_setup_status_context), db: AsyncSession = Depends(get_db)):
    """``SetupState`` do usuário (token ``mfa_setup`` ou access token). Nunca contém segredo."""
    return await setup_state(db, ctx.user, ctx.grants)


@router.post("/setup/password")
async def setup_password(body: SetupPasswordIn, ctx: AuthContext = Depends(get_setup_status_context),
                         db: AsyncSession = Depends(get_db)):
    """Passo 1 do primeiro acesso: troca obrigatória de senha.

    Com token de setup devolve um NOVO token de setup (o anterior deixa de valer: vínculo ao hash da senha);
    com access token mantém a sessão atual e revoga as demais.
    """
    forbid_impersonation(ctx)
    user = (await db.execute(select(User).where(User.id == ctx.user_id).with_for_update())).scalar_one()
    if not user.must_change_password:
        raise Conflict("PASSWORD_CHANGE_NOT_REQUIRED", "Troca de senha não pendente. Use Minha conta → Segurança.")
    remaining = svc.lock_remaining_s(user)
    if remaining > 0:
        svc.raise_locked(remaining)
    ok, _ = svc.check_password(user, body.current_password)
    if not ok:
        await _count_failure(db, ctx.user_id, "setup_password")
        raise DomainError("INVALID_PASSWORD", "Senha atual incorreta.", status=400)
    validate_password_policy(body.new_password, user.email)
    same, _ = svc.check_password(user, body.new_password)
    if same:
        raise DomainError("PASSWORD_REUSED", "A nova senha deve ser diferente da atual.", status=400)
    await svc.apply_password(db, user, body.new_password)
    setup_token = ctx.token_typ == tokens.TYP_MFA_SETUP
    revoked = await svc.revoke_all_user_sessions(
        db, user.id, "password_changed", except_session_id=None if setup_token else ctx.session_id
    )
    await audit(db, ctx, "auth.password_changed", "user", user.id,
                after={"setup": True, "other_sessions_revoked": len(revoked)})
    await svc.after_commit_revocations(revoked)
    state = await setup_state(db, user, ctx.grants)
    if setup_token:
        tok, exp = svc.create_mfa_setup_token(user)
        return {"status": "mfa_setup_required", "mfa_setup_token": tok, "expires_in": exp, "setup": state}
    return {"status": "ok", "setup": state}


@router.post("/setup/financial-password")
async def setup_financial_password(body: SetupFinancialPasswordIn,
                                   ctx: AuthContext = Depends(get_setup_context("FINANCIAL_PASSWORD")),
                                   db: AsyncSession = Depends(get_db)):
    """Passo 3 (titular de assento): cria a PRÓPRIA senha financeira (senha de login + TOTP).

    Delega a ``app.modules.financial.security.set_financial_password`` (assinatura congelada)."""
    from app.modules.financial.security import set_financial_password
    from app.modules.security.notifications import status_changed_for_masters

    await set_financial_password(db, ctx, login_password=body.login_password, totp_code=body.totp_code,
                                 new_password=body.financial_password)
    await status_changed_for_masters(db, reason="financial_password_set", extra_user_ids=[ctx.user_id])
    return {"status": "ok", "setup": await setup_state(db, ctx.user, ctx.grants)}


# ─── ATIVAÇÃO DO 2º ADMIN MASTER (público; token de uso único do e-mail) ─────

class MasterActivationTokenIn(BaseModel):
    token: str = Field(min_length=20, max_length=256)


class MasterActivationCompleteIn(BaseModel):
    token: str = Field(min_length=20, max_length=256)
    password: str = Field(min_length=1, max_length=256)
    totp_code: str = Field(min_length=6, max_length=6, pattern=r"^\d{6}$")
    financial_password: str = Field(min_length=1, max_length=256)


async def _activation_rate_limit(request: Request) -> None:
    await ensure_redis_available()
    await ratelimit.enforce("master_activation_ip", client_ip(request), limit=20, window_s=900)


@router.post("/master-activation/inspect")
async def master_activation_inspect(body: MasterActivationTokenIn, request: Request,
                                    db: AsyncSession = Depends(get_db)):
    from app.modules.security import invites

    await _activation_rate_limit(request)
    return await invites.inspect(db, body.token)


@router.post("/master-activation/totp")
async def master_activation_totp(body: MasterActivationTokenIn, request: Request,
                                 db: AsyncSession = Depends(get_db)):
    """Gera (e substitui) o segredo TOTP pendente do convite. O QR é gerado localmente no frontend."""
    from app.modules.security import invites

    await _activation_rate_limit(request)
    from sqlalchemy.exc import DBAPIError

    from app.modules.security.approval_mode import map_db_error

    try:
        return await invites.start_totp(db, body.token)
    except DBAPIError as exc:  # D2: lock_timeout/deadlock ⇒ 409 com nova tentativa (nunca 500)
        mapped = map_db_error(exc)
        if mapped is not None:
            raise mapped from None
        raise


@router.post("/master-activation/complete")
async def master_activation_complete(body: MasterActivationCompleteIn, request: Request):
    """Ativação ATÔMICA do 2º Master: senha + TOTP + senha financeira do próprio convidado → assento + sessão."""
    from app.modules.security import invites

    ip, ua = _meta(request)
    await _activation_rate_limit(request)
    payload, refresh_raw = await invites.complete_activation(
        raw_token=body.token, password=body.password, totp_code=body.totp_code,
        financial_password=body.financial_password, ip=ip, user_agent=ua,
    )
    resp = JSONResponse(jsonable(payload))
    set_refresh_cookie(resp, refresh_raw)
    return resp


# ─── CONVITES ────────────────────────────────────────────────────────────────

INVITE_TTL = timedelta(days=7)


async def _role_with_perms(db: AsyncSession, slug: str) -> tuple[Role, frozenset[str]]:
    role = (await db.execute(select(Role).where(Role.slug == slug))).scalar_one_or_none()
    if role is None:
        raise DomainError("ROLE_NOT_FOUND", "Perfil inexistente.", status=400)
    perms = frozenset(
        c
        for (c,) in (
            await db.execute(
                select(Permission.code)
                .join(RolePermission, RolePermission.permission_id == Permission.id)
                .where(RolePermission.role_id == role.id)
            )
        ).all()
    )
    return role, perms


def scope_grant(role: Role, perms: frozenset[str], scope_type: str, scope_id, scope_value) -> Grant:
    if scope_type not in SCOPE_TYPES:
        raise DomainError("SCOPE_INVALID", "Tipo de escopo inválido.", status=400)
    if scope_type == SCOPE_GLOBAL and (scope_id or scope_value):
        raise DomainError("SCOPE_INVALID", "Escopo GLOBAL não aceita identificador.", status=400)
    if scope_type in (SCOPE_ORGANIZATION, SCOPE_ESTABLISHMENT) and (scope_id is None or scope_value):
        raise DomainError("SCOPE_INVALID", "Informe scope_id para este escopo.", status=400)
    if scope_type == SCOPE_CITY:
        scope_value = normalize_city_key(scope_value)
        if not scope_value or scope_id:
            raise DomainError("SCOPE_INVALID", "Informe scope_value (UF:cidade ou código IBGE).", status=400)
    return Grant(
        assignment_id=uuid.uuid4(), role_id=role.id, role_slug=role.slug, role_mfa_required=role.mfa_required,
        scope_type=scope_type, scope_id=scope_id, scope_value=scope_value, permissions=perms,
    )


async def validate_scope_target(db: AsyncSession, g: Grant) -> None:
    """O alvo do escopo precisa existir (organização / estabelecimento)."""
    if g.scope_type == SCOPE_ORGANIZATION:
        from app.models.organizations import Organization

        if await db.get(Organization, g.scope_id) is None:
            raise NotFound()
    elif g.scope_type == SCOPE_ESTABLISHMENT:
        from app.models.establishments import Establishment

        if await db.get(Establishment, g.scope_id) is None:
            raise NotFound()


@router.post("/invites", status_code=201)
async def create_invite(
    body: InviteIn,
    background: BackgroundTasks,
    ctx: AuthContext = Depends(require("users.manage", allow_impersonation=False)),
    db: AsyncSession = Depends(get_db),
):
    email = svc.normalize_email(body.email)
    if not svc._EMAIL_RE.match(email):
        raise DomainError("EMAIL_INVALID", "E-mail inválido.", status=400)
    role, perms = await _role_with_perms(db, body.role)
    if role_reserved(role.slug):
        raise Forbidden("ROLE_RESERVED_FOR_ADMIN_MASTER", "O perfil Super Admin é exclusivo dos Administradores Master.")
    g = scope_grant(role, perms, body.scope_type, body.scope_id, body.scope_value)
    if not can_grant_role_in_scope(ctx, role.slug, perms, g):
        # escopo fora do alcance do ator → 404 (não vazar); perfil acima do ator → 403
        from app.core.authz import covers_grant

        if not covers_grant(ctx, "users.manage", g):
            raise NotFound()
        raise Forbidden("ROLE_ESCALATION", "Você não pode conceder este perfil.")
    await validate_scope_target(db, g)
    existing = (await db.execute(select(User).where(User.email == email))).scalar_one_or_none()
    if existing is not None and existing.is_admin_master:
        raise Forbidden("ADMIN_MASTER_PROTECTED", "Operação não permitida para Admin Master.")
    raw = secrets.token_urlsafe(32)
    inv = UserInvite(
        email=email, token_hash=sha256_hex(raw), invited_by=ctx.user_id, role_id=role.id,
        scope_type=g.scope_type, scope_id=g.scope_id, scope_value=g.scope_value,
        expires_at=utcnow() + INVITE_TTL,
    )
    db.add(inv)
    await db.flush()
    await audit(db, ctx, "auth.invite_created", "user_invite", inv.id,
                after={"email": email, "role": role.slug, "scope_type": g.scope_type,
                       "scope_id": g.scope_id, "scope_value": g.scope_value},
                org_id=g.scope_id if g.scope_type == SCOPE_ORGANIZATION else None)
    link = f"{settings.FRONTEND_URL.rstrip('/')}/convite?token={raw}"
    background.add_task(
        send_safely,
        OutgoingEmail(
            to=email,
            subject="RD Mídia Indoor — você foi convidado",
            text=(
                f"Você foi convidado para acessar a RD Mídia Indoor com o perfil {role.name}.\n\n"
                f"Acesse: {link}\n\nO convite vale por 7 dias."
            ),
            tags={"kind": "invite"},
        ),
    )
    return {"id": str(inv.id), "email": email, "role": role.slug, "scope_type": g.scope_type,
            "scope_id": g.scope_id, "scope_value": g.scope_value, "expires_at": inv.expires_at}


@router.get("/invites")
async def list_invites(ctx: AuthContext = Depends(require("users.manage")), db: AsyncSession = Depends(get_db)):
    rows = (
        await db.execute(
            select(UserInvite, Role.slug).join(Role, Role.id == UserInvite.role_id)
            .where(UserInvite.accepted_at.is_(None), UserInvite.revoked_at.is_(None))
            .order_by(UserInvite.created_at.desc()).limit(200)
        )
    ).all()
    items = []
    for inv, slug in rows:
        g = Grant(inv.id, inv.role_id, slug, False, inv.scope_type, inv.scope_id, inv.scope_value, frozenset())
        from app.core.authz import covers_grant

        if covers_grant(ctx, "users.manage", g):
            items.append({"id": str(inv.id), "email": inv.email, "role": slug, "scope_type": inv.scope_type,
                          "scope_id": inv.scope_id, "scope_value": inv.scope_value, "expires_at": inv.expires_at,
                          "created_at": inv.created_at})
    return {"items": items}


@router.delete("/invites/{invite_id}", status_code=204)
async def revoke_invite(invite_id: uuid.UUID, ctx: AuthContext = Depends(require("users.manage", allow_impersonation=False)),
                        db: AsyncSession = Depends(get_db)):
    from app.core.authz import covers_grant

    inv = await db.get(UserInvite, invite_id)
    if inv is None or inv.accepted_at is not None or inv.revoked_at is not None:
        raise NotFound()
    g = Grant(inv.id, inv.role_id, "", False, inv.scope_type, inv.scope_id, inv.scope_value, frozenset())
    if not covers_grant(ctx, "users.manage", g):
        raise NotFound()
    inv.revoked_at = utcnow()
    await audit(db, ctx, "auth.invite_revoked", "user_invite", inv.id)
    return Response(status_code=204)


@router.post("/invites/accept")
async def accept_invite(body: InviteAcceptIn, request: Request, response: Response):
    ip, ua = _meta(request)
    await ensure_redis_available()
    await ratelimit.enforce("invite_ip", ip, limit=20, window_s=900)
    invalid = DomainError("INVITE_INVALID", "Convite inválido ou expirado.", status=400)
    async with independent_session() as s:
        inv = (
            await s.execute(select(UserInvite).where(UserInvite.token_hash == sha256_hex(body.token)).with_for_update())
        ).scalar_one_or_none()
        now = utcnow()
        if inv is None or inv.accepted_at is not None or inv.revoked_at is not None or inv.expires_at <= now:
            raise invalid
        inv_role = await s.get(Role, inv.role_id)
        if inv_role is None or role_reserved(inv_role.slug):  # convite antigo de super_admin não vale (D9)
            raise invalid
        user = (await s.execute(select(User).where(User.email == inv.email).with_for_update())).scalar_one_or_none()
        if user is not None:
            # conta existente: precisa provar a senha atual para vincular o novo perfil
            ok, _ = svc.check_password(user, body.password)
            if not ok or not user.is_active or user.anonymized_at is not None:
                raise invalid
        else:
            validate_password_policy(body.password, inv.email)
            user = User(
                id=uuid.uuid4(), email=inv.email, full_name=body.name.strip(), hashed_password=svc.hash_password(body.password),
                is_active=True, is_verified=True, password_changed_at=now,
            )
            s.add(user)
            await s.flush()
        exists = (
            await s.execute(
                select(RoleAssignment).where(
                    RoleAssignment.user_id == user.id, RoleAssignment.role_id == inv.role_id,
                    RoleAssignment.scope_type == inv.scope_type,
                    RoleAssignment.scope_id.is_not_distinct_from(inv.scope_id),
                    RoleAssignment.scope_value.is_not_distinct_from(inv.scope_value),
                )
            )
        ).scalar_one_or_none()
        if exists is None:
            s.add(RoleAssignment(user_id=user.id, role_id=inv.role_id, scope_type=inv.scope_type,
                                 scope_id=inv.scope_id, scope_value=inv.scope_value, granted_by=inv.invited_by))
        inv.accepted_at = now
        inv.accepted_user_id = user.id
        await s.flush()
        await audit(s, _user_ctx(user), "auth.invite_accepted", "user_invite", inv.id,
                    after={"user_id": user.id, "role_id": inv.role_id, "scope_type": inv.scope_type})
        grants = await load_grants(s, user.id)
        if user.totp_enabled:
            mfa_token, exp = svc.create_mfa_token(user)
            return {"status": "mfa_required", "mfa_token": mfa_token, "expires_in": exp}
        if svc.grants_require_mfa(grants):
            setup_token, exp = svc.create_mfa_setup_token(user)
            return {"status": "mfa_setup_required", "mfa_setup_token": setup_token, "expires_in": exp,
                    "user": svc.user_public(user), "setup": await setup_state(s, user, grants)}
        user.last_login = now
        issued = await svc.create_session(s, user, ip=ip, user_agent=ua, amr=["pwd"])
        await audit(s, _user_ctx(user), "auth.login", "user_session", issued.session.id, after={"via": "invite"})
        payload = _ok_payload(user, issued)
    set_refresh_cookie(response, issued.refresh_token)
    return payload


# ─── IMPERSONATION (§2.1) ────────────────────────────────────────────────────

PRIVILEGED_ROLES = frozenset({"super_admin", "admin", "financial"})


@router.post("/impersonation")
async def start_impersonation(
    body: ImpersonationIn,
    request: Request,
    ctx: AuthContext = Depends(require("users.impersonate", allow_impersonation=False)),
    db: AsyncSession = Depends(get_db),
):
    ip, ua = _meta(request)
    target = await db.get(User, body.user_id)
    if target is None or target.anonymized_at is not None:
        raise NotFound()
    if target.id == ctx.user_id:
        raise BadRequest("IMPERSONATION_SELF", "Não é possível personificar a si mesmo.")
    tgrants = await load_grants(db, target.id)
    if target.is_admin_master or any(g.role_slug in PRIVILEGED_ROLES for g in tgrants):
        raise Forbidden("IMPERSONATION_FORBIDDEN_TARGET", "Não é permitido personificar administradores.")
    if not target.is_active:
        raise BadRequest("USER_INACTIVE", "Usuário inativo.")
    if not any(ctx.can("users.impersonate", **_grant_scope_kwargs(g)) for g in tgrants) and not ctx.can(
        "users.impersonate", global_only=True
    ):
        raise NotFound()
    reason = body.reason.strip()
    if len(reason) < 10:
        raise BadRequest("REASON_TOO_SHORT", "Informe um motivo com pelo menos 10 caracteres.")
    real = ctx.user
    issued = await svc.create_session(
        db, target, ip=ip, user_agent=ua, amr=["imp"], impersonator=real, parent_session_id=ctx.session_id,
        reason=reason,
    )
    await audit(db, ctx, "auth.impersonation_started", "user", target.id, reason=reason,
                after={"impersonation_session_id": issued.session.id, "target_user_id": target.id})
    return {
        "access_token": issued.access_token,
        "token_type": "bearer",
        "expires_in": issued.expires_in,
        "impersonation": {
            "active": True,
            "session_id": str(issued.session.id),
            "real_user": {"id": str(real.id), "name": real.full_name},
            "user": svc.user_public(target),
            "reason": reason,
        },
    }


def _grant_scope_kwargs(g: Grant) -> dict:
    if g.scope_type == SCOPE_ORGANIZATION:
        return {"org_id": g.scope_id}
    if g.scope_type == SCOPE_ESTABLISHMENT:
        return {"establishment_id": g.scope_id}
    if g.scope_type == SCOPE_CITY:
        return {"city": g.scope_value}
    return {"global_only": True}


@router.delete("/impersonation", status_code=204)
async def end_impersonation(ctx: AuthContext = Depends(get_auth_context_lenient), db: AsyncSession = Depends(get_db)):
    if ctx.impersonation is None:
        raise BadRequest("NOT_IMPERSONATING", "Nenhuma impersonation ativa.")
    revoked = await svc.revoke_sessions(db, [ctx.session], "impersonation_ended")
    await audit(db, ctx, "auth.impersonation_ended", "user", ctx.user_id,
                after={"impersonation_session_id": ctx.session_id})
    await svc.after_commit_revocations(revoked)
    return Response(status_code=204)
