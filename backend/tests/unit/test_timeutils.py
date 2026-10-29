"""Fusos IANA e fim do dia local (P002 item 25)."""
from datetime import date, datetime, timezone

import pytest

from app.core import timeutils as tu


def test_validate_timezone():
    assert tu.validate_timezone("America/Sao_Paulo") == "America/Sao_Paulo"
    for bad in ("", "Brasil/Brasilia", "UTC+3", "America/Nowhere", None):
        with pytest.raises(ValueError):
            tu.validate_timezone(bad)


@pytest.mark.parametrize(
    "tz,expected_utc",
    [
        ("America/Sao_Paulo", datetime(2026, 10, 8, 2, 59, 59, 999999, tzinfo=timezone.utc)),  # UTC-3
        ("America/Manaus", datetime(2026, 10, 8, 3, 59, 59, 999999, tzinfo=timezone.utc)),  # UTC-4
        ("America/Rio_Branco", datetime(2026, 10, 8, 4, 59, 59, 999999, tzinfo=timezone.utc)),  # UTC-5
    ],
)
def test_end_of_local_day_in_utc(tz, expected_utc):
    assert tu.end_of_local_day_utc(date(2026, 10, 7), tz) == expected_utc


def test_day_turnover_local_vs_utc():
    # 02:30 UTC de 08/10 ainda é 07/10 em São Paulo (23:30) e em Rio Branco (21:30)
    instant = datetime(2026, 10, 8, 2, 30, tzinfo=timezone.utc)
    assert tu.local_date(instant, "America/Sao_Paulo") == date(2026, 10, 7)
    assert tu.local_date(instant, "America/Rio_Branco") == date(2026, 10, 7)
    # 03:30 UTC: já é 08/10 em SP (00:30), ainda 07/10 em Manaus (23:30)
    instant = datetime(2026, 10, 8, 3, 30, tzinfo=timezone.utc)
    assert tu.local_date(instant, "America/Sao_Paulo") == date(2026, 10, 8)
    assert tu.local_date(instant, "America/Manaus") == date(2026, 10, 7)


def test_day_bounds_and_contract_expiry():
    start, end = tu.local_day_bounds_utc(date(2026, 12, 31), "America/Sao_Paulo")
    assert start == datetime(2026, 12, 31, 3, 0, tzinfo=timezone.utc)
    assert end == datetime(2027, 1, 1, 3, 0, tzinfo=timezone.utc)
    eod = tu.end_of_local_day_utc(date(2026, 12, 31), "America/Sao_Paulo")
    assert start <= eod < end


def test_naive_rejected_and_isoformat_z():
    with pytest.raises(ValueError):
        tu.to_utc(datetime(2026, 1, 1))
    assert tu.isoformat_z(datetime(2026, 1, 1, 12, tzinfo=timezone.utc)) == "2026-01-01T12:00:00Z"
    assert tu.utcnow().tzinfo is not None
