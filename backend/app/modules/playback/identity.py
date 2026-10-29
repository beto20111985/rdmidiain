"""
RD Mídia Indoor — Identidade, pareamento e autenticação de dispositivos (RDPP/1 §1–§3, §8.2.5–6).

- Pedido de pareamento com chave pública ES256 (SPKI P-256 validada), código
  ``XXXX-XXXX`` (``new_pairing_code``, 15 min), QR = ``panel_pair_url``.
- Claim pelo painel (``screens.pair`` no escopo da tela) ou por login controlado de
  técnico no dispositivo (credenciais nunca armazenadas). O claim revoga o
  dispositivo ATIVO anterior da tela (o WS dele é fechado com ``device_revoked``).
- Resultado assinado pelo dispositivo (headers ``X-RD-Device-Ts-Ms`` /
  ``X-RD-Device-Signature``, ±120 s).
- Desafio (32 bytes, uso único, 60 s) → JWT ``typ=device`` de 15 min (``sub``,
  ``scr``, ``org``, ``sid``); registrado em ``authz.register_device_authenticator``.
- Revogação imediata: banco (fonte de verdade, checada em toda request) + marca no
  Redis (checagem periódica das conexões WS) + publicação ``device_revoked``.
"""
from __future__ import annotations

import hashlib
import json
import re
import secrets
import uuid
from datetime import timedelta
from typing import Any, Optional

import structlog
from fastapi import Request
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import authz, ids, ratelimit, tokens
from app.core.audit import audit
from app.core.authz import AuthContext
from app.core.errors import Conflict, DomainError, NotFound, Unauthorized
from app.core.redis import get_redis, rkey
from app.core.timeutils import utcnow
from app.models.devices import Device, DeviceStatus, PairingMethod, PairingRequest, PairingStatus
from app.modules.playback import config as pcfg
from app.modules.playback import integration as integ
from app.modules.playback import protocol as proto

log = structlog.get_logger("rd.playback.identity")

PLATFORMS = ("web", "windows", "android")
_VERSION_RE = re.compile(r"^[0-9]+(\.[0-9]+){0,3}([-+][0-9A-Za-z.\-]{1,30})?$")


def ms(dt) -> int:
    return int(dt.timestamp() * 1000)


def _err_revoked() -> Unauthorized:
    return Unauthorized("DEVICE_REVOKED", "Dispositivo revogado. Faça um novo pareamento.")


def k_revoked(device_id: Any) -> str:
    return rkey("rdpp", "revoked", device_id)


async def is_revoked_cached(device_id: Any) -> bool:
    try:
        r = await get_redis()
        return bool(await r.exists(k_revoked(device_id)))
    except Exception:  # o banco continua sendo checado em toda request HTTP
        return False


# ─── Pedido de pareamento ────────────────────────────────────────────────────

def _clean_device_info(info: Any) -> Optional[dict]:
    if info is None:
        return None
    if not isinstance(info, dict):
        raise DomainError("INVALID_DEVICE_INFO", "device_info inválido.", status=422)
    out: dict = {}
    for k in ("model", "os", "resolution", "manufacturer", "serial"):
        v = info.get(k)
        if isinstance(v, str):
            out[k] = v[:120]
    return out


async def create_pairing_request(
    db: AsyncSession, *, public_key: str, key_alg: str, platform: str, app_version: Optional[str], device_info: Any
) -> PairingRequest:
    if key_alg != proto.ALG:
        raise DomainError("UNSUPPORTED_KEY_ALG", "Algoritmo de chave não suportado (use ES256).", status=422)
    if platform not in PLATFORMS:
        raise DomainError("INVALID_PLATFORM", "Plataforma inválida.", status=422)
    if app_version is not None and not _VERSION_RE.match(app_version):
        raise DomainError("INVALID_APP_VERSION", "Versão do app inválida.", status=422)
    try:
        spki, fingerprint = proto.normalize_public_spki(public_key)
    except proto.ProtocolCryptoError:
        raise DomainError("INVALID_PUBLIC_KEY", "Chave pública inválida (esperado SPKI EC P-256).", status=422) from None
    used = (await db.execute(select(Device.id).where(Device.key_fingerprint == fingerprint))).first()
    if used is not None:
        # dispositivo revogado nunca volta: re-pareamento exige NOVA chave (RDPP §1.2)
        raise Conflict("KEY_ALREADY_USED", "Esta chave já pertence a um dispositivo. Gere uma nova chave para parear.")

    async def _exists(code: str) -> bool:
        return (await db.execute(select(PairingRequest.id).where(PairingRequest.pairing_code == code))).first() is not None

    code = await ids.generate_unique(ids.new_pairing_code, _exists)
    now = utcnow()
    req = PairingRequest(
        id=uuid.uuid4(),
        pairing_code=code,
        public_key=spki,
        key_alg=proto.ALG,
        key_fingerprint=fingerprint,
        platform=platform,
        app_version=app_version,
        device_info=_clean_device_info(device_info),
        status=PairingStatus.PENDING.value,
        expires_at=now + timedelta(seconds=pcfg.PAIRING_TTL_S),
        created_at=now,
    )
    db.add(req)
    await db.flush()
    return req


def effective_status(req: PairingRequest) -> str:
    if req.status == PairingStatus.PENDING.value and req.expires_at <= utcnow():
        return PairingStatus.EXPIRED.value
    return req.status


async def get_request(db: AsyncSession, request_id: Any, *, for_update: bool = False) -> PairingRequest:
    try:
        rid = uuid.UUID(str(request_id))
    except ValueError:
        raise NotFound() from None
    q = select(PairingRequest).where(PairingRequest.id == rid)
    if for_update:
        q = q.with_for_update()
    req = (await db.execute(q)).scalar_one_or_none()
    if req is None:
        raise NotFound()
    return req


# ─── Claim (painel e login no dispositivo) ───────────────────────────────────

class ClaimOutcome:
    def __init__(self, request: PairingRequest, device: Device, revoked: list[Device], screen_id: uuid.UUID) -> None:
        self.request = request
        self.device = device
        self.revoked = revoked
        self.screen_id = screen_id


async def _claim(
    db: AsyncSession, req: PairingRequest, screen: Any, method: str, actor: AuthContext, org_id: Any
) -> ClaimOutcome:
    from app.models.screens import Screen

    status = effective_status(req)
    if status == PairingStatus.EXPIRED.value:
        raise DomainError("PAIRING_CODE_EXPIRED", "Código de pareamento expirado. Gere um novo no dispositivo.", status=410)
    if status != PairingStatus.PENDING.value:
        raise Conflict("PAIRING_ALREADY_CLAIMED", "Este código de pareamento já foi utilizado.")
    if (getattr(screen, "status", None) or "").upper() == "DECOMMISSIONED":
        raise Conflict("SCREEN_DECOMMISSIONED", "Tela desativada definitivamente.")
    # serializa pareamentos concorrentes da MESMA tela
    await db.execute(select(Screen.id).where(Screen.id == screen.id).with_for_update())
    now = utcnow()
    prev = (
        await db.execute(
            select(Device).where(Device.screen_id == screen.id, Device.status == DeviceStatus.ACTIVE.value).with_for_update()
        )
    ).scalars().all()
    for d in prev:
        d.status = DeviceStatus.REVOKED.value
        d.revoked_at = now
        d.revoked_reason = "re-pareamento da tela"
        d.revoked_by_user_id = actor.user_id
        d.presence = "offline"
    await db.flush()
    used = (await db.execute(select(Device.id).where(Device.key_fingerprint == req.key_fingerprint))).first()
    if used is not None:
        raise Conflict("KEY_ALREADY_USED", "Esta chave já pertence a um dispositivo. Gere uma nova chave para parear.")
    info = req.device_info or {}
    dev = Device(
        id=uuid.uuid4(),
        screen_id=screen.id,
        public_key=req.public_key,
        key_alg=req.key_alg,
        key_fingerprint=req.key_fingerprint,
        platform=req.platform,
        app_version=req.app_version,
        os_version=(info.get("os") if isinstance(info.get("os"), str) else None),
        device_info=req.device_info,
        status=DeviceStatus.ACTIVE.value,
        paired_at=now,
        pairing_method=method,
        paired_by_user_id=actor.user_id,
        presence="offline",
    )
    db.add(dev)
    await db.flush()
    screen.device_id = dev.id
    req.status = PairingStatus.CLAIMED.value
    req.claimed_at = now
    req.claim_method = method
    req.claimed_by_user_id = actor.user_id
    req.screen_id = screen.id
    req.device_id = dev.id
    for d in prev:
        await audit(db, actor, "device.revoked", "device", d.id, before={"status": "ACTIVE"},
                    after={"status": "REVOKED", "reason": "re-pareamento", "new_device_id": str(dev.id)}, org_id=org_id)
    await audit(
        db,
        actor,
        "device.paired",
        "device",
        dev.id,
        after={
            "screen_id": str(screen.id),
            "platform": dev.platform,
            "method": method,
            "pairing_request_id": str(req.id),
            "key_fingerprint": dev.key_fingerprint,
            "revoked_devices": [str(d.id) for d in prev],
        },
        org_id=org_id,
    )
    return ClaimOutcome(req, dev, list(prev), screen.id)


async def after_claim(outcome: ClaimOutcome) -> None:
    """Efeitos pós-commit: avisa o WS de pareamento e derruba o dispositivo anterior."""
    from app.modules.presence import service as presence
    from app.modules.websocket import bus

    for d in outcome.revoked:
        await mark_revoked_runtime(d.id)
    await bus.to_pairing(outcome.request.id, "pairing_claimed", {})
    await presence.forget_scope(outcome.screen_id)
    await presence.publish_status(
        outcome.screen_id, outcome.device.id, {"status": presence.STATUS_OFFLINE, "app_version": outcome.device.app_version}
    )


async def claim_by_panel(db: AsyncSession, ctx: AuthContext, screen_id: uuid.UUID, pairing_code: str) -> ClaimOutcome:
    from app.models.screens import Screen

    info = await integ.screen_info(db, screen_id)
    if info is None:
        raise NotFound()
    ctx.ensure("screens.pair", org_id=info.org_id, establishment_id=info.establishment_id, city=info.city_key)
    try:
        code = ids.normalize_pairing_code(pairing_code)
    except ValueError:
        raise DomainError("PAIRING_CODE_INVALID", "Código de pareamento inválido.", status=422) from None
    req = (
        await db.execute(select(PairingRequest).where(PairingRequest.pairing_code == code).with_for_update())
    ).scalar_one_or_none()
    if req is None:
        raise NotFound("PAIRING_CODE_NOT_FOUND", "Código de pareamento não encontrado.")
    screen = await db.get(Screen, screen_id)
    return await _claim(db, req, screen, PairingMethod.PANEL.value, ctx, info.org_id)


async def claim_by_device_login(
    db: AsyncSession,
    request_id: Any,
    *,
    login: str,
    password: str,
    totp_code: Optional[str],
    screen_code: str,
    ip: str,
) -> ClaimOutcome:
    """Login controlado do técnico NO dispositivo (credenciais nunca armazenadas)."""
    from app.core.database import independent_session
    from app.core.passwords import dummy_verify
    from app.models.users import User
    from app.modules.auth import service as auth_svc

    await ratelimit.enforce("pair_login_ip", ip, limit=20, window_s=60)
    req = await get_request(db, request_id, for_update=True)
    _, ident = auth_svc.login_identifier(login)
    invalid = ("INVALID_CREDENTIALS", "Login ou senha inválidos.")

    async with independent_session() as s:
        user = await auth_svc.find_user_by_login(s, login)
        if user is None:
            dummy_verify()
            retry = await auth_svc.unknown_locked(ident or login)
            if retry:
                auth_svc.raise_locked(retry)
            retry = await auth_svc.register_unknown_failure(ident or login)
            if retry:
                auth_svc.raise_locked(retry)
            raise Unauthorized(*invalid)
        remaining = auth_svc.lock_remaining_s(user)
        if remaining > 0:
            auth_svc.raise_locked(remaining)
        ok, _ = auth_svc.check_password(user, password)
        user_id = user.id
        active = user.is_active and user.anonymized_at is None
    if not ok:
        fr = await auth_svc.register_failure(user_id, "password")
        if fr.just_locked:
            from app.core.config import settings

            auth_svc.raise_locked(settings.LOGIN_LOCKOUT_MINUTES * 60)
        raise Unauthorized(*invalid)
    if not active:
        raise Unauthorized(*invalid)

    second_ok = True
    async with independent_session() as s:
        user = (await s.execute(select(User).where(User.id == user_id).with_for_update())).scalar_one()
        grants = await authz.load_grants(s, user.id)
        if user.totp_enabled:
            second_ok = bool(totp_code) and (await auth_svc.verify_second_factor(s, user, totp_code or "", allow_recovery=False)) is not None
        elif auth_svc.grants_require_mfa(grants):
            raise DomainError("MFA_SETUP_REQUIRED", "Configure a verificação em duas etapas no painel antes de parear.", status=403)
        if second_ok:
            await auth_svc.reset_failures(s, user)
    if not second_ok:
        fr = await auth_svc.register_failure(user_id, "totp")
        if fr.just_locked:
            from app.core.config import settings

            auth_svc.raise_locked(settings.LOGIN_LOCKOUT_MINUTES * 60)
        raise Unauthorized("INVALID_MFA_CODE", "Código de verificação inválido.")

    user = await db.get(User, user_id)
    ctx = AuthContext(kind="user", user=user, grants=grants)
    screen = await integ.screen_by_code(db, screen_code or "")
    if screen is None:
        raise NotFound("SCREEN_NOT_FOUND", "Tela não encontrada.")
    info = await integ.screen_info(db, screen.id)
    ctx.ensure("screens.pair", org_id=info.org_id, establishment_id=info.establishment_id, city=info.city_key)
    return await _claim(db, req, screen, PairingMethod.DEVICE_LOGIN.value, ctx, info.org_id)


async def pairing_result(db: AsyncSession, request_id: Any, ts_header: Optional[str], sig_header: Optional[str]) -> dict:
    req = await get_request(db, request_id, for_update=True)
    try:
        ts_ms = int(ts_header or "")
    except ValueError:
        raise Unauthorized("BAD_SIGNATURE", "Assinatura do dispositivo ausente ou inválida.") from None
    now_ms = ms(utcnow())
    if abs(now_ms - ts_ms) > pcfg.SIGNED_TS_SKEW_MS:
        raise Unauthorized("CLOCK_SKEW", "Relógio do dispositivo fora da tolerância (±120 s). Corrija a data e hora.")
    msg = {"pairing_request_id": str(req.id), "purpose": proto.PURPOSE_PAIRING_RESULT, "ts_ms": ts_ms}
    if not sig_header or not proto.verify_es256(req.public_key, msg, sig_header):
        raise Unauthorized("BAD_SIGNATURE", "Assinatura do dispositivo ausente ou inválida.")
    status = effective_status(req)
    if status == PairingStatus.EXPIRED.value:
        raise DomainError("PAIRING_EXPIRED", "Pedido de pareamento expirado.", status=410)
    if status != PairingStatus.CLAIMED.value or req.device_id is None:
        raise Conflict("PAIRING_PENDING", "Pareamento ainda não confirmado.")
    dev = await db.get(Device, req.device_id)
    if dev is None or dev.status != DeviceStatus.ACTIVE.value:
        raise _err_revoked()
    from app.models.screens import Screen

    screen = await db.get(Screen, dev.screen_id)
    if req.result_fetched_at is None:
        req.result_fetched_at = utcnow()
    return {
        "device_id": str(dev.id),
        "screen_id": str(dev.screen_id),
        "screen_code": integ.screen_code_of(screen),
        "server_keys": integ.manifest_public_keys(),
    }


# ─── Desafio / token ─────────────────────────────────────────────────────────

def _k_challenge(challenge: str) -> str:
    return rkey("rdpp", "chal", hashlib.sha256(challenge.encode()).hexdigest())


async def create_challenge(device_id: uuid.UUID, ip: str) -> dict:
    await ratelimit.enforce("dev_chal_ip", ip, limit=120, window_s=60)
    await ratelimit.enforce("dev_chal_dev", str(device_id), limit=20, window_s=60)
    challenge = proto.b64u_encode(secrets.token_bytes(32))
    r = await get_redis()
    await r.set(_k_challenge(challenge), str(device_id), ex=pcfg.CHALLENGE_TTL_S, nx=True)
    return {"challenge": challenge, "expires_at_ms": ms(utcnow()) + pcfg.CHALLENGE_TTL_S * 1000}


async def issue_token(db: AsyncSession, device_id: uuid.UUID, challenge: str, signature: str) -> dict:
    if not isinstance(challenge, str) or len(challenge) > 64:
        raise Unauthorized("INVALID_CHALLENGE", "Desafio inválido ou expirado.")
    r = await get_redis()
    owner = await r.getdel(_k_challenge(challenge))  # uso único, mesmo se a assinatura falhar
    if owner is None or owner != str(device_id):
        raise Unauthorized("INVALID_CHALLENGE", "Desafio inválido ou expirado.")
    dev = await db.get(Device, device_id)
    if dev is None:
        raise Unauthorized("INVALID_DEVICE", "Dispositivo desconhecido.")
    msg = {"challenge": challenge, "device_id": str(device_id), "purpose": proto.PURPOSE_AUTH}
    if not proto.verify_es256(dev.public_key, msg, signature or ""):
        raise Unauthorized("BAD_SIGNATURE", "Assinatura inválida.")
    if dev.status != DeviceStatus.ACTIVE.value:
        raise _err_revoked()
    info = await integ.screen_info(db, dev.screen_id)
    sid = str(uuid.uuid4())
    token, _ = tokens.encode(
        str(dev.id),
        tokens.TYP_DEVICE,
        timedelta(seconds=pcfg.DEVICE_TOKEN_TTL_S),
        {"scr": str(dev.screen_id), "org": str(info.org_id) if info and info.org_id else None, "sid": sid},
    )
    return {"access_token": token, "expires_in": pcfg.DEVICE_TOKEN_TTL_S, "server_time_ms": ms(utcnow())}


async def authenticate_device(db: AsyncSession, claims: dict, request: Request) -> AuthContext:
    """Autenticador registrado na fundação (JWT ``typ=device``)."""
    try:
        device_id = uuid.UUID(claims["sub"])
        screen_id = uuid.UUID(claims["scr"])
    except (KeyError, ValueError, TypeError):
        raise Unauthorized("INVALID_TOKEN", "Token inválido.") from None
    if await is_revoked_cached(device_id):
        raise _err_revoked()
    dev = await db.get(Device, device_id)
    if dev is None:
        raise Unauthorized("INVALID_TOKEN", "Token inválido.")
    if dev.status != DeviceStatus.ACTIVE.value:
        raise _err_revoked()
    if dev.screen_id != screen_id:
        raise Unauthorized("INVALID_TOKEN", "Token inválido.")
    org = claims.get("org")
    ctx = AuthContext(
        kind="device",
        claims=claims,
        token_typ=tokens.TYP_DEVICE,
        device_id=device_id,
        screen_id=screen_id,
        device_org_id=uuid.UUID(org) if org else None,
        origin="player",
    )
    request.state.device = dev
    return ctx


authz.register_device_authenticator(authenticate_device)


# ─── Revogação ───────────────────────────────────────────────────────────────

async def mark_revoked_runtime(device_id: Any) -> None:
    """Efeitos imediatos (pós-commit) da revogação: Redis + WS fechado + presença."""
    from app.modules.presence import service as presence
    from app.modules.websocket import bus

    r = await get_redis()
    await r.set(k_revoked(device_id), "1", ex=7 * 24 * 3600)
    await bus.to_device(device_id, "device_revoked", {})
    await presence.mark_revoked(device_id)


async def revoke_device(db: AsyncSession, ctx: AuthContext, device: Device, reason: str, org_id: Any) -> bool:
    from app.models.screens import Screen

    if device.status != DeviceStatus.ACTIVE.value:
        return False
    device.status = DeviceStatus.REVOKED.value
    device.revoked_at = utcnow()
    device.revoked_reason = (reason or "revogado pelo painel")[:255]
    device.revoked_by_user_id = ctx.user_id
    device.presence = "offline"
    screen = await db.get(Screen, device.screen_id)
    if screen is not None and screen.device_id == device.id:
        screen.device_id = None
    await audit(db, ctx, "device.revoked", "device", device.id, before={"status": "ACTIVE"},
                after={"status": "REVOKED"}, reason=reason, org_id=org_id)
    return True


# ─── Ticket de WS do player ──────────────────────────────────────────────────

def _k_ticket(ticket: str) -> str:
    return rkey("rdpp", "wst", hashlib.sha256(ticket.encode()).hexdigest())


async def create_ws_ticket(ctx: AuthContext) -> dict:
    ticket = secrets.token_urlsafe(32)
    info = {"device_id": str(ctx.device_id), "screen_id": str(ctx.screen_id), "sid": ctx.claims.get("sid")}
    r = await get_redis()
    await r.set(_k_ticket(ticket), json.dumps(info), ex=pcfg.WS_TICKET_TTL_S, nx=True)
    return {"ticket": ticket, "expires_in": pcfg.WS_TICKET_TTL_S}


async def consume_ws_ticket(ticket: str) -> Optional[dict]:
    if not ticket or len(ticket) > 128:
        return None
    r = await get_redis()
    raw = await r.getdel(_k_ticket(ticket))
    if not raw:
        return None
    try:
        return json.loads(raw)
    except ValueError:
        return None


# ─── Efeitos pós-commit e remoção definitiva de telas (W-ORG) ────────────────

_bg_tasks: set = set()


def _fire_after_commit(session) -> None:  # noqa: ANN001 — evento síncrono do SQLAlchemy
    import asyncio

    items = session.info.pop("wplay_after_commit", [])
    if not items:
        return
    loop = asyncio.get_running_loop()
    for factory in items:
        t = loop.create_task(factory())
        _bg_tasks.add(t)
        t.add_done_callback(_bg_tasks.discard)


def _clear_after_commit(session) -> None:  # noqa: ANN001
    session.info.pop("wplay_after_commit", None)


def after_commit(db: AsyncSession, factory) -> None:
    """Agenda ``await factory()`` para DEPOIS do commit da transação de ``db`` (descartado no rollback)."""
    from sqlalchemy import event

    sess = db.sync_session
    sess.info.setdefault("wplay_after_commit", []).append(factory)
    if not sess.info.get("wplay_hooked"):
        sess.info["wplay_hooked"] = True
        event.listen(sess, "after_commit", _fire_after_commit)
        event.listen(sess, "after_rollback", _clear_after_commit)


async def on_screens_removed(db: AsyncSession, screen_ids: list, reason: str) -> None:
    """Listener do W-ORG (retirada definitiva / estabelecimento inativado): revoga o(s)
    dispositivo(s) ativo(s) na MESMA transação; após o commit fecha o WS (``device_revoked``)."""
    from app.models.screens import Screen

    ids = [uuid.UUID(str(x)) for x in screen_ids]
    if not ids:
        return
    devs = (
        await db.execute(
            select(Device).where(Device.screen_id.in_(ids), Device.status == DeviceStatus.ACTIVE.value).with_for_update()
        )
    ).scalars().all()
    if not devs:
        return
    now = utcnow()
    sysctx = AuthContext.system("system")
    for d in devs:
        d.status = DeviceStatus.REVOKED.value
        d.revoked_at = now
        d.revoked_reason = f"tela removida: {reason}"[:255]
        d.presence = "offline"
        screen = await db.get(Screen, d.screen_id)
        if screen is not None and screen.device_id == d.id:
            screen.device_id = None
        await audit(db, sysctx, "device.revoked", "device", d.id, before={"status": "ACTIVE"},
                    after={"status": "REVOKED", "screen_id": str(d.screen_id)}, reason=reason)
    revoked = [d.id for d in devs]

    async def _publish() -> None:
        for did in revoked:
            try:
                await mark_revoked_runtime(did)
            except Exception as exc:  # pragma: no cover
                log.error("revoke_runtime_failed", error_type=type(exc).__name__)

    after_commit(db, _publish)


def _register_screen_listener() -> None:
    try:
        from app.modules.screens.service import register_screen_removed_listener
    except ImportError:  # pragma: no cover
        log.error("screen_removed_listener_unavailable")
        return
    register_screen_removed_listener(on_screens_removed)


_register_screen_listener()
