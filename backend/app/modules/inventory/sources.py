"""
RD Mídia Indoor — Leitura dos domínios vizinhos pelo inventário/gerador (W-INV).

Isola TODO acoplamento com W-ORG (telas/estabelecimentos), W-CAMP (campanhas,
criativos, motor de conflitos, transições), W-MEDIA (descritores de mídia) e
W-PLAY (kills). Regras:
- Motor de conflitos ausente ⇒ falha FECHADA (tela inelegível; venda recusada).
- Somente criativos APROVADOS com mídia READY (sha256 conhecido) entram no manifesto.
- Transições de campanha são pedidas às funções de serviço do W-CAMP (se ausentes,
  nada é alterado e o fato é registrado em log — nunca simulado).
"""
from __future__ import annotations

import importlib
import uuid
from dataclasses import dataclass
from typing import Any, Iterable, Optional

import structlog
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.scheduler.manifest import MediaRef

log = structlog.get_logger("rd.inventory.sources")

PLAYABLE_CAMPAIGN_STATUSES_DEFAULT = ("APROVADA", "AGENDADA", "SINCRONIZANDO", "ATIVA")
CREATIVE_APPROVED = "APROVADO"
ALLOW = "ALLOW"
BLOCK = "BLOCK"


def _mod(name: str):
    try:
        return importlib.import_module(name)
    except ImportError:
        return None


def _fn(module: str, name: str):
    m = _mod(module)
    return getattr(m, name, None) if m is not None else None


def _u(v: Any) -> uuid.UUID:
    return v if isinstance(v, uuid.UUID) else uuid.UUID(str(v))


# ─── Telas ───────────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class ScreenInfo:
    id: uuid.UUID
    code: str
    establishment_id: uuid.UUID
    org_id: uuid.UUID
    timezone: str
    state: Optional[str]
    city_key: Optional[str]
    partner_plan_id: Optional[uuid.UUID]
    status: str
    sellable: bool
    establishment_active: bool
    orientation: str  # LANDSCAPE | PORTRAIT
    programming_mode: str = "ESTABLISHMENT_DEFAULT"  # W-ORG: ESTABLISHMENT_DEFAULT | CUSTOM (REQ-ORG-003)

    @property
    def operational(self) -> bool:
        return self.status in ("ACTIVE", "MAINTENANCE") and self.establishment_active


async def load_screens(db: AsyncSession, screen_ids: Iterable[Any]) -> dict[uuid.UUID, ScreenInfo]:
    from app.models.establishments import Establishment
    from app.models.screens import Screen

    ids = sorted({_u(s) for s in screen_ids}, key=str)
    if not ids:
        return {}
    rows = (await db.execute(
        select(Screen, Establishment).join(Establishment, Establishment.id == Screen.establishment_id)
        .where(Screen.id.in_(ids))
    )).all()
    out: dict[uuid.UUID, ScreenInfo] = {}
    for scr, est in rows:
        est_active = bool(getattr(est, "is_active", True)) and getattr(est, "deleted_at", None) is None
        orient = "PORTRAIT" if str(getattr(scr, "orientation", "HORIZONTAL")).upper() in ("VERTICAL", "PORTRAIT") \
            else "LANDSCAPE"
        out[scr.id] = ScreenInfo(
            id=scr.id,
            code=scr.friendly_id,
            establishment_id=est.id,
            org_id=est.org_id,
            timezone=est.timezone or "America/Sao_Paulo",
            state=getattr(est, "state", None),
            city_key=getattr(est, "city_key", None),
            partner_plan_id=getattr(est, "partner_plan_id", None),
            status=str(getattr(scr, "status", "ACTIVE")),
            sellable=bool(getattr(scr, "sellable", True)),
            establishment_active=est_active,
            orientation=orient,
            programming_mode=str(getattr(scr, "programming_mode", None) or "ESTABLISHMENT_DEFAULT"),
        )
    return out


async def screens_matching(db: AsyncSession, *, establishment_ids: Iterable[Any] = (), states: Iterable[str] = (),
                           city_keys: Iterable[str] = (), screen_ids: Iterable[Any] = (),
                           all_screens: bool = False) -> list[uuid.UUID]:
    """Telas (não descomissionadas) que casam com um alvo (emergência, disponibilidade)."""
    from app.models.establishments import Establishment
    from app.models.screens import Screen

    q = select(Screen.id).join(Establishment, Establishment.id == Screen.establishment_id).where(
        Screen.status != "DECOMMISSIONED")
    conds = []
    if not all_screens:
        est = [_u(e) for e in establishment_ids]
        scr = [_u(s) for s in screen_ids]
        if est:
            conds.append(Screen.establishment_id.in_(est))
        if scr:
            conds.append(Screen.id.in_(scr))
        if states:
            conds.append(Establishment.state.in_([s.upper() for s in states]))
        if city_keys:
            conds.append(Establishment.city_key.in_(list(city_keys)))
        if not conds:
            return []
        from sqlalchemy import or_

        q = q.where(or_(*conds))
    return sorted((await db.execute(q)).scalars().all(), key=str)


# ─── Campanhas ───────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class CampaignInfo:
    id: uuid.UUID
    org_id: uuid.UUID
    status: str
    name: str


async def load_campaigns(db: AsyncSession, campaign_ids: Iterable[Any], *, for_update: bool = False
                         ) -> dict[uuid.UUID, CampaignInfo]:
    from app.models.campaigns import Campaign

    ids = sorted({_u(c) for c in campaign_ids}, key=str)
    if not ids:
        return {}
    q = select(Campaign.id, Campaign.org_id, Campaign.status, Campaign.name).where(Campaign.id.in_(ids)).order_by(
        Campaign.id)
    if for_update:
        q = q.with_for_update(of=Campaign)
    return {r[0]: CampaignInfo(r[0], r[1], r[2], r[3]) for r in (await db.execute(q)).all()}


def playable_statuses() -> tuple[str, ...]:
    v = _fn("app.modules.campaigns.service", "PLAYABLE_STATUSES")
    if v:
        return tuple(v)
    v = _fn("app.modules.campaigns.states", "PLAYABLE_STATUSES")
    return tuple(v) if v else PLAYABLE_CAMPAIGN_STATUSES_DEFAULT


# ─── Motor de conflitos (W-CAMP) ─────────────────────────────────────────────

@dataclass(frozen=True)
class ConflictVerdict:
    result: str
    reason_code: str
    decision_id: Optional[uuid.UUID]

    @property
    def allowed(self) -> bool:
        return self.result == ALLOW


UNAVAILABLE = ConflictVerdict(BLOCK, "CONFLICT_ENGINE_UNAVAILABLE", None)


async def screen_conflicts(db: AsyncSession, campaign_id: Any, establishment_ids: Iterable[Any], *,
                           record: bool = False, ctx: Any = None) -> dict[uuid.UUID, ConflictVerdict]:
    """Decisão anúncio × estabelecimento para a campanha (falha FECHADA)."""
    from app.models.campaigns import Campaign

    ests = sorted({_u(e) for e in establishment_ids}, key=str)
    if not ests:
        return {}
    fn = _fn("app.modules.conflicts.service", "evaluate_campaign_establishments")
    camp = await db.get(Campaign, _u(campaign_id))
    if fn is None or camp is None:
        if fn is None:
            log.error("conflict_engine_unavailable")
        return {e: UNAVAILABLE for e in ests}
    try:
        res = await fn(db, camp, ests, record=record, ctx=ctx)
    except Exception as exc:  # motor indisponível ⇒ fechado
        log.error("conflict_engine_error", error_type=type(exc).__name__)
        return {e: UNAVAILABLE for e in ests}
    out: dict[uuid.UUID, ConflictVerdict] = {}
    for e in ests:
        d = res.get(e)
        if d is None:
            out[e] = ConflictVerdict(BLOCK, "ESTABLISHMENT_FACTS_MISSING", None)
        else:
            out[e] = ConflictVerdict(str(getattr(d, "result", BLOCK)), str(getattr(d, "reason_code", "")),
                                     getattr(d, "decision_id", None))
    return out


async def campaign_creative_ids(db: AsyncSession, campaign_ids: Iterable[Any]) -> dict[uuid.UUID, list[uuid.UUID]]:
    from app.models.campaigns import CampaignCreative

    ids = sorted({_u(c) for c in campaign_ids}, key=str)
    out: dict[uuid.UUID, list[uuid.UUID]] = {c: [] for c in ids}
    if not ids:
        return out
    q = select(CampaignCreative.campaign_id, CampaignCreative.creative_id).where(
        CampaignCreative.campaign_id.in_(ids))
    if hasattr(CampaignCreative, "removed_at"):
        q = q.where(CampaignCreative.removed_at.is_(None))
    for cid, crid in (await db.execute(q)).all():
        out.setdefault(cid, []).append(crid)
    return out


async def competitor_campaign_pairs(db: AsyncSession, campaign_ids: Iterable[Any]) -> frozenset:
    """Pares de campanhas CONCORRENTES (strings) — mesma função anúncio×anúncio do W-CAMP.

    Concorrência = algum par de criativos concorrente (``competitor_pairs``) OU os fatos
    declarados das campanhas (segmentos/produto/marca/anunciante) concorrem
    (``ad_vs_ad`` sobre ``campaign_ad_facts``) — cobre campanhas ainda sem mídia.
    """
    ids = sorted({_u(c) for c in campaign_ids}, key=str)
    if len(ids) < 2:
        return frozenset()
    svc = _mod("app.modules.conflicts.service")
    eng = _mod("app.modules.conflicts.engine")
    if svc is None or eng is None:
        raise RuntimeError("CONFLICT_ENGINE_UNAVAILABLE")
    from app.models.campaigns import Campaign

    pairs: set = set()
    by_campaign = await campaign_creative_ids(db, ids)
    owner: dict[uuid.UUID, uuid.UUID] = {}
    for cid, crs in by_campaign.items():
        for cr in crs:
            owner[cr] = cid
    if len(owner) >= 2:
        for pair in await svc.competitor_pairs(db, list(owner)):
            a, b = tuple(pair)
            ca, cb = owner.get(a), owner.get(b)
            if ca and cb and ca != cb:
                pairs.add(frozenset((str(ca), str(cb))))
    snap = await svc.load_taxonomy(db)
    camps = {c.id: c for c in (await db.execute(select(Campaign).where(Campaign.id.in_(ids)))).scalars().all()}
    facts = {cid: await svc.campaign_ad_facts(db, snap, c) for cid, c in camps.items()}
    orgs = {c.org_id for c in camps.values()}
    links: set = set()
    try:
        from app.models.taxonomy import CompetitorLink

        links = {frozenset((a, b)) for a, b in (await db.execute(
            select(CompetitorLink.org_a_id, CompetitorLink.org_b_id).where(
                CompetitorLink.org_a_id.in_(orgs), CompetitorLink.org_b_id.in_(orgs)))).all()}
    except ImportError:  # pragma: no cover
        links = set()
    cl = sorted(camps.values(), key=lambda c: str(c.id))
    for i, a in enumerate(cl):
        for b in cl[i + 1:]:
            d = eng.ad_vs_ad(facts[a.id], facts[b.id], competitor_link=frozenset((a.org_id, b.org_id)) in links)
            if getattr(d, "result", None) == BLOCK:
                pairs.add(frozenset((str(a.id), str(b.id))))
    return frozenset(pairs)


# ─── Criativos tocáveis e mídia ──────────────────────────────────────────────

@dataclass(frozen=True)
class CreativeInfo:
    id: uuid.UUID
    code: str
    kind: str
    org_id: uuid.UUID
    duration_s: int
    media_file_id: uuid.UUID
    status: str
    order: int = 0


async def load_creatives(db: AsyncSession, creative_ids: Iterable[Any]) -> dict[uuid.UUID, CreativeInfo]:
    from app.models.campaigns import Creative

    ids = sorted({_u(c) for c in creative_ids}, key=str)
    if not ids:
        return {}
    out = {}
    for c in (await db.execute(select(Creative).where(Creative.id.in_(ids)))).scalars().all():
        out[c.id] = CreativeInfo(c.id, c.code, c.kind, c.org_id, int(c.duration_seconds), c.media_file_id,
                                 str(c.status))
    return out


async def playable_campaign_creatives(db: AsyncSession, campaign_ids: Iterable[Any], screen_id: Any = None
                                      ) -> dict[uuid.UUID, list[CreativeInfo]]:
    """Criativos APROVADOS (revisão vigente) por campanha, na ordem de rotação.

    Usa ``campaigns.service.playable_campaign_specs`` (revisão APROVADA vigente) quando
    publicado pelo W-CAMP; senão, os vínculos ativos de ``campaign_creatives``.
    """
    from app.models.campaigns import CampaignCreative, Creative

    ids = sorted({_u(c) for c in campaign_ids}, key=str)
    out: dict[uuid.UUID, list[CreativeInfo]] = {c: [] for c in ids}
    if not ids:
        return out
    specs_fn = _fn("app.modules.campaigns.service", "playable_campaign_specs")
    if specs_fn is not None:
        try:
            specs = await specs_fn(db, screen_id=screen_id)
        except TypeError:
            specs = await specs_fn(db)
        wanted = set(ids)
        crs_by_c: dict[uuid.UUID, list[uuid.UUID]] = {}
        for sp in specs or []:
            cid = _u(_get(sp, "campaign_id"))
            if cid not in wanted:
                continue
            crs_by_c[cid] = [_u(x) for x in (_get(sp, "creative_ids") or [])]
        infos = await load_creatives(db, [x for v in crs_by_c.values() for x in v])
        for cid, crs in crs_by_c.items():
            out[cid] = [infos[x] for x in crs if x in infos and infos[x].status == CREATIVE_APPROVED]
        return out
    q = (select(CampaignCreative.campaign_id, CampaignCreative.order, Creative)
         .join(Creative, Creative.id == CampaignCreative.creative_id)
         .where(CampaignCreative.campaign_id.in_(ids), CampaignCreative.is_active.is_(True),
                Creative.status == CREATIVE_APPROVED))
    if hasattr(CampaignCreative, "removed_at"):
        q = q.where(CampaignCreative.removed_at.is_(None))
    if hasattr(CampaignCreative, "pending"):
        q = q.where(CampaignCreative.pending.is_(False))
    rows = (await db.execute(q)).all()
    for cid, order, c in sorted(rows, key=lambda r: (str(r[0]), r[1], r[2].code)):
        out[cid].append(CreativeInfo(c.id, c.code, c.kind, c.org_id, int(c.duration_seconds), c.media_file_id,
                                     str(c.status), int(order or 0)))
    return out


def _get(obj: Any, key: str) -> Any:
    return obj.get(key) if isinstance(obj, dict) else getattr(obj, key, None)


async def media_refs(db: AsyncSession, media_ids: Iterable[Any]) -> dict[uuid.UUID, MediaRef]:
    """Somente mídias READY com sha256 conhecido (W-MEDIA ``get_media_descriptors``)."""
    ids = sorted({_u(m) for m in media_ids}, key=str)
    if not ids:
        return {}
    fn = _fn("app.modules.media.service", "get_media_descriptors")
    out: dict[uuid.UUID, MediaRef] = {}
    if fn is not None:
        desc = await fn(db, ids)
        for mid, d in desc.items():
            sha = _get(d, "sha256")
            if not sha:
                continue
            out[_u(mid)] = MediaRef(str(_u(mid)), str(sha), int(_get(d, "size") or 0), str(_get(d, "mime") or ""),
                                    _int(_get(d, "width")), _int(_get(d, "height")), _int(_get(d, "duration_ms")))
        return out
    from app.models.media import MediaFile

    for m in (await db.execute(select(MediaFile).where(MediaFile.id.in_(ids)))).scalars().all():
        if m.status != "READY" or not m.sha256 or getattr(m, "deleted_at", None) is not None:
            continue
        out[m.id] = MediaRef(str(m.id), m.sha256, int(m.size_bytes or 0), m.mime_type or "",
                             _int(m.width), _int(m.height), _int(m.duration_ms))
    return out


def _int(v: Any) -> Optional[int]:
    if v is None:
        return None
    return int(v)


async def revoked_creatives(db: AsyncSession, screen_id: Any) -> list[str]:
    fn = _fn("app.modules.playback.kills", "revoked_creative_ids")
    if fn is None:
        log.warning("kills_module_unavailable")
        return []
    return sorted(str(x) for x in await fn(db, _u(screen_id)))


# ─── Transições de campanha (W-CAMP) ─────────────────────────────────────────

async def campaign_hook(name: str, db: AsyncSession, *args: Any, **kwargs: Any) -> bool:
    fn = _fn("app.modules.campaigns.service", name)
    if fn is None:
        log.warning("campaign_hook_unavailable", hook=name)
        return False
    await fn(db, *args, **kwargs)
    return True
