"""
RD Mídia Indoor — Comissão de estabelecimentos (ADR_009 item 3; A5 D2; P001 §55–§57; P002 itens 5, 30).

TERMOS POR PAGAMENTO (correção (h), AÇÃO 002): cada cobrança paga de uma campanha —
contratação inicial, RENOVAÇÃO ou ALTERAÇÃO paga — cria um termo (``commission_campaigns``,
chave (campanha, cobrança)) com o SEU pool ``P = floor(R_elegível_da_cobrança · pool_bps / 10000)``
(padrão 500 bps = 5 %) e o SEU período (renovação = período novo; alteração = do pagamento ao
fim). Pools nunca se sobrepõem sobre a mesma receita; o estorno de uma cobrança afeta só o seu termo.

Algoritmo (inteiros/frações exatas; µc = centavo × 10⁶):
- Orçamento do dia ``d`` no termo: ``b_d = P · E_d / E_total`` — ``E_d`` = exibições contratadas
  no dia (BOOKING do W-INV, snapshot do termo na 1ª apropriação), ``E_total = Σ E_d`` no período
  (Σ b_d ≤ P por construção; ``e_day`` limitado ao saldo do orçamento).
- Plano do dia (``commission_day_plans``): exibições por tela no dia, congelado na 1ª apropriação
  do dia a partir do BOOKING VIGENTE — troca de telas sem custo passa a valer nos dias seguintes.
- Peso da tela (congelado no termo na 1ª aparição): ``w = peso_estab × peso_tela / 1000``
  (correção (i)); exceção de taxa vira multiplicador: ``u = w × taxa_bps``.
  * Modelo A: ``ganho_sd = b_d · u_s · min(V_sd, E_sd) / Σ_s' u_s' · E_s'd``.
  * Modelo B: receita do dia ``r_d = R · E_d / E_total`` atribuída às telas do dia por
    ``A_s · E_sd`` (A = receita atribuída pelo W-CAMP; sem atribuição ⇒ igual);
    ``nominal_sd = r_sd · taxa_s / 10000``; escala ``min(1, b_d / Σ nominal_d)``;
    ``ganho_sd = nominal_sd · escala · min(V,E)/E``.
  * Estabelecimento sem comissão: ganho 0 (a parcela fica com a RD).
- Só PoP VÁLIDO (eventos ``EXTERNAL`` do W-PLAY), idempotente por ``playback_uuid``; 90/100 ⇒ 90 %.
  Piso ACUMULADO por (termo, tela): ``acumulado = floor(Σ_d ganho_sd)`` ⇒ execução integral exata.
- Fechamento mensal (último dia 23:59:59 America/Sao_Paulo): floor por estabelecimento de Σ µc;
  ledger D ``RD:Expense:Commission`` (pool liberado) / C ``EST:…:Pending`` / C
  ``RD:Income:RoundingResidue`` (resíduo da RD). Dia 1: Pending → Available; automático ≥ R$ 1.
- Ajuste manual: só por dupla aprovação (``COMMISSION_ADJUSTMENT``).
"""
from __future__ import annotations

import uuid
from collections import defaultdict
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from fractions import Fraction
from typing import Any, Awaitable, Callable, Iterable, Optional, Sequence

import structlog
from sqlalchemy import func, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import DomainError
from app.core.settings_service import get_setting
from app.core.timeutils import utcnow
from app.models.financial import (
    CommissionAccrual,
    CommissionAccrualEvent,
    CommissionCampaign,
    CommissionCampaignScreen,
    CommissionClosing,
    CommissionCompetenceRun,
    CommissionDayPlan,
    CommissionPoolRelease,
    EstablishmentCommissionSetting,
)
from app.modules.financial import common, ledger

log = structlog.get_logger("rd.financial.commission")

UC = 1_000_000  # micro-centavos por centavo
MODEL_A, MODEL_B = "A_POOL", "B_ATTRIBUTED"
EXTERNAL = "EXTERNAL"


class CommissionPlanUnavailable(DomainError):
    status, code, message = 503, "COMMISSION_PLAN_UNAVAILABLE", (
        "Plano de exibições contratadas indisponível (inventário); a apropriação será reprocessada.")


# ─── Algoritmo puro ──────────────────────────────────────────────────────────

@dataclass(frozen=True)
class DayScreen:
    screen_id: Any
    weight: int  # u = peso efetivo × taxa (modelo A)
    attributed: int  # peso de atribuição de receita (modelo B)
    rate_bps: int
    receives: bool
    expected: int  # E_sd
    executed: int  # V_sd (limitado a E_sd no cálculo)


def day_earnings(model: str, pool_day: Fraction, revenue_day: Fraction,
                 screens: Sequence[DayScreen]) -> dict[Any, Fraction]:
    """Ganho EXATO (µc, fração) de cada tela num dia. Σ ≤ ``pool_day`` sempre."""
    if model not in (MODEL_A, MODEL_B):
        raise ValueError("modelo de comissão inválido")
    out: dict[Any, Fraction] = {s.screen_id: Fraction(0) for s in screens}
    act = [s for s in screens if s.expected > 0]
    if pool_day <= 0 or not act:
        return out
    if model == MODEL_A:
        denom = sum(s.weight * s.expected for s in act)
        if denom <= 0:
            return out
        for s in act:
            if s.receives and s.executed > 0:
                out[s.screen_id] = pool_day * s.weight * min(s.executed, s.expected) / denom
        return out
    attr = {s.screen_id: (s.attributed if any(x.attributed > 0 for x in act) else 1) for s in act}
    denom = sum(attr[s.screen_id] * s.expected for s in act)
    if denom <= 0:
        return out
    nominal = {s.screen_id: (revenue_day * attr[s.screen_id] * s.expected / denom * s.rate_bps / 10_000
                             if s.receives else Fraction(0)) for s in act}
    total_n = sum(nominal.values(), Fraction(0))
    if total_n <= 0:
        return out
    scale = Fraction(1) if total_n <= pool_day else pool_day / total_n
    for s in act:
        if s.executed > 0:
            out[s.screen_id] = nominal[s.screen_id] * scale * min(s.executed, s.expected) / s.expected
    return out


@dataclass(frozen=True)
class ScreenTerms:
    """Termos de uma tela num cenário de UM dia (exemplos do painel e testes da tabela A5 D2.6)."""

    screen_id: Any
    weight_milli: int = 1000
    rate_bps: int = 500
    receives: bool = True
    attributed_cents: int = 0
    expected_total: int = 1


def compute_shares(model: str, pool_cents: int, pool_bps: int, terms: Sequence[ScreenTerms]) -> dict[Any, int]:
    """Participação de cada tela para execução INTEGRAL (µc, floor) — mesmo ``day_earnings`` da apropriação."""
    screens = [DayScreen(t.screen_id, t.weight_milli * t.rate_bps, t.attributed_cents, t.rate_bps, t.receives,
                         t.expected_total, t.expected_total) for t in terms]
    revenue = Fraction(sum(t.attributed_cents for t in terms) * UC)
    ex = day_earnings(model, Fraction(pool_cents * UC), revenue, screens)
    return {k: int(v) for k, v in ex.items()}


def accrued_for(share_ucents: int, executed: int, expected_total: int) -> int:
    """µc apropriados para ``executed`` = Σ min(V_d, E_d) (piso acumulado)."""
    if expected_total <= 0 or executed <= 0:
        return 0
    return (share_ucents * min(executed, expected_total)) // expected_total


def floor_cents(ucents: int) -> int:
    return ucents // UC


# ─── Configuração ────────────────────────────────────────────────────────────

async def current_terms(db: AsyncSession) -> tuple[int, str]:
    return int(await get_setting(db, "commission.pool_bps")), str(await get_setting(db, "commission.model"))


async def active_exception(db: AsyncSession, establishment_id: uuid.UUID, at: Optional[datetime] = None) \
        -> Optional[EstablishmentCommissionSetting]:
    at = at or utcnow()
    return (await db.execute(
        select(EstablishmentCommissionSetting)
        .where(EstablishmentCommissionSetting.establishment_id == establishment_id,
               EstablishmentCommissionSetting.valid_from <= at,
               (EstablishmentCommissionSetting.valid_to.is_(None)) | (EstablishmentCommissionSetting.valid_to > at))
        .order_by(EstablishmentCommissionSetting.valid_from.desc()).limit(1)
    )).scalar_one_or_none()


@dataclass(frozen=True)
class EffectiveTerms:
    receives: bool
    weight_milli: int
    rate_bps: int
    auto_payout: bool
    exception_id: Optional[uuid.UUID]


async def effective_terms(db: AsyncSession, establishment_id: uuid.UUID, pool_bps: int,
                          at: Optional[datetime] = None) -> EffectiveTerms:
    from app.models.establishments import Establishment

    est = await db.get(Establishment, establishment_id)
    base_receives = bool(getattr(est, "receives_commission", True)) if est is not None else False
    base_weight = int(getattr(est, "weight_milli", 1000) or 1000) if est is not None else 1000
    exc = await active_exception(db, establishment_id, at)
    receives = base_receives and (exc.receives_commission if exc is not None and exc.receives_commission is not None
                                  else True)
    weight = exc.weight_milli if exc is not None and exc.weight_milli is not None else base_weight
    rate = exc.rate_bps if exc is not None and exc.rate_bps is not None else pool_bps
    return EffectiveTerms(receives, int(weight), int(rate), bool(exc.auto_payout) if exc is not None else True,
                          exc.id if exc is not None else None)


# ─── Integração com outros domínios (registries com fallback) ────────────────

PlanProvider = Callable[[AsyncSession, uuid.UUID], Awaitable[dict]]
_plan_provider: Optional[PlanProvider] = None
_attribution_provider: Optional[Callable[[AsyncSession, uuid.UUID], Awaitable[dict]]] = None


def set_contracted_plan_provider(fn: Optional[PlanProvider]) -> None:
    global _plan_provider
    _plan_provider = fn


def set_attribution_provider(fn) -> None:
    global _attribution_provider
    _attribution_provider = fn


async def contracted_plan(db: AsyncSession, campaign_id: uuid.UUID) -> dict[uuid.UUID, dict[date, int]]:
    """E_i,d (W-INV ``contracted_exhibitions``): {screen_id: {dia_local: exibições}}."""
    if _plan_provider is not None:
        raw = await _plan_provider(db, campaign_id)
    else:
        try:
            from app.modules.inventory import service as inventory
            fn = inventory.contracted_exhibitions
        except (ImportError, AttributeError):
            raise CommissionPlanUnavailable() from None
        raw = await fn(db, campaign_id)
    out: dict[uuid.UUID, dict[date, int]] = {}
    for sid, days in (raw or {}).items():
        out[common.as_uuid(sid)] = {(d if isinstance(d, date) else date.fromisoformat(str(d))): int(n)
                                    for d, n in days.items() if int(n) > 0}
    return out


async def attributed_revenue(db: AsyncSession, campaign_id: uuid.UUID) -> Optional[dict]:
    if _attribution_provider is not None:
        return await _attribution_provider(db, campaign_id)
    try:
        from app.modules.campaigns import service as campaigns
        fn = campaigns.attributed_revenue_by_screen
    except (ImportError, AttributeError):
        return None
    return await fn(db, campaign_id)


async def screen_establishment(db: AsyncSession, screen_id: uuid.UUID, at: Optional[datetime] = None) -> Optional[uuid.UUID]:
    """Estabelecimento da tela na data (W-ORG ``screen_establishment_at``; fallback cadastro atual)."""
    try:
        from app.modules.screens import service as screens
        fn = screens.screen_establishment_at
    except (ImportError, AttributeError):
        fn = None
    if fn is not None:
        r = await fn(db, screen_id, at or utcnow())
        if r is not None:
            return common.as_uuid(getattr(r, "id", r))
    from app.models.screens import Screen

    s = await db.get(Screen, screen_id)
    return getattr(s, "establishment_id", None) if s is not None else None


# ─── Termos por pagamento ────────────────────────────────────────────────────

TERMS_KINDS = ("INITIAL", "RENEWAL", "CHANGE")


async def register_terms(db: AsyncSession, *, campaign_id: uuid.UUID, payment_order_id: uuid.UUID, org_id: uuid.UUID,
                         eligible_cents: int, start: date, end: date, kind: str, paid_on: date,
                         pool_bps: int, model: str) -> CommissionCampaign:
    """Cria (idempotente por (campanha, cobrança)) o termo de comissão de UMA cobrança paga."""
    if kind not in TERMS_KINDS:
        raise ValueError("tipo de termo inválido")
    if kind == "CHANGE":  # a diferença paga vale do pagamento em diante (não retroage a dias já executados)
        start = min(max(start, paid_on), end)
    tbl = CommissionCampaign.__table__
    await db.execute(pg_insert(tbl).values(
        id=uuid.uuid4(), campaign_id=campaign_id, payment_order_id=payment_order_id, org_id=org_id, kind=kind,
        model=model, pool_bps=pool_bps, eligible_revenue_cents=eligible_cents,
        pool_cents=(eligible_cents * pool_bps) // 10_000, start_date=start, end_date=end, status="ACTIVE",
        snapshot={"model": model, "pool_bps": pool_bps, "eligible_revenue_cents": eligible_cents, "kind": kind,
                  "payment_order_id": str(payment_order_id)},
    ).on_conflict_do_nothing(constraint="uq_commission_campaigns_campaign_payment"))
    return (await db.execute(select(CommissionCampaign).where(
        CommissionCampaign.campaign_id == campaign_id, CommissionCampaign.payment_order_id == payment_order_id)
    )).scalar_one()


async def terms_of(db: AsyncSession, campaign_id: uuid.UUID) -> list[CommissionCampaign]:
    return list((await db.execute(select(CommissionCampaign).where(CommissionCampaign.campaign_id == campaign_id)
                                  .order_by(CommissionCampaign.registered_at, CommissionCampaign.id))).scalars().all())


async def terms_for_payment(db: AsyncSession, campaign_id: Any, payment_order_id: Any) -> Optional[CommissionCampaign]:
    return (await db.execute(select(CommissionCampaign).where(
        CommissionCampaign.campaign_id == common.as_uuid(campaign_id),
        CommissionCampaign.payment_order_id == common.as_uuid(payment_order_id)))).scalar_one_or_none()


class _PlanCache:
    """Plano contratado vigente da campanha (uma consulta ao W-INV por lote)."""

    def __init__(self) -> None:
        self._plans: dict[uuid.UUID, dict] = {}
        self._attr: dict[uuid.UUID, Optional[dict]] = {}

    async def plan(self, db: AsyncSession, campaign_id: uuid.UUID) -> dict[uuid.UUID, dict[date, int]]:
        if campaign_id not in self._plans:
            p = await contracted_plan(db, campaign_id)
            if not p:
                raise CommissionPlanUnavailable(details={"campaign_id": str(campaign_id)})
            self._plans[campaign_id] = p
        return self._plans[campaign_id]

    async def attribution(self, db: AsyncSession, campaign_id: uuid.UUID) -> dict:
        if campaign_id not in self._attr:
            raw = await attributed_revenue(db, campaign_id)
            self._attr[campaign_id] = {common.as_uuid(k): int(v) for k, v in (raw or {}).items()}
        return self._attr[campaign_id] or {}


async def ensure_daily_plan(db: AsyncSession, t: CommissionCampaign, cache: _PlanCache) -> dict[date, int]:
    """Snapshot (uma vez) das exibições contratadas por DIA no período do termo (BOOKING do W-INV)."""
    if t.daily_plan is not None:
        return {date.fromisoformat(k): int(v) for k, v in t.daily_plan.items()}
    plan = await cache.plan(db, t.campaign_id)
    daily: dict[date, int] = defaultdict(int)
    for days in plan.values():
        for d, n in days.items():
            if t.start_date <= d <= t.end_date:
                daily[d] += int(n)
    t.daily_plan = {d.isoformat(): n for d, n in sorted(daily.items())}
    t.expected_total = sum(daily.values())
    if not t.expected_total:
        log.warning("commission_terms_without_plan", terms=str(t.id), campaign=str(t.campaign_id))
    await db.flush()
    return dict(daily)


async def ensure_terms_screen(db: AsyncSession, t: CommissionCampaign, screen_id: uuid.UUID,
                              cache: _PlanCache) -> CommissionCampaignScreen:
    row = await db.get(CommissionCampaignScreen, (t.id, screen_id))
    if row is not None:
        return row
    from app.models.establishments import Establishment
    from app.models.screens import Screen

    eid = await screen_establishment(db, screen_id)
    if eid is None:
        raise CommissionPlanUnavailable(details={"screen_id": str(screen_id), "reason": "tela sem estabelecimento"})
    eff = await effective_terms(db, eid, t.pool_bps)
    scr = await db.get(Screen, screen_id)
    screen_w = int(getattr(scr, "weight_milli", 1000) or 1000)
    est = await db.get(Establishment, eid)
    attributed = 0
    if t.model == MODEL_B:
        attributed = int((await cache.attribution(db, t.campaign_id)).get(screen_id, 0))
    row = CommissionCampaignScreen(
        terms_id=t.id, screen_id=screen_id, campaign_id=t.campaign_id, establishment_id=eid,
        establishment_org_id=getattr(est, "org_id", None), receives_commission=eff.receives,
        weight_milli=(eff.weight_milli * screen_w) // 1000, establishment_weight_milli=eff.weight_milli,
        screen_weight_milli=screen_w, rate_bps=eff.rate_bps, attributed_revenue_cents=attributed,
    )
    db.add(row)
    await db.flush()
    return row


async def ensure_day_plan(db: AsyncSession, t: CommissionCampaign, day: date,
                          cache: _PlanCache) -> CommissionDayPlan:
    row = await db.get(CommissionDayPlan, (t.id, day))
    if row is not None:
        return row
    daily = await ensure_daily_plan(db, t, cache)
    plan = await cache.plan(db, t.campaign_id)
    per = {str(sid): int(days.get(day, 0)) for sid, days in plan.items() if int(days.get(day, 0)) > 0}
    used = int((await db.execute(select(func.coalesce(func.sum(CommissionDayPlan.e_day), 0))
                                 .where(CommissionDayPlan.terms_id == t.id))).scalar_one())
    e_day = max(0, min(int(daily.get(day, 0)), int(t.expected_total or 0) - used))
    await db.execute(pg_insert(CommissionDayPlan.__table__).values(
        terms_id=t.id, local_day=day, plan=per, e_day=e_day).on_conflict_do_nothing())
    row = await db.get(CommissionDayPlan, (t.id, day))
    for sid in sorted(per):
        await ensure_terms_screen(db, t, uuid.UUID(sid), cache)
    return row


async def _terms_screen_target(db: AsyncSession, t: CommissionCampaign, screen_id: uuid.UUID) -> int:
    """µc devidos (piso acumulado) à tela no termo, sobre todos os dias com PoP apropriado."""
    if not t.expected_total:
        return 0
    days = (await db.execute(
        select(CommissionAccrual.local_day, func.sum(CommissionAccrual.valid_count))
        .where(CommissionAccrual.terms_id == t.id, CommissionAccrual.screen_id == screen_id)
        .group_by(CommissionAccrual.local_day)
    )).all()
    if not days:
        return 0
    screens = {r.screen_id: r for r in (await db.execute(
        select(CommissionCampaignScreen).where(CommissionCampaignScreen.terms_id == t.id))).scalars()}
    total = Fraction(0)
    for d, v in days:
        dp = await db.get(CommissionDayPlan, (t.id, d))
        if dp is None or not dp.e_day:
            continue
        pool_day = Fraction(t.pool_cents * UC * dp.e_day, t.expected_total)
        revenue_day = Fraction(t.eligible_revenue_cents * UC * dp.e_day, t.expected_total)
        ds = []
        for sid_s, e in sorted(dp.plan.items()):
            sid = uuid.UUID(sid_s)
            sc = screens.get(sid)
            if sc is None:
                continue
            ds.append(DayScreen(sid, sc.weight_milli * sc.rate_bps, sc.attributed_revenue_cents, sc.rate_bps,
                                sc.receives_commission, int(e), int(v) if sid == screen_id else 0))
        total += day_earnings(t.model, pool_day, revenue_day, ds).get(screen_id, Fraction(0))
    return int(total)  # floor (positivo)


# ─── Apropriação diária (chamada pelo W-PLAY) ────────────────────────────────

@dataclass(frozen=True)
class _Ev:
    playback_uuid: uuid.UUID
    campaign_id: uuid.UUID
    screen_id: uuid.UUID
    local_day: date


def _get(e: Any, *names: str) -> Any:
    for n in names:
        v = e.get(n) if isinstance(e, dict) else getattr(e, n, None)
        if v is not None:
            return v
    return None


def _normalize(events: Iterable[Any]) -> list[_Ev]:
    out = []
    seen = set()
    for e in events:
        kind = _get(e, "kind")
        if kind is not None and str(getattr(kind, "value", kind)).upper() != EXTERNAL:
            continue
        valid = _get(e, "is_valid", "valid")
        if valid is False:
            continue
        cid, sid, pid = _get(e, "campaign_id"), _get(e, "screen_id"), _get(e, "playback_uuid", "event_id", "id")
        day = _get(e, "local_day")
        if cid is None or sid is None or pid is None or day is None:
            continue
        if isinstance(day, datetime):
            raise ValueError("local_day deve ser date")
        if not isinstance(day, date):
            day = date.fromisoformat(str(day))
        p = common.as_uuid(pid)
        if p in seen:
            continue
        seen.add(p)
        out.append(_Ev(p, common.as_uuid(cid), common.as_uuid(sid), day))
    return out


async def competence_closed(db: AsyncSession, competence: date) -> bool:
    return (await db.get(CommissionCompetenceRun, competence)) is not None


async def open_competence_for(db: AsyncSession, day: date) -> date:
    comp = ledger.month_start(day)
    while await competence_closed(db, comp):
        comp = ledger.month_start(ledger.month_end(comp) + timedelta(days=1))
    return comp


def _covers(t: CommissionCampaign, day: date) -> bool:
    if not (t.start_date <= day <= t.end_date):
        return False
    return t.status == "ACTIVE" or (t.cancelled_on is not None and day <= t.cancelled_on)


async def accrue_commission_for_events(db: AsyncSession, events: Iterable[Any]) -> dict:
    """Apropria comissão para PoP VÁLIDOS (idempotente por ``playback_uuid``; sem commit próprio).

    Cada evento conta para TODO termo (cobrança paga) cujo período cobre o dia e cujo plano do
    dia contém a tela. ``CommissionPlanUnavailable`` (503) ⇒ o W-PLAY mantém o lote pendente.
    """
    evs = _normalize(events)
    stats = {"received": len(evs), "new": 0, "duplicates": 0, "counted": 0, "ignored": 0, "accrued_ucents": 0}
    by_campaign: dict[uuid.UUID, list[_Ev]] = defaultdict(list)
    for e in evs:
        by_campaign[e.campaign_id].append(e)
    cache = _PlanCache()
    for cid in sorted(by_campaign, key=str):
        group = by_campaign[cid]
        await ledger.advisory_lock(db, "commission_campaign", cid)
        terms = await terms_of(db, cid)
        counts: dict[tuple[uuid.UUID, uuid.UUID, date], int] = defaultdict(int)
        day_plans: dict[tuple[uuid.UUID, date], CommissionDayPlan] = {}
        for e in group:
            matched = []
            for t in terms:
                if not _covers(t, e.local_day):
                    continue
                key = (t.id, e.local_day)
                if key not in day_plans:
                    day_plans[key] = await ensure_day_plan(db, t, e.local_day, cache)
                if str(e.screen_id) in (day_plans[key].plan or {}):
                    matched.append(t)
            ins = (pg_insert(CommissionAccrualEvent.__table__)
                   .values(playback_uuid=e.playback_uuid, campaign_id=cid, screen_id=e.screen_id,
                           local_day=e.local_day, counted=bool(matched))
                   .on_conflict_do_nothing(index_elements=["playback_uuid"])
                   .returning(CommissionAccrualEvent.__table__.c.playback_uuid))
            if (await db.execute(ins)).first() is None:
                stats["duplicates"] += 1
                continue
            stats["new"] += 1
            if matched:
                for t in matched:
                    counts[(t.id, e.screen_id, e.local_day)] += 1
                stats["counted"] += 1
            else:
                stats["ignored"] += 1
        by_id = {t.id: t for t in terms}
        for (tid, sid, day), n in sorted(counts.items(), key=lambda kv: (str(kv[0][0]), str(kv[0][1]), kv[0][2])):
            stats["accrued_ucents"] += await _apply(db, by_id[tid], sid, day, n, day_plans[(tid, day)])
    return stats


async def _apply(db: AsyncSession, t: CommissionCampaign, screen_id: uuid.UUID, day: date, n: int,
                 dp: CommissionDayPlan) -> int:
    scr = await db.get(CommissionCampaignScreen, (t.id, screen_id))
    comp = await open_competence_for(db, day)
    row = (await db.execute(
        select(CommissionAccrual).where(CommissionAccrual.terms_id == t.id, CommissionAccrual.screen_id == screen_id,
                                        CommissionAccrual.local_day == day, CommissionAccrual.competence == comp)
        .with_for_update()
    )).scalar_one_or_none()
    if row is None:
        row = CommissionAccrual(id=uuid.uuid4(), terms_id=t.id, campaign_id=t.campaign_id, screen_id=screen_id,
                                establishment_id=scr.establishment_id, local_day=day, competence=comp, valid_count=0,
                                expected_count=int((dp.plan or {}).get(str(screen_id), 0)), amount_ucents=0,
                                reversed_ucents=0)
        db.add(row)
    row.valid_count += n
    await db.flush()
    already = int((await db.execute(
        select(func.coalesce(func.sum(CommissionAccrual.amount_ucents), 0))
        .where(CommissionAccrual.terms_id == t.id, CommissionAccrual.screen_id == screen_id))).scalar_one())
    delta = await _terms_screen_target(db, t, screen_id) - already
    if delta > 0:
        row.amount_ucents += delta
        await db.flush()
        return delta
    return 0


# ─── Fechamento mensal ───────────────────────────────────────────────────────

async def _pool_target(db: AsyncSession, t: CommissionCampaign, m_end: date) -> int:
    """Pool do termo liberado até o fim da competência (pelo orçamento diário contratado)."""
    end = t.end_date
    if t.status == "CANCELLED" and t.cancelled_on is not None:
        end = min(end, t.cancelled_on)
    if end <= m_end:
        return t.pool_cents
    if t.daily_plan and t.expected_total:
        through = sum(int(v) for k, v in t.daily_plan.items() if date.fromisoformat(k) <= m_end)
        return (t.pool_cents * min(through, t.expected_total)) // t.expected_total
    total = (t.end_date - t.start_date).days + 1
    elapsed = min(max((m_end - t.start_date).days + 1, 0), total)
    return (t.pool_cents * elapsed) // total


async def close_competence(db: AsyncSession, competence: date, *, now: Optional[datetime] = None,
                           created_by: Any = None) -> Optional[CommissionCompetenceRun]:
    """Fecha a competência (só depois de 23:59:59 do último dia, America/Sao_Paulo). Idempotente."""
    competence = ledger.month_start(competence)
    m_end = ledger.month_end(competence)
    today = ledger.local_date(now or utcnow(), ledger.FINANCIAL_TZ)
    if today <= m_end:
        raise DomainError("COMMISSION_COMPETENCE_OPEN",
                          "A competência só fecha após 23:59:59 do último dia (America/Sao_Paulo).", status=409,
                          details={"competence": competence.isoformat()})
    await ledger.advisory_lock(db, "commission_close", "global")
    if await competence_closed(db, competence):
        return await db.get(CommissionCompetenceRun, competence)
    now_ts = utcnow()
    rows = (await db.execute(
        select(CommissionAccrual.establishment_id,
               func.sum(CommissionAccrual.amount_ucents - CommissionAccrual.reversed_ucents))
        .where(CommissionAccrual.competence == competence).group_by(CommissionAccrual.establishment_id)
    )).all()
    closings = []
    for eid, u in rows:
        u = int(u or 0)
        if u < 0:
            u = 0
        closings.append((eid, u, u // UC, u % UC))
    commission_cents = sum(c for _, _, c, _ in closings)
    # pool liberado na competência (piso acumulado por TERMO = por cobrança paga)
    released_total = 0
    releases = []
    for t in (await db.execute(select(CommissionCampaign))).scalars().all():
        before = int((await db.execute(
            select(func.coalesce(func.sum(CommissionPoolRelease.released_cents), 0))
            .where(CommissionPoolRelease.terms_id == t.id, CommissionPoolRelease.competence < competence)
        )).scalar_one())
        x = await _pool_target(db, t, m_end) - before
        if x != 0:
            releases.append((t.id, x))
            released_total += x
    prev = (await db.execute(
        select(CommissionCompetenceRun).where(CommissionCompetenceRun.competence < competence)
        .order_by(CommissionCompetenceRun.competence.desc()).limit(1)
    )).scalar_one_or_none()
    carry_prev = prev.carry_cents if prev is not None else 0
    residue = released_total + carry_prev - commission_cents
    residue_posted, carry = (residue, 0) if residue >= 0 else (0, residue)
    tx_id = None
    if commission_cents or residue_posted:
        exp = await ledger.account(db, "RD:Expense:Commission")
        legs: list = [(exp, commission_cents + residue_posted, "pool de comissão liberado")]
        for eid, _u, c, _r in closings:
            if c > 0:
                acc = await _est_account(db, "EST:CommissionPayable:Pending", eid)
                legs.append((acc, -c, "comissão da competência"))
        if residue_posted:
            res_acc = await ledger.account(db, "RD:Income:RoundingResidue")
            legs.append((res_acc, -residue_posted, "resíduo do pool (não executado + arredondamento)"))
        r = await ledger.post(
            db, kind="COMMISSION_CLOSE", idempotency_key=f"commission:close:{competence.isoformat()}", legs=legs,
            description=f"Fechamento de comissões — competência {competence:%m/%Y}", competence_date=m_end,
            created_by=created_by, ref_type="commission_competence", ref_id=competence.isoformat(),
            meta={"pool_released_cents": released_total, "commission_cents": commission_cents,
                  "residue_cents": residue_posted, "carry_cents": carry},
        )
        tx_id = r.transaction_id
    run = CommissionCompetenceRun(
        competence=competence, status="CLOSED", pool_released_cents=released_total, commission_cents=commission_cents,
        residue_cents=residue_posted, carry_cents=carry, rounding_residue_ucents=sum(r for *_, r in closings),
        close_transaction_id=tx_id,
    )
    db.add(run)
    await db.flush()
    for eid, u, c, r in closings:
        db.add(CommissionClosing(competence=competence, establishment_id=eid, accrued_ucents=u, amount_cents=c,
                                 residue_ucents=r))
    for cid, x in releases:
        db.add(CommissionPoolRelease(terms_id=cid, competence=competence, released_cents=x))
    await db.execute(update(CommissionAccrual).where(CommissionAccrual.competence == competence)
                     .values(closed_at=now_ts))
    await db.flush()
    from app.core.audit import audit

    await audit(db, None, "commission.competence_closed", "commission_competence", competence.isoformat(),
                origin="worker", after={"commission_cents": commission_cents, "pool_released_cents": released_total,
                                        "residue_cents": residue_posted, "carry_cents": carry,
                                        "establishments": len(closings)})
    return run


async def _est_account(db: AsyncSession, code: str, establishment_id: uuid.UUID):
    from app.models.establishments import Establishment

    est = await db.get(Establishment, establishment_id)
    return await ledger.account(db, code, owner_id=establishment_id, org_id=getattr(est, "org_id", None))


async def release_competence(db: AsyncSession, competence: date, *, created_by: Any = None) -> dict:
    """Dia 1: Pending → Available (compensa saldo negativo). Idempotente."""
    competence = ledger.month_start(competence)
    run = (await db.execute(select(CommissionCompetenceRun).where(CommissionCompetenceRun.competence == competence)
                            .with_for_update())).scalar_one_or_none()
    if run is None:
        raise DomainError("COMMISSION_COMPETENCE_NOT_CLOSED", "Competência ainda não fechada.", status=409)
    if run.status == "RELEASED":
        return {"competence": competence.isoformat(), "released": 0, "already": True}
    est_ids = [r[0] for r in (await db.execute(
        select(CommissionClosing.establishment_id).where(CommissionClosing.competence == competence)
    )).all()]
    legs: list = []
    released = 0
    made_available: list[uuid.UUID] = []
    for eid in sorted(set(est_ids), key=str):
        pend = await ledger.balance_of(db, "EST:CommissionPayable:Pending", eid)
        if pend <= 0:
            continue
        recv = max(await ledger.balance_of(db, "EST:Receivable", eid), 0)
        offset = min(pend, recv)
        legs.append((await _est_account(db, "EST:CommissionPayable:Pending", eid), pend, "liberação"))
        if offset:
            legs.append((await _est_account(db, "EST:Receivable", eid), -offset, "compensa saldo negativo"))
        if pend - offset:
            legs.append((await _est_account(db, "EST:CommissionPayable:Available", eid), -(pend - offset), "disponível"))
            made_available.append(eid)
        released += pend
    tx_id = None
    if legs:
        r = await ledger.post(db, kind="COMMISSION_RELEASE", idempotency_key=f"commission:release:{competence.isoformat()}",
                              legs=legs, description=f"Liberação de comissões — competência {competence:%m/%Y}",
                              created_by=created_by, ref_type="commission_competence", ref_id=competence.isoformat())
        tx_id = r.transaction_id
    run.status = "RELEASED"
    run.released_at = utcnow()
    run.release_transaction_id = tx_id
    await db.flush()
    for eid in made_available:
        await _notify_balance_available(db, eid, competence)
    return {"competence": competence.isoformat(), "released": released, "already": False}


async def _notify_balance_available(db: AsyncSession, establishment_id: uuid.UUID, competence: date) -> None:
    """``balance.available`` (catálogo W-BIZ) ao dono do estabelecimento — só o valor disponível,
    nenhum dado comercial do anunciante; idempotente por (competência, estabelecimento)."""
    from app.models.establishments import Establishment
    from app.modules.notifications.service import EstablishmentRecipients

    available = await ledger.balance_of(db, "EST:CommissionPayable:Available", establishment_id)
    if available <= 0:
        return
    est = await db.get(Establishment, establishment_id)
    await common.notify(db, [EstablishmentRecipients(establishment_id, "withdrawals.view")], "balance.available",
                        {"amount": common.brl(available)},
                        dedupe_key=f"balance.available:{competence.isoformat()}:{establishment_id}",
                        org_id=getattr(est, "org_id", None), entity=("establishment", establishment_id))


async def _notify_negative_balance(db: AsyncSession, establishment_id: uuid.UUID, event_key: str) -> None:
    """Saldo negativo (comissão já sacada estornada) ⇒ saques bloqueados. Idempotente por evento de estorno."""
    from app.models.establishments import Establishment
    from app.modules.notifications.service import EstablishmentRecipients

    negative = await ledger.balance_of(db, "EST:Receivable", establishment_id)
    if negative <= 0:
        return
    est = await db.get(Establishment, establishment_id)
    await common.notify(db, [EstablishmentRecipients(establishment_id, "withdrawals.view")], common.EVENT_BALANCE_NEGATIVE,
                        {"amount": common.brl(negative)},
                        dedupe_key=f"{common.EVENT_BALANCE_NEGATIVE}:{event_key}:{establishment_id}"[:255],
                        org_id=getattr(est, "org_id", None), entity=("establishment", establishment_id))


async def due_competences(db: AsyncSession, *, now: Optional[datetime] = None) -> list[date]:
    """Competências encerradas (até o mês anterior, SP) ainda sem fechamento, em ordem."""
    today = ledger.local_date(now or utcnow(), ledger.FINANCIAL_TZ)
    last = ledger.month_start(today) - timedelta(days=1)
    first_acc = (await db.execute(select(func.min(CommissionAccrual.competence)))).scalar_one_or_none()
    first_reg = (await db.execute(select(func.min(CommissionCampaign.registered_at)))).scalar_one_or_none()
    candidates = [d for d in (first_acc, ledger.local_date(first_reg, ledger.FINANCIAL_TZ) if first_reg else None) if d]
    if not candidates:
        return []
    comp = ledger.month_start(min(candidates))
    out = []
    while comp <= last:
        if not await competence_closed(db, comp):
            out.append(comp)
        comp = ledger.month_start(ledger.month_end(comp) + timedelta(days=1))
    return out


# ─── Estorno de comissão (cancelamento/reembolso) — POR TERMO (cobrança) ─────

async def clawback_terms(
    db: AsyncSession,
    terms: CommissionCampaign,
    *,
    fraction: Fraction,
    key: str,
    reason: str,
    cancel_from: Optional[date] = None,
    approval_request_id: Any = None,
    created_by: Any = None,
) -> dict:
    """Reverte ``fraction`` da comissão de UM termo (só a parcela da cobrança estornada).

    - Apropriações em competência ABERTA: revertidas no cálculo provisório ("pendente revertida").
    - Já fechadas: D Pending → D Available → D ``EST:Receivable`` (comissão já sacada ⇒
      saldo negativo ⇒ saque bloqueado) / C ``RD:Expense:Commission``.
    """
    if not (0 <= fraction <= 1):
        raise ValueError("fração inválida")
    await ledger.advisory_lock(db, "commission_campaign", terms.campaign_id)
    if cancel_from is not None:
        terms.status = "CANCELLED"
        terms.cancelled_on = cancel_from
    if fraction == 0:
        await db.flush()
        return {"open_reverted_ucents": 0, "closed_clawback_cents": 0, "establishments": {}}
    rows = (await db.execute(
        select(CommissionAccrual).where(CommissionAccrual.terms_id == terms.id).with_for_update()
    )).scalars().all()
    open_rev = 0
    closed_by_est: dict[uuid.UUID, int] = defaultdict(int)
    for r in rows:
        net = r.amount_ucents - r.reversed_ucents
        rev = int(net * fraction)
        if rev <= 0:
            continue
        r.reversed_ucents += rev
        if r.closed_at is None:
            open_rev += rev
        else:
            closed_by_est[r.establishment_id] += rev
    legs: list = []
    total = 0
    details = {}
    for eid in sorted(closed_by_est, key=str):
        cents = closed_by_est[eid] // UC
        if cents <= 0:
            continue
        remaining = cents
        pend = max(await ledger.balance_of(db, "EST:CommissionPayable:Pending", eid), 0)
        avail = max(await ledger.balance_of(db, "EST:CommissionPayable:Available", eid), 0)
        take_p = min(pend, remaining)
        remaining -= take_p
        take_a = min(avail, remaining)
        remaining -= take_a
        if take_p:
            legs.append((await _est_account(db, "EST:CommissionPayable:Pending", eid), take_p, "estorno de comissão"))
        if take_a:
            legs.append((await _est_account(db, "EST:CommissionPayable:Available", eid), take_a, "estorno de comissão"))
        if remaining:
            legs.append((await _est_account(db, "EST:Receivable", eid), remaining,
                         "comissão já sacada: saldo negativo (bloqueio de saque)"))
        total += cents
        details[str(eid)] = {"clawback_cents": cents, "from_pending": take_p, "from_available": take_a,
                             "negative_balance": remaining}
    if total:
        legs.append((await ledger.account(db, "RD:Expense:Commission"), -total, "estorno de comissão"))
        res = await ledger.post(db, kind="COMMISSION_CLAWBACK", idempotency_key=f"commission:clawback:{terms.id}:{key}",
                          legs=legs, description=f"Estorno de comissão — campanha {terms.campaign_id}: {reason}"[:500],
                          approval_request_id=approval_request_id, created_by=created_by,
                          ref_type="campaign", ref_id=terms.campaign_id,
                          meta={"fraction": str(fraction), "terms_id": str(terms.id),
                                "payment_order_id": str(terms.payment_order_id), "detail": details})
        if getattr(res, "created", True):
            for eid_s, det in details.items():
                if det["negative_balance"] > 0:
                    await _notify_negative_balance(db, uuid.UUID(eid_s), f"{terms.id}:{key}")
    await db.flush()
    return {"open_reverted_ucents": open_rev, "closed_clawback_cents": total, "establishments": details}


# ─── Consultas (painel do estabelecimento / relatórios) ──────────────────────

async def establishment_balance(db: AsyncSession, establishment_id: uuid.UUID) -> dict:
    from app.models.financial import Withdrawal

    provisional = int((await db.execute(
        select(func.coalesce(func.sum(CommissionAccrual.amount_ucents - CommissionAccrual.reversed_ucents), 0))
        .where(CommissionAccrual.establishment_id == establishment_id, CommissionAccrual.closed_at.is_(None))
    )).scalar_one())
    paid = int((await db.execute(
        select(func.coalesce(func.sum(Withdrawal.amount_cents), 0))
        .where(Withdrawal.establishment_id == establishment_id, Withdrawal.status == "COMPLETED")
    )).scalar_one())
    closed_total = int((await db.execute(
        select(func.coalesce(func.sum(CommissionClosing.amount_cents), 0))
        .where(CommissionClosing.establishment_id == establishment_id)
    )).scalar_one())
    pending_closed = await ledger.balance_of(db, "EST:CommissionPayable:Pending", establishment_id)
    available = await ledger.balance_of(db, "EST:CommissionPayable:Available", establishment_id)
    in_transit = await ledger.balance_of(db, "EST:CommissionPayable:InTransit", establishment_id)
    receivable = await ledger.balance_of(db, "EST:Receivable", establishment_id)
    deposit = await ledger.balance_of(db, "EST:DepositHeld", establishment_id)
    return {
        "establishment_id": str(establishment_id),
        "pending_provisional_cents": provisional // UC,
        "pending_provisional_ucents": provisional,
        "pending_closed_cents": pending_closed,
        "available_cents": available,
        "in_transit_cents": in_transit,
        "negative_balance_cents": max(receivable, 0),
        "withdrawals_blocked": receivable > 0,
        "paid_cents": paid,
        "accumulated_cents": closed_total + provisional // UC,
        "deposit_held_cents": deposit,
    }


async def accrual_report(db: AsyncSession, *, establishment_ids: Optional[set] = None, campaign_id=None,
                         screen_id=None, start: Optional[date] = None, end: Optional[date] = None,
                         group_by: str = "day") -> list[dict]:
    """Valor/reproduções por anúncio(campanha)/tela/dia/mês — sem dados comerciais do anunciante."""
    keys = {"campaign": [CommissionAccrual.campaign_id], "screen": [CommissionAccrual.screen_id],
            "day": [CommissionAccrual.local_day], "month": [CommissionAccrual.competence],
            "campaign_screen_day": [CommissionAccrual.campaign_id, CommissionAccrual.screen_id, CommissionAccrual.local_day]}
    cols = keys.get(group_by)
    if cols is None:
        raise DomainError("INVALID_GROUP_BY", "Agrupamento inválido.", details={"allowed": list(keys)})
    q = select(*cols, CommissionAccrual.establishment_id, func.sum(CommissionAccrual.valid_count),
               func.sum(CommissionAccrual.expected_count),
               func.sum(CommissionAccrual.amount_ucents - CommissionAccrual.reversed_ucents),
               func.bool_and(CommissionAccrual.closed_at.isnot(None)))
    if establishment_ids is not None:
        q = q.where(CommissionAccrual.establishment_id.in_(list(establishment_ids)))
    if campaign_id is not None:
        q = q.where(CommissionAccrual.campaign_id == campaign_id)
    if screen_id is not None:
        q = q.where(CommissionAccrual.screen_id == screen_id)
    if start is not None:
        q = q.where(CommissionAccrual.local_day >= start)
    if end is not None:
        q = q.where(CommissionAccrual.local_day <= end)
    q = q.group_by(*cols, CommissionAccrual.establishment_id).order_by(*cols)
    out = []
    for row in (await db.execute(q)).all():
        *k, eid, plays, expected, ucents, closed = row
        item = {name: (str(v) if isinstance(v, uuid.UUID) else (v.isoformat() if isinstance(v, date) else v))
                for name, v in zip([c.key for c in cols], k)}
        item.update({"establishment_id": str(eid), "valid_plays": int(plays or 0), "expected_plays": int(expected or 0),
                     "amount_ucents": int(ucents or 0), "amount_cents": int(ucents or 0) // UC,
                     "status": "FECHADO" if closed else "PENDENTE"})
        out.append(item)
    return out


async def campaign_terms_report(db: AsyncSession, campaign_id: uuid.UUID, *,
                                establishment_ids: Optional[set] = None) -> Optional[dict]:
    """Termos de comissão POR PAGAMENTO de uma campanha (painel financeiro) — consultas agregadas fixas.

    ``establishment_ids=None`` (equipe RD com escopo GLOBAL): visão COMPLETA (cobrança, receita
    elegível, pool, liberado, apropriado e estornado de cada termo). Com um conjunto de
    estabelecimentos (perfil do estabelecimento): só os termos em que ALGUMA tela dele participa e
    só a comissão DELE — sem cobrança, receita, pool nem liberação (dados comerciais do anunciante;
    matriz C-16 / P001 §101). ``None`` se não houver nada visível (a rota responde 404).
    """
    terms = (await db.execute(select(CommissionCampaign).where(CommissionCampaign.campaign_id == campaign_id)
                              .order_by(CommissionCampaign.start_date, CommissionCampaign.registered_at,
                                        CommissionCampaign.id))).scalars().all()
    if not terms:
        return None
    full = establishment_ids is None
    if not full:
        visible = {r[0] for r in (await db.execute(
            select(CommissionCampaignScreen.terms_id).where(
                CommissionCampaignScreen.campaign_id == campaign_id,
                CommissionCampaignScreen.establishment_id.in_(list(establishment_ids or ())))
            .distinct())).all()}
        terms = [t for t in terms if t.id in visible]
        if not terms:
            return None
    ids = [t.id for t in terms]
    acc_q = (select(CommissionAccrual.terms_id, func.coalesce(func.sum(CommissionAccrual.amount_ucents), 0),
                    func.coalesce(func.sum(CommissionAccrual.reversed_ucents), 0),
                    func.coalesce(func.sum(CommissionAccrual.valid_count), 0))
             .where(CommissionAccrual.terms_id.in_(ids)).group_by(CommissionAccrual.terms_id))
    if not full:
        acc_q = acc_q.where(CommissionAccrual.establishment_id.in_(list(establishment_ids)))
    acc = {r[0]: (int(r[1]), int(r[2]), int(r[3])) for r in (await db.execute(acc_q)).all()}
    released = {}
    if full:
        released = {r[0]: int(r[1]) for r in (await db.execute(
            select(CommissionPoolRelease.terms_id, func.sum(CommissionPoolRelease.released_cents))
            .where(CommissionPoolRelease.terms_id.in_(ids)).group_by(CommissionPoolRelease.terms_id))).all()}
    items = []
    for t in terms:
        gross, rev, plays = acc.get(t.id, (0, 0, 0))
        item = {"terms_id": str(t.id), "kind": t.kind, "period_start": t.start_date.isoformat(),
                "period_end": t.end_date.isoformat(), "status": t.status,
                "cancelled_on": t.cancelled_on.isoformat() if t.cancelled_on else None,
                "valid_plays": plays, "commission_cents": (gross - rev) // UC, "commission_ucents": gross - rev,
                "clawback_cents": rev // UC}
        if full:
            item.update({"payment_order_id": str(t.payment_order_id), "model": t.model, "pool_bps": t.pool_bps,
                         "eligible_revenue_cents": t.eligible_revenue_cents, "pool_cents": t.pool_cents,
                         "released_cents": released.get(t.id, 0), "expected_exhibitions": t.expected_total,
                         "registered_at": t.registered_at.isoformat() if t.registered_at else None})
        items.append(item)
    totals = {k: sum(i[k] for i in items) for k in ("commission_cents", "clawback_cents", "valid_plays")}
    if full:
        totals.update({k: sum(i[k] for i in items) for k in ("eligible_revenue_cents", "pool_cents", "released_cents")})
    return {"campaign_id": str(campaign_id), "visibility": "FULL" if full else "ESTABLISHMENT",
            "terms": items, "totals": totals}


# ─── Explicação dos modelos (painel; P001 §55) ───────────────────────────────

def _example(model: str, revenue_cents: int, pool_bps: int, terms: list[ScreenTerms], executed: dict) -> dict:
    pool = revenue_cents * pool_bps // 10_000
    shares = compute_shares(model, pool, pool_bps, terms)
    per = []
    for t in terms:
        u = accrued_for(shares[t.screen_id], executed.get(t.screen_id, t.expected_total), t.expected_total)
        per.append(u // UC)
    paid = sum(per)
    return {"pool_cents": pool, "per_screen_cents": per, "total_cents": paid, "rd_residue_cents": pool - paid}


def models_explanation(pool_bps: int = 500, current_model: Optional[str] = None) -> dict:
    R = 5_000

    def eq(n: int, **kw) -> list[ScreenTerms]:
        return [ScreenTerms(f"t{i}", rate_bps=pool_bps, expected_total=100, **kw) for i in range(n)]

    mixed = ([ScreenTerms(f"p{i}", rate_bps=700, expected_total=100) for i in range(2)]
             + [ScreenTerms("m0", rate_bps=300, expected_total=100)]
             + [ScreenTerms(f"s{i}", rate_bps=pool_bps, expected_total=100) for i in range(7)])
    venues = ([ScreenTerms(f"shop{i}", rate_bps=pool_bps, attributed_cents=1000, expected_total=100) for i in range(2)]
              + [ScreenTerms(f"gym{i}", rate_bps=pool_bps, attributed_cents=375, expected_total=100) for i in range(8)])
    examples = []
    for label, terms, executed in [
        ("1 tela, 100/100 exibições", eq(1), {}),
        ("1 tela, 90/100 exibições", eq(1), {"t0": 90}),
        ("7 telas, peso 1,0", eq(7), {}),
        ("10 telas, peso 1,0", eq(10), {}),
        ("10 telas, uma com 90/100", eq(10), {"t0": 90}),
        ("100 telas, peso 1,0", eq(100), {}),
        ("10 telas: 2 com exceção 7 %, 1 com 3 %, 7 com 5 %", mixed, {}),
        ("2 telas de shopping (R$ 10 cada) + 8 academias (R$ 3,75 cada)", venues, {}),
    ]:
        b_terms = [ScreenTerms(t.screen_id, t.weight_milli, t.rate_bps, t.receives,
                               t.attributed_cents or R // len(terms), t.expected_total) for t in terms]
        examples.append({
            "scenario": label,
            "model_a": _example(MODEL_A, R, pool_bps, terms, executed),
            "model_b": _example(MODEL_B, R, pool_bps, b_terms, executed),
        })
    return {
        "current_model": current_model,
        "pool_bps": pool_bps,
        "base_revenue_cents": R,
        "models": {
            MODEL_A: {
                "name": "Modelo A — pool fixo",
                "description": ("O pool (padrão 5 % da receita elegível de mídia) é dividido entre TODAS as telas "
                                "contratadas na proporção do peso de cada uma; exceções de taxa (ex.: 7 % ou 3 %) "
                                "viram multiplicador de peso. Cada tela recebe sua parte proporcional às exibições "
                                "comprovadas (90 de 100 ⇒ 90 %). O que não é executado e os centavos do "
                                "arredondamento ficam com a RD. A soma nunca ultrapassa o pool."),
            },
            MODEL_B: {
                "name": "Modelo B — receita atribuída",
                "description": ("Cada tela recebe a taxa (padrão 5 %; exceções 3 %/7 %) sobre a receita atribuída a "
                                "ela pela tabela de preços. Se a soma passar do pool, todas são reduzidas na mesma "
                                "proporção para caber no pool. Pagamento proporcional às exibições comprovadas; "
                                "sobras e arredondamentos ficam com a RD."),
            },
        },
        "rules": [
            "Somente proof-of-play válido (100 % da duração) conta; sem tolerância.",
            "Acúmulo diário em micro-centavos; fechamento no último dia do mês às 23:59:59 (America/Sao_Paulo) "
            "com arredondamento para baixo por estabelecimento; o resíduo é lançado para a RD.",
            "No dia 1 o valor pendente vira disponível; pagamento automático a partir de R$ 1,00; "
            "valores menores acumulam sem expiração.",
            "Ajustes manuais exigem dupla aprovação dos Admin Master.",
        ],
        "examples": examples,
    }
