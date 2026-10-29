"""W-INV — montagem pura do manifesto RDPP/1 (§5) e validade local (P001 §51, P002 §25)."""
from __future__ import annotations

from datetime import date, datetime, timezone

import pytest

from app.core.timeutils import end_of_local_day_utc, start_of_local_day_utc
from app.modules.inventory.state import ms_of
from app.modules.playback import protocol as P
from app.modules.scheduler.manifest import (
    CreativeRef,
    EmergencyRef,
    ManifestInputs,
    MediaRef,
    assemble_manifest,
    content_view,
    default_settings,
    insertion_id,
)
from app.modules.scheduler.packer import FREQ_PER_HOUR, Line, Packer
from app.modules.scheduler.policy import RotationSpec
from app.modules.scheduler.resolve import resolve

TZ = "America/Sao_Paulo"
D = date(2026, 10, 7)
R22 = RotationSpec(120, 120, policy_id="00000000-0000-4000-8000-000000000120")


def zms(s: str) -> int:
    return int(datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp() * 1000)


@pytest.mark.parametrize("tz,expected", [
    ("America/Sao_Paulo", "2026-02-01T02:59:59.999Z"),
    ("America/Manaus", "2026-02-01T03:59:59.999Z"),
    ("America/Rio_Branco", "2026-02-01T04:59:59.999Z"),
])
def test_valid_until_is_235959999_local_of_last_day(tz, expected):
    assert ms_of(end_of_local_day_utc(date(2026, 1, 31), tz)) == zms(expected)


def _media(i: int) -> MediaRef:
    return MediaRef(f"00000000-0000-4000-8000-{i:012d}", f"{i:064x}", 1000 + i, "video/mp4", 1920, 1080, 30000)


def _inputs(lines_creatives=True, **over) -> ManifestInputs:
    line = Line("L1", "camp-1", "adv-1", 1, FREQ_PER_HOUR, 2, D, D, 127, ((15 * 60, 16 * 60),), (), 600)
    plans = Packer("scr-1", TZ, strict=True).pack_days([(D, R22)], [line])
    vf = ms_of(start_of_local_day_utc(D, TZ))
    vu = ms_of(end_of_local_day_utc(D, TZ))
    ext = CreativeRef("cr-1", "RD-AAAAAA", "EXTERNAL", _media(1), 30000, "camp-1", "adv-1", vf, vu)
    internal = [CreativeRef("cr-7", "RD-BBBBBB", "INTERNAL", _media(7), 60000)]
    fb = [CreativeRef("cr-9", "RD-CCCCCC", "FALLBACK", _media(9), 30000)]
    h_from = ms_of(start_of_local_day_utc(D, TZ))
    kw = dict(screen_id="scr-1", screen_code="TEL-GO-AAAA", timezone=TZ, rotation=R22, anchor_ms=None,
              horizon_from_ms=h_from, horizon_to_ms=h_from + 7 * 86_400_000,
              settings=default_settings(panel_url="http://painel"), day_plans=plans,
              line_creatives={"L1": [ext]} if lines_creatives else {}, internal_items=internal, fallback_items=fb,
              emergencies=[], revoked=[], version=3, manifest_id="m-1", generated_at_ms=123)
    kw.update(over)
    return ManifestInputs(**kw)


REQUIRED = {"schema", "manifest_id", "version", "screen_id", "screen_code", "generated_at_ms", "timezone", "rotation",
            "anchor_ms", "horizon", "settings", "media", "creatives", "internal_pattern", "fallback",
            "external_plan", "emergency", "revoked"}


def test_manifest_has_every_protocol_key_and_only_integers():
    m = assemble_manifest(_inputs())
    assert set(m) == REQUIRED
    assert m["schema"] == "rd.manifest/1"
    assert m["rotation"] == {"internal_ms": 120000, "external_ms": 120000, "cycle_ms": 240000, "unit_ms": 30000,
                             "policy_id": R22.policy_id}
    assert m["horizon"]["repeat_period_ms"] == 7 * 86_400_000
    P.canonical_bytes(m)  # RDJ1: lança se houver float


def test_external_plan_entries_labels_and_internal_fill():
    m = assemble_manifest(_inputs())
    cr = m["creatives"]
    ext_keys = [k for k, v in cr.items() if v["kind"] == "EXTERNAL"]
    assert len(ext_keys) == 1 and cr[ext_keys[0]]["label"] == "Publicidade"
    assert {v["label"] for v in cr.values() if v["kind"] == "INTERNAL"} == {"Conteúdo do estabelecimento"}
    assert {v["label"] for v in cr.values() if v["kind"] == "FALLBACK"} == {"RD Mídia Indoor"}
    ext_entries = [(b, e) for b, es in m["external_plan"].items() for e in es if e["c"] == ext_keys[0]]
    assert len(ext_entries) == 2
    # todo bucket com anúncio: unidades restantes preenchidas com INTERNO (60 s) — 30+60 de 120 s
    for b, e in ext_entries:
        entries = m["external_plan"][b]
        assert entries[0]["u"] == 0 and entries[0]["d"] == 30000
        assert [cr[x["c"]]["kind"] for x in entries] == ["EXTERNAL", "INTERNAL"]
        assert entries[1]["u"] == 1 and entries[1]["d"] == 60000
        assert e["i"] == insertion_id("scr-1", int(b), 0)
    # sobra de buckets sem venda também vira interno (2 peças de 60 s por bloco de 120 s)
    other = [es for b, es in m["external_plan"].items() if all(cr[x["c"]]["kind"] == "INTERNAL" for x in es)]
    assert other and all([x["u"] for x in es] == [0, 2] for es in other)
    assert m["internal_pattern"] == [[{"c": k, "d": 60000}, {"c": k, "d": 60000}]
                                     for k in [k for k, v in cr.items() if v["kind"] == "INTERNAL"]]


def test_sold_units_without_playable_creative_return_to_internal():
    m = assemble_manifest(_inputs(lines_creatives=False))
    assert all(v["kind"] != "EXTERNAL" for v in m["creatives"].values())


def test_media_map_sha256_and_keys():
    m = assemble_manifest(_inputs())
    for k, v in m["media"].items():
        assert k.startswith("m") and len(v["sha256"]) == 64 and isinstance(v["size"], int)
    for v in m["creatives"].values():
        assert v["media"] in m["media"]


def test_determinism_content_view_ignores_volatile_fields():
    a = assemble_manifest(_inputs(version=1, manifest_id="a", generated_at_ms=1))
    b = assemble_manifest(_inputs(version=9, manifest_id="b", generated_at_ms=999))
    assert P.canonical_bytes(content_view(a)) == P.canonical_bytes(content_view(b))
    assert P.canonical_bytes(a) != P.canonical_bytes(b)


def test_emergency_and_revoked_and_resolve_roundtrip():
    em = EmergencyRef("em-1", CreativeRef("cr-e", "RD-DDDDDD", "INSTITUTIONAL", _media(5), 30000), 1000, 2000)
    m = assemble_manifest(_inputs(emergencies=[em], revoked=["cr-x", "cr-a"]))
    assert m["revoked"] == ["cr-a", "cr-x"]
    assert m["emergency"][0]["id"] == "em-1" and m["creatives"][m["emergency"][0]["c"]]["kind"] == "INSTITUTIONAL"
    # o plano gerado é consumível pelo resolve() de referência
    anchor = zms("2026-10-07T18:00:00Z")  # 15:00 local
    ready = {"revoked": [], "media_ready": list(m["media"])}
    seen = set()
    for i in range(15):
        s = anchor + i * 240000 + 120000
        seen.add(resolve(m, anchor, ready, s).creative_key)
    ext = [k for k, v in m["creatives"].items() if v["kind"] == "EXTERNAL"][0]
    assert ext in seen


def test_signature_roundtrip_with_protocol_primitives():
    m = assemble_manifest(_inputs())
    key = P.generate_private_key()
    sig = P.sign_es256(key, m)
    assert P.verify_es256(P.public_spki_b64u(key), m, sig)
    m2 = dict(m, version=m["version"] + 1)
    assert not P.verify_es256(P.public_spki_b64u(key), m2, sig)
