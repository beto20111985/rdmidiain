"""Vetores de conformidade RDPP/1 (§9): regeneração determinística == arquivos publicados,
mais verificações INDEPENDENTES (calculadas à mão) para não aceitar "o que o gerador
disser" — RDJ1, ES256 e pontos-chave do ``resolve()``."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path

import pytest

from app.modules.playback import protocol as proto
from app.modules.playback import vectors

FILES = vectors.build_all()


def _vectors_dir() -> Path | None:
    for cand in (os.environ.get("RD_VECTORS_DIR"), "/protocol/vectors",
                 str(Path(__file__).resolve().parents[3] / "protocol" / "vectors")):
        if cand and Path(cand, "canonical_json.json").exists():
            return Path(cand)
    return None


def test_generation_is_deterministic():
    again = vectors.build_all()
    assert {k: vectors.dumps(v) for k, v in again.items()} == {k: vectors.dumps(v) for k, v in FILES.items()}


def test_regenerated_matches_lock():
    lock = json.loads(vectors.LOCK_PATH.read_text(encoding="utf-8"))
    assert vectors.lock_of(FILES) == lock, "vetores mudaram: regenere protocol/vectors e o lock (mudança de protocolo?)"


def test_regenerated_matches_published_files_when_available():
    d = _vectors_dir()
    if d is None:
        # sem o diretório montado no container: o lock (acima) é a prova; o host confere com sha256sum
        lock = json.loads(vectors.LOCK_PATH.read_text(encoding="utf-8"))
        assert set(lock) == set(vectors.FILES)
        return
    for name, obj in FILES.items():
        assert (d / name).read_text(encoding="utf-8") == vectors.dumps(obj), name


def test_files_have_required_envelope():
    for name, doc in FILES.items():
        assert doc["version"] == 1 and doc["generated_by"] and isinstance(doc["cases"], list) and doc["cases"], name


def test_canonical_vectors_independent_checks():
    cases = {c["name"]: c for c in FILES["canonical_json.json"]["cases"]}
    assert cases["chaves_ordenadas"]["canonical"] == '{"":7,"B":3,"_":4,"a":2,"a0":6,"aa":5,"b":1}'
    assert cases["controles_u00xx_minusculo"]["canonical"] == '{"s":"\\u0000\\u0001\\u001f\\u000b\\u001e"}'
    assert cases["escapes_minimos"]["canonical"] == '{"s":"aspas\\" barra\\\\ bs\\b ff\\f nl\\n cr\\r tab\\t"}'
    assert cases["nao_ascii_sem_escape"]["canonical"] == '{"s":"São Paulo — Goiânia ✓ 😀"}'
    assert cases["del_e_barra_nao_escapados"]["canonical"] == '{"s":"\x7f/</script>"}'
    assert cases["chaves_unicode_por_ponto_de_codigo"]["canonical"].startswith('{"e":3,"z":1,"~":5,"É":4,"é":2,"ü":6}')
    errs = [c for c in cases.values() if "error" in c]
    assert errs and all(c["error"] == "FLOAT_NOT_ALLOWED" for c in errs)
    for c in cases.values():
        if "canonical" in c:
            assert proto.canonical_json(c["input"]) == c["canonical"]
            assert json.loads(c["canonical"]) == c["input"]


def test_es256_vectors_verify_and_roundtrip():
    doc = FILES["es256_signatures.json"]
    for c in doc["cases"]:
        assert proto.canonical_json(c["message"]) == c["canonical"]
        assert proto.verify_bytes(c["public_spki_b64u"], c["canonical"].encode(), c["signature_b64u"]) is c["valid"], c["name"]
    key = proto.load_private_key_pkcs8_b64u(doc["test_key"]["private_pkcs8_b64u"])
    assert proto.public_spki_b64u(key) == doc["test_key"]["public_spki_b64u"]
    msg = {"x": 1, "purpose": "teste"}
    sig = proto.sign_es256(key, msg)  # aleatório (produção)
    assert proto.verify_es256(doc["test_key"]["public_spki_b64u"], msg, sig)
    assert "SOMENTE TESTE" in doc["test_key"]["note"]


def test_cycle_resolution_hand_computed_points():
    """Pontos calculados à mão a partir das regras §5.1/§5.2/§8.2 (independe do resolve())."""
    cases = {c["name"]: c for c in FILES["cycle_resolution.json"]["cases"]}
    c = cases["ancora_0807_consultas_0847_084910"]
    A = c["anchor_ms"]
    assert A == vectors.ms_of("2026-10-12T11:07:00Z") and c["manifest"]["anchor_ms"] is None
    q = {x["t_ms"] - A: x["expected"] for x in c["queries"]}
    # 08:47:00 → d = 40 min, k = 10, fase 0 → interno, padrão[10 mod 2 = 0] = c7 (60 s)
    assert q[40 * 60_000] == {"block": "INTERNAL", "kind": "ITEM", "creative_key": "c7", "start_ms": A + 2_400_000,
                              "end_ms": A + 2_460_000}
    # 08:49:10 → fase 130 s ≥ I → externo; s = A + 10C + I = 08:49:00; c2 u=0 d=60 s
    assert q[42 * 60_000 + 10_000] == {"block": "EXTERNAL", "kind": "ITEM", "creative_key": "c2",
                                       "start_ms": A + 2_520_000, "end_ms": A + 2_580_000}
    # 08:50:10 → lacuna [08:50:00, 08:50:30) → fallback índice floor((s−A)/U) mod 2 = 86 mod 2 = 0 → f1
    assert q[43 * 60_000 + 10_000]["creative_key"] == "f1" and q[43 * 60_000 + 10_000]["kind"] == "ITEM"

    c = cases["revogado_e_midia_ausente"]
    A = c["anchor_ms"]
    q = {x["t_ms"] - A: x["expected"] for x in c["queries"]}
    # c2 revogado [120 s,180 s): f1 (30 s) cabe; f2 (60 s) não cabe no resto de 30 s → FILLER f2 sem PoP
    assert (q[121_000]["creative_key"], q[121_000]["kind"]) == ("f1", "ITEM")
    assert q[151_000] == {"block": "EXTERNAL", "kind": "FILLER", "creative_key": "f2", "start_ms": A + 150_000,
                          "end_ms": A + 180_000}

    c = cases["emergencia"]
    A = c["anchor_ms"]
    f = A + 50_000
    q = {x["t_ms"]: x["expected"] for x in c["queries"]}
    assert q[f]["block"] == "EMERGENCY" and q[f]["creative_key"] == "c6" and q[f]["end_ms"] == f + 30_000
    # última repetição [f+90 s, f+100 s) não cabe inteira → FILLER com o próprio criativo
    assert q[f + 95_000] == {"block": "EMERGENCY", "kind": "FILLER", "creative_key": "c6", "start_ms": f + 90_000,
                             "end_ms": f + 100_000}

    c = cases["fora_do_horizonte_repeticao_semanal"]
    A = c["anchor_ms"]
    later = c["queries"][0]
    # b = A + 7d + C → b' = A + C (n = 1); c2 em u=0 toca de s a s+60 s
    assert later["expected"]["creative_key"] == "c2" and later["expected"]["start_ms"] == A + vectors.WEEK_MS + 240_000 + 120_000

    c = cases["fallback_vazio_e_interno_vazio"]
    assert all(x["expected"]["kind"] == "FILLER" and x["expected"]["creative_key"] is None for x in c["queries"])


def test_cycle_vectors_reproduce_with_reference_resolve():
    from app.modules.scheduler.resolve import resolve

    for c in FILES["cycle_resolution.json"]["cases"]:
        for qq in c["queries"]:
            assert resolve(c["manifest"], c["anchor_ms"], c["local_state"], qq["t_ms"]).to_dict() == qq["expected"], c["name"]


def test_pop_vectors_shape_and_signatures():
    doc = FILES["pop_validation.json"]
    spki = doc["device_key"]["public_spki_b64u"]
    names = {c["name"] for c in doc["cases"]}
    for required in ("valido_30_de_30", "invalido_29999_de_30000", "invalido_25_de_30_interrompido",
                     "duplicado_mesmo_conteudo", "nonce_repetido", "seq_rollback", "assinatura_errada",
                     "dispositivo_errado", "criativo_fora_do_manifesto", "campanha_errada", "manifesto_errado",
                     "relogio_no_futuro", "offline_sincronizado_6_dias_depois_valido", "sobreposicao_de_intervalos",
                     "volume_impossivel_na_tela"):
        assert required in names, required
    for c in doc["cases"]:
        if c["name"] == "schema_invalido_chave_ausente":
            continue
        assert proto.canonical_json(c["event"]) == c["canonical"]
        good = c["expected"]["reason"] not in ("BAD_SIGNATURE",)
        assert proto.verify_es256(spki, c["event"], c["signature_b64u"]) is good, c["name"]
    by = {c["name"]: c["expected"] for c in doc["cases"]}
    assert by["invalido_29999_de_30000"] == {"status": "ACCEPTED", "valid": False, "reason": "INCOMPLETE_DURATION", "flags": []}
    assert by["valido_30_de_30"]["valid"] is True
    # prev_hash = sha256 hex do RDJ1 do evento anterior
    e1 = doc["batch_cases"][0]["items"][1]["event"]
    e2 = doc["batch_cases"][0]["items"][0]["event"]
    assert e2["prev_hash"] == hashlib.sha256(proto.canonical_bytes(e1)).hexdigest()


@pytest.mark.parametrize("bad", ["", "AAAA", "x" * 300])
def test_spki_validation_rejects_garbage(bad):
    with pytest.raises(proto.ProtocolCryptoError):
        proto.load_public_spki(bad)


def test_float_rejected_and_bool_is_not_int():
    with pytest.raises(proto.CanonicalError) as e:
        proto.canonical_json({"a": 1.0})
    assert e.value.code == "FLOAT_NOT_ALLOWED"
    assert proto.canonical_json({"t": True, "n": 1}) == '{"n":1,"t":true}'
