"""
RD Mídia Indoor — Cotação, reserva (hold 2 h), conversão, liberação e expiração (W-INV).

Algoritmo de reserva (A6 §10.3, ADR_006 §5–§6):
  READ COMMITTED; ``SET LOCAL lock_timeout``; lock da campanha; idempotência por
  (org, Idempotency-Key) com hash do pedido; elegibilidade (tela operacional e
  vendável, conflito ALLOW gravado, duração cabe no bloco); LOCK das telas em ordem
  (``ORDER BY screen_id FOR UPDATE``); NOVO statement lê BOOKED + HELD não vencidos
  (expiração LAZY pelo relógio do banco); recálculo com o MESMO empacotador do
  manifesto (estrito: nenhuma ficha sem posição); exclusividade concorrencial;
  tudo-ou-nada por padrão (modo parcial explícito); INSERT do hold com
  ``hold_expires_at = now() + inventory.reservation_hours``.

Conversão (A6 §10.4): lock do hold; HELD válido → CONVERTED/BOOKED; vencido ou
cancelado → recaptura sob lock das telas; sem capacidade → PAID_WITHOUT_INVENTORY +
alerta + sinal de estorno (nunca overbooking). Idempotente.

Sweeper set-based (beat 60 s) com ``FOR UPDATE SKIP LOCKED`` — nunca disputa com a
conversão em andamento; a expiração lazy garante a venda mesmo com o sweeper atrasado.
"""
from __future__ import annotations

import hashlib
import json
import uuid
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Any, Iterable, Optional, Sequence

import structlog
from sqlalchemy import and_, func, or_, select, text, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.audit import audit
from app.core.errors import Conflict, DomainError, NotFound
from app.core.settings_service import get_setting
from app.core.timeutils import local_date
from app.models.inventory import BookingPattern, BookingScreen, InventoryHold, ScreenInventoryState, ScreenRetirement
from app.modules.inventory import notify as N
from app.modules.inventory import sources as S
from app.modules.inventory.policies import PolicyResolver, resolve_for_screens
from app.modules.inventory.state import db_now, ensure_read_committed, ensure_states, lock_screens, set_lock_timeout
from app.modules.inventory.types import (
    EXCLUSIVITY_COMPETITOR,
    CapacityResult,
    HoldResult,
    PatternSpec,
    ScreenQuote,
    line_from_row,
)
from app.modules.scheduler.packer import (
    Line,
    Packer,
    PackingError,
    day_frame,
    demand_units_per_hour,
    line_insertions_on_day,
    validate_line,
)
from app.modules.scheduler.policy import RotationSpec

log = structlog.get_logger("rd.inventory.booking")

CONVERTED_RELEASE_REASONS = frozenset({"SUPERSEDED_BY_REVISION", "CAMPAIGN_CANCELLED", "REFUNDED"})
ACTIVE_HELD_OR_BOOKED = ("HELD", "BOOKED")


def _u(v: Any) -> uuid.UUID:
    return v if isinstance(v, uuid.UUID) else uuid.UUID(str(v))


def daterange(a: date, b: date) -> list[date]:
    return [a + timedelta(days=i) for i in range((b - a).days + 1)]


def _overlap(a_from: date, a_to: date, b_from: date, b_to: date) -> bool:
    return a_from <= b_to and b_from <= a_to


@dataclass
class ActiveLine:
    line: Line
    pattern: BookingPattern
    screen_id: uuid.UUID
    status: str
    hold_id: uuid.UUID
    campaign_id: uuid.UUID


async def load_active_lines(
    db: AsyncSession,
    screen_ids: Iterable[Any],
    d_from: date,
    d_to: date,
    *,
    exclude_hold_ids: Iterable[Any] = (),
    statuses: Sequence[str] = ACTIVE_HELD_OR_BOOKED,
) -> dict[uuid.UUID, list[ActiveLine]]:
    """BOOKED + HELD NÃO VENCIDOS (lazy, relógio do banco) que tocam [d_from, d_to]."""
    ids = sorted({_u(s) for s in screen_ids}, key=str)
    out: dict[uuid.UUID, list[ActiveLine]] = {s: [] for s in ids}
    if not ids:
        return out
    conds = []
    if "BOOKED" in statuses:
        conds.append(BookingScreen.status == "BOOKED")
    if "HELD" in statuses:
        conds.append(and_(BookingScreen.status == "HELD", BookingScreen.hold_expires_at > func.now()))
    q = (select(BookingScreen.screen_id, BookingScreen.status, BookingScreen.hold_id, BookingPattern)
         .join(BookingPattern, BookingPattern.id == BookingScreen.pattern_id)
         .where(BookingScreen.screen_id.in_(ids), or_(*conds),
                BookingPattern.date_to >= d_from, BookingPattern.date_from <= d_to))
    excl = [_u(h) for h in exclude_hold_ids if h is not None]
    if excl:
        q = q.where(BookingScreen.hold_id.notin_(excl))
    for sid, st, hid, bp in (await db.execute(q)).all():
        out[sid].append(ActiveLine(line_from_row(bp), bp, sid, st, hid, bp.campaign_id))
    for v in out.values():
        v.sort(key=lambda a: a.line.line_id)
    return out


def _request_hash(campaign_id: Any, screen_ids: Sequence[Any], specs: Sequence[PatternSpec], partial: bool,
                  replaces_hold_id: Any, order_id: Any) -> str:
    doc = {
        "campaign_id": str(campaign_id),
        "screen_ids": sorted(str(s) for s in screen_ids),
        "patterns": [p.canonical() for p in specs],
        "partial": bool(partial),
        "replaces_hold_id": str(replaces_hold_id) if replaces_hold_id else None,
        "order_id": str(order_id) if order_id else None,
    }
    return hashlib.sha256(json.dumps(doc, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


async def _ttl_seconds(db: AsyncSession, ttl: Any) -> int:
    if ttl is None:
        hours = int(await get_setting(db, "inventory.reservation_hours"))
        return hours * 3600
    secs = int(ttl.total_seconds()) if isinstance(ttl, timedelta) else int(ttl)
    if not 60 <= secs <= 48 * 3600:
        raise DomainError("HOLD_TTL_INVALID", "TTL da reserva fora de 1 min..48 h.", status=400)
    return secs


# ─── Avaliação (núcleo comum de cotação, reserva e recaptura) ────────────────

@dataclass
class _Eval:
    result: CapacityResult
    screens: dict
    resolver: PolicyResolver
    competitors: frozenset
    days_by_screen: dict


def _rot_snapshot(rot: RotationSpec) -> dict:
    return {"internal_seconds": rot.internal_s, "external_seconds": rot.external_s, "cycle_seconds": rot.cycle_s,
            "policy_id": rot.policy_id}


async def _evaluate(
    db: AsyncSession,
    *,
    campaign_id: Optional[uuid.UUID],
    advertiser_org_id: Optional[uuid.UUID],
    screen_ids: Sequence[uuid.UUID],
    new_lines_by_spec: list[tuple[PatternSpec, Line]],
    exclude_hold_ids: Sequence[uuid.UUID] = (),
    record_conflicts: bool = False,
    ctx: Any = None,
    states: Optional[dict] = None,
    check_conflicts: bool = True,
) -> _Eval:
    screens = await S.load_screens(db, screen_ids)
    resolver = await resolve_for_screens(db, screens.values())
    quotes: dict[uuid.UUID, ScreenQuote] = {}
    if states is None:
        states = {r.screen_id: r for r in (await db.execute(
            select(ScreenInventoryState).where(ScreenInventoryState.screen_id.in_(list(screens))))).scalars().all()}
    eligible: list[uuid.UUID] = []
    for sid in screen_ids:
        info = screens.get(sid)
        if info is None:
            quotes[sid] = ScreenQuote(sid, False, False, "SCREEN_NOT_FOUND", "Tela inexistente.")
            continue
        st = states.get(sid)
        if not info.operational:
            quotes[sid] = ScreenQuote(sid, False, False, "SCREEN_NOT_OPERATIONAL",
                                      "Tela inativa ou estabelecimento inativo.")
            continue
        if not info.sellable or (st is not None and not st.sellable):
            quotes[sid] = ScreenQuote(sid, False, False, "SCREEN_NOT_SELLABLE", "Tela fora do inventário vendável.")
            continue
        quotes[sid] = ScreenQuote(sid, True, False)
        eligible.append(sid)

    # conflito anúncio × estabelecimento (falha fechada)
    if campaign_id is not None and check_conflicts and eligible:
        ests = {screens[s].establishment_id for s in eligible}
        verdicts = await S.screen_conflicts(db, campaign_id, ests, record=record_conflicts, ctx=ctx)
        for sid in list(eligible):
            v = verdicts.get(screens[sid].establishment_id, S.UNAVAILABLE)
            q = quotes[sid]
            q.conflict_decision_id = v.decision_id
            q.details["conflict"] = {"result": v.result, "reason_code": v.reason_code}
            if not v.allowed:
                q.eligible = False
                q.reason = "CONFLICT_BLOCKED" if v.reason_code != S.UNAVAILABLE.reason_code else v.reason_code
                q.message = "Bloqueado pela política de concorrência/conteúdo do estabelecimento."
                eligible.remove(sid)

    # dias a verificar por tela: dias (≥ hoje local) em que alguma linha nova está ativa
    days_by_screen: dict[uuid.UUID, list[date]] = {}
    lo = min((ln.date_from for _, ln in new_lines_by_spec), default=None)
    hi = max((ln.date_to for _, ln in new_lines_by_spec), default=None)
    now = await db_now(db)
    for sid in list(eligible):
        info = screens[sid]
        today = local_date(now, info.timezone)
        days = [d for d in daterange(max(lo, today), hi) if any(ln.active_on(d) for _, ln in new_lines_by_spec)] \
            if lo and hi and hi >= today else []
        days_by_screen[sid] = days
        q = quotes[sid]
        rot0 = resolver.for_day(info, days[0] if days else today)
        q.units_per_hour = rot0.units_per_hour
        q.demand_units_per_hour = sum(demand_units_per_hour(ln, rot0) for _, ln in new_lines_by_spec)
        if lo and hi and hi < today:
            q.eligible, q.reason, q.message = False, "PERIOD_IN_PAST", "Período já encerrado."
            eligible.remove(sid)
            continue
        # elegibilidade de duração/frequência em cada política vigente no período
        try:
            seen: set = set()
            for d in days:
                rot = resolver.for_day(info, d)
                for _, ln in new_lines_by_spec:
                    if ln.active_on(d) and (rot.key(), ln.line_id) not in seen:
                        seen.add((rot.key(), ln.line_id))
                        validate_line(ln, rot)
        except PackingError as exc:
            q.eligible, q.reason, q.message = False, exc.code, exc.message
            q.details.update(exc.details)
            eligible.remove(sid)

    existing = await load_active_lines(db, eligible, lo or date.min, hi or date.max,
                                       exclude_hold_ids=exclude_hold_ids) if eligible else {}
    camp_ids = {str(campaign_id)} if campaign_id else {"__quote__"}
    for lst in existing.values():
        camp_ids |= {str(a.campaign_id) for a in lst}
    real_ids = [c for c in camp_ids if c != "__quote__"]
    try:
        competitors = await S.competitor_campaign_pairs(db, real_ids) if len(real_ids) >= 2 else frozenset()
    except RuntimeError:
        competitors = None  # motor indisponível ⇒ falha fechada abaixo
    for sid in list(eligible):
        q = quotes[sid]
        if competitors is None:
            q.eligible, q.reason = False, "CONFLICT_ENGINE_UNAVAILABLE"
            q.message = "Motor de concorrência indisponível; venda bloqueada."
            eligible.remove(sid)
            continue
        info = screens[sid]
        ex = existing.get(sid, [])
        # exclusividade concorrencial (ETAPA2 #492)
        excl_hit = None
        if campaign_id is not None:
            for al in ex:
                if al.campaign_id == campaign_id:
                    continue
                if frozenset((str(al.campaign_id), str(campaign_id))) not in competitors:
                    continue
                for spec, ln in new_lines_by_spec:
                    if _overlap(ln.date_from, ln.date_to, al.pattern.date_from, al.pattern.date_to) and (
                            spec.exclusivity == EXCLUSIVITY_COMPETITOR
                            or al.pattern.exclusivity == EXCLUSIVITY_COMPETITOR):
                        excl_hit = al
                        break
                if excl_hit:
                    break
        if excl_hit is not None:
            q.eligible, q.reason = False, "EXCLUSIVITY_CONFLICT"
            q.message = "Há exclusividade concorrencial contratada nesta tela no período."
            eligible.remove(sid)
            continue
        packer = Packer(str(sid), info.timezone, competitors=competitors, strict=True)
        try:
            packer.pack_days([(d, resolver.for_day(info, d)) for d in days_by_screen[sid]],
                             [a.line for a in ex] + [ln for _, ln in new_lines_by_spec])
            q.feasible = True
        except PackingError as exc:
            q.feasible, q.reason, q.message = False, exc.code, exc.message
            q.details.update(exc.details)
        q.details["packs"] = packer.packs
    result = CapacityResult(
        feasible=bool(screen_ids) and all(quotes[s].eligible and quotes[s].feasible for s in screen_ids),
        screens=[quotes[s] for s in screen_ids],
    )
    return _Eval(result, screens, resolver, competitors or frozenset(), days_by_screen)


def _new_lines(specs: Sequence[PatternSpec], pattern_ids: Sequence[uuid.UUID], campaign_id: Any,
               advertiser_id: Any) -> list[tuple[PatternSpec, Line]]:
    return [(sp, sp.to_line(str(pid), str(campaign_id), str(advertiser_id))) for sp, pid in zip(specs, pattern_ids)]


# ─── Cotação ─────────────────────────────────────────────────────────────────

async def quote_capacity(db: AsyncSession, ctx: Any, screen_ids: Sequence[Any], patterns: Sequence[Any], *,
                         campaign_id: Any = None, advertiser_org_id: Any = None) -> CapacityResult:
    """Cabimento SEM reservar (sem locks). Mesmo empacotador da reserva/manifesto."""
    specs = [PatternSpec.parse(p) for p in patterns]
    if not specs:
        raise DomainError("PATTERN_REQUIRED", "Informe ao menos um padrão de contratação.", status=400)
    sids = [_u(s) for s in dict.fromkeys(screen_ids)]
    cid = _u(campaign_id) if campaign_id else None
    adv = advertiser_org_id
    if cid is not None and adv is None:
        c = (await S.load_campaigns(db, [cid])).get(cid)
        if c is None:
            raise NotFound()
        adv = c.org_id
    pids = [uuid.uuid5(uuid.NAMESPACE_URL, f"quote:{cid}:{i}") for i in range(len(specs))]
    ev = await _evaluate(db, campaign_id=cid, advertiser_org_id=adv, screen_ids=sids,
                         new_lines_by_spec=_new_lines(specs, pids, cid or "__quote__", adv or "__quote__"),
                         check_conflicts=cid is not None)
    return ev.result


# ─── Reserva (hold) ──────────────────────────────────────────────────────────

async def _hold_screen_ids(db: AsyncSession, hold_id: uuid.UUID) -> list[uuid.UUID]:
    rows = (await db.execute(select(BookingScreen.screen_id).where(BookingScreen.hold_id == hold_id)
                             .distinct())).scalars().all()
    return sorted(set(rows), key=str)


async def _replay(db: AsyncSession, hold: InventoryHold) -> InventoryHold:
    hold.replayed = True  # type: ignore[attr-defined]
    hold.held_screen_ids = await _hold_screen_ids(db, hold.id)  # type: ignore[attr-defined]
    hold.excluded = list((hold.details or {}).get("excluded", []))  # type: ignore[attr-defined]
    return hold


async def hold(
    db: AsyncSession,
    ctx: Any,
    campaign_id: Any,
    screen_ids: Sequence[Any],
    patterns: Sequence[Any],
    idempotency_key: str,
    ttl: Any = None,
    *,
    partial: bool = False,
    order_id: Any = None,
    replaces_hold_id: Any = None,
) -> InventoryHold:
    """Reserva temporária (2 h) — algoritmo A6 §10.3. Ver docstring do módulo."""
    if not idempotency_key or len(idempotency_key) > 255:
        raise DomainError("IDEMPOTENCY_KEY_REQUIRED", "Idempotency-Key obrigatória.", status=400)
    specs = [PatternSpec.parse(p) for p in patterns]
    if not specs:
        raise DomainError("PATTERN_REQUIRED", "Informe ao menos um padrão de contratação.", status=400)
    sids = [_u(s) for s in dict.fromkeys(screen_ids)]
    if not sids:
        raise DomainError("SCREENS_REQUIRED", "Nenhuma tela informada para a reserva.", status=400)
    cid = _u(campaign_id)
    await ensure_read_committed(db)
    await set_lock_timeout(db)

    camps = await S.load_campaigns(db, [cid], for_update=True)
    camp = camps.get(cid)
    if camp is None:
        raise NotFound()
    if ctx is not None and getattr(ctx, "kind", "user") == "user":
        if not (ctx.can("checkout.create", org_id=camp.org_id) or ctx.can("inventory.reserve", org_id=camp.org_id)):
            raise NotFound()
    rhash = _request_hash(cid, sids, specs, partial, replaces_hold_id, order_id)
    existing = (await db.execute(select(InventoryHold).where(
        InventoryHold.org_id == camp.org_id, InventoryHold.idempotency_key == idempotency_key))).scalar_one_or_none()
    if existing is not None:
        if existing.request_hash != rhash:
            raise Conflict("IDEMPOTENCY_KEY_REUSED", "Idempotency-Key já usada com outro pedido.")
        return await _replay(db, existing)

    replaces = None
    if replaces_hold_id is not None:
        replaces = await db.get(InventoryHold, _u(replaces_hold_id))
        if replaces is None or replaces.campaign_id != cid or replaces.status != "CONVERTED":
            raise DomainError("REPLACES_HOLD_INVALID", "A reserva substituída deve ser a contratação vigente "
                              "(CONVERTED) da mesma campanha.", status=400)

    # expiração lazy da reserva anterior desta campanha (libera o índice parcial HELD)
    await db.execute(update(InventoryHold).where(
        InventoryHold.campaign_id == cid, InventoryHold.status == "HELD", InventoryHold.hold_expires_at <= func.now()
    ).values(status="EXPIRED", released_at=func.now(), release_reason="TTL_LAZY", updated_at=func.now()))
    active = (await db.execute(select(InventoryHold.id).where(
        InventoryHold.campaign_id == cid, InventoryHold.status == "HELD"))).scalar_one_or_none()
    if active is not None:
        raise Conflict("HOLD_ALREADY_ACTIVE", "Esta campanha já possui uma reserva ativa.",
                       details={"hold_id": str(active)})

    screens = await S.load_screens(db, sids)
    await ensure_states(db, screens)
    states = await lock_screens(db, screens.keys())  # ORDER BY screen_id FOR UPDATE
    pattern_ids = [uuid.uuid4() for _ in specs]
    new_lines = _new_lines(specs, pattern_ids, cid, camp.org_id)
    ev = await _evaluate(db, campaign_id=cid, advertiser_org_id=camp.org_id, screen_ids=sids,
                         new_lines_by_spec=new_lines,
                         exclude_hold_ids=[replaces.id] if replaces else [], record_conflicts=True, ctx=ctx,
                         states=states)
    ok = [q for q in ev.result.screens if q.eligible and q.feasible]
    bad = [q for q in ev.result.screens if not (q.eligible and q.feasible)]
    if (bad and not partial) or not ok:
        raise Conflict("INVENTORY_UNAVAILABLE",
                       "Não há inventário suficiente para a contratação nas telas solicitadas.",
                       details={"available": len(ok), "requested": len(sids),
                                "screens": [q.to_dict() for q in bad][:200]})

    ttl_s = await _ttl_seconds(db, ttl)
    hold_id = uuid.uuid4()
    excluded = [q.to_dict() for q in bad]
    ins = await db.execute(
        pg_insert(InventoryHold).values(
            id=hold_id, org_id=camp.org_id, campaign_id=cid, order_id=_u(order_id) if order_id else None,
            replaces_hold_id=replaces.id if replaces else None, status="HELD",
            mode="PARTIAL" if partial else "ALL_OR_NOTHING", idempotency_key=idempotency_key, request_hash=rhash,
            hold_expires_at=func.now() + timedelta(seconds=ttl_s), screens_requested=len(sids),
            screens_held=len(ok), details={"excluded": excluded} if excluded else None,
            created_by=getattr(ctx, "user_id", None),
        ).on_conflict_do_nothing().returning(InventoryHold.id, InventoryHold.hold_expires_at,
                                             InventoryHold.created_at)
    )
    row = ins.first()
    if row is None:  # corrida com a mesma chave (ou outro HELD da campanha)
        again = (await db.execute(select(InventoryHold).where(
            InventoryHold.org_id == camp.org_id, InventoryHold.idempotency_key == idempotency_key)
        )).scalar_one_or_none()
        if again is not None and again.request_hash == rhash:
            return await _replay(db, again)
        raise Conflict("HOLD_CONFLICT", "Reserva concorrente para a mesma campanha/chave.")
    expires_at = row[1]
    for sp, pid in zip(specs, pattern_ids):
        db.add(BookingPattern(
            id=pid, hold_id=hold_id, campaign_id=cid, advertiser_org_id=camp.org_id, date_from=sp.date_from,
            date_to=sp.date_to, days_mask=sp.days_mask, holiday_mode=sp.holiday_mode,
            time_bands=[list(b) for b in sp.time_bands], duration_units=sp.duration_units, freq_kind=sp.freq_kind,
            freq_value=sp.freq_value, specific_times=list(sp.specific_times) or None,
            min_interval_seconds=sp.min_interval_seconds, priority=sp.priority,
            allow_same_advertiser_consecutive=sp.allow_same_advertiser_consecutive, exclusivity=sp.exclusivity,
            per_day_distribution=sp.distribution(),
        ))
    await db.flush()
    for q in ok:
        info = ev.screens[q.screen_id]
        rot = ev.resolver.for_day(info, min(sp.date_from for sp in specs))
        for pid in pattern_ids:
            db.add(BookingScreen(
                pattern_id=pid, screen_id=q.screen_id, hold_id=hold_id, campaign_id=cid, status="HELD",
                hold_expires_at=expires_at, conflict_decision_id=q.conflict_decision_id,
                conflict_result="ALLOW", conflict_reason=(q.details.get("conflict") or {}).get("reason_code"),
                tz_snapshot=info.timezone, policy_id_snapshot=_u(rot.policy_id) if rot.policy_id else None,
                rotation_snapshot=_rot_snapshot(rot),
            ))
    held_ids = sorted((q.screen_id for q in ok), key=str)
    await db.flush()
    await db.execute(update(ScreenInventoryState).where(ScreenInventoryState.screen_id.in_(held_ids)).values(
        inventory_version=ScreenInventoryState.inventory_version + 1, updated_at=func.now()))
    await audit(db, ctx, "inventory.hold_created", "inventory_hold", hold_id,
                after={"campaign_id": str(cid), "screens_held": len(ok), "screens_requested": len(sids),
                       "mode": "PARTIAL" if partial else "ALL_OR_NOTHING", "hold_expires_at": expires_at,
                       "ttl_seconds": ttl_s, "patterns": [sp.canonical() for sp in specs],
                       "replaces_hold_id": str(replaces.id) if replaces else None},
                org_id=camp.org_id)
    N.defer_event(db, "inventory.updated", {"hold_id": str(hold_id), "campaign_id": str(cid), "status": "HELD",
                                            "screens": len(held_ids)}, org_id=camp.org_id)
    h = await db.get(InventoryHold, hold_id)
    assert h is not None
    h.replayed = False  # type: ignore[attr-defined]
    h.held_screen_ids = held_ids  # type: ignore[attr-defined]
    h.excluded = excluded  # type: ignore[attr-defined]
    return h


# ─── Conversão no pagamento ──────────────────────────────────────────────────

async def _lock_hold(db: AsyncSession, hold_id: Any) -> InventoryHold:
    await ensure_read_committed(db)
    await set_lock_timeout(db)
    h = (await db.execute(select(InventoryHold).where(InventoryHold.id == _u(hold_id)).with_for_update()
                          .execution_options(populate_existing=True))).scalar_one_or_none()
    if h is None:
        raise NotFound()
    return h


async def convert_hold(db: AsyncSession, hold_id: Any) -> HoldResult:
    """Pagamento confirmado → contratação definitiva (idempotente; nunca overbooking)."""
    h = await _lock_hold(db, hold_id)
    if h.status in ("CONVERTED", "PAID_WITHOUT_INVENTORY"):
        return HoldResult(h.id, h.status, already_processed=True,
                          refund_required=h.status == "PAID_WITHOUT_INVENTORY",
                          screen_ids=await _hold_screen_ids(db, h.id))
    now = await db_now(db)
    screen_ids = await _hold_screen_ids(db, h.id)
    if h.status == "HELD" and h.hold_expires_at > now:
        await lock_screens(db, screen_ids)
        await db.execute(update(BookingScreen).where(BookingScreen.hold_id == h.id, BookingScreen.status == "HELD")
                         .values(status="BOOKED", hold_expires_at=None, updated_at=func.now()))
        h.status, h.converted_at = "CONVERTED", now
        await _after_status(db, h, screen_ids, "inventory.hold_converted", {"path": "IN_TIME"})
        return HoldResult(h.id, "CONVERTED", screen_ids=screen_ids)

    # pagamento tardio (vencido / expirado / cancelado): recaptura sob lock das telas
    prev_status = h.status
    patterns = (await db.execute(select(BookingPattern).where(BookingPattern.hold_id == h.id)
                                 .order_by(BookingPattern.id))).scalars().all()
    await ensure_states(db, await S.load_screens(db, screen_ids))
    states = await lock_screens(db, screen_ids)
    camp = (await S.load_campaigns(db, [h.campaign_id])).get(h.campaign_id)
    new_lines = [(PatternSpec.parse({
        "date_from": p.date_from, "date_to": p.date_to, "days_mask": p.days_mask, "time_bands": p.time_bands,
        "duration_units": p.duration_units, "freq_kind": p.freq_kind, "freq_value": p.freq_value,
        "specific_times": p.specific_times, "min_interval_seconds": p.min_interval_seconds,
        "priority": p.priority, "allow_same_advertiser_consecutive": p.allow_same_advertiser_consecutive,
        "exclusivity": p.exclusivity}), line_from_row(p)) for p in patterns]
    ev = await _evaluate(db, campaign_id=h.campaign_id, advertiser_org_id=h.org_id, screen_ids=screen_ids,
                         new_lines_by_spec=new_lines, exclude_hold_ids=[h.id], states=states)
    if screen_ids and ev.result.feasible and camp is not None:
        await db.execute(update(BookingScreen).where(
            BookingScreen.hold_id == h.id, BookingScreen.status.in_(("HELD", "EXPIRED", "CANCELLED"))
        ).values(status="BOOKED", hold_expires_at=None, status_reason="RECAPTURED", updated_at=func.now()))
        h.status, h.converted_at = "CONVERTED", now
        h.details = {**(h.details or {}), "recaptured_from": prev_status}
        await _after_status(db, h, screen_ids, "inventory.hold_converted", {"path": "RECAPTURED",
                                                                             "from": prev_status})
        return HoldResult(h.id, "CONVERTED", screen_ids=screen_ids, reason="RECAPTURED")

    # sem capacidade: PAGO SEM INVENTÁRIO (alerta + sinal de estorno). Nunca overbooking.
    await db.execute(update(BookingScreen).where(BookingScreen.hold_id == h.id, BookingScreen.status == "HELD")
                     .values(status="EXPIRED", status_reason="PAID_LATE_NO_CAPACITY", updated_at=func.now()))
    reasons = [q.to_dict() for q in ev.result.screens if not (q.eligible and q.feasible)][:50]
    h.status = "PAID_WITHOUT_INVENTORY"
    h.details = {**(h.details or {}), "paid_without_inventory": {"from": prev_status, "screens": reasons}}
    await _after_status(db, h, screen_ids, "inventory.paid_without_inventory", {"from": prev_status,
                                                                                 "screens": reasons})
    name = camp.name if camp else str(h.campaign_id)
    for perm in ("finance.view", "inventory.view"):
        await N.notify_safe(db, N.staff(perm), "inventory.paid_without_inventory",
                            {"campaign_name": name, "campaign_id": str(h.campaign_id), "hold_id": str(h.id),
                             "order_id": str(h.order_id) if h.order_id else ""},
                            dedupe_key=f"inventory.pwi:{h.id}:{perm}")
    return HoldResult(h.id, "PAID_WITHOUT_INVENTORY", refund_required=True, screen_ids=screen_ids,
                      reason="NO_CAPACITY")


async def _after_status(db: AsyncSession, h: InventoryHold, screen_ids: Sequence[uuid.UUID], action: str,
                        extra: dict) -> None:
    h.updated_at = await db_now(db)
    if screen_ids:
        await db.execute(update(ScreenInventoryState).where(ScreenInventoryState.screen_id.in_(list(screen_ids)))
                         .values(inventory_version=ScreenInventoryState.inventory_version + 1,
                                 updated_at=func.now()))
    await audit(db, None, action, "inventory_hold", h.id, after={"status": h.status, **extra}, org_id=h.org_id,
                origin="system")
    N.defer_event(db, "inventory.updated", {"hold_id": str(h.id), "campaign_id": str(h.campaign_id),
                                            "status": h.status}, org_id=h.org_id)
    await db.flush()


# ─── Liberação ───────────────────────────────────────────────────────────────

async def release_hold(db: AsyncSession, hold_id: Any, reason: str) -> HoldResult:
    """HELD → CANCELLED; CONVERTED só com motivo de substituição/cancelamento/estorno."""
    if not reason:
        raise DomainError("REASON_REQUIRED", "Informe o motivo da liberação.", status=400)
    h = await _lock_hold(db, hold_id)
    screen_ids = await _hold_screen_ids(db, h.id)
    if h.status == "HELD":
        await lock_screens(db, screen_ids)
        await db.execute(update(BookingScreen).where(BookingScreen.hold_id == h.id, BookingScreen.status == "HELD")
                         .values(status="CANCELLED", status_reason=reason[:64], updated_at=func.now()))
        h.status, h.released_at, h.release_reason = "CANCELLED", await db_now(db), reason[:64]
        await _after_status(db, h, screen_ids, "inventory.hold_released", {"reason": reason})
        return HoldResult(h.id, "CANCELLED", screen_ids=screen_ids, reason=reason)
    if h.status == "CONVERTED":
        if reason not in CONVERTED_RELEASE_REASONS:
            raise Conflict("HOLD_CONVERTED", "Contratação paga só é liberada pelo fluxo de substituição de "
                           "revisão, cancelamento ou estorno.", details={"allowed": sorted(CONVERTED_RELEASE_REASONS)})
        await lock_screens(db, screen_ids)
        await db.execute(update(BookingScreen).where(
            BookingScreen.hold_id == h.id, BookingScreen.status.in_(("BOOKED", "SUSPENDED"))
        ).values(status="CANCELLED", status_reason=reason[:64], updated_at=func.now()))
        h.status, h.released_at, h.release_reason = "CANCELLED", await db_now(db), reason[:64]
        await _after_status(db, h, screen_ids, "inventory.booking_released", {"reason": reason})
        from app.modules.inventory.manifests import invalidate_screens

        await invalidate_screens(db, screen_ids, f"booking_released:{reason}"[:64])
        return HoldResult(h.id, "CANCELLED", screen_ids=screen_ids, reason=reason)
    return HoldResult(h.id, h.status, already_processed=True, screen_ids=screen_ids)


async def cancel_campaign_bookings(db: AsyncSession, campaign_id: Any, reason: str) -> list[HoldResult]:
    """Fluxo de cancelamento/estorno: libera todas as reservas/contratações da campanha."""
    ids = (await db.execute(select(InventoryHold.id).where(
        InventoryHold.campaign_id == _u(campaign_id), InventoryHold.status.in_(("HELD", "CONVERTED")))
        .order_by(InventoryHold.id))).scalars().all()
    return [await release_hold(db, i, reason if reason in CONVERTED_RELEASE_REASONS else "CAMPAIGN_CANCELLED")
            for i in ids]


# ─── Expiração (sweeper set-based + lazy) ────────────────────────────────────

SWEEP_SQL = text("""
WITH cand AS (
    SELECT id FROM inventory_holds
     WHERE status = 'HELD' AND hold_expires_at <= now()
     ORDER BY hold_expires_at
     LIMIT 1000
     FOR UPDATE SKIP LOCKED
), exp AS (
    UPDATE inventory_holds h
       SET status = 'EXPIRED', released_at = now(), release_reason = 'TTL', updated_at = now()
      FROM cand
     WHERE h.id = cand.id AND h.status = 'HELD'
    RETURNING h.id, h.campaign_id, h.org_id, h.order_id
), bs AS (
    UPDATE booking_screens b
       SET status = 'EXPIRED', status_reason = 'TTL', updated_at = now()
      FROM exp
     WHERE b.hold_id = exp.id AND b.status = 'HELD'
    RETURNING b.hold_id
)
SELECT exp.id, exp.campaign_id, exp.org_id, exp.order_id,
       (SELECT count(*) FROM bs WHERE bs.hold_id = exp.id) AS screens
  FROM exp
""")


async def sweep_expired_holds(db: AsyncSession) -> list[dict]:
    """Idempotente: segunda execução não encontra nada. Não bloqueia conversões (SKIP LOCKED)."""
    rows = (await db.execute(SWEEP_SQL)).all()
    out = []
    if not rows:
        return out
    camps = await S.load_campaigns(db, [r[1] for r in rows])
    for hid, cid, org_id, order_id, n in rows:
        out.append({"hold_id": str(hid), "campaign_id": str(cid), "order_id": str(order_id) if order_id else None,
                    "screens": int(n)})
        await audit(db, None, "inventory.hold_expired", "inventory_hold", hid,
                    after={"status": "EXPIRED", "reason": "TTL", "screens": int(n)}, org_id=org_id, origin="worker")
        N.defer_event(db, "inventory.updated", {"hold_id": str(hid), "campaign_id": str(cid), "status": "EXPIRED"},
                      org_id=org_id)
        if order_id is not None:
            try:
                async with db.begin_nested():
                    await S.campaign_hook("on_order_expired", db, order_id)
            except Exception as exc:  # não desfaz a expiração do inventário
                log.error("on_order_expired_failed", hold_id=str(hid), error_type=type(exc).__name__)
        else:
            c = camps.get(cid)
            await N.notify_safe(db, N.org(org_id), "inventory.hold_expired",
                                {"campaign_name": c.name if c else "", "campaign_id": str(cid), "hold_id": str(hid)},
                                dedupe_key=f"inventory.hold_expired:{hid}")
    return out


# ─── Mudança de política × contratado ────────────────────────────────────────

async def assert_policy_change_keeps_contracts(db: AsyncSession, *, scope: str, scope_id: Optional[uuid.UUID],
                                               new_spec: RotationSpec, effective_from) -> None:
    from app.models.establishments import Establishment
    from app.models.screens import Screen

    q = (select(BookingScreen.screen_id).join(BookingPattern, BookingPattern.id == BookingScreen.pattern_id)
         .where(or_(BookingScreen.status == "BOOKED",
                    and_(BookingScreen.status == "HELD", BookingScreen.hold_expires_at > func.now())),
                BookingPattern.date_to >= effective_from.date() - timedelta(days=1)).distinct())
    if scope == "SCREEN":
        q = q.where(BookingScreen.screen_id == scope_id)
    elif scope == "ESTABLISHMENT":
        q = q.where(BookingScreen.screen_id.in_(select(Screen.id).where(Screen.establishment_id == scope_id)))
    elif scope == "PARTNER_PLAN":
        q = q.where(BookingScreen.screen_id.in_(
            select(Screen.id).join(Establishment, Establishment.id == Screen.establishment_id)
            .where(Establishment.partner_plan_id == scope_id)))
    sids = sorted(set((await db.execute(q)).scalars().all()), key=str)
    if not sids:
        return
    from app.models.rotation import SCOPE_RANK

    screens = await S.load_screens(db, sids)
    resolver = await resolve_for_screens(db, screens.values())
    lines = await load_active_lines(db, sids, effective_from.date() - timedelta(days=1), date.max)
    camp_ids = {str(a.campaign_id) for v in lines.values() for a in v}
    competitors = await S.competitor_campaign_pairs(db, camp_ids) if len(camp_ids) >= 2 else frozenset()
    problems = []
    for sid in sids:
        info = screens.get(sid)
        if info is None or not lines.get(sid):
            continue
        ls = [a.line for a in lines[sid]]
        hi = max(ln.date_to for ln in ls)
        start = local_date(effective_from, info.timezone)
        days = []
        for d in daterange(start, hi):
            from app.core.timeutils import start_of_local_day_utc

            if start_of_local_day_utc(d, info.timezone) < effective_from:
                continue
            if not any(ln.active_on(d) for ln in ls):
                continue
            cur, cur_scope = resolver.for_day_scope(info, d)
            if cur_scope is not None and SCOPE_RANK[cur_scope] > SCOPE_RANK[scope]:
                continue  # escopo mais específico continua valendo
            days.append((d, new_spec))
        if not days:
            continue
        try:
            for ln in ls:
                validate_line(ln, new_spec)
            Packer(str(sid), info.timezone, competitors=competitors, strict=True).pack_days(days, ls)
        except PackingError as exc:
            problems.append({"screen_id": str(sid), "code": exc.code, "message": exc.message, **exc.details})
    if problems:
        raise Conflict("POLICY_REDUCES_CONTRACTED_CAPACITY",
                       "A nova política reduziria a capacidade abaixo do já contratado.",
                       details={"screens": problems[:100]})


# ─── Exibições contratadas (denominador E_i do W-FIN) ────────────────────────

async def contracted_exhibitions(db: AsyncSession, campaign_id: Any) -> dict[uuid.UUID, dict[date, int]]:
    """Inserções contratadas por tela e dia LOCAL (BOOKED), pela mesma expansão do empacotador."""
    cid = _u(campaign_id)
    rows = (await db.execute(
        select(BookingScreen.screen_id, BookingPattern).join(BookingPattern, BookingPattern.id == BookingScreen.pattern_id)
        .where(BookingScreen.campaign_id == cid, BookingScreen.status == "BOOKED")
    )).all()
    screens = await S.load_screens(db, {r[0] for r in rows})
    resolver = await resolve_for_screens(db, screens.values())
    out: dict[uuid.UUID, dict[date, int]] = {}
    for sid, bp in rows:
        info = screens.get(sid)
        if info is None:
            continue
        ln = line_from_row(bp)
        per = out.setdefault(sid, {})
        for d in daterange(bp.date_from, bp.date_to):
            if not ln.active_on(d):
                continue
            n = line_insertions_on_day(ln, day_frame(d, info.timezone, resolver.for_day(info, d)))
            if n:
                per[d] = per.get(d, 0) + n
    return out


# ─── Remoção definitiva de tela/estabelecimento (#1116) ─────────────────────

async def on_screens_removed(db: AsyncSession, screen_ids: Sequence[Any], reason: str) -> list[ScreenRetirement]:
    """Listener do W-ORG (mesma transação): tira do inventário, suspende contratações
    ativas e abre relatório de redistribuição para decisão do admin (sem automação)."""
    sids = sorted({_u(s) for s in screen_ids}, key=str)
    if not sids:
        return []
    screens = await S.load_screens(db, sids)
    await ensure_states(db, screens)
    await lock_screens(db, sids)
    now = await db_now(db)
    reports = []
    for sid in sids:
        info = screens.get(sid)
        today = local_date(now, info.timezone) if info else now.date()
        act = (await db.execute(
            select(BookingScreen, BookingPattern).join(BookingPattern, BookingPattern.id == BookingScreen.pattern_id)
            .where(BookingScreen.screen_id == sid, BookingScreen.status.in_(("HELD", "BOOKED")),
                   BookingPattern.date_to >= today)
        )).all()
        affected = [{"campaign_id": str(bs.campaign_id), "hold_id": str(bs.hold_id), "pattern_id": str(bp.id),
                     "status_before": bs.status, "date_from": bp.date_from.isoformat(),
                     "date_to": bp.date_to.isoformat(), "advertiser_org_id": str(bp.advertiser_org_id)}
                    for bs, bp in act]
        for bs, _ in act:
            bs.status, bs.status_reason = "SUSPENDED", "SCREEN_REMOVED"
        await db.execute(update(ScreenInventoryState).where(ScreenInventoryState.screen_id == sid).values(
            sellable=False, unsellable_reason=(reason or "removida")[:500],
            inventory_version=ScreenInventoryState.inventory_version + 1, updated_at=func.now()))
        rep = ScreenRetirement(id=uuid.uuid4(), screen_id=sid, establishment_id=info.establishment_id if info else None,
                               reason=reason or "Remoção definitiva", affected=affected, status="OPEN")
        db.add(rep)
        reports.append(rep)
        await audit(db, None, "inventory.screen_retired", "screen", sid,
                    after={"affected": len(affected), "report_id": str(rep.id)}, reason=reason,
                    org_id=info.org_id if info else None, origin="system")
        if affected:
            await N.notify_safe(db, N.staff("inventory.view"), "inventory.screen_retired",
                                {"screen_code": info.code if info else str(sid), "screen_id": str(sid),
                                 "affected": len(affected), "report_id": str(rep.id)},
                                dedupe_key=f"inventory.screen_retired:{rep.id}")
    await db.flush()
    from app.modules.inventory.manifests import invalidate_screens

    await invalidate_screens(db, sids, "screen_removed")
    return reports
