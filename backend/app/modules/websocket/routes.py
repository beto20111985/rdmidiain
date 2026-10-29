"""
RD Mídia Indoor — Rotas WebSocket (players e painel).

- ``/ws/player?ticket=``          RDPP/1 §4 (ticket de uso único de ``POST /api/v1/player/ws-ticket``).
- ``/ws/player/pairing?request_id=`` RDPP/1 §2.2 (sem segredo; só recebe ``pairing_claimed``/``pairing_expired``).
- ``/ws/admin?ticket=``           contrato §5 (ticket de ``POST /api/v1/realtime/ticket``; filtro por escopo).

Nenhuma rota usa ``Depends(get_db)``: o banco é acessado por sessões CURTAS por
mensagem (``presence.short_session``), nunca presas à conexão (A2 E34).
"""
from __future__ import annotations

import asyncio
import json
import time
import uuid
from typing import Any, Optional

import structlog
from fastapi import APIRouter, Query, WebSocket, status
from starlette.websockets import WebSocketDisconnect

from app.core import events
from app.core.config import settings
from app.modules.playback import config as pcfg
from app.modules.websocket import bus
from app.modules.websocket.gateway import AdminConn, PairingConn, PlayerConn, get_hub

ws_router = APIRouter()
log = structlog.get_logger("rd.ws")

MAX_MESSAGE_BYTES = 64 * 1024
PLAYER_RECV_MARGIN_S = 5  # sem mensagem por (player.presence_ttl_s + margem) → conexão meio-aberta encerrada
ADMIN_RECV_TIMEOUT_S = 300
RATE_WINDOW_S = 10
RATE_MAX_MSGS = 60


class _Rate:
    def __init__(self) -> None:
        self.t0 = time.monotonic()
        self.n = 0

    def hit(self) -> bool:
        now = time.monotonic()
        if now - self.t0 > RATE_WINDOW_S:
            self.t0, self.n = now, 0
        self.n += 1
        return self.n <= RATE_MAX_MSGS


TIMEOUT = object()


async def _receive_json(ws: WebSocket, timeout: float) -> Optional[Any]:
    """Recebe um frame JSON. ``None`` = encerrar (desconexão/abuso); ``TIMEOUT`` = sem mensagem no prazo."""
    try:
        msg = await asyncio.wait_for(ws.receive(), timeout=timeout)
    except asyncio.TimeoutError:
        return TIMEOUT
    if msg.get("type") == "websocket.disconnect":
        return None
    raw = msg.get("text")
    if raw is None:
        b = msg.get("bytes")
        if b is None or len(b) > MAX_MESSAGE_BYTES:
            return None
        try:
            raw = b.decode("utf-8")
        except UnicodeDecodeError:
            return None
    if len(raw) > MAX_MESSAGE_BYTES:
        return None
    try:
        return json.loads(raw)
    except ValueError:
        return {}


def _panel_origin_ok(origin: Optional[str]) -> bool:
    """Origin ausente (cliente não-navegador) é aceito — o ticket de uso único autentica;
    presente deve estar nas origens do PAINEL (``settings.panel_origins``)."""
    from app.core.origins import origin_in

    if not origin:
        return True
    return origin_in(origin, settings.panel_origins)


# ─── Player ──────────────────────────────────────────────────────────────────

@ws_router.websocket("/player")
async def player_websocket(websocket: WebSocket, ticket: str = Query(default="", max_length=128)):
    from app.models.devices import Device, DeviceStatus
    from app.modules.playback import identity
    from app.modules.presence import service as presence

    if not pcfg.origin_allowed(websocket.headers.get("origin")):
        await websocket.close(code=status.WS_1008_POLICY_VIOLATION)
        return
    info = await identity.consume_ws_ticket(ticket)
    if not info:
        await websocket.close(code=status.WS_1008_POLICY_VIOLATION)
        return
    device_id, screen_id = info.get("device_id"), info.get("screen_id")
    try:
        dev_uuid = uuid.UUID(str(device_id))
    except ValueError:
        await websocket.close(code=status.WS_1008_POLICY_VIOLATION)
        return
    async with presence.short_session() as s:
        dev = await s.get(Device, dev_uuid)
        ok = dev is not None and dev.status == DeviceStatus.ACTIVE.value and str(dev.screen_id) == str(screen_id)
        app_version = dev.app_version if dev is not None else None
    if not ok or await identity.is_revoked_cached(dev_uuid):
        await websocket.close(code=4003)
        return

    hub = await get_hub()
    await websocket.accept()
    conn = PlayerConn(websocket, hub, str(device_id), str(screen_id))
    conn.start()
    hub.add_player(conn)
    # derruba conexões antigas do mesmo dispositivo em OUTROS processos
    try:
        from app.core.redis import get_redis

        r = await get_redis()
        await r.publish(bus.ch_device(device_id), json.dumps({"_ctl": "replaced", "conn": conn.conn_id}))
    except Exception as exc:
        log.error("ws_replace_publish_failed", error_type=type(exc).__name__)
    rate = _Rate()
    try:
        await presence.connected(device_id, screen_id, conn.conn_id, app_version=app_version)
        conn.last_server_time = time.monotonic()
        conn.offer(bus.envelope("server_time", {"server_time_ms": bus.now_ms()}))
        await _deliver_backlog(conn, dev_uuid)
        while not conn.closed:
            recv_timeout = (await pcfg.presence_ttl_s()) + PLAYER_RECV_MARGIN_S  # configurável, cache curto
            msg = await _receive_json(websocket, recv_timeout)
            if msg is None or msg is TIMEOUT:
                break  # desconexão ou conexão meio-aberta (sem heartbeat)
            hub.metrics["in"] += 1
            if not rate.hit():
                log.warning("ws_player_rate_limited", device_id=str(device_id))
                conn.abort(status.WS_1008_POLICY_VIOLATION)
                break
            await _handle_player_message(conn, msg)
    except WebSocketDisconnect:
        pass
    except Exception as exc:
        log.error("ws_player_error", error_type=type(exc).__name__)
    finally:
        hub.remove_player(conn)
        await conn.finish()
        try:
            await presence.disconnected(device_id, screen_id, conn.conn_id)
        except Exception as exc:
            log.error("ws_presence_disconnect_failed", error_type=type(exc).__name__)


async def _deliver_backlog(conn: PlayerConn, device_id: uuid.UUID) -> None:
    """Reentrega comandos ainda abertos (sem ack final) a cada (re)conexão."""
    from app.modules.playback.commands import command_data, open_commands
    from app.modules.presence.service import short_session
    from app.modules.websocket.gateway import _mark_delivered_cb

    async with short_session() as s:
        cmds = await open_commands(s, device_id)
    for c in cmds:
        conn.offer(bus.envelope("command", command_data(c)), on_sent=_mark_delivered_cb(str(c.id)))


async def _handle_player_message(conn: PlayerConn, msg: Any) -> None:
    from app.modules.presence import service as presence

    if not isinstance(msg, dict):
        return
    t = msg.get("type")
    data = msg.get("data") if isinstance(msg.get("data"), dict) else {}
    if t == "heartbeat":
        await presence.heartbeat(conn.device_id, conn.screen_id, conn.conn_id, data)
        return
    await presence.touch(conn.device_id)
    if t == "telemetry":
        await presence.record_telemetry(conn.device_id, conn.screen_id, data)
    elif t == "command_ack":
        await _on_command_ack(conn, data)
    elif t == "manifest_applied":
        await _on_manifest_applied(conn, data)
    elif t == "manifest_failed":
        await _on_manifest_failed(conn, data)
    elif t == "media_error":
        await _on_media_error(conn, data)
    else:
        log.info("ws_player_unknown_type", type=str(t)[:40])


async def _on_command_ack(conn: PlayerConn, data: dict) -> None:
    from app.modules.playback.commands import apply_ack
    from app.modules.presence import service as presence

    async with presence.short_session() as s:
        cmd = await apply_ack(s, uuid.UUID(conn.device_id), data)
        snap = None
        if cmd is not None:
            snap = {"id": str(cmd.id), "command": cmd.command, "status": cmd.status, "detail": cmd.result_detail}
    if snap is not None:
        cur = await presence.get_snapshot(conn.device_id)
        await presence.publish_status(conn.screen_id, conn.device_id, {**cur, "command": snap})


async def _on_manifest_applied(conn: PlayerConn, data: dict) -> None:
    from sqlalchemy import update

    from app.core.timeutils import utcnow
    from app.models.devices import Device
    from app.modules.playback import integration as integ
    from app.modules.presence import service as presence

    v = data.get("version")
    if not isinstance(v, int) or isinstance(v, bool) or v < 0:
        return
    async with presence.short_session() as s:
        await s.execute(
            update(Device)
            .where(Device.id == uuid.UUID(conn.device_id))
            .values(applied_manifest_version=v, pending_manifest_version=None, last_sync_at=utcnow(), last_error=None)
        )
        try:
            await integ.mark_manifest_applied(s, uuid.UUID(conn.screen_id), v, uuid.UUID(conn.device_id))
        except integ.DependencyUnavailable:
            log.error("manifest_applied_inventory_unavailable")
        from app.modules.playback import syncevents

        recv = await syncevents.received_at_ms(conn.device_id, v)
        await syncevents.record(conn.screen_id, conn.device_id, "MANIFEST_APPLIED", db=s, manifest_version=v,
                                duration_ms=(bus.now_ms() - recv) if recv else None)
    from app.core.redis import get_redis

    r = await get_redis()
    await r.hset(presence.k_dev(conn.device_id), mapping={"applied": str(v), "pending": "", "last_sync_ms": str(bus.now_ms())})
    await presence.publish_sync_event(conn.screen_id, {"version": v, "status": presence.STATUS_SYNCED, "device_id": conn.device_id})


async def _on_manifest_failed(conn: PlayerConn, data: dict) -> None:
    from sqlalchemy import update

    from app.models.devices import Device
    from app.modules.presence import service as presence

    v = data.get("version")
    reason = str(data.get("reason") or "")[:200]
    async with presence.short_session() as s:
        await s.execute(update(Device).where(Device.id == uuid.UUID(conn.device_id)).values(last_error=f"manifest_failed:{reason}"[:255]))
        from app.modules.playback import syncevents

        await syncevents.record(conn.screen_id, conn.device_id, "MANIFEST_FAILED", db=s,
                                manifest_version=v if isinstance(v, int) and not isinstance(v, bool) else None, reason=reason)
    await presence.publish_sync_event(
        conn.screen_id, {"version": v if isinstance(v, int) else None, "status": "falhou", "reason": reason, "device_id": conn.device_id}
    )


async def _on_media_error(conn: PlayerConn, data: dict) -> None:
    from sqlalchemy import update

    from app.models.devices import Device
    from app.modules.presence import service as presence

    media_id = str(data.get("media_id") or "")[:64]
    reason = str(data.get("reason") or "")[:160]
    attempts = data.get("attempts") if isinstance(data.get("attempts"), int) else None
    async with presence.short_session() as s:
        await s.execute(
            update(Device).where(Device.id == uuid.UUID(conn.device_id)).values(last_error=f"media_error:{media_id}:{reason}"[:255])
        )
        from app.modules.playback import syncevents

        await syncevents.record(conn.screen_id, conn.device_id, "MEDIA_ERROR", db=s, media_id=media_id,
                                reason=f"{reason} (tentativas={attempts})" if attempts is not None else reason)
    cur = await presence.get_snapshot(conn.device_id)
    await presence.publish_status(
        conn.screen_id, conn.device_id, {**cur, "error": {"kind": "media_error", "media_id": media_id, "reason": reason, "attempts": attempts}}
    )


# ─── Pareamento ──────────────────────────────────────────────────────────────

@ws_router.websocket("/player/pairing")
async def pairing_websocket(websocket: WebSocket, request_id: str = Query(default="", max_length=64)):
    from app.core.timeutils import utcnow
    from app.modules.playback import identity
    from app.modules.presence.service import short_session

    if not pcfg.origin_allowed(websocket.headers.get("origin")):
        await websocket.close(code=status.WS_1008_POLICY_VIOLATION)
        return
    try:
        rid = str(uuid.UUID(request_id))
    except ValueError:
        await websocket.close(code=status.WS_1008_POLICY_VIOLATION)
        return
    hub = await get_hub()
    if len(hub.pairings.get(rid, ())) >= 5:
        await websocket.close(code=status.WS_1008_POLICY_VIOLATION)
        return
    await websocket.accept()
    conn = PairingConn(websocket, hub, rid)
    conn.start()
    hub.add_pairing(conn)  # registra ANTES de ler o estado: sem janela para perder o claim
    try:
        async with short_session() as s:
            try:
                req = await identity.get_request(s, rid)
                st = identity.effective_status(req)
                expires_at = req.expires_at
            except Exception:
                st, expires_at = None, None
        if st is None:
            conn.abort(status.WS_1008_POLICY_VIOLATION)
        elif st == "CLAIMED":
            conn.offer(bus.envelope("pairing_claimed", {}), close_after=1000)
        elif st == "EXPIRED":
            conn.offer(bus.envelope("pairing_expired", {}), close_after=1000)
        else:
            remaining = max(0.0, (expires_at - utcnow()).total_seconds())
            deadline = time.monotonic() + remaining
            while not conn.closed:
                left = deadline - time.monotonic()
                if left <= 0:
                    conn.offer(bus.envelope("pairing_expired", {}), close_after=1000)
                    break
                msg = await _receive_json(websocket, min(left, 30))
                if msg is None:
                    break  # cliente saiu
    except WebSocketDisconnect:
        pass
    except Exception as exc:
        log.error("ws_pairing_error", error_type=type(exc).__name__)
    finally:
        hub.remove_pairing(conn)
        if conn.writer is not None and not conn.writer.done():
            try:
                await asyncio.wait_for(asyncio.shield(conn.writer), timeout=2)
            except Exception:
                pass
        await conn.finish()


# ─── Painel ──────────────────────────────────────────────────────────────────

@ws_router.websocket("/admin")
async def admin_websocket(websocket: WebSocket, ticket: str = Query(default="", max_length=128)):
    """WS do painel — SOMENTE ticket de uso único (contrato §5; corrige A2 E10 / A7 A13).

    Cliente → servidor: ``{"type":"ping"}`` (resposta ``pong``) e
    ``{"type":"subscribe","topics":[...]}`` (filtra tipos; lista vazia = todos).
    Eventos entregues somente se ``events.visible_to(ctx, env)`` (escopo do usuário).
    """
    from app.core.authz import AuthContext, load_grants
    from app.core.timeutils import utcnow
    from app.models.users import User, UserSession
    from app.modules.presence.service import short_session

    if not _panel_origin_ok(websocket.headers.get("origin")):
        await websocket.close(code=status.WS_1008_POLICY_VIOLATION)
        return
    info = await events.consume_ws_ticket(ticket)
    if not info or not info.get("user_id") or not info.get("session_id"):
        await websocket.close(code=status.WS_1008_POLICY_VIOLATION)
        return
    try:
        user_id = uuid.UUID(info["user_id"])
        session_id = uuid.UUID(info["session_id"])
    except (ValueError, TypeError):
        await websocket.close(code=status.WS_1008_POLICY_VIOLATION)
        return
    async with short_session() as s:
        sess = await s.get(UserSession, session_id)
        user = await s.get(User, user_id)
        valid = (
            sess is not None and user is not None and user.is_active and sess.user_id == user_id
            and sess.revoked_at is None and sess.expires_at > utcnow()
        )
        grants = await load_grants(s, user_id) if valid else ()
    if not valid:
        await websocket.close(code=status.WS_1008_POLICY_VIOLATION)
        return
    from app.core.authz import _revoked_in_cache

    if await _revoked_in_cache(str(session_id)):
        await websocket.close(code=status.WS_1008_POLICY_VIOLATION)
        return
    ctx = AuthContext(kind="user", user=user, session=sess, grants=grants)
    hub = await get_hub()
    await websocket.accept()
    conn = AdminConn(websocket, hub, ctx, str(session_id))
    conn.start()
    hub.add_admin(conn)
    rate = _Rate()
    try:
        while not conn.closed:
            msg = await _receive_json(websocket, ADMIN_RECV_TIMEOUT_S)
            if msg is None:
                break
            if msg is TIMEOUT:
                continue
            hub.metrics["in"] += 1
            if not rate.hit():
                conn.abort(status.WS_1008_POLICY_VIOLATION)
                break
            if not isinstance(msg, dict):
                continue
            t = msg.get("type")
            if t == "ping":
                conn.offer({"type": "pong", "id": str(uuid.uuid4()), "ts": _iso_now(), "data": {}})
            elif t == "subscribe":
                topics = msg.get("topics")
                if isinstance(topics, list):
                    sel = {x for x in topics if isinstance(x, str) and x in events.EVENT_TYPES}
                    conn.topics = sel or None
    except WebSocketDisconnect:
        pass
    except Exception as exc:
        log.error("ws_admin_error", error_type=type(exc).__name__)
    finally:
        hub.remove_admin(conn)
        await conn.finish()


def _iso_now() -> str:
    from app.core.timeutils import isoformat_z, utcnow

    return isoformat_z(utcnow())
