"""
RD Mídia Indoor — Histórico de sincronização/download dos players (ETAPA2 #798–#799; P001 §103).

Tipos: MANIFEST_RECEIVED (``GET /player/manifest`` 200), DOWNLOAD_STARTED/DOWNLOAD_COMPLETED
(TRANSIÇÕES observadas no heartbeat — nunca a cada heartbeat), MANIFEST_APPLIED
(``manifest_applied``; ``duration_ms`` = recebimento → aplicação da versão),
MANIFEST_FAILED, MEDIA_ERROR. Retenção: ``player.sync_events_retention_days``.

``sync_report(db, *, screen_ids, org_id, start, end, group_by)``: contagens por grupo e tipo,
latência até aplicar a versão (n, média, p50, p95, máx.) e falhas por mídia — para o W-BIZ.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timedelta
from typing import Any, Iterable, Optional

import structlog
from sqlalchemy import delete, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.redis import get_redis, rkey
from app.core.settings_service import SettingDef, get_setting, register
from app.core.timeutils import utcnow, validate_timezone
from app.models.devices import DeviceSyncEvent, SyncEventType

log = structlog.get_logger("rd.playback.sync")

register(
    SettingDef(
        "player.sync_events_retention_days",
        180,
        "int",
        "Retenção do histórico de sincronização/download dos players (dias).",
        min=7,
        max=730,
        category="players",
    )
)

TYPES = tuple(t.value for t in SyncEventType)
GROUPS = ("screen", "day", "org", "establishment")
RECV_TTL_S = 14 * 24 * 3600


def _k_recv(device_id: Any, version: int) -> str:
    return rkey("rdpp", "recv", device_id, int(version))


async def note_received(device_id: Any, version: int, at_ms: int) -> None:
    """Marca o 1º recebimento da versão pelo dispositivo (base da latência até aplicar)."""
    r = await get_redis()
    await r.set(_k_recv(device_id, version), str(at_ms), ex=RECV_TTL_S, nx=True)


async def received_at_ms(device_id: Any, version: int) -> Optional[int]:
    r = await get_redis()
    v = await r.get(_k_recv(device_id, version))
    return int(v) if v else None


def build(screen_id: Any, device_id: Any, type: str, *, scope: Optional[dict] = None, manifest_version: Optional[int] = None,
          media_id: Any = None, bytes_: Optional[int] = None, duration_ms: Optional[int] = None,
          reason: Optional[str] = None, occurred_at: Optional[datetime] = None) -> DeviceSyncEvent:
    if type not in TYPES:
        raise ValueError(f"tipo de evento de sincronização inválido: {type}")
    sc = scope or {}
    mid = None
    if media_id:
        try:
            mid = uuid.UUID(str(media_id))
        except ValueError:
            mid = None
    return DeviceSyncEvent(
        screen_id=uuid.UUID(str(screen_id)),
        device_id=uuid.UUID(str(device_id)),
        org_id=uuid.UUID(sc["org_id"]) if sc.get("org_id") else None,
        establishment_id=uuid.UUID(sc["establishment_id"]) if sc.get("establishment_id") else None,
        type=type,
        manifest_version=manifest_version,
        media_id=mid,
        bytes=bytes_ if isinstance(bytes_, int) and not isinstance(bytes_, bool) and bytes_ >= 0 else None,
        duration_ms=duration_ms if isinstance(duration_ms, int) and duration_ms >= 0 else None,
        reason=(reason or None) and str(reason)[:255],
        occurred_at=occurred_at or utcnow(),
    )


async def record(screen_id: Any, device_id: Any, type: str, *, db: Optional[AsyncSession] = None, **kw: Any) -> None:
    """Grava um evento (sessão curta própria quando ``db`` não é dado). Falha não derruba o WS."""
    from app.modules.presence import service as presence

    try:
        scope = await presence.screen_scope(screen_id)
        ev = build(screen_id, device_id, type, scope=scope, **kw)
        if db is not None:
            db.add(ev)
            return
        async with presence.short_session() as s:
            s.add(ev)
    except Exception as exc:
        log.error("sync_event_record_failed", event_type=str(type), error_type=exc.__class__.__name__)


async def purge(db: AsyncSession, *, now: Optional[datetime] = None) -> int:
    days = int(await get_setting(db, "player.sync_events_retention_days"))
    res = await db.execute(delete(DeviceSyncEvent).where(DeviceSyncEvent.occurred_at < (now or utcnow()) - timedelta(days=days)))
    return res.rowcount or 0


def _where(screen_ids: Optional[Iterable[Any]], org_id: Any, start: datetime, end: datetime) -> tuple[str, dict]:
    clauses = ["occurred_at >= :start", "occurred_at < :end"]
    params: dict = {"start": start, "end": end}
    if screen_ids is not None:
        ids = [uuid.UUID(str(x)) for x in screen_ids]
        if not ids:
            clauses.append("false")
        else:
            clauses.append("screen_id = ANY(:screens)")
            params["screens"] = ids
    if org_id is not None:
        clauses.append("org_id = :org")
        params["org"] = uuid.UUID(str(org_id))
    return " AND ".join(clauses), params


async def sync_report(
    db: AsyncSession,
    *,
    screen_ids: Optional[Iterable[Any]] = None,
    org_id: Any = None,
    start: datetime,
    end: datetime,
    group_by: str = "screen",
    tz: str = "America/Sao_Paulo",
) -> dict:
    """Relatório de sincronizações/downloads (o chamador aplica o escopo de permissão)."""
    if group_by not in GROUPS:
        raise ValueError(f"group_by inválido: {group_by}")
    validate_timezone(tz)
    where, params = _where(screen_ids, org_id, start, end)
    params["tz"] = tz
    key = {
        "screen": "screen_id::text",
        "org": "org_id::text",
        "establishment": "establishment_id::text",
        "day": "to_char((occurred_at AT TIME ZONE :tz)::date, 'YYYY-MM-DD')",
    }[group_by]
    counts = (
        await db.execute(
            text(f"SELECT {key} AS k, type, count(*) AS n, COALESCE(sum(bytes), 0) AS b FROM device_sync_events "
                 f"WHERE {where} GROUP BY 1, 2 ORDER BY 1, 2"),
            params,
        )
    ).all()
    groups: dict[str, dict] = {}
    for k, typ, n, b in counts:
        g = groups.setdefault(k or "", {"key": k, "counts": {t: 0 for t in TYPES}, "bytes_downloaded": 0})
        g["counts"][typ] = int(n)
        if typ == SyncEventType.DOWNLOAD_COMPLETED.value:
            g["bytes_downloaded"] += int(b)
    lat = (
        await db.execute(
            text(f"SELECT {key} AS k, count(duration_ms) AS n, avg(duration_ms) AS avg_ms, "
                 "percentile_cont(0.5) WITHIN GROUP (ORDER BY duration_ms) AS p50, "
                 "percentile_cont(0.95) WITHIN GROUP (ORDER BY duration_ms) AS p95, max(duration_ms) AS max_ms "
                 f"FROM device_sync_events WHERE {where} AND type = 'MANIFEST_APPLIED' AND duration_ms IS NOT NULL "
                 "GROUP BY 1"),
            params,
        )
    ).all()
    for k, n, avg_ms, p50, p95, mx in lat:
        g = groups.setdefault(k or "", {"key": k, "counts": {t: 0 for t in TYPES}, "bytes_downloaded": 0})
        g["time_to_apply_ms"] = {"n": int(n), "avg": int(avg_ms) if avg_ms is not None else None,
                                 "p50": int(p50) if p50 is not None else None, "p95": int(p95) if p95 is not None else None,
                                 "max": int(mx) if mx is not None else None}
    for g in groups.values():
        g.setdefault("time_to_apply_ms", {"n": 0, "avg": None, "p50": None, "p95": None, "max": None})
    failures = (
        await db.execute(
            text("SELECT media_id::text, count(*) AS n, count(DISTINCT screen_id) AS screens, max(occurred_at) AS last_at, "
                 "(array_agg(reason ORDER BY occurred_at DESC))[1] AS last_reason FROM device_sync_events "
                 f"WHERE {where} AND type = 'MEDIA_ERROR' AND media_id IS NOT NULL GROUP BY media_id ORDER BY n DESC LIMIT 100"),
            params,
        )
    ).all()
    return {
        "group_by": group_by,
        "start": start.isoformat(),
        "end": end.isoformat(),
        "groups": sorted(groups.values(), key=lambda g: str(g["key"])),
        "media_failures": [
            {"media_id": m, "count": int(n), "screens": int(sc), "last_at": la.isoformat() if la else None,
             "last_reason": lr}
            for m, n, sc, la, lr in failures
        ],
    }
