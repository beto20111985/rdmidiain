"""
RD Mídia Indoor — Convite e ativação do 2º Administrador Master (ADR_011 D7; SPEC §5.1, §5.2).

- Token de uso único (``secrets.token_urlsafe(32)``) guardado SÓ como ``sha256_hex``; vai SOMENTE no
  e-mail ao convidado, no fragmento ``…/ativar-master#token=…`` (não chega a logs de servidor). Nunca é
  devolvido pela API, auditado ou logado.
- Um convite pendente por vez (``UNIQUE (seat) WHERE status='PENDING'`` + trigger + lock de assentos).
- Ativação ATÔMICA (uma transação): B cria a PRÓPRIA senha, confirma o PRÓPRIO TOTP (segredo gerado em
  ``/master-activation/totp`` e cifrado com o id reservado de B) e cria a PRÓPRIA senha financeira; a
  função definer ``rd_security_assign_master_seat(..., 'MASTER_INVITE', ...)`` só aceita usuário com os
  três fatores. Não existe estado intermediário "Master sem TOTP".
- 5 TOTP errados ⇒ convite ``LOCKED`` (contagem em transação independente).
- D1 (revisor I): todo convite carrega ``seal`` (HMAC da aplicação sobre id/assento/e-mail/convidante/
  ``token_hash``/validade). ``inspect``/``totp``/``complete`` e o reenvio conferem o selo (convite inserido
  direto no banco pelo papel de runtime ⇒ resposta genérica + alerta); a ativação confere também o selo de
  credencial do convidante.
- D2: ordem ÚNICA de locks em todas as operações de convite — modo (compartilhado) → assentos → linha do
  convite; deadlock/serialização/lock_timeout ⇒ 409 ``CONCURRENT_OPERATION`` com ``Retry-After``.
"""
from __future__ import annotations

import secrets
import uuid
from datetime import timedelta
from typing import Any, Optional

import structlog
from sqlalchemy import select, update
from sqlalchemy.exc import DBAPIError, IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import totp
from app.core.audit import audit
from app.core.config import settings
from app.core.crypto import sha256_hex
from app.core.email import OutgoingEmail, mask_email
from app.core.errors import Conflict, DomainError, NotFound, RateLimited, Unauthorized
from app.core.timeutils import utcnow

log = structlog.get_logger("rd.security.invites")

INVITE_TTL = timedelta(hours=72)
INVITE_MAX_TTL = timedelta(days=7)
MAX_FAILED_ATTEMPTS = 5
RESEND_PER_HOUR = 3
MAX_SENDS = 5
ACTIVATION_STEPS = ["PASSWORD", "TOTP", "FINANCIAL_PASSWORD"]


def invite_invalid() -> DomainError:
    return DomainError("MASTER_INVITE_INVALID", "Convite inválido ou expirado.", status=400)


def seats_full() -> DomainError:
    return Conflict("ADMIN_MASTER_SEATS_FULL", "Os dois assentos de Administrador Master já estão ocupados.")


def email_registered() -> DomainError:
    return Conflict("EMAIL_ALREADY_REGISTERED", "Este e-mail já pertence a outra conta.")


def _model():
    from app.models.security import AdminMasterInvite

    return AdminMasterInvite


def activation_link(raw_token: str) -> str:
    return f"{settings.panel_base_url}/ativar-master#token={raw_token}"


async def _user_name(db: AsyncSession, user_id: Any) -> Optional[str]:
    from app.models.users import User

    if user_id is None:
        return None
    return (await db.execute(select(User.full_name).where(User.id == user_id))).scalar_one_or_none()


async def invite_public(db: AsyncSession, inv: Any) -> dict:
    return {
        "id": str(inv.id),
        "seat": int(inv.seat),
        "full_name": inv.full_name,
        "email_masked": mask_email(inv.email),
        "status": inv.status,
        "expires_at": inv.expires_at,
        "invited_by_name": await _user_name(db, inv.invited_by),
        "sent_count": int(inv.sent_count or 0),
    }


async def expire_stale(db: AsyncSession, ctx: Any) -> int:
    """Marca ``EXPIRED`` os convites pendentes vencidos (auditoria ``admin_master.invite_expired``)."""
    Inv = _model()
    rows = (
        await db.execute(
            update(Inv).where(Inv.status == "PENDING", Inv.expires_at <= utcnow())
            .values(status="EXPIRED").returning(Inv.id, Inv.seat)
        )
    ).all()
    for iid, seat in rows:
        await audit(db, ctx, "admin_master.invite_expired", "admin_master_invite", iid, after={"seat": int(seat)})
    if rows:
        from app.modules.security.notifications import status_changed_for_masters

        await status_changed_for_masters(db, reason="invite_expired")
    return len(rows)


def _invite_email(inv: Any, raw_token: str, *, inviter_name: str, instance_name: str) -> OutgoingEmail:
    hours = int(INVITE_TTL.total_seconds() // 3600)
    return OutgoingEmail(
        to=inv.email,
        subject=f"{instance_name} — convite para Administrador Master",
        text=(
            f"Olá, {inv.full_name}.\n\n"
            f"{inviter_name} convidou você para ocupar o assento {inv.seat} de Administrador Master da "
            f"{instance_name}, responsável pela dupla aprovação financeira.\n\n"
            f"Para ativar, acesse (link pessoal e de uso único, válido por {hours} horas):\n"
            f"{activation_link(raw_token)}\n\n"
            "Na ativação você criará a SUA senha de acesso, configurará o SEU aplicativo autenticador (TOTP) e "
            "criará a SUA senha financeira. Ninguém mais — nem quem convidou — conhece ou define essas "
            "credenciais. Não encaminhe este e-mail.\n\n"
            "Se você não esperava este convite, ignore esta mensagem e avise a RD."
        ),
        tags={"kind": "admin_master_invite"},
    )


async def _send(email: OutgoingEmail) -> None:
    from app.core.email import send_safely

    await send_safely(email)


def _require_active_master(ctx: Any) -> None:
    from app.modules.security.masters import is_active_master_user

    if ctx.impersonation is not None:
        raise DomainError("IMPERSONATION_FORBIDDEN", "Ação não permitida durante impersonation.", status=403)
    if not is_active_master_user(ctx):
        raise DomainError("ADMIN_MASTER_REQUIRED", "Somente um Administrador Master ativo pode executar esta ação.",
                          status=403)


async def _lock_for_invites(db: AsyncSession) -> None:
    from app.modules.security import ddl
    from app.modules.security.approval_mode import lock_mode

    await lock_mode(db, exclusive=False)  # ordem global: APPROVAL_MODE → MASTER_SEATS
    from sqlalchemy import text

    await db.execute(text("SELECT pg_advisory_xact_lock(:k)"), {"k": ddl.LOCK_MASTER_SEATS})


async def _vacant_seat(db: AsyncSession) -> Optional[int]:
    from app.modules.security.masters import seat_holders

    holders = await seat_holders(db)
    for s in (1, 2):
        if s not in holders:
            return s
    return None


async def create_invite(db: AsyncSession, ctx: Any, *, full_name: str, email: str, totp_code: str,
                        count_failure) -> tuple[Any, OutgoingEmail]:
    """Cria o convite do 2º Master. Devolve (convite, e-mail a enviar APÓS o commit). Não faz commit.

    ``count_failure(user_id, kind)``: corrotina do endpoint que desfaz a transação e conta a falha de
    TOTP no lockout (mesmo padrão de ``/auth/mfa/totp/setup``).
    """
    from app.models.users import User
    from app.modules.auth import service as svc
    from app.modules.instance.service import brand_name
    from app.modules.security.approval_mode import current_state, map_db_error

    _require_active_master(ctx)
    await _lock_for_invites(db)
    state = await current_state(db)
    if state is None:
        from app.modules.security.approval_mode import not_initialized

        raise not_initialized()
    if state.mode != "DUAL":
        raise Conflict("APPROVAL_MODE_SINGLE", "Esta instalação usa aprovação financeira única.")
    seat = await _vacant_seat(db)
    if seat is None:
        raise seats_full()
    email_n = svc.normalize_email(email)
    if not svc._EMAIL_RE.match(email_n) or len(email_n) > 255:
        raise DomainError("EMAIL_INVALID", "E-mail inválido.", status=400)
    name = (full_name or "").strip()
    if len(name) < 2:
        raise DomainError("NAME_INVALID", "Informe o nome completo do convidado.", status=400)
    if (await db.execute(select(User.id).where(User.email == email_n))).first() is not None:
        raise email_registered()
    Inv = _model()
    if (await db.execute(select(Inv.id).where(Inv.status == "PENDING", Inv.expires_at > utcnow()))).first():
        raise Conflict("MASTER_INVITE_ALREADY_PENDING", "Já existe um convite pendente para o segundo Administrador Master.")
    me = (await db.execute(select(User).where(User.id == ctx.user_id).with_for_update())).scalar_one()
    from app.modules.security.credentials import ensure_master_credential

    await ensure_master_credential(me, origin="invite_create")  # D1: convidante com credencial selada
    remaining = svc.lock_remaining_s(me)
    if remaining > 0:
        svc.raise_locked(remaining)
    if await svc.verify_second_factor(db, me, totp_code, allow_recovery=False) != "totp":
        await count_failure(ctx.user_id, "master_invite_totp")
        raise DomainError("INVALID_MFA_CODE", "Código inválido.", status=400)
    await expire_stale(db, ctx)
    raw = secrets.token_urlsafe(32)
    now = utcnow()
    inv = Inv(id=uuid.uuid4(), seat=seat, email=email_n, full_name=name[:255], token_hash=sha256_hex(raw),
              activation_user_id=uuid.uuid4(), invited_by=ctx.user_id, status="PENDING", created_at=now,
              expires_at=now + INVITE_TTL, failed_attempts=0, sent_count=1, last_sent_at=now)
    from app.modules.security.credentials import compute_invite

    inv.seal = compute_invite(inv)  # D1: o banco recusa INSERT do papel de runtime sem selo
    db.add(inv)
    try:
        async with db.begin_nested():
            await db.flush()
    except IntegrityError:
        raise Conflict("MASTER_INVITE_ALREADY_PENDING",
                       "Já existe um convite pendente para o segundo Administrador Master.") from None
    except DBAPIError as exc:
        mapped = map_db_error(exc)
        if mapped is not None:
            raise mapped from None
        raise
    await audit(db, ctx, "admin_master.invite_created", "admin_master_invite", inv.id,
                after={"seat": seat, "email": mask_email(email_n), "expires_at": inv.expires_at})
    from app.modules.security.notifications import status_changed_for_masters

    await status_changed_for_masters(db, reason="invite_created")
    mail = _invite_email(inv, raw, inviter_name=ctx.user.full_name, instance_name=await brand_name(db))
    log.info("master_invite_created", seat=seat, invite_id=str(inv.id), email=mask_email(email_n))
    return inv, mail


async def _get_pending_for_manage(db: AsyncSession, invite_id: uuid.UUID) -> Any:
    Inv = _model()
    inv = (await db.execute(select(Inv).where(Inv.id == invite_id).with_for_update())).scalar_one_or_none()
    if inv is None or inv.status != "PENDING":
        raise NotFound()
    return inv


async def resend_invite(db: AsyncSession, ctx: Any, invite_id: uuid.UUID) -> tuple[Any, OutgoingEmail]:
    """Novo token (o anterior deixa de valer); validade +72 h limitada a 7 dias desde a criação."""
    from app.core import ratelimit
    from app.modules.instance.service import brand_name

    _require_active_master(ctx)
    await _lock_for_invites(db)
    await expire_stale(db, ctx)
    inv = await _get_pending_for_manage(db, invite_id)
    from app.modules.security.credentials import check_invite, compute_invite

    if not check_invite(inv, origin="invite_resend"):
        # D1: nunca re-selar (e mandar token novo para) um convite que não foi criado pela aplicação
        from app.modules.security.approval_mode import integrity_alert

        raise integrity_alert()
    if int(inv.sent_count or 0) >= MAX_SENDS:
        raise RateLimited("MASTER_INVITE_RATE_LIMITED", "Muitos reenvios. Tente mais tarde.")
    allowed, retry = await ratelimit.hit("master_invite_resend", str(inv.id), RESEND_PER_HOUR, 3600)
    if not allowed:
        raise RateLimited("MASTER_INVITE_RATE_LIMITED", "Muitos reenvios. Tente mais tarde.",
                          details={"retry_after_s": retry}, headers={"Retry-After": str(retry)})
    now = utcnow()
    raw = secrets.token_urlsafe(32)
    inv.token_hash = sha256_hex(raw)
    inv.expires_at = min(now + INVITE_TTL, inv.created_at + INVITE_MAX_TTL)
    inv.sent_count = int(inv.sent_count or 0) + 1
    inv.last_sent_at = now
    inv.seal = compute_invite(inv)  # token_hash e selo mudam JUNTOS (regra do banco)
    await db.flush()
    await audit(db, ctx, "admin_master.invite_resent", "admin_master_invite", inv.id,
                after={"sent_count": inv.sent_count, "expires_at": inv.expires_at})
    mail = _invite_email(inv, raw, inviter_name=ctx.user.full_name, instance_name=await brand_name(db))
    return inv, mail


async def revoke_invite(db: AsyncSession, ctx: Any, invite_id: uuid.UUID, reason: Optional[str]) -> None:
    _require_active_master(ctx)
    await _lock_for_invites(db)
    inv = await _get_pending_for_manage(db, invite_id)
    inv.status = "REVOKED"
    inv.revoked_at = utcnow()
    inv.revoked_by = ctx.user_id
    inv.revoke_reason = (reason or "").strip()[:300] or None
    await db.flush()
    await audit(db, ctx, "admin_master.invite_revoked", "admin_master_invite", inv.id, reason=inv.revoke_reason,
                after={"seat": int(inv.seat)})
    from app.modules.security.notifications import status_changed_for_masters

    await status_changed_for_masters(db, reason="invite_revoked")


# ─── Ativação pública (B) ────────────────────────────────────────────────────

async def find_valid_invite(db: AsyncSession, raw_token: str, *, for_update: bool = False,
                            origin: str = "master_activation") -> Any:
    """Convite PENDENTE, não expirado, não bloqueado e com SELO válido para o token; senão 400 genérico
    (selo inválido também agenda o alerta de integridade)."""
    Inv = _model()
    if not raw_token or len(raw_token) > 256:
        raise invite_invalid()
    q = select(Inv).where(Inv.token_hash == sha256_hex(raw_token))
    if for_update:
        q = q.with_for_update()
    inv = (await db.execute(q)).scalar_one_or_none()
    if (inv is None or inv.status != "PENDING" or inv.expires_at <= utcnow()
            or int(inv.failed_attempts or 0) >= MAX_FAILED_ATTEMPTS):
        raise invite_invalid()
    from app.modules.security.credentials import check_invite

    if not check_invite(inv, origin=origin):
        raise invite_invalid()
    return inv


async def inspect(db: AsyncSession, raw_token: str) -> dict:
    from app.modules.instance.service import brand_name

    inv = await find_valid_invite(db, raw_token, origin="master_activation_inspect")
    return {
        "full_name": inv.full_name,
        "email_masked": mask_email(inv.email),
        "expires_at": inv.expires_at,
        "instance_name": await brand_name(db),
        "steps": list(ACTIVATION_STEPS),
    }


async def start_totp(db: AsyncSession, raw_token: str) -> dict:
    """Gera (e substitui) o segredo TOTP pendente do convite, cifrado com o id reservado de B."""
    from app.modules.auth import service as svc

    await _lock_for_invites(db)  # D2: mesma ordem de locks do reenvio/revogação/ativação
    inv = await find_valid_invite(db, raw_token, for_update=True, origin="master_activation_totp")
    secret = totp.new_secret()
    inv.totp_pending_secret_enc = svc.encrypt_totp_secret(inv.activation_user_id, secret)
    await db.flush()
    return {"otpauth_uri": totp.otpauth_uri(secret, inv.email), "secret": secret}


def financial_password_problem(new: str, login_password: str) -> Optional[str]:
    """Mesma política de ``app.modules.financial.security`` + diferente da senha de login."""
    from app.modules.financial.security import FIN_PASSWORD_MAX, FIN_PASSWORD_MIN

    if not isinstance(new, str) or not (FIN_PASSWORD_MIN <= len(new) <= FIN_PASSWORD_MAX):
        return f"A senha financeira deve ter entre {FIN_PASSWORD_MIN} e {FIN_PASSWORD_MAX} caracteres."
    if new.isdigit() and len(set(new)) <= 2:
        return "Senha financeira muito simples."
    if new == login_password:
        return "A senha financeira deve ser diferente da senha de login."
    return None


async def register_activation_failure(invite_id: uuid.UUID) -> None:
    """TOTP errado na ativação: +1 em transação independente; 5ª falha ⇒ ``LOCKED``. Sempre levanta 400."""
    from app.core.authz import AuthContext
    from app.core.database import independent_session

    Inv = _model()
    locked = False
    async with independent_session() as s:
        inv = (await s.execute(select(Inv).where(Inv.id == invite_id).with_for_update())).scalar_one_or_none()
        if inv is not None and inv.status == "PENDING":
            inv.failed_attempts = int(inv.failed_attempts or 0) + 1
            if inv.failed_attempts >= MAX_FAILED_ATTEMPTS:
                inv.status = "LOCKED"
                locked = True
            await s.flush()
            await audit(s, AuthContext.system(origin="api"), "admin_master.invite_locked" if locked
                        else "admin_master.activation_failed", "admin_master_invite", inv.id,
                        after={"failed_attempts": inv.failed_attempts}, origin="api")
            if locked:
                from app.modules.security.notifications import status_changed_for_masters

                await status_changed_for_masters(s, reason="invite_locked")
    if locked:
        raise DomainError("MASTER_INVITE_LOCKED", "Convite bloqueado por tentativas inválidas. Peça um novo convite.",
                          status=400)
    raise DomainError("INVALID_MFA_CODE", "Código inválido.", status=400)


async def _same_device_as_inviter(db: AsyncSession, inviter_id: uuid.UUID, ip: Optional[str], ua: Optional[str]) -> bool:
    from app.models.users import UserSession

    last = (
        await db.execute(
            select(UserSession.ip_address, UserSession.user_agent)
            .where(UserSession.user_id == inviter_id, UserSession.impersonator_user_id.is_(None))
            .order_by(UserSession.created_at.desc()).limit(1)
        )
    ).first()
    if last is None or not ip or not ua:
        return False
    return (last[0] or "") == (ip or "")[:64] and (last[1] or "") == (ua or "")[:500]


class _ActivationTotpFailed(Exception):
    def __init__(self, invite_id: uuid.UUID) -> None:
        super().__init__("totp")
        self.invite_id = invite_id


async def complete_activation(*, raw_token: str, password: str, totp_code: str, financial_password: str,
                              ip: Optional[str], user_agent: Optional[str]) -> tuple[dict, str]:
    """Ativação atômica do 2º Master. Devolve (payload de login + ``recovery_codes``, refresh cru).

    TOTP errado ⇒ +1 tentativa em transação independente (5ª ⇒ ``LOCKED``). Deadlock/serialização/lock_timeout
    ⇒ 409 ``CONCURRENT_OPERATION`` (D2), nunca 500.
    """
    from app.modules.security.approval_mode import map_db_error

    try:
        payload, refresh_raw = await _complete_tx(raw_token=raw_token, password=password, totp_code=totp_code,
                                                  financial_password=financial_password, ip=ip, user_agent=user_agent)
    except _ActivationTotpFailed as exc:
        await register_activation_failure(exc.invite_id)
        raise  # pragma: no cover - register_activation_failure sempre levanta
    except DBAPIError as exc:
        mapped = map_db_error(exc)
        if mapped is not None:
            raise mapped from None
        raise
    return payload, refresh_raw


async def _complete_tx(*, raw_token: str, password: str, totp_code: str, financial_password: str,
                       ip: Optional[str], user_agent: Optional[str]) -> tuple[dict, str]:
    from app.core.authz import AuthContext
    from app.core.database import independent_session
    from app.core.passwords import hash_password, validate_password_policy
    from app.models.users import User
    from app.modules.auth import service as svc
    from app.modules.auth.accounts import login_payload
    from app.modules.security import masters
    from app.modules.security.approval_mode import current_state
    from app.modules.security.credentials import ensure_master_credential
    from app.modules.security.notifications import (
        notify_same_device,
        notify_second_master_activated,
        status_changed_for_masters,
    )

    async with independent_session(lock_timeout_ms=20000) as s:
        # D2: ordem única — modo (compartilhado) → assentos → linha do convite (igual a reenvio/revogação)
        await _lock_for_invites(s)
        inv = await find_valid_invite(s, raw_token, for_update=True, origin="master_activation_complete")
        state = await current_state(s)
        if state is None or state.mode != "DUAL":
            raise invite_invalid()
        # D1: o convidante precisa ter credencial selada válida (convite de "Master" forjado não vale)
        inviter = (await s.execute(select(User).where(User.id == inv.invited_by))).scalar_one_or_none()
        if inviter is None:
            raise invite_invalid()
        await ensure_master_credential(inviter, origin="master_activation_inviter",
                                       error=Unauthorized("MASTER_INVITE_INVALID", "Convite inválido ou expirado."))
        holder = (await s.execute(select(User.id).where(User.admin_master_seat == inv.seat))).first()
        if holder is not None:
            raise seats_full()
        if (await s.execute(select(User.id).where(User.email == inv.email))).first() is not None:
            raise email_registered()
        validate_password_policy(password, inv.email)
        problem = financial_password_problem(financial_password, password)
        if problem:
            raise DomainError("FINANCIAL_PASSWORD_POLICY", problem, status=400)
        if not inv.totp_pending_secret_enc:
            raise DomainError("MFA_SETUP_NOT_STARTED", "Configure o aplicativo autenticador primeiro.", status=400)
        secret = svc.decrypt_totp_secret(inv.activation_user_id, inv.totp_pending_secret_enc)
        step = totp.match_step(secret, totp_code)
        if step is None:
            raise _ActivationTotpFailed(inv.id)  # sai da transação (rollback) e conta a falha fora dela
        now = utcnow()
        user = User(
            id=inv.activation_user_id, email=inv.email, full_name=inv.full_name,
            hashed_password=hash_password(password), password_changed_at=now, is_active=True, is_verified=True,
            must_change_password=False, totp_enabled=True, totp_secret_enc=inv.totp_pending_secret_enc,
            totp_pending_secret_enc=None, totp_last_step=step, totp_enabled_at=now,
            financial_password_hash=hash_password(financial_password), financial_password_changed_at=now,
        )
        s.add(user)
        try:
            async with s.begin_nested():
                await s.flush()
        except IntegrityError:
            raise email_registered() from None
        # assento pela função definer + selo de credencial na MESMA transação (masters.assign_seat)
        await masters.assign_seat(s, user_id=user.id, seat=int(inv.seat), source="MASTER_INVITE",
                                  invite_id=inv.id, actor_user_id=user.id)
        codes = await svc.replace_recovery_codes(s, user)
        same_device = await _same_device_as_inviter(s, inv.invited_by, ip, user_agent)
        user.last_login = now
        issued = await svc.create_session(s, user, ip=ip, user_agent=user_agent, amr=["pwd", "otp"])
        ctx = AuthContext(kind="user", user=user)
        await audit(s, ctx, "admin_master.activated", "user", user.id,
                    after={"seat": int(inv.seat), "invite_id": inv.id, "invited_by": inv.invited_by,
                           "same_device_as_inviter": same_device})
        await audit(s, ctx, "auth.login", "user_session", issued.session.id,
                    after={"amr": ["pwd", "otp"], "via": "master_activation"})
        await notify_second_master_activated(s, name=user.full_name, seat=int(inv.seat), invite_id=inv.id)
        if same_device:
            await notify_same_device(s, name=user.full_name, seat=int(inv.seat), invite_id=inv.id)
        await status_changed_for_masters(s, reason="master_activated")
        payload = login_payload(user, issued)
        payload["recovery_codes"] = codes
        log.warning("master_activated", seat=int(inv.seat), invite_id=str(inv.id), same_device=same_device)
    return payload, issued.refresh_token
