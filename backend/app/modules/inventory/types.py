"""
RD Mídia Indoor — Tipos públicos do inventário (W-INV): padrão de contratação e resultados.

``PatternSpec.parse(d)`` aceita o dict que o W-CAMP envia (chaves de A6 §10.2):
``date_from``, ``date_to`` (datas LOCAIS), ``days_mask`` (1..127, bit0 = segunda),
``holiday_mode`` (só ``INCLUDE``), ``time_bands`` ([[ini_min, fim_min)] locais,
alinhadas à hora), ``duration_units`` (1|2|4), ``freq_kind``
(PER_HOUR|PER_DAY|SPECIFIC_TIME), ``freq_value``, ``specific_times`` (["HH:MM"]),
``min_interval_seconds``, ``priority`` (STANDARD|PREMIUM),
``allow_same_advertiser_consecutive``, ``exclusivity`` (NONE|COMPETITOR_EXCLUSIVE).
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any, Optional

from app.core.errors import DomainError
from app.modules.scheduler.packer import (
    FREQ_KINDS,
    FREQ_PER_DAY,
    FREQ_SPECIFIC,
    PRIORITIES,
    Line,
    fmt_hhmm,
    parse_hhmm,
    per_day_distribution,
)

EXCLUSIVITY_NONE = "NONE"
EXCLUSIVITY_COMPETITOR = "COMPETITOR_EXCLUSIVE"
EXCLUSIVITIES = (EXCLUSIVITY_NONE, EXCLUSIVITY_COMPETITOR)
MAX_PATTERN_DAYS = 400


def _bad(msg: str, **details: Any) -> DomainError:
    return DomainError("PATTERN_INVALID", msg, status=400, details=details)


def _date(v: Any, name: str) -> date:
    if isinstance(v, datetime):
        raise _bad(f"{name} deve ser uma data local (AAAA-MM-DD), não um instante.", field=name)
    if isinstance(v, date):
        return v
    try:
        return date.fromisoformat(str(v))
    except ValueError:
        raise _bad(f"{name} inválida.", field=name) from None


def _int(v: Any, name: str) -> int:
    if isinstance(v, bool) or not isinstance(v, int):
        raise _bad(f"{name} deve ser inteiro.", field=name)
    return v


@dataclass(frozen=True)
class PatternSpec:
    date_from: date
    date_to: date
    duration_units: int
    freq_kind: str
    freq_value: int
    days_mask: int = 127
    time_bands: tuple = ((0, 1440),)
    specific_times: tuple = ()
    min_interval_seconds: int = 0
    priority: str = "STANDARD"
    allow_same_advertiser_consecutive: bool = False
    exclusivity: str = EXCLUSIVITY_NONE
    holiday_mode: str = "INCLUDE"

    @classmethod
    def parse(cls, raw: Any) -> "PatternSpec":
        if isinstance(raw, PatternSpec):
            return raw
        if not isinstance(raw, dict):
            raise _bad("Padrão de contratação inválido.")
        d_from = _date(raw.get("date_from"), "date_from")
        d_to = _date(raw.get("date_to"), "date_to")
        if d_to < d_from:
            raise _bad("date_to anterior a date_from.")
        if (d_to - d_from).days + 1 > MAX_PATTERN_DAYS:
            raise _bad(f"Período máximo de {MAX_PATTERN_DAYS} dias por padrão.")
        du = raw.get("duration_units")
        if du is None and raw.get("duration_s") is not None:
            ds = _int(raw.get("duration_s"), "duration_s")
            if ds % 30:
                raise _bad("Duração deve ser 30, 60 ou 120 s.")
            du = ds // 30
        du = _int(du, "duration_units")
        if du not in (1, 2, 4):
            raise _bad("Duração deve ser 30, 60 ou 120 s (15/45/90 s proibidos).")
        fk = str(raw.get("freq_kind") or "")
        if fk not in FREQ_KINDS:
            raise _bad("freq_kind inválido.", allowed=list(FREQ_KINDS))
        days_mask = _int(raw.get("days_mask", 127), "days_mask")
        if not 1 <= days_mask <= 127:
            raise _bad("days_mask deve estar entre 1 e 127.")
        hm = raw.get("holiday_mode") or "INCLUDE"
        if hm != "INCLUDE":
            raise DomainError("PATTERN_UNSUPPORTED",
                              "Contratação por feriados ainda não é suportada (calendário de feriados pendente).",
                              status=400, details={"holiday_mode": hm})
        excl = raw.get("exclusivity") or EXCLUSIVITY_NONE
        if excl is False:
            excl = EXCLUSIVITY_NONE
        if excl not in EXCLUSIVITIES:
            raise DomainError("PATTERN_UNSUPPORTED", "Exclusividade inválida.", status=400,
                              details={"allowed": list(EXCLUSIVITIES)})
        prio = str(raw.get("priority") or "STANDARD")
        if prio not in PRIORITIES:
            raise _bad("priority inválida.", allowed=list(PRIORITIES))
        mi = _int(raw.get("min_interval_seconds", raw.get("min_interval_s", 0)) or 0, "min_interval_seconds")
        if not 0 <= mi <= 86400:
            raise _bad("min_interval_seconds fora de 0..86400.")
        bands_raw = raw.get("time_bands") or [[0, 1440]]
        bands: list[tuple[int, int]] = []
        for b in bands_raw:
            if not isinstance(b, (list, tuple)) or len(b) != 2:
                raise _bad("time_bands deve ser [[inicio_min, fim_min], …].")
            s, e = _int(b[0], "time_bands"), _int(b[1], "time_bands")
            if not (0 <= s < e <= 1440) or s % 60 or e % 60:
                raise _bad("Faixas horárias devem estar alinhadas à hora, dentro de 00:00–24:00.")
            bands.append((s, e))
        bands.sort()
        for (s1, e1), (s2, e2) in zip(bands, bands[1:]):
            if s2 < e1:
                raise _bad("Faixas horárias sobrepostas.")
        specific: tuple = ()
        fv = raw.get("freq_value")
        if fk == FREQ_SPECIFIC:
            st = raw.get("specific_times") or []
            if not isinstance(st, (list, tuple)) or not st:
                raise _bad("specific_times obrigatório para SPECIFIC_TIME.")
            try:
                mins = sorted({parse_hhmm(x) for x in st})
            except (ValueError, TypeError):
                raise _bad("Horário específico inválido (use HH:MM).") from None
            if len(mins) != len(st):
                raise _bad("Horários específicos repetidos.")
            specific = tuple(fmt_hhmm(m) for m in mins)
            if fv is None:
                fv = len(specific)
        fv = _int(fv, "freq_value")
        if fv <= 0 or fv > 1440:
            raise _bad("freq_value deve ser positivo.")
        if fk == FREQ_SPECIFIC and fv != len(specific):
            raise _bad("freq_value deve ser igual à quantidade de horários específicos.")
        return cls(
            date_from=d_from, date_to=d_to, duration_units=du, freq_kind=fk, freq_value=fv, days_mask=days_mask,
            time_bands=tuple(bands), specific_times=specific, min_interval_seconds=mi, priority=prio,
            allow_same_advertiser_consecutive=bool(raw.get("allow_same_advertiser_consecutive", False)),
            exclusivity=excl, holiday_mode="INCLUDE",
        )

    def canonical(self) -> dict:
        return {
            "date_from": self.date_from.isoformat(), "date_to": self.date_to.isoformat(),
            "days_mask": self.days_mask, "time_bands": [list(b) for b in self.time_bands],
            "duration_units": self.duration_units, "freq_kind": self.freq_kind, "freq_value": self.freq_value,
            "specific_times": list(self.specific_times), "min_interval_seconds": self.min_interval_seconds,
            "priority": self.priority, "allow_same_advertiser_consecutive": self.allow_same_advertiser_consecutive,
            "exclusivity": self.exclusivity, "holiday_mode": self.holiday_mode,
        }

    def distribution(self) -> Optional[dict]:
        if self.freq_kind != FREQ_PER_DAY:
            return None
        return per_day_distribution(self.freq_value, self.time_bands)

    def to_line(self, line_id: str, campaign_id: str, advertiser_id: str,
                distribution: Optional[dict] = None) -> Line:
        dist = distribution if distribution is not None else (self.distribution() or {})
        return Line(
            line_id=line_id, campaign_id=campaign_id, advertiser_id=advertiser_id,
            duration_units=self.duration_units, freq_kind=self.freq_kind, freq_value=self.freq_value,
            date_from=self.date_from, date_to=self.date_to, days_mask=self.days_mask,
            time_bands=tuple(tuple(b) for b in self.time_bands),
            specific_minutes=tuple(sorted(parse_hhmm(x) for x in self.specific_times)),
            min_interval_s=self.min_interval_seconds, priority=self.priority,
            allow_same_advertiser=self.allow_same_advertiser_consecutive,
            distribution=tuple(sorted((int(h), int(c)) for h, c in dist.items())),
        )


def line_from_row(bp: Any, screen_campaign_id: Optional[str] = None) -> Line:
    """``Line`` a partir de uma linha ``booking_patterns`` (registro congelado)."""
    dist = bp.per_day_distribution or {}
    return Line(
        line_id=str(bp.id), campaign_id=screen_campaign_id or str(bp.campaign_id),
        advertiser_id=str(bp.advertiser_org_id), duration_units=int(bp.duration_units),
        freq_kind=bp.freq_kind, freq_value=int(bp.freq_value), date_from=bp.date_from, date_to=bp.date_to,
        days_mask=int(bp.days_mask), time_bands=tuple(tuple(int(x) for x in b) for b in (bp.time_bands or [])),
        specific_minutes=tuple(sorted(parse_hhmm(x) for x in (bp.specific_times or []))),
        min_interval_s=int(bp.min_interval_seconds), priority=bp.priority,
        allow_same_advertiser=bool(bp.allow_same_advertiser_consecutive),
        distribution=tuple(sorted((int(h), int(c)) for h, c in dist.items())),
    )


@dataclass
class ScreenQuote:
    screen_id: uuid.UUID
    eligible: bool
    feasible: bool
    reason: Optional[str] = None
    message: Optional[str] = None
    details: dict = field(default_factory=dict)
    conflict_decision_id: Optional[uuid.UUID] = None
    units_per_hour: int = 0
    demand_units_per_hour: int = 0

    def to_dict(self, *, public: bool = False) -> dict:
        d = {
            "screen_id": str(self.screen_id),
            "eligible": self.eligible,
            "feasible": self.feasible,
            "reason": self.reason,
            "message": self.message,
            "units_per_hour": self.units_per_hour,
            "demand_units_per_hour": self.demand_units_per_hour,
        }
        if not public:
            d["details"] = self.details
        return d


@dataclass
class CapacityResult:
    feasible: bool
    screens: list[ScreenQuote]

    @property
    def available_screen_ids(self) -> list[uuid.UUID]:
        return [s.screen_id for s in self.screens if s.eligible and s.feasible]

    def to_dict(self, *, public: bool = False) -> dict:
        avail = self.available_screen_ids
        return {
            "feasible": self.feasible,
            "requested": len(self.screens),
            "available": len(avail),
            "screens": [s.to_dict(public=public) for s in self.screens],
        }


@dataclass
class HoldResult:
    hold_id: uuid.UUID
    status: str
    already_processed: bool = False
    refund_required: bool = False
    screen_ids: list = field(default_factory=list)
    reason: Optional[str] = None

    def to_dict(self) -> dict:
        return {
            "hold_id": str(self.hold_id),
            "status": self.status,
            "already_processed": self.already_processed,
            "refund_required": self.refund_required,
            "screen_ids": [str(s) for s in self.screen_ids],
            "reason": self.reason,
        }
