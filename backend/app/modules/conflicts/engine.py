"""
RD Mídia Indoor — Motor de conflitos PURO (sem banco) — A6 §10.7 / ADR_006 #11.

Precedência fixa (implementada exatamente, primeira regra que decide encerra):

1. anunciante == organização do estabelecimento → ALLOW ``MESMA_REDE`` (configurável)
2. algum segmento do anúncio (ou ancestral) com política de conteúdo BLOCK
   → BLOCK ``PROIBIDO_UNIVERSAL`` (NÃO sobrescrevível por override/exceção/regra)
3. override admin válido (campanha × estabelecimento) → ALLOW ``OVERRIDE_ADMIN``
4. exceção válida do estabelecimento (anunciante/marca/campanha) → ALLOW ``EXCECAO_ESTABELECIMENTO``
5. regras explícitas que casam (sujeito × objeto, com descendentes) → a mais específica
   decide (produto > marca/anunciante > segmento mais profundo > subcategoria >
   categoria > setor); EMPATE ⇒ BLOCK ``REGRA_EXPLICITA``
6. mesmo ramo (categoria anunciante × estabelecimento, abaixo do nível SETOR) ou
   concorrente explícito → BLOCK ``MESMO_RAMO`` / ``CONCORRENTE``
7. segmentos do anúncio ∩ segmentos do estabelecimento (sobreposição
   ancestral/descendente) ≠ ∅ → BLOCK ``SEGMENTO``
8. caso contrário → ALLOW ``SEM_CONFLITO``

A mesma lógica aplicada anúncio × anúncio (passos 6–7 + concorrência explícita)
define "concorrente" para o empacotador (W-INV): ``ad_vs_ad``.

Tudo é determinístico; ``rule_path`` registra cada passo avaliado (explicação).
"""
from __future__ import annotations

import hashlib
import json
import uuid
from dataclasses import dataclass, field
from typing import Iterable, Optional

ENGINE_VERSION = "conflicts-1.0"

ALLOW = "ALLOW"
BLOCK = "BLOCK"

# Especificidade do OBJETO da regra (A6: produto > marca/anunciante > segmento mais
# profundo > subcategoria > categoria > setor). Segmento: 300 + 10 × profundidade.
OBJECT_SPEC_PRODUCT = 700
OBJECT_SPEC_BRAND = 600
OBJECT_SPEC_ADVERTISER = 600
OBJECT_SPEC_SEGMENT_BASE = 300
OBJECT_SPEC_TAXONOMY = {"SUBCATEGORY": 250, "CATEGORY": 200, "SECTOR": 150}
# Especificidade do SUJEITO (desempate secundário).
SUBJECT_SPEC_ESTABLISHMENT = 90
SUBJECT_SPEC_ORG = 80
SUBJECT_SPEC_SEGMENT_BASE = 30
SUBJECT_SPEC_TAXONOMY = {"SUBCATEGORY": 25, "CATEGORY": 20, "SECTOR": 15}


@dataclass(frozen=True)
class NodeRef:
    """Nó de taxonomia (setor/categoria/subcategoria) com caminho raiz→próprio."""
    id: uuid.UUID
    kind: str
    path_ids: tuple[uuid.UUID, ...]


@dataclass(frozen=True)
class SegRef:
    id: uuid.UUID
    path_ids: tuple[uuid.UUID, ...]  # raiz → próprio

    @property
    def depth(self) -> int:
        return len(self.path_ids) - 1


@dataclass(frozen=True)
class AdFacts:
    advertiser_org_id: uuid.UUID
    advertiser_category: Optional[NodeRef]
    advertiser_category_kinds: tuple[tuple[uuid.UUID, str], ...] = ()  # (id do nó do caminho, kind)
    segments: tuple[SegRef, ...] = ()
    segments_source: str = "NONE"  # CREATIVE | CAMPAIGN | ADVERTISER | NONE
    product_id: Optional[uuid.UUID] = None
    brand_id: Optional[uuid.UUID] = None
    campaign_id: Optional[uuid.UUID] = None
    creative_id: Optional[uuid.UUID] = None


@dataclass(frozen=True)
class EstFacts:
    establishment_id: uuid.UUID
    org_id: uuid.UUID
    category: Optional[NodeRef]
    category_kinds: tuple[tuple[uuid.UUID, str], ...] = ()
    segments: tuple[SegRef, ...] = ()


@dataclass(frozen=True)
class RuleFact:
    id: uuid.UUID
    subject_type: str
    subject_id: uuid.UUID
    object_type: str
    object_id: uuid.UUID
    include_descendants: bool
    effect: str
    specificity: int  # calculada pelo adaptador (objeto*100 + sujeito)


@dataclass(frozen=True)
class ExceptionFact:
    id: uuid.UUID
    object_type: str  # ADVERTISER | BRAND | CAMPAIGN
    object_id: uuid.UUID


@dataclass(frozen=True)
class OverrideFact:
    id: uuid.UUID
    justification: str


@dataclass
class Decision:
    result: str
    reason_code: str
    rule_path: list = field(default_factory=list)
    engine_version: str = ENGINE_VERSION
    facts_hash: str = ""
    decision_id: Optional[uuid.UUID] = None  # preenchido quando gravada

    @property
    def allowed(self) -> bool:
        return self.result == ALLOW

    def as_dict(self) -> dict:
        return {
            "result": self.result,
            "reason_code": self.reason_code,
            "rule_path": self.rule_path,
            "engine_version": self.engine_version,
            "facts_hash": self.facts_hash,
            "decision_id": str(self.decision_id) if self.decision_id else None,
        }


# ─── utilidades ──────────────────────────────────────────────────────────────

def specificity_for(subject_type: str, subject_kind_or_depth, object_type: str, object_kind_or_depth) -> int:
    """Especificidade = objeto×100 + sujeito. ``*_kind_or_depth``: kind (taxonomia) ou profundidade (segmento)."""
    if object_type == "AD_PRODUCT":
        o = OBJECT_SPEC_PRODUCT
    elif object_type == "AD_BRAND":
        o = OBJECT_SPEC_BRAND
    elif object_type == "AD_ADVERTISER":
        o = OBJECT_SPEC_ADVERTISER
    elif object_type == "AD_SEGMENT":
        o = OBJECT_SPEC_SEGMENT_BASE + 10 * int(object_kind_or_depth or 0)
    elif object_type == "AD_CATEGORY":
        o = OBJECT_SPEC_TAXONOMY.get(str(object_kind_or_depth), 150)
    else:  # pragma: no cover - validado no schema
        raise ValueError(f"object_type inválido: {object_type}")
    if subject_type == "ESTABLISHMENT":
        s = SUBJECT_SPEC_ESTABLISHMENT
    elif subject_type == "EST_ORG":
        s = SUBJECT_SPEC_ORG
    elif subject_type == "EST_SEGMENT":
        s = SUBJECT_SPEC_SEGMENT_BASE + int(subject_kind_or_depth or 0)
    elif subject_type == "EST_CATEGORY":
        s = SUBJECT_SPEC_TAXONOMY.get(str(subject_kind_or_depth), 15)
    else:  # pragma: no cover
        raise ValueError(f"subject_type inválido: {subject_type}")
    return o * 100 + s


def _node_matches(node: Optional[NodeRef], target_id: uuid.UUID, include_descendants: bool) -> bool:
    if node is None:
        return False
    if include_descendants:
        return target_id in node.path_ids
    return node.id == target_id


def _seg_matches(segs: Iterable[SegRef], target_id: uuid.UUID, include_descendants: bool) -> bool:
    for s in segs:
        if (target_id in s.path_ids) if include_descendants else (s.id == target_id):
            return True
    return False


def same_branch(a: Optional[NodeRef], a_kinds: Iterable[tuple], b: Optional[NodeRef]) -> Optional[uuid.UUID]:
    """Nó mais profundo compartilhado entre dois ramos, se NÃO for SETOR (mesmo ramo)."""
    if a is None or b is None:
        return None
    kinds = dict(a_kinds)
    common = None
    for x, y in zip(a.path_ids, b.path_ids):
        if x != y:
            break
        common = x
    if common is None:
        return None
    kind = kinds.get(common)
    if kind is None:  # sem informação de tipo: profundidade 0 = setor
        kind = "SECTOR" if a.path_ids.index(common) == 0 else "CATEGORY"
    return None if kind == "SECTOR" else common


def segment_overlap(a: Iterable[SegRef], b: Iterable[SegRef]) -> Optional[tuple[uuid.UUID, uuid.UUID]]:
    """Primeiro par (seg_a, seg_b) em relação ancestral/descendente/igual (ordem determinística)."""
    bl = sorted(b, key=lambda s: str(s.id))
    for sa in sorted(a, key=lambda s: str(s.id)):
        for sb in bl:
            if sa.id in sb.path_ids or sb.id in sa.path_ids:
                return sa.id, sb.id
    return None


def _u(v) -> Optional[str]:
    return None if v is None else str(v)


def facts_hash(*parts) -> str:
    def norm(x):
        if isinstance(x, (AdFacts, EstFacts, RuleFact, ExceptionFact, OverrideFact, NodeRef, SegRef)):
            return {k: norm(v) for k, v in sorted(x.__dict__.items())}
        if isinstance(x, (list, tuple, set, frozenset)):
            items = [norm(i) for i in x]
            return sorted(items, key=lambda i: json.dumps(i, sort_keys=True, default=str)) if isinstance(
                x, (set, frozenset)) else items
        if isinstance(x, dict):
            return {str(k): norm(v) for k, v in sorted(x.items(), key=lambda kv: str(kv[0]))}
        if isinstance(x, uuid.UUID):
            return str(x)
        return x

    raw = json.dumps([norm(p) for p in parts], sort_keys=True, default=str, separators=(",", ":"))
    return hashlib.sha256(raw.encode()).hexdigest()


# ─── decisão anúncio × estabelecimento ──────────────────────────────────────

def decide(
    ad: AdFacts,
    est: EstFacts,
    *,
    blocked_policy_segment_ids: Iterable[uuid.UUID] = (),
    override: Optional[OverrideFact] = None,
    exceptions: Iterable[ExceptionFact] = (),
    rules: Iterable[RuleFact] = (),
    competitor_link: bool = False,
    same_network_allow: bool = True,
) -> Decision:
    blocked = set(blocked_policy_segment_ids)
    exceptions = sorted(exceptions, key=lambda e: str(e.id))
    rules = sorted(rules, key=lambda r: str(r.id))
    h = facts_hash(ad, est, sorted(blocked, key=str), override, exceptions, rules, competitor_link, same_network_allow)
    path: list[dict] = []

    def done(result: str, code: str, step: int, detail: Optional[dict] = None) -> Decision:
        path.append({"step": step, "check": code, "result": result, **({"detail": detail} if detail else {})})
        return Decision(result=result, reason_code=code, rule_path=path, facts_hash=h)

    def skip(step: int, check: str, note: Optional[str] = None) -> None:
        path.append({"step": step, "check": check, "result": "SKIP", **({"note": note} if note else {})})

    # 1. mesma rede
    if same_network_allow and ad.advertiser_org_id == est.org_id:
        return done(ALLOW, "MESMA_REDE", 1, {"org_id": _u(est.org_id)})
    skip(1, "MESMA_REDE")

    # 2. proibido universal (não sobrescrevível)
    for s in sorted(ad.segments, key=lambda x: str(x.id)):
        hit = [p for p in s.path_ids if p in blocked]
        if hit:
            return done(BLOCK, "PROIBIDO_UNIVERSAL", 2, {"segment_id": _u(s.id), "policy_segment_id": _u(hit[0])})
    skip(2, "PROIBIDO_UNIVERSAL")

    # 3. override admin
    if override is not None:
        return done(ALLOW, "OVERRIDE_ADMIN", 3, {"override_id": _u(override.id)})
    skip(3, "OVERRIDE_ADMIN")

    # 4. exceção do estabelecimento
    for e in exceptions:
        if (
            (e.object_type == "ADVERTISER" and e.object_id == ad.advertiser_org_id)
            or (e.object_type == "BRAND" and ad.brand_id is not None and e.object_id == ad.brand_id)
            or (e.object_type == "CAMPAIGN" and ad.campaign_id is not None and e.object_id == ad.campaign_id)
        ):
            return done(ALLOW, "EXCECAO_ESTABELECIMENTO", 4, {"exception_id": _u(e.id), "object_type": e.object_type})
    skip(4, "EXCECAO_ESTABELECIMENTO")

    # 5. regras explícitas — mais específica decide; empate ⇒ BLOCK
    matched: list[RuleFact] = []
    for r in rules:
        if r.subject_type == "EST_CATEGORY":
            subj = _node_matches(est.category, r.subject_id, r.include_descendants)
        elif r.subject_type == "EST_SEGMENT":
            subj = _seg_matches(est.segments, r.subject_id, r.include_descendants)
        elif r.subject_type == "EST_ORG":
            subj = r.subject_id == est.org_id
        elif r.subject_type == "ESTABLISHMENT":
            subj = r.subject_id == est.establishment_id
        else:
            subj = False
        if not subj:
            continue
        if r.object_type == "AD_CATEGORY":
            obj = _node_matches(ad.advertiser_category, r.object_id, r.include_descendants)
        elif r.object_type == "AD_SEGMENT":
            obj = _seg_matches(ad.segments, r.object_id, r.include_descendants)
        elif r.object_type == "AD_PRODUCT":
            obj = ad.product_id is not None and r.object_id == ad.product_id
        elif r.object_type == "AD_BRAND":
            obj = ad.brand_id is not None and r.object_id == ad.brand_id
        elif r.object_type == "AD_ADVERTISER":
            obj = r.object_id == ad.advertiser_org_id
        else:
            obj = False
        if obj:
            matched.append(r)
    if matched:
        top = max(r.specificity for r in matched)
        winners = [r for r in matched if r.specificity == top]
        effects = {r.effect for r in winners}
        result = BLOCK if BLOCK in effects else ALLOW
        detail = {
            "rule_ids": [_u(r.id) for r in winners],
            "specificity": top,
            "tie": len(effects) > 1,
            "matched": len(matched),
        }
        return done(result, "REGRA_EXPLICITA", 5, detail)
    skip(5, "REGRA_EXPLICITA", "nenhuma regra casou")

    # 6. mesmo ramo / concorrente explícito
    common = same_branch(ad.advertiser_category, ad.advertiser_category_kinds, est.category)
    if common is not None:
        return done(BLOCK, "MESMO_RAMO", 6, {"taxonomy_node_id": _u(common)})
    if competitor_link:
        return done(BLOCK, "CONCORRENTE", 6, {"advertiser_org_id": _u(ad.advertiser_org_id), "est_org_id": _u(est.org_id)})
    skip(6, "MESMO_RAMO")

    # 7. sobreposição de segmentos (anúncio × o que o estabelecimento vende)
    ov = segment_overlap(ad.segments, est.segments)
    if ov is not None:
        return done(BLOCK, "SEGMENTO", 7, {"ad_segment_id": _u(ov[0]), "establishment_segment_id": _u(ov[1])})
    skip(7, "SEGMENTO")

    # 8. liberado
    return done(ALLOW, "SEM_CONFLITO", 8)


# ─── anúncio × anúncio (concorrência para o empacotador) ────────────────────

def ad_vs_ad(a: AdFacts, b: AdFacts, *, competitor_link: bool = False) -> Decision:
    """BLOCK = são CONCORRENTES (não podem dividir o bloco externo / exclusividade)."""
    h = facts_hash(a, b, competitor_link)
    path: list[dict] = []

    def done(result: str, code: str, step: int, detail: Optional[dict] = None) -> Decision:
        path.append({"step": step, "check": code, "result": result, **({"detail": detail} if detail else {})})
        return Decision(result=result, reason_code=code, rule_path=path, facts_hash=h)

    if a.advertiser_org_id == b.advertiser_org_id:
        return done(ALLOW, "MESMO_ANUNCIANTE", 0)
    if competitor_link:
        return done(BLOCK, "CONCORRENTE", 6)
    common = same_branch(a.advertiser_category, a.advertiser_category_kinds, b.advertiser_category)
    if common is not None:
        return done(BLOCK, "MESMO_RAMO", 6, {"taxonomy_node_id": _u(common)})
    ov = segment_overlap(a.segments, b.segments)
    if ov is not None:
        return done(BLOCK, "SEGMENTO", 7, {"segment_a": _u(ov[0]), "segment_b": _u(ov[1])})
    return done(ALLOW, "SEM_CONFLITO", 8)


def combine(decisions: list[tuple[Optional[uuid.UUID], Decision]]) -> Decision:
    """Decisão da campanha num estabelecimento = BLOCK se QUALQUER criativo for BLOCK."""
    if not decisions:
        raise ValueError("sem decisões")
    path = [{"creative_id": _u(cid), "result": d.result, "reason_code": d.reason_code, "rule_path": d.rule_path}
            for cid, d in decisions]
    blocking = [d for _, d in decisions if d.result == BLOCK]
    first = blocking[0] if blocking else decisions[0][1]
    h = hashlib.sha256("|".join(d.facts_hash for _, d in decisions).encode()).hexdigest()
    return Decision(result=BLOCK if blocking else ALLOW, reason_code=first.reason_code, rule_path=path, facts_hash=h)
