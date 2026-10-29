"""Motor de preços PURO: cálculo determinístico, faixas, descontos, preço mínimo, taxas e margem."""
from datetime import date

import pytest

from app.modules.pricing import engine as PE


def it(i, kind, mode, value, key="*", lo=None, hi=None):
    return PE.ItemFact(id=f"i{i:03d}", kind=kind, key=key, mode=mode, min_value=lo, max_value=hi, value=value)


BASE = [
    it(1, "DURATION_BASE", "BASE_CENTS", 10, "30"),
    it(2, "DURATION_BASE", "BASE_CENTS", 18, "60"),
    it(3, "DURATION_BASE", "BASE_CENTS", 32, "120"),
]


def screens(n, **kw):
    return tuple(PE.ScreenFact(screen_id=f"s{i:04d}", establishment_id=f"e{i:04d}", **kw) for i in range(n))


def inp(**kw):
    d = dict(duration_s=30, freq_kind="PER_HOUR", freq_value=2, start_date=date(2026, 11, 2),
             end_date=date(2026, 11, 8), period_type="AVULSO", screens=screens(1), with_seller=False,
             commission_pool_bps=500, min_margin_bps=4000)
    d.update(kw)
    return PE.QuoteInput(**d)


def test_base_price_per_insertion():
    # 2×/h × 24 h × 7 dias × 1 tela × 10 c = 3.360 c
    q = PE.compute_quote(BASE, inp())
    assert q["media_gross_cents"] == 3360
    assert q["insertions"]["total"] == 336
    assert q["total_cents"] == 3360
    assert q["media_cents"] == 3360


def test_duration_proportional_and_disallowed_durations():
    assert PE.compute_quote(BASE, inp(duration_s=60))["media_gross_cents"] == 6048
    assert PE.compute_quote(BASE, inp(duration_s=120))["media_gross_cents"] == 10752
    for bad in (15, 45, 90):
        with pytest.raises(PE.PricingError) as e:
            PE.compute_quote(BASE, inp(duration_s=bad))
        assert e.value.code == "DURATION_NOT_ALLOWED"


def test_trimestral_rejeitado():
    with pytest.raises(PE.PricingError) as e:
        PE.compute_quote(BASE, inp(period_type="TRIMESTRAL"))
    assert e.value.code == "PERIOD_NOT_ALLOWED"


def test_days_mask_and_time_bands_and_band_multiplier():
    # só seg–sex (bits 0..4): 5 dias; faixa 08–12 (4 h) e 18–20 (2 h) = 6 h × 2 = 12/dia
    q = PE.compute_quote(BASE, inp(days_mask=0b0011111, time_bands=((480, 720), (1080, 1200))))
    assert q["media_gross_cents"] == 12 * 5 * 10
    # pico 18–20 com ×1,5 → (8 + 4×1,5) × 5 × 10
    items = BASE + [it(9, "TIME_BAND", "MULTIPLIER_BPS", 15000, lo=1080, hi=1200)]
    q = PE.compute_quote(items, inp(days_mask=0b0011111, time_bands=((480, 720), (1080, 1200))))
    assert q["media_gross_cents"] == (8 + 6) * 5 * 10


def test_campaign_and_screen_multipliers_most_specific_region():
    items = BASE + [
        it(10, "SCREEN_COUNT", "MULTIPLIER_BPS", 9000, lo=2, hi=10),  # volume −10%
        it(11, "REGION", "MULTIPLIER_BPS", 12000, key="GO"),  # UF
        it(12, "REGION", "MULTIPLIER_BPS", 15000, key="GO:goiania"),  # cidade vence UF
        it(13, "SCREEN_CLASS", "MULTIPLIER_BPS", 20000, key="PREMIUM"),
        it(14, "PRIORITY", "MULTIPLIER_BPS", 13000, key="PREMIUM"),
    ]
    scr = (PE.ScreenFact("a", "e1", state="GO", city_key="GO:goiania"),
           PE.ScreenFact("b", "e2", state="GO", city_key="GO:anapolis", screen_class="PREMIUM"))
    q = PE.compute_quote(items, inp(screens=scr, priority="PREMIUM", freq_value=1, end_date=date(2026, 11, 2)))
    # por tela/dia: 24 inserções; unit = 10 × 0,9 × 1,3 = 11,7 → a: ×1,5 = 17,55; b: ×1,2×2,0 = 28,08
    assert q["media_gross_cents"] == round((17.55 + 28.08) * 24)
    assert sum(g["media_cents"] for g in q["groups"]) == q["media_gross_cents"]
    assert sum(q["per_screen_weights"].values()) > 0


def test_discount_sequence_min_price_and_fees():
    items = BASE + [it(20, "MIN_PRICE", "FIXED_CENTS", 3000), it(21, "FEE", "FIXED_CENTS", 15000, key="ART")]
    coupon = PE.CouponFact("c1", "PROMO50", "PERCENT", discount_bps=5000)
    q = PE.compute_quote(items, inp(plan_discount_bps=1000, coupon=coupon, fees=("ART",)))
    # 3360 −10% (336) = 3024; cupom 50% (1512) = 1512 < mínimo 3000 → ajuste +1488
    codes = [(l["code"], l["amount_cents"]) for l in q["lines"]]
    assert ("PLAN_DISCOUNT", -336) in codes and ("COUPON", -1512) in codes
    assert q["min_price_adjustment_cents"] == 1488
    assert q["media_cents"] == 3000
    assert q["fees_cents"] == 15000 and q["total_cents"] == 18000
    with pytest.raises(PE.PricingError) as e:
        PE.compute_quote(BASE, inp(fees=("ART",)))
    assert e.value.code == "PRICE_TABLE_INCOMPLETE"


def test_fixed_coupon_never_below_zero():
    q = PE.compute_quote(BASE, inp(coupon=PE.CouponFact("c", "X", "FIXED", discount_cents=10**9)))
    assert q["media_cents"] == 0 and q["total_cents"] == 0


def test_margin_components_negative_and_below_min():
    costs = [
        it(30, "COST_GATEWAY", "PERCENT_BPS", 199), it(31, "COST_GATEWAY", "FIXED_CENTS", 50),
        it(32, "TAX", "PERCENT_BPS", 600, key="ISS"), it(33, "COST_SELLER", "PERCENT_BPS", 1000),
        it(34, "COST_SERVER", "PER_SCREEN_MONTH_CENTS", 300), it(35, "COST_EQUIPMENT", "PER_SCREEN_MONTH_CENTS", 600),
        it(36, "COST_DIRECT", "FIXED_CENTS", 100),
    ]
    q = PE.compute_quote(BASE + costs, inp(with_seller=True))
    m = q["margin"]
    R = 3360
    assert m["gateway_cents"] == R * 199 // 10000 + 50
    assert m["establishment_commission_cents"] == R * 500 // 10000  # pool 5% da receita de mídia
    assert m["seller_commission_cents"] == R * 1000 // 10000
    assert m["taxes_cents"] == R * 600 // 10000
    assert m["server_cents"] == round(300 * 7 / 30) and m["equipment_cents"] == round(600 * 7 / 30)
    assert m["margin_cents"] == R - m["total_costs_cents"]
    assert m["status"] == "OK" and m["margin_bps"] >= 4000
    # custo direto altíssimo → negativa
    neg = PE.compute_quote(BASE + costs + [it(37, "COST_DIRECT", "FIXED_CENTS", 10000)], inp(with_seller=True))
    assert neg["margin"]["status"] == "NEGATIVE" and neg["margin"]["margin_cents"] < 0
    # abaixo de 40% mas positiva
    low = PE.compute_quote(BASE + costs + [it(38, "COST_DIRECT", "PERCENT_BPS", 3500)], inp(with_seller=True))
    assert low["margin"]["status"] == "BELOW_MIN" and 0 <= low["margin"]["margin_bps"] < 4000


def test_quote_is_deterministic_regardless_of_order():
    items = BASE + [it(10, "SCREEN_COUNT", "MULTIPLIER_BPS", 9000, lo=2, hi=10)]
    a = PE.compute_quote(items, inp(screens=screens(3)))
    b = PE.compute_quote(list(reversed(items)), inp(screens=tuple(reversed(screens(3)))))
    assert a["quote_hash"] == b["quote_hash"] and a == b


def test_public_view_hides_costs_and_screen_weights():
    q = PE.compute_quote(BASE, inp())
    pv = PE.public_view(q)
    assert "margin" not in pv and "per_screen_weights" not in pv and "applied_item_ids" not in pv


def test_specific_time_and_per_day():
    q = PE.compute_quote(BASE, inp(freq_kind="SPECIFIC_TIME", freq_value=2, specific_times=("12:00", "18:00")))
    assert q["media_gross_cents"] == 2 * 7 * 10
    q = PE.compute_quote(BASE, inp(freq_kind="PER_DAY", freq_value=5))
    assert q["media_gross_cents"] == 5 * 7 * 10
    with pytest.raises(PE.PricingError):
        PE.compute_quote(BASE, inp(freq_kind="SPECIFIC_TIME", freq_value=1, specific_times=("25:00",)))
