"""Algoritmo de comissão (A5 D2.4–D2.8): reproduz a tabela D2.6 (R$ 50; pool 5 % = 250 c),
modelos A e B, pesos, exceções 3 %/7 %, PoP 90/100, tela que não tocou, Σ ≤ pool sempre
(Hypothesis) e nunca 30 %."""
from fractions import Fraction

import pytest
from hypothesis import given, settings as hsettings, strategies as st

from app.modules.financial.commission import (
    MODEL_A,
    MODEL_B,
    UC,
    ScreenTerms,
    accrued_for,
    compute_shares,
    models_explanation,
)

R = 5_000
POOL = 250


def paid(model, terms, executed=None, pool=POOL, pool_bps=500):
    """Centavos por tela após a apropriação e o floor por estabelecimento (1 tela/estabelecimento)."""
    executed = executed or {}
    shares = compute_shares(model, pool, pool_bps, terms)
    return [accrued_for(shares[t.screen_id], executed.get(t.screen_id, t.expected_total), t.expected_total) // UC
            for t in terms]


def eq(n, **kw):
    return [ScreenTerms(f"t{i}", expected_total=100, **kw) for i in range(n)]


def with_attr(terms, cents_each=None):
    n = len(terms)
    return [ScreenTerms(t.screen_id, t.weight_milli, t.rate_bps, t.receives,
                        cents_each if cents_each is not None else R // n, t.expected_total) for t in terms]


@pytest.mark.parametrize("model", [MODEL_A, MODEL_B])
def test_d26_one_screen_full_and_90_of_100(model):
    t = with_attr(eq(1))
    assert paid(model, t) == [250]
    assert paid(model, t, {"t0": 90}) == [225]  # resíduo RD 25


@pytest.mark.parametrize("model", [MODEL_A, MODEL_B])
def test_d26_seven_ten_hundred_screens(model):
    p7 = paid(model, with_attr(eq(7)))
    assert p7 == [35] * 7 and POOL - sum(p7) == 5
    p10 = paid(model, with_attr(eq(10)))
    assert p10 == [25] * 10 and POOL - sum(p10) == 0
    p10b = paid(model, with_attr(eq(10)), {"t0": 90})
    assert sorted(p10b) == [22] + [25] * 9 and sum(p10b) == 247
    p100 = paid(model, with_attr(eq(100)))
    assert p100 == [2] * 100 and POOL - sum(p100) == 50


def _mixed():
    return ([ScreenTerms(f"p{i}", rate_bps=700, expected_total=100) for i in range(2)]
            + [ScreenTerms("m0", rate_bps=300, expected_total=100)]
            + [ScreenTerms(f"s{i}", expected_total=100) for i in range(7)])


def test_d26_exceptions_7_and_3_percent_model_a_weights():
    p = paid(MODEL_A, _mixed())
    assert p == [33, 33, 14] + [24] * 7 and sum(p) == 248 and POOL - sum(p) == 2


def test_d26_exceptions_model_b_scaled_to_pool():
    p = paid(MODEL_B, with_attr(_mixed()))
    assert p == [33, 33, 14] + [24] * 7 and sum(p) == 248  # nominal 260 escalado para 250


def test_d26_model_b_two_3pct_eight_5pct_below_pool():
    terms = with_attr([ScreenTerms(f"x{i}", rate_bps=300, expected_total=100) for i in range(2)]
                      + [ScreenTerms(f"y{i}", expected_total=100) for i in range(8)])
    p = paid(MODEL_B, terms)
    assert p == [15, 15] + [25] * 8 and POOL - sum(p) == 20


def test_d26_shopping_and_gyms():
    venues = ([ScreenTerms(f"shop{i}", attributed_cents=1000, expected_total=100) for i in range(2)]
              + [ScreenTerms(f"gym{i}", attributed_cents=375, expected_total=100) for i in range(8)])
    assert paid(MODEL_A, venues) == [25] * 10
    pb = paid(MODEL_B, venues)
    assert pb == [50, 50] + [18] * 8 and POOL - sum(pb) == 6


def test_weights_are_configurable_and_screen_that_never_played_gets_zero():
    terms = [ScreenTerms("a", weight_milli=2000, expected_total=10), ScreenTerms("b", expected_total=10),
             ScreenTerms("c", expected_total=10)]
    p = paid(MODEL_A, terms, {"c": 0})
    assert p == [125, 62, 0]  # w=2,1,1 → 125, 62,5, 0 (não tocou)
    assert POOL - sum(p) == 63


def test_establishment_without_commission_share_stays_with_rd():
    terms = [ScreenTerms("a", expected_total=10), ScreenTerms("b", receives=False, expected_total=10)]
    assert paid(MODEL_A, terms) == [125, 0]


def test_cumulative_floor_makes_full_execution_exact_over_many_days():
    share = compute_shares(MODEL_A, POOL, 500, eq(10))["t0"]
    # 30 dias com 1 exibição/dia: o acumulado no fim é exatamente o share
    total = 0
    for day in range(1, 31):
        total = accrued_for(share, day, 30)
    assert total == share == 25 * UC


def test_never_thirty_percent():
    p = paid(MODEL_A, with_attr(eq(7)))
    assert sum(p) <= R * 500 // 10_000
    assert sum(p) * 10_000 // R < 3000


@hsettings(max_examples=300, deadline=None)
@given(
    revenue=st.integers(min_value=1, max_value=10**9),
    pool_bps=st.integers(min_value=0, max_value=10_000),
    model=st.sampled_from([MODEL_A, MODEL_B]),
    screens=st.lists(st.tuples(st.integers(0, 5000), st.integers(0, 10_000), st.booleans(),
                               st.integers(0, 10**7), st.integers(0, 500), st.integers(0, 600)), min_size=1, max_size=40),
)
def test_property_sum_never_exceeds_pool(revenue, pool_bps, model, screens):
    pool = revenue * pool_bps // 10_000
    terms = [ScreenTerms(i, w, r, rec, a, e) for i, (w, r, rec, a, e, _) in enumerate(screens)]
    shares = compute_shares(model, pool, pool_bps, terms)
    assert all(v >= 0 for v in shares.values())
    assert sum(shares.values()) <= pool * UC
    paid_cents = sum(accrued_for(shares[t.screen_id], v, t.expected_total) // UC
                     for t, (*_, v) in zip(terms, screens))
    assert paid_cents <= pool
    assert pool - paid_cents >= 0  # resíduo da RD nunca negativo


def test_models_explanation_uses_same_algorithm():
    ex = models_explanation(500, MODEL_A)
    by = {e["scenario"]: e for e in ex["examples"]}
    assert by["7 telas, peso 1,0"]["model_a"]["per_screen_cents"] == [35] * 7
    assert by["7 telas, peso 1,0"]["model_a"]["rd_residue_cents"] == 5
    assert by["100 telas, peso 1,0"]["model_a"]["rd_residue_cents"] == 50
    assert by["2 telas de shopping (R$ 10 cada) + 8 academias (R$ 3,75 cada)"]["model_b"]["total_cents"] == 244
    assert set(ex["models"]) == {MODEL_A, MODEL_B}
    assert all(e["model_a"]["total_cents"] <= e["model_a"]["pool_cents"] for e in ex["examples"])
    assert Fraction(ex["pool_bps"], 10_000) == Fraction(1, 20)
