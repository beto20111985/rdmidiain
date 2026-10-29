"""
RD Mídia Indoor — Tempo e fusos (P002 item 25; contrato §1).

Regras:
- Banco e API em UTC (``timestamptz``; respostas ISO-8601 com ``Z``).
- Cada estabelecimento tem um timezone IANA (padrão ``America/Sao_Paulo``).
- Datas de negócio ("vence no dia X") são interpretadas no fuso do
  estabelecimento: fim do dia local = 23:59:59.999999 local → convertido para UTC.

Funções:
- ``utcnow()``                     → datetime aware em UTC
- ``validate_timezone(name)``       → nome IANA canônico ou ``ValueError``
- ``to_utc(dt)`` / ``to_local(dt, tz)``
- ``local_to_utc(naive, tz)``       → resolve horário local ingênuo (DST: fold=0)
- ``start_of_local_day_utc(d, tz)`` / ``end_of_local_day_utc(d, tz)``
- ``local_date(dt_utc, tz)``        → data local de um instante
- ``isoformat_z(dt)``               → ``2026-10-07T12:00:00Z``
"""
from __future__ import annotations

from datetime import date, datetime, time, timedelta, timezone
from functools import lru_cache
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError, available_timezones

DEFAULT_TIMEZONE = "America/Sao_Paulo"
UTC = timezone.utc
END_OF_DAY = time(23, 59, 59, 999999)


def utcnow() -> datetime:
    return datetime.now(UTC)


@lru_cache(maxsize=1)
def _available() -> frozenset[str]:
    return frozenset(available_timezones())


@lru_cache(maxsize=512)
def get_zone(name: str) -> ZoneInfo:
    return ZoneInfo(validate_timezone(name))


def validate_timezone(name: str) -> str:
    if not isinstance(name, str) or not name or len(name) > 64:
        raise ValueError("Timezone inválido")
    if name not in _available():
        raise ValueError(f"Timezone IANA desconhecido: {name}")
    try:
        ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError):
        raise ValueError(f"Timezone IANA desconhecido: {name}") from None
    return name


def ensure_aware(dt: datetime) -> datetime:
    if dt.tzinfo is None or dt.tzinfo.utcoffset(dt) is None:
        raise ValueError("datetime sem timezone (naive) não é aceito")
    return dt


def to_utc(dt: datetime) -> datetime:
    return ensure_aware(dt).astimezone(UTC)


def to_local(dt: datetime, tz: str) -> datetime:
    return ensure_aware(dt).astimezone(get_zone(tz))


def local_to_utc(naive_local: datetime, tz: str) -> datetime:
    if naive_local.tzinfo is not None:
        raise ValueError("esperado datetime local sem tzinfo")
    return naive_local.replace(tzinfo=get_zone(tz)).astimezone(UTC)


def start_of_local_day_utc(d: date, tz: str) -> datetime:
    return local_to_utc(datetime.combine(d, time(0, 0)), tz)


def end_of_local_day_utc(d: date, tz: str) -> datetime:
    """23:59:59.999999 do dia ``d`` no fuso ``tz``, em UTC."""
    return local_to_utc(datetime.combine(d, END_OF_DAY), tz)


def local_date(dt: datetime, tz: str) -> date:
    return to_local(dt, tz).date()


def local_day_bounds_utc(d: date, tz: str) -> tuple[datetime, datetime]:
    """[início, início do dia seguinte) em UTC — use para filtros por dia local."""
    start = start_of_local_day_utc(d, tz)
    end = start_of_local_day_utc(d + timedelta(days=1), tz)
    return start, end


def isoformat_z(dt: datetime) -> str:
    return to_utc(dt).isoformat().replace("+00:00", "Z")
