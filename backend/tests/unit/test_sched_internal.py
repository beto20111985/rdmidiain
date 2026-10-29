"""W-INV — padrão do conteúdo interno (#423, #1063–#1064; A6 §10.6 item 6)."""
from __future__ import annotations

from app.modules.scheduler.internal import InternalStream, build_internal_pattern


def test_single_60s_piece_repeats_inside_120s_block():
    assert build_internal_pattern([("p", 60_000)], 120_000) == [[{"c": "p", "d": 60_000}, {"c": "p", "d": 60_000}]]


def test_ten_minutes_of_pieces_rotate_between_blocks():
    items = [(f"p{i}", 60_000) for i in range(10)]  # 10 min de peças
    pat = build_internal_pattern(items, 120_000)
    assert len(pat) == 5
    assert [[x["c"] for x in blk] for blk in pat] == [["p0", "p1"], ["p2", "p3"], ["p4", "p5"], ["p6", "p7"],
                                                     ["p8", "p9"]]
    # cada peça aparece exatamente uma vez por volta completa (rotação justa)
    assert sorted(x["c"] for blk in pat for x in blk) == sorted(k for k, _ in items)


def test_never_cuts_and_skips_pieces_longer_than_block():
    pat = build_internal_pattern([("long", 180_000), ("a", 90_000), ("b", 60_000)], 120_000)
    flat = [x for blk in pat for x in blk]
    assert all(x["c"] != "long" for x in flat)  # > bloco interno: barrado
    assert all(sum(x["d"] for x in blk) <= 120_000 for blk in pat)  # nunca corta
    assert pat == [[{"c": "a", "d": 90_000}], [{"c": "b", "d": 60_000}]]


def test_odd_mix_periodic_from_start():
    pat = build_internal_pattern([("x", 30_000), ("y", 30_000), ("z", 90_000)], 120_000)
    # [x,y] (z não cabe nos 60 s restantes) → [z,x] → [y,z] → volta ao estado inicial
    assert [[i["c"] for i in b] for b in pat] == [["x", "y"], ["z", "x"], ["y", "z"]]


def test_empty_list_gives_empty_pattern():
    assert build_internal_pattern([], 120_000) == []


def test_stream_fills_external_gaps_first_fit_and_keeps_rotation():
    s = InternalStream([("a", 60_000), ("b", 30_000), ("c", 60_000)])
    assert s.fill(90_000) == [("a", 60_000), ("b", 30_000)]
    # lacuna de 30 s: c e a (60 s) não cabem; a próxima que cabe é b (nunca corta)
    assert s.fill(30_000) == [("b", 30_000)]
    assert s.fill(60_000) == [("c", 60_000)]
    assert s.fill(120_000) == [("a", 60_000), ("b", 30_000), ("b", 30_000)]
    only_long = InternalStream([("l", 120_000)])
    assert only_long.fill(90_000) == []  # sobra vai para o fallback do player


def test_pattern_starts_with_first_piece_even_with_non_returning_greedy():
    # [p2 30 s, p1 60 s] em blocos de 120 s: [p2,p1,p2] → [p1,p2] → (estado repetido) — começa por p2
    pat = build_internal_pattern([("p2", 30_000), ("p1", 60_000)], 120_000)
    assert [[x["c"] for x in b] for b in pat] == [["p2", "p1", "p2"], ["p1", "p2"]]
