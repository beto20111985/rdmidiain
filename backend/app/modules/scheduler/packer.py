"""
RD Mídia Indoor — Empacotador DETERMINÍSTICO único (venda e manifesto). Módulo PURO.

Fonte normativa: ADR_006 §7 e A6 §10.8 (simulação de referência
``stack/scripts/002/a6_capacity_model_sim.py``, reproduzida em
``tests/unit/test_sched_packer.py``).

Modelo
------
- Uma tela tem, por dia LOCAL, uma sequência de *buckets* UTC alinhados
  (``b = floor(t/C)·C``); cada bucket contém exatamente um início de bloco externo
  (qualquer âncora), com ``E`` unidades de 30 s. Os buckets são agrupados por hora
  local (fusos do Brasil têm offset inteiro ⇒ K buckets por hora).
- Uma *linha* (``Line``) é um padrão contratado numa tela (booking_patterns ×
  booking_screens). Ela é expandida, por dia, em *fichas* de ``du`` unidades:
  ``PER_HOUR n`` ⇒ n fichas em cada hora da faixa; ``PER_DAY n`` ⇒ distribuição
  Bresenham REGISTRADA no padrão (``per_day_distribution``); ``SPECIFIC_TIME T`` ⇒
  1 ficha fixa no bucket que contém T (precisão contratual ±1 ciclo).
- Ordem de colocação: horário específico → PREMIUM → maior duração → maior
  intervalo → maior frequência → ``line_id`` (desempate estável). O dia é
  processado hora a hora (cronológico) e, dentro da hora, linha a linha.
- Posição ideal da i-ésima ficha: ``floor((i + φ)·K_h / n) mod K_h`` com
  ``φ = hash(campaign_id, screen_id) ∈ [0,1)`` (aritmética inteira); candidatos por
  distância (p, p+1, p−1, …).
- Aceita o bucket se: unidades livres ≥ du; distância a outras fichas da MESMA
  linha ≥ ``gap`` medida CIRCULARMENTE no dia (vale entre horas e, como dias com as
  mesmas linhas têm o mesmo plano, também na virada da meia-noite); NENHUM
  concorrente no bloco; existe ordenação interna sem mesmo anunciante adjacente
  (salvo se as duas linhas contrataram) — a permutação lexicograficamente menor.
- Cada dia local é função PURA das suas entradas (estrutura do dia, linhas ativas,
  concorrentes) ⇒ venda (hold) e manifesto produzem exatamente o mesmo plano para o
  mesmo dia, e dias repetidos são memoizados. Limitação documentada: na virada de um
  dia para outro com CONJUNTO DE LINHAS diferente, o intervalo mínimo de uma linha
  presente nos dois dias é garantido dentro de cada dia (não entre eles).
- Sobra de cada bloco ⇒ conteúdo interno (preenchido pelo gerador) ⇒ fallback.
- Modo ``strict`` (venda): qualquer ficha sem posição ⇒ ``PackingError``.
  Modo best-effort (manifesto): fichas sem posição vão para ``unplaced`` (alerta).
- Determinismo: o resultado não depende da ordem de entrada (``plan_checksum``).
"""
from __future__ import annotations

import bisect
import hashlib
import json
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta, timezone
from typing import Iterable, Optional, Sequence
from zoneinfo import ZoneInfo

from app.modules.scheduler.policy import ALLOWED_DURATION_UNITS, UNIT_S, RotationSpec

FREQ_PER_HOUR = "PER_HOUR"
FREQ_PER_DAY = "PER_DAY"
FREQ_SPECIFIC = "SPECIFIC_TIME"
FREQ_KINDS = (FREQ_PER_HOUR, FREQ_PER_DAY, FREQ_SPECIFIC)
PRIORITY_STANDARD = "STANDARD"
PRIORITY_PREMIUM = "PREMIUM"
PRIORITIES = (PRIORITY_STANDARD, PRIORITY_PREMIUM)

_PHI_SCALE = 1 << 32
_ARRANGE_BUDGET = 20000  # nós da busca de ordenação por bloco (E ≤ 8 ⇒ trivial)


class PackingError(Exception):
    """Ficha sem posição viável (modo strict) — a venda deve ser recusada."""

    def __init__(self, code: str, message: str, details: Optional[dict] = None) -> None:
        self.code = code
        self.message = message
        self.details = dict(details or {})
        super().__init__(f"{code}: {message}")


# ─── Utilidades ──────────────────────────────────────────────────────────────

def phase_int(campaign_id: str, screen_id: str) -> int:
    """φ ∈ [0, 2^32) — espalha campanhas entre telas (determinístico)."""
    h = hashlib.sha256(f"{campaign_id}|{screen_id}".encode()).digest()
    return int.from_bytes(h[:4], "big")


def bresenham_counts(n: int, slots: int) -> list[int]:
    """Distribui ``n`` fichas em ``slots`` horas (centrado, inteiro, soma exata)."""
    if slots <= 0:
        return []
    half = slots // 2
    return [((i + 1) * n + half) // slots - (i * n + half) // slots for i in range(slots)]


def band_hours(time_bands: Sequence[Sequence[int]]) -> list[int]:
    """Horas locais (0–23) cobertas pelas faixas (faixas em minutos, alinhadas à hora)."""
    hours: set[int] = set()
    for s, e in time_bands:
        for h in range(s // 60, e // 60):
            hours.add(h)
    return sorted(hours)


def per_day_distribution(freq_value: int, time_bands: Sequence[Sequence[int]]) -> dict[str, int]:
    """Distribuição Bresenham REGISTRADA no padrão PER_DAY: {"hora_local": fichas}."""
    hours = band_hours(time_bands)
    counts = bresenham_counts(freq_value, len(hours))
    return {str(h): c for h, c in zip(hours, counts) if c > 0}


def parse_hhmm(value: str) -> int:
    hh, _, mm = str(value).partition(":")
    h, m = int(hh), int(mm)
    if not (0 <= h <= 23 and 0 <= m <= 59) or len(mm) != 2:
        raise ValueError(f"horário inválido: {value!r}")
    return h * 60 + m


def fmt_hhmm(minute: int) -> str:
    return f"{minute // 60:02d}:{minute % 60:02d}"


# ─── Linhas (padrões contratados numa tela) ──────────────────────────────────

@dataclass(frozen=True)
class Line:
    line_id: str  # booking_pattern.id (estável)
    campaign_id: str
    advertiser_id: str
    duration_units: int
    freq_kind: str
    freq_value: int
    date_from: date
    date_to: date
    days_mask: int = 127  # bit0 = segunda … bit6 = domingo
    time_bands: tuple = ((0, 1440),)  # minutos locais [ini, fim)
    specific_minutes: tuple = ()  # minutos locais (SPECIFIC_TIME)
    min_interval_s: int = 0
    priority: str = PRIORITY_STANDARD
    allow_same_advertiser: bool = False
    distribution: tuple = ()  # PER_DAY: ((hora, fichas), …) registrado

    def active_on(self, d: date) -> bool:
        return self.date_from <= d <= self.date_to and bool(self.days_mask & (1 << d.weekday()))

    def in_band(self, minute_of_day: int) -> bool:
        return any(s <= minute_of_day < e for s, e in self.time_bands)

    def sort_key(self) -> tuple:
        return (
            0 if self.freq_kind == FREQ_SPECIFIC else 1,
            0 if self.priority == PRIORITY_PREMIUM else 1,
            -self.duration_units,
            -self.min_interval_s,
            -self.freq_value,
            self.line_id,
        )

    def memo_key(self) -> tuple:
        return (
            self.line_id, self.campaign_id, self.advertiser_id, self.duration_units, self.freq_kind,
            self.freq_value, self.time_bands, self.specific_minutes, self.min_interval_s, self.priority,
            self.allow_same_advertiser, self.distribution,
        )


def validate_line(line: Line, rotation: RotationSpec) -> None:
    """Validação no ato (antes de empacotar): elegibilidade e frequência × intervalo."""
    if line.duration_units not in ALLOWED_DURATION_UNITS:
        raise PackingError("DURATION_INVALID", "Duração deve ser 30, 60 ou 120 s.", {"line_id": line.line_id})
    if not rotation.eligible(line.duration_units):
        raise PackingError(
            "DURATION_NOT_ELIGIBLE",
            f"Anúncio de {line.duration_units * UNIT_S} s não cabe no bloco externo de "
            f"{rotation.external_s} s desta tela (nunca cortar).",
            {"line_id": line.line_id, "external_seconds": rotation.external_s},
        )
    max_h = rotation.max_per_hour(line.min_interval_s)
    if line.freq_kind == FREQ_PER_HOUR:
        if line.freq_value > max_h:
            raise PackingError(
                "FREQUENCY_INCOMPATIBLE",
                f"Frequência {line.freq_value}/h incompatível com intervalo mínimo de "
                f"{line.min_interval_s} s (máximo {max_h}/h nesta tela).",
                {"line_id": line.line_id, "max_per_hour": max_h},
            )
    elif line.freq_kind == FREQ_PER_DAY:
        dist = dict(line.distribution)
        worst = max(dist.values()) if dist else 0
        if sum(dist.values()) != line.freq_value:
            raise PackingError("FREQUENCY_INCOMPATIBLE", "Distribuição diária não cobre a frequência contratada.",
                               {"line_id": line.line_id})
        if worst > max_h:
            raise PackingError(
                "FREQUENCY_INCOMPATIBLE",
                f"{line.freq_value}/dia na faixa contratada exige {worst}/h; máximo {max_h}/h com o intervalo.",
                {"line_id": line.line_id, "max_per_hour": max_h},
            )
    elif line.freq_kind == FREQ_SPECIFIC:
        if len(line.specific_minutes) != line.freq_value:
            raise PackingError("FREQUENCY_INCOMPATIBLE", "Quantidade de horários específicos ≠ frequência.",
                               {"line_id": line.line_id})
        mins = sorted(line.specific_minutes)
        if len(set(mins)) != len(mins):
            raise PackingError("FREQUENCY_INCOMPATIBLE", "Horários específicos repetidos.", {"line_id": line.line_id})
        need = rotation.gap_blocks(line.min_interval_s) * rotation.cycle_s
        circular = list(zip(mins, mins[1:])) + ([(mins[-1], mins[0] + 1440)] if len(mins) > 1 else [])
        for a, b in circular:
            if (b - a) * 60 < need:
                raise PackingError(
                    "FREQUENCY_INCOMPATIBLE",
                    f"Horários {fmt_hhmm(a)} e {fmt_hhmm(b % 1440)} ficam mais próximos que o intervalo mínimo "
                    "permite (inclusive na virada do dia).",
                    {"line_id": line.line_id},
                )
    else:
        raise PackingError("FREQUENCY_INVALID", "Tipo de frequência inválido.", {"line_id": line.line_id})


# ─── Estrutura do dia local ──────────────────────────────────────────────────

@dataclass(frozen=True)
class DayFrame:
    local_date: date
    rotation: RotationSpec
    tz_name: str
    buckets: tuple  # inícios UTC em ms (crescente)
    minutes: tuple  # minuto local do início de cada bucket
    groups: tuple  # ((hora_local, primeiro_idx, ultimo_idx_exclusivo), …) em ordem cronológica
    struct_key: tuple = ()  # identifica a ESTRUTURA (independe da data) — memoização

    def signature(self) -> tuple:
        return self.struct_key or (self.rotation.key(), self.minutes, self.groups)


def _local_midnight_utc(d: date, tz: ZoneInfo) -> datetime:
    return datetime.combine(d, time(0, 0)).replace(tzinfo=tz).astimezone(timezone.utc)


_STRUCT_CACHE: dict = {}


def day_frame(local_date: date, tz_name: str, rotation: RotationSpec) -> DayFrame:
    tz = ZoneInfo(tz_name)
    start = _local_midnight_utc(local_date, tz)
    end = _local_midnight_utc(local_date + timedelta(days=1), tz)
    c_ms = rotation.cycle_ms
    s_ms = int(start.timestamp() * 1000)
    e_ms = int(end.timestamp() * 1000)
    first = -(-s_ms // c_ms) * c_ms
    buckets = tuple(range(first, e_ms, c_ms))
    off_start = start.astimezone(tz).utcoffset()
    off_end = (end - timedelta(milliseconds=1)).astimezone(tz).utcoffset()
    regular = off_start == off_end and e_ms - s_ms == 86_400_000
    if regular:
        skey = ("R", rotation.key(), first - s_ms, len(buckets))
        cached = _STRUCT_CACHE.get(skey)
        if cached is None:
            minutes = tuple(((first - s_ms) + i * c_ms) // 60000 for i in range(len(buckets)))
            groups: list[list] = []
            for i, m in enumerate(minutes):
                h = m // 60
                if groups and groups[-1][0] == h:
                    groups[-1][2] = i + 1
                else:
                    groups.append([h, i, i + 1])
            cached = (minutes, tuple(tuple(g) for g in groups))
            _STRUCT_CACHE[skey] = cached
        return DayFrame(local_date, rotation, tz_name, buckets, cached[0], cached[1], skey)
    # dia com transição de horário (DST): estrutura calculada explicitamente
    minutes_l: list[int] = []
    groups_l: list[list] = []
    last_key = None
    for i, b in enumerate(buckets):
        lt = datetime.fromtimestamp(b / 1000, tz=timezone.utc).astimezone(tz)
        minutes_l.append(lt.hour * 60 + lt.minute)
        key = (lt.hour, lt.utcoffset())
        if key != last_key:
            groups_l.append([lt.hour, i, i + 1])
            last_key = key
        else:
            groups_l[-1][2] = i + 1
    return DayFrame(local_date, rotation, tz_name, buckets, tuple(minutes_l), tuple(tuple(g) for g in groups_l))


def specific_bucket_index(frame: DayFrame, minute_of_day: int) -> Optional[int]:
    """Índice do bucket que CONTÉM o horário local ``minute_of_day`` do dia do frame."""
    tz = ZoneInfo(frame.tz_name)
    t = datetime.combine(frame.local_date, time(minute_of_day // 60, minute_of_day % 60)).replace(tzinfo=tz)
    t_ms = int(t.astimezone(timezone.utc).timestamp() * 1000)
    idx = bisect.bisect_right(frame.buckets, t_ms) - 1
    if idx < 0 or not frame.buckets:
        return None
    if t_ms >= frame.buckets[idx] + frame.rotation.cycle_ms:
        return None
    return idx


# ─── Fichas e blocos ─────────────────────────────────────────────────────────

@dataclass(frozen=True)
class Token:
    line_id: str
    campaign_id: str
    advertiser_id: str
    du: int
    allow_same: bool
    rank: int  # posição da linha na ordem de colocação
    seq: int  # índice da ficha da linha no dia


@dataclass(frozen=True)
class Placement:
    line_id: str
    campaign_id: str
    seq: int
    u: int
    du: int


@dataclass
class DayPlan:
    frame: DayFrame
    blocks: list  # por bucket: list[Placement] ordenado por u
    unplaced: list = field(default_factory=list)  # [{"line_id","seq","hour","reason"}]

    def used_units(self, idx: int) -> int:
        return sum(p.du for p in self.blocks[idx])

    def total_units(self) -> int:
        return sum(self.used_units(i) for i in range(len(self.blocks)))


def _adjacent_conflict(a: Token, b: Token) -> bool:
    return a.advertiser_id == b.advertiser_id and not (a.allow_same and b.allow_same)


def arrange_block(tokens: Sequence[Token]) -> Optional[list[Token]]:
    """Permutação lexicograficamente menor (por (rank, seq)) sem adjacência proibida."""
    items = sorted(tokens, key=lambda t: (t.rank, t.seq))
    n = len(items)
    if n <= 1:
        return list(items)
    used = [False] * n
    out: list[Token] = []
    budget = [_ARRANGE_BUDGET]

    def dfs() -> bool:
        if len(out) == n:
            return True
        budget[0] -= 1
        if budget[0] < 0:
            return False
        prev = out[-1] if out else None
        seen: set = set()
        for i in range(n):
            if used[i]:
                continue
            t = items[i]
            sig = (t.line_id, t.advertiser_id, t.allow_same)
            if sig in seen:  # fichas equivalentes: evita ramos simétricos
                continue
            seen.add(sig)
            if prev is not None and _adjacent_conflict(prev, t):
                continue
            used[i] = True
            out.append(t)
            if dfs():
                return True
            out.pop()
            used[i] = False
        return False

    return out if dfs() else None


class _DayState:
    def __init__(self, frame: DayFrame):
        self.frame = frame
        self.e = frame.rotation.units_per_block
        self.n = len(frame.buckets)
        self.tokens: list[list[Token]] = [[] for _ in range(self.n)]
        self.used = [0] * self.n
        self.pos: dict[str, list[int]] = {}  # posições por linha, ordenadas

    def gap_ok(self, line_id: str, idx: int, gap: int) -> bool:
        """Distância CIRCULAR (no dia) às outras fichas da mesma linha ≥ gap."""
        arr = self.pos.get(line_id)
        if not arr:
            return True
        j = bisect.bisect_left(arr, idx)
        hi = arr[j] if j < len(arr) else arr[0] + self.n
        lo = arr[j - 1] if j > 0 else arr[-1] - self.n
        return hi - idx >= gap and idx - lo >= gap

    def place(self, tok: Token, idx: int, arranged: list[Token]) -> None:
        self.tokens[idx] = arranged
        self.used[idx] += tok.du
        bisect.insort(self.pos.setdefault(tok.line_id, []), idx)


def _ideal_index(i: int, n: int, k_h: int, phi: int) -> int:
    return ((i * _PHI_SCALE + phi) * k_h // (n * _PHI_SCALE)) % k_h


def _hour_demand(line: Line, frame: DayFrame, hour: int, first_idx: int, end_idx: int,
                 seen_hours: set, specific: dict) -> tuple[list[int], int]:
    """(índices candidatos na hora, nº de fichas) da linha nesta hora local."""
    if line.freq_kind == FREQ_SPECIFIC:
        fixed = [i for i in specific.get(line.line_id, ()) if first_idx <= i < end_idx]
        return fixed, len(fixed)
    cands = [i for i in range(first_idx, end_idx) if line.in_band(frame.minutes[i])]
    if not cands:
        return [], 0
    if line.freq_kind == FREQ_PER_HOUR:
        return cands, line.freq_value
    # PER_DAY: distribuição registrada; hora duplicada (DST) só na 1ª ocorrência
    if hour in seen_hours:
        return cands, 0
    return cands, dict(line.distribution).get(hour, 0)


def pack_day(
    frame: DayFrame,
    lines: Sequence[Line],
    *,
    screen_id: str,
    competitors: frozenset = frozenset(),
    strict: bool = True,
) -> DayPlan:
    """Empacota um dia local (função pura das entradas)."""
    rot = frame.rotation
    active = sorted((ln for ln in lines if ln.active_on(frame.local_date)), key=Line.sort_key)
    state = _DayState(frame)
    unplaced: list[dict] = []
    ranks = {ln.line_id: r for r, ln in enumerate(active)}
    gaps = {ln.line_id: rot.gap_blocks(ln.min_interval_s) for ln in active}
    phis = {ln.line_id: phase_int(ln.campaign_id, screen_id) for ln in active}
    seq_counter = {ln.line_id: 0 for ln in active}
    specific: dict[str, list[int]] = {}
    for ln in active:
        if ln.freq_kind == FREQ_SPECIFIC:
            idxs = [specific_bucket_index(frame, m) for m in sorted(ln.specific_minutes)]
            specific[ln.line_id] = sorted(i for i in idxs if i is not None)

    for ln in active:
        if not rot.eligible(ln.duration_units):
            if strict:
                raise PackingError("DURATION_NOT_ELIGIBLE",
                                   f"Duração {ln.duration_units * UNIT_S} s não cabe no bloco externo.",
                                   {"line_id": ln.line_id, "date": frame.local_date.isoformat()})
            unplaced.append({"line_id": ln.line_id, "seq": -1, "hour": None, "reason": "DURATION_NOT_ELIGIBLE"})

    seen_hours: set[int] = set()
    for hour, a, b in frame.groups:
        for ln in active:
            if not rot.eligible(ln.duration_units):
                continue
            cands, n = _hour_demand(ln, frame, hour, a, b, seen_hours, specific)
            if n <= 0:
                continue
            k_h = len(cands)
            for i in range(n):
                seq = seq_counter[ln.line_id]
                seq_counter[ln.line_id] = seq + 1
                tok = Token(ln.line_id, ln.campaign_id, ln.advertiser_id, ln.duration_units,
                            ln.allow_same_advertiser, ranks[ln.line_id], seq)
                if ln.freq_kind == FREQ_SPECIFIC:
                    order = [cands[i]]
                else:
                    p = _ideal_index(i, n, k_h, phis[ln.line_id])
                    order = [cands[j] for j in sorted(range(k_h), key=lambda j: (abs(j - p), j))]
                placed = False
                reason = "NO_CAPACITY"
                for idx in order:
                    if state.used[idx] + tok.du > state.e:
                        reason = "NO_CAPACITY"
                        continue
                    if not state.gap_ok(ln.line_id, idx, gaps[ln.line_id]):
                        reason = "MIN_INTERVAL"
                        continue
                    if any(t.campaign_id != tok.campaign_id and frozenset((t.campaign_id, tok.campaign_id)) in competitors
                           for t in state.tokens[idx]):
                        reason = "COMPETITOR_IN_BLOCK"
                        continue
                    arranged = arrange_block(state.tokens[idx] + [tok])
                    if arranged is None:
                        reason = "SAME_ADVERTISER_ADJACENT"
                        continue
                    state.place(tok, idx, arranged)
                    placed = True
                    break
                if not placed:
                    info = {"line_id": ln.line_id, "campaign_id": ln.campaign_id, "seq": seq,
                            "date": frame.local_date.isoformat(), "hour": hour, "reason": reason}
                    if strict:
                        raise PackingError(
                            "INVENTORY_UNAVAILABLE",
                            f"Sem posição para a inserção {seq + 1} de {frame.local_date.isoformat()} "
                            f"{hour:02d}h ({reason}).",
                            info,
                        )
                    unplaced.append(info)
        seen_hours.add(hour)

    blocks: list[list[Placement]] = []
    for toks in state.tokens:
        u = 0
        out = []
        for t in toks:
            out.append(Placement(t.line_id, t.campaign_id, t.seq, u, t.du))
            u += t.du
        blocks.append(out)
    return DayPlan(frame, blocks, unplaced)


# ─── Vários dias (cadeia determinística) ─────────────────────────────────────

class Packer:
    """Empacota dias de UMA tela com memoização por assinatura do dia.

    O plano de um dia depende apenas de (estrutura do dia, linhas ativas no dia,
    concorrentes entre elas): dias repetidos não são recomputados.
    """

    def __init__(self, screen_id: str, tz_name: str, *, competitors: frozenset = frozenset(), strict: bool = True):
        self.screen_id = screen_id
        self.tz_name = tz_name
        self.competitors = competitors
        self.strict = strict
        self._memo: dict = {}
        self.packs = 0
        self.memo_hits = 0

    def pack_days(self, days: Sequence[tuple[date, RotationSpec]], lines: Sequence[Line]) -> list[DayPlan]:
        plans: list[DayPlan] = []
        for d, rot in days:
            frame = day_frame(d, self.tz_name, rot)
            active = [ln for ln in lines if ln.active_on(d)]
            act_ids = {ln.campaign_id for ln in active}
            comp = frozenset(p for p in self.competitors if p <= act_ids)
            key = (frame.signature(), tuple(sorted(ln.memo_key() for ln in active)), comp)
            hit = self._memo.get(key)
            if hit is not None:
                self.memo_hits += 1
                blocks, unplaced = hit
                plans.append(DayPlan(frame, blocks, [dict(u, date=d.isoformat()) for u in unplaced]))
                continue
            self.packs += 1
            plan = pack_day(frame, active, screen_id=self.screen_id, competitors=comp, strict=self.strict)
            self._memo[key] = (plan.blocks, plan.unplaced)
            plans.append(plan)
        return plans


def plan_checksum(plans: Iterable[DayPlan]) -> str:
    """Checksum canônico do plano (independe da ordem de entrada das linhas)."""
    doc = []
    for p in plans:
        doc.append([
            p.frame.local_date.isoformat(),
            [[b, [[x.line_id, x.seq, x.u, x.du] for x in blk]] for b, blk in zip(p.frame.buckets, p.blocks) if blk],
        ])
    return hashlib.sha256(json.dumps(doc, separators=(",", ":"), sort_keys=True).encode()).hexdigest()


def line_insertions_on_day(line: Line, frame: DayFrame) -> int:
    """Inserções que a linha DEMANDA no dia do frame (contratadas/planejadas)."""
    if not line.active_on(frame.local_date):
        return 0
    if line.freq_kind == FREQ_SPECIFIC:
        return sum(1 for m in line.specific_minutes if specific_bucket_index(frame, m) is not None)
    total = 0
    seen: set[int] = set()
    for hour, a, b in frame.groups:
        _, n = _hour_demand(line, frame, hour, a, b, seen, {})
        total += n
        seen.add(hour)
    return total


def demand_units_per_hour(line: Line, rotation: RotationSpec) -> int:
    """Unidades por hora (pior hora) que a linha consome."""
    if line.freq_kind == FREQ_PER_HOUR:
        return line.freq_value * line.duration_units
    if line.freq_kind == FREQ_PER_DAY:
        dist = dict(line.distribution)
        return (max(dist.values()) if dist else 0) * line.duration_units
    per_hour: dict[int, int] = {}
    for m in line.specific_minutes:
        per_hour[m // 60] = per_hour.get(m // 60, 0) + 1
    return (max(per_hour.values()) if per_hour else 0) * line.duration_units
