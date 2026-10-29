"""
RD Mídia Indoor — Serviço de autenticação (contrato §2).

Toda lógica com efeitos de segurança que precisam persistir mesmo quando a resposta
é erro (contador de falhas, lockout, detecção de reuso de refresh, auditoria de
falhas) roda em ``independent_session()`` — commit próprio, imune ao rollback da
request (corrige A2 E1 / A7 C4).
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import re
import secrets
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Iterable, Optional

import structlog
from sqlalchemy import or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import tokens, totp
from app.core.audit import audit
from app.core.authz import AuthContext, Grant, load_grants, mark_sessions_revoked_in_cache
from app.core.config import settings
from app.core.crypto import decrypt_str, encrypt_str, hmac_hex, sha256_hex
from app.core.database import independent_session
from app.core.errors import DomainError, Locked, Unauthorized
from app.core.ids import random_safe
from app.core.passwords import hash_password, verify_password
from app.core.permissions import MFA_REQUIRED_ROLES
from app.core.errors import ServiceUnavailable
from app.core.redis import get_redis, mark_redis_down, op_timeout_s, rkey
from app.core.timeutils import utcnow
from app.models.users import RefreshToken, User, UserRecoveryCode, UserSession

log = structlog.get_logger("rd.auth")

RECOVERY_CODES_COUNT = 10


# ─── Normalização de login ───────────────────────────────────────────────────

_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


def normalize_email(email: str) -> str:
    return (email or "").strip().lower()


def normalize_phone(raw: Optional[str]) -> Optional[str]:
    """Somente dígitos com DDI. 10/11 dígitos sem DDI → Brasil (55)."""
    if raw is None:
        return None
    digits = re.sub(r"\D", "", raw)
    if not digits:
        return None
    if len(digits) in (10, 11):
        digits = "55" + digits
    if not (12 <= len(digits) <= 15):
        raise DomainError("PHONE_INVALID", "Telefone inválido.", status=400)
    return digits


def login_identifier(login: str) -> tuple[str, str]:
    login = (login or "").strip()
    if "@" in login:
        return "email", normalize_email(login)
    try:
        phone = normalize_phone(login)
    except DomainError:
        phone = None
    return "phone", phone or ""


async def find_user_by_login(db: AsyncSession, login: str, *, for_update: bool = False) -> Optional[User]:
    kind, value = login_identifier(login)
    if not value:
        return None
    q = select(User).where(User.email == value) if kind == "email" else select(User).where(User.phone == value)
    if for_update:
        q = q.with_for_update()
    return (await db.execute(q)).scalar_one_or_none()


# ─── Perfis / MFA ────────────────────────────────────────────────────────────

def grants_require_mfa(grants: Iterable[Grant]) -> bool:
    return any(g.role_mfa_required or g.role_slug in MFA_REQUIRED_ROLES for g in grants)


def user_public(user: User) -> dict:
    return {
        "id": str(user.id),
        "name": user.full_name,
        "email": user.email,
        "phone": user.phone,
        "mfa_enabled": bool(user.totp_enabled),
        "is_admin_master": bool(user.is_admin_master),
        "theme": user.theme,
        "must_change_password": bool(user.must_change_password),
    }


# ─── Lockout ─────────────────────────────────────────────────────────────────

@dataclass
class FailureResult:
    attempts: int
    locked_until: Optional[datetime]
    just_locked: bool


def lock_remaining_s(user: User) -> int:
    if user.locked_until is None:
        return 0
    rem = (user.locked_until - utcnow()).total_seconds()
    return max(int(rem) + 1, 0) if rem > 0 else 0


def raise_locked(retry_after_s: int) -> None:
    raise Locked(
        "ACCOUNT_LOCKED",
        "Conta bloqueada temporariamente por excesso de tentativas. Tente novamente mais tarde.",
        details={"retry_after_s": retry_after_s},
        headers={"Retry-After": str(retry_after_s)},
    )


async def register_failure(user_id: uuid.UUID, kind: str) -> FailureResult:
    """Incrementa falhas (senha OU 2º fator) e bloqueia ao atingir o limite — transação própria."""
    async with independent_session() as s:
        u = (await s.execute(select(User).where(User.id == user_id).with_for_update())).scalar_one()
        now = utcnow()
        attempts = u.failed_login_attempts or 0
        if u.locked_until is not None and u.locked_until <= now:
            attempts = 0  # janela anterior expirou → recomeça a contagem
            u.locked_until = None
        attempts += 1
        u.failed_login_attempts = attempts
        just_locked = False
        if attempts >= settings.LOGIN_MAX_FAILED_ATTEMPTS and (u.locked_until is None or u.locked_until <= now):
            u.locked_until = now + timedelta(minutes=settings.LOGIN_LOCKOUT_MINUTES)
            just_locked = True
        await audit(s, None, "auth.login_failed", "user", u.id, after={"kind": kind, "attempts": attempts},
                    actor_user_id=None, origin="api")
        if just_locked:
            await audit(s, None, "auth.account_locked", "user", u.id,
                        after={"locked_until": u.locked_until, "attempts": attempts}, origin="api")
        return FailureResult(attempts, u.locked_until, just_locked)


async def reset_failures(db: AsyncSession, user: User) -> None:
    user.failed_login_attempts = 0
    user.locked_until = None


# Logins inexistentes: contador em Redis para o mesmo comportamento observável
# (401 → 423) sem revelar se o e-mail existe.
async def register_unknown_failure(identifier: str) -> Optional[int]:
    key = rkey("login", "unknown", hashlib.sha256(identifier.encode()).hexdigest()[:32])
    window = settings.LOGIN_LOCKOUT_MINUTES * 60
    try:
        r = await get_redis()
        pipe = r.pipeline()
        pipe.incr(key)
        pipe.expire(key, window, nx=True)
        pipe.ttl(key)
        count, _, ttl = await asyncio.wait_for(pipe.execute(), timeout=op_timeout_s())
    except Exception as exc:
        mark_redis_down()
        log.error("unknown_login_counter_error", error_type=type(exc).__name__)
        raise ServiceUnavailable() from None
    if int(count) >= settings.LOGIN_MAX_FAILED_ATTEMPTS:
        return max(int(ttl), 1)
    return None


async def unknown_locked(identifier: str) -> Optional[int]:
    key = rkey("login", "unknown", hashlib.sha256(identifier.encode()).hexdigest()[:32])
    try:
        r = await get_redis()
        count = await asyncio.wait_for(r.get(key), timeout=op_timeout_s())
        if count and int(count) >= settings.LOGIN_MAX_FAILED_ATTEMPTS:
            return max(int(await asyncio.wait_for(r.ttl(key), timeout=op_timeout_s())), 1)
    except Exception as exc:
        mark_redis_down()
        log.error("unknown_login_counter_error", error_type=type(exc).__name__)
        raise ServiceUnavailable() from None
    return None


# ─── Sessões e tokens ───────────────────────────────────────────────────────

def device_label_from_ua(ua: Optional[str]) -> Optional[str]:
    if not ua:
        return None
    u = ua.lower()
    browser = next(
        (n for k, n in (("edg/", "Edge"), ("opr/", "Opera"), ("chrome/", "Chrome"), ("firefox/", "Firefox"),
                        ("safari/", "Safari")) if k in u),
        "Navegador",
    )
    os_ = next(
        (n for k, n in (("windows", "Windows"), ("android", "Android"), ("iphone", "iOS"), ("ipad", "iOS"),
                        ("mac os", "macOS"), ("linux", "Linux")) if k in u),
        "",
    )
    return f"{browser} em {os_}" if os_ else browser


@dataclass
class IssuedSession:
    session: UserSession
    access_token: str
    expires_in: int
    refresh_token: Optional[str]


def _new_refresh_row(session: UserSession) -> tuple[RefreshToken, str]:
    raw = secrets.token_urlsafe(48)
    now = utcnow()
    expires = min(now + timedelta(days=settings.REFRESH_TOKEN_EXPIRE_DAYS), session.expires_at)
    return RefreshToken(id=uuid.uuid4(), session_id=session.id, token_hash=sha256_hex(raw), expires_at=expires), raw


async def create_session(
    db: AsyncSession,
    user: User,
    *,
    ip: Optional[str],
    user_agent: Optional[str],
    amr: list[str],
    impersonator: Optional[User] = None,
    parent_session_id: Optional[uuid.UUID] = None,
    reason: Optional[str] = None,
) -> IssuedSession:
    now = utcnow()
    if impersonator is not None:
        expires = now + timedelta(minutes=settings.IMPERSONATION_EXPIRE_MINUTES)
    else:
        expires = now + timedelta(days=settings.SESSION_MAX_DAYS)
    session = UserSession(
        id=uuid.uuid4(),
        user_id=user.id,
        created_at=now,
        last_seen_at=now,
        expires_at=expires,
        ip_address=(ip or "")[:64] or None,
        user_agent=(user_agent or "")[:500] or None,
        device_label=device_label_from_ua(user_agent),
        amr=amr,
        mfa_verified_at=now if "otp" in amr else None,
        impersonator_user_id=impersonator.id if impersonator else None,
        impersonation_reason=reason,
        parent_session_id=parent_session_id,
    )
    db.add(session)
    await db.flush()  # a sessão precisa existir antes do refresh token (FK)
    refresh_raw = None
    if impersonator is None:
        rt, refresh_raw = _new_refresh_row(session)
        db.add(rt)
    ttl = tokens.access_ttl()
    if impersonator is not None:
        ttl = min(ttl, expires - now)
    access, _ = tokens.create_access_token(
        user.id, session.id, amr=amr, impersonated_by=impersonator.id if impersonator else None, ttl=ttl
    )
    await db.flush()
    return IssuedSession(session, access, int(ttl.total_seconds()), refresh_raw)


async def revoke_sessions(db: AsyncSession, sessions: list[UserSession], reason: str) -> list[uuid.UUID]:
    now = utcnow()
    ids: list[uuid.UUID] = []
    for s in sessions:
        if s.revoked_at is None:
            s.revoked_at = now
            s.revoked_reason = reason[:50]
            ids.append(s.id)
    if ids:
        # sessões de impersonation iniciadas a partir destas também são encerradas
        children = (
            await db.execute(
                select(UserSession).where(UserSession.parent_session_id.in_(ids), UserSession.revoked_at.is_(None))
            )
        ).scalars().all()
        for c in children:
            c.revoked_at = now
            c.revoked_reason = "parent_revoked"
            ids.append(c.id)
        await db.flush()
    return ids


async def revoke_all_user_sessions(
    db: AsyncSession, user_id: uuid.UUID, reason: str, *, except_session_id: Optional[uuid.UUID] = None
) -> list[uuid.UUID]:
    q = select(UserSession).where(UserSession.user_id == user_id, UserSession.revoked_at.is_(None))
    if except_session_id is not None:
        q = q.where(UserSession.id != except_session_id)
    sessions = list((await db.execute(q)).scalars().all())
    return await revoke_sessions(db, sessions, reason)


async def after_commit_revocations(ids: Iterable[uuid.UUID]) -> None:
    ids = list(ids)
    if ids:
        await mark_sessions_revoked_in_cache(ids)


@dataclass
class RefreshOutcome:
    access_token: str
    expires_in: int
    refresh_token: str
    user: User


class RefreshReuse(Exception):
    pass


async def rotate_refresh(raw_token: Optional[str]) -> RefreshOutcome:
    """Rotação com detecção de reuso (reuso revoga a família = sessão inteira)."""
    if not raw_token or len(raw_token) > 256:
        raise Unauthorized("INVALID_REFRESH", "Sessão expirada. Entre novamente.")
    h = sha256_hex(raw_token)
    revoked_ids: list[uuid.UUID] = []
    reuse = False
    async with independent_session() as s:
        rt = (
            await s.execute(select(RefreshToken).where(RefreshToken.token_hash == h).with_for_update())
        ).scalar_one_or_none()
        if rt is None:
            raise Unauthorized("INVALID_REFRESH", "Sessão expirada. Entre novamente.")
        session = (
            await s.execute(select(UserSession).where(UserSession.id == rt.session_id).with_for_update())
        ).scalar_one()
        now = utcnow()
        if rt.used_at is not None:
            # REUSO: token já rotacionado sendo apresentado de novo → comprometido
            revoked_ids = await revoke_sessions(s, [session], "refresh_reuse")
            await audit(s, None, "auth.refresh_reuse_detected", "user_session", session.id,
                        after={"user_id": session.user_id}, actor_user_id=session.user_id)
            reuse = True
        elif session.revoked_at is not None or session.expires_at <= now or rt.expires_at <= now:
            raise Unauthorized("INVALID_REFRESH", "Sessão expirada. Entre novamente.")
        else:
            user = await s.get(User, session.user_id)
            if user is None or not user.is_active or user.anonymized_at is not None:
                raise Unauthorized("INVALID_REFRESH", "Sessão expirada. Entre novamente.")
            # AÇÃO 003 — D1: titular com selo de credencial inválido não renova sessão (alerta de integridade)
            from app.modules.security.credentials import ensure_master_credential

            await ensure_master_credential(user, origin="refresh",
                                           error=Unauthorized("INVALID_REFRESH", "Sessão expirada. Entre novamente."))
            new_rt, new_raw = _new_refresh_row(session)
            s.add(new_rt)
            rt.used_at = now
            rt.replaced_by_id = new_rt.id
            session.last_seen_at = now
            access, _ = tokens.create_access_token(user.id, session.id, amr=list(session.amr or ["pwd"]))
            outcome = RefreshOutcome(access, int(tokens.access_ttl().total_seconds()), new_raw, user)
    if reuse:
        await after_commit_revocations(revoked_ids)
        raise Unauthorized("REFRESH_REUSED", "Sessão encerrada por segurança. Entre novamente.")
    return outcome


async def session_from_refresh(db: AsyncSession, raw_token: Optional[str]) -> Optional[UserSession]:
    if not raw_token or len(raw_token) > 256:
        return None
    rt = (await db.execute(select(RefreshToken).where(RefreshToken.token_hash == sha256_hex(raw_token)))).scalar_one_or_none()
    if rt is None:
        return None
    return await db.get(UserSession, rt.session_id)


# ─── MFA ─────────────────────────────────────────────────────────────────────

def _totp_aad(user_id: uuid.UUID) -> str:
    return f"user:{user_id}"


def encrypt_totp_secret(user_id: uuid.UUID, secret: str) -> str:
    return encrypt_str(secret, purpose="totp", aad=_totp_aad(user_id))


def decrypt_totp_secret(user_id: uuid.UUID, enc: str) -> str:
    return decrypt_str(enc, purpose="totp", aad=_totp_aad(user_id))


def _recovery_hash(user_id: uuid.UUID, code: str) -> str:
    norm = re.sub(r"[\s\-]", "", code.upper())
    return hmac_hex(f"{user_id}:{norm}", purpose="recovery")


def new_recovery_codes() -> list[str]:
    return [f"{random_safe(5)}-{random_safe(5)}" for _ in range(RECOVERY_CODES_COUNT)]


async def replace_recovery_codes(db: AsyncSession, user: User) -> list[str]:
    from sqlalchemy import delete

    await db.execute(delete(UserRecoveryCode).where(UserRecoveryCode.user_id == user.id))
    codes = new_recovery_codes()
    for c in codes:
        db.add(UserRecoveryCode(user_id=user.id, code_hash=_recovery_hash(user.id, c)))
    return codes


async def verify_second_factor(db: AsyncSession, user: User, code: str, *, allow_recovery: bool = True) -> Optional[str]:
    """Verifica TOTP (anti-replay) ou código de recuperação. ``user`` deve estar com FOR UPDATE.

    Retorna "totp" | "recovery" | None.
    """
    code = (code or "").strip()
    if user.totp_enabled and user.totp_secret_enc and re.fullmatch(r"\d{6}", code.replace(" ", "")):
        secret = decrypt_totp_secret(user.id, user.totp_secret_enc)
        step = totp.match_step(secret, code)
        if step is not None and (user.totp_last_step is None or step > user.totp_last_step):
            user.totp_last_step = step
            return "totp"
        return None
    if allow_recovery and user.totp_enabled and re.fullmatch(r"[A-Za-z0-9]{5}-?[A-Za-z0-9]{5}", code):
        h = _recovery_hash(user.id, code)
        rc = (
            await db.execute(
                select(UserRecoveryCode)
                .where(UserRecoveryCode.user_id == user.id, UserRecoveryCode.code_hash == h,
                       UserRecoveryCode.used_at.is_(None))
                .with_for_update()
            )
        ).scalar_one_or_none()
        if rc is not None:
            rc.used_at = utcnow()
            return "recovery"
    return None


def pwd_ts(user: User) -> int:
    return int(user.password_changed_at.timestamp()) if user.password_changed_at else 0


def create_mfa_token(user: User) -> tuple[str, int]:
    ttl = timedelta(seconds=settings.MFA_TOKEN_EXPIRE_SECONDS)
    tok, _ = tokens.encode(str(user.id), tokens.TYP_MFA, ttl, {"pwd_ts": pwd_ts(user)})
    return tok, int(ttl.total_seconds())


def password_binding(user: User) -> str:
    """Vínculo do token ``mfa_setup`` à senha ATUAL (HMAC do hash; não revela nada da senha).

    Qualquer troca de senha (novo hash argon2 com sal novo) invalida tokens de setup anteriores — mesmo
    dentro do mesmo segundo (o ``pwd_ts`` tem granularidade de segundos)."""
    return hmac_hex(f"{user.id}:{user.hashed_password or ''}", purpose="mfa_setup_binding")[:32]


def create_mfa_setup_token(user: User) -> tuple[str, int]:
    ttl = timedelta(seconds=settings.MFA_SETUP_TOKEN_EXPIRE_SECONDS)
    tok, _ = tokens.encode(str(user.id), tokens.TYP_MFA_SETUP, ttl,
                           {"pwd_ts": pwd_ts(user), "pwv": password_binding(user)})
    return tok, int(ttl.total_seconds())


async def mfa_token_attempt(jti: str) -> int:
    key = rkey("mfa", "attempts", jti)
    try:
        r = await get_redis()
        pipe = r.pipeline()
        pipe.incr(key)
        pipe.expire(key, settings.MFA_TOKEN_EXPIRE_SECONDS + 60)
        n, _ = await asyncio.wait_for(pipe.execute(), timeout=op_timeout_s())
        return int(n)
    except Exception as exc:  # sem contador de tentativas não se verifica código (fail-closed)
        mark_redis_down()
        log.error("mfa_attempt_counter_error", error_type=type(exc).__name__)
        raise ServiceUnavailable() from None


async def consume_mfa_jti(jti: str) -> bool:
    """Marca o mfa_token como usado (uso único). False se já usado."""
    key = rkey("mfa", "used", jti)
    try:
        r = await get_redis()
        return bool(await asyncio.wait_for(r.set(key, "1", nx=True, ex=settings.MFA_TOKEN_EXPIRE_SECONDS + 60),
                                           timeout=op_timeout_s()))
    except Exception as exc:
        mark_redis_down()
        log.error("mfa_jti_store_error", error_type=type(exc).__name__)
        raise ServiceUnavailable() from None


async def mfa_jti_used(jti: str) -> bool:
    try:
        r = await get_redis()
        return bool(await asyncio.wait_for(r.exists(rkey("mfa", "used", jti)), timeout=op_timeout_s()))
    except Exception as exc:  # uso único não verificável → não prosseguir (fail-closed)
        mark_redis_down()
        log.error("mfa_jti_store_error", error_type=type(exc).__name__)
        raise ServiceUnavailable() from None


async def apply_password(db: AsyncSession, user: User, new_password: str) -> None:
    user.hashed_password = hash_password(new_password)
    user.password_changed_at = utcnow()
    user.must_change_password = False


def check_password(user: User, password: str) -> tuple[bool, bool]:
    return verify_password(password, user.hashed_password)


async def grants_for(db: AsyncSession, user_id: uuid.UUID) -> tuple[Grant, ...]:
    return await load_grants(db, user_id)


def dumps(v: Any) -> str:
    return json.dumps(v, default=str)


__all__ = [
    "AuthContext",
    "create_session",
    "rotate_refresh",
    "revoke_sessions",
    "revoke_all_user_sessions",
    "register_failure",
    "verify_second_factor",
]
