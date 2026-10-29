"""W-INV — resolve() de referência (RDPP §5.1, §5.2, §8.2) e exemplos A6 §10.6."""
from __future__ import annotations

from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from app.modules.scheduler.resolve import resolve

SP = ZoneInfo("America/Sao_Paulo")
U = 30_000
C = 240_000
I = 120_000
WEEK = 7 * 24 * 3600 * 1000


def ms(dt: datetime) -> int:
    return int(dt.astimezone(timezone.utc).timestamp() * 1000)


def sp(h, m, s=0) -> int:
    return ms(datetime(2026, 10, 7, h, m, s, tzinfo=SP))


ANCHOR = sp(8, 7)  # âncora 08:07:00 local (São Paulo)


def bucket(t: int) -> int:
    return (t // C) * C


def cr(cid, media, d=30_000, kind="EXTERNAL", vf=0, vu=253402300799999):
    return {"creative_id": cid, "code": "RD-AAAAAA", "kind": kind, "campaign_id": None, "advertiser_id": None,
            "media": media, "duration_ms": d, "valid_from_ms": vf, "valid_until_ms": vu, "label": "Publicidade"}


def manifest(**over):
    m = {
        "schema": "rd.manifest/1", "manifest_id": "x", "version": 1, "screen_id": "s", "screen_code": "TEL-GO-AAAA",
        "generated_at_ms": 0, "timezone": "America/Sao_Paulo",
        "rotation": {"internal_ms": I, "external_ms": I, "cycle_ms": C, "unit_ms": U, "policy_id": None},
        "anchor_ms": ANCHOR,
        "horizon": {"from_ms": ms(datetime(2026, 10, 7, tzinfo=SP)), "to_ms": ms(datetime(2026, 10, 7, tzinfo=SP)) + WEEK,
                    "repeat_period_ms": WEEK},
        "settings": {}, "media": {},
        "creatives": {"c1": cr("cr-1", "m1"), "c2": cr("cr-2", "m2", 60_000), "c7": cr("cr-7", "m7", 60_000, "INTERNAL"),
                      "c8": cr("cr-8", "m8", 60_000, "INTERNAL"), "f1": cr("fb-1", "mf1", 30_000, "FALLBACK"),
                      "f2": cr("fb-2", "mf2", 60_000, "FALLBACK"), "e1": cr("em-1", "me1", 30_000, "INSTITUTIONAL")},
        "internal_pattern": [[{"c": "c7", "d": 60_000}, {"c": "c8", "d": 60_000}]],
        "fallback": [{"c": "f1", "d": 30_000}],
        "external_plan": {},
        "emergency": [],
        "revoked": [],
    }
    m.update(over)
    return m


READY = {"revoked": [], "media_ready": ["m1", "m2", "m7", "m8", "mf1", "mf2", "me1"]}


def seg(m, t, state=READY, anchor=ANCHOR):
    return resolve(m, anchor, state, t).to_dict()


# ─── A6 §10.6 ────────────────────────────────────────────────────────────────

def test_a6_reboot_0847_is_internal_block_cycle_10_offset_0():
    t = sp(8, 47)
    assert (t - ANCHOR) // C == 10 and (t - ANCHOR) % C == 0
    assert seg(manifest(), t) == {"block": "INTERNAL", "kind": "ITEM", "creative_key": "c7",
                                  "start_ms": sp(8, 47), "end_ms": sp(8, 48)}


def test_a6_reboot_084910_is_external_unit0_item_and_runtime_skips_it():
    s = sp(8, 49)  # início do bloco externo do ciclo 10
    m = manifest(external_plan={str(bucket(s)): [{"i": "i1", "c": "c1", "u": 0, "d": 30_000},
                                                 {"i": "i2", "c": "c2", "u": 1, "d": 60_000}]})
    t = sp(8, 49, 10)
    assert (t - ANCHOR) // C == 10
    # resolve() devolve o ITEM (o runtime exibe fallback até 08:49:30 e gera SKIPPED)
    assert seg(m, t) == {"block": "EXTERNAL", "kind": "ITEM", "creative_key": "c1", "start_ms": sp(8, 49),
                         "end_ms": sp(8, 49, 30)}
    assert seg(m, sp(8, 49, 30))["creative_key"] == "c2"


def test_a6_0907_is_cycle_15_hour_cycle_preserved():
    t = sp(9, 7)
    assert (t - ANCHOR) // C == 15 and (t - ANCHOR) % C == 0
    assert seg(manifest(), t)["block"] == "INTERNAL"


def test_bucket_key_independent_of_anchor():
    """Mesma tela, âncoras diferentes: o bloco externo cai no bucket que contém seu início."""
    for anchor in (ANCHOR, ANCHOR + 17_000, ANCHOR + 199_000):
        s = anchor + 10 * C + I
        m = manifest(anchor_ms=anchor, external_plan={str(bucket(s)): [{"i": "i", "c": "c1", "u": 0, "d": U}]})
        assert seg(m, s + 1, anchor=anchor)["creative_key"] == "c1"


# ─── Fallback (§5.2, §8.2) ───────────────────────────────────────────────────

def test_fallback_cyclic_index_and_filler_remainder():
    s = ANCHOR + I  # bloco externo do ciclo 0, sem entradas
    m = manifest(fallback=[{"c": "f2", "d": 60_000}, {"c": "f1", "d": 30_000}])
    idx0 = ((s - ANCHOR) // U) % 2  # = 4 % 2 = 0 → f2
    assert idx0 == 0
    assert seg(m, s) == {"block": "EXTERNAL", "kind": "ITEM", "creative_key": "f2", "start_ms": s, "end_ms": s + 60_000}
    assert seg(m, s + 60_000)["creative_key"] == "f1"
    # restante 30 s < próximo item (f2, 60 s) ⇒ FILLER exibindo f2, sem PoP
    assert seg(m, s + 95_000) == {"block": "EXTERNAL", "kind": "FILLER", "creative_key": "f2",
                                  "start_ms": s + 90_000, "end_ms": s + 120_000}


def test_empty_fallback_is_filler_null():
    s = ANCHOR + I
    assert seg(manifest(fallback=[]), s + 1) == {"block": "EXTERNAL", "kind": "FILLER", "creative_key": None,
                                                 "start_ms": s, "end_ms": s + I}


def test_fallback_and_emergency_skip_validity_checks():
    expired = cr("fb-x", "missing", 30_000, "FALLBACK", 0, 1)
    m = manifest(creatives={**manifest()["creatives"], "fx": expired}, fallback=[{"c": "fx", "d": 30_000}])
    assert seg(m, ANCHOR + I)["creative_key"] == "fx"


def test_fallback_intervals_do_not_merge():
    s = ANCHOR + I
    m = manifest(fallback=[{"c": "f1", "d": 30_000}, {"c": "f2", "d": 60_000}],
                 external_plan={str(bucket(s)): [{"i": "i", "c": "c1", "u": 0, "d": U}]},
                 revoked=["cr-1"])
    # item revogado [s, s+30) é intervalo próprio: índice floor((s−A)/U) mod 2 = 0 → f1
    assert seg(m, s) == {"block": "EXTERNAL", "kind": "ITEM", "creative_key": "f1", "start_ms": s, "end_ms": s + U}
    # a lacuna [s+30, s+120) é OUTRO intervalo: índice floor((s+30−A)/U) mod 2 = 1 → f2 (60 s) e depois f1
    assert seg(m, s + U) == {"block": "EXTERNAL", "kind": "ITEM", "creative_key": "f2", "start_ms": s + U,
                             "end_ms": s + 90_000}
    assert seg(m, s + 90_000)["creative_key"] == "f1"


# ─── Validade / revogação / mídia ────────────────────────────────────────────

def test_item_validity_checked_at_item_start():
    s = ANCHOR + I
    plan = {str(bucket(s)): [{"i": "i", "c": "cv", "u": 0, "d": U}]}
    base = manifest()["creatives"]
    ok = manifest(creatives={**base, "cv": cr("cr-v", "m1", U, vf=s, vu=s + 1)}, external_plan=plan)
    assert seg(ok, s + 10)["creative_key"] == "cv"  # início s ∈ [vf, vu)
    late = manifest(creatives={**base, "cv": cr("cr-v", "m1", U, vf=0, vu=s)}, external_plan=plan)
    assert seg(late, s + 10)["creative_key"] == "f1"  # valid_until == início ⇒ não toca
    early = manifest(creatives={**base, "cv": cr("cr-v", "m1", U, vf=s + 1)}, external_plan=plan)
    assert seg(early, s + 10)["creative_key"] == "f1"


def test_local_revocation_and_media_not_ready():
    s = ANCHOR + I
    m = manifest(external_plan={str(bucket(s)): [{"i": "i", "c": "c1", "u": 0, "d": U}]})
    assert seg(m, s)["creative_key"] == "c1"
    assert seg(m, s, {"revoked": ["cr-1"], "media_ready": READY["media_ready"]})["creative_key"] == "f1"
    assert seg(m, s, {"revoked": [], "media_ready": ["m2"]})["creative_key"] == "f1"


# ─── §8.2 item 4: sobreposição / estouro ─────────────────────────────────────

def test_overlap_first_plays_second_rejected_and_overflow_goes_to_fallback():
    s = ANCHOR + I
    m = manifest(external_plan={str(bucket(s)): [
        {"i": "a", "c": "c2", "u": 0, "d": 60_000},  # [s, s+60)
        {"i": "b", "c": "c1", "u": 1, "d": 30_000},  # sobrepõe → [s+60, s+60) vazio (contido)
        {"i": "c", "c": "c2", "u": 3, "d": 60_000},  # estoura o bloco → [s+90, s+120) fallback
    ]})
    assert seg(m, s + 10)["creative_key"] == "c2" and seg(m, s + 10)["end_ms"] == s + 60_000
    g = seg(m, s + 70_000)  # lacuna [s+60, s+90) → fallback próprio
    assert g["creative_key"] == "f1" and g["start_ms"] == s + 60_000
    o = seg(m, s + 100_000)
    assert (o["block"], o["start_ms"]) == ("EXTERNAL", s + 90_000) and o["creative_key"] == "f1"


def test_overlap_partially_beyond_cursor_becomes_own_fallback_interval():
    s = ANCHOR + I
    m = manifest(fallback=[{"c": "f1", "d": U}, {"c": "f2", "d": 60_000}], external_plan={str(bucket(s)): [
        {"i": "a", "c": "c1", "u": 0, "d": U},        # [s, s+30)
        {"i": "b", "c": "c2", "u": 0, "d": 60_000},   # começa antes do cursor → [s+30, s+60) fallback
    ]})
    r = seg(m, s + 40_000)
    # intervalo próprio [s+30, s+60): índice floor(((s+30)−A)/U) mod 2 = 5 mod 2 = 1 → f2 (60 s) não cabe → FILLER f2
    assert r == {"block": "EXTERNAL", "kind": "FILLER", "creative_key": "f2", "start_ms": s + U, "end_ms": s + 60_000}


# ─── Horizonte (repetição semanal) ───────────────────────────────────────────

def test_horizon_weekly_repeat_both_directions():
    s = ANCHOR + I
    m = manifest(external_plan={str(bucket(s)): [{"i": "i", "c": "c1", "u": 0, "d": U}]})
    later = s + 3 * WEEK  # b ≥ to ⇒ n ≥ 1
    assert seg(m, later)["creative_key"] == "c1" and seg(m, later)["start_ms"] == later
    # b < from ⇒ n ≤ −1
    m2 = manifest(external_plan={str(bucket(s) + WEEK): [{"i": "i", "c": "c1", "u": 0, "d": U}]},
                  horizon={"from_ms": bucket(s) + 1000, "to_ms": bucket(s) + 1000 + WEEK, "repeat_period_ms": WEEK})
    assert seg(m2, s)["creative_key"] == "c1"


def test_horizon_without_match_is_whole_block_fallback():
    s = ANCHOR + I
    m = manifest(horizon={"from_ms": 0, "to_ms": 1000, "repeat_period_ms": 0},
                 external_plan={str(bucket(s)): [{"i": "i", "c": "c1", "u": 0, "d": U}]})
    assert seg(m, s)["creative_key"] == "f1"


# ─── Interno ─────────────────────────────────────────────────────────────────

def test_internal_pattern_rotation_and_overflowing_item():
    m = manifest(internal_pattern=[[{"c": "c7", "d": 60_000}, {"c": "c8", "d": 60_000}],
                                   [{"c": "c8", "d": 60_000}, {"c": "c2", "d": 90_000}]])
    k0 = ANCHOR
    k1 = ANCHOR + C
    assert seg(m, k0)["creative_key"] == "c7"
    assert seg(m, k1)["creative_key"] == "c8"
    # 2º item de k1 (90 s) ultrapassa o bloco de 120 s → não toca; resto do bloco vai a fallback
    r = seg(m, k1 + 70_000)
    assert r["block"] == "INTERNAL" and r["creative_key"] == "f1" and r["start_ms"] == k1 + 60_000
    assert seg(m, ANCHOR + 2 * C)["creative_key"] == "c7"  # k=2 ⇒ pattern[0] de novo


def test_empty_internal_pattern_is_fallback():
    r = seg(manifest(internal_pattern=[]), ANCHOR + 5)
    assert (r["block"], r["creative_key"], r["start_ms"], r["end_ms"]) == ("INTERNAL", "f1", ANCHOR, ANCHOR + U)


# ─── Emergência ──────────────────────────────────────────────────────────────

def test_emergency_preempts_repeats_and_last_partial_is_filler():
    f = ANCHOR + 7_000
    u = f + 75_000  # 2 repetições de 30 s + 15 s
    m = manifest(emergency=[{"id": "x", "c": "e1", "from_ms": f, "until_ms": u},
                            {"id": "y", "c": "c1", "from_ms": f, "until_ms": u + WEEK}])
    assert seg(m, f) == {"block": "EMERGENCY", "kind": "ITEM", "creative_key": "e1", "start_ms": f, "end_ms": f + U}
    assert seg(m, f + 31_000)["start_ms"] == f + U
    assert seg(m, f + 61_000) == {"block": "EMERGENCY", "kind": "FILLER", "creative_key": "e1",
                                  "start_ms": f + 60_000, "end_ms": u}
    # fora da 1ª janela, vale a próxima da lista
    assert seg(m, u + 1)["creative_key"] == "c1"
