"""
RD Mídia Indoor — Presença, heartbeat e telemetria dos players (RDPP/1 §4; ADR_007 item 7).

Estado "quente" SOMENTE no Redis:
- ``presence:dev:<device_id>`` (hash, TTL ``player.presence_ttl_s`` — default 90 s, configurável —
  renovado a cada heartbeat/mensagem);
- ``presence:last_seen`` (sorted set device_id → last_seen_ms).

PostgreSQL apenas em TRANSIÇÕES (online↔offline, mudança de estado exibido, de versão
aplicada/pendente, erro) — sessões curtas por operação, nunca presas à conexão — e
num flush coalescido de ``last_seen_at`` (job de 5 min). Conexões meio-abertas e
processos mortos são detectados pelo job controlado de 60 s (``sweep``) sobre o sorted
set (permitido por P001 §70: "jobs controlados").

Estados exibidos (``status``): conectado, online, offline, sincronizando, sincronizado,
reproduzindo, travado, erro. Evento ao painel: ``screen.status`` (filtrado por escopo
no gateway com ``events.visible_to``).
"""
from __future__ import annotations

import json
import time
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import Any, AsyncIterator, Optional

import structlog
from sqlalchemy import text, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import events
from app.core.database import session_factory
from app.core.redis import get_redis, rkey
from app.modules.playback import config as pcfg

log = structlog.get_logger("rd.presence")

STATUS_CONNECTED = "conectado"
STATUS_ONLINE = "online"
STATUS_OFFLINE = "offline"
STATUS_SYNCING = "sincronizando"
STATUS_SYNCED = "sincronizado"
STATUS_PLAYING = "reproduzindo"
STATUS_FROZEN = "travado"
STATUS_ERROR = "erro"
DISPLAY_STATUSES = (
    STATUS_CONNECTED,
    STATUS_ONLINE,
    STATUS_OFFLINE,
    STATUS_SYNCING,
    STATUS_SYNCED,
    STATUS_PLAYING,
    STATUS_FROZEN,
    STATUS_ERROR,
)
PLAYER_STATES = frozenset({"PLAYING", "IDLE", "SYNCING", "ERROR", "FROZEN", "UNPAIRED"})

TELEMETRY_KEYS = (
    "cpu_pct",
    "ram_used_mb",
    "ram_total_mb",
    "disk_free_mb",
    "disk_total_mb",
    "uptime_s",
    "os",
    "app_version",
    "platform",
    "resolution",
    "orientation",
    "wifi",
    "hdmi_connected",
)

_DISCONNECT_LUA = """
if redis.call('HGET', KEYS[1], 'conn') == ARGV[1] then
  redis.call('DEL', KEYS[1])
  redis.call('ZREM', KEYS[2], ARGV[2])
  return 1
end
return 0
"""

_SWEEP_LUA = """
local score = redis.call('ZSCORE', KEYS[2], ARGV[1])
if score and tonumber(score) <= tonumber(ARGV[2]) then
  redis.call('ZREM', KEYS[2], ARGV[1])
  redis.call('DEL', KEYS[1])
  return 1
end
return 0
"""


def now_ms() -> int:
    return int(time.time() * 1000)


def k_dev(device_id: Any) -> str:
    return rkey("presence", "dev", device_id)


def k_last_seen() -> str:
    return rkey("presence", "last_seen")


def k_scope(screen_id: Any) -> str:
    return rkey("presence", "scope", screen_id)


def k_telemetry_gate(device_id: Any) -> str:
    return rkey("presence", "telgate", device_id)


@asynccontextmanager
async def short_session() -> AsyncIterator[AsyncSession]:
    """Sessão CURTA (uma operação), commit no fim — nunca presa à conexão WS."""
    async with session_factory()() as s:
        try:
            yield s
            await s.commit()
        except BaseException:
            await s.rollback()
            raise


def display_status(connected: bool, state: Optional[str]) -> str:
    if not connected:
        return STATUS_OFFLINE
    if state is None:
        return STATUS_CONNECTED
    return {
        "PLAYING": STATUS_PLAYING,
        "SYNCING": STATUS_SYNCING,
        "FROZEN": STATUS_FROZEN,
        "ERROR": STATUS_ERROR,
        "UNPAIRED": STATUS_ERROR,
        "IDLE": STATUS_ONLINE,
    }.get(state, STATUS_ONLINE)


def download_of(snap: dict) -> Optional[dict]:
    """``{done_bytes, total_bytes}`` do último heartbeat (ou None)."""
    raw = snap.get("download")
    if not raw:
        return None
    try:
        d = json.loads(raw) if isinstance(raw, str) else raw
    except ValueError:
        return None
    if not isinstance(d, dict):
        return None
    done, total = _int_or_none(d.get("done_bytes")), _int_or_none(d.get("total_bytes"))
    if done is None and total is None:
        return None
    return {"done_bytes": done, "total_bytes": total}


def sync_status(applied: Optional[int], pending: Optional[int]) -> Optional[str]:
    if pending is not None:
        return STATUS_SYNCING
    if applied is not None:
        return STATUS_SYNCED
    return None


def _int_or_none(v: Any) -> Optional[int]:
    if isinstance(v, bool) or v is None:
        return None
    if isinstance(v, int):
        return v
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


def _ms_to_dt(ms: Optional[int]) -> Optional[datetime]:
    return None if ms is None else datetime.fromtimestamp(ms / 1000, tz=timezone.utc)


# ─── Escopo do evento ao painel ──────────────────────────────────────────────

async def screen_scope(screen_id: Any) -> dict:
    r = await get_redis()
    raw = await r.get(k_scope(screen_id))
    if raw:
        try:
            return json.loads(raw)
        except ValueError:
            pass
    from app.modules.playback.integration import screen_info

    async with short_session() as s:
        info = await screen_info(s, uuid.UUID(str(screen_id)))
    scope = (
        {
            "org_id": str(info.org_id) if info.org_id else None,
            "establishment_id": str(info.establishment_id),
            "city": info.city_key,
            "screen_code": info.code,
        }
        if info
        else {"org_id": None, "establishment_id": None, "city": None, "screen_code": None}
    )
    await r.set(k_scope(screen_id), json.dumps(scope), ex=600)
    return scope


async def forget_scope(screen_id: Any) -> None:
    r = await get_redis()
    await r.delete(k_scope(screen_id))


async def publish_status(screen_id: Any, device_id: Any, snap: dict) -> None:
    sc = await screen_scope(screen_id)
    data = {
        "screen_id": str(screen_id),
        "screen_code": sc.get("screen_code"),
        "device_id": str(device_id) if device_id else None,
        "presence": "offline" if snap.get("status") == STATUS_OFFLINE else "online",
        "status": snap.get("status"),
        "sync": sync_status(_int_or_none(snap.get("applied")), _int_or_none(snap.get("pending"))),
        "player_state": snap.get("state"),
        "manifest_version_applied": _int_or_none(snap.get("applied")),
        "manifest_version_pending": _int_or_none(snap.get("pending")),
        "last_seen_ms": _int_or_none(snap.get("last_seen_ms")),
        "app_version": snap.get("app_version"),
        "download": download_of(snap),
    }
    for extra in ("command", "error"):
        if snap.get(extra) is not None:
            data[extra] = snap[extra]
    await events.publish(
        "screen.status",
        data,
        org_id=sc.get("org_id"),
        establishment_id=sc.get("establishment_id"),
        city=sc.get("city"),
    )


async def publish_sync_event(screen_id: Any, data: dict) -> None:
    sc = await screen_scope(screen_id)
    await events.publish(
        "screen.sync",
        {"screen_id": str(screen_id), "screen_code": sc.get("screen_code"), **data},
        org_id=sc.get("org_id"),
        establishment_id=sc.get("establishment_id"),
        city=sc.get("city"),
    )


# ─── Persistência de transições ──────────────────────────────────────────────

async def _persist(device_id: Any, values: dict) -> None:
    from app.models.devices import Device

    async with short_session() as s:
        await s.execute(update(Device).where(Device.id == uuid.UUID(str(device_id))).values(**values))


# ─── Ciclo de vida da conexão ────────────────────────────────────────────────

async def get_snapshot(device_id: Any) -> dict:
    r = await get_redis()
    return await r.hgetall(k_dev(device_id)) or {}


async def connected(device_id: Any, screen_id: Any, conn_id: str, *, app_version: Optional[str] = None) -> dict:
    r = await get_redis()
    ttl = await pcfg.presence_ttl_s()
    prev = await r.hgetall(k_dev(device_id))
    ts = now_ms()
    snap = {
        "screen_id": str(screen_id),
        "conn": conn_id,
        "connected": "1",
        "status": STATUS_CONNECTED,
        "last_seen_ms": str(ts),
    }
    if app_version:
        snap["app_version"] = app_version
    pipe = r.pipeline(transaction=True)
    pipe.delete(k_dev(device_id))
    keep = {k: v for k, v in prev.items() if k in (
        "applied", "pending", "app_version", "last_sync_ms", "last_play_ms", "dl_started_ms", "dl_version", "dl_done", "dl_total")}
    pipe.hset(k_dev(device_id), mapping={**keep, **snap})
    pipe.expire(k_dev(device_id), ttl)
    pipe.zadd(k_last_seen(), {str(device_id): ts})
    await pipe.execute()
    full = {**keep, **snap}
    await _persist(device_id, {"presence": "online", "last_seen_at": _ms_to_dt(ts)})
    await publish_status(screen_id, device_id, full)
    return full


async def touch(device_id: Any) -> None:
    """Qualquer mensagem do player renova a presença (sem banco)."""
    r = await get_redis()
    ttl = await pcfg.presence_ttl_s()
    ts = now_ms()
    pipe = r.pipeline(transaction=False)
    pipe.hset(k_dev(device_id), "last_seen_ms", str(ts))
    pipe.expire(k_dev(device_id), ttl)
    pipe.zadd(k_last_seen(), {str(device_id): ts})
    await pipe.execute()


async def heartbeat(device_id: Any, screen_id: Any, conn_id: str, data: dict) -> dict:
    """Heartbeat (30 s): só Redis; banco + evento apenas se houver transição."""
    if not isinstance(data, dict):
        data = {}
    ttl = await pcfg.presence_ttl_s()
    state = data.get("state") if data.get("state") in PLAYER_STATES else None
    applied = _int_or_none(data.get("manifest_version_applied"))
    pending = _int_or_none(data.get("manifest_version_pending"))
    last_sync_ms = _int_or_none(data.get("last_sync_ms"))
    last_play_ms = _int_or_none(data.get("last_play_ms"))
    r = await get_redis()
    prev = await r.hgetall(k_dev(device_id))
    ts = now_ms()
    status = display_status(True, state)
    snap = {
        "screen_id": str(screen_id),
        "conn": conn_id,
        "connected": "1",
        "state": state or "",
        "status": status,
        "last_seen_ms": str(ts),
    }
    if applied is not None:
        snap["applied"] = str(applied)
    snap["pending"] = "" if pending is None else str(pending)
    if last_sync_ms is not None:
        snap["last_sync_ms"] = str(last_sync_ms)
    if last_play_ms is not None:
        snap["last_play_ms"] = str(last_play_ms)
    cur = data.get("current")
    if isinstance(cur, dict) and isinstance(cur.get("creative_code"), str):
        snap["current_code"] = cur["creative_code"][:16]
    for jkey in ("download", "storage", "alerts"):
        if jkey in data:
            snap[jkey] = json.dumps(data[jkey], separators=(",", ":"), default=str)[:1000]
    off = _int_or_none(data.get("clock_offset_ms"))
    if off is not None:
        snap["clock_offset_ms"] = str(off)
    pipe = r.pipeline(transaction=False)
    pipe.hset(k_dev(device_id), mapping=snap)
    pipe.expire(k_dev(device_id), ttl)
    pipe.zadd(k_last_seen(), {str(device_id): ts})
    await pipe.execute()

    def _p(d: dict, k: str) -> Optional[int]:
        return _int_or_none(d.get(k) or None)

    changed = (
        prev.get("status") != status
        or _p(prev, "applied") != applied
        or _p(prev, "pending") != pending
        or (prev.get("state") or None) != state
    )
    full = {**prev, **snap}
    await _download_transitions(device_id, screen_id, prev, data, pending, applied, ts)
    if changed:
        values: dict = {
            "presence": "online",
            "player_state": state,
            "pending_manifest_version": pending,
            "last_seen_at": _ms_to_dt(ts),
        }
        if applied is not None:
            values["applied_manifest_version"] = applied
        if last_sync_ms is not None:
            values["last_sync_at"] = _ms_to_dt(last_sync_ms)
        if last_play_ms is not None:
            values["last_play_at"] = _ms_to_dt(last_play_ms)
        if state in ("ERROR", "FROZEN"):
            values["last_error"] = f"state:{state}"
        await _persist(device_id, values)
        await publish_status(screen_id, device_id, full)
    return {"changed": changed, "status": status}


async def disconnected(device_id: Any, screen_id: Any, conn_id: str) -> bool:
    """Desconexão = offline imediato, SOMENTE se a conexão registrada ainda é esta
    (reconexão rápida em outro processo não é sobrescrita)."""
    r = await get_redis()
    snap = await r.hgetall(k_dev(device_id))
    removed = await r.eval(_DISCONNECT_LUA, 2, k_dev(device_id), k_last_seen(), conn_id, str(device_id))
    if not int(removed or 0):
        return False
    await _persist(device_id, {"presence": "offline", "last_seen_at": _ms_to_dt(_int_or_none(snap.get("last_seen_ms")) or now_ms())})
    await publish_status(screen_id, device_id, {**snap, "status": STATUS_OFFLINE, "connected": "0"})
    return True


async def mark_revoked(device_id: Any) -> None:
    r = await get_redis()
    pipe = r.pipeline(transaction=False)
    pipe.delete(k_dev(device_id))
    pipe.zrem(k_last_seen(), str(device_id))
    await pipe.execute()


# ─── Telemetria (5 min; sem IP) ──────────────────────────────────────────────

def sanitize_telemetry(data: Any) -> dict:
    if not isinstance(data, dict):
        return {}
    out: dict = {}
    for k in TELEMETRY_KEYS:
        if k not in data:
            continue
        v = data[k]
        if k == "wifi":
            if isinstance(v, dict):
                out["wifi"] = {
                    "ssid": str(v.get("ssid"))[:64] if v.get("ssid") is not None else None,
                    "rssi_dbm": v.get("rssi_dbm") if isinstance(v.get("rssi_dbm"), (int, float)) else None,
                }
            else:
                out["wifi"] = None
        elif isinstance(v, bool) or v is None:
            out[k] = v
        elif isinstance(v, (int, float)):
            out[k] = v
        elif isinstance(v, str):
            out[k] = v[:80]
    return out


async def record_telemetry(device_id: Any, screen_id: Any, data: Any) -> bool:
    """Persiste no máximo uma amostra a cada ~4 min por dispositivo (cadência de 5 min)."""
    from app.models.devices import Device, DeviceTelemetry
    from app.core.timeutils import utcnow

    clean = sanitize_telemetry(data)
    if not clean:
        return False
    r = await get_redis()
    if not await r.set(k_telemetry_gate(device_id), "1", nx=True, ex=pcfg.TELEMETRY_MIN_INTERVAL_S):
        return False
    now = utcnow()
    values: dict = {"telemetry": clean, "telemetry_at": now}
    if isinstance(clean.get("app_version"), str):
        values["app_version"] = clean["app_version"][:40]
        await r.hset(k_dev(device_id), "app_version", clean["app_version"][:40])
    if isinstance(clean.get("os"), str):
        values["os_version"] = clean["os"][:80]
    async with short_session() as s:
        s.add(DeviceTelemetry(device_id=uuid.UUID(str(device_id)), screen_id=uuid.UUID(str(screen_id)), recorded_at=now, data=clean))
        await s.execute(update(Device).where(Device.id == uuid.UUID(str(device_id))).values(**values))
    return True


# ─── Jobs controlados ────────────────────────────────────────────────────────

async def sweep(*, cutoff_ms: Optional[int] = None, db: Optional[AsyncSession] = None) -> list[str]:
    """Marca offline quem não deu sinal há mais de ``player.presence_ttl_s`` (default 90 s) (meio-aberto, processo morto)."""
    from app.models.devices import Device

    r = await get_redis()
    if cutoff_ms is None:
        cutoff_ms = now_ms() - (await pcfg.presence_ttl_s(db)) * 1000
    cutoff = cutoff_ms
    stale = await r.zrangebyscore(k_last_seen(), "-inf", cutoff)
    gone: list[str] = []
    snaps: dict[str, dict] = {}
    for dev in stale:
        snap = await r.hgetall(k_dev(dev))
        if int(await r.eval(_SWEEP_LUA, 2, k_dev(dev), k_last_seen(), dev, cutoff) or 0):
            gone.append(dev)
            snaps[dev] = snap
    if not gone:
        return []

    async def _apply(s: AsyncSession) -> list[tuple[str, str]]:
        rows = (
            await s.execute(
                update(Device)
                .where(Device.id.in_([uuid.UUID(d) for d in gone]))
                .values(presence="offline")
                .returning(Device.id, Device.screen_id)
            )
        ).all()
        return [(str(a), str(b)) for a, b in rows]

    if db is not None:
        pairs = await _apply(db)
        await db.commit()
    else:
        async with short_session() as s:
            pairs = await _apply(s)
    for dev, scr in pairs:
        await publish_status(scr, dev, {**snaps.get(dev, {}), "status": STATUS_OFFLINE, "connected": "0"})
    return gone


async def flush_last_seen(db: AsyncSession) -> int:
    """Flush coalescido: um UPDATE em lote de ``last_seen_at`` para todos os presentes."""
    r = await get_redis()
    items = await r.zrange(k_last_seen(), 0, -1, withscores=True)
    total = 0
    for i in range(0, len(items), 1000):
        chunk = items[i : i + 1000]
        if not chunk:
            continue
        params: dict = {}
        values = []
        for j, (dev, score) in enumerate(chunk):
            try:
                params[f"d{j}"] = uuid.UUID(dev)
            except ValueError:
                continue
            params[f"t{j}"] = _ms_to_dt(int(score))
            values.append(f"(CAST(:d{j} AS uuid), CAST(:t{j} AS timestamptz))")
        if not values:
            continue
        res = await db.execute(
            text(
                "UPDATE devices AS d SET last_seen_at = v.ts FROM (VALUES "
                + ",".join(values)
                + ") AS v(id, ts) WHERE d.id = v.id AND (d.last_seen_at IS NULL OR d.last_seen_at < v.ts)"
            ),
            params,
        )
        total += res.rowcount or 0
    return total


async def online_count() -> int:
    r = await get_redis()
    return int(await r.zcard(k_last_seen()))


async def _download_transitions(device_id: Any, screen_id: Any, prev: dict, data: dict, pending: Optional[int],
                                applied: Optional[int], ts: int) -> None:
    """DOWNLOAD_STARTED/COMPLETED somente em TRANSIÇÕES do heartbeat (nunca a cada heartbeat)."""
    from app.modules.playback import syncevents

    dl = data.get("download") if isinstance(data.get("download"), dict) else None
    done = _int_or_none(dl.get("done_bytes")) if dl else None
    total = _int_or_none(dl.get("total_bytes")) if dl else None
    prev_pending = _int_or_none(prev.get("pending") or None)
    started_ms = _int_or_none(prev.get("dl_started_ms") or None)
    done_flag = prev.get("dl_done") or ""
    extra: dict = {}
    events: list[tuple] = []
    if pending is not None and pending != prev_pending:
        events.append(("DOWNLOAD_STARTED", pending, total, None))
        started_ms, done_flag = ts, ""
        extra.update(dl_started_ms=str(ts), dl_version=str(pending), dl_done="")
    if total is not None:
        extra["dl_total"] = str(total)
    if pending is not None and total and done is not None and done >= total > 0 and not done_flag:
        events.append(("DOWNLOAD_COMPLETED", pending, total, (ts - started_ms) if started_ms else None))
        extra["dl_done"] = "1"
    elif pending is None and prev_pending is not None and applied == prev_pending and not done_flag:
        ptotal = _int_or_none(prev.get("dl_total") or None)
        events.append(("DOWNLOAD_COMPLETED", prev_pending, ptotal, (ts - started_ms) if started_ms else None))
        extra["dl_done"] = "1"
    if extra:
        r = await get_redis()
        await r.hset(k_dev(device_id), mapping=extra)
    for typ, version, nbytes, dur in events:
        await syncevents.record(screen_id, device_id, typ, manifest_version=version, bytes_=nbytes, duration_ms=dur)
