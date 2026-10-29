"""
RD Mídia Indoor — ``resolve()`` de REFERÊNCIA (RDPP/1 §5.1, §5.2, §8.2). Função PURA.

    resolve(manifest, anchor_ms, local_state, t_ms) -> Segment

``local_state = {"revoked": [creative_id, …], "media_ready": [media_key, …]}``;
``Segment = {block: INTERNAL|EXTERNAL|EMERGENCY, kind: ITEM|FILLER, creative_key, start_ms, end_ms}``.

É esta função que os vetores ``protocol/vectors/cycle_resolution.json`` testam
(gerados pelo W-PLAY com esta implementação). Regras implementadas exatamente:

1. Emergência ativa (primeira da lista com ``from ≤ t < until``) preempta tudo; o
   criativo repete em passos de ``creatives[c].duration_ms`` a partir de ``from_ms``;
   a última repetição que não cabe até ``until_ms`` vira FILLER com o próprio
   criativo. Emergência e fallback não passam por validade/media_ready/revoked.
2. ``d = t − A``, ``k = floor(d / C)``, ``fase = d − k·C``.
3. Bloco INTERNO (``fase < I``): ``internal_pattern[k mod len]``; item que ultrapassa
   o fim do bloco não toca e o resto do bloco vai para fallback; padrão vazio ⇒ o
   bloco inteiro é fallback.
4. Bloco EXTERNO: ``s = A + k·C + I``; ``b = floor(s / C)·C``; fora do horizonte ⇒
   ``b' = b − n·repeat_period_ms`` com o menor ``|n|`` (n ≥ 1 se ``b ≥ to``; n ≤ −1 se
   ``b < from``); se não cair ⇒ bloco inteiro em fallback. Entradas processadas em
   ordem estável por ``u`` com cursor (§8.2 item 4): sobreposição/estouro ⇒ o
   intervalo ``[max(início, cursor), min(fim, fim_do_bloco))`` vira fallback próprio
   (a primeira de um par sobreposto toca); início ≥ fim do bloco ⇒ ignorada; cada
   lacuna máxima é um intervalo de fallback próprio.
5. Validade no INÍCIO do item (``valid_from ≤ início < valid_until``, com o ``t``
   real), mídia em ``media_ready`` e criativo fora de ``manifest.revoked ∪
   local_state.revoked``; inválido ⇒ seu intervalo vira fallback.
6. Fallback de ``[s, e)``: itens em ordem cíclica a partir do índice
   ``floor((s − A)/U) mod len``; cada item só entra inteiro; o resto menor que o
   próximo item é FILLER exibindo esse item (sem PoP); fallback vazio ⇒ FILLER com
   ``creative_key = null``.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Optional

BLOCK_INTERNAL = "INTERNAL"
BLOCK_EXTERNAL = "EXTERNAL"
BLOCK_EMERGENCY = "EMERGENCY"
KIND_ITEM = "ITEM"
KIND_FILLER = "FILLER"


@dataclass(frozen=True)
class Segment:
    block: str
    kind: str
    creative_key: Optional[str]
    start_ms: int
    end_ms: int

    def to_dict(self) -> dict:
        return {
            "block": self.block,
            "kind": self.kind,
            "creative_key": self.creative_key,
            "start_ms": self.start_ms,
            "end_ms": self.end_ms,
        }


def _fallback_fill(manifest: dict, anchor_ms: int, unit_ms: int, s: int, e: int, t: int, block: str) -> Segment:
    fb = manifest.get("fallback") or []
    if not fb:
        return Segment(block, KIND_FILLER, None, s, e)
    n = len(fb)
    idx = ((s - anchor_ms) // unit_ms) % n
    cur = s
    # cada item só entra inteiro; o laço termina porque d > 0 e o intervalo é finito
    while True:
        item = fb[idx]
        d = int(item["d"])
        if d <= 0:
            return Segment(block, KIND_FILLER, item["c"], cur, e)
        if cur + d <= e:
            if cur <= t < cur + d:
                return Segment(block, KIND_ITEM, item["c"], cur, cur + d)
            cur += d
            idx = (idx + 1) % n
            if cur >= e:  # t fora de [s, e) — defesa (não ocorre com chamadas internas)
                return Segment(block, KIND_FILLER, item["c"], cur, e)
        else:
            return Segment(block, KIND_FILLER, item["c"], cur, e)


def _item_valid(manifest: dict, local_state: dict, revoked: set, ckey: str, start: int) -> bool:
    cr = (manifest.get("creatives") or {}).get(ckey)
    if cr is None:
        return False
    if cr.get("creative_id") in revoked:
        return False
    vf = cr.get("valid_from_ms")
    vu = cr.get("valid_until_ms")
    if vf is not None and start < vf:
        return False
    if vu is not None and not start < vu:
        return False
    media_key = cr.get("media")
    return media_key in set(local_state.get("media_ready") or [])


def _horizon_bucket(manifest: dict, b: int) -> Optional[int]:
    hz = manifest.get("horizon") or {}
    f = hz.get("from_ms")
    to = hz.get("to_ms")
    p = hz.get("repeat_period_ms")
    if f is None or to is None:
        return b
    if f <= b < to:
        return b
    if not p or p <= 0:
        return None
    if b >= to:
        n = (b - to) // p + 1
        bp = b - n * p
    else:
        m = -((b - f) // p)  # ceil((f - b) / p)
        bp = b + m * p
    return bp if f <= bp < to else None


def resolve(manifest: dict, anchor_ms: int, local_state: Optional[dict], t_ms: int) -> Segment:
    local_state = local_state or {}
    rot = manifest["rotation"]
    C = int(rot["cycle_ms"])
    I = int(rot["internal_ms"])
    E = int(rot["external_ms"])
    U = int(rot["unit_ms"])
    A = int(anchor_ms)
    t = int(t_ms)
    creatives = manifest.get("creatives") or {}
    revoked = set(manifest.get("revoked") or []) | set(local_state.get("revoked") or [])

    # 1. emergência
    for em in manifest.get("emergency") or []:
        f, u = int(em["from_ms"]), int(em["until_ms"])
        if f <= t < u:
            c = em["c"]
            dur = int((creatives.get(c) or {}).get("duration_ms") or 0)
            if dur <= 0:
                return Segment(BLOCK_EMERGENCY, KIND_FILLER, c, f, u)
            n = (t - f) // dur
            s = f + n * dur
            e = s + dur
            if e > u:
                return Segment(BLOCK_EMERGENCY, KIND_FILLER, c, s, u)
            return Segment(BLOCK_EMERGENCY, KIND_ITEM, c, s, e)

    d = t - A
    k = d // C
    phase = d - k * C

    # 2. bloco interno
    if phase < I:
        bs = A + k * C
        be = bs + I
        pattern = manifest.get("internal_pattern") or []
        if not pattern:
            return _fallback_fill(manifest, A, U, bs, be, t, BLOCK_INTERNAL)
        seq = pattern[k % len(pattern)]
        cur = bs
        for it in seq:
            s = cur
            e = s + int(it["d"])
            if e > be:
                break  # não toca; resto do bloco → fallback
            if s <= t < e:
                if _item_valid(manifest, local_state, revoked, it["c"], s):
                    return Segment(BLOCK_INTERNAL, KIND_ITEM, it["c"], s, e)
                return _fallback_fill(manifest, A, U, s, e, t, BLOCK_INTERNAL)
            cur = e
        return _fallback_fill(manifest, A, U, cur, be, t, BLOCK_INTERNAL)

    # 3. bloco externo
    s0 = A + k * C + I
    be = s0 + E
    b = (s0 // C) * C
    bp = _horizon_bucket(manifest, b)
    if bp is None:
        return _fallback_fill(manifest, A, U, s0, be, t, BLOCK_EXTERNAL)
    entries = (manifest.get("external_plan") or {}).get(str(bp)) or []
    ordered = sorted(enumerate(entries), key=lambda p: (int(p[1]["u"]), p[0]))
    intervals: list[tuple[int, int, Optional[dict]]] = []  # (ini, fim, entrada|None=fallback)
    cursor = s0
    for _, ent in ordered:
        es = s0 + int(ent["u"]) * U
        ee = es + int(ent["d"])
        if es >= be:
            continue
        if es < cursor or ee > be:
            fs, fe = max(es, cursor), min(ee, be)
            if fs > cursor:
                intervals.append((cursor, fs, None))
            if fs < fe:
                intervals.append((fs, fe, None))
                cursor = fe
            continue
        if es > cursor:
            intervals.append((cursor, es, None))
        intervals.append((es, ee, ent))
        cursor = ee
    if cursor < be:
        intervals.append((cursor, be, None))
    for s, e, ent in intervals:
        if s <= t < e:
            if ent is not None and _item_valid(manifest, local_state, revoked, ent["c"], s):
                return Segment(BLOCK_EXTERNAL, KIND_ITEM, ent["c"], s, e)
            return _fallback_fill(manifest, A, U, s, e, t, BLOCK_EXTERNAL)
    # inalcançável: os intervalos cobrem [s0, be)
    return _fallback_fill(manifest, A, U, s0, be, t, BLOCK_EXTERNAL)


__all__ = ["Segment", "resolve", "BLOCK_INTERNAL", "BLOCK_EXTERNAL", "BLOCK_EMERGENCY", "KIND_ITEM", "KIND_FILLER"]


def resolve_dict(manifest: dict, anchor_ms: int, local_state: Any, t_ms: int) -> dict:
    """Atalho para os vetores: devolve o Segment como dict."""
    return resolve(manifest, anchor_ms, local_state, t_ms).to_dict()
