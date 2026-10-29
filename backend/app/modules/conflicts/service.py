"""
RD Mídia Indoor — Serviço do motor de conflitos (adaptador banco → ``engine``).

Interfaces públicas (plano §4; consumidas por W-CAMP/W-INV):

- ``evaluate_conflict(db, subject, establishment, *, at=None, record=False, ctx=None) -> Decision``
  ``subject`` = ``Creative`` ou ``Campaign`` (ou seus UUIDs); ``establishment`` = model ou UUID.
  Para campanha, a decisão é BLOCK se QUALQUER criativo (vigente ou pendente) bloquear;
  sem criativos usa a classificação da campanha (ou do anunciante).
- ``evaluate_campaign_establishments(db, campaign, est_ids, ...) -> dict[est_id, Decision]`` (lote)
- ``are_competitors(db, creative_a, creative_b) -> Decision`` (BLOCK = concorrentes)
- ``competitor_pairs(db, creative_ids) -> set[frozenset[UUID]]`` (lote, para o empacotador)

Taxonomia/política de conteúdo/concorrentes explícitos vêm do W-ORG (app.models.taxonomy).
Decisões gravadas em ``conflict_decisions`` (append-only) quando ``record=True``.
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Iterable, Optional, Union

from sqlalchemy import and_, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import NotFound
from app.core.settings_service import get_setting
from app.core.timeutils import utcnow
from app.modules.campaigns import config as _config  # noqa: F401  (registra settings)
from app.modules.conflicts import engine as E

UUIDLike = Union[uuid.UUID, str]


def _uuid(v: Any) -> uuid.UUID:
    return v if isinstance(v, uuid.UUID) else uuid.UUID(str(v))


def path_ids(path: Optional[str]) -> tuple[uuid.UUID, ...]:
    if not path:
        return ()
    out = []
    for part in path.strip("/").split("/"):
        if part:
            out.append(uuid.UUID(hex=part))
    return tuple(out)


# ─── snapshot da taxonomia ───────────────────────────────────────────────────

@dataclass
class TaxonomySnapshot:
    nodes: dict = field(default_factory=dict)  # id → (kind, path_ids)
    segments: dict = field(default_factory=dict)  # id → path_ids
    products: dict = field(default_factory=dict)  # id → (segment_id, brand_id)
    blocked_policy: set = field(default_factory=set)

    def seg(self, sid: uuid.UUID) -> Optional[E.SegRef]:
        p = self.segments.get(sid)
        return E.SegRef(id=sid, path_ids=p) if p else None

    def node(self, nid: Optional[uuid.UUID]) -> tuple[Optional[E.NodeRef], tuple]:
        if nid is None or nid not in self.nodes:
            return None, ()
        kind, p = self.nodes[nid]
        kinds = tuple((x, self.nodes[x][0]) for x in p if x in self.nodes)
        return E.NodeRef(id=nid, kind=kind, path_ids=p), kinds


async def load_taxonomy(db: AsyncSession) -> TaxonomySnapshot:
    from app.models.taxonomy import ContentPolicy, Product, Segment, TaxonomyNode

    snap = TaxonomySnapshot()
    for nid, kind, path in (await db.execute(select(TaxonomyNode.id, TaxonomyNode.kind, TaxonomyNode.path))).all():
        snap.nodes[nid] = (kind, path_ids(path))
    for sid, path in (await db.execute(select(Segment.id, Segment.path))).all():
        snap.segments[sid] = path_ids(path)
    for pid, seg, brand in (await db.execute(select(Product.id, Product.segment_id, Product.brand_id))).all():
        snap.products[pid] = (seg, brand)
    snap.blocked_policy = set(
        (await db.execute(select(ContentPolicy.segment_id).where(ContentPolicy.effect == "BLOCK"))).scalars()
    )
    return snap


# ─── fatos do anúncio ────────────────────────────────────────────────────────

async def _advertiser(db: AsyncSession, snap: TaxonomySnapshot, org_id: uuid.UUID):
    from app.models.taxonomy import AdvertiserProfile, AdvertiserSegment

    cat_id = (await db.execute(select(AdvertiserProfile.category_id).where(AdvertiserProfile.org_id == org_id))).scalar()
    segs = list((await db.execute(select(AdvertiserSegment.segment_id).where(AdvertiserSegment.org_id == org_id))).scalars())
    node, kinds = snap.node(cat_id)
    return node, kinds, segs


def _segrefs(snap: TaxonomySnapshot, ids: Iterable[uuid.UUID]) -> tuple[E.SegRef, ...]:
    out = []
    for sid in sorted(set(ids), key=str):
        r = snap.seg(sid)
        if r is not None:
            out.append(r)
    return tuple(out)


async def campaign_ad_facts(db: AsyncSession, snap: TaxonomySnapshot, campaign) -> E.AdFacts:
    from app.models.campaigns import CampaignSegment

    node, kinds, adv_segs = await _advertiser(db, snap, campaign.org_id)
    seg_ids = list((await db.execute(select(CampaignSegment.segment_id)
                                     .where(CampaignSegment.campaign_id == campaign.id))).scalars())
    brand = campaign.brand_id
    if campaign.product_id and campaign.product_id in snap.products:
        pseg, pbrand = snap.products[campaign.product_id]
        seg_ids.append(pseg)
        brand = brand or pbrand
    source = "CAMPAIGN"
    if not seg_ids:
        seg_ids, source = adv_segs, ("ADVERTISER" if adv_segs else "NONE")
    return E.AdFacts(
        advertiser_org_id=campaign.org_id, advertiser_category=node, advertiser_category_kinds=kinds,
        segments=_segrefs(snap, seg_ids), segments_source=source, product_id=campaign.product_id,
        brand_id=brand, campaign_id=campaign.id, creative_id=None,
    )


async def creative_ad_facts(db: AsyncSession, snap: TaxonomySnapshot, creative, campaign=None) -> E.AdFacts:
    from app.models.campaigns import CreativeSegment

    node, kinds, adv_segs = await _advertiser(db, snap, creative.org_id)
    seg_ids = list((await db.execute(select(CreativeSegment.segment_id)
                                     .where(CreativeSegment.creative_id == creative.id))).scalars())
    brand = creative.brand_id
    product = creative.product_id
    if product and product in snap.products:
        pseg, pbrand = snap.products[product]
        seg_ids.append(pseg)
        brand = brand or pbrand
    source = "CREATIVE"
    if not seg_ids and campaign is not None:
        cf = await campaign_ad_facts(db, snap, campaign)
        seg_ids = [s.id for s in cf.segments]
        source = cf.segments_source
        brand = brand or cf.brand_id
        product = product or cf.product_id
    if not seg_ids:
        seg_ids, source = adv_segs, ("ADVERTISER" if adv_segs else "NONE")
    return E.AdFacts(
        advertiser_org_id=creative.org_id, advertiser_category=node, advertiser_category_kinds=kinds,
        segments=_segrefs(snap, seg_ids), segments_source=source, product_id=product, brand_id=brand,
        campaign_id=getattr(campaign, "id", None), creative_id=creative.id,
    )


# ─── fatos do estabelecimento ────────────────────────────────────────────────

async def establishment_facts(db: AsyncSession, snap: TaxonomySnapshot, est_ids: Iterable[uuid.UUID]) -> dict:
    from app.models.establishments import Establishment
    from app.models.taxonomy import EstablishmentProduct, EstablishmentSegment

    ids = list({_uuid(e) for e in est_ids})
    if not ids:
        return {}
    rows = (await db.execute(select(Establishment.id, Establishment.org_id, Establishment.category_id)
                             .where(Establishment.id.in_(ids)))).all()
    segs: dict[uuid.UUID, set] = {}
    for eid, sid in (await db.execute(select(EstablishmentSegment.establishment_id, EstablishmentSegment.segment_id)
                                      .where(EstablishmentSegment.establishment_id.in_(ids)))).all():
        segs.setdefault(eid, set()).add(sid)
    for eid, pid in (await db.execute(select(EstablishmentProduct.establishment_id, EstablishmentProduct.product_id)
                                      .where(EstablishmentProduct.establishment_id.in_(ids)))).all():
        if pid in snap.products:
            segs.setdefault(eid, set()).add(snap.products[pid][0])
    out = {}
    for eid, org_id, cat_id in rows:
        node, kinds = snap.node(cat_id)
        out[eid] = E.EstFacts(establishment_id=eid, org_id=org_id, category=node, category_kinds=kinds,
                              segments=_segrefs(snap, segs.get(eid, ())))
    return out


# ─── regras / exceções / overrides / concorrentes ───────────────────────────

def _valid_at(model, at: datetime):
    conds = []
    if hasattr(model, "valid_from"):
        conds.append(or_(model.valid_from.is_(None), model.valid_from <= at))
    if hasattr(model, "valid_until"):
        conds.append(or_(model.valid_until.is_(None), model.valid_until > at))
    return and_(*conds) if conds else True


async def load_rules(db: AsyncSession, snap: TaxonomySnapshot, at: datetime) -> list[E.RuleFact]:
    from app.models.conflicts import ConflictRule

    rows = (await db.execute(select(ConflictRule).where(ConflictRule.active.is_(True), _valid_at(ConflictRule, at))
                             )).scalars().all()
    out = []
    for r in rows:
        def kind_or_depth(t: str, oid: uuid.UUID):
            if t in ("AD_CATEGORY", "EST_CATEGORY"):
                return snap.nodes.get(oid, ("SECTOR", ()))[0]
            if t in ("AD_SEGMENT", "EST_SEGMENT"):
                p = snap.segments.get(oid, ())
                return max(len(p) - 1, 0)
            return None

        spec = E.specificity_for(r.subject_type, kind_or_depth(r.subject_type, r.subject_id),
                                 r.object_type, kind_or_depth(r.object_type, r.object_id))
        out.append(E.RuleFact(id=r.id, subject_type=r.subject_type, subject_id=r.subject_id,
                              object_type=r.object_type, object_id=r.object_id,
                              include_descendants=r.include_descendants, effect=r.effect, specificity=spec))
    return out


async def load_exceptions(db: AsyncSession, est_ids: Iterable[uuid.UUID], at: datetime) -> dict:
    from app.models.conflicts import ConflictException

    ids = list(est_ids)
    if not ids:
        return {}
    rows = (await db.execute(select(ConflictException).where(
        ConflictException.establishment_id.in_(ids), ConflictException.revoked_at.is_(None),
        ConflictException.valid_from <= at,
        or_(ConflictException.valid_until.is_(None), ConflictException.valid_until > at),
    ))).scalars().all()
    out: dict = {}
    for x in rows:
        out.setdefault(x.establishment_id, []).append(E.ExceptionFact(id=x.id, object_type=x.object_type,
                                                                      object_id=x.object_id))
    return out


async def load_overrides(db: AsyncSession, campaign_id: Optional[uuid.UUID], est_ids: Iterable[uuid.UUID],
                         at: datetime) -> dict:
    from app.models.conflicts import ConflictOverride

    ids = list(est_ids)
    if campaign_id is None or not ids:
        return {}
    rows = (await db.execute(select(ConflictOverride).where(
        ConflictOverride.campaign_id == campaign_id, ConflictOverride.establishment_id.in_(ids),
        ConflictOverride.revoked_at.is_(None), ConflictOverride.approved_at <= at,
        or_(ConflictOverride.expires_at.is_(None), ConflictOverride.expires_at > at),
    ))).scalars().all()
    return {o.establishment_id: E.OverrideFact(id=o.id, justification=o.justification) for o in rows}


async def competitor_orgs(db: AsyncSession, org_id: uuid.UUID) -> set:
    from app.models.taxonomy import CompetitorLink

    rows = (await db.execute(select(CompetitorLink.org_a_id, CompetitorLink.org_b_id).where(
        or_(CompetitorLink.org_a_id == org_id, CompetitorLink.org_b_id == org_id)))).all()
    return {b if a == org_id else a for a, b in rows}


# ─── gravação ────────────────────────────────────────────────────────────────

async def record_decision(db: AsyncSession, d: E.Decision, *, campaign_id=None, creative_id=None,
                          establishment_id=None, other_creative_id=None, ctx=None) -> E.Decision:
    from app.models.conflicts import ConflictDecision

    row = ConflictDecision(
        id=uuid.uuid4(), campaign_id=campaign_id, creative_id=creative_id, establishment_id=establishment_id,
        other_creative_id=other_creative_id, result=d.result, reason_code=d.reason_code, rule_path=d.rule_path,
        engine_version=d.engine_version, facts_hash=d.facts_hash, evaluated_by=getattr(ctx, "user_id", None),
        evaluated_at=utcnow(),
    )
    db.add(row)
    d.decision_id = row.id
    return d


# ─── API pública ─────────────────────────────────────────────────────────────

async def _campaign_creatives(db: AsyncSession, campaign_id: uuid.UUID, *, include_pending: bool = True) -> list:
    from app.models.campaigns import CampaignCreative, Creative

    q = (select(Creative).join(CampaignCreative, CampaignCreative.creative_id == Creative.id)
         .where(CampaignCreative.campaign_id == campaign_id, CampaignCreative.removed_at.is_(None)))
    if not include_pending:
        q = q.where(CampaignCreative.is_active.is_(True))
    return list((await db.execute(q.order_by(Creative.code))).scalars().all())


async def evaluate_campaign_establishments(
    db: AsyncSession,
    campaign,
    est_ids: Iterable[UUIDLike],
    *,
    at: Optional[datetime] = None,
    record: bool = False,
    ctx=None,
    creatives: Optional[list] = None,
    snap: Optional[TaxonomySnapshot] = None,
) -> dict:
    at = at or utcnow()
    ids = sorted({_uuid(e) for e in est_ids}, key=str)
    snap = snap or await load_taxonomy(db)
    est_facts = await establishment_facts(db, snap, ids)
    rules = await load_rules(db, snap, at)
    exceptions = await load_exceptions(db, ids, at)
    overrides = await load_overrides(db, campaign.id, ids, at)
    competitors = await competitor_orgs(db, campaign.org_id)
    same_network = bool(await get_setting(db, "conflicts.same_network_allow"))
    if creatives is None:
        creatives = await _campaign_creatives(db, campaign.id)
    ad_list: list[tuple[Optional[uuid.UUID], E.AdFacts]] = []
    if creatives:
        for cr in creatives:
            ad_list.append((cr.id, await creative_ad_facts(db, snap, cr, campaign)))
    else:
        ad_list.append((None, await campaign_ad_facts(db, snap, campaign)))
    out: dict = {}
    for eid in ids:
        ef = est_facts.get(eid)
        if ef is None:
            continue
        per = []
        for cid, ad in ad_list:
            per.append((cid, E.decide(
                ad, ef, blocked_policy_segment_ids=snap.blocked_policy, override=overrides.get(eid),
                exceptions=exceptions.get(eid, ()), rules=rules, competitor_link=ef.org_id in competitors,
                same_network_allow=same_network,
            )))
        d = per[0][1] if len(per) == 1 and per[0][0] is None else E.combine(per)
        if record:
            await record_decision(db, d, campaign_id=campaign.id,
                                  creative_id=per[0][0] if len(per) == 1 else None, establishment_id=eid, ctx=ctx)
        out[eid] = d
    return out


async def evaluate_conflict(db: AsyncSession, subject, establishment, *, at: Optional[datetime] = None,
                            record: bool = False, ctx=None, campaign=None) -> E.Decision:
    """Decisão anúncio × estabelecimento (``subject`` = Creative | Campaign | UUID de um deles)."""
    from app.models.campaigns import Campaign, Creative
    from app.models.establishments import Establishment

    at = at or utcnow()
    if isinstance(subject, (uuid.UUID, str)):
        sid = _uuid(subject)
        subject = await db.get(Campaign, sid) or await db.get(Creative, sid)
        if subject is None:
            raise NotFound()
    est_id = establishment.id if isinstance(establishment, Establishment) else _uuid(establishment)
    if isinstance(subject, Campaign):
        res = await evaluate_campaign_establishments(db, subject, [est_id], at=at, record=record, ctx=ctx)
        if est_id not in res:
            raise NotFound()
        return res[est_id]
    # criativo isolado (opcionalmente no contexto de uma campanha — override/exceção por campanha)
    snap = await load_taxonomy(db)
    ef = (await establishment_facts(db, snap, [est_id])).get(est_id)
    if ef is None:
        raise NotFound()
    ad = await creative_ad_facts(db, snap, subject, campaign)
    overrides = await load_overrides(db, getattr(campaign, "id", None), [est_id], at)
    d = E.decide(
        ad, ef, blocked_policy_segment_ids=snap.blocked_policy, override=overrides.get(est_id),
        exceptions=(await load_exceptions(db, [est_id], at)).get(est_id, ()), rules=await load_rules(db, snap, at),
        competitor_link=ef.org_id in await competitor_orgs(db, subject.org_id),
        same_network_allow=bool(await get_setting(db, "conflicts.same_network_allow")),
    )
    if record:
        await record_decision(db, d, campaign_id=getattr(campaign, "id", None), creative_id=subject.id,
                              establishment_id=est_id, ctx=ctx)
    return d


async def are_competitors(db: AsyncSession, creative_a, creative_b, *, record: bool = False, ctx=None) -> E.Decision:
    """Anúncio × anúncio: BLOCK = concorrentes (não dividem bloco; exclusividade)."""
    from app.models.campaigns import Creative

    a = creative_a if isinstance(creative_a, Creative) else await db.get(Creative, _uuid(creative_a))
    b = creative_b if isinstance(creative_b, Creative) else await db.get(Creative, _uuid(creative_b))
    if a is None or b is None:
        raise NotFound()
    snap = await load_taxonomy(db)
    fa = await creative_ad_facts(db, snap, a)
    fb = await creative_ad_facts(db, snap, b)
    link = b.org_id in await competitor_orgs(db, a.org_id)
    d = E.ad_vs_ad(fa, fb, competitor_link=link)
    if record:
        await record_decision(db, d, creative_id=a.id, other_creative_id=b.id, ctx=ctx)
    return d


async def competitor_pairs(db: AsyncSession, creative_ids: Iterable[UUIDLike]) -> set:
    """Todos os pares concorrentes entre os criativos informados (lote, determinístico)."""
    from app.models.campaigns import Creative
    from app.models.taxonomy import CompetitorLink

    ids = sorted({_uuid(c) for c in creative_ids}, key=str)
    if len(ids) < 2:
        return set()
    creatives = (await db.execute(select(Creative).where(Creative.id.in_(ids)))).scalars().all()
    snap = await load_taxonomy(db)
    facts = {c.id: await creative_ad_facts(db, snap, c) for c in creatives}
    orgs = {c.org_id for c in creatives}
    links = {
        frozenset((a, b)) for a, b in (await db.execute(select(CompetitorLink.org_a_id, CompetitorLink.org_b_id).where(
            CompetitorLink.org_a_id.in_(orgs), CompetitorLink.org_b_id.in_(orgs)))).all()
    }
    out: set = set()
    cl = sorted(creatives, key=lambda c: str(c.id))
    for i, a in enumerate(cl):
        for b in cl[i + 1:]:
            d = E.ad_vs_ad(facts[a.id], facts[b.id], competitor_link=frozenset((a.org_id, b.org_id)) in links)
            if d.result == E.BLOCK:
                out.add(frozenset((a.id, b.id)))
    return out
