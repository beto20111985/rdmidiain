"""
RD Mídia Indoor — Step-up de segurança financeira (P001 §88; ETAPA2 #687–#689).

- TOTP com anti-replay (serviço do W1 ``verify_second_factor``; códigos de
  recuperação NÃO valem para dinheiro) + **senha financeira** (Argon2, separada da
  senha de login: ``users.financial_password_hash``) + **código por e-mail** em toda
  aprovação/autorização financeira e em todo saque.
- Master recuperado pela CLI (``recover-master``): step-up bloqueado (423
  ``MASTER_RECENTLY_RECOVERED``) por ``finance.password_reset_cooldown_hours``.
- Bloqueio progressivo: 5 falhas em 15 min ⇒ 423 (contador no Redis, fora da
  transação da request; falha auditada em transação independente).
- Só o próprio usuário define/troca a sua senha financeira (independência dos
  Admin Master — P001 §84).
"""
from __future__ import annotations

import secrets
import uuid
from datetime import timedelta
from typing import Any, Optional

import structlog
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.audit import audit, audit_independent
from app.core.crypto import constant_time_equals, hmac_hex
from app.core.errors import DomainError, Locked
from app.core.passwords import hash_password, verify_password
from app.core.redis import get_redis, rkey
from app.core.timeutils import utcnow

log = structlog.get_logger("rd.financial.security")

MAX_FAILURES = 5
FAILURE_WINDOW_S = 15 * 60
EMAIL_CODE_TTL = timedelta(minutes=10)
EMAIL_CODE_MAX_ATTEMPTS = 5
FIN_PASSWORD_MIN = 8
FIN_PASSWORD_MAX = 128


def _fail_key(user_id: Any) -> str:
    return rkey("fin", "stepup", "fail", user_id)


async def _check_locked(user_id: Any) -> None:
    try:
        r = await get_redis()
        n = int(await r.get(_fail_key(user_id)) or 0)
        if n >= MAX_FAILURES:
            ttl = await r.ttl(_fail_key(user_id))
            raise Locked("FINANCIAL_STEP_UP_LOCKED", "Muitas tentativas inválidas. Tente novamente mais tarde.",
                         details={"retry_after_s": max(int(ttl), 1)})
    except Locked:
        raise
    except Exception as exc:  # Redis fora: não bloqueia, mas registra
        log.error("stepup_counter_unavailable", error_type=type(exc).__name__)


async def _register_failure(ctx: Any, user_id: Any, purpose: str) -> None:
    try:
        r = await get_redis()
        k = _fail_key(user_id)
        n = await r.incr(k)
        if n == 1:
            await r.expire(k, FAILURE_WINDOW_S)
    except Exception as exc:
        log.error("stepup_counter_unavailable", error_type=type(exc).__name__)
    try:
        await audit_independent(ctx, "finance.step_up_failed", "user", user_id, after={"purpose": purpose})
    except Exception as exc:
        log.error("stepup_audit_failed", error_type=type(exc).__name__)


async def _reset_failures(user_id: Any) -> None:
    try:
        r = await get_redis()
        await r.delete(_fail_key(user_id))
    except Exception:
        pass


def _invalid() -> DomainError:
    return DomainError("FINANCIAL_STEP_UP_INVALID", "Código de verificação ou senha financeira inválidos.", status=403)


async def _master_credential_guard(user: Any) -> None:
    """AÇÃO 003 — D1 (agente B): titular de assento só faz step-up/troca de senha financeira com o selo de
    credencial válido (escrita direta do papel de runtime sem a chave ⇒ 401 genérico + alerta)."""
    from app.modules.security.credentials import ensure_master_credential

    await ensure_master_credential(user, origin="financial_step_up")


async def lock_user(db: AsyncSession, user_id: Any):
    from app.models.users import User

    # FOR NO KEY UPDATE: serializa o anti-replay do TOTP sem bloquear o FK (KEY SHARE) da
    # auditoria independente de falhas (que referencia users.id).
    return (await db.execute(select(User).where(User.id == user_id).with_for_update(key_share=True))).scalar_one()


async def verify_step_up(
    db: AsyncSession,
    ctx: Any,
    *,
    totp_code: Optional[str],
    financial_password: Optional[str],
    purpose: str,
) -> Any:
    """Valida TOTP (anti-replay) + senha financeira do usuário do ``ctx``. Retorna o User (FOR UPDATE)."""
    from app.modules.auth.service import verify_second_factor

    if ctx.impersonation is not None:
        raise DomainError("IMPERSONATION_FORBIDDEN", "Ação não permitida durante impersonation.", status=403)
    await _check_locked(ctx.user_id)
    user = await lock_user(db, ctx.user_id)
    await _master_credential_guard(user)  # AÇÃO 003 — D1 (agente B): titular com selo inválido ⇒ 401 + alerta
    if not user.totp_enabled:
        raise DomainError("MFA_REQUIRED_FOR_FINANCE", "Ative a verificação em duas etapas para operar o financeiro.",
                          status=403)
    if not user.financial_password_hash:
        raise DomainError("FINANCIAL_PASSWORD_NOT_SET", "Defina sua senha financeira antes de continuar.", status=403)
    recovered_until = await recovery_cooldown_until(db, user)
    if recovered_until is not None and recovered_until > utcnow():
        from app.core.timeutils import isoformat_z

        raise Locked("MASTER_RECENTLY_RECOVERED",
                     "Credenciais recuperadas recentemente: operações financeiras bloqueadas temporariamente.",
                     details={"blocked_until": isoformat_z(recovered_until)})
    until = await reset_cooldown_until(db, user.id)
    if until is not None and until > utcnow():
        from app.core.timeutils import isoformat_z

        raise Locked("FINANCIAL_PASSWORD_RECENTLY_RESET",
                     "Senha financeira redefinida recentemente: saques e aprovações bloqueados temporariamente.",
                     details={"blocked_until": isoformat_z(until)})
    if not financial_password or not totp_code:
        await _register_failure(ctx, user.id, purpose)
        raise _invalid()
    ok, _ = verify_password(financial_password, user.financial_password_hash)
    if not ok:
        await _register_failure(ctx, user.id, purpose)
        raise _invalid()
    method = await verify_second_factor(db, user, totp_code, allow_recovery=False)
    if method != "totp":
        await _register_failure(ctx, user.id, purpose)
        raise _invalid()
    await _reset_failures(user.id)
    return user


# ─── Senha financeira ────────────────────────────────────────────────────────

def _policy(new: str) -> None:
    if not isinstance(new, str) or not (FIN_PASSWORD_MIN <= len(new) <= FIN_PASSWORD_MAX):
        raise DomainError("FINANCIAL_PASSWORD_POLICY",
                          f"A senha financeira deve ter entre {FIN_PASSWORD_MIN} e {FIN_PASSWORD_MAX} caracteres.")
    if new.isdigit() and len(set(new)) <= 2:
        raise DomainError("FINANCIAL_PASSWORD_POLICY", "Senha financeira muito simples.")


async def set_financial_password(
    db: AsyncSession,
    ctx: Any,
    *,
    login_password: str,
    totp_code: str,
    new_password: str,
    current_financial_password: Optional[str] = None,
) -> None:
    """Define/troca a senha financeira DO PRÓPRIO usuário (login + TOTP [+ atual])."""
    from app.modules.auth.service import verify_second_factor

    if ctx.impersonation is not None:
        raise DomainError("IMPERSONATION_FORBIDDEN", "Ação não permitida durante impersonation.", status=403)
    await _check_locked(ctx.user_id)
    user = await lock_user(db, ctx.user_id)
    await _master_credential_guard(user)  # AÇÃO 003 — D1 (agente B): titular com selo inválido ⇒ 401 + alerta
    if not user.totp_enabled:
        raise DomainError("MFA_REQUIRED_FOR_FINANCE", "Ative a verificação em duas etapas primeiro.", status=403)
    ok, _ = verify_password(login_password or "", user.hashed_password)
    if not ok:
        await _register_failure(ctx, user.id, "financial_password")
        raise _invalid()
    if user.financial_password_hash:
        cur_ok, _ = verify_password(current_financial_password or "", user.financial_password_hash)
        if not cur_ok:
            await _register_failure(ctx, user.id, "financial_password")
            raise _invalid()
    _policy(new_password)
    same_as_login, _ = verify_password(new_password, user.hashed_password)
    if same_as_login:
        raise DomainError("FINANCIAL_PASSWORD_POLICY", "A senha financeira deve ser diferente da senha de login.")
    if await verify_second_factor(db, user, totp_code, allow_recovery=False) != "totp":
        await _register_failure(ctx, user.id, "financial_password")
        raise _invalid()
    first = user.financial_password_hash is None
    user.financial_password_hash = hash_password(new_password)
    user.financial_password_changed_at = utcnow()
    await _reset_failures(user.id)
    await audit(db, ctx, "finance.financial_password_set" if first else "finance.financial_password_changed",
                "user", user.id)


# ─── Código por e-mail (PIX OUT) ─────────────────────────────────────────────

# ─── Código por e-mail (3º fator) — TODA aprovação financeira e TODO saque ──────
# REQ-DUP-005 / REQ-SAQ-006 (ETAPA2 #687–#689; matriz C-32 "saque/aprovação financeira: e-mail
# SIM"). Um único mecanismo para aprovações e saques: código de 6 dígitos, 10 min, uso único,
# vinculado a (usuário, ação, recurso) e guardado só como HMAC no Redis; no máximo 5 tentativas
# por código (verificação atômica em Lua: uso único/anti-replay mesmo com requisições
# concorrentes); novo envio substitui o anterior; 5 envios por 15 min por usuário. Envio e
# falhas são auditados. Redis indisponível ⇒ 503 (falha FECHADA: nunca dispensa o fator).

EMAIL_CODE_SEND_LIMIT = 5
EMAIL_CODE_SEND_WINDOW_S = 15 * 60

_VERIFY_LUA = """
local h = redis.call('HGET', KEYS[1], 'h')
if not h then return -1 end
local a = redis.call('HINCRBY', KEYS[1], 'a', 1)
if a > tonumber(ARGV[2]) then redis.call('DEL', KEYS[1]); return -2 end
if h == ARGV[1] then redis.call('DEL', KEYS[1]); return 1 end
return 0
"""


def new_email_code() -> str:
    return f"{secrets.randbelow(10**6):06d}"


def _ecode_key(user_id: Any, purpose: str, resource: str) -> str:
    import hashlib

    return rkey("fin", "ecode", user_id, purpose, hashlib.sha256(resource.encode()).hexdigest()[:32])


def _ecode_hash(user_id: Any, purpose: str, resource: str, code: str) -> str:
    return hmac_hex(f"{purpose}|{resource}|{user_id}|{code.strip()}", purpose="fin_email_code")


def _ecode_unavailable() -> DomainError:
    return DomainError("FINANCIAL_EMAIL_CODE_UNAVAILABLE",
                       "Verificação por e-mail indisponível no momento. Tente novamente em instantes.", status=503)


async def send_action_code(db: AsyncSession, ctx: Any, *, purpose: str, resource: str, summary: str) -> dict:
    """Envia o código da ação ao e-mail do PRÓPRIO usuário (substitui um código anterior da mesma ação)."""
    from app.core.email import OutgoingEmail, send_safely
    from app.core.errors import RateLimited

    if ctx.impersonation is not None:
        raise DomainError("IMPERSONATION_FORBIDDEN", "Ação não permitida durante impersonation.", status=403)
    user = ctx.user
    if not getattr(user, "email", None):
        raise DomainError("EMAIL_REQUIRED", "Cadastre um e-mail para receber o código.", status=409)
    code = new_email_code()
    try:
        r = await get_redis()
        rl = rkey("fin", "ecode_sends", ctx.user_id)
        n = await r.incr(rl)
        if n == 1:
            await r.expire(rl, EMAIL_CODE_SEND_WINDOW_S)
        if n > EMAIL_CODE_SEND_LIMIT:
            raise RateLimited("FINANCIAL_EMAIL_CODE_RATE_LIMITED", "Muitos códigos solicitados. Aguarde alguns minutos.",
                              details={"retry_after_s": max(int(await r.ttl(rl)), 1)})
        key = _ecode_key(ctx.user_id, purpose, resource)
        await r.delete(key)
        await r.hset(key, mapping={"h": _ecode_hash(ctx.user_id, purpose, resource, code), "a": 0})
        await r.expire(key, int(EMAIL_CODE_TTL.total_seconds()))
    except DomainError:
        raise
    except Exception as exc:
        log.error("email_code_store_unavailable", error_type=type(exc).__name__)
        raise _ecode_unavailable() from None
    await audit(db, ctx, "finance.email_code_sent", purpose, resource[:255], after={"summary": summary[:200]})
    await send_safely(OutgoingEmail(
        to=user.email,
        subject="RD Mídia Indoor — código de confirmação financeira",
        text=(f"Seu código para confirmar: {summary} é {code}.\n"
              "Ele expira em 10 minutos e vale uma única vez. Se você não reconhece esta operação, "
              "não informe o código a ninguém e avise a RD."),
        tags={"kind": "fin_email_code", "purpose": purpose},
    ))
    return {"sent": True, "expires_in": int(EMAIL_CODE_TTL.total_seconds())}


async def verify_action_code(ctx: Any, *, purpose: str, resource: str, code: Optional[str]) -> None:
    """Valida e CONSOME o código (uso único). Sem código ⇒ 403 REQUIRED; errado/expirado/reusado/de
    outra ação ⇒ 403 INVALID (falha contada no bloqueio progressivo e auditada)."""
    code = (code or "").strip()
    if not code:
        await _register_failure(ctx, ctx.user_id, f"email_code:{purpose}")
        raise DomainError("FINANCIAL_EMAIL_CODE_REQUIRED",
                          "Informe o código enviado ao seu e-mail (solicite um novo se necessário).", status=403)
    try:
        r = await get_redis()
        res = int(await r.eval(_VERIFY_LUA, 1, _ecode_key(ctx.user_id, purpose, resource),
                               _ecode_hash(ctx.user_id, purpose, resource, code), EMAIL_CODE_MAX_ATTEMPTS))
    except Exception as exc:
        log.error("email_code_store_unavailable", error_type=type(exc).__name__)
        raise _ecode_unavailable() from None
    if res != 1:
        await _register_failure(ctx, ctx.user_id, f"email_code:{purpose}")
        raise DomainError("FINANCIAL_EMAIL_CODE_INVALID", "Código de e-mail inválido, expirado ou já utilizado.",
                          status=403)


# ─── Recuperação da senha financeira esquecida ───────────────────────────────
# Só o PRÓPRIO usuário (sessão autenticada) + código por e-mail (15 min, uso único) + TOTP
# válido (anti-replay). Nenhuma rota recebe user_id: um Admin Master nunca redefine a senha
# financeira do outro (P001 §84). Após a redefinição, saques/aprovações ficam bloqueados por
# ``finance.password_reset_cooldown_hours`` (proteção contra sequestro de conta).

RESET_CODE_TTL = timedelta(minutes=15)
RESET_CODE_MAX_ATTEMPTS = 5
RESET_MAX_REQUESTS_PER_HOUR = 3


def _reset_hash(user_id: Any, code: str) -> str:
    return hmac_hex(f"{user_id}:{code.strip()}", purpose="fin_pwd_reset")


async def recovery_cooldown_until(db: AsyncSession, user: Any):
    """Fim do bloqueio financeiro após ``recover-master`` (ADR_011 D8; SPEC 003 §4.4):
    ``users.master_recovered_at + finance.password_reset_cooldown_hours``. ``None`` se nunca recuperado."""
    from app.core.settings_service import get_setting

    recovered_at = getattr(user, "master_recovered_at", None)
    if recovered_at is None:
        return None
    return recovered_at + timedelta(hours=int(await get_setting(db, "finance.password_reset_cooldown_hours")))


async def reset_cooldown_until(db: AsyncSession, user_id: Any):
    from sqlalchemy import func

    from app.core.settings_service import get_setting
    from app.models.financial import FinancialPasswordResetCode as R

    last = (await db.execute(select(func.max(R.used_at)).where(R.user_id == user_id))).scalar_one_or_none()
    if last is None:
        return None
    return last + timedelta(hours=int(await get_setting(db, "finance.password_reset_cooldown_hours")))


async def request_password_reset(db: AsyncSession, ctx: Any) -> dict:
    """Envia código de 6 dígitos ao e-mail do PRÓPRIO usuário (hash HMAC; 15 min; uso único)."""
    from sqlalchemy import func, update

    from app.core.email import OutgoingEmail, send_safely
    from app.core.errors import RateLimited
    from app.core.logging import current_request_meta
    from app.models.financial import FinancialPasswordResetCode as R

    if ctx.impersonation is not None:
        raise DomainError("IMPERSONATION_FORBIDDEN", "Ação não permitida durante impersonation.", status=403)
    user = await lock_user(db, ctx.user_id)
    await _master_credential_guard(user)  # AÇÃO 003 — D1 (agente B): titular com selo inválido ⇒ 401 + alerta
    if not user.totp_enabled:
        raise DomainError("MFA_REQUIRED_FOR_FINANCE", "Ative a verificação em duas etapas primeiro.", status=403)
    now = utcnow()
    recent = (await db.execute(select(func.count()).select_from(R).where(
        R.user_id == user.id, R.created_at > now - timedelta(hours=1)))).scalar_one()
    if recent >= RESET_MAX_REQUESTS_PER_HOUR:
        raise RateLimited("FINANCIAL_PASSWORD_RESET_RATE_LIMITED",
                          "Muitas solicitações de recuperação. Tente novamente mais tarde.",
                          details={"retry_after_s": 3600})
    await db.execute(update(R).where(R.user_id == user.id, R.used_at.is_(None), R.invalidated_at.is_(None))
                     .values(invalidated_at=now))
    code = new_email_code()
    db.add(R(id=uuid.uuid4(), user_id=user.id, code_hash=_reset_hash(user.id, code),
             expires_at=now + RESET_CODE_TTL, request_ip=(current_request_meta().ip or None)))
    await audit(db, ctx, "finance.financial_password_reset_requested", "user", user.id)
    await db.flush()
    await send_safely(OutgoingEmail(
        to=user.email,
        subject="RD Mídia Indoor — recuperação da senha financeira",
        text=(f"Seu código para redefinir a senha financeira é {code}. Ele expira em 15 minutos e só pode ser "
              "usado uma vez, junto com o código do seu aplicativo autenticador.\n"
              "Se você não pediu, ignore este e-mail e avise a RD."),
        tags={"kind": "fin_password_reset"},
    ))
    return {"sent": True, "expires_in": int(RESET_CODE_TTL.total_seconds())}


async def reset_financial_password(db: AsyncSession, ctx: Any, *, code: str, totp_code: str, new_password: str) -> dict:
    """Redefine a senha financeira DO PRÓPRIO usuário com código de e-mail + TOTP; abre o bloqueio temporário."""
    from sqlalchemy import update

    from app.core.database import independent_session
    from app.core.settings_service import get_setting
    from app.core.timeutils import isoformat_z
    from app.models.financial import FinancialPasswordResetCode as R
    from app.modules.auth.service import verify_second_factor

    if ctx.impersonation is not None:
        raise DomainError("IMPERSONATION_FORBIDDEN", "Ação não permitida durante impersonation.", status=403)
    await _check_locked(ctx.user_id)
    user = await lock_user(db, ctx.user_id)
    await _master_credential_guard(user)  # AÇÃO 003 — D1 (agente B): titular com selo inválido ⇒ 401 + alerta
    if not user.totp_enabled:
        raise DomainError("MFA_REQUIRED_FOR_FINANCE", "Ative a verificação em duas etapas primeiro.", status=403)
    now = utcnow()
    bad = DomainError("FINANCIAL_PASSWORD_RESET_INVALID", "Código de recuperação ou verificação inválidos.", status=403)
    row = (await db.execute(select(R).where(R.user_id == user.id, R.used_at.is_(None), R.invalidated_at.is_(None))
                            .order_by(R.created_at.desc()).limit(1))).scalar_one_or_none()
    if row is None or row.expires_at <= now or row.attempts >= RESET_CODE_MAX_ATTEMPTS:
        await _register_failure(ctx, user.id, "financial_password_reset")
        raise bad
    if not code or not constant_time_equals(_reset_hash(user.id, code), row.code_hash):
        await _register_failure(ctx, user.id, "financial_password_reset")
        try:  # tentativa persistida fora da transação (que sofrerá rollback)
            async with independent_session() as s:
                await s.execute(update(R).where(R.id == row.id).values(attempts=R.attempts + 1))
        except Exception as exc:
            log.error("reset_attempt_count_failed", error_type=type(exc).__name__)
        raise bad
    row = (await db.execute(select(R).where(R.id == row.id).with_for_update()
                            .execution_options(populate_existing=True))).scalar_one()
    if row.used_at is not None or row.invalidated_at is not None:
        raise bad  # uso concorrente: somente um vence
    if await verify_second_factor(db, user, totp_code, allow_recovery=False) != "totp":
        await _register_failure(ctx, user.id, "financial_password_reset")
        raise bad
    _policy(new_password)
    same_as_login, _ = verify_password(new_password, user.hashed_password)
    if same_as_login:
        raise DomainError("FINANCIAL_PASSWORD_POLICY", "A senha financeira deve ser diferente da senha de login.")
    user.financial_password_hash = hash_password(new_password)
    user.financial_password_changed_at = now
    row.used_at = now
    await db.execute(update(R).where(R.user_id == user.id, R.id != row.id, R.used_at.is_(None),
                                     R.invalidated_at.is_(None)).values(invalidated_at=now))
    await _reset_failures(user.id)
    blocked_until = now + timedelta(hours=int(await get_setting(db, "finance.password_reset_cooldown_hours")))
    await audit(db, ctx, "finance.financial_password_reset", "user", user.id,
                after={"blocked_until": isoformat_z(blocked_until)})
    from app.modules.financial import common

    await common.notify(db, [user.id], "finance.password_reset",
                        {"when": isoformat_z(now), "blocked_until": isoformat_z(blocked_until)},
                        dedupe_key=f"finance.password_reset:{row.id}", entity=("user", user.id))
    await db.flush()
    return {"reset": True, "blocked_until": blocked_until}


def security_status(user: Any) -> dict:
    return {
        "totp_enabled": bool(user.totp_enabled),
        "financial_password_set": bool(user.financial_password_hash),
        "financial_password_changed_at": user.financial_password_changed_at,
    }


def new_idempotency_key() -> str:
    return uuid.uuid4().hex
