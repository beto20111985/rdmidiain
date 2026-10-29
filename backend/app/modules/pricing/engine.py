"""
RD Mídia Indoor — Motor de preços PURO (sem banco), determinístico, aritmética exata.

ETAPA2 #487–#542; P001 §52–§54; P002 #29; ADR_009 #7.

Preço de mídia (por tela ``s``)::

    unit_s  = (BASE[duração] + Σ adições) × Π multiplicadores(campanha) × Π multiplicadores(tela s)
    media   = round_half_up( Σ_s unit_s × inserções_ponderadas_por_dia × dias_contratados )

- ``BASE`` (DURATION_BASE): centavos por inserção por tela para 30/60/120 s.
- Multiplicadores de campanha: FREQUENCY (faixa de freq_value), SCREEN_COUNT (faixa
  de telas), PERIOD, PRIORITY, EXCLUSIVITY. Por tela: REGION (cidade > UF > região),
  SCREEN_CLASS, VENUE_CLASS, ESTABLISHMENT. Faixa horária (TIME_BAND) pondera as
  inserções de cada intervalo de minutos local.
- Para cada ``kind`` é escolhido UM item: chave exata antes de ``*``; faixa mais
  estreita; desempate pelo id (determinístico).
- Descontos sequenciais (floor, ``money.apply_bps``): plano → promoção → desconto
  comercial aprovado → cupom. Depois o PREÇO MÍNIMO (MIN_PRICE) é garantido por um
  ajuste explícito. Taxas (arte/instalação/upgrade) são linhas SEPARADAS (SERVICE).
- Margem (P001 §53): receita − gateway − comissão estabelecimentos (``commission.pool_bps``
  sobre a receita de MÍDIA) − comissão vendedor − impostos − servidor − equipamento −
  designer − custos diretos. NEGATIVA ⇒ bloqueio; abaixo de ``pricing.min_margin_bps`` ⇒ alerta.

Nenhum ``float``: ``Fraction`` exata internamente; saída em centavos inteiros.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import date, timedelta
from fractions import Fraction
from typing import Iterable, Optional

from app.core.money import BPS_DENOMINATOR, allocate, apply_bps

ENGINE_VERSION = "pricing-1.0"
ONE = Fraction(1)
BPS = Fraction(BPS_DENOMINATOR)
DAY_MINUTES = 1440
VALID_PERIODS = ("AVULSO", "MENSAL", "SEMESTRAL", "ANUAL")
VALID_DURATIONS = (30, 60, 120)


class PricingError(Exception):
    def __init__(self, code: str, message: str, details: Optional[dict] = None):
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message
        self.details = details or {}


@dataclass(frozen=True)
class ItemFact:
    id: str
    kind: str
    key: str
    mode: str
    min_value: Optional[int]
    max_value: Optional[int]
    value: int


@dataclass(frozen=True)
class ScreenFact:
    screen_id: str
    establishment_id: str
    region: Optional[str] = None
    state: Optional[str] = None
    city_key: Optional[str] = None
    screen_class: str = "STANDARD"
    venue_class: str = "STANDARD"


@dataclass(frozen=True)
class CouponFact:
    id: str
    code: str
    discount_type: str  # PERCENT | FIXED
    discount_bps: int = 0
    discount_cents: int = 0


@dataclass(frozen=True)
class QuoteInput:
    duration_s: int
    freq_kind: str
    freq_value: int
    start_date: date
    end_date: date
    period_type: str
    screens: tuple[ScreenFact, ...]
    days_mask: int = 127
    time_bands: tuple[tuple[int, int], ...] = ((0, DAY_MINUTES),)
    specific_times: tuple[str, ...] = ()
    priority: str = "STANDARD"
    exclusivity: str = "NONE"
    fees: tuple[str, ...] = ()  # ART | INSTALLATION | UPGRADE
    plan_id: Optional[str] = None
    plan_discount_bps: int = 0
    promotion: Optional[tuple[str, str, int]] = None  # (id, nome, bps)
    commercial_discount_bps: int = 0
    coupon: Optional[CouponFact] = None
    with_seller: bool = True
    commission_pool_bps: int = 500
    min_margin_bps: int = 4000
    gateway_method: str = "DEFAULT"


# ─── utilidades ──────────────────────────────────────────────────────────────

def round_half_up(x: Fraction) -> int:
    if x < 0:
        return -round_half_up(-x)
    return int((x + Fraction(1, 2)).__floor__())


def parse_hhmm(v: str) -> int:
    try:
        hh, mm = v.split(":")
        h, m = int(hh), int(mm)
    except (ValueError, AttributeError):
        raise PricingError("SPECIFIC_TIME_INVALID", "Horário específico inválido (use HH:MM).", {"value": v}) from None
    if not (0 <= h <= 23 and 0 <= m <= 59) or len(hh) != 2 or len(mm) != 2:
        raise PricingError("SPECIFIC_TIME_INVALID", "Horário específico inválido (use HH:MM).", {"value": v})
    return h * 60 + m


def count_days(start: date, end: date, days_mask: int) -> int:
    if end < start:
        raise PricingError("DATES_INVALID", "Data final anterior à inicial.")
    n = 0
    d = start
    while d <= end:
        if days_mask & (1 << d.weekday()):
            n += 1
        d += timedelta(days=1)
    return n


def _in_range(item: ItemFact, value: Optional[int]) -> bool:
    if value is None:
        return item.min_value is None and item.max_value is None
    if item.min_value is not None and value < item.min_value:
        return False
    if item.max_value is not None and value > item.max_value:
        return False
    return True


def _width(item: ItemFact) -> int:
    if item.min_value is None or item.max_value is None:
        return 10**12
    return item.max_value - item.min_value


def pick(items: Iterable[ItemFact], kind: str, *, key: Optional[str] = None, value: Optional[int] = None,
         modes: tuple[str, ...] = ("MULTIPLIER_BPS", "ADD_CENTS")) -> Optional[ItemFact]:
    cands = []
    for it in items:
        if it.kind != kind or it.mode not in modes:
            continue
        if key is not None and it.key not in (key, "*"):
            continue
        if value is not None and not _in_range(it, value):
            continue
        if value is None and (it.min_value is not None or it.max_value is not None) and kind not in ("TIME_BAND",):
            continue
        cands.append(it)
    if not cands:
        return None
    cands.sort(key=lambda it: (0 if (key is not None and it.key == key) else 1, _width(it), it.id))
    return cands[0]


def _apply(item: Optional[ItemFact], adds: list, mults: list, applied: list) -> None:
    if item is None:
        return
    applied.append(item.id)
    if item.mode == "MULTIPLIER_BPS":
        mults.append(Fraction(item.value) / BPS)
    elif item.mode == "ADD_CENTS":
        adds.append(Fraction(item.value))


def _band_multiplier_fn(items: list[ItemFact]):
    bands = sorted(
        [i for i in items if i.kind == "TIME_BAND" and i.mode == "MULTIPLIER_BPS"],
        key=lambda i: (_width(i), i.min_value or 0, i.id),
    )

    def at(minute: int) -> tuple[Fraction, Optional[str]]:
        for b in bands:
            lo = b.min_value if b.min_value is not None else 0
            hi = b.max_value if b.max_value is not None else DAY_MINUTES
            if lo <= minute < hi:
                return Fraction(b.value) / BPS, b.id
        return ONE, None

    breakpoints = set()
    for b in bands:
        if b.min_value is not None:
            breakpoints.add(b.min_value)
        if b.max_value is not None:
            breakpoints.add(b.max_value)
    return at, breakpoints


def weighted_insertions_per_day(inp: QuoteInput, items: list[ItemFact]) -> tuple[Fraction, Fraction, list[str]]:
    """(inserções/dia, inserções ponderadas pela faixa horária/dia, itens TIME_BAND usados)."""
    at, bps_points = _band_multiplier_fn(items)
    used: set[str] = set()
    bands = [tuple(b) for b in inp.time_bands]
    for a, b in bands:
        if not (0 <= a < b <= DAY_MINUTES):
            raise PricingError("TIME_BAND_INVALID", "Faixa horária inválida.", {"band": [a, b]})
    total_minutes = sum(b - a for a, b in bands)
    weighted_minutes = Fraction(0)
    for a, b in bands:
        cuts = sorted({a, b} | {p for p in bps_points if a < p < b})
        for x, y in zip(cuts, cuts[1:]):
            m, iid = at(x)
            if iid:
                used.add(iid)
            weighted_minutes += (y - x) * m
    if inp.freq_kind == "PER_HOUR":
        raw = Fraction(inp.freq_value * total_minutes, 60)
        weighted = inp.freq_value * weighted_minutes / 60
    elif inp.freq_kind == "PER_DAY":
        raw = Fraction(inp.freq_value)
        weighted = inp.freq_value * weighted_minutes / total_minutes
    elif inp.freq_kind == "SPECIFIC_TIME":
        if not inp.specific_times:
            raise PricingError("SPECIFIC_TIME_REQUIRED", "Informe ao menos um horário específico.")
        raw = Fraction(len(inp.specific_times))
        weighted = Fraction(0)
        for t in inp.specific_times:
            minute = parse_hhmm(t)
            if not any(a <= minute < b for a, b in bands):
                raise PricingError("SPECIFIC_TIME_OUTSIDE_BAND", "Horário específico fora das faixas contratadas.",
                                   {"time": t})
            m, iid = at(minute)
            if iid:
                used.add(iid)
            weighted += m
    else:
        raise PricingError("FREQUENCY_INVALID", "Tipo de frequência inválido.")
    return raw, weighted, sorted(used)


def _region_item(items: list[ItemFact], s: ScreenFact) -> Optional[ItemFact]:
    for key in (s.city_key, s.state, s.region):
        if not key:
            continue
        exact = [i for i in items if i.kind == "REGION" and i.key == key and i.mode in ("MULTIPLIER_BPS", "ADD_CENTS")]
        if exact:
            return sorted(exact, key=lambda i: i.id)[0]
    return pick(items, "REGION", key="*")


def canonical_hash(obj: dict) -> str:
    raw = json.dumps(obj, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(raw.encode()).hexdigest()


# ─── cálculo ─────────────────────────────────────────────────────────────────

def compute_quote(items: Iterable[ItemFact], inp: QuoteInput, *, table: Optional[dict] = None) -> dict:
    items = sorted(items, key=lambda i: i.id)
    if inp.duration_s not in VALID_DURATIONS:
        raise PricingError("DURATION_NOT_ALLOWED", "Duração permitida: 30, 60 ou 120 segundos.",
                           {"duration_s": inp.duration_s})
    if inp.period_type not in VALID_PERIODS:
        raise PricingError("PERIOD_NOT_ALLOWED", "Período permitido: avulso, mensal, semestral ou anual.",
                           {"period_type": inp.period_type})
    if inp.freq_value <= 0:
        raise PricingError("FREQUENCY_INVALID", "Frequência deve ser positiva.")
    if not inp.screens:
        raise PricingError("NO_SCREENS", "Nenhuma tela disponível para a segmentação escolhida.")
    if not (1 <= inp.days_mask <= 127):
        raise PricingError("DAYS_INVALID", "Selecione ao menos um dia da semana.")

    days = count_days(inp.start_date, inp.end_date, inp.days_mask)
    calendar_days = (inp.end_date - inp.start_date).days + 1
    if days == 0:
        raise PricingError("NO_DAYS", "O período não contém nenhum dos dias da semana escolhidos.")
    n_screens = len(inp.screens)

    base_item = pick(items, "DURATION_BASE", key=str(inp.duration_s), modes=("BASE_CENTS",))
    if base_item is None or base_item.key != str(inp.duration_s):
        raise PricingError("PRICE_TABLE_INCOMPLETE", "Tabela de preços sem preço-base para a duração.",
                           {"missing": f"DURATION_BASE:{inp.duration_s}"})
    applied: list[str] = [base_item.id]

    raw_per_day, weighted_per_day, band_ids = weighted_insertions_per_day(inp, items)
    applied += band_ids

    c_adds: list[Fraction] = []
    c_mults: list[Fraction] = []
    _apply(pick(items, "FREQUENCY", key=inp.freq_kind, value=inp.freq_value), c_adds, c_mults, applied)
    _apply(pick(items, "SCREEN_COUNT", value=n_screens), c_adds, c_mults, applied)
    _apply(pick(items, "PERIOD", key=inp.period_type), c_adds, c_mults, applied)
    if inp.priority != "STANDARD":
        _apply(pick(items, "PRIORITY", key=inp.priority), c_adds, c_mults, applied)
    if inp.exclusivity != "NONE":
        ex = pick(items, "EXCLUSIVITY", key=inp.exclusivity)
        if ex is None:
            raise PricingError("PRICE_TABLE_INCOMPLETE", "Tabela de preços sem item de exclusividade.",
                               {"missing": f"EXCLUSIVITY:{inp.exclusivity}"})
        _apply(ex, c_adds, c_mults, applied)

    campaign_mult = ONE
    for m in c_mults:
        campaign_mult *= m
    base = Fraction(base_item.value) + sum(c_adds, Fraction(0))

    per_screen: dict[str, Fraction] = {}
    groups: dict[tuple, dict] = {}
    for s in sorted(inp.screens, key=lambda x: x.screen_id):
        s_adds: list[Fraction] = []
        s_mults: list[Fraction] = []
        s_applied: list[str] = []
        _apply(_region_item(items, s), s_adds, s_mults, s_applied)
        _apply(pick(items, "SCREEN_CLASS", key=s.screen_class), s_adds, s_mults, s_applied)
        _apply(pick(items, "VENUE_CLASS", key=s.venue_class), s_adds, s_mults, s_applied)
        est_item = next((i for i in items if i.kind == "ESTABLISHMENT" and i.key == s.establishment_id
                         and i.mode == "MULTIPLIER_BPS"), None)
        _apply(est_item, s_adds, s_mults, s_applied)
        unit = (base + sum(s_adds, Fraction(0))) * campaign_mult
        for m in s_mults:
            unit *= m
        value = unit * weighted_per_day * days
        per_screen[s.screen_id] = value
        applied += s_applied
        gk = (s.region or "", s.state or "", s.city_key or "", s.screen_class, s.venue_class)
        g = groups.setdefault(gk, {"region": s.region, "state": s.state, "city_key": s.city_key,
                                   "screen_class": s.screen_class, "venue_class": s.venue_class,
                                   "screens": 0, "_value": Fraction(0), "_unit": unit})
        g["screens"] += 1
        g["_value"] += value

    media_raw = sum(per_screen.values(), Fraction(0))
    media_gross = round_half_up(media_raw)

    # pesos inteiros (milicentavos) para rateios (modelo B de comissão, exibição por grupo)
    weights = {sid: round_half_up(v * 1000) for sid, v in per_screen.items()}
    group_list = sorted(groups.values(), key=lambda g: (g["region"] or "", g["state"] or "", g["city_key"] or "",
                                                        g["screen_class"], g["venue_class"]))
    galloc = allocate(media_gross, [(i, round_half_up(g["_value"] * 1000)) for i, g in enumerate(group_list)])
    group_out = []
    shares = dict(galloc.shares)
    if group_list:
        # resíduo do rateio vai para o primeiro grupo (determinístico) — a soma fecha com media_gross
        shares[0] = shares.get(0, 0) + galloc.residual_cents
    for i, g in enumerate(group_list):
        group_out.append({
            "region": g["region"], "state": g["state"], "city_key": g["city_key"],
            "screen_class": g["screen_class"], "venue_class": g["venue_class"], "screens": g["screens"],
            "unit_price_millicents": round_half_up(g["_unit"] * 1000), "media_cents": shares.get(i, 0),
        })

    lines: list[dict] = [{
        "code": "MEDIA", "kind": "MEDIA", "amount_cents": media_gross,
        "description": f"Veiculação {inp.duration_s}s em {n_screens} tela(s), {days} dia(s)",
    }]

    # descontos sequenciais
    net = media_gross
    discounts: list[dict] = []

    def add_discount(code: str, amount: int, desc: str, ref: Optional[str] = None) -> None:
        nonlocal net
        amount = max(0, min(amount, net))
        if amount <= 0:
            return
        net -= amount
        d = {"code": code, "kind": "DISCOUNT", "amount_cents": -amount, "description": desc}
        if ref:
            d["ref"] = ref
        discounts.append(d)

    if inp.plan_discount_bps:
        add_discount("PLAN_DISCOUNT", apply_bps(net, inp.plan_discount_bps), "Desconto do plano", inp.plan_id)
    if inp.promotion:
        pid, pname, pbps = inp.promotion
        add_discount("PROMOTION", apply_bps(net, pbps), f"Promoção {pname}", pid)
    if inp.commercial_discount_bps:
        add_discount("COMMERCIAL_DISCOUNT", apply_bps(net, inp.commercial_discount_bps), "Desconto comercial aprovado")
    if inp.coupon:
        c = inp.coupon
        amt = apply_bps(net, c.discount_bps) if c.discount_type == "PERCENT" else c.discount_cents
        add_discount("COUPON", amt, f"Cupom {c.code}", c.id)
    lines += discounts

    min_item = pick(items, "MIN_PRICE", modes=("FIXED_CENTS",))
    min_adjust = 0
    if min_item is not None and net < min_item.value:
        min_adjust = min_item.value - net
        net = min_item.value
        applied.append(min_item.id)
        lines.append({"code": "MIN_PRICE_ADJUSTMENT", "kind": "ADJUSTMENT", "amount_cents": min_adjust,
                      "description": "Ajuste para o preço mínimo"})

    fees_cents = 0
    fee_lines = []
    for fee in sorted(set(inp.fees)):
        it = pick(items, "FEE", key=fee, modes=("FIXED_CENTS",))
        if it is None or it.key != fee:
            raise PricingError("PRICE_TABLE_INCOMPLETE", "Tabela de preços sem a taxa solicitada.",
                               {"missing": f"FEE:{fee}"})
        applied.append(it.id)
        fees_cents += it.value
        fee_lines.append({"code": f"FEE_{fee}", "kind": "SERVICE", "amount_cents": it.value,
                          "description": {"ART": "Criação de arte", "INSTALLATION": "Instalação",
                                          "UPGRADE": "Upgrade de TV"}.get(fee, fee)})
    lines += fee_lines

    media_net = net
    total = media_net + fees_cents
    discount_total = -sum(d["amount_cents"] for d in discounts)

    margin = compute_margin(items, inp, revenue_cents=total, media_revenue_cents=media_net, n_screens=n_screens,
                            calendar_days=calendar_days, applied=applied)

    out = {
        "engine_version": ENGINE_VERSION,
        "currency": "BRL",
        "price_table": table or {},
        "inputs": {
            "duration_s": inp.duration_s, "freq_kind": inp.freq_kind, "freq_value": inp.freq_value,
            "specific_times": list(inp.specific_times), "days_mask": inp.days_mask,
            "time_bands": [list(b) for b in inp.time_bands], "start_date": inp.start_date.isoformat(),
            "end_date": inp.end_date.isoformat(), "period_type": inp.period_type, "priority": inp.priority,
            "exclusivity": inp.exclusivity, "fees": sorted(set(inp.fees)), "plan_id": inp.plan_id,
            "screens_count": n_screens, "contracted_days": days, "calendar_days": calendar_days,
            "coupon_code": inp.coupon.code if inp.coupon else None,
        },
        "insertions": {
            "per_screen_per_day": float_free(raw_per_day),
            "total": int((raw_per_day * days * n_screens).__floor__()),
        },
        "lines": lines,
        "groups": group_out,
        "media_gross_cents": media_gross,
        "discount_cents": discount_total,
        "min_price_adjustment_cents": min_adjust,
        "media_cents": media_net,
        "fees_cents": fees_cents,
        "total_cents": total,
        "margin": margin,
        "applied_item_ids": sorted(set(applied)),
        "per_screen_weights": weights,
    }
    out["quote_hash"] = canonical_hash(out)
    return out


def float_free(x: Fraction) -> str:
    """Representação textual exata (sem float): '30' ou '45/2'."""
    return str(x.numerator) if x.denominator == 1 else f"{x.numerator}/{x.denominator}"


def compute_margin(items: list[ItemFact], inp: QuoteInput, *, revenue_cents: int, media_revenue_cents: int,
                   n_screens: int, calendar_days: int, applied: Optional[list] = None) -> dict:
    applied = applied if applied is not None else []
    R = revenue_cents

    def pct(bps: int, base: int) -> int:
        return apply_bps(base, bps) if base > 0 else 0

    def matching(kind: str, key: Optional[str] = None) -> list[ItemFact]:
        cand = [i for i in items if i.kind == kind]
        if key is not None:
            exact = [i for i in cand if i.key == key]
            cand = exact if exact else [i for i in cand if i.key in ("*", "DEFAULT")]
        return cand

    comps: dict[str, int] = {}
    gw = matching("COST_GATEWAY", inp.gateway_method)
    comps["gateway_cents"] = sum(pct(i.value, R) for i in gw if i.mode == "PERCENT_BPS") + sum(
        i.value for i in gw if i.mode == "FIXED_CENTS")
    applied += [i.id for i in gw]
    comps["establishment_commission_cents"] = pct(inp.commission_pool_bps, media_revenue_cents)
    seller = matching("COST_SELLER") if inp.with_seller else []
    comps["seller_commission_cents"] = sum(pct(i.value, R) for i in seller if i.mode == "PERCENT_BPS")
    applied += [i.id for i in seller]
    taxes = matching("TAX")
    comps["taxes_cents"] = sum(pct(i.value, R) for i in taxes if i.mode == "PERCENT_BPS")
    applied += [i.id for i in taxes]

    def per_screen_month(kind: str) -> int:
        its = [i for i in matching(kind) if i.mode == "PER_SCREEN_MONTH_CENTS"]
        applied.extend(i.id for i in its)
        return sum(round_half_up(Fraction(i.value * n_screens * calendar_days, 30)) for i in its)

    comps["server_cents"] = per_screen_month("COST_SERVER")
    comps["equipment_cents"] = per_screen_month("COST_EQUIPMENT")
    designer = matching("COST_DESIGNER") if "ART" in inp.fees else []
    comps["designer_cents"] = sum(i.value for i in designer if i.mode == "FIXED_CENTS")
    applied += [i.id for i in designer]
    direct = matching("COST_DIRECT")
    comps["direct_costs_cents"] = sum(i.value for i in direct if i.mode == "FIXED_CENTS") + sum(
        pct(i.value, R) for i in direct if i.mode == "PERCENT_BPS")
    applied += [i.id for i in direct]

    total_costs = sum(comps.values())
    margin = R - total_costs
    if R > 0:
        margin_bps = (margin * BPS_DENOMINATOR) // R
    else:
        margin_bps = -BPS_DENOMINATOR if margin < 0 else 0
    if margin < 0:
        status = "NEGATIVE"
    elif margin_bps < inp.min_margin_bps:
        status = "BELOW_MIN"
    else:
        status = "OK"
    return {
        "revenue_cents": R,
        "media_revenue_cents": media_revenue_cents,
        **comps,
        "total_costs_cents": total_costs,
        "margin_cents": margin,
        "margin_bps": margin_bps,
        "min_margin_bps": inp.min_margin_bps,
        "commission_pool_bps": inp.commission_pool_bps,
        "status": status,
    }


def public_view(quote: dict) -> dict:
    """Visão do anunciante: sem pesos por tela, sem custos/margem e sem ids de itens."""
    q = {k: v for k, v in quote.items() if k not in ("per_screen_weights", "margin", "applied_item_ids")}
    return q
