"""W-INV — política de rotação e invariante âncora × bucket (ADR_006 §1; A6 §10.0, §10.9)."""
from __future__ import annotations

import pytest
from hypothesis import given, settings, strategies as st

from app.modules.scheduler.policy import PolicyError, RotationSpec, validate_rotation


def test_capacity_2_plus_2_is_60_units_per_hour():
    r = RotationSpec(120, 120)
    assert (r.cycle_s, r.cycles_per_hour, r.units_per_block, r.units_per_hour) == (240, 15, 4, 60)
    assert r.eligible(4) and r.eligible(2) and r.eligible(1)
    # intervalo de 10 min ⇒ gap 3 blocos ⇒ máx. 5/h por campanha (A6 §10.9)
    assert r.gap_blocks(600) == 3 and r.max_per_hour(600) == 5


def test_capacity_3_plus_1_is_30_units_and_120s_ineligible():
    r = RotationSpec(180, 60)
    assert (r.cycles_per_hour, r.units_per_block, r.units_per_hour) == (15, 2, 30)
    assert not r.eligible(4), "120 s não cabe em bloco externo de 60 s (nunca cortar)"
    assert r.eligible(2)


@pytest.mark.parametrize("i,e,k,units", [(60, 60, 30, 60), (120, 180, 12, 72), (90, 150, 15, 75)])
def test_examples_table(i, e, k, units):
    r = RotationSpec(i, e)
    assert r.cycles_per_hour == k and r.units_per_hour == units


@pytest.mark.parametrize("i,e", [(120, 150), (150, 120), (0, 120), (120, 0), (45, 75), (100, 140)])
def test_invalid_policies_rejected(i, e):
    with pytest.raises(PolicyError):
        RotationSpec(i, e)


def test_overflow_requires_flag_and_limit():
    with pytest.raises(PolicyError):
        validate_rotation(120, 120, False, 30)
    with pytest.raises(PolicyError):
        validate_rotation(120, 120, True, 150)
    validate_rotation(120, 120, True, 60)


VALID = [(i, e) for i in range(30, 3601, 30) for e in range(30, 3601, 30) if 3600 % (i + e) == 0]


@settings(max_examples=200, deadline=None)
@given(pol=st.sampled_from(VALID), anchor=st.integers(min_value=0, max_value=10**13),
       hour_start=st.integers(min_value=0, max_value=10**13))
def test_any_anchor_gives_k_external_starts_per_hour_and_one_per_window(pol, anchor, hour_start):
    """Para QUALQUER âncora: toda janela de 3600 s tem K inícios externos e toda janela C tem 1."""
    i, e = pol
    r = RotationSpec(i, e)
    c = r.cycle_ms
    off = (anchor + i * 1000) % c  # inícios externos = anchor + k·C + I ≡ off (mod C)

    def count(a: int, length: int) -> int:
        first = a + ((off - a) % c)
        return 0 if first >= a + length else (a + length - 1 - first) // c + 1

    assert count(hour_start, 3_600_000) == r.cycles_per_hour
    assert count(hour_start, c) == 1
