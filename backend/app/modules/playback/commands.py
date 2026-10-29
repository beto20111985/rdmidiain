"""
RD Mídia Indoor — Comandos remotos persistidos (RDPP/1 §8; ETAPA2 #1386–#1391; P001 §73).

- Persistidos em ``device_commands`` (emissor, motivo, TTL, entrega, ack, resultado) e
  auditados; ``reboot``/``clear_cache``/``update_app`` exigem confirmação explícita.
- Entregues pelo WS (``command {command_id, command, args, expires_at_ms}``) e
  REENTREGUES a cada reconexão até ``command_ack`` final (DONE/FAILED/UNSUPPORTED)
  ou expiração.
"""
from __future__ import annotations

import hashlib
import json
import re
import secrets
import uuid
from datetime import timedelta
from typing import Any, Optional

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.audit import audit
from app.core.authz import AuthContext
from app.core.errors import Conflict, DomainError
from app.core.redis import get_redis, rkey
from app.core.timeutils import utcnow
from app.models.devices import COMMAND_FINAL_STATUSES, CommandStatus, Device, DeviceCommand, DeviceStatus
from app.modules.playback import config as pcfg

COMMANDS = ("restart_app", "resync", "clear_cache", "redownload", "update_app", "reboot", "reload_manifest", "set_tech_pin")
PIN_ITERATIONS = 210_000
_PIN_RE = re.compile(r"^[0-9]{6,12}$")
CONFIRM_REQUIRED = frozenset({"reboot", "clear_cache", "update_app"})
ACK_STATUSES = frozenset({"RECEIVED", "DONE", "FAILED", "UNSUPPORTED"})
OPEN_STATUSES = (CommandStatus.PENDING.value, CommandStatus.DELIVERED.value, CommandStatus.RECEIVED.value)


def _ms(dt) -> int:
    return int(dt.timestamp() * 1000)


def command_data(cmd: DeviceCommand) -> dict:
    return {
        "command_id": str(cmd.id),
        "command": cmd.command,
        "args": cmd.args or {},
        "expires_at_ms": _ms(cmd.expires_at),
    }


def tech_pin_args(pin: str) -> dict:
    """PBKDF2-SHA256 (210 000 iterações, sal de 16 bytes). O PIN em claro nunca é guardado nem enviado."""
    if not isinstance(pin, str) or not _PIN_RE.match(pin):
        raise DomainError("INVALID_PIN", "O PIN técnico deve ter de 6 a 12 dígitos.", status=422)
    from app.modules.playback.protocol import b64u_encode

    salt = secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac("sha256", pin.encode("utf-8"), salt, PIN_ITERATIONS, dklen=32)
    return {"algo": "pbkdf2-sha256", "iterations": PIN_ITERATIONS, "salt_b64u": b64u_encode(salt), "hash_b64u": b64u_encode(digest)}


def _public_args(cmd: DeviceCommand) -> dict:
    args = cmd.args or {}
    if cmd.command == "set_tech_pin":  # nunca expor sal/hash no painel
        return {"algo": args.get("algo"), "iterations": args.get("iterations")}
    return args


def command_public(cmd: DeviceCommand, names: Optional[dict] = None) -> dict:
    """``names`` = {user_id: nome} resolvido em lote por ``commands_public`` (sem N+1)."""
    def iso(d):
        return d.isoformat().replace("+00:00", "Z") if d else None

    return {
        "id": str(cmd.id),
        "device_id": str(cmd.device_id),
        "screen_id": str(cmd.screen_id),
        "command": cmd.command,
        "args": _public_args(cmd),
        "reason": cmd.reason,
        "confirmed": cmd.confirmed,
        "issued_by_user_id": str(cmd.issued_by_user_id) if cmd.issued_by_user_id else None,
        "issued_by_name": (names or {}).get(cmd.issued_by_user_id) if cmd.issued_by_user_id else None,
        "status": cmd.status,
        "created_at": iso(cmd.created_at),
        "expires_at": iso(cmd.expires_at),
        "delivered_at": iso(cmd.delivered_at),
        "delivery_count": cmd.delivery_count,
        "acked_at": iso(cmd.acked_at),
        "ack_status": cmd.ack_status,
        "result_detail": cmd.result_detail,
    }


async def commands_public(db: AsyncSession, cmds: list) -> list[dict]:
    """Serializa comandos com o NOME do autor resolvido numa única consulta."""
    from app.models.users import User

    ids = {c.issued_by_user_id for c in cmds if c.issued_by_user_id}
    names: dict = {}
    if ids:
        names = dict((await db.execute(select(User.id, User.full_name).where(User.id.in_(list(ids))))).all())
    return [command_public(c, names) for c in cmds]


def _clean_args(command: str, args: Any) -> dict:
    if args is None:
        return {}
    if not isinstance(args, dict) or len(json.dumps(args, default=str)) > 2000:
        raise DomainError("INVALID_COMMAND_ARGS", "Argumentos do comando inválidos.", status=422)
    return args


async def _idempotent(ctx: AuthContext, key: Optional[str], body: dict) -> tuple[Optional[str], Optional[str]]:
    if not key:
        return None, None
    if len(key) > 100:
        raise DomainError("INVALID_IDEMPOTENCY_KEY", "Idempotency-Key inválida.", status=422)
    h = hashlib.sha256(json.dumps(body, sort_keys=True, default=str).encode()).hexdigest()
    rk = rkey("rdpp", "cmd_idem", ctx.user_id, hashlib.sha256(key.encode()).hexdigest())
    r = await get_redis()
    raw = await r.get(rk)
    if raw:
        stored = json.loads(raw)
        if stored["h"] != h:
            raise Conflict("IDEMPOTENCY_KEY_REUSED", "Idempotency-Key reutilizada com outro conteúdo.")
        return stored["id"], rk
    return None, rk


async def issue_command(
    db: AsyncSession,
    ctx: AuthContext,
    screen_id: uuid.UUID,
    *,
    command: str,
    args: Any,
    reason: str,
    confirm: bool,
    ttl_s: Optional[int],
    idempotency_key: Optional[str],
    org_id: Any,
    idem_body: Optional[dict] = None,
) -> tuple[DeviceCommand, bool]:
    """Cria o comando (retorna ``(cmd, created)``). Quem chama faz commit e publica."""
    if command not in COMMANDS:
        raise DomainError("INVALID_COMMAND", "Comando inválido.", status=422, details={"allowed": list(COMMANDS)})
    if not reason or len(reason.strip()) < 3:
        raise DomainError("REASON_REQUIRED", "Informe o motivo do comando.", status=422)
    if command in CONFIRM_REQUIRED and not confirm:
        raise DomainError(
            "CONFIRMATION_REQUIRED",
            "Este comando exige confirmação explícita (confirm=true).",
            status=400,
            details={"command": command},
        )
    ttl = pcfg.COMMAND_DEFAULT_TTL_S if ttl_s is None else int(ttl_s)
    if not (60 <= ttl <= pcfg.COMMAND_MAX_TTL_S):
        raise DomainError("INVALID_TTL", "TTL do comando fora do intervalo (60 s a 7 dias).", status=422)
    clean = _clean_args(command, args)
    body = idem_body or {"screen_id": str(screen_id), "command": command, "args": clean, "reason": reason, "confirm": confirm,
                         "ttl": ttl}
    existing_id, idem_key = await _idempotent(ctx, idempotency_key, body)
    if existing_id:
        cmd = await db.get(DeviceCommand, uuid.UUID(existing_id))
        if cmd is not None:
            return cmd, False
    dev = (
        await db.execute(select(Device).where(Device.screen_id == screen_id, Device.status == DeviceStatus.ACTIVE.value))
    ).scalar_one_or_none()
    if dev is None:
        raise Conflict("NO_ACTIVE_DEVICE", "A tela não possui dispositivo pareado.")
    now = utcnow()
    cmd = DeviceCommand(
        id=uuid.uuid4(),
        device_id=dev.id,
        screen_id=screen_id,
        command=command,
        args=clean,
        reason=reason.strip()[:2000],
        confirmed=bool(confirm),
        issued_by_user_id=ctx.user_id,
        status=CommandStatus.PENDING.value,
        created_at=now,
        expires_at=now + timedelta(seconds=ttl),
        delivery_count=0,
    )
    db.add(cmd)
    await db.flush()
    await audit(db, ctx, "device.command_issued", "device_command", cmd.id,
                after={"command": command, "device_id": str(dev.id), "screen_id": str(screen_id), "confirmed": bool(confirm),
                       "args": _public_args(cmd)}, reason=reason, org_id=org_id)
    if idem_key:
        r = await get_redis()
        h = hashlib.sha256(json.dumps(body, sort_keys=True, default=str).encode()).hexdigest()
        await r.set(idem_key, json.dumps({"id": str(cmd.id), "h": h}), ex=24 * 3600)
    return cmd, True


async def open_commands(db: AsyncSession, device_id: uuid.UUID) -> list[DeviceCommand]:
    now = utcnow()
    return list(
        (
            await db.execute(
                select(DeviceCommand)
                .where(
                    DeviceCommand.device_id == device_id,
                    DeviceCommand.status.in_(OPEN_STATUSES),
                    DeviceCommand.expires_at > now,
                )
                .order_by(DeviceCommand.created_at)
            )
        ).scalars().all()
    )


async def mark_delivered(db: AsyncSession, command_id: uuid.UUID) -> None:
    now = utcnow()
    await db.execute(
        update(DeviceCommand)
        .where(DeviceCommand.id == command_id)
        .values(
            delivered_at=now,
            delivery_count=DeviceCommand.delivery_count + 1,
            status=_delivered_status(),
        )
    )


def _delivered_status():
    from sqlalchemy import case

    # só PENDING avança para DELIVERED; RECEIVED e estados finais (ack já processado) são preservados
    return case(
        (DeviceCommand.status == CommandStatus.PENDING.value, CommandStatus.DELIVERED.value),
        else_=DeviceCommand.status,
    )


async def apply_ack(db: AsyncSession, device_id: uuid.UUID, data: Any) -> Optional[DeviceCommand]:
    """``command_ack {command_id, status, detail}`` do player (somente do dispositivo destinatário)."""
    if not isinstance(data, dict):
        return None
    st = data.get("status")
    try:
        cid = uuid.UUID(str(data.get("command_id")))
    except ValueError:
        return None
    if st not in ACK_STATUSES:
        return None
    cmd = (
        await db.execute(select(DeviceCommand).where(DeviceCommand.id == cid, DeviceCommand.device_id == device_id).with_for_update())
    ).scalar_one_or_none()
    if cmd is None or cmd.status in COMMAND_FINAL_STATUSES:
        return cmd
    now = utcnow()
    detail = data.get("detail")
    cmd.ack_status = st
    cmd.acked_at = now
    if detail is not None:
        cmd.result_detail = str(detail)[:2000]
    cmd.status = st
    if st in ("DONE", "FAILED", "UNSUPPORTED"):
        await audit(db, AuthContext(kind="device", device_id=device_id, origin="player"), "device.command_result",
                    "device_command", cmd.id, after={"status": st, "detail": cmd.result_detail})
    return cmd


async def expire_commands(db: AsyncSession) -> int:
    res = await db.execute(
        update(DeviceCommand)
        .where(DeviceCommand.status.in_(OPEN_STATUSES), DeviceCommand.expires_at <= utcnow())
        .values(status=CommandStatus.EXPIRED.value)
    )
    return res.rowcount or 0
