"""
RD Mídia Indoor — Serviço de preços (tabela ativa, planos, promoções, cupons, margem).

``price_quote(db, ctx, request) -> dict`` é a ÚNICA fonte de preço (plano §4): o servidor
calcula a partir da tabela ATIVA; nenhum valor vindo do cliente é aceito como preço.
O resultado é determinístico (mesma entrada ⇒ mesmo ``quote_hash``) e é congelado no
pedido (``order_items.quote``) e na revisão da campanha.
"""
from __future__ import annotations

import calendar
import uuid
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Iterable, Optional

from sqlalchemy import func, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import Conflict, DomainError, NotFound
from app.core.settings_service import get_setting
from app.core.timeutils import local_date, utcnow
from app.models.pricing import (
    Coupon,
    CouponRedemption,
    Plan,
    PlanStatus,
    PriceItem,
    PriceTable,
    PriceTableStatus,
    Promotion,
)
from app.modules.pricing import engine as PE

DEFAULT_TZ = "America/Sao_Paulo"


# ─── datas de período ────────────────────────────────────────────────────────

def add_months(d: date, months: int) -> date:
    y, m = divmod(d.month - 1 + months, 12)
    y += d.year
    m += 1
    return date(y, m, min(d.day, calendar.monthrange(y, m)[1]))


def period_end(start: date, period_type: str, period_days: Optional[int] = None) -> date:
    if period_type == "AVULSO":
        if not period_days or period_days < 1:
            raise DomainError("PERIOD_DAYS_REQUIRED", "Informe a quantidade de dias do período avulso.", status=400)
        return start + timedelta(days=period_days - 1)
    months = {"MENSAL": 1, "SEMESTRAL": 6, "ANUAL": 12}.get(period_type)
    if months is None:
        raise DomainError("PERIOD_NOT_ALLOWED", "Período permitido: avulso, mensal, semestral ou anual "
                          "(trimestral não é oferecido).", status=400, details={"period_type": period_type})
    return add_months(start, months) - timedelta(days=1)


# ─── tabela ativa ────────────────────────────────────────────────────────────

async def active_table(db: AsyncSession) -> PriceTable:
    t = (await db.execute(select(PriceTable).where(PriceTable.status == PriceTableStatus.ATIVA.value))).scalar_one_or_none()
    if t is None:
        raise Conflict("PRICE_TABLE_NOT_ACTIVE", "Não há tabela de preços ativa. Configure os preços no painel.")
    return t


async def item_facts(db: AsyncSession, table_id: uuid.UUID) -> list[PE.ItemFact]:
    rows = (await db.execute(select(PriceItem).where(PriceItem.table_id == table_id))).scalars().all()
    return [PE.ItemFact(id=str(i.id), kind=i.kind, key=i.key, mode=i.mode, min_value=i.min_value,
                        max_value=i.max_value, value=int(i.value)) for i in rows]


# ─── cupom ───────────────────────────────────────────────────────────────────

async def find_coupon(db: AsyncSession, code: str, *, org_id: uuid.UUID, plan_id: Optional[uuid.UUID],
                      at: datetime) -> Coupon:
    norm = (code or "").strip().upper()
    c = (await db.execute(select(Coupon).where(Coupon.code == norm))).scalar_one_or_none()
    if c is None or not c.active:
        raise DomainError("COUPON_INVALID", "Cupom inválido.", status=400)
    if at < c.valid_from:
        raise DomainError("COUPON_NOT_STARTED", "Cupom ainda não está válido.", status=400)
    if at >= c.valid_until:
        raise DomainError("COUPON_EXPIRED", "Cupom expirado.", status=400)
    if c.max_uses is not None and c.uses_count >= c.max_uses:
        raise DomainError("COUPON_EXHAUSTED", "Cupom esgotado.", status=409)
    if c.plan_ids and (plan_id is None or str(plan_id) not in [str(p) for p in c.plan_ids]):
        raise DomainError("COUPON_PLAN_MISMATCH", "Cupom não vale para este plano.", status=400)
    if c.max_uses_per_org is not None:
        used = (await db.execute(select(func.count()).select_from(CouponRedemption).where(
            CouponRedemption.coupon_id == c.id, CouponRedemption.org_id == org_id,
            CouponRedemption.released_at.is_(None)))).scalar_one()
        if used >= c.max_uses_per_org:
            raise DomainError("COUPON_ORG_LIMIT", "Limite de uso deste cupom atingido para a sua organização.",
                              status=409)
    return c


async def redeem_coupon(db: AsyncSession, coupon: Coupon, *, org_id: uuid.UUID, order_id: uuid.UUID,
                        discount_cents: int) -> CouponRedemption:
    """Consumo ATÔMICO: ``UPDATE … WHERE uses_count < max_uses`` (corrida ⇒ só um vence)."""
    now = utcnow()
    res = await db.execute(
        update(Coupon)
        .where(Coupon.id == coupon.id, Coupon.active.is_(True), Coupon.valid_from <= now, Coupon.valid_until > now,
               or_(Coupon.max_uses.is_(None), Coupon.uses_count < Coupon.max_uses))
        .values(uses_count=Coupon.uses_count + 1)
        .returning(Coupon.id)
        .execution_options(synchronize_session=False)
    )
    if res.first() is None:
        raise DomainError("COUPON_EXHAUSTED", "Cupom esgotado ou expirado.", status=409)
    if coupon.max_uses_per_org is not None:
        used = (await db.execute(select(func.count()).select_from(CouponRedemption).where(
            CouponRedemption.coupon_id == coupon.id, CouponRedemption.org_id == org_id,
            CouponRedemption.released_at.is_(None)))).scalar_one()
        if used >= coupon.max_uses_per_org:
            raise DomainError("COUPON_ORG_LIMIT", "Limite de uso deste cupom atingido para a sua organização.",
                              status=409)
    r = CouponRedemption(id=uuid.uuid4(), coupon_id=coupon.id, org_id=org_id, order_id=order_id,
                         discount_cents=discount_cents)
    db.add(r)
    await db.flush([r])
    return r


async def release_coupon(db: AsyncSession, order_id: uuid.UUID) -> int:
    rows = (await db.execute(select(CouponRedemption).where(CouponRedemption.order_id == order_id,
                                                            CouponRedemption.released_at.is_(None))
                             .with_for_update().execution_options(populate_existing=True))).scalars().all()
    for r in rows:
        r.released_at = utcnow()
        await db.execute(update(Coupon).where(Coupon.id == r.coupon_id, Coupon.uses_count > 0)
                         .values(uses_count=Coupon.uses_count - 1).execution_options(synchronize_session=False))
    return len(rows)


# ─── promoção / plano ────────────────────────────────────────────────────────

async def best_promotion(db: AsyncSession, plan_id: Optional[uuid.UUID], at: datetime) -> Optional[Promotion]:
    rows = (await db.execute(select(Promotion).where(Promotion.active.is_(True), Promotion.valid_from <= at,
                                                     Promotion.valid_until > at))).scalars().all()
    ok = [p for p in rows if not p.plan_ids or (plan_id is not None and str(plan_id) in [str(x) for x in p.plan_ids])]
    if not ok:
        return None
    return sorted(ok, key=lambda p: (-p.discount_bps, str(p.id)))[0]


async def usable_plan(db: AsyncSession, plan_id: uuid.UUID, org_id: uuid.UUID) -> Plan:
    p = await db.get(Plan, plan_id)
    if p is None or (p.kind == "CUSTOM" and p.org_id != org_id):
        raise NotFound()
    if p.status != PlanStatus.ATIVO.value:
        raise DomainError("PLAN_NOT_ACTIVE", "Plano indisponível.", status=409)
    return p


def plan_mismatch(plan: Plan, *, duration_s: int, freq_kind: str, freq_value: int, period_type: str,
                  screens: int, exclusivity: str, priority: str) -> list[str]:
    bad = []
    if plan.duration_s != duration_s:
        bad.append("duration_s")
    if plan.freq_kind != freq_kind or plan.freq_value != freq_value:
        bad.append("frequency")
    if plan.period_type != period_type:
        bad.append("period_type")
    if not (plan.screens_min <= screens <= plan.screens_max):
        bad.append("screens")
    if plan.max_screens is not None and screens > plan.max_screens:
        bad.append("max_screens")
    if plan.exclusivity != exclusivity:
        bad.append("exclusivity")
    if plan.priority != priority:
        bad.append("priority")
    return bad


# ─── cotação ─────────────────────────────────────────────────────────────────

@dataclass
class PriceQuoteRequest:
    org_id: uuid.UUID
    duration_s: int
    freq_kind: str
    freq_value: int
    start_date: date
    end_date: date
    period_type: str
    screens: list  # ResolvedScreen (resolvidas NO SERVIDOR)
    days_mask: int = 127
    time_bands: list = field(default_factory=lambda: [[0, 1440]])
    specific_times: list = field(default_factory=list)
    priority: str = "STANDARD"
    exclusivity: str = "NONE"
    fees: tuple = ()
    plan_id: Optional[uuid.UUID] = None
    coupon_code: Optional[str] = None
    commercial_discount_bps: int = 0
    with_seller: bool = True
    gateway_method: str = "DEFAULT"
    at: Optional[datetime] = None
    # cupom já validado no nível do pedido (ex.: valor fixo rateado entre as campanhas do carrinho)
    coupon_fact: Optional[PE.CouponFact] = None


def _screen_facts(screens: Iterable) -> tuple[PE.ScreenFact, ...]:
    out = []
    for s in screens:
        out.append(PE.ScreenFact(
            screen_id=str(s.screen_id), establishment_id=str(s.establishment_id), region=s.region, state=s.state,
            city_key=s.city_key, screen_class=s.screen_class or "STANDARD", venue_class=s.venue_class or "STANDARD"))
    return tuple(sorted(out, key=lambda x: x.screen_id))


async def price_quote(db: AsyncSession, ctx, request: PriceQuoteRequest) -> dict:
    """Cotação determinística (plano §4). Erros de negócio → DomainError (400/409)."""
    at = request.at or utcnow()
    table = await active_table(db)
    items = await item_facts(db, table.id)
    plan = None
    plan_discount = 0
    if request.plan_id is not None:
        plan = await usable_plan(db, request.plan_id, request.org_id)
        bad = plan_mismatch(plan, duration_s=request.duration_s, freq_kind=request.freq_kind,
                            freq_value=request.freq_value, period_type=request.period_type,
                            screens=len(request.screens), exclusivity=request.exclusivity, priority=request.priority)
        if bad:
            raise DomainError("PLAN_MISMATCH", "A campanha não corresponde aos parâmetros do plano.", status=400,
                              details={"fields": bad})
        plan_discount = plan.discount_bps
    promo = await best_promotion(db, request.plan_id, at)
    coupon = None
    if request.coupon_fact is not None:
        coupon = None
    elif request.coupon_code:
        coupon = await find_coupon(db, request.coupon_code, org_id=request.org_id, plan_id=request.plan_id, at=at)
    inp = PE.QuoteInput(
        duration_s=request.duration_s, freq_kind=request.freq_kind, freq_value=request.freq_value,
        start_date=request.start_date, end_date=request.end_date, period_type=request.period_type,
        screens=_screen_facts(request.screens), days_mask=request.days_mask,
        time_bands=tuple(tuple(b) for b in request.time_bands), specific_times=tuple(request.specific_times or ()),
        priority=request.priority, exclusivity=request.exclusivity, fees=tuple(sorted(set(request.fees))),
        plan_id=str(plan.id) if plan else None, plan_discount_bps=plan_discount,
        promotion=(str(promo.id), promo.name, promo.discount_bps) if promo else None,
        commercial_discount_bps=request.commercial_discount_bps,
        coupon=request.coupon_fact or (PE.CouponFact(id=str(coupon.id), code=coupon.code,
                                                     discount_type=coupon.discount_type,
                                                     discount_bps=coupon.discount_bps,
                                                     discount_cents=int(coupon.discount_cents)) if coupon else None),
        with_seller=request.with_seller,
        commission_pool_bps=int(await get_setting(db, "commission.pool_bps")),
        min_margin_bps=int(await get_setting(db, "pricing.min_margin_bps")),
        gateway_method=request.gateway_method,
    )
    try:
        q = PE.compute_quote(items, inp, table={"id": str(table.id), "version": table.version})
    except PE.PricingError as exc:
        status = 409 if exc.code == "PRICE_TABLE_INCOMPLETE" else 400
        raise DomainError(exc.code, exc.message, status=status, details=exc.details) from None
    if coupon is not None and q["media_gross_cents"] < int(coupon.min_order_cents):
        raise DomainError("COUPON_MIN_ORDER", "Valor mínimo do pedido para este cupom não atingido.", status=400,
                          details={"min_order_cents": int(coupon.min_order_cents)})
    # defesa em profundidade: preço final (após plano/promoção/cupom/desconto) nunca abaixo do piso RD
    from app.modules.pricing import floors as FL

    await FL.enforce_quote(db, q)
    return q


def ensure_margin_allowed(quote: dict) -> list[str]:
    """Bloqueia margem negativa; devolve alertas (abaixo do mínimo)."""
    m = quote["margin"]
    if m["status"] == "NEGATIVE":
        raise DomainError("MARGIN_NEGATIVE", "Operação bloqueada: a margem ficaria negativa.", status=409,
                          details={"margin_cents": m["margin_cents"], "margin_bps": m["margin_bps"]})
    if m["status"] == "BELOW_MIN":
        return [f"MARGIN_BELOW_MIN:{m['margin_bps']}<{m['min_margin_bps']}"]
    return []


# ─── margem de plano (ativação) ──────────────────────────────────────────────

async def plan_reference_quote(db: AsyncSession, plan: Plan, *, table: Optional[PriceTable] = None,
                               today: Optional[date] = None, promotion: Optional[tuple] = None,
                               coupon: Optional[PE.CouponFact] = None) -> dict:
    table = table or await active_table(db)
    items = await item_facts(db, table.id)
    today = today or local_date(utcnow(), DEFAULT_TZ)
    end = period_end(today, plan.period_type, plan.period_days)
    screens = tuple(PE.ScreenFact(screen_id=f"ref-{i:05d}", establishment_id=f"ref-est-{i:05d}")
                    for i in range(plan.screens_max))
    inp = PE.QuoteInput(
        duration_s=plan.duration_s, freq_kind=plan.freq_kind, freq_value=plan.freq_value, start_date=today,
        end_date=end, period_type=plan.period_type, screens=screens, days_mask=plan.days_mask,
        time_bands=tuple(tuple(b) for b in (plan.time_bands or [[0, 1440]])),
        specific_times=tuple(plan.specific_times or ()), priority=plan.priority, exclusivity=plan.exclusivity,
        plan_id=str(plan.id), plan_discount_bps=plan.discount_bps, with_seller=True,
        commission_pool_bps=int(await get_setting(db, "commission.pool_bps")),
        min_margin_bps=int(await get_setting(db, "pricing.min_margin_bps")), promotion=promotion, coupon=coupon,
    )
    try:
        return PE.compute_quote(items, inp, table={"id": str(table.id), "version": table.version})
    except PE.PricingError as exc:
        raise DomainError(exc.code, exc.message, status=409 if exc.code == "PRICE_TABLE_INCOMPLETE" else 400,
                          details=exc.details) from None


async def active_table_or_none(db: AsyncSession) -> Optional[PriceTable]:
    return (await db.execute(select(PriceTable).where(PriceTable.status == PriceTableStatus.ATIVA.value))
            ).scalar_one_or_none()


async def enforce_plan_floor(db: AsyncSession, plan: Plan, *, table: Optional[PriceTable] = None,
                             promotion: Optional[tuple] = None, coupon: Optional[PE.CouponFact] = None,
                             quote: Optional[dict] = None) -> Optional[dict]:
    """Piso RD sobre o cenário de referência do plano (opcionalmente com promoção/cupom). Sem tabela ativa ⇒ sem
    cenário calculável: a verificação ocorre na ativação do plano e em toda cotação/checkout."""
    from app.modules.pricing import floors as FL

    if not await FL.floors_apply(db):
        return quote
    table = table or await active_table_or_none(db)
    if table is None and quote is None:
        return None
    q = quote or await plan_reference_quote(db, plan, table=table, promotion=promotion, coupon=coupon)
    await FL.enforce_quote(db, q)
    return q


async def plans_affected(db: AsyncSession, plan_ids: Optional[list]) -> list[Plan]:
    q = select(Plan).where(Plan.status == PlanStatus.ATIVO.value)
    if plan_ids:
        q = q.where(Plan.id.in_([uuid.UUID(str(x)) for x in plan_ids]))
    return list((await db.execute(q.order_by(Plan.id))).scalars())
