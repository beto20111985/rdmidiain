"""
RD Mídia Indoor — Gateway WebSocket por processo (players e painel) com Redis Pub/Sub.

Desenho (P002 itens 52–53; ADR_007 item 6; ADR_010 item 7; corrige A2 E32–E34 e A4 C4/C7):
- Cada processo da API mantém um ``Hub`` (um por event loop) com o registro LOCAL das
  conexões e UMA assinatura Redis (``psubscribe``) nos padrões ``rdpp:*`` (players) e
  ``events:*`` (painel). Qualquer processo (API com N workers, Celery) publica; cada
  Hub entrega às SUAS conexões.
- Envio desacoplado: cada conexão tem fila própria limitada + tarefa escritora; o
  despacho nunca espera um socket lento (fila cheia ⇒ conexão encerrada). NENHUM lock
  é mantido durante envio (o antigo ``disconnect`` → ``_notify_admins`` → ``disconnect``
  sob ``asyncio.Lock`` não reentrante travava o subsistema — A2 E33).
- Banco: SEM sessão presa à conexão. Cada mensagem que precisa do banco abre uma
  sessão curta e fecha (``presence.short_session``).
- Tarefa periódica do Hub (15 s): publica contadores do processo no Redis (métricas
  globais baratas para ``/metrics``), checa revogação de dispositivos (pipeline
  ``EXISTS`` no Redis), sessões revogadas/expiradas do painel e envia ``server_time``.
"""
from __future__ import annotations

import asyncio
import json
import os
import socket
import time
import uuid
import weakref
from collections import OrderedDict
from typing import Any, Awaitable, Callable, Optional

import redis.asyncio as aioredis
import structlog

from app.core import events
from app.core.config import settings
from app.core.redis import get_redis, rkey
from app.modules.websocket import bus

log = structlog.get_logger("rd.ws.gateway")

QUEUE_MAX = 256
TICK_S = 15
SERVER_TIME_EVERY_S = 600
PROC_ID = f"{socket.gethostname()}:{os.getpid()}:{uuid.uuid4().hex[:6]}"

CLOSE_REVOKED = 4003
CLOSE_REPLACED = 4000
CLOSE_SLOW = 1013
CLOSE_SESSION = 4401


class Conn:
    """Conexão WS com fila de saída e escritor dedicado."""

    def __init__(self, ws: Any, kind: str, hub: "Hub") -> None:
        self.ws = ws
        self.kind = kind
        self.hub = hub
        self.conn_id = uuid.uuid4().hex
        self.queue: asyncio.Queue = asyncio.Queue(maxsize=QUEUE_MAX)
        self.closed = False
        self.writer: Optional[asyncio.Task] = None
        self.close_code: Optional[int] = None

    def start(self) -> None:
        self.writer = asyncio.create_task(self._writer_loop(), name=f"ws-writer-{self.kind}-{self.conn_id[:6]}")

    def offer(self, payload: dict, *, close_after: Optional[int] = None,
              on_sent: Optional[Callable[[], Awaitable[None]]] = None) -> bool:
        if self.closed:
            return False
        try:
            self.queue.put_nowait((payload, close_after, on_sent))
            return True
        except asyncio.QueueFull:
            self.hub.metrics["dropped"] += 1
            log.warning("ws_slow_consumer_closed", kind=self.kind)
            self.abort(CLOSE_SLOW)
            return False

    def abort(self, code: int) -> None:
        """Encerra a conexão a partir de qualquer tarefa (sem bloquear)."""
        if self.closed:
            return
        self.closed = True
        self.close_code = code
        try:
            while True:
                self.queue.get_nowait()
        except asyncio.QueueEmpty:
            pass
        self.queue.put_nowait((None, code, None))

    async def _writer_loop(self) -> None:
        try:
            while True:
                payload, close_after, on_sent = await self.queue.get()
                if payload is None:
                    await self._close(close_after or 1000)
                    return
                await self.ws.send_text(json.dumps(payload, separators=(",", ":"), default=str))
                self.hub.metrics["out"] += 1
                if on_sent is not None:
                    try:
                        await on_sent()
                    except Exception as exc:  # pragma: no cover - log e segue
                        log.error("ws_on_sent_failed", error_type=type(exc).__name__)
                if close_after:
                    self.closed = True
                    self.close_code = close_after
                    await self._close(close_after)
                    return
        except Exception:
            self.closed = True
        finally:
            self.closed = True

    async def _close(self, code: int) -> None:
        try:
            await self.ws.close(code=code)
        except Exception:
            pass

    async def finish(self) -> None:
        """Chamado pelo handler ao sair: encerra o escritor."""
        self.abort(self.close_code or 1000)
        if self.writer is not None:
            try:
                await asyncio.wait_for(self.writer, timeout=2)
            except (asyncio.TimeoutError, Exception):
                self.writer.cancel()


class PlayerConn(Conn):
    def __init__(self, ws: Any, hub: "Hub", device_id: str, screen_id: str) -> None:
        super().__init__(ws, "player", hub)
        self.device_id = device_id
        self.screen_id = screen_id
        self.last_server_time = 0.0


class AdminConn(Conn):
    def __init__(self, ws: Any, hub: "Hub", ctx: Any, session_id: str) -> None:
        super().__init__(ws, "admin", hub)
        self.ctx = ctx
        self.session_id = session_id
        self.topics: Optional[set[str]] = None
        self.last_db_check = time.monotonic()


class PairingConn(Conn):
    def __init__(self, ws: Any, hub: "Hub", request_id: str) -> None:
        super().__init__(ws, "pairing", hub)
        self.request_id = request_id


class Hub:
    def __init__(self) -> None:
        self.players: dict[str, PlayerConn] = {}
        self.by_screen: dict[str, set[str]] = {}
        self.admins: set[AdminConn] = set()
        self.pairings: dict[str, set[PairingConn]] = {}
        self.metrics = {"in": 0, "out": 0, "dropped": 0, "pubsub_reconnects": 0, "rejected": 0}
        self._listener: Optional[asyncio.Task] = None
        self._ticker: Optional[asyncio.Task] = None
        self._ready = asyncio.Event()
        self._seen_admin: "OrderedDict[str, None]" = OrderedDict()
        self._client: Optional[aioredis.Redis] = None
        self._stopping = False
        self._counts_task: Optional[asyncio.Task] = None
        self._bg: set = set()

    def _counts_changed(self) -> None:
        """Publica os contadores do processo logo após conectar/desconectar (coalescido)."""
        if self._counts_task is None or self._counts_task.done():
            self._counts_task = asyncio.create_task(self._publish_counts())

    async def _publish_counts(self) -> None:
        try:
            await asyncio.sleep(0.2)
            r = await get_redis()
            await r.set(rkey("ws", "proc", PROC_ID), json.dumps({**self.counts(), **self.metrics}), ex=TICK_S * 3)
        except Exception as exc:  # pragma: no cover
            log.error("ws_counts_publish_failed", error_type=type(exc).__name__)

    # ── ciclo de vida ──
    async def ensure_started(self) -> None:
        if self._listener is None or self._listener.done():
            self._ready.clear()
            self._listener = asyncio.create_task(self._listen(), name="ws-hub-listener")
            self._ticker = asyncio.create_task(self._tick(), name="ws-hub-ticker")
        await asyncio.wait_for(self._ready.wait(), timeout=10)

    async def stop(self) -> None:
        self._stopping = True
        for c in list(self.players.values()) + list(self.admins) + [p for s in self.pairings.values() for p in s]:
            c.abort(1001)
        for t in (self._listener, self._ticker):
            if t is not None:
                t.cancel()
        try:
            r = await get_redis()
            await r.delete(rkey("ws", "proc", PROC_ID))
        except Exception:
            pass
        if self._client is not None:
            try:
                await self._client.aclose()
            except Exception:
                pass

    async def _listen(self) -> None:
        backoff = 0.5
        while not self._stopping:
            pubsub = None
            try:
                self._client = aioredis.from_url(
                    settings.REDIS_URL, decode_responses=True, socket_connect_timeout=3, health_check_interval=30
                )
                pubsub = self._client.pubsub()
                await pubsub.psubscribe(bus.pattern(), events.subscribe_pattern())
                self._ready.set()
                backoff = 0.5
                while not self._stopping:
                    msg = await pubsub.get_message(ignore_subscribe_messages=True, timeout=1.0)
                    if msg is None:
                        continue
                    try:
                        self._dispatch(msg.get("channel") or "", msg.get("data"))
                    except Exception as exc:
                        log.error("ws_dispatch_failed", error_type=type(exc).__name__)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self.metrics["pubsub_reconnects"] += 1
                log.error("ws_pubsub_error", error_type=type(exc).__name__)
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 10)
            finally:
                if pubsub is not None:
                    try:
                        await pubsub.aclose()
                    except Exception:
                        pass
                if self._client is not None:
                    try:
                        await self._client.aclose()
                    except Exception:
                        pass
                    self._client = None

    # ── registro ──
    def add_player(self, conn: PlayerConn) -> None:
        old = self.players.get(conn.device_id)
        if old is not None and old is not conn:
            old.abort(CLOSE_REPLACED)
        self.players[conn.device_id] = conn
        self.by_screen.setdefault(conn.screen_id, set()).add(conn.device_id)
        self._counts_changed()

    def remove_player(self, conn: PlayerConn) -> None:
        if self.players.get(conn.device_id) is conn:
            del self.players[conn.device_id]
            s = self.by_screen.get(conn.screen_id)
            if s is not None:
                s.discard(conn.device_id)
                if not s:
                    del self.by_screen[conn.screen_id]
            self._counts_changed()

    def add_admin(self, conn: AdminConn) -> None:
        self.admins.add(conn)
        self._counts_changed()

    def remove_admin(self, conn: AdminConn) -> None:
        self.admins.discard(conn)
        self._counts_changed()

    def add_pairing(self, conn: PairingConn) -> None:
        self.pairings.setdefault(conn.request_id, set()).add(conn)

    def remove_pairing(self, conn: PairingConn) -> None:
        s = self.pairings.get(conn.request_id)
        if s is not None:
            s.discard(conn)
            if not s:
                del self.pairings[conn.request_id]

    def counts(self) -> dict:
        return {
            "player": len(self.players),
            "admin": len(self.admins),
            "pairing": sum(len(s) for s in self.pairings.values()),
        }

    # ── despacho ──
    def _deliver_player(self, conn: PlayerConn, env: dict) -> None:
        t = env.get("type")
        if t == "device_revoked":
            conn.offer(env, close_after=CLOSE_REVOKED)
        elif t == "command":
            cid = (env.get("data") or {}).get("command_id")
            conn.offer(env, on_sent=_mark_delivered_cb(cid) if cid else None)
        else:
            conn.offer(env)

    def _dispatch(self, channel: str, data: Any) -> None:
        if not isinstance(data, str):
            return
        rdpp = rkey("rdpp", "")
        evp = rkey("events", "")
        if channel.startswith(rdpp):
            rest = channel[len(rdpp):]
            msg = json.loads(data)
            if "_ctl" in msg:
                self._control(rest, msg)
                return
            if rest.startswith("dev:"):
                c = self.players.get(rest[4:])
                if c is not None:
                    self._deliver_player(c, msg)
            elif rest.startswith("scr:"):
                for dev in list(self.by_screen.get(rest[4:], ())):
                    c = self.players.get(dev)
                    if c is not None:
                        self._deliver_player(c, msg)
            elif rest == "all":
                for c in list(self.players.values()):
                    self._deliver_player(c, msg)
            elif rest.startswith("pair:"):
                for c in list(self.pairings.get(rest[5:], ())):
                    c.offer(msg, close_after=1000)
            return
        if channel.startswith(evp):
            env = json.loads(data)
            if not isinstance(env, dict) or "type" not in env:
                return
            if channel.startswith(rkey("events", "screen", "")) and env.get("type") == "screen.sync":
                version = (env.get("data") or {}).get("version")
                scr = channel[len(rkey("events", "screen", "")):]
                if isinstance(version, int) and not isinstance(version, bool):
                    out = bus.envelope("sync_required", {"version": version})
                    for dev in list(self.by_screen.get(scr, ())):
                        c = self.players.get(dev)
                        if c is not None:
                            c.offer(out)
            self._deliver_admins(env)

    def _deliver_admins(self, env: dict) -> None:
        eid = env.get("id")
        if not eid or eid in self._seen_admin:
            return
        self._seen_admin[eid] = None
        if len(self._seen_admin) > 4096:
            self._seen_admin.popitem(last=False)
        if not self.admins:
            return
        pub = events.public_envelope(env)
        for c in list(self.admins):
            if c.topics is not None and env.get("type") not in c.topics:
                continue
            try:
                if events.visible_to(c.ctx, env):
                    c.offer(pub)
            except Exception as exc:  # contexto inválido nunca derruba o despacho
                log.error("ws_visibility_error", error_type=type(exc).__name__)

    def _control(self, rest: str, msg: dict) -> None:
        if msg.get("_ctl") == "replaced" and rest.startswith("dev:"):
            c = self.players.get(rest[4:])
            if c is not None and c.conn_id != msg.get("conn"):
                c.abort(CLOSE_REPLACED)

    # ── tarefa periódica ──
    async def _tick(self) -> None:
        from app.core.authz import _revoked_in_cache  # noqa: PLC0415 (cache de sessões da fundação)
        from app.modules.playback.identity import k_revoked

        while not self._stopping:
            try:
                await asyncio.sleep(TICK_S)
                r = await get_redis()
                await r.set(rkey("ws", "proc", PROC_ID), json.dumps({**self.counts(), **self.metrics}), ex=TICK_S * 3)
                players = list(self.players.values())
                if players:
                    pipe = r.pipeline(transaction=False)
                    for c in players:
                        pipe.exists(k_revoked(c.device_id))
                    flags = await pipe.execute()
                    now = time.monotonic()
                    for c, revoked in zip(players, flags):
                        if revoked:
                            c.offer(bus.envelope("device_revoked", {}), close_after=CLOSE_REVOKED)
                        elif now - c.last_server_time >= SERVER_TIME_EVERY_S:
                            c.last_server_time = now
                            c.offer(bus.envelope("server_time", {"server_time_ms": bus.now_ms()}))
                for a in list(self.admins):
                    if await _revoked_in_cache(a.session_id):
                        a.abort(CLOSE_SESSION)
                    elif time.monotonic() - a.last_db_check >= 300:
                        a.last_db_check = time.monotonic()
                        t = asyncio.create_task(_recheck_admin(a))
                        self._bg.add(t)  # referência forte (A4 M9: task sem referência pode ser coletada)
                        t.add_done_callback(self._bg.discard)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                log.error("ws_tick_failed", error_type=type(exc).__name__)


def _mark_delivered_cb(command_id: str) -> Callable[[], Awaitable[None]]:
    async def _cb() -> None:
        from app.modules.playback.commands import mark_delivered
        from app.modules.presence.service import short_session

        async with short_session() as s:
            await mark_delivered(s, uuid.UUID(command_id))

    return _cb


async def _recheck_admin(conn: AdminConn) -> None:
    """Revalida sessão e recarrega permissões do admin (a cada 5 min)."""
    from app.core.authz import load_grants
    from app.core.timeutils import utcnow
    from app.models.users import User, UserSession
    from app.modules.presence.service import short_session

    try:
        async with short_session() as s:
            sess = await s.get(UserSession, uuid.UUID(conn.session_id))
            user = await s.get(User, conn.ctx.user_id) if conn.ctx.user_id else None
            ok = (
                sess is not None and user is not None and user.is_active and sess.revoked_at is None
                and sess.expires_at > utcnow()
            )
            grants = await load_grants(s, user.id) if ok else ()
        if not ok:
            conn.abort(CLOSE_SESSION)
        else:
            conn.ctx.grants = grants
    except Exception as exc:
        log.error("ws_admin_recheck_failed", error_type=type(exc).__name__)


_hubs: "weakref.WeakKeyDictionary[asyncio.AbstractEventLoop, Hub]" = weakref.WeakKeyDictionary()


async def get_hub() -> Hub:
    loop = asyncio.get_running_loop()
    hub = _hubs.get(loop)
    if hub is None:
        hub = Hub()
        _hubs[loop] = hub
    await hub.ensure_started()
    return hub


def current_hub() -> Optional[Hub]:
    try:
        return _hubs.get(asyncio.get_running_loop())
    except RuntimeError:
        return None


async def startup() -> None:
    await get_hub()


async def shutdown() -> None:
    hub = current_hub()
    if hub is not None:
        await hub.stop()
        try:
            del _hubs[asyncio.get_running_loop()]
        except KeyError:
            pass


async def global_counts() -> dict:
    """Soma dos contadores publicados por TODOS os processos (barato: poucas chaves)."""
    r = await get_redis()
    hub = current_hub()
    if hub is not None:
        await r.set(rkey("ws", "proc", PROC_ID), json.dumps({**hub.counts(), **hub.metrics}), ex=TICK_S * 3)
    total = {"player": 0, "admin": 0, "pairing": 0, "in": 0, "out": 0, "dropped": 0, "processes": 0}
    async for key in r.scan_iter(match=rkey("ws", "proc", "*"), count=100):
        raw = await r.get(key)
        if not raw:
            continue
        try:
            d = json.loads(raw)
        except ValueError:
            continue
        total["processes"] += 1
        for k in ("player", "admin", "pairing", "in", "out", "dropped"):
            total[k] += int(d.get(k, 0))
    return total
