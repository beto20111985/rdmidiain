"""W-INV — empacotador determinístico (ADR_006 §7; A6 §10.8; simulação a6_capacity_model_sim.py)."""
from __future__ import annotations

import random
from datetime import date

import pytest
from hypothesis import HealthCheck, given, settings, strategies as st

from app.modules.scheduler.packer import (
    FREQ_PER_DAY,
    FREQ_PER_HOUR,
    FREQ_SPECIFIC,
    PRIORITY_PREMIUM,
    PRIORITY_STANDARD,
    Line,
    Packer,
    PackingError,
    bresenham_counts,
    day_frame,
    line_insertions_on_day,
    pack_day,
    per_day_distribution,
    plan_checksum,
    specific_bucket_index,
    validate_line,
)
from app.modules.scheduler.policy import UNIT_S, RotationSpec

D = date(2026, 10, 7)
TZ = "America/Sao_Paulo"
R22 = RotationSpec(120, 120)
BAND_15H = ((15 * 60, 16 * 60),)


def L(lid, adv, du, n, iv=0, *, prem=False, spec=None, same=False, band=BAND_15H, kind=FREQ_PER_HOUR, dist=(),
      camp=None, d_from=D, d_to=D, mask=127):
    if spec is not None:
        return Line(lid, camp or lid, adv, du, FREQ_SPECIFIC, len(spec), d_from, d_to, mask, band, tuple(spec), iv,
                    PRIORITY_PREMIUM if prem else PRIORITY_STANDARD, same)
    return Line(lid, camp or lid, adv, du, kind, n, d_from, d_to, mask, band, (), iv,
                PRIORITY_PREMIUM if prem else PRIORITY_STANDARD, same, dist)


# Cenário da simulação A6 (§10.8): A e B concorrentes (bebidas), C mesmo anunciante de A,
# D 120 s premium, E institucional RD, F horário específico 15:00.
A6_LINES = [
    L("A-super-bebida", "SUPER", 1, 4, 600),
    L("B-distribuidora", "DIST2", 1, 4, 600),
    L("C-super-salgadinho", "SUPER", 2, 3, 900),
    L("D-academia-120s", "GYM", 4, 2, 1200, prem=True),
    L("E-rd-institucional", "RD", 1, 2, 900),
    L("F-especifico", "PIZZA", 2, 1, spec=[15 * 60]),
]
A6_COMP = frozenset({frozenset(("A-super-bebida", "B-distribuidora"))})


def _plan(lines, comp=A6_COMP, screen="TELA-OFICINA", rot=R22, d=D):
    return Packer(screen, TZ, competitors=comp, strict=True).pack_days([(d, rot)], lines)[0]


def _tokens(plan):
    """[(bucket_ms, u, du, line_id, campaign_id)]"""
    return [(b, p.u, p.du, p.line_id, p.campaign_id) for b, blk in zip(plan.frame.buckets, plan.blocks) for p in blk]


def assert_invariants(plan, lines, comp, rot):
    by = {ln.line_id: ln for ln in lines}
    e = rot.units_per_block
    for b, blk in zip(plan.frame.buckets, plan.blocks):
        # capacidade: nunca excede E unidades por bloco (nenhum overbooking)
        assert sum(p.du for p in blk) <= e
        # ocupação contígua desde u=0, sem sobreposição
        u = 0
        for p in blk:
            assert p.u == u
            u += p.du
        camps = [p.campaign_id for p in blk]
        # concorrentes NUNCA no mesmo bloco
        for i, a in enumerate(camps):
            for c in camps[i + 1:]:
                assert a == c or frozenset((a, c)) not in comp
        # mesmo anunciante adjacente só se as duas linhas contrataram
        for x, y in zip(blk, blk[1:]):
            lx, ly = by[x.line_id], by[y.line_id]
            if lx.advertiser_id == ly.advertiser_id:
                assert lx.allow_same_advertiser and ly.allow_same_advertiser
    # intervalo mínimo (tempo real entre inícios, inclusive na virada da hora e circular no dia)
    n_day_ms = len(plan.frame.buckets) * rot.cycle_ms
    for ln in lines:
        starts = sorted(b + u * UNIT_S * 1000 for b, u, _, lid, _ in _tokens(plan) if lid == ln.line_id)
        if len(starts) < 2 or ln.min_interval_s == 0:
            continue
        gaps = [y - x for x, y in zip(starts, starts[1:])] + [starts[0] + n_day_ms - starts[-1]]
        assert min(gaps) >= ln.min_interval_s * 1000, (ln.line_id, min(gaps))
    # 120 s sozinho no bloco (2+2)
    if e == 4:
        for blk in plan.blocks:
            if any(p.du == 4 for p in blk):
                assert len(blk) == 1


def test_a6_scenario_reproduced():
    plan = _plan(A6_LINES)
    assert_invariants(plan, A6_LINES, A6_COMP, R22)
    # ocupação 26/60 na hora (sobra 34 volta ao estabelecimento)
    assert plan.total_units() == 26
    by_line = {}
    for b, u, du, lid, _ in _tokens(plan):
        by_line[lid] = by_line.get(lid, 0) + 1
    assert by_line == {"A-super-bebida": 4, "B-distribuidora": 4, "C-super-salgadinho": 3, "D-academia-120s": 2,
                       "E-rd-institucional": 2, "F-especifico": 1}
    # horário específico 15:00 → bucket que contém 15:00 (primeiro bucket da hora)
    f = [b for b, u, du, lid, _ in _tokens(plan) if lid == "F-especifico"]
    assert f == [plan.frame.buckets[specific_bucket_index(plan.frame, 15 * 60)]]


def test_competitors_never_share_a_block_even_when_forced():
    # 2 concorrentes de 30 s, 15/h cada, mas só 15 blocos: cabem (bloco de 4 un.) se juntos — proibido ⇒ recusa
    lines = [L("X", "AX", 1, 8), L("Y", "AY", 1, 8)]
    comp = frozenset({frozenset(("X", "Y"))})
    with pytest.raises(PackingError) as ei:
        _plan(lines, comp)
    assert ei.value.details["reason"] == "COMPETITOR_IN_BLOCK"
    # sem concorrência os mesmos pedidos cabem
    plan = _plan(lines, frozenset())
    assert_invariants(plan, lines, frozenset(), R22)


def test_min_interval_respected_between_hours():
    band = ((14 * 60, 17 * 60),)  # três horas seguidas
    lines = [L("I", "AI", 1, 5, 600, band=band), L("J", "AJ", 2, 3, 900, band=band), L("K", "AK", 1, 2, 1200, band=band)]
    for ln in lines:
        validate_line(ln, R22)
    plan = _plan(lines, frozenset())
    assert_invariants(plan, lines, frozenset(), R22)
    # verificação explícita na fronteira 14h→15h→16h para a linha I (5/h, 10 min)
    starts = sorted(b + u * 30000 for b, u, _, lid, _ in _tokens(plan) if lid == "I")
    assert len(starts) == 15
    assert all(y - x >= 600_000 for x, y in zip(starts, starts[1:]))


def test_same_advertiser_not_adjacent_unless_contracted():
    lines = [L("P1", "MESMO", 1, 5), L("P2", "MESMO", 1, 5), L("Q", "OUTRO", 1, 15)]
    plan = _plan(lines, frozenset())
    assert_invariants(plan, lines, frozenset(), R22)
    adv = {"P1": "MESMO", "P2": "MESMO", "Q": "OUTRO"}
    for blk in plan.blocks:
        ads = [adv[p.line_id] for p in blk]
        assert all(a != b for a, b in zip(ads, ads[1:]))
    # 20 inserções do mesmo anunciante em 15 blocos exigiriam adjacência ⇒ recusado…
    with pytest.raises(PackingError) as ei:
        _plan([L("P1", "MESMO", 1, 10), L("P2", "MESMO", 1, 10)], frozenset())
    assert ei.value.details["reason"] == "SAME_ADVERTISER_ADJACENT"
    # …a não ser que AMBAS as contratações permitam a adjacência
    ok = [L("P1", "MESMO", 1, 10, same=True), L("P2", "MESMO", 1, 10, same=True)]
    plan = _plan(ok, frozenset())
    assert_invariants(plan, ok, frozenset(), R22)
    assert any(len(b) >= 2 for b in plan.blocks)
    # só uma das duas permitir NÃO basta
    with pytest.raises(PackingError):
        _plan([L("P1", "MESMO", 1, 10, same=True), L("P2", "MESMO", 1, 10)], frozenset())


def test_120s_occupies_whole_block_and_ineligible_in_3_plus_1():
    lines = [L("BIG", "A", 4, 3, 0), L("SMALL", "B", 1, 12, 0)]
    plan = _plan(lines, frozenset())
    assert_invariants(plan, lines, frozenset(), R22)
    big = [b for b, u, du, lid, _ in _tokens(plan) if lid == "BIG"]
    assert len(big) == 3
    with pytest.raises(PackingError) as ei:
        validate_line(L("BIG", "A", 4, 1), RotationSpec(180, 60))
    assert ei.value.code == "DURATION_NOT_ELIGIBLE"


def test_determinism_permuted_inputs_same_checksum():
    p1 = _plan(A6_LINES)
    rnd = random.Random(42)
    for _ in range(10):
        perm = A6_LINES[:]
        rnd.shuffle(perm)
        assert plan_checksum([_plan(perm)]) == plan_checksum([p1])


def test_incompatible_frequency_refused_6_per_hour_with_10_min():
    with pytest.raises(PackingError) as ei:
        validate_line(L("X", "X", 1, 6, 600), R22)
    assert ei.value.code == "FREQUENCY_INCOMPATIBLE" and ei.value.details["max_per_hour"] == 5
    validate_line(L("X", "X", 1, 5, 600), R22)  # 5/h cabe


def test_per_day_bresenham_registered_distribution():
    assert sum(bresenham_counts(7, 14)) == 7
    dist = per_day_distribution(3, [[8 * 60, 22 * 60]])
    assert sum(dist.values()) == 3 and all(v == 1 for v in dist.values())
    assert sorted(int(h) for h in dist) == [10, 14, 19]  # espalhado (centrado), não nas pontas
    ln = L("PD", "A", 1, 3, 0, kind=FREQ_PER_DAY, band=((8 * 60, 22 * 60),),
           dist=tuple(sorted((int(h), c) for h, c in dist.items())))
    plan = _plan([ln], frozenset())
    hours = sorted(plan.frame.minutes[i] // 60 for i, blk in enumerate(plan.blocks) if blk)
    assert hours == [10, 14, 19]
    assert line_insertions_on_day(ln, plan.frame) == 3


def test_specific_time_goes_to_bucket_containing_t():
    fr = day_frame(D, TZ, R22)
    idx = specific_bucket_index(fr, 12 * 60 + 2)  # 12:02 → bucket 12:00–12:04
    assert fr.minutes[idx] == 12 * 60
    ln = L("S", "A", 1, 0, spec=[12 * 60 + 2, 18 * 60 + 30], band=((0, 1440),))
    plan = _plan([ln], frozenset())
    got = sorted(plan.frame.minutes[i] for i, blk in enumerate(plan.blocks) if blk)
    assert got == [12 * 60, 18 * 60 + 28]


def test_specific_times_closer_than_interval_refused_including_midnight_wrap():
    with pytest.raises(PackingError):
        validate_line(L("S", "A", 1, 0, 600, spec=[12 * 60, 12 * 60 + 5], band=((0, 1440),)), R22)
    with pytest.raises(PackingError):
        validate_line(L("S", "A", 1, 0, 600, spec=[0, 23 * 60 + 58], band=((0, 1440),)), R22)


def test_capacity_exceeded_refused_strict_and_reported_best_effort():
    lines = [L(f"L{i}", f"A{i}", 1, 15) for i in range(5)]  # 75 > 60 un./h
    with pytest.raises(PackingError) as ei:
        _plan(lines, frozenset())
    assert ei.value.code == "INVENTORY_UNAVAILABLE"
    best = Packer("T", TZ, strict=False).pack_days([(D, R22)], lines)[0]
    assert best.total_units() <= 60 * 1 and len(best.unplaced) == 15


def test_memoization_reuses_identical_days():
    ln = L("M", "A", 1, 2, 600, band=((8 * 60, 20 * 60),), d_from=D, d_to=date(2026, 11, 5))
    pk = Packer("T", TZ, strict=True)
    days = [(date(2026, 10, 7).fromordinal(D.toordinal() + i), R22) for i in range(30)]
    plans = pk.pack_days(days, [ln])
    assert pk.packs == 1 and pk.memo_hits == 29
    assert all(p.total_units() == 24 for p in plans)


# ─── Propriedade (Hypothesis): nenhum overbooking / invariantes sempre válidos ─────

line_st = st.builds(
    lambda i, adv, du, n, iv, prem, same: (i, adv, du, n, iv, prem, same),
    st.integers(0, 10**6), st.sampled_from(["A1", "A2", "A3", "A4", "A5"]), st.sampled_from([1, 2, 4]),
    st.integers(1, 5), st.sampled_from([0, 300, 600, 900]), st.booleans(), st.booleans(),
)


@settings(max_examples=120, deadline=None, suppress_health_check=[HealthCheck.too_slow])
@given(raw=st.lists(line_st, min_size=1, max_size=9),
       comp_bits=st.lists(st.booleans(), min_size=36, max_size=36),
       policy=st.sampled_from([(120, 120), (180, 60), (60, 60), (120, 180)]),
       seed=st.integers(0, 1000))
def test_property_sequential_checkouts_never_overbook(raw, comp_bits, policy, seed):
    """Simula checkouts sequenciais: aceita a linha só se o conjunto empacota (modo estrito).
    O conjunto aceito SEMPRE respeita capacidade, concorrência, adjacência e intervalos,
    e o plano é idêntico com a entrada permutada."""
    rot = RotationSpec(*policy)
    band = ((10 * 60, 12 * 60),)
    lines = []
    for k, (i, adv, du, n, iv, prem, same) in enumerate(raw):
        n = min(n, rot.max_per_hour(iv))
        if n <= 0:
            continue
        lines.append(L(f"L{k:02d}-{i}", adv, du, n, iv, prem=prem, same=same, band=band, camp=f"C{k:02d}"))
    camps = [ln.campaign_id for ln in lines]
    comp = set()
    bit = 0
    for a in range(len(camps)):
        for b in range(a + 1, len(camps)):
            if bit < len(comp_bits) and comp_bits[bit]:
                comp.add(frozenset((camps[a], camps[b])))
            bit += 1
    comp = frozenset(comp)
    accepted = []
    for ln in lines:
        try:
            validate_line(ln, rot)
            Packer("TELA", TZ, competitors=comp, strict=True).pack_days([(D, rot)], accepted + [ln])
        except PackingError:
            continue
        accepted.append(ln)
    if not accepted:
        return
    plan = Packer("TELA", TZ, competitors=comp, strict=True).pack_days([(D, rot)], accepted)[0]
    assert_invariants(plan, accepted, comp, rot)
    for ln in accepted:  # cada linha aceita recebeu TODAS as suas inserções
        got = sum(1 for *_, lid, _ in _tokens(plan) if lid == ln.line_id)
        assert got == ln.freq_value * 2
    shuffled = accepted[:]
    random.Random(seed).shuffle(shuffled)
    plan2 = Packer("TELA", TZ, competitors=comp, strict=True).pack_days([(D, rot)], shuffled)[0]
    assert plan_checksum([plan]) == plan_checksum([plan2])


def test_dst_day_frames_have_correct_hours():
    # America/Sao_Paulo teve DST até 2019: 2018-11-04 começou (hora 00:00 inexistente)
    fr = day_frame(date(2018, 11, 4), TZ, R22)
    assert len(fr.buckets) == 23 * 15
    fr2 = day_frame(date(2019, 2, 16), TZ, R22)  # fim do DST (17/02 00:00 → 16/02 23:00): hora 23 duplicada
    assert len(fr2.buckets) == 25 * 15
    assert [g[0] for g in fr2.groups].count(23) == 2
    ln = L("H", "A", 1, 1, 0, band=((0, 1440),), d_from=date(2019, 2, 16), d_to=date(2019, 2, 16))
    plan = pack_day(fr2, [ln], screen_id="T")
    assert sum(len(b) for b in plan.blocks) == 25  # hora duplicada também recebe a inserção PER_HOUR
