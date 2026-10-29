"""W-INV — validação do padrão de contratação (PatternSpec) recebido do checkout."""
from __future__ import annotations

from datetime import date

import pytest

from app.core.errors import DomainError
from app.modules.inventory.types import PatternSpec

BASE = {"date_from": "2026-11-01", "date_to": "2026-11-30", "duration_units": 1, "freq_kind": "PER_HOUR",
        "freq_value": 2}


def P(**kw):
    return PatternSpec.parse({**BASE, **kw})


def test_valid_defaults():
    p = P()
    assert (p.days_mask, p.time_bands, p.exclusivity, p.holiday_mode) == (127, ((0, 1440),), "NONE", "INCLUDE")
    assert p.date_from == date(2026, 11, 1)


@pytest.mark.parametrize("bad", [{"duration_units": 3}, {"duration_s": 45}, {"duration_s": 15}, {"duration_s": 90}])
def test_forbidden_durations(bad):
    raw = {**BASE, **bad}
    if "duration_s" in bad:
        raw.pop("duration_units")
    with pytest.raises(DomainError) as ei:
        PatternSpec.parse(raw)
    assert ei.value.code == "PATTERN_INVALID"


def test_duration_seconds_alias():
    raw = {k: v for k, v in BASE.items() if k != "duration_units"}
    assert PatternSpec.parse({**raw, "duration_s": 120}).duration_units == 4


@pytest.mark.parametrize("kw,code", [
    ({"date_to": "2026-10-01"}, "PATTERN_INVALID"),
    ({"date_from": "2026-11-01T10:00:00Z"}, "PATTERN_INVALID"),
    ({"days_mask": 0}, "PATTERN_INVALID"),
    ({"days_mask": 128}, "PATTERN_INVALID"),
    ({"time_bands": [[600, 630]]}, "PATTERN_INVALID"),
    ({"time_bands": [[600, 900], [840, 960]]}, "PATTERN_INVALID"),
    ({"freq_kind": "PER_WEEK"}, "PATTERN_INVALID"),
    ({"priority": "VIP"}, "PATTERN_INVALID"),
    ({"holiday_mode": "EXCLUDE"}, "PATTERN_UNSUPPORTED"),
    ({"exclusivity": "TOTAL"}, "PATTERN_UNSUPPORTED"),
    ({"freq_kind": "SPECIFIC_TIME", "specific_times": ["12:00", "12:00"], "freq_value": 2}, "PATTERN_INVALID"),
    ({"freq_kind": "SPECIFIC_TIME", "specific_times": ["25:00"], "freq_value": 1}, "PATTERN_INVALID"),
    ({"freq_kind": "SPECIFIC_TIME", "specific_times": ["12:00"], "freq_value": 2}, "PATTERN_INVALID"),
    ({"date_from": "2026-01-01", "date_to": "2027-12-31"}, "PATTERN_INVALID"),
])
def test_invalid_patterns(kw, code):
    with pytest.raises(DomainError) as ei:
        P(**kw)
    assert ei.value.code == code


def test_per_day_distribution_registered_and_specific_normalized():
    p = P(freq_kind="PER_DAY", freq_value=3, time_bands=[[480, 1320]])
    assert sum(p.distribution().values()) == 3
    s = P(freq_kind="SPECIFIC_TIME", specific_times=["18:30", "09:05"], freq_value=2)
    assert s.specific_times == ("09:05", "18:30")
    ln = s.to_line("l", "c", "a")
    assert ln.specific_minutes == (9 * 60 + 5, 18 * 60 + 30)
