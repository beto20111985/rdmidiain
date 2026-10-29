"""
RD Mídia Indoor — Interface ESTÁVEL do inventário/manifesto (W-INV; plano §4).

Consumidores:
- W-CAMP: ``quote_capacity``, ``hold``, ``convert_hold`` (via ``on_order_paid``),
  ``release_hold``, ``cancel_campaign_bookings``, ``invalidate_screens``.
- W-FIN: ``contracted_exhibitions`` (denominador E_i), ``HoldResult.refund_required``.
- W-PLAY: ``current_manifest``/``generate_manifest``, ``get_manifest_version``,
  ``get_manifest_refs``, ``set_anchor``, ``mark_manifest_applied``,
  ``server_public_keys``/``manifest_public_keys``; ``app.modules.scheduler.resolve``.
- W-ORG: listener de remoção definitiva (registrado no import deste módulo).
- W-MEDIA: referenciador de mídia (manifestos vigentes/recentes — rollback).
"""
from __future__ import annotations

import uuid
from datetime import date, datetime, timedelta
from typing import Any, Optional

import structlog
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import DomainError, NotFound
from app.core.timeutils import local_date
from app.models.inventory import InventoryHold, ScreenInventoryState
from app.models.manifests import ManifestVersion
from app.modules.inventory import sources as S
from app.modules.inventory.booking import (  # noqa: F401  (interface pública)
    cancel_campaign_bookings,
    contracted_exhibitions,
    convert_hold,
    daterange,
    hold,
    load_active_lines,
    on_screens_removed,
    quote_capacity,
    release_hold,
    sweep_expired_holds,
)
from app.modules.inventory.keys import server_public_keys  # noqa: F401
from app.modules.inventory.manifests import (  # noqa: F401
    SignedManifest,
    build_manifest,
    current_manifest,
    generate_manifest,
    get_manifest_refs,
    get_manifest_version,
    invalidate_campaign,
    invalidate_screens,
    mark_manifest_applied,
    media_reference_counts,
    screens_with_creative,
)
from app.modules.inventory.policies import resolve_for_screens
from app.modules.inventory.state import anchor_history, db_now, ms_of, reset_anchor, set_anchor  # noqa: F401
from app.modules.inventory.types import CapacityResult, HoldResult, PatternSpec  # noqa: F401
from app.modules.scheduler.packer import Packer

log = structlog.get_logger("rd.inventory.service")


def manifest_public_keys() -> list[dict]:
    """Alias de ``server_public_keys`` (chaves de MANIFESTO; o W-PLAY usa no pareamento)."""
    return server_public_keys()


# ─── Estado de inventário de uma tela (admin) ────────────────────────────────

async def screen_inventory_view(db: AsyncSession, screen_id: Any) -> dict:
    sid = S._u(screen_id)
    info = (await S.load_screens(db, [sid])).get(sid)
    if info is None:
        raise NotFound()
    st = await db.get(ScreenInventoryState, sid)
    now = await db_now(db)
    today = local_date(now, info.timezone)
    resolver = await resolve_for_screens(db, [info])
    rot, scope = resolver.for_day_scope(info, today)
    latest = (await db.execute(select(ManifestVersion).where(ManifestVersion.screen_id == sid)
                               .order_by(ManifestVersion.version.desc()).limit(1))).scalar_one_or_none()
    applied = (await db.execute(select(func.max(ManifestVersion.version)).where(
        ManifestVersion.screen_id == sid, ManifestVersion.applied_at.is_not(None)))).scalar_one_or_none()
    return {
        "screen_id": str(sid),
        "screen_code": info.code,
        "establishment_id": str(info.establishment_id),
        "timezone": info.timezone,
        "local_date": today.isoformat(),
        "policy": {**rot.summary(), "policy_id": rot.policy_id, "scope": scope or "DEFAULT"},
        "anchor_ms": ms_of(st.anchor_utc) if st and st.anchor_utc else None,
        "anchor_source": st.anchor_source if st else None,
        "anchor_reported_at": st.anchor_reported_at if st else None,
        "sellable": bool(info.sellable and (st.sellable if st else True)),
        "unsellable_reason": st.unsellable_reason if st else None,
        "inventory_version": int(st.inventory_version) if st else 0,
        "manifest_version": int(latest.version) if latest else 0,
        "manifest_generated_at": latest.generated_at if latest else None,
        "manifest_applied_version": int(applied) if applied else None,
        "manifest_unplaced": int(latest.unplaced_count) if latest else 0,
    }


async def external_seconds_by_screen(db: AsyncSession, screen_ids: Any, *, on: Optional[date] = None) -> dict:
    """Segundos do bloco EXTERNO da política vigente (hoje local, ou ``on``) por tela."""
    screens = await S.load_screens(db, screen_ids)
    resolver = await resolve_for_screens(db, screens.values())
    now = await db_now(db)
    return {sid: resolver.for_day(info, on or local_date(now, info.timezone)).external_s
            for sid, info in screens.items()}


# ─── Disponibilidade agregada (sem nomes de anunciantes) ─────────────────────

MAX_AVAIL_DAYS = 31
MAX_AVAIL_SCREENS = 500


async def availability(db: AsyncSession, *, screen_ids: list[uuid.UUID], date_from: date, date_to: date) -> dict:
    """Capacidade × ocupação (BOOKED + reservas válidas) por dia — só números."""
    if date_to < date_from or (date_to - date_from).days + 1 > MAX_AVAIL_DAYS:
        raise DomainError("PERIOD_INVALID", f"Período de 1 a {MAX_AVAIL_DAYS} dias.", status=400)
    if len(screen_ids) > MAX_AVAIL_SCREENS:
        raise DomainError("TOO_MANY_SCREENS", f"Máximo de {MAX_AVAIL_SCREENS} telas por consulta.", status=400)
    screens = await S.load_screens(db, screen_ids)
    resolver = await resolve_for_screens(db, screens.values())
    lines = await load_active_lines(db, list(screens), date_from, date_to)
    camp_ids = {str(a.campaign_id) for v in lines.values() for a in v}
    try:
        competitors = await S.competitor_campaign_pairs(db, camp_ids) if len(camp_ids) >= 2 else frozenset()
    except RuntimeError:
        competitors = frozenset()
    per_day: dict[str, dict] = {}
    sellable = 0
    for sid, info in sorted(screens.items(), key=lambda kv: str(kv[0])):
        if not (info.operational and info.sellable):
            continue
        sellable += 1
        days = [(d, resolver.for_day(info, d)) for d in daterange(date_from, date_to)]
        plans = Packer(str(sid), info.timezone, competitors=competitors, strict=False).pack_days(
            days, [a.line for a in lines.get(sid, [])])
        for (d, rot), p in zip(days, plans):
            cap = len(p.frame.buckets) * rot.units_per_block
            used = p.total_units()
            agg = per_day.setdefault(d.isoformat(), {"date": d.isoformat(), "capacity_units": 0, "used_units": 0})
            agg["capacity_units"] += cap
            agg["used_units"] += used
    out_days = []
    for k in sorted(per_day):
        a = per_day[k]
        a["free_units"] = a["capacity_units"] - a["used_units"]
        a["occupancy_bps"] = (a["used_units"] * 10000 // a["capacity_units"]) if a["capacity_units"] else 0
        out_days.append(a)
    return {"screens": len(screens), "sellable_screens": sellable, "unit_seconds": 30, "days": out_days}


# ─── Pré-visualização da grade (RD) ──────────────────────────────────────────

async def preview_grid(db: AsyncSession, screen_id: Any, *, hours: int = 6, at: Optional[datetime] = None) -> dict:
    """Plano das próximas horas (buckets) — mesmo gerador do manifesto, sem gravar."""
    if not 1 <= hours <= 48:
        raise DomainError("HOURS_INVALID", "hours deve estar entre 1 e 48.", status=400)
    sid = S._u(screen_id)
    b = await build_manifest(db, sid)
    m = b.manifest
    now = at or await db_now(db)
    start = ms_of(now)
    end = start + hours * 3600 * 1000
    c_ms = m["rotation"]["cycle_ms"]
    first = (start // c_ms) * c_ms
    creatives = m["creatives"]
    blocks = []
    for bucket in range(first, end, c_ms):
        entries = m["external_plan"].get(str(bucket), [])
        blocks.append({
            "bucket_ms": bucket,
            "entries": [{"u": e["u"], "d": e["d"], "code": creatives[e["c"]]["code"],
                         "kind": creatives[e["c"]]["kind"], "campaign_id": creatives[e["c"]]["campaign_id"],
                         "creative_id": creatives[e["c"]]["creative_id"]} for e in entries],
        })
    return {
        "screen_id": str(sid), "timezone": m["timezone"], "rotation": m["rotation"], "anchor_ms": m["anchor_ms"],
        "horizon": m["horizon"], "internal_pattern": m["internal_pattern"], "fallback": m["fallback"],
        "emergency": m["emergency"], "revoked": m["revoked"], "blocks": blocks,
        "unplaced": b.unplaced[:200], "plan_sha256": b.plan_sha256,
    }


async def programming_view(db: AsyncSession, screen_id: Any, *, hours: int = 6, at: Optional[datetime] = None) -> dict:
    """Programação prevista para o ESTABELECIMENTO (FE-OPS) — visão SEGURA.

    Entradas dos blocos expõem SOMENTE ``{code, kind}`` (RD-XXXXXX); nunca campaign_id,
    creative_id, anunciante, preços ou ids internos. O padrão interno traz código e
    nome das peças do próprio estabelecimento."""
    from app.models.campaigns import Creative

    if not 1 <= hours <= 48:
        raise DomainError("HOURS_INVALID", "hours deve estar entre 1 e 48.", status=400)
    sid = S._u(screen_id)
    b = await build_manifest(db, sid)
    m = b.manifest
    now = at or await db_now(db)
    start = ms_of(now)
    end = start + hours * 3600 * 1000
    c_ms = m["rotation"]["cycle_ms"]
    creatives = m["creatives"]
    blocks = []
    for bucket in range(((start // c_ms) * c_ms), end, c_ms):
        entries = sorted(m["external_plan"].get(str(bucket), []), key=lambda e: e["u"])
        blocks.append({"bucket_ms": bucket,
                       "entries": [{"code": creatives[e["c"]]["code"], "kind": creatives[e["c"]]["kind"]}
                                   for e in entries]})
    internal_ids = {creatives[x["c"]]["creative_id"] for blk in m["internal_pattern"] for x in blk}
    names = {}
    if internal_ids:
        rows = (await db.execute(select(Creative.id, Creative.name).where(
            Creative.id.in_([uuid.UUID(x) for x in internal_ids])))).all()
        names = {str(i): n for i, n in rows}
    internal = [[{"code": creatives[x["c"]]["code"], "name": names.get(creatives[x["c"]]["creative_id"]),
                  "duration_ms": x["d"]} for x in blk] for blk in m["internal_pattern"]]
    return {
        "screen_id": str(sid), "screen_code": m["screen_code"], "timezone": m["timezone"],
        "rotation": {k: m["rotation"][k] for k in ("internal_ms", "external_ms", "cycle_ms", "unit_ms")},
        "anchor_ms": m["anchor_ms"], "from_ms": start, "to_ms": end, "blocks": blocks, "internal_pattern": internal,
    }


# ─── Holds (consulta) ────────────────────────────────────────────────────────

def hold_dict(h: InventoryHold, screen_ids: Optional[list] = None) -> dict:
    return {
        "id": str(h.id), "org_id": str(h.org_id), "campaign_id": str(h.campaign_id),
        "order_id": str(h.order_id) if h.order_id else None, "status": h.status, "mode": h.mode,
        "created_at": h.created_at, "hold_expires_at": h.hold_expires_at, "converted_at": h.converted_at,
        "released_at": h.released_at, "release_reason": h.release_reason,
        "screens_requested": h.screens_requested, "screens_held": h.screens_held,
        "replaces_hold_id": str(h.replaces_hold_id) if h.replaces_hold_id else None,
        "screen_ids": [str(s) for s in (screen_ids or [])],
    }


# ─── Registros em outros domínios ────────────────────────────────────────────

def _register_integrations() -> None:
    reg = S._fn("app.modules.screens.service", "register_screen_removed_listener")
    if reg is not None:
        reg(on_screens_removed)
    else:  # pragma: no cover
        log.warning("screen_removed_listener_not_registered")
    mreg = S._fn("app.modules.media.service", "register_media_referencer")
    if mreg is not None:
        mreg("inventory.manifests", media_reference_counts)
    else:  # pragma: no cover
        log.warning("media_referencer_not_registered")


_register_integrations()
