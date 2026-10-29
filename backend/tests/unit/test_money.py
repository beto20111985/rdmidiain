"""Dinheiro em centavos inteiros, basis points e rateio determinístico."""
from decimal import Decimal

import pytest
from hypothesis import given
from hypothesis import strategies as st

from app.core import money


def test_rejects_float_and_bool():
    with pytest.raises(TypeError):
        money.ensure_cents(10.5)
    with pytest.raises(TypeError):
        money.ensure_cents(True)
    with pytest.raises(TypeError):
        money.apply_bps(1000, 5.0)
    with pytest.raises(TypeError):
        money.reais_to_cents(10.0)
    with pytest.raises(money.MoneyError):
        money.ensure_cents(-1)


def test_apply_bps_floor():
    assert money.apply_bps(10_000, 500) == 500  # 5% de R$ 100
    assert money.apply_bps(999, 500) == 49  # 49,95 → 49 (floor)
    assert money.split_bps(999, 500) == (49, 950)
    with pytest.raises(money.MoneyError):
        money.apply_bps(100, 10_001)


def test_allocate_floor_with_explicit_residual():
    a = money.allocate(100, {"a": 1, "b": 1, "c": 1})
    assert a.shares == {"a": 33, "b": 33, "c": 33}
    assert a.residual_cents == 1
    a = money.allocate(500, [("x", 0), ("y", 0)])
    assert a.shares == {"x": 0, "y": 0} and a.residual_cents == 500


@given(
    total=st.integers(min_value=0, max_value=10**12),
    weights=st.lists(st.integers(min_value=0, max_value=10**6), min_size=1, max_size=120),
)
def test_allocate_never_exceeds_total_and_is_deterministic(total, weights):
    w = {i: x for i, x in enumerate(weights)}
    a = money.allocate(total, w)
    assert sum(a.shares.values()) + a.residual_cents == total
    assert a.residual_cents >= 0
    nonzero = len([x for x in weights if x])
    if nonzero:
        # cada floor perde < 1 centavo → resíduo ≤ (nº de pesos não nulos − 1)
        assert a.residual_cents <= nonzero - 1
    assert money.allocate(total, dict(reversed(list(w.items())))).shares == a.shares


def test_reais_conversion():
    assert money.cents_to_reais(12345) == Decimal("123.45")
    assert money.cents_to_reais(5) == Decimal("0.05")
    assert money.reais_to_cents(Decimal("123.45")) == 12345
    assert money.reais_to_cents("10,50") == 1050
    assert money.reais_to_cents("7") == 700
    with pytest.raises(money.MoneyError):
        money.reais_to_cents("1.234")
    with pytest.raises(money.MoneyError):
        money.reais_to_cents("abc")


@given(st.integers(min_value=-10**12, max_value=10**12))
def test_roundtrip(cents):
    assert money.reais_to_cents(money.cents_to_reais(cents)) == cents


def test_format_brl():
    assert money.format_brl(123456) == "R$ 1.234,56"
    assert money.format_brl(-5) == "-R$ 0,05"
