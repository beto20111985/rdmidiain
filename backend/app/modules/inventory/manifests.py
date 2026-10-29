"""
RD Mídia Indoor — GERADOR ÚNICO de manifesto RDPP/1 (W-INV; protocolo §5; A6 §10.8).

Fluxo de ``generate_manifest(db, screen_id)``:
1. linha ``screen_inventory_state`` da tela travada (versão monotônica sob lock);
2. horizonte = 7 dias locais a partir da meia-noite local de hoje (truncado na
   próxima mudança de política; ``repeat_period_ms`` = 7 d);
3. linhas BOOKED da tela → MESMO empacotador da venda (modo best-effort: fichas sem
   posição são alertadas, nunca inventadas);
4. peças tocáveis: campanhas em status tocável (revisão APROVADA vigente do W-CAMP),
   conflito ALLOW reavaliado agora, criativos APROVADOS, mídia READY (sha256),
   validade = 00:00 do 1º dia → 23:59:59.999 local do último dia (fuso da tela);
5. ``internal_pattern`` (lista ordenada do estabelecimento/tela), ``fallback``,
   ``emergency`` (janela/alvo), ``revoked`` (kills do W-PLAY);
6. se o conteúdo (sem campos voláteis) não mudou ⇒ devolve a versão vigente;
   senão nova versão, assinada ES256 sobre RDJ1 (``app.modules.playback.protocol``);
7. campanhas APROVADA/AGENDADA presentes no plano → ``mark_syncing``; com contratação
   futura (fora do horizonte) → ``mark_scheduled`` (funções do W-CAMP; lock da
   campanha com SKIP LOCKED — nunca espera ⇒ sem deadlock com checkouts).
``invalidate_screens`` gera as versões e publica ``screen.sync`` APÓS o commit.
"""
from __future__ import annotations

import gzip
import hashlib
import uuid
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Any, Iterable, Optional, Sequence

import structlog
from sqlalchemy import and_, func, or_, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.settings_service import get_setting
from app.core.timeutils import end_of_local_day_utc, local_date, start_of_local_day_utc
from app.models.content import EmergencyBroadcast, FallbackContent, InternalContent
from app.models.inventory import BookingPattern, BookingScreen, ScreenInventoryState
from app.models.manifests import ManifestVersion
from app.modules.inventory import keys as K
from app.modules.inventory import notify as N
from app.modules.inventory import sources as S
from app.modules.inventory.policies import resolve_for_screens
from app.modules.inventory.state import db_now, get_or_create_state, ms_of, set_lock_timeout
from app.modules.inventory.types import line_from_row
from app.modules.playback import protocol as P
from app.modules.scheduler.manifest import (
    MAX_VALID_UNTIL_MS,
    CreativeRef,
    EmergencyRef,
    ManifestInputs,
    assemble_manifest,
    content_view,
    default_settings,
    referenced_creatives,
    referenced_media_ids,
)
from app.modules.scheduler.packer import Packer

log = structlog.get_logger("rd.inventory.manifests")

HORIZON_DAYS = 7
DEFER_THRESHOLD = 25  # acima disso a regeneração vai para o worker (após o commit)
KEEP_CONTENT_VERSIONS = 5
# Retenção do CONTEÚDO das versões (validação de PoP offline com até 28+ dias de atraso — alerta de
# 4 semanas): mínimo 35 dias, configurável em ``manifest.content_retention_days``. As linhas
# (hashes + listas de criativos/campanhas/mídias usadas por ``get_manifest_refs``) NUNCA são apagadas.
KEEP_CONTENT_DAYS = 35
RETENTION_SETTING = "manifest.content_retention_days"


def _register_settings() -> None:
    from app.core.settings_service import REGISTRY, SettingDef, register

    if RETENTION_SETTING not in REGISTRY:
        register(SettingDef(RETENTION_SETTING, KEEP_CONTENT_DAYS, "int",
                            "Dias de retenção do conteúdo das versões de manifesto (validação de PoP offline; "
                            "mínimo 35).", min=KEEP_CONTENT_DAYS, max=3650, category="scheduler"))


_register_settings()


async def content_retention_days(db: AsyncSession) -> int:
    v = int(await get_setting(db, RETENTION_SETTING))
    return max(v, KEEP_CONTENT_DAYS)


@dataclass
class SignedManifest:
    screen_id: uuid.UUID
    version: int
    manifest: dict
    signature: str
    key_id: str
    content_sha256: str
    plan_sha256: str
    generated_at: datetime
    created: bool = False
    alg: str = P.ALG

    def envelope(self) -> dict:
        return {"alg": self.alg, "key_id": self.key_id, "signature": self.signature, "manifest": self.manifest}

    @property
    def etag(self) -> str:
        return f'"{self.version}"'


def _gz(data: bytes) -> bytes:
    return gzip.compress(data, compresslevel=6, mtime=0)


def _ungz(data: bytes) -> bytes:
    return gzip.decompress(data)


def _from_row(row: ManifestVersion, manifest: Optional[dict] = None) -> SignedManifest:
    if manifest is None:
        import json

        manifest = json.loads(_ungz(row.content_gz).decode("utf-8")) if row.content_gz else {}
    return SignedManifest(row.screen_id, int(row.version), manifest, row.signature, row.key_id, row.content_sha256,
                          row.plan_sha256, row.generated_at)


async def _latest(db: AsyncSession, screen_id: uuid.UUID) -> Optional[ManifestVersion]:
    return (await db.execute(select(ManifestVersion).where(ManifestVersion.screen_id == screen_id)
                             .order_by(ManifestVersion.version.desc()).limit(1))).scalar_one_or_none()


# ─── Dados do manifesto ──────────────────────────────────────────────────────

async def _creative_refs(db: AsyncSession, creative_ids: Iterable[uuid.UUID]) -> dict[uuid.UUID, tuple]:
    """{creative_id: (CreativeInfo, MediaRef)} apenas APROVADOS com mídia READY."""
    infos = await S.load_creatives(db, creative_ids)
    infos = {k: v for k, v in infos.items() if v.status == S.CREATIVE_APPROVED}
    media = await S.media_refs(db, {v.media_file_id for v in infos.values()})
    return {k: (v, media[v.media_file_id]) for k, v in infos.items() if v.media_file_id in media}


def _ref(info, media, *, campaign_id=None, advertiser_id=None, valid_from_ms=0,
         valid_until_ms=MAX_VALID_UNTIL_MS, kind=None) -> CreativeRef:
    return CreativeRef(
        creative_id=str(info.id), code=info.code, kind=kind or info.kind, media=media,
        duration_ms=int(info.duration_s) * 1000, campaign_id=str(campaign_id) if campaign_id else None,
        advertiser_id=str(advertiser_id) if advertiser_id else None, valid_from_ms=int(valid_from_ms),
        valid_until_ms=int(valid_until_ms),
    )


async def _internal_items(db: AsyncSession, info: S.ScreenInfo) -> list[CreativeRef]:
    """Programação interna conforme ``screens.programming_mode`` (W-ORG; REQ-ORG-003, #77–#79):
    ESTABLISHMENT_DEFAULT ⇒ lista comum do estabelecimento (``screen_id IS NULL``), ignorando a
    lista própria da tela (pode ser preparada antes da troca de modo); CUSTOM ⇒ SOMENTE a lista
    própria da tela — se vazia, o bloco interno é preenchido pelo fallback (RDPP §5.2)."""
    custom = info.programming_mode == "CUSTOM"
    rows = (await db.execute(
        select(InternalContent).where(
            InternalContent.establishment_id == info.establishment_id, InternalContent.active.is_(True),
            InternalContent.screen_id == info.id if custom else InternalContent.screen_id.is_(None))
        .order_by(InternalContent.position, InternalContent.id)
    )).scalars().all()
    chosen = list(rows)
    refs = await _creative_refs(db, [r.creative_id for r in chosen])
    out = []
    for r in chosen:
        if r.creative_id in refs:
            ci, m = refs[r.creative_id]
            out.append(_ref(ci, m, kind="INTERNAL"))
    return out


async def _fallback_items(db: AsyncSession, info: S.ScreenInfo) -> list[CreativeRef]:
    rows = (await db.execute(
        select(FallbackContent).where(FallbackContent.active.is_(True), or_(
            FallbackContent.scope == "GLOBAL",
            and_(FallbackContent.scope == "ESTABLISHMENT", FallbackContent.establishment_id == info.establishment_id)))
    )).scalars().all()
    rows = sorted(rows, key=lambda r: (0 if r.scope == "ESTABLISHMENT" else 1, r.position, str(r.id)))
    refs = await _creative_refs(db, [r.creative_id for r in rows])
    return [_ref(*refs[r.creative_id], kind="FALLBACK") for r in rows if r.creative_id in refs]


def _targets_screen(e: EmergencyBroadcast, info: S.ScreenInfo) -> bool:
    ids = {str(x) for x in (e.target_ids or [])}
    if e.target_kind == "ALL":
        return True
    if e.target_kind == "SCREENS":
        return str(info.id) in ids
    if e.target_kind == "ESTABLISHMENTS":
        return str(info.establishment_id) in ids
    if e.target_kind == "STATES":
        return (info.state or "").upper() in {x.upper() for x in ids}
    if e.target_kind == "CITIES":
        return (info.city_key or "") in ids
    return False


async def _emergencies(db: AsyncSession, info: S.ScreenInfo, from_dt: datetime, now: datetime) -> list[EmergencyRef]:
    rows = (await db.execute(select(EmergencyBroadcast).where(
        EmergencyBroadcast.status == "ACTIVE", EmergencyBroadcast.ends_at > min(from_dt, now))
        .order_by(EmergencyBroadcast.starts_at, EmergencyBroadcast.id))).scalars().all()
    rows = [e for e in rows if _targets_screen(e, info)]
    refs = await _creative_refs(db, [e.creative_id for e in rows])
    out = []
    for e in rows:
        if e.creative_id not in refs:
            log.error("emergency_creative_not_ready", emergency_id=str(e.id))
            continue
        ci, m = refs[e.creative_id]
        out.append(EmergencyRef(str(e.id), _ref(ci, m), ms_of(e.starts_at), ms_of(e.ends_at)))
    return out


async def _settings(db: AsyncSession, info: S.ScreenInfo) -> dict:
    days = await get_setting(db, "player.offline_alert_days")
    panel = ""
    try:
        from app.modules.playback.config import panel_base_url

        panel = panel_base_url()
    except Exception:  # pragma: no cover
        from app.core.config import settings as cfg

        panel = str(getattr(cfg, "FRONTEND_URL", "") or "")
    return default_settings(orientation=info.orientation, offline_alert_days=list(days), panel_url=panel)


@dataclass
class _Build:
    manifest: dict
    plan_sha256: str
    unplaced: list
    in_plan_campaigns: set
    future_campaigns: set
    horizon_from: datetime
    horizon_to: datetime


async def build_manifest(db: AsyncSession, screen_id: uuid.UUID, *, now: Optional[datetime] = None,
                         anchor_ms: Optional[int] = None) -> _Build:
    """Monta (sem gravar) o manifesto da tela — usado pela geração e pela pré-visualização."""
    info = (await S.load_screens(db, [screen_id])).get(screen_id)
    if info is None:
        from app.core.errors import NotFound

        raise NotFound()
    now = now or await db_now(db)
    tz = info.timezone
    today = local_date(now, tz)
    resolver = await resolve_for_screens(db, [info])
    rot = resolver.for_day(info, today)
    change = resolver.next_change_after(info, today, HORIZON_DAYS - 1)
    h_from = start_of_local_day_utc(today, tz)
    h_to = h_from + timedelta(days=HORIZON_DAYS)
    if change is not None:
        h_to = start_of_local_day_utc(change, tz)
    days = []
    d = today
    while start_of_local_day_utc(d, tz) < h_to:
        days.append(d)
        d += timedelta(days=1)

    # linhas BOOKED (ocupam capacidade mesmo se a campanha está pausada / sem peça)
    rows = (await db.execute(
        select(BookingScreen.hold_id, BookingScreen.status, BookingPattern)
        .join(BookingPattern, BookingPattern.id == BookingScreen.pattern_id)
        .where(BookingScreen.screen_id == screen_id, BookingScreen.status == "BOOKED",
               BookingPattern.date_to >= today)
    )).all()
    lines = [line_from_row(bp) for _, _, bp in rows]
    by_line = {str(bp.id): (hold_id, bp) for hold_id, _, bp in rows}
    camp_ids = sorted({bp.campaign_id for _, _, bp in rows}, key=str)
    try:
        competitors = await S.competitor_campaign_pairs(db, camp_ids) if len(camp_ids) >= 2 else frozenset()
    except RuntimeError:
        # motor indisponível: trata TODAS as campanhas distintas como concorrentes (mais seguro)
        competitors = frozenset(frozenset((str(a), str(b))) for i, a in enumerate(camp_ids) for b in camp_ids[i + 1:])
    plans = Packer(str(screen_id), tz, competitors=competitors, strict=False).pack_days(
        [(x, rot) for x in days], lines)
    unplaced = [u for p in plans for u in p.unplaced]

    # peças tocáveis por linha
    playable = {}
    specs_fn = S._fn("app.modules.campaigns.service", "playable_campaign_specs")
    if specs_fn is not None:
        for sp in await specs_fn(db, screen_id=screen_id):
            playable[S._u(S._get(sp, "campaign_id"))] = sp
    else:
        camps = await S.load_campaigns(db, camp_ids)
        playable = {cid: None for cid, c in camps.items() if c.status in S.playable_statuses()}
    verdicts: dict = {}
    if playable:
        for cid in sorted(set(playable) & set(camp_ids), key=str):
            verdicts[cid] = (await S.screen_conflicts(db, cid, [info.establishment_id])).get(info.establishment_id,
                                                                                            S.UNAVAILABLE)
    creative_lists = await S.playable_campaign_creatives(db, [c for c in playable if c in verdicts], screen_id)
    all_creatives = {c.id for v in creative_lists.values() for c in v}
    refs = await _creative_refs(db, all_creatives)
    line_creatives: dict[str, list[CreativeRef]] = {}
    in_plan: set = set()
    blocked = []
    for lid, (hold_id, bp) in by_line.items():
        cid = bp.campaign_id
        if cid not in playable:
            continue
        v = verdicts.get(cid)
        if v is None or not v.allowed:
            blocked.append(str(cid))
            continue
        if playable[cid] is not None:
            # reservas EM VIGOR da revisão aprovada: a própria + as herdadas (renovação estende o
            # período com um hold novo — todas tocam; cada padrão cobre só o seu período, sem dupla contagem)
            live = {S._u(x) for x in (S._get(playable[cid], "hold_ids") or [])}
            if S._get(playable[cid], "hold_id") is not None:
                live.add(S._u(S._get(playable[cid], "hold_id")))
            if live and hold_id not in live:
                continue  # contratação de revisão ainda não aprovada: ocupa, mas não toca
        vf = ms_of(start_of_local_day_utc(bp.date_from, tz))
        vu = ms_of(end_of_local_day_utc(bp.date_to, tz))  # 23:59:59.999 local
        crs = []
        for ci in creative_lists.get(cid, []):
            if ci.id in refs and ci.duration_s == int(bp.duration_units) * 30:
                c_info, media = refs[ci.id]
                crs.append(_ref(c_info, media, campaign_id=cid, advertiser_id=bp.advertiser_org_id,
                                valid_from_ms=vf, valid_until_ms=vu))
        if crs:
            line_creatives[lid] = crs
    h_from_ms, h_to_ms = ms_of(h_from), ms_of(h_to)
    for p in plans:
        for b, blk in zip(p.frame.buckets, p.blocks):
            if h_from_ms <= b < h_to_ms:
                for pl in blk:
                    if pl.line_id in line_creatives:
                        in_plan.add(by_line[pl.line_id][1].campaign_id)
    future = {bp.campaign_id for _, (_, bp) in by_line.items()
              if bp.campaign_id in playable and bp.date_from >= days[-1] + timedelta(days=1)} - in_plan
    st = await db.get(ScreenInventoryState, screen_id)
    anchor = anchor_ms if anchor_ms is not None else (ms_of(st.anchor_utc) if st and st.anchor_utc else None)
    inputs = ManifestInputs(
        screen_id=str(screen_id), screen_code=info.code, timezone=tz, rotation=rot, anchor_ms=anchor,
        horizon_from_ms=h_from_ms, horizon_to_ms=h_to_ms, settings=await _settings(db, info), day_plans=plans,
        line_creatives=line_creatives, internal_items=await _internal_items(db, info),
        fallback_items=await _fallback_items(db, info), emergencies=await _emergencies(db, info, h_from, now),
        revoked=await S.revoked_creatives(db, screen_id),
    )
    manifest = assemble_manifest(inputs)
    plan_sha = hashlib.sha256(P.canonical_bytes(content_view(manifest))).hexdigest()
    if blocked:
        log.warning("manifest_conflict_blocked", screen_id=str(screen_id), campaigns=sorted(set(blocked)))
    return _Build(manifest, plan_sha, unplaced, in_plan, future, h_from, h_to)


# ─── Geração / consulta ──────────────────────────────────────────────────────

async def _campaign_transitions(db: AsyncSession, in_plan: set, future: set) -> None:
    """APROVADA/AGENDADA no plano → SINCRONIZANDO; APROVADA só futura → AGENDADA (sem esperar lock)."""
    from app.models.campaigns import Campaign

    for cid, hook, allowed in (
        *[(c, "mark_syncing", ("APROVADA", "AGENDADA")) for c in sorted(in_plan, key=str)],
        *[(c, "mark_scheduled", ("APROVADA",)) for c in sorted(future, key=str)],
    ):
        st = (await db.execute(select(Campaign.status).where(Campaign.id == cid)
                               .with_for_update(skip_locked=True))).scalar_one_or_none()
        if st is None or st not in allowed:
            continue
        try:
            async with db.begin_nested():
                await S.campaign_hook(hook, db, cid)
        except Exception as exc:
            log.error("campaign_transition_failed", hook=hook, campaign_id=str(cid), error_type=type(exc).__name__)


async def generate_manifest(db: AsyncSession, screen_id: Any, *, reason: str = "on_demand",
                            force: bool = False) -> SignedManifest:
    sid = S._u(screen_id)
    st, info = await get_or_create_state(db, sid)
    await set_lock_timeout(db)
    st = (await db.execute(select(ScreenInventoryState).where(ScreenInventoryState.screen_id == sid)
                           .with_for_update().execution_options(populate_existing=True))).scalar_one()
    if st.timezone != info.timezone:
        st.timezone = info.timezone
    now = await db_now(db)
    b = await build_manifest(db, sid, now=now)
    latest = await _latest(db, sid)
    if latest is not None and latest.plan_sha256 == b.plan_sha256 and not force and latest.content_gz:
        return _from_row(latest)
    version = int(st.manifest_version) + 1
    st.manifest_version = version
    st.last_manifest_at = now
    m = b.manifest
    m["version"] = version
    m["manifest_id"] = str(uuid.uuid4())
    m["generated_at_ms"] = ms_of(now)
    raw = P.canonical_bytes(m)
    key, kid, _ = K.signing_key()
    sig = P.sign_bytes(key, raw)
    creative_ids, campaign_ids = referenced_creatives(m)
    row = ManifestVersion(
        screen_id=sid, version=version, manifest_id=uuid.UUID(m["manifest_id"]), content_gz=_gz(raw),
        content_sha256=hashlib.sha256(raw).hexdigest(), plan_sha256=b.plan_sha256, size_bytes=len(raw),
        signature=sig, key_id=kid, reason=(reason or "on_demand")[:64], generated_at=now,
        horizon_from=b.horizon_from, horizon_to=b.horizon_to,
        media_ids=[uuid.UUID(x) for x in referenced_media_ids(m)],
        creative_ids=[uuid.UUID(x) for x in creative_ids], campaign_ids=[uuid.UUID(x) for x in campaign_ids],
        unplaced_count=len(b.unplaced),
    )
    db.add(row)
    await db.flush()
    if b.unplaced:
        log.warning("manifest_unplaced", screen_id=str(sid), version=version, count=len(b.unplaced))
        await N.notify_safe(db, N.staff("inventory.view"), "inventory.unplaced_insertions",
                            {"screen_code": info.code, "screen_id": str(sid), "version": version,
                             "count": len(b.unplaced)}, dedupe_key=f"inventory.unplaced:{sid}:{b.plan_sha256[:16]}")
    await _campaign_transitions(db, b.in_plan_campaigns, b.future_campaigns)
    sm = SignedManifest(sid, version, m, sig, kid, row.content_sha256, b.plan_sha256, now, created=True)
    return sm


async def current_manifest(db: AsyncSession, screen_id: Any) -> SignedManifest:
    """Versão vigente (gera se não houver ou se o horizonte começa antes de hoje local)."""
    sid = S._u(screen_id)
    latest = await _latest(db, sid)
    if latest is not None and latest.content_gz:
        info = (await S.load_screens(db, [sid])).get(sid)
        now = await db_now(db)
        if info is not None and latest.horizon_from >= start_of_local_day_utc(local_date(now, info.timezone),
                                                                             info.timezone):
            return _from_row(latest)
    sm = await generate_manifest(db, sid, reason="stale_horizon" if latest else "first")
    if sm.created:
        _defer_sync(db, info=(await S.load_screens(db, [sid])).get(sid), sm=sm, reason="stale_horizon")
    return sm


async def get_manifest_version(db: AsyncSession, screen_id: Any, version: int) -> Optional[dict]:
    row = await db.get(ManifestVersion, (S._u(screen_id), int(version)))
    if row is None or not row.content_gz:
        return None
    return _from_row(row).manifest


async def get_manifest_refs(db: AsyncSession, screen_id: Any, version: int) -> Optional[dict]:
    """Referências de uma versão (mesmo após poda do conteúdo) — validação de PoP."""
    row = await db.get(ManifestVersion, (S._u(screen_id), int(version)))
    if row is None:
        return None
    return {"creative_ids": [str(x) for x in row.creative_ids], "campaign_ids": [str(x) for x in row.campaign_ids],
            "media_ids": [str(x) for x in row.media_ids], "generated_at": row.generated_at,
            "horizon_from": row.horizon_from, "horizon_to": row.horizon_to}


async def screens_with_creative(db: AsyncSession, creative_id: Any) -> list[uuid.UUID]:
    """Telas cujo manifesto MAIS RECENTE referencia o criativo (kill global do W-PLAY)."""
    rows = (await db.execute(text("""
        SELECT l.screen_id FROM (
            SELECT DISTINCT ON (screen_id) screen_id, creative_ids
              FROM manifest_versions ORDER BY screen_id, version DESC) l
         WHERE CAST(:cid AS uuid) = ANY(l.creative_ids)
         ORDER BY l.screen_id
    """), {"cid": str(S._u(creative_id))})).all()
    return [r[0] for r in rows]


def _defer_sync(db: AsyncSession, *, info: Optional[S.ScreenInfo], sm: SignedManifest, reason: str) -> None:
    N.defer_event(db, "screen.sync", {"screen_id": str(sm.screen_id), "version": sm.version, "reason": reason},
                  screen_id=sm.screen_id, org_id=info.org_id if info else None,
                  establishment_id=info.establishment_id if info else None)


async def invalidate_screens(db: AsyncSession, screen_ids: Iterable[Any], reason: str) -> dict:
    """Gera nova versão (se o conteúdo mudou) e publica ``screen.sync`` após o commit.

    Conjuntos grandes (> ``DEFER_THRESHOLD``) são regenerados pelo worker logo após o
    commit (``app.workers.tasks.scheduler.regenerate_manifests``)."""
    sids = sorted({S._u(s) for s in screen_ids}, key=str)
    out: dict = {}
    if not sids:
        return out
    if len(sids) > DEFER_THRESHOLD:
        ids = [str(s) for s in sids]

        def _enqueue() -> None:
            from app.workers.tasks.scheduler import regenerate_manifests

            regenerate_manifests.delay(ids, reason[:64])

        N.after_commit(db, _enqueue)
        return {s: None for s in sids}
    infos = await S.load_screens(db, sids)
    for sid in sids:
        if sid not in infos:
            continue
        sm = await generate_manifest(db, sid, reason=reason)
        out[sid] = sm.version
        if sm.created:
            _defer_sync(db, info=infos.get(sid), sm=sm, reason=reason)
    return out


async def invalidate_campaign(db: AsyncSession, campaign_id: Any, reason: str) -> dict:
    sids = (await db.execute(select(BookingScreen.screen_id).where(
        BookingScreen.campaign_id == S._u(campaign_id), BookingScreen.status.in_(("BOOKED", "SUSPENDED")))
        .distinct())).scalars().all()
    return await invalidate_screens(db, sids, reason)


async def mark_manifest_applied(db: AsyncSession, screen_id: Any, version: int, device_id: Any) -> bool:
    """Ack do player (W-PLAY): grava aplicação e pede SINCRONIZANDO → ATIVA ao W-CAMP."""
    sid = S._u(screen_id)
    row = await db.get(ManifestVersion, (sid, int(version)))
    if row is None:
        return False
    if row.applied_at is None:
        row.applied_at = await db_now(db)
        row.applied_by_device = S._u(device_id) if device_id else None
    if row.campaign_ids:
        try:
            async with db.begin_nested():
                await S.campaign_hook("mark_active_from_ack", db, list(row.campaign_ids), screen_id=sid,
                                      manifest_version=int(version))
        except Exception as exc:
            log.error("mark_active_from_ack_failed", screen_id=str(sid), error_type=type(exc).__name__)
    await db.flush()
    return True


async def prune_manifest_content(db: AsyncSession) -> int:
    """Poda o CONTEÚDO (bytes) de versões antigas: geradas há mais de ``manifest.content_retention_days``
    (≥ 35) E fora das 5 últimas da tela. Hashes, assinatura e listas de referências são mantidos."""
    days = await content_retention_days(db)
    res = await db.execute(text("""
        UPDATE manifest_versions mv SET content_gz = NULL
          FROM (SELECT screen_id, version,
                       row_number() OVER (PARTITION BY screen_id ORDER BY version DESC) AS rn
                  FROM manifest_versions WHERE content_gz IS NOT NULL) r
         WHERE mv.screen_id = r.screen_id AND mv.version = r.version
           AND r.rn > :keep AND mv.generated_at < now() - make_interval(days => :days)
    """), {"keep": KEEP_CONTENT_VERSIONS, "days": days})
    return int(res.rowcount or 0)


async def media_reference_counts(db: AsyncSession, media_ids: Iterable[Any]) -> dict:
    """Referenciador do W-MEDIA: mídias em manifestos vigentes/recentes (rollback) não podem ser apagadas."""
    ids = [S._u(m) for m in media_ids]
    if not ids:
        return {}
    rows = (await db.execute(text("""
        SELECT m AS media_id, count(*) AS n
          FROM manifest_versions mv, unnest(mv.media_ids) AS m
         WHERE m = ANY(:ids) AND (mv.content_gz IS NOT NULL
                                  OR mv.generated_at > now() - make_interval(days => :days))
         GROUP BY m
    """), {"ids": ids, "days": await content_retention_days(db)})).all()
    return {r[0]: int(r[1]) for r in rows}


def stale_screens_sql() -> Any:
    """Telas cujo último manifesto começa antes da meia-noite local de hoje (regeneração diária)."""
    return text("""
        SELECT s.screen_id
          FROM screen_inventory_state s
          LEFT JOIN LATERAL (SELECT horizon_from FROM manifest_versions mv
                              WHERE mv.screen_id = s.screen_id ORDER BY version DESC LIMIT 1) l ON true
         WHERE l.horizon_from IS NULL
            OR l.horizon_from < (date_trunc('day', now() AT TIME ZONE s.timezone) AT TIME ZONE s.timezone)
         ORDER BY s.screen_id
         LIMIT :limit
    """)
