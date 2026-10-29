"""
RD Mídia Indoor — Gerador DETERMINÍSTICO dos vetores de conformidade RDPP/1 (§9).

    docker exec -w /app rd-backend python -m app.modules.playback.vectors --out /app/.vectors
    (depois copiar para ``protocol/vectors/``; ou ``--check DIR`` para comparar)

Arquivos: ``canonical_json.json``, ``es256_signatures.json``, ``cycle_resolution.json``,
``pop_validation.json`` (todos ``{"version": 1, "generated_by", "cases": [...]}``).

Determinismo: chaves de TESTE derivadas de rótulos fixos (escalar = SHA-256 do rótulo),
assinaturas ECDSA determinísticas (RFC 6979 — somente aqui; em produção o ECDSA é
aleatório e as implementações VERIFICAM as assinaturas dadas), UUIDs por uuid5, tempos
fixos. AS CHAVES DESTE ARQUIVO SÃO SOMENTE PARA TESTE — nunca usar em produção.

Fontes de verdade usadas:
- RDJ1/ES256: ``app.modules.playback.protocol`` (canônico);
- ``resolve()``: ``app.modules.scheduler.resolve`` (W-INV, implementação de referência);
- validação PoP: ``app.modules.playback.ingest`` (``precheck``/``rule_reasons`` — as MESMAS
  funções da ingestão real; o teste ``test_vectors_pop_db`` reexecuta cada caso pelo
  caminho com PostgreSQL e confirma o mesmo resultado).
"""
from __future__ import annotations

import argparse
import copy
import hashlib
import json
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.asymmetric.utils import encode_dss_signature

from app.modules.playback import ingest
from app.modules.playback import protocol as proto

VERSION = 1
GENERATED_BY = "backend/app/modules/playback/vectors.py (RDPP/1 — implementação de referência)"
NS = uuid.UUID("2b0f6c55-9d3e-4f1a-8c7b-5e4d3c2b1a09")
P256_N = 0xFFFFFFFF00000000FFFFFFFFFFFFFFFFBCE6FAADA7179E84F3B9CAC2FC632551
WEEK_MS = 7 * 24 * 3600 * 1000
MAX_VALID_UNTIL_MS = 253402300799999
FILES = ("canonical_json.json", "es256_signatures.json", "cycle_resolution.json", "pop_validation.json")


def uid(label: str) -> str:
    return str(uuid.uuid5(NS, label))


def ms_of(iso: str) -> int:
    return int(datetime.fromisoformat(iso.replace("Z", "+00:00")).astimezone(timezone.utc).timestamp() * 1000)


def test_key(label: str) -> ec.EllipticCurvePrivateKey:
    scalar = int.from_bytes(hashlib.sha256(f"rd-vectors/{label}".encode()).digest(), "big") % (P256_N - 1) + 1
    return ec.derive_private_key(scalar, ec.SECP256R1())


def sign(key: ec.EllipticCurvePrivateKey, obj: Any) -> str:
    return proto.sign_es256(key, obj, deterministic=True)


def key_block(key: ec.EllipticCurvePrivateKey) -> dict:
    return {
        "note": "SOMENTE TESTE — chave pública/privada de vetores de conformidade; NUNCA usar em produção",
        "private_pkcs8_b64u": proto.private_pkcs8_b64u(key),
        "public_spki_b64u": proto.public_spki_b64u(key),
    }


def doc(cases: list, **extra: Any) -> dict:
    out: dict = {"version": VERSION, "generated_by": GENERATED_BY}
    out.update(extra)
    out["cases"] = cases
    return out


# ═══ 1. JSON canônico (RDJ1) ═════════════════════════════════════════════════

def _materialize(v: Any) -> Any:
    """Converte marcadores ``{"$float": "1.5"}`` em float real (para testar a rejeição)."""
    if isinstance(v, dict):
        if set(v) == {"$float"}:
            return float(v["$float"])
        return {k: _materialize(x) for k, x in v.items()}
    if isinstance(v, list):
        return [_materialize(x) for x in v]
    return v


def build_canonical() -> dict:
    ok_inputs = [
        ("objeto_vazio", {}),
        ("lista_vazia", []),
        ("string_vazia", ""),
        ("literais", [True, False, None]),
        ("inteiros", [0, -1, 1, 30000, 1791380000000, 9007199254740991, -9007199254740991]),
        ("chaves_ordenadas", {"b": 1, "a": 2, "B": 3, "_": 4, "aa": 5, "a0": 6, "": 7}),
        ("chaves_unicode_por_ponto_de_codigo", {"z": 1, "é": 2, "e": 3, "É": 4, "~": 5, "ü": 6}),
        ("aninhado", {"z": {"y": [3, {"b": None, "a": [1, 2]}]}, "a": {"c": True}}),
        ("escapes_minimos", {"s": "aspas\" barra\\ bs\b ff\f nl\n cr\r tab\t"}),
        ("controles_u00xx_minusculo", {"s": "\u0000\u0001\u001f\u000b\u001e"}),
        ("del_e_barra_nao_escapados", {"s": "\u007f/</script>"}),
        ("nao_ascii_sem_escape", {"s": "São Paulo — Goiânia ✓ 😀"}),
        (
            "evento_pop_exemplo",
            {
                "schema": "rd.pop/1",
                "playback_uuid": uid("pop-ex"),
                "seq": 1017,
                "offline": False,
                "campaign_id": None,
                "started_at_ms": 1791381600000,
            },
        ),
        ("mensagem_auth", {"challenge": "q1w2e3", "device_id": uid("dev"), "purpose": proto.PURPOSE_AUTH}),
    ]
    cases = [{"name": n, "input": v, "canonical": proto.canonical_json(v)} for n, v in ok_inputs]
    err_inputs = [
        ("float_simples", {"$float": "1.5"}),
        ("float_integral", {"a": {"$float": "1.0"}}),
        ("float_aninhado", {"a": [1, {"b": {"$float": "0.1"}}]}),
        ("float_negativo_zero", [{"$float": "-0.0"}]),
        ("float_expoente", {"x": {"$float": "3e10"}}),
    ]
    for n, v in err_inputs:
        try:
            proto.canonical_json(_materialize(v))
            raise AssertionError(f"float aceito no caso {n}")
        except proto.CanonicalError as exc:
            cases.append({"name": n, "input": v, "error": exc.code})
    return doc(cases, float_marker='{"$float": "<texto>"} representa um número de ponto flutuante real')


# ═══ 2. ES256 ════════════════════════════════════════════════════════════════

def _raw(sig_b64u: str) -> tuple[int, int]:
    raw = proto.b64u_decode(sig_b64u)
    return int.from_bytes(raw[:32], "big"), int.from_bytes(raw[32:], "big")


def _enc(r: int, s: int) -> str:
    return proto.b64u_encode(r.to_bytes(32, "big") + s.to_bytes(32, "big"))


def build_es256() -> dict:
    k = test_key("es256-test-key")
    other = test_key("es256-other-key")
    spki = proto.public_spki_b64u(k)
    other_spki = proto.public_spki_b64u(other)
    msgs = {
        "auth": {"challenge": proto.b64u_encode(hashlib.sha256(b"challenge-1").digest()), "device_id": uid("device-1"),
                 "purpose": proto.PURPOSE_AUTH},
        "pairing": {"pairing_request_id": uid("pairing-1"), "purpose": proto.PURPOSE_PAIRING_RESULT, "ts_ms": 1791381600000},
        "update": {"platform": "android", "purpose": proto.PURPOSE_APP_UPDATE, "sha256": hashlib.sha256(b"apk").hexdigest(),
                   "size": 18874368, "version": "1.2.0"},
        "unicode": {"texto": "Publicidade — RD-A7M2P9 ✓", "n": -42, "lista": [1, None, True]},
    }
    cases = []
    for name, m in msgs.items():
        cases.append({"name": f"valida_{name}", "message": m, "canonical": proto.canonical_json(m),
                      "signature_b64u": sign(k, m), "public_spki_b64u": spki, "valid": True})
    m = msgs["auth"]
    good = sign(k, m)
    r, s = _raw(good)
    tampered = dict(m, device_id=uid("device-2"))
    cases += [
        {"name": "mensagem_adulterada", "message": tampered, "canonical": proto.canonical_json(tampered),
         "signature_b64u": good, "public_spki_b64u": spki, "valid": False},
        {"name": "chave_errada", "message": m, "canonical": proto.canonical_json(m), "signature_b64u": good,
         "public_spki_b64u": other_spki, "valid": False},
        {"name": "assinada_por_outra_chave", "message": m, "canonical": proto.canonical_json(m),
         "signature_b64u": sign(other, m), "public_spki_b64u": spki, "valid": False},
        {"name": "high_s_maleavel_aceita", "message": m, "canonical": proto.canonical_json(m),
         "signature_b64u": _enc(r, P256_N - s), "public_spki_b64u": spki, "valid": True},
        {"name": "s_zero", "message": m, "canonical": proto.canonical_json(m), "signature_b64u": _enc(r, 0),
         "public_spki_b64u": spki, "valid": False},
        {"name": "r_maior_ou_igual_n", "message": m, "canonical": proto.canonical_json(m),
         "signature_b64u": _enc(P256_N, s), "public_spki_b64u": spki, "valid": False},
        {"name": "truncada_63_bytes", "message": m, "canonical": proto.canonical_json(m),
         "signature_b64u": proto.b64u_encode(proto.b64u_decode(good)[:63]), "public_spki_b64u": spki, "valid": False},
        {"name": "formato_der_nao_aceito", "message": m, "canonical": proto.canonical_json(m),
         "signature_b64u": proto.b64u_encode(encode_dss_signature(r, s)), "public_spki_b64u": spki, "valid": False},
        {"name": "base64url_com_padding_rejeitado", "message": m, "canonical": proto.canonical_json(m),
         "signature_b64u": good + "==" if len(good) % 4 else good + "=", "public_spki_b64u": spki, "valid": False},
    ]
    for c in cases:  # autoverificação: o gerador nunca publica um vetor que ele mesmo reprova
        got = proto.verify_bytes(c["public_spki_b64u"], c["canonical"].encode("utf-8"), c["signature_b64u"])
        if got != c["valid"]:
            raise AssertionError(f"es256 {c['name']}: esperado {c['valid']}, obtido {got}")
    return doc(cases, test_key=key_block(k), other_key=key_block(other))


# ═══ 3. Resolução do ciclo (resolve) ═════════════════════════════════════════

T0 = ms_of("2026-10-12T11:00:00Z")  # 08:00 em America/Sao_Paulo (UTC−3)
SEC = 1000
MIN = 60 * SEC


def _media(n: int, mime: str = "video/mp4", duration: Optional[int] = None) -> dict:
    return {"media_id": uid(f"media-{n}"), "sha256": hashlib.sha256(f"media-{n}".encode()).hexdigest(), "size": 1_000_000 + n,
            "mime": mime, "width": 1920, "height": 1080, "duration_ms": duration}


def _cr(label: str, code: str, kind: str, media: str, duration: int, *, campaign: Optional[str] = None,
        vf: int = 0, vu: int = MAX_VALID_UNTIL_MS) -> dict:
    labels = {"EXTERNAL": "Publicidade", "INSTITUTIONAL": "Publicidade", "INTERNAL": "Conteúdo do estabelecimento",
              "FALLBACK": "RD Mídia Indoor"}
    return {"creative_id": uid(f"creative-{label}"), "code": code, "kind": kind,
            "campaign_id": uid(f"campaign-{campaign}") if campaign else None,
            "advertiser_id": uid(f"advertiser-{campaign}") if campaign else None,
            "media": media, "duration_ms": duration, "valid_from_ms": vf, "valid_until_ms": vu, "label": labels[kind]}


def base_manifest(*, internal_ms: int = 120000, external_ms: int = 120000, anchor: Optional[int] = T0,
                  version: int = 42, screen: str = "screen-1") -> dict:
    cycle = internal_ms + external_ms
    media = {f"m{i}": _media(i, duration=None if i in (7,) else 30000) for i in range(1, 10)}
    creatives = {
        "c1": _cr("c1", "RD-A7M2P9", "EXTERNAL", "m1", 120000, campaign="1"),
        "c2": _cr("c2", "RD-B3C4D5", "EXTERNAL", "m2", 60000, campaign="2"),
        "c3": _cr("c3", "RD-E6F7G8", "EXTERNAL", "m3", 30000, campaign="3"),
        "c4": _cr("c4", "RD-H9J2M3", "EXTERNAL", "m4", 60000, campaign="4"),
        "c5": _cr("c5", "RD-N4P5Q6", "EXTERNAL", "m5", 30000, campaign="5", vf=T0, vu=T0 + 2 * cycle),
        "c6": _cr("c6", "RD-U2V3Y4", "INSTITUTIONAL", "m6", 30000),
        "c7": _cr("c7", "RD-R7S8T9", "INTERNAL", "m7", 60000),
        "c8": _cr("c8", "RD-Z5A6B7", "INTERNAL", "m8", 60000),
        "f1": _cr("f1", "RD-F2A3B4", "FALLBACK", "m9", 30000),
        "f2": _cr("f2", "RD-G5H6J7", "FALLBACK", "m9", 60000),
    }
    return {
        "schema": "rd.manifest/1",
        "manifest_id": uid(f"manifest-{screen}-{version}"),
        "version": version,
        "screen_id": uid(screen),
        "screen_code": "TEL-GO-7Q3M",
        "generated_at_ms": T0 - 3600 * SEC,
        "timezone": "America/Sao_Paulo",
        "rotation": {"internal_ms": internal_ms, "external_ms": external_ms, "cycle_ms": cycle, "unit_ms": 30000,
                     "policy_id": uid("policy-default")},
        "anchor_ms": anchor,
        "horizon": {"from_ms": T0 - 24 * 3600 * SEC, "to_ms": T0 - 24 * 3600 * SEC + WEEK_MS, "repeat_period_ms": WEEK_MS},
        "settings": {"image_default_ms": 30000, "fade_ms": 400, "orientation": "LANDSCAPE",
                     "footer": {"enabled": True, "height_permille": 60}, "offline_alert_days": [7, 28],
                     "heartbeat_interval_ms": 30000, "telemetry_interval_ms": 300000, "panel_url": "https://painel.exemplo.test"},
        "media": media,
        "creatives": creatives,
        "internal_pattern": [[{"c": "c7", "d": 60000}, {"c": "c8", "d": 60000}], [{"c": "c8", "d": 60000}, {"c": "c7", "d": 60000}]],
        "fallback": [{"c": "f1", "d": 30000}, {"c": "f2", "d": 60000}],
        "external_plan": {},
        "emergency": [],
        "revoked": [],
    }


def _ins(screen: str, b: int, u: int) -> str:
    return uid(f"ins-{screen}-{b}-{u}")


def _plan(m: dict, bucket: int, entries: list[tuple[str, int, int]], screen: str = "screen-1") -> None:
    m["external_plan"][str(bucket)] = [{"i": _ins(screen, bucket, u), "c": c, "u": u, "d": d} for c, u, d in entries]


def _ext_bucket(anchor: int, k: int, internal_ms: int, cycle_ms: int) -> int:
    s = anchor + k * cycle_ms + internal_ms
    return (s // cycle_ms) * cycle_ms


ALL_MEDIA = [f"m{i}" for i in range(1, 10)]


def _resolve_case(name: str, manifest: dict, anchor: int, local_state: dict, ts: list[int], note: str = "") -> dict:
    from app.modules.scheduler.resolve import resolve

    queries = [{"t_ms": t, "expected": resolve(manifest, anchor, local_state, t).to_dict()} for t in ts]
    c = {"name": name, "manifest": manifest, "anchor_ms": anchor, "local_state": local_state, "queries": queries}
    if note:
        c["note"] = note
    return c


def build_cycle() -> dict:
    cases = []
    ls_all = {"revoked": [], "media_ready": list(ALL_MEDIA)}

    # 2+2 básico (âncora alinhada ao ciclo)
    m = base_manifest()
    C, I = 240000, 120000
    _plan(m, _ext_bucket(T0, 0, I, C), [("c1", 0, 120000)])
    _plan(m, _ext_bucket(T0, 1, I, C), [("c2", 0, 60000), ("c4", 2, 60000)])
    _plan(m, _ext_bucket(T0, 2, I, C), [("c3", 1, 30000)])
    # ciclo 3 sem chave → bloco externo inteiro em fallback
    ts = [T0, T0 + 59999, T0 + 60000, T0 + 119999, T0 + 120000, T0 + 239999,
          T0 + C, T0 + C + 60000, T0 + C + I, T0 + C + I + 60000, T0 + C + I + 119999,
          T0 + 2 * C + I, T0 + 2 * C + I + 30000, T0 + 2 * C + I + 60000, T0 + 2 * C + I + 90000,
          T0 + 3 * C + I, T0 + 3 * C + I + 30000, T0 + 3 * C + I + 90000]
    cases.append(_resolve_case("2+2_basico", m, T0, ls_all, ts,
                               "interno alterna padrões por ciclo (k mod 2); 120 s ocupa o bloco externo; lacunas → fallback"))

    # 3+1
    m = base_manifest(internal_ms=180000, external_ms=60000)
    m["internal_pattern"] = [[{"c": "c7", "d": 60000}, {"c": "c8", "d": 60000}, {"c": "c7", "d": 60000}]]
    C, I = 240000, 180000
    _plan(m, _ext_bucket(T0, 0, I, C), [("c2", 0, 60000)])
    _plan(m, _ext_bucket(T0, 1, I, C), [("c3", 0, 30000), ("c6", 1, 30000)])
    ts = [T0, T0 + 120000, T0 + 179999, T0 + 180000, T0 + 239999, T0 + C + I, T0 + C + I + 30000, T0 + 2 * C + I + 15000]
    cases.append(_resolve_case("3+1", m, T0, ls_all, ts))

    # âncora 08:07 local (não alinhada ao ciclo): 08:47 e 08:49:10
    a807 = ms_of("2026-10-12T11:07:00Z")
    m = base_manifest(anchor=None)
    C, I = 240000, 120000
    b = _ext_bucket(a807, 10, I, C)
    _plan(m, b, [("c2", 0, 60000), ("c3", 3, 30000)])
    _plan(m, _ext_bucket(a807, 0, I, C), [("c4", 0, 60000)])
    ts = [a807, a807 + 119999, a807 + I, ms_of("2026-10-12T11:47:00Z"), ms_of("2026-10-12T11:47:30Z"),
          ms_of("2026-10-12T11:49:00Z"), ms_of("2026-10-12T11:49:10Z"), ms_of("2026-10-12T11:50:10Z"),
          ms_of("2026-10-12T11:50:40Z")]
    cases.append(_resolve_case("ancora_0807_consultas_0847_084910", m, a807, ls_all, ts,
                               "manifest.anchor_ms = null; âncora local 08:07 (America/Sao_Paulo); bucket = floor(s/C)·C"))

    # fora do horizonte (repetição semanal) e horizonte curto (não cai → fallback)
    m = base_manifest()
    C, I = 240000, 120000
    _plan(m, _ext_bucket(T0, 1, I, C), [("c2", 0, 60000), ("c4", 2, 60000)])
    later = T0 + WEEK_MS + C + I + 10000  # mesmo instante na semana seguinte → b' = b − 7 d
    earlier = T0 - WEEK_MS + C + I + 70000  # semana anterior ao horizonte → b' = b + 7 d
    cases.append(_resolve_case("fora_do_horizonte_repeticao_semanal", m, T0, ls_all, [later, earlier, later + 60000]))
    m2 = copy.deepcopy(m)
    m2["horizon"] = {"from_ms": T0, "to_ms": T0 + 24 * 3600 * SEC, "repeat_period_ms": WEEK_MS}
    cases.append(_resolve_case("horizonte_curto_sem_correspondencia", m2, T0, ls_all,
                               [T0 + 2 * 24 * 3600 * SEC + C + I + 10000]))

    # vencido / ainda não vigente (validade no INÍCIO do item, com o t real)
    m = base_manifest()
    C, I = 240000, 120000
    _plan(m, _ext_bucket(T0, 1, I, C), [("c5", 0, 30000)])  # início T0+C+I < vu=T0+2C → toca
    _plan(m, _ext_bucket(T0, 2, I, C), [("c5", 0, 30000)])  # início T0+2C+I ≥ vu → fallback
    m["creatives"]["c3"]["valid_from_ms"] = T0 + 3 * C + I + 1
    _plan(m, _ext_bucket(T0, 3, I, C), [("c3", 0, 30000)])  # ainda não vigente no início
    cases.append(_resolve_case("vencido_e_nao_vigente", m, T0, ls_all,
                               [T0 + C + I + 1000, T0 + 2 * C + I + 1000, T0 + 3 * C + I + 1000]))

    # revogado (manifesto e local) e mídia ausente
    m = base_manifest()
    C, I = 240000, 120000
    _plan(m, _ext_bucket(T0, 0, I, C), [("c2", 0, 60000), ("c4", 2, 60000)])
    _plan(m, _ext_bucket(T0, 1, I, C), [("c3", 0, 30000), ("c6", 1, 30000)])
    m["revoked"] = [m["creatives"]["c2"]["creative_id"]]
    ls = {"revoked": [m["creatives"]["c4"]["creative_id"]], "media_ready": [x for x in ALL_MEDIA if x != "m3"]}
    cases.append(_resolve_case("revogado_e_midia_ausente", m, T0, ls,
                               [T0 + I + 1000, T0 + I + 31000, T0 + I + 61000, T0 + I + 91000, T0 + C + I + 5000,
                                T0 + C + I + 35000, T0 + C + I + 65000],
                               "c2 revogado no manifesto, c4 revogado localmente (kill), mídia m3 ausente"))

    # emergência (primeira ativa vence; última repetição parcial vira FILLER)
    m = base_manifest()
    C, I = 240000, 120000
    _plan(m, _ext_bucket(T0, 0, I, C), [("c1", 0, 120000)])
    f = T0 + 50000
    m["emergency"] = [
        {"id": uid("em-1"), "c": "c6", "from_ms": f, "until_ms": f + 100000},
        {"id": uid("em-2"), "c": "c3", "from_ms": f, "until_ms": f + 400000},
    ]
    cases.append(_resolve_case("emergencia", m, T0, ls_all,
                               [f - 1, f, f + 29999, f + 30000, f + 95000, f + 99999, f + 100000, f + 200000,
                                f + 400000]))

    # fallback vazio e padrão interno vazio
    m = base_manifest()
    m["fallback"] = []
    m["internal_pattern"] = []
    cases.append(_resolve_case("fallback_vazio_e_interno_vazio", m, T0, ls_all, [T0, T0 + 130000]))
    m = base_manifest()
    m["internal_pattern"] = []
    cases.append(_resolve_case("interno_vazio_preenche_com_fallback", m, T0, ls_all,
                               [T0, T0 + 30000, T0 + 60000, T0 + 90000, T0 + 119999]))

    # entradas sobrepostas / extrapolando o bloco; item interno que estoura o bloco
    m = base_manifest()
    C, I = 240000, 120000
    _plan(m, _ext_bucket(T0, 0, I, C), [("c2", 0, 60000), ("c4", 1, 60000), ("c3", 3, 60000), ("c6", 4, 30000)])
    m["internal_pattern"] = [[{"c": "c7", "d": 60000}, {"c": "c1", "d": 120000}]]
    cases.append(_resolve_case("entradas_sobrepostas_e_extrapolando", m, T0, ls_all,
                               [T0 + 10000, T0 + 60000, T0 + 90000, T0 + I + 10000, T0 + I + 60000, T0 + I + 70000,
                                T0 + I + 90000, T0 + I + 100000, T0 + I + 119999],
                               "defesa (§8.2.4): c4 sobrepõe c2; c3 extrapola o bloco; c6 começa no fim do bloco; "
                               "intervalos de 30 s com fallback de 60 s viram FILLER"))
    return doc(cases)


# ═══ 4. Validação de PoP ═════════════════════════════════════════════════════

DEVICE = "device-pop-1"
SCREEN = "screen-1"
NOW = T0 + 24 * 3600 * SEC
PAIRED = T0 - 30 * 24 * 3600 * SEC


def _pop_manifest() -> dict:
    m = base_manifest(version=42)
    C, I = 240000, 120000
    _plan(m, _ext_bucket(T0, 0, I, C), [("c3", 0, 30000), ("c2", 1, 60000), ("c6", 3, 30000)])
    m["creatives"]["c5"]["valid_until_ms"] = T0 + 2 * C
    _plan(m, _ext_bucket(T0, 2, I, C), [("c5", 0, 30000)])
    return m


def _nonce(label: str) -> str:
    return proto.b64u_encode(hashlib.sha256(f"nonce-{label}".encode()).digest()[:16])


PREV_HASH = hashlib.sha256(b"evento-1016").hexdigest()


def _event(m: dict, label: str, *, ckey: str = "c3", start: Optional[int] = None, actual: Optional[int] = None,
           **over: Any) -> dict:
    cr = m["creatives"][ckey]
    bucket = _ext_bucket(T0, 0, 120000, 240000)
    ins = {e["c"]: e["i"] for e in m["external_plan"][str(bucket)]}
    st = T0 + 120000 if start is None else start
    exp = cr["duration_ms"]
    act = exp if actual is None else actual
    ev = {
        "schema": "rd.pop/1",
        "playback_uuid": uid(f"pop-{label}"),
        "device_id": uid(DEVICE),
        "screen_id": uid(SCREEN),
        "creative_id": cr["creative_id"],
        "creative_code": cr["code"],
        "campaign_id": cr["campaign_id"],
        "insertion_id": ins.get(ckey) if cr["kind"] == "EXTERNAL" else None,
        "kind": cr["kind"],
        "manifest_version": 42,
        "started_at_ms": st,
        "finished_at_ms": st + act,
        "expected_ms": exp,
        "actual_ms": act,
        "result": "COMPLETED",
        "seq": 1017,
        "nonce": _nonce(label),
        "prev_hash": PREV_HASH,
        "clock_offset_ms": 0,
        "offline": False,
    }
    ev.update(over)
    return ev


def _ctx(m: dict, **over: Any) -> dict:
    ctx = {
        "now_ms": NOW,
        "authenticated_device_id": uid(DEVICE),
        "device": {"device_id": uid(DEVICE), "screen_id": uid(SCREEN), "paired_at_ms": PAIRED, "status": "ACTIVE"},
        "last_seq": 1016,
        "last_hash": PREV_HASH,
        "known_events": [],
        "kills": [],
        "manifests": {"42": m},
    }
    ctx.update(over)
    return ctx


def evaluate_with_context(item: dict, ctx: dict, device_public_key: str) -> dict:
    """Mesma decisão da ingestão (``ingest.precheck``/``rule_reasons``), com os fatos
    levantados de ``ctx`` em memória com a MESMA semântica das consultas SQL."""
    ev = item.get("event")
    sig = item.get("signature_b64u") if "signature_b64u" in item else item.get("signature")
    pu = ev.get("playback_uuid") if isinstance(ev, dict) else None
    if ingest.schema_errors(ev) or not isinstance(sig, str) or len(sig) > 100:
        return {"status": "REJECTED", "valid": False, "reason": "SCHEMA_INVALID", "flags": []}
    ehash = proto.sha256_hex_of(ev)
    for k in ctx["known_events"]:
        if k["playback_uuid"] == pu:
            r = ingest.duplicate_result(pu, ev["seq"], ehash, k.get("valid"), k.get("reason"), k.get("event_hash"))
            return {"status": r.status, "valid": r.valid, "reason": r.reason, "flags": []}
    dev = ctx["device"]
    facts_dev = ingest.DeviceFacts(ctx["authenticated_device_id"], dev["screen_id"], dev["paired_at_ms"], device_public_key)
    status, reasons = ingest.precheck(ev, sig, facts_dev)
    if status == "REJECTED":
        return {"status": status, "valid": False, "reason": reasons[0], "flags": []}
    st, fin = ev["started_at_ms"], ev["finished_at_ms"]
    mine = [k for k in ctx["known_events"] if k["device_id"] == facts_dev.device_id]
    accepted = lambda k: k.get("status", "ACCEPTED") == "ACCEPTED" and 0 < k["actual_ms"] <= ingest.MAX_SPAN_MS  # noqa: E731
    overlap = fin > st and any(accepted(k) and k["started_at_ms"] < fin and k["finished_at_ms"] > st for k in mine)
    h0 = (st // ingest.HOUR_MS) * ingest.HOUR_MS
    h1 = h0 + ingest.HOUR_MS
    vol = sum(max(0, min(k["finished_at_ms"], h1) - max(k["started_at_ms"], h0))
              for k in ctx["known_events"] if k["screen_id"] == dev["screen_id"] and accepted(k))
    vol += max(0, min(fin, h1) - max(st, h0))
    killed = any(
        kl["creative_id"] == ev["creative_id"]
        and (kl.get("screen_id") in (None, dev["screen_id"]))
        and kl["created_at_ms"] <= st
        and (kl.get("lifted_at_ms") is None or kl["lifted_at_ms"] > st)
        for kl in ctx["kills"]
    )
    man = ctx["manifests"].get(str(ev["manifest_version"]))
    facts = ingest.RuleFacts(
        now_ms=ctx["now_ms"],
        last_seq=ctx["last_seq"],
        last_hash=ctx["last_hash"],
        nonce_used=any(k["nonce"] == ev["nonce"] for k in mine),
        killed=killed,
        overlap=overlap,
        hour_volume_ms=vol if not overlap and ev["actual_ms"] <= ingest.MAX_SPAN_MS else 0,
        manifest=ingest.index_manifest(man) if man else None,
    )
    reasons, flags = ingest.rule_reasons(ev, facts_dev, facts)
    return {"status": "ACCEPTED", "valid": not reasons, "reason": reasons[0] if reasons else None, "flags": flags}




def simulate_batch(items: list, ctx: dict, device_public_key: str) -> tuple[dict, dict]:
    """Simula ``ingest_batch`` em memória (mesma ordem/dedupe/atualização de estado).
    Retorna ``(resposta_publica, contexto_após)``."""
    ctx = copy.deepcopy(ctx)
    results, parsed = ingest.parse_items(items)
    for i, ev, sig in parsed:
        pu = ev["playback_uuid"]
        ehash = proto.sha256_hex_of(ev)
        known = next((k for k in ctx["known_events"] if k["playback_uuid"] == pu), None)
        if known is not None:
            results[i] = ingest.duplicate_result(pu, ev["seq"], ehash, known.get("valid"), known.get("reason"),
                                                 known.get("event_hash"))
            continue
        res = evaluate_with_context({"event": ev, "signature": sig}, ctx, device_public_key)
        rec = _known(ev, valid=res["valid"], reason=res["reason"], status=res["status"],
                     device=ctx["authenticated_device_id"])
        rec["screen_id"] = ctx["device"]["screen_id"]
        ctx["known_events"].append(rec)
        if res["status"] == "ACCEPTED" and (ctx["last_seq"] is None or ev["seq"] > ctx["last_seq"]):
            ctx["last_seq"], ctx["last_hash"] = ev["seq"], ehash
        results[i] = ingest.EventResult(pu, res["status"], res["valid"], res["reason"], ev["seq"])
    final = [r for r in results if r is not None]
    return {"results": [r.public() for r in final], "ack_upto_seq": ingest.ack_upto(ctx["last_seq"], final)}, ctx


def _known(ev: dict, *, valid: bool = True, reason: Optional[str] = None, status: str = "ACCEPTED",
           device: Optional[str] = None) -> dict:
    return {
        "playback_uuid": ev["playback_uuid"],
        "device_id": device or ev["device_id"],
        "screen_id": ev["screen_id"],
        "event_hash": proto.sha256_hex_of(ev),
        "status": status,
        "valid": valid,
        "reason": reason,
        "nonce": ev["nonce"],
        "seq": ev["seq"],
        "started_at_ms": ev["started_at_ms"],
        "finished_at_ms": ev["finished_at_ms"],
        "actual_ms": ev["actual_ms"],
    }


def build_pop() -> dict:
    key = test_key("pop-device-key")
    wrong = test_key("pop-wrong-key")
    spki = proto.public_spki_b64u(key)
    m = _pop_manifest()
    C, I = 240000, 120000
    cases: list[dict] = []

    def add(name: str, ev: dict, ctx: dict, *, signer=key, sig: Optional[str] = None, expect: Optional[tuple] = None,
            note: str = "") -> None:
        s = sig if sig is not None else sign(signer, ev)
        res = evaluate_with_context({"event": ev, "signature_b64u": s}, ctx, spki)
        if expect is not None and (res["status"], res["valid"], res["reason"]) != expect:
            raise AssertionError(f"pop {name}: esperado {expect}, obtido {res}")
        c = {"name": name, "event": ev, "canonical": proto.canonical_json(ev), "signature_b64u": s, "context": ctx,
             "expected": res}
        if note:
            c["note"] = note
        cases.append(c)

    base = _event(m, "valido")
    add("valido_30_de_30", base, _ctx(m), expect=("ACCEPTED", True, None))
    add("invalido_29999_de_30000", _event(m, "29999", actual=29999), _ctx(m),
        expect=("ACCEPTED", False, "INCOMPLETE_DURATION"), note="100% sem tolerância")
    add("invalido_25_de_30_interrompido", _event(m, "25", actual=25000, result="INTERRUPTED"), _ctx(m),
        expect=("ACCEPTED", False, "NOT_COMPLETED"))
    add("invalido_25_de_30_marcado_completed", _event(m, "25c", actual=25000), _ctx(m),
        expect=("ACCEPTED", False, "INCOMPLETE_DURATION"))
    add("skipped_sem_validade", _event(m, "skip", actual=0, result="SKIPPED"), _ctx(m),
        expect=("ACCEPTED", False, "NOT_COMPLETED"))
    add("duplicado_mesmo_conteudo", base, _ctx(m, known_events=[_known(base)]), expect=("DUPLICATE", True, None))
    changed = dict(base, actual_ms=30001, finished_at_ms=base["started_at_ms"] + 30001)
    add("uuid_duplicado_conteudo_diferente", changed, _ctx(m, known_events=[_known(base)]),
        expect=("DUPLICATE", False, "DUPLICATE_UUID_MISMATCH"))
    other = _event(m, "outro", start=T0 - 3600 * SEC, seq=1000)
    add("nonce_repetido", _event(m, "nonce", nonce=other["nonce"]), _ctx(m, known_events=[_known(other)]),
        expect=("ACCEPTED", False, "NONCE_REUSED"))
    add("seq_rollback", _event(m, "rollback", seq=1016), _ctx(m), expect=("ACCEPTED", False, "SEQ_ROLLBACK"))
    add("replay_com_novo_uuid", _event(m, "replay", seq=other["seq"], nonce=other["nonce"],
                                       start=T0 + C + I + 300000),
        _ctx(m, known_events=[_known(other)]), expect=("ACCEPTED", False, "SEQ_ROLLBACK"))
    add("lacuna_de_seq_sinalizada", _event(m, "gap", seq=1020), _ctx(m), expect=("ACCEPTED", True, None))
    add("cadeia_quebrada_sinalizada", _event(m, "chain", prev_hash="0" * 64), _ctx(m), expect=("ACCEPTED", True, None))
    add("assinatura_errada", _event(m, "badsig"), _ctx(m), signer=wrong, expect=("REJECTED", False, "BAD_SIGNATURE"))
    signed = _event(m, "tamper")
    s_ok = sign(key, signed)
    add("evento_adulterado_apos_assinatura", dict(signed, actual_ms=30000, campaign_id=uid("campaign-x")), _ctx(m),
        sig=s_ok, expect=("REJECTED", False, "BAD_SIGNATURE"))
    add("dispositivo_errado", _event(m, "wrongdev", device_id=uid("device-intruso")), _ctx(m),
        expect=("REJECTED", False, "DEVICE_MISMATCH"))
    add("tela_errada", _event(m, "wrongscreen", screen_id=uid("screen-2")), _ctx(m),
        expect=("ACCEPTED", False, "DEVICE_NOT_BOUND"))
    add("criativo_fora_do_manifesto", _event(m, "wrongcr", creative_id=uid("creative-intruso")), _ctx(m),
        expect=("ACCEPTED", False, "CREATIVE_NOT_IN_MANIFEST"))
    add("codigo_do_criativo_divergente", _event(m, "wrongcode", creative_code="RD-ZZZZZZ"), _ctx(m),
        expect=("ACCEPTED", False, "CREATIVE_MISMATCH"))
    add("campanha_errada", _event(m, "wrongcamp", campaign_id=uid("campaign-2")), _ctx(m),
        expect=("ACCEPTED", False, "CAMPAIGN_MISMATCH"))
    add("manifesto_errado", _event(m, "wrongman", manifest_version=41), _ctx(m),
        expect=("ACCEPTED", False, "MANIFEST_UNKNOWN"))
    add("insercao_inexistente", _event(m, "wrongins", insertion_id=uid("ins-intrusa")), _ctx(m),
        expect=("ACCEPTED", False, "INSERTION_NOT_IN_MANIFEST"))
    add("duracao_esperada_divergente", _event(m, "wrongexp", ckey="c2", actual=30000, expected_ms=30000), _ctx(m),
        expect=("ACCEPTED", False, "EXPECTED_MISMATCH"), note="c2 tem 60 s; declarar 30 s não torna 30 s exibidos válidos")
    c5ins = m["external_plan"][str(_ext_bucket(T0, 2, I, C))][0]["i"]
    add("campanha_vencida", _event(m, "expired", ckey="c5", start=T0 + 2 * C + I, insertion_id=c5ins), _ctx(m),
        expect=("ACCEPTED", False, "CAMPAIGN_NOT_ACTIVE"))
    add("relogio_no_futuro", _event(m, "future", start=NOW + 121000), _ctx(m), expect=("ACCEPTED", False, "CLOCK_FUTURE"))
    add("relogio_futuro_dentro_da_tolerancia", _event(m, "future-ok", start=NOW + 90000 - 30000), _ctx(m),
        expect=("ACCEPTED", True, None))
    add("relogio_adulterado_intervalo_inconsistente", _event(m, "skew", finished_at_ms=T0 + 120000 + 45000), _ctx(m),
        expect=("ACCEPTED", False, "TIME_INCONSISTENT"))
    add("antes_do_pareamento", _event(m, "prepair", start=PAIRED - 60000), _ctx(m),
        expect=("ACCEPTED", False, "BEFORE_PAIRING"))
    add("offline_sincronizado_6_dias_depois_valido", _event(m, "offline", offline=True, clock_offset_ms=-1500),
        _ctx(m, now_ms=T0 + 6 * 24 * 3600 * SEC), expect=("ACCEPTED", True, None),
        note="eventos offline não são invalidados por chegarem tarde")
    prev_ov = _event(m, "ovprev", start=T0 + 120000 - 10000, seq=1016)
    add("sobreposicao_de_intervalos", _event(m, "overlap"), _ctx(m, known_events=[_known(prev_ov)]),
        expect=("ACCEPTED", False, "OVERLAP"))
    contig = _event(m, "contig", start=T0 + 120000 - 30000, seq=1016)
    add("intervalos_contiguos_validos", _event(m, "contig2"), _ctx(m, known_events=[_known(contig)]),
        expect=("ACCEPTED", True, None))
    h0 = (T0 + 120000) // ingest.HOUR_MS * ingest.HOUR_MS
    filler = _event(m, "otherdev", start=h0, actual=0)
    filler.update(actual_ms=ingest.HOUR_MS - 15000, finished_at_ms=h0 + ingest.HOUR_MS - 15000,
                  device_id=uid("device-anterior"), seq=5)
    add("volume_impossivel_na_tela", _event(m, "volume", start=h0 + ingest.HOUR_MS - 30000),
        _ctx(m, known_events=[_known(filler, device=uid("device-anterior"))]),
        expect=("ACCEPTED", False, "IMPOSSIBLE_VOLUME"),
        note="dispositivo anterior da mesma tela já somou 59min45s nesta hora")
    cid = m["creatives"]["c3"]["creative_id"]
    add("criativo_com_kill_ativo", _event(m, "killed"),
        _ctx(m, kills=[{"creative_id": cid, "screen_id": None, "created_at_ms": T0 + 60000, "lifted_at_ms": None}]),
        expect=("ACCEPTED", False, "CREATIVE_REVOKED"))
    add("kill_posterior_ao_inicio_valido", _event(m, "killafter"),
        _ctx(m, kills=[{"creative_id": cid, "screen_id": uid(SCREEN), "created_at_ms": T0 + 120001, "lifted_at_ms": None}]),
        expect=("ACCEPTED", True, None))
    mr = copy.deepcopy(m)
    mr["revoked"] = [cid]
    add("criativo_revogado_no_manifesto", _event(m, "revoked"), _ctx(m, manifests={"42": mr}),
        expect=("ACCEPTED", False, "CREATIVE_REVOKED"))
    add("interno_valido_sem_comissao", _event(m, "internal", ckey="c7", start=T0), _ctx(m), expect=("ACCEPTED", True, None))
    add("fallback_valido_sem_comissao", _event(m, "fallback", ckey="f1", start=T0 + 2 * C + I + 60000), _ctx(m),
        expect=("ACCEPTED", True, None))
    bad = dict(_event(m, "schema"))
    bad.pop("nonce")
    add("schema_invalido_chave_ausente", bad, _ctx(m), sig=sign(key, bad), expect=("REJECTED", False, "SCHEMA_INVALID"))

    # ── lotes (expectativas calculadas pelo simulador = mesma lógica da ingestão) ──
    batch_cases = []
    e1 = _event(m, "b1", seq=1017, start=T0 + 120000)
    e2 = _event(m, "b2", ckey="c2", seq=1018, start=T0 + 150000, prev_hash=proto.sha256_hex_of(e1))
    e3 = dict(_event(m, "b3", seq=1019))
    e3.pop("kind")
    e4 = _event(m, "b4", seq=1020, start=T0 + 300000, prev_hash=proto.sha256_hex_of(e2))
    e5 = _event(m, "b5", ckey="c6", seq=1021, start=T0 + 200000)
    e6 = _event(m, "b6", ckey="c6", seq=1022, start=T0 + 210000, nonce=e1["nonce"])
    items = [
        {"event": e2, "signature": sign(key, e2)},
        {"event": e1, "signature": sign(key, e1)},
        {"event": e3, "signature": sign(key, e3)},
        {"event": e4, "signature": sign(wrong, e4)},
        {"event": e1, "signature": sign(key, e1)},
        {"event": e5, "signature": sign(key, e5)},
        {"event": e6, "signature": sign(key, e6)},
    ]
    ctx0 = _ctx(m)
    first, ctx1 = simulate_batch(items, ctx0, spki)
    retry, _ = simulate_batch(items, ctx1, spki)
    want = [("ACCEPTED", True, None), ("ACCEPTED", True, None), ("REJECTED", False, "SCHEMA_INVALID"),
            ("REJECTED", False, "BAD_SIGNATURE"), ("DUPLICATE", True, None),
            ("ACCEPTED", False, "OVERLAP"), ("ACCEPTED", False, "NONCE_REUSED")]
    got = [(r["status"], r["valid"], r["reason"]) for r in first["results"]]
    if got != want or first["ack_upto_seq"] != 1022:
        raise AssertionError(f"lote_parcial: {got} ack={first['ack_upto_seq']}")
    if any(r["status"] not in ("DUPLICATE", "REJECTED") for r in retry["results"]):
        raise AssertionError(f"retry: {retry}")
    batch_cases.append({
        "name": "lote_parcial_e_retry_idempotente",
        "note": "itens fora de ordem (processados por seq); malformado (não gravado); assinatura errada; UUID repetido "
                "no lote (DUPLICATE com a validade do original); sobreposição; nonce repetido no mesmo lote. 'expected_retry' = reenvio do MESMO lote "
                "(falha de rede): sem efeito, tudo DUPLICATE (exceto o malformado, que nunca é gravado).",
        "context": ctx0,
        "items": items,
        "expected": first,
        "expected_retry": retry,
    })
    return doc(cases, device_key=key_block(key), wrong_key=key_block(wrong), batch_cases=batch_cases,
               notes={"expected.flags": "anomalias sinalizadas (não invalidam): SEQ_GAP, CHAIN_BREAK, LARGE_CLOCK_OFFSET",
                      "context": "estado do servidor antes do evento; known_events = eventos já gravados"})


# ═══ Saída ═══════════════════════════════════════════════════════════════════

def build_all() -> dict[str, dict]:
    return {
        "canonical_json.json": build_canonical(),
        "es256_signatures.json": build_es256(),
        "cycle_resolution.json": build_cycle(),
        "pop_validation.json": build_pop(),
    }


def dumps(obj: Any) -> str:
    return json.dumps(obj, ensure_ascii=False, indent=2) + "\n"


def lock_of(files: dict[str, dict]) -> dict:
    return {name: hashlib.sha256(dumps(obj).encode("utf-8")).hexdigest() for name, obj in sorted(files.items())}


LOCK_PATH = Path(__file__).with_name("vectors.lock.json")


def main(argv: Optional[list[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="Gera/compara os vetores de conformidade RDPP/1")
    ap.add_argument("--out", help="diretório de saída")
    ap.add_argument("--check", help="diretório com os vetores publicados para comparar")
    ap.add_argument("--write-lock", action="store_true", help="atualiza vectors.lock.json")
    a = ap.parse_args(argv)
    files = build_all()
    if a.out:
        out = Path(a.out)
        out.mkdir(parents=True, exist_ok=True)
        for name, obj in files.items():
            (out / name).write_text(dumps(obj), encoding="utf-8")
    if a.write_lock:
        LOCK_PATH.write_text(json.dumps(lock_of(files), indent=2) + "\n", encoding="utf-8")
    if a.check:
        bad = [n for n, obj in files.items() if (Path(a.check) / n).read_text(encoding="utf-8") != dumps(obj)]
        if bad:
            print("DIVERGENTES:", ", ".join(bad))
            return 1
        print("OK: vetores publicados == regenerados")
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
