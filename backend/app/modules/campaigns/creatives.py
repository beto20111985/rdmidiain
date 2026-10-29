"""
RD Mídia Indoor — Criativos: código RD imutável, validação técnica, versões (W-CAMP).

- Código ``RD-XXXXXX`` (``app.core.ids.new_creative_code``; alfabeto sem L,I,X,W,O,K,0,1).
  UNIQUE no banco + retry em colisão (pré-checagem e, numa corrida, IntegrityError do
  UNIQUE dentro de SAVEPOINT). O código NUNCA muda (trigger de imutabilidade).
- Duração declarada ∈ {30, 60, 120} s. Vídeo: duração real exige
  ``declarada − 1,0 s ≤ real ≤ declarada + 0,04 s`` (ADR_006 #9): 31,2 s e 30,5 s
  REJEITADOS para 30 s; nunca cortar. Imagem: duração configurável dentro de 30/60/120.
- Orientação herdada da mídia (letterbox na tela de outra orientação; nunca esticar).
- Nova versão relevante (mídia, duração, texto, classificação) = NOVO criativo com
  NOVO código (``supersedes_id``/``root_id``); a anterior continua até a nova ser aprovada.
"""
from __future__ import annotations

import uuid
from typing import Callable, Iterable, Optional

from sqlalchemy import delete, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.audit import audit
from app.core.errors import Conflict, DomainError, NotFound  # noqa: F401
from app.core.ids import IdGenerationError, new_creative_code
from app.core.timeutils import utcnow
from app.models.campaigns import (
    ALLOWED_DURATIONS_S,
    Creative,
    CreativeKind,
    CreativeSegment,
    CreativeStatus,
    TechnicalStatus,
)

VIDEO_TOLERANCE_BELOW_MS = 1000
VIDEO_TOLERANCE_ABOVE_MS = 40
MAX_CODE_ATTEMPTS = 10


def duration_problem(media_kind: str, declared_s: int, media_duration_ms: Optional[int]) -> Optional[dict]:
    """``None`` se a duração é aceita; senão ``{"code": ..., "message": ..., "details": ...}``."""
    if declared_s not in ALLOWED_DURATIONS_S:
        return {"code": "DURATION_NOT_ALLOWED", "message": "Duração permitida: 30, 60 ou 120 segundos.",
                "details": {"duration_seconds": declared_s, "allowed": list(ALLOWED_DURATIONS_S)}}
    if (media_kind or "").upper() == "VIDEO":
        if media_duration_ms is None:
            return {"code": "MEDIA_DURATION_UNKNOWN", "message": "Duração do vídeo desconhecida.", "details": {}}
        lo = declared_s * 1000 - VIDEO_TOLERANCE_BELOW_MS
        hi = declared_s * 1000 + VIDEO_TOLERANCE_ABOVE_MS
        if not (lo <= media_duration_ms <= hi):
            return {
                "code": "DURATION_MISMATCH",
                "message": (f"O vídeo tem {media_duration_ms / 1000:.2f} s e não cabe no slot de {declared_s} s "
                            f"(aceito de {lo / 1000:.2f} s a {hi / 1000:.2f} s; nunca cortamos a mídia)."),
                "details": {"media_duration_ms": media_duration_ms, "declared_seconds": declared_s,
                            "min_ms": lo, "max_ms": hi},
            }
    return None


async def _load_media(db: AsyncSession, ctx, media_id: uuid.UUID, *, org_id: uuid.UUID, for_update: bool = False):
    """Mídia utilizável (escopo + READY). Usa o serviço do W-MEDIA quando disponível."""
    try:
        from app.modules.media import service as media_service  # type: ignore

        ensure = getattr(media_service, "ensure_media_usable", None)
    except Exception:  # pragma: no cover
        ensure = None
    if ensure is not None and ctx is not None and getattr(ctx, "kind", "user") == "user":
        m = await ensure(db, ctx, media_id, perm="media.view", for_update=for_update)
    else:
        from app.models.media import MediaFile

        q = select(MediaFile).where(MediaFile.id == media_id)
        if for_update:
            q = q.with_for_update(read=True)
        m = (await db.execute(q)).scalar_one_or_none()
        if m is None or m.deleted_at is not None:
            raise NotFound()
        if ctx is not None and getattr(ctx, "kind", "user") == "user":
            ctx.ensure("media.view", org_id=m.org_id)
        if m.status != "READY":
            raise Conflict("MEDIA_NOT_READY", "A mídia ainda não está pronta (processamento em andamento ou recusada).",
                           details={"media_status": m.status})
    if m.org_id != org_id:
        # criativo só usa mídia da própria organização
        raise NotFound()
    return m


def technical_check(creative: Creative, media) -> None:
    """Atualiza ``technical_status``/orientação/duração real do criativo a partir da mídia."""
    if media is None or media.status != "READY" or media.deleted_at is not None:
        creative.technical_status = TechnicalStatus.PENDENTE.value
        creative.technical_error = "MEDIA_NOT_READY"
        return
    prob = duration_problem(media.kind, creative.duration_seconds, media.duration_ms)
    creative.media_duration_ms = media.duration_ms
    if media.orientation in ("HORIZONTAL", "VERTICAL"):
        creative.orientation = media.orientation
    elif media.width and media.height:
        creative.orientation = "HORIZONTAL" if media.width >= media.height else "VERTICAL"
    if prob:
        creative.technical_status = TechnicalStatus.INVALIDO.value
        creative.technical_error = prob["code"]
    else:
        creative.technical_status = TechnicalStatus.VALIDO.value
        creative.technical_error = None


async def code_exists(db: AsyncSession, code: str) -> bool:
    return (await db.execute(select(Creative.id).where(Creative.code == code))).first() is not None


async def insert_with_unique_code(
    db: AsyncSession,
    creative: Creative,
    *,
    gen: Callable[[], str] = new_creative_code,
    precheck: bool = True,
    max_attempts: int = MAX_CODE_ATTEMPTS,
) -> Creative:
    """Insere o criativo com código único; colisão (pré-checagem ou UNIQUE) ⇒ novo código."""
    for _ in range(max_attempts):
        code = gen()
        if precheck and await code_exists(db, code):
            continue
        creative.code = code
        try:
            async with db.begin_nested():
                db.add(creative)
                await db.flush([creative])
            return creative
        except IntegrityError as exc:
            constraint = getattr(getattr(exc.orig, "__cause__", None), "constraint_name", None) or ""
            if "code" not in constraint and "uq_creatives_code" not in str(exc.orig):
                raise
            # SAVEPOINT desfeito; objeto volta a ser transitório — tenta outro código
            if creative in db:
                db.expunge(creative)
            continue
    raise IdGenerationError(f"Não foi possível gerar código único após {max_attempts} tentativas")


async def set_segments(db: AsyncSession, creative_id: uuid.UUID, segment_ids: Iterable[uuid.UUID]) -> list[uuid.UUID]:
    from app.models.taxonomy import Segment

    ids = sorted({uuid.UUID(str(s)) for s in segment_ids}, key=str)
    if ids:
        found = set((await db.execute(select(Segment.id).where(Segment.id.in_(ids), Segment.is_active.is_(True))
                                      )).scalars())
        missing = [str(i) for i in ids if i not in found]
        if missing:
            raise DomainError("SEGMENT_NOT_FOUND", "Segmento inexistente ou inativo.", status=400,
                              details={"segment_ids": missing})
    await db.execute(delete(CreativeSegment).where(CreativeSegment.creative_id == creative_id))
    for sid in ids:
        db.add(CreativeSegment(creative_id=creative_id, segment_id=sid))
    return ids


async def validate_classification(db: AsyncSession, product_id, brand_id) -> None:
    from app.models.taxonomy import Brand, Product

    if product_id is not None and await db.get(Product, product_id) is None:
        raise DomainError("PRODUCT_NOT_FOUND", "Produto inexistente.", status=400)
    if brand_id is not None and await db.get(Brand, brand_id) is None:
        raise DomainError("BRAND_NOT_FOUND", "Marca inexistente.", status=400)


async def create_creative(
    db: AsyncSession,
    ctx,
    *,
    org_id: uuid.UUID,
    name: str,
    media_file_id: uuid.UUID,
    duration_seconds: int,
    kind: str = CreativeKind.EXTERNAL.value,
    ad_text: Optional[str] = None,
    product_id: Optional[uuid.UUID] = None,
    brand_id: Optional[uuid.UUID] = None,
    segment_ids: Iterable[uuid.UUID] = (),
    supersedes: Optional[Creative] = None,
    gen: Callable[[], str] = new_creative_code,
) -> Creative:
    if kind not in CreativeKind.__members__:
        raise DomainError("CREATIVE_KIND_INVALID", "Tipo de criativo inválido.", status=400)
    if kind != CreativeKind.EXTERNAL.value and not ctx.can("creatives.manage", global_only=True):
        # conteúdo interno/institucional/fallback é controlado pela RD (P001 §34)
        raise DomainError("CREATIVE_KIND_FORBIDDEN", "Somente a equipe RD cria conteúdo interno/institucional/fallback.",
                          status=403)
    prob = duration_problem("IMAGE", duration_seconds, None)  # só valida o conjunto permitido
    if prob:
        raise DomainError(prob["code"], prob["message"], status=400, details=prob["details"])
    await validate_classification(db, product_id, brand_id)
    media = await _load_media(db, ctx, media_file_id, org_id=org_id, for_update=True)
    prob = duration_problem(media.kind, duration_seconds, media.duration_ms)
    if prob:
        raise DomainError(prob["code"], prob["message"], status=400, details=prob["details"])
    c = Creative(
        id=uuid.uuid4(), org_id=org_id, name=name.strip(), kind=kind, media_file_id=media.id,
        duration_seconds=duration_seconds, ad_text=ad_text, product_id=product_id, brand_id=brand_id,
        status=CreativeStatus.RASCUNHO.value, created_by=getattr(ctx, "user_id", None),
        version=(supersedes.version + 1) if supersedes else 1,
        supersedes_id=supersedes.id if supersedes else None,
        root_id=(supersedes.root_id or supersedes.id) if supersedes else None,
    )
    technical_check(c, media)
    await insert_with_unique_code(db, c, gen=gen)
    await set_segments(db, c.id, segment_ids)
    await audit(db, ctx, "creative.created" if supersedes is None else "creative.version_created", "creative", c.id,
                after={"code": c.code, "name": c.name, "kind": c.kind, "duration_seconds": c.duration_seconds,
                       "media_file_id": str(c.media_file_id), "version": c.version,
                       "supersedes": supersedes.code if supersedes else None,
                       "segment_ids": [str(s) for s in segment_ids]},
                org_id=org_id)
    return c


async def get_creative(db: AsyncSession, ctx, creative_id: uuid.UUID, *, perm: str = "creatives.view",
                       for_update: bool = False) -> Creative:
    q = select(Creative).where(Creative.id == creative_id)
    if for_update:
        q = q.with_for_update().execution_options(populate_existing=True)
    c = (await db.execute(q)).scalar_one_or_none()
    if c is None:
        raise NotFound()
    if ctx is not None and getattr(ctx, "kind", "user") == "user":
        ctx.ensure(perm, org_id=c.org_id)
    return c


async def segment_ids_of(db: AsyncSession, creative_id: uuid.UUID) -> list[uuid.UUID]:
    return list((await db.execute(select(CreativeSegment.segment_id).where(CreativeSegment.creative_id == creative_id)
                                  .order_by(CreativeSegment.segment_id))).scalars())


async def create_version(db: AsyncSession, ctx, old: Creative, *, name: Optional[str] = None,
                         media_file_id: Optional[uuid.UUID] = None, duration_seconds: Optional[int] = None,
                         ad_text: Optional[str] = ..., product_id=..., brand_id=..., segment_ids=None) -> Creative:
    if old.status in (CreativeStatus.SUBSTITUIDO.value, CreativeStatus.ARQUIVADO.value):
        raise Conflict("CREATIVE_NOT_CURRENT", "Este criativo já foi substituído/arquivado; versione a versão atual.")
    newer = (await db.execute(select(Creative.id).where(Creative.supersedes_id == old.id,
                                                       Creative.status.notin_(["REJEITADO", "ARQUIVADO"])))).first()
    if newer is not None:
        raise Conflict("CREATIVE_VERSION_PENDING", "Já existe uma nova versão deste criativo em andamento.")
    segs = segment_ids if segment_ids is not None else await segment_ids_of(db, old.id)
    return await create_creative(
        db, ctx, org_id=old.org_id, name=name or old.name, media_file_id=media_file_id or old.media_file_id,
        duration_seconds=duration_seconds or old.duration_seconds, kind=old.kind,
        ad_text=old.ad_text if ad_text is ... else ad_text,
        product_id=old.product_id if product_id is ... else product_id,
        brand_id=old.brand_id if brand_id is ... else brand_id, segment_ids=segs, supersedes=old,
    )


async def refresh_technical(db: AsyncSession, creatives: Iterable[Creative]) -> None:
    from app.models.media import MediaFile

    cl = list(creatives)
    if not cl:
        return
    medias = {m.id: m for m in (await db.execute(select(MediaFile).where(
        MediaFile.id.in_([c.media_file_id for c in cl])))).scalars()}
    for c in cl:
        technical_check(c, medias.get(c.media_file_id))


def creative_out(c: Creative, segment_ids: Optional[list] = None) -> dict:
    return {
        "id": str(c.id), "code": c.code, "org_id": str(c.org_id), "name": c.name, "kind": c.kind,
        "media_file_id": str(c.media_file_id), "duration_seconds": c.duration_seconds, "orientation": c.orientation,
        "media_duration_ms": c.media_duration_ms, "ad_text": c.ad_text, "status": c.status,
        "technical_status": c.technical_status, "technical_error": c.technical_error,
        "product_id": str(c.product_id) if c.product_id else None,
        "brand_id": str(c.brand_id) if c.brand_id else None,
        "segment_ids": [str(s) for s in (segment_ids or [])],
        "version": c.version, "supersedes_id": str(c.supersedes_id) if c.supersedes_id else None,
        "root_id": str(c.root_id) if c.root_id else None, "rejection_reason": c.rejection_reason,
        "created_at": c.created_at, "updated_at": c.updated_at,
    }


# ─── Revisão de conteúdo (máquina de estados ÚNICA do criativo) ──────────────
# Usada pela aprovação/rejeição de campanha (criativos EXTERNAL) e pela revisão avulsa de
# conteúdo da RD (INTERNAL/INSTITUTIONAL/FALLBACK — sem campanha). Rejeitado não volta a ser
# revisado: a correção é uma NOVA VERSÃO (novo código RD).
REVIEWABLE_STATUSES = frozenset({CreativeStatus.RASCUNHO.value, CreativeStatus.EM_ANALISE.value})
REVIEW_TRANSITIONS: dict[tuple[str, bool], str] = {
    (s, approved): (CreativeStatus.APROVADO.value if approved else CreativeStatus.REJEITADO.value)
    for s in REVIEWABLE_STATUSES for approved in (True, False)
}
REVIEW_REASON_MIN = 10


def can_review(status: str) -> bool:
    return status in REVIEWABLE_STATUSES


def mark_reviewed(c: Creative, *, approved: bool, ctx, reason: Optional[str] = None) -> None:
    to = REVIEW_TRANSITIONS.get((c.status, approved))
    if to is None:
        raise Conflict("CREATIVE_NOT_REVIEWABLE", "Criativo não está em estado revisável.",
                       details={"status": c.status, "reviewable": sorted(REVIEWABLE_STATUSES)})
    c.status = to
    c.reviewed_by = getattr(ctx, "user_id", None)
    c.reviewed_at = utcnow()
    c.rejection_reason = None if approved else reason


async def review_creative(db: AsyncSession, ctx, creative_id: uuid.UUID, *, approve: bool,
                          reason: Optional[str] = None) -> Creative:
    """Revisão de conteúdo da RD para criativos SEM campanha (INTERNAL/INSTITUTIONAL/FALLBACK).

    Criativos EXTERNAL são revisados na aprovação/rejeição da campanha (mesma máquina de estados).
    Aprovar exige mídia READY e duração válida (revalida agora); rejeitar exige motivo (≥ 10).
    O código RD (sem L, I, X, W, O, K, 0, 1) nunca muda (trigger de imutabilidade)."""
    c = await get_creative(db, ctx, creative_id, perm="creatives.view", for_update=True)
    if c.kind == CreativeKind.EXTERNAL.value:
        raise Conflict("CREATIVE_REVIEW_VIA_CAMPAIGN",
                       "Criativos de anunciante são revisados na aprovação da campanha.",
                       details={"kind": c.kind})
    reason = (reason or "").strip() or None
    if not can_review(c.status):
        raise Conflict("CREATIVE_NOT_REVIEWABLE", "Criativo não está em estado revisável.",
                       details={"status": c.status, "reviewable": sorted(REVIEWABLE_STATUSES)})
    if not approve and (reason is None or len(reason) < REVIEW_REASON_MIN):
        raise DomainError("REJECTION_REASON_REQUIRED",
                          f"Informe o motivo da rejeição (mín. {REVIEW_REASON_MIN} caracteres).", status=400)
    if approve:
        await refresh_technical(db, [c])
        if c.technical_status != TechnicalStatus.VALIDO.value:
            raise Conflict("CREATIVE_NOT_READY", "A mídia do criativo precisa estar pronta (READY) e com duração válida.",
                           details={"technical_status": c.technical_status, "problem": c.technical_error})
    before = {"status": c.status}
    mark_reviewed(c, approved=approve, ctx=ctx, reason=reason)
    await audit(db, ctx, "creative.approved" if approve else "creative.rejected", "creative", c.id,
                before=before, after={"status": c.status, "code": c.code, "kind": c.kind}, reason=reason,
                org_id=c.org_id)
    return c
