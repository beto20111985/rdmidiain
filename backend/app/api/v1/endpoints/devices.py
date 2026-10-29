"""
RD Mídia Indoor — Administração de dispositivos (players), pareamento pelo painel,
presença, comandos remotos e versões do app (W-PLAY).

Rotas (``/api/v1``):
- ``GET  /devices``                         lista (escopo ``screens.view``)
- ``GET  /devices/releases`` · ``POST /devices/releases`` · ``PATCH /devices/releases/{id}``
  (versões do app; ``screens.manage`` GLOBAL)
- ``GET  /devices/{id}``                    detalhe + presença (Redis)
- ``POST /devices/{id}/revoke``             revoga (``screens.manage`` no escopo) e fecha o WS
- ``PATCH /devices/{id}``                   grupo de rollout (``screens.manage``)
- ``POST /screens/{screen_id}/pairing``     claim pelo painel (``screens.pair`` no escopo da tela)
- ``GET  /screens/{screen_id}/presence``    estado atual (``screens.view``)
- ``POST /screens/{screen_id}/commands``    comando remoto (``screens.command``; confirmação p/ reboot/clear_cache/update_app)
- ``GET  /screens/{screen_id}/commands``    histórico paginado (``screens.view``)
"""
from __future__ import annotations

import uuid
from typing import Any, Optional

from fastapi import APIRouter, Depends, Header, Query
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.authz import AuthContext, require
from app.core.database import get_db
from app.core.errors import NotFound
from app.models.devices import AppRelease, Device, DeviceCommand, DeviceStatus
from app.modules.playback import commands as cmd_svc
from app.modules.playback import maintenance as _maintenance  # noqa: F401  (registra configurações do domínio)
from app.modules.playback import syncevents as _syncevents  # noqa: F401  (registra configurações do domínio)
from app.modules.playback import identity
from app.modules.playback import integration as integ
from app.modules.playback import updates
from app.modules.websocket import bus

router = APIRouter()
screen_router = APIRouter()


def _iso(d) -> Optional[str]:
    return d.isoformat().replace("+00:00", "Z") if d else None


def device_public(d: Device, presence_snap: Optional[dict] = None) -> dict:
    out = {
        "id": str(d.id),
        "screen_id": str(d.screen_id),
        "platform": d.platform,
        "app_version": d.app_version,
        "os_version": d.os_version,
        "device_info": d.device_info,
        "update_group": d.update_group,
        "status": d.status,
        "key_fingerprint": d.key_fingerprint,
        "paired_at": _iso(d.paired_at),
        "pairing_method": d.pairing_method,
        "revoked_at": _iso(d.revoked_at),
        "revoked_reason": d.revoked_reason,
        "presence": d.presence,
        "player_state": d.player_state,
        "last_seen_at": _iso(d.last_seen_at),
        "last_sync_at": _iso(d.last_sync_at),
        "last_play_at": _iso(d.last_play_at),
        "applied_manifest_version": d.applied_manifest_version,
        "pending_manifest_version": d.pending_manifest_version,
        "last_error": d.last_error,
        "telemetry": d.telemetry,
        "telemetry_at": _iso(d.telemetry_at),
    }
    if presence_snap is not None:
        out["live"] = _live(presence_snap)
    return out


def _live(snap: dict) -> dict:
    from app.modules.presence import service as presence

    if not snap:
        return {"status": presence.STATUS_OFFLINE}
    def _i(k):
        v = snap.get(k)
        return int(v) if v not in (None, "") and str(v).lstrip("-").isdigit() else None

    return {
        "status": snap.get("status") or presence.STATUS_CONNECTED,
        "player_state": snap.get("state") or None,
        "manifest_version_applied": _i("applied"),
        "manifest_version_pending": _i("pending"),
        "sync": presence.sync_status(_i("applied"), _i("pending")),
        "last_seen_ms": _i("last_seen_ms"),
        "last_sync_ms": _i("last_sync_ms"),
        "last_play_ms": _i("last_play_ms"),
        "current_code": snap.get("current_code"),
        "clock_offset_ms": _i("clock_offset_ms"),
        "download": presence.download_of(snap),
        "app_version": snap.get("app_version"),
    }


async def _screen_scoped(db: AsyncSession, ctx: AuthContext, perm: str, screen_id: uuid.UUID) -> integ.ScreenInfo:
    info = await integ.screen_info(db, screen_id)
    if info is None:
        raise NotFound()
    ctx.ensure(perm, org_id=info.org_id, establishment_id=info.establishment_id, city=info.city_key)
    return info


async def _device_scoped(db: AsyncSession, ctx: AuthContext, perm: str, device_id: uuid.UUID) -> tuple[Device, integ.ScreenInfo]:
    dev = await db.get(Device, device_id)
    if dev is None:
        raise NotFound()
    info = await _screen_scoped(db, ctx, perm, dev.screen_id)
    return dev, info


# ─── Versões do app (antes de /{device_id}) ──────────────────────────────────

class ReleaseIn(BaseModel):
    platform: str
    version: str
    url: str = Field(..., max_length=2000)
    sha256: str
    size: int
    signature: str = Field(..., max_length=100)
    key_id: str = Field(..., max_length=64)
    mandatory: bool = False
    notes: Optional[str] = Field(None, max_length=2000)


class ReleasePatch(BaseModel):
    status: Optional[str] = None
    rollout_percent: Optional[int] = None
    target_groups: Optional[list[str]] = None
    max_concurrent: Optional[int] = None
    mandatory: Optional[bool] = None
    reason: Optional[str] = Field(None, max_length=500)


@router.get("/releases")
async def list_releases(
    platform: Optional[str] = Query(None, max_length=20),
    ctx: AuthContext = Depends(require("screens.manage", global_only=True)),
    db: AsyncSession = Depends(get_db),
):
    q = select(AppRelease).order_by(AppRelease.created_at.desc())
    if platform:
        q = q.where(AppRelease.platform == platform)
    rows = (await db.execute(q)).scalars().all()
    return {"items": [updates.release_public(r) for r in rows], "total": len(rows), "page": 1, "page_size": len(rows)}


@router.post("/releases", status_code=201)
async def create_release(
    body: ReleaseIn,
    ctx: AuthContext = Depends(require("screens.manage", global_only=True, allow_impersonation=False)),
    db: AsyncSession = Depends(get_db),
):
    rel = await updates.create_release(db, ctx, body.model_dump())
    return updates.release_public(rel)


@router.patch("/releases/{release_id}")
async def patch_release(
    release_id: uuid.UUID,
    body: ReleasePatch,
    ctx: AuthContext = Depends(require("screens.manage", global_only=True, allow_impersonation=False)),
    db: AsyncSession = Depends(get_db),
):
    rel = await updates.update_release(db, ctx, release_id, body.model_dump(exclude_unset=True))
    return updates.release_public(rel)


# ─── Dispositivos ────────────────────────────────────────────────────────────

@router.get("")
async def list_devices(
    screen_id: Optional[uuid.UUID] = None,
    status: Optional[str] = Query(None, max_length=20),
    presence: Optional[str] = Query(None, max_length=20),
    page: int = Query(1, ge=1),
    page_size: int = Query(25, ge=1, le=100),
    ctx: AuthContext = Depends(require("screens.view")),
    db: AsyncSession = Depends(get_db),
):
    from app.core.authz import city_key  # noqa: F401
    from app.models.establishments import Establishment
    from app.models.screens import Screen

    base = (
        select(Device)
        .join(Screen, Screen.id == Device.screen_id)
        .join(Establishment, Establishment.id == Screen.establishment_id)
    )
    city_col = getattr(Establishment, "city_key", None)
    base = base.where(
        ctx.scope_filter("screens.view", org_col=Establishment.org_id, establishment_col=Establishment.id, city_col=city_col)
    )
    if screen_id:
        base = base.where(Device.screen_id == screen_id)
    if status:
        base = base.where(Device.status == status)
    if presence:
        base = base.where(Device.presence == presence)
    total = (await db.execute(select(func.count()).select_from(base.subquery()))).scalar_one()
    rows = (
        await db.execute(base.order_by(Device.paired_at.desc()).offset((page - 1) * page_size).limit(page_size))
    ).scalars().all()
    return {"items": [device_public(d) for d in rows], "total": int(total), "page": page, "page_size": page_size}


@router.get("/{device_id}")
async def get_device(
    device_id: uuid.UUID, ctx: AuthContext = Depends(require("screens.view")), db: AsyncSession = Depends(get_db)
):
    from app.modules.presence import service as presence

    dev, _ = await _device_scoped(db, ctx, "screens.view", device_id)
    snap = await presence.get_snapshot(dev.id) if dev.status == DeviceStatus.ACTIVE.value else {}
    return device_public(dev, snap)


class RevokeIn(BaseModel):
    reason: str = Field(..., min_length=3, max_length=255)


@router.post("/{device_id}/revoke")
async def revoke_device(
    device_id: uuid.UUID,
    body: RevokeIn,
    ctx: AuthContext = Depends(require("screens.manage")),
    db: AsyncSession = Depends(get_db),
):
    from app.modules.presence import service as presence

    dev, info = await _device_scoped(db, ctx, "screens.manage", device_id)
    changed = await identity.revoke_device(db, ctx, dev, body.reason, info.org_id)
    await db.commit()
    if changed:
        await identity.mark_revoked_runtime(dev.id)
        await presence.publish_status(dev.screen_id, dev.id, {"status": presence.STATUS_OFFLINE})
    return device_public(dev)


class DevicePatch(BaseModel):
    update_group: Optional[str] = Field(None, max_length=40)


@router.patch("/{device_id}")
async def patch_device(
    device_id: uuid.UUID,
    body: DevicePatch,
    ctx: AuthContext = Depends(require("screens.manage")),
    db: AsyncSession = Depends(get_db),
):
    from app.core.audit import audit

    dev, info = await _device_scoped(db, ctx, "screens.manage", device_id)
    before = {"update_group": dev.update_group}
    dev.update_group = body.update_group or None
    await audit(db, ctx, "device.updated", "device", dev.id, before=before, after={"update_group": dev.update_group},
                org_id=info.org_id)
    return device_public(dev)


# ─── Rotas por tela (pareamento pelo painel, presença, comandos) ─────────────

class PanelPairIn(BaseModel):
    pairing_code: str = Field(..., max_length=12)


@screen_router.post("/screens/{screen_id}/pairing", status_code=201)
async def pair_from_panel(
    screen_id: uuid.UUID,
    body: PanelPairIn,
    ctx: AuthContext = Depends(require("screens.pair")),
    db: AsyncSession = Depends(get_db),
):
    outcome = await identity.claim_by_panel(db, ctx, screen_id, body.pairing_code)
    await db.commit()
    await identity.after_claim(outcome)
    return {
        "device": device_public(outcome.device),
        "revoked_device_ids": [str(d.id) for d in outcome.revoked],
        "pairing_request_id": str(outcome.request.id),
    }


@screen_router.get("/screens/{screen_id}/presence")
async def screen_presence(
    screen_id: uuid.UUID, ctx: AuthContext = Depends(require("screens.view")), db: AsyncSession = Depends(get_db)
):
    from app.modules.presence import service as presence

    info = await _screen_scoped(db, ctx, "screens.view", screen_id)
    dev = (
        await db.execute(select(Device).where(Device.screen_id == screen_id, Device.status == DeviceStatus.ACTIVE.value))
    ).scalar_one_or_none()
    if dev is None:
        return {"screen_id": str(screen_id), "screen_code": info.code, "paired": False, "device": None,
                "live": {"status": presence.STATUS_OFFLINE}}
    snap = await presence.get_snapshot(dev.id)
    return {"screen_id": str(screen_id), "screen_code": info.code, "paired": True, "device": device_public(dev),
            "live": _live(snap)}


class CommandIn(BaseModel):
    command: str = Field(..., max_length=30)
    args: Optional[dict] = None
    reason: str = Field(..., min_length=3, max_length=2000)
    confirm: bool = False
    ttl_s: Optional[int] = None


@screen_router.post("/screens/{screen_id}/commands", status_code=202)
async def send_command(
    screen_id: uuid.UUID,
    body: CommandIn,
    idempotency_key: Optional[str] = Header(None, alias="Idempotency-Key"),
    ctx: AuthContext = Depends(require("screens.command")),
    db: AsyncSession = Depends(get_db),
):
    info = await _screen_scoped(db, ctx, "screens.command", screen_id)
    if body.command == "set_tech_pin":
        from app.core.errors import DomainError

        raise DomainError("USE_TECH_PIN_ENDPOINT", "Use POST /screens/{id}/tech-pin para definir o PIN técnico.", status=422)
    cmd, created = await cmd_svc.issue_command(
        db, ctx, screen_id, command=body.command, args=body.args, reason=body.reason, confirm=body.confirm,
        ttl_s=body.ttl_s, idempotency_key=idempotency_key, org_id=info.org_id,
    )
    await db.commit()
    if created:
        # entrega imediata se conectado (qualquer processo); senão fica para a reconexão
        await bus.to_device(cmd.device_id, "command", cmd_svc.command_data(cmd))
    return (await cmd_svc.commands_public(db, [cmd]))[0]


@screen_router.get("/screens/{screen_id}/commands")
async def list_commands(
    screen_id: uuid.UUID,
    page: int = Query(1, ge=1),
    page_size: int = Query(25, ge=1, le=100),
    ctx: AuthContext = Depends(require("screens.view")),
    db: AsyncSession = Depends(get_db),
):
    await _screen_scoped(db, ctx, "screens.view", screen_id)
    base = select(DeviceCommand).where(DeviceCommand.screen_id == screen_id)
    total = (await db.execute(select(func.count()).select_from(base.subquery()))).scalar_one()
    rows = (
        await db.execute(base.order_by(DeviceCommand.created_at.desc()).offset((page - 1) * page_size).limit(page_size))
    ).scalars().all()
    return {"items": await cmd_svc.commands_public(db, list(rows)), "total": int(total), "page": page, "page_size": page_size}


# ─── PIN técnico (comando set_tech_pin — RDPP §4) ────────────────────────────

class TechPinIn(BaseModel):
    pin: str = Field(..., min_length=6, max_length=12)
    reason: str = Field(..., min_length=3, max_length=2000)
    ttl_s: Optional[int] = None


async def _issue_pin(db: AsyncSession, ctx: AuthContext, screen_id: uuid.UUID, body: TechPinIn, org_id: Any):
    args = cmd_svc.tech_pin_args(body.pin)
    return await cmd_svc.issue_command(
        db, ctx, screen_id, command="set_tech_pin", args=args, reason=body.reason, confirm=True, ttl_s=body.ttl_s,
        idempotency_key=None, org_id=org_id,
    )


@screen_router.post("/screens/{screen_id}/tech-pin", status_code=202)
async def set_screen_tech_pin(
    screen_id: uuid.UUID,
    body: TechPinIn,
    ctx: AuthContext = Depends(require("screens.command", allow_impersonation=False)),
    db: AsyncSession = Depends(get_db),
):
    """Define o PIN técnico do player (menu técnico/saída do kiosk). O servidor gera
    sal+hash PBKDF2-SHA256 (210 000 it.) e NUNCA envia nem guarda o PIN em claro."""
    info = await _screen_scoped(db, ctx, "screens.command", screen_id)
    cmd, _ = await _issue_pin(db, ctx, screen_id, body, info.org_id)
    await db.commit()
    await bus.to_device(cmd.device_id, "command", cmd_svc.command_data(cmd))
    return (await cmd_svc.commands_public(db, [cmd]))[0]


@screen_router.post("/establishments/{establishment_id}/tech-pin", status_code=202)
async def set_establishment_tech_pin(
    establishment_id: uuid.UUID,
    body: TechPinIn,
    ctx: AuthContext = Depends(require("screens.command", allow_impersonation=False)),
    db: AsyncSession = Depends(get_db),
):
    """Enfileira ``set_tech_pin`` para TODAS as telas pareadas do estabelecimento (sal próprio por tela)."""
    from app.core.authz import city_key
    from app.models.establishments import Establishment
    from app.models.screens import Screen

    est = await db.get(Establishment, establishment_id)
    if est is None:
        raise NotFound()
    ck = getattr(est, "city_key", None) or (city_key(est.state, est.city) if est.state and est.city else None)
    ctx.ensure("screens.command", org_id=est.org_id, establishment_id=est.id, city=ck)
    screen_ids = (
        await db.execute(
            select(Device.screen_id)
            .join(Screen, Screen.id == Device.screen_id)
            .where(Screen.establishment_id == est.id, Device.status == DeviceStatus.ACTIVE.value)
        )
    ).scalars().all()
    cmds = [(await _issue_pin(db, ctx, sid, body, est.org_id))[0] for sid in screen_ids]
    await db.commit()
    for c in cmds:
        await bus.to_device(c.device_id, "command", cmd_svc.command_data(c))
    return {"items": await cmd_svc.commands_public(db, cmds), "total": len(cmds), "page": 1, "page_size": len(cmds)}


# ─── Presença em LOTE (listas do painel: 1 consulta + 1 pipeline Redis) ──────

@screen_router.get("/screens/presence")
async def screens_presence_batch(
    screen_id: list[uuid.UUID] = Query(..., max_length=100),
    ctx: AuthContext = Depends(require("screens.view")),
    db: AsyncSession = Depends(get_db),
):
    """Estado de presença de até 100 telas (somente as do escopo do usuário; demais omitidas)."""
    from app.core.errors import DomainError
    from app.core.redis import get_redis
    from app.models.establishments import Establishment
    from app.models.screens import Screen
    from app.modules.presence import service as presence

    ids = list(dict.fromkeys(screen_id))
    if not ids or len(ids) > 100:
        raise DomainError("VALIDATION_ERROR", "Informe de 1 a 100 telas.", status=422)
    city_col = getattr(Establishment, "city_key", None)
    rows = (
        await db.execute(
            select(Screen.id, Device)
            .join(Establishment, Establishment.id == Screen.establishment_id)
            .outerjoin(Device, (Device.screen_id == Screen.id) & (Device.status == DeviceStatus.ACTIVE.value))
            .where(
                Screen.id.in_(ids),
                ctx.scope_filter("screens.view", org_col=Establishment.org_id, establishment_col=Establishment.id,
                                 city_col=city_col),
            )
        )
    ).all()
    by_screen = {sid: dev for sid, dev in rows}
    devs = [d for d in by_screen.values() if d is not None]
    snaps: dict = {}
    if devs:
        r = await get_redis()
        pipe = r.pipeline(transaction=False)
        for d in devs:
            pipe.hgetall(presence.k_dev(d.id))
        for d, snap in zip(devs, await pipe.execute()):
            snaps[d.id] = snap or {}
    items = []
    for sid in ids:
        if sid not in by_screen:
            continue  # inexistente ou fora do escopo: não vaza existência
        d = by_screen[sid]
        if d is None:
            items.append({"screen_id": str(sid), "paired": False, "device_id": None, "status": presence.STATUS_OFFLINE,
                          "player_state": None, "last_seen_at": None, "app_version": None,
                          "manifest_version_applied": None, "manifest_version_pending": None, "download": None})
            continue
        live = _live(snaps.get(d.id) or {})
        seen_ms = live.get("last_seen_ms")
        items.append({
            "screen_id": str(sid),
            "paired": True,
            "device_id": str(d.id),
            "status": live["status"],
            "player_state": live.get("player_state") or d.player_state,
            "last_seen_at": (_iso_ms(seen_ms) if seen_ms else _iso(d.last_seen_at)),
            "app_version": live.get("app_version") or d.app_version,
            "manifest_version_applied": (live.get("manifest_version_applied")
                                         if live.get("manifest_version_applied") is not None else d.applied_manifest_version),
            "manifest_version_pending": live.get("manifest_version_pending"),
            "download": live.get("download"),
        })
    return {"items": items, "total": len(items), "page": 1, "page_size": len(items)}


def _iso_ms(ms: int) -> str:
    from datetime import datetime, timezone

    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).isoformat().replace("+00:00", "Z")
