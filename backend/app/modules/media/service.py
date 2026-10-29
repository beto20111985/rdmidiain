"""
RD Mídia Indoor — Serviço de mídia (interface ESTÁVEL para os outros domínios).

Funções públicas (nomes congelados — AÇÃO 002 / W-MEDIA):

- ``intake_stream(db, ctx, org_id, filename, declared_mime, chunks, *, origin, name, created_by)``
  → ``IntakeResult(media, created)`` — núcleo do upload: extensão, magic bytes nos
  primeiros bytes, limite por tipo DURANTE o streaming, SHA-256 incremental,
  idempotência (org_id, sha256), registro UPLOADED + auditoria + enfileiramento do
  processamento no ``after_commit`` da transação do chamador.
- ``ingest_generated_file(db, ctx, org_id, filename, content_type, data, purpose, *, name=None)``
  → ``MediaFile`` — arquivo gerado no servidor (arte aprovada do designer, PNG/JPG do
  editor de templates). MESMA validação e pipeline do upload. Autorização é do chamador.
- ``open_media_range(db, media_id, range_header=None, *, if_range=None, variant="main",
  head_only=False, cache_control=...)`` → ``MediaRangeResult`` (200/206/416 + headers +
  corpo em streaming; ``.to_response()``). NÃO autoriza — o chamador (painel com escopo;
  player via manifesto, W-PLAY) autoriza antes. Só mídia READY e não purgada.
- ``get_media_descriptors(db, media_ids)`` → ``{id: MediaDescriptor}`` (somente READY) —
  para o manifesto RDPP (W-INV): sha256, size, mime, width, height, duration_ms.
- ``ensure_media_usable(db, ctx, media_id, *, perm="media.view", for_update=False)`` →
  ``MediaFile`` READY dentro do escopo (404 fora; 409 ``MEDIA_NOT_READY``).
  ``for_update=True`` trava a linha (FOR SHARE) contra exclusão concorrente — use ao
  criar criativo (W-CAMP).
- ``register_media_referencer(name, fn)`` — ``fn(db, media_ids) -> {media_id: n}``.
  Exclusão e GC só prosseguem com ZERO referências (FK de qualquer tabela para
  ``media_files.id`` já conta automaticamente) e falham FECHADO se um referenciador
  lançar exceção.
- ``soft_delete_media(db, ctx, media, reason=None)``; ``count_references(db, ids)``;
  ``gc_deleted_media()``; ``requeue_stale()`` (recupera lease vencido/worker caído — BUG-QA-01);
  ``media_out(m)`` (serialização da API).
"""
from __future__ import annotations

import inspect
import re
import uuid
from dataclasses import dataclass
from datetime import timedelta
from typing import Any, AsyncIterable, Awaitable, Callable, Iterable, Optional, Union

import structlog
from fastapi.responses import Response, StreamingResponse
from sqlalchemy import event, func, or_, select, true
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.audit import audit, audit_independent
from app.core.authz import AuthContext
from app.core.config import settings as core_settings
from app.core.errors import DomainError, NotFound
from app.core.storage import (
    InvalidRange,
    ObjectNotFound,
    ObjectTooLarge,
    StorageProvider,
    aiter_chunks,
    build_key,
    get_storage,
)
from app.core.timeutils import utcnow
from app.models.media import MediaFile, MediaFileStatus
from app.modules.media.policy import (
    max_bytes_for_kind,
    max_upload_bytes,
    media_settings,
)
from app.modules.media.sniff import (
    SNIFF_BYTES,
    SniffRejected,
    check_extension,
    extension_matches,
    sanitize_filename,
    sniff,
)

log = structlog.get_logger("rd.media.service")

S = MediaFileStatus
_PURPOSE_RE = re.compile(r"^[A-Z][A-Z0-9_]{1,29}$")


# ─── Enfileiramento do processamento (sempre DEPOIS do commit) ───────────────

Enqueuer = Callable[[str], None]
_enqueuer: Optional[Enqueuer] = None
TEST_ENQUEUED: list[str] = []  # ENVIRONMENT=test: nunca publica no broker real


def set_enqueuer(fn: Optional[Enqueuer]) -> None:
    """Troca o transporte de enfileiramento (testes). ``None`` volta ao padrão (Celery)."""
    global _enqueuer
    _enqueuer = fn


def _default_enqueue(media_id: str, countdown: Optional[int] = None) -> None:
    if core_settings.is_test:
        TEST_ENQUEUED.append(media_id)
        return
    from app.workers.tasks.media import process_media

    process_media.apply_async(args=[media_id], queue="media", countdown=countdown or None)


def _accepts_countdown(fn: Callable) -> bool:
    try:
        params = inspect.signature(fn).parameters.values()
    except (TypeError, ValueError):
        return False
    return any(p.name == "countdown" or p.kind is inspect.Parameter.VAR_KEYWORD for p in params)


def enqueue_processing(media_id: Any, countdown: Optional[int] = None) -> bool:
    """Enfileira o processamento. ``countdown`` (s) = backoff; enfileiradores de teste com
    um único argumento continuam aceitos (o backoff é então ignorado)."""
    fn = _enqueuer or _default_enqueue
    try:
        if countdown is not None and _accepts_countdown(fn):
            fn(str(media_id), countdown=int(countdown))
        else:
            fn(str(media_id))
        return True
    except Exception as exc:  # broker fora: a varredura (requeue_stale) recupera
        log.error("media_enqueue_failed", media_id=str(media_id), error_type=type(exc).__name__)
        return False


_INFO_ENQ = "rd_media_enqueue"
_INFO_CLEAN = "rd_media_cleanup_keys"
_INFO_HOOKED = "rd_media_hooked"


def _after_commit(session) -> None:
    ids = session.info.pop(_INFO_ENQ, [])
    session.info.pop(_INFO_CLEAN, None)
    for mid in ids:
        enqueue_processing(mid)


def _after_rollback(session) -> None:
    session.info.pop(_INFO_ENQ, None)
    keys = session.info.pop(_INFO_CLEAN, [])
    if keys:
        st = get_storage()
        for k in keys:
            try:
                st.delete_blocking(k)
            except Exception as exc:
                log.warning("media_orphan_cleanup_failed", error_type=type(exc).__name__)


def enqueue_after_commit(db: AsyncSession, media_id: Any, *, cleanup_on_rollback: Iterable[str] = ()) -> None:
    """Enfileira ``media_id`` quando a transação de ``db`` fizer COMMIT; em rollback, apaga
    os objetos recém-gravados (``cleanup_on_rollback``) para não deixar órfãos."""
    sync = db.sync_session
    if not sync.info.get(_INFO_HOOKED):
        event.listen(sync, "after_commit", _after_commit)
        event.listen(sync, "after_rollback", _after_rollback)
        sync.info[_INFO_HOOKED] = True
    sync.info.setdefault(_INFO_ENQ, []).append(str(media_id))
    sync.info.setdefault(_INFO_CLEAN, []).extend(cleanup_on_rollback)


# ─── Intake (upload / ingestão) ──────────────────────────────────────────────

class MediaTooLarge(Exception):
    def __init__(self, limit: int, kind: Optional[str]) -> None:
        super().__init__("too large")
        self.limit = limit
        self.kind = kind


@dataclass
class IntakeResult:
    media: MediaFile
    created: bool


def too_large_error(limit: int, kind: Optional[str]) -> DomainError:
    label = {"IMAGE": "imagem", "VIDEO": "vídeo"}.get(kind or "", "arquivo")
    return DomainError(
        "MEDIA_TOO_LARGE",
        f"Arquivo muito grande: o limite para {label} é {limit // (1024 * 1024)} MB.",
        status=413,
        details={"limit_bytes": limit, "kind": kind},
    )


def _rejected_error(e: SniffRejected) -> DomainError:
    status = 400 if e.code in ("MEDIA_EMPTY",) else 415
    return DomainError(e.code, e.message, status=status, details={"detected": e.detected} if e.detected else {})


async def _guarded(chunks: AsyncIterable[bytes], holder: dict) -> AsyncIterable[bytes]:
    """Primeiros bytes → magic bytes → limite do TIPO aplicado durante o resto do stream."""
    it = chunks.__aiter__()
    head = bytearray()
    while len(head) < SNIFF_BYTES:
        try:
            c = await it.__anext__()
        except StopAsyncIteration:
            break
        head += c
        if len(head) > max_upload_bytes():
            raise MediaTooLarge(max_upload_bytes(), None)
    sn = sniff(bytes(head))  # SniffRejected → aborta antes de aceitar o resto
    holder["sniffed"] = sn
    limit = max_bytes_for_kind(sn.kind)
    total = len(head)
    if total > limit:
        raise MediaTooLarge(limit, sn.kind)
    yield bytes(head)
    async for c in it:
        total += len(c)
        if total > limit:
            raise MediaTooLarge(limit, sn.kind)
        yield c


def check_free_space() -> None:
    from app.modules.media.pipeline import workspace_free_bytes

    free = workspace_free_bytes()
    if free is not None and free < media_settings().MIN_FREE_BYTES + max_upload_bytes():
        log.error("media_storage_low_space", free_bytes=free)
        raise DomainError("MEDIA_STORAGE_FULL", "Armazenamento temporariamente sem espaço. Tente mais tarde.",
                          status=507)


async def intake_stream(
    db: AsyncSession,
    ctx: Optional[AuthContext],
    org_id: uuid.UUID,
    filename: Optional[str],
    declared_mime: Optional[str],
    chunks: Union[bytes, AsyncIterable[bytes], Iterable[bytes]],
    *,
    origin: str = "UPLOAD",
    name: Optional[str] = None,
    created_by: Optional[uuid.UUID] = None,
) -> IntakeResult:
    """Recebe um arquivo em streaming (nunca inteiro em RAM) e cria a mídia UPLOADED.

    Erros (``DomainError``): 415 tipo/extensão não aceitos (inclui WebP), 413 acima do
    limite (abortado durante o streaming, temporário apagado), 400 vazio.
    """
    if not _PURPOSE_RE.match(origin or ""):
        raise ValueError("origin inválido")
    original = sanitize_filename(filename)
    try:
        ext = check_extension(original)
    except SniffRejected as e:
        raise _rejected_error(e) from None

    storage: StorageProvider = get_storage()
    media_id = uuid.uuid4()
    key = build_key("media", org_id, media_id, "source")
    holder: dict = {}
    source = chunks if hasattr(chunks, "__aiter__") else aiter_chunks(chunks)  # type: ignore[arg-type]
    try:
        obj = await storage.put_stream(key, _guarded(source, holder), None, max_bytes=max_upload_bytes())
    except SniffRejected as e:
        raise _rejected_error(e) from None
    except MediaTooLarge as e:
        raise too_large_error(e.limit, e.kind) from None
    except ObjectTooLarge as e:
        raise too_large_error(e.limit, None) from None
    sn = holder["sniffed"]

    # idempotência: mesmo conteúdo na mesma organização → mesma mídia
    existing = await _find_by_sha(db, org_id, obj.sha256)
    if existing is not None:
        await storage.delete(key)
        return IntakeResult(existing, False)

    display_name = sanitize_filename(name) if name else original
    m = MediaFile(
        id=media_id,
        org_id=org_id,
        created_by=created_by,
        origin=origin,
        name=display_name[:255],
        original_filename=original[:255],
        original_extension=ext,
        declared_mime=(declared_mime or None) and declared_mime[:100],
        extension_mismatch=not extension_matches(ext, sn.format),
        kind=sn.kind,
        source_format=sn.format,
        source_mime=sn.mime,
        source_size_bytes=obj.size,
        source_sha256=obj.sha256,
        source_storage_key=key,
        status=S.UPLOADED.value,
        progress_pct=0,
        attempts=0,
        animated=False,
    )
    try:
        async with db.begin_nested():
            db.add(m)
            await db.flush()
    except IntegrityError:
        # corrida: outro upload do mesmo arquivo venceu
        await storage.delete(key)
        existing = await _find_by_sha(db, org_id, obj.sha256)
        if existing is None:
            raise
        return IntakeResult(existing, False)
    await audit(
        db, ctx, "media.uploaded", "media", m.id,
        origin=_audit_origin(ctx),
        after={"name": m.name, "kind": m.kind, "format": m.source_format, "size_bytes": m.source_size_bytes,
               "sha256": m.source_sha256, "origin": m.origin, "extension_mismatch": m.extension_mismatch},
        org_id=org_id,
    )
    enqueue_after_commit(db, m.id, cleanup_on_rollback=[key])
    return IntakeResult(m, True)


_AUDIT_ORIGINS = {"api", "worker", "player", "system", "cli"}


def _audit_origin(ctx: Optional[AuthContext]) -> Optional[str]:
    """``AuthContext.system(origin=…)`` com origem fora do CHECK da auditoria → ``system``."""
    if ctx is None or getattr(ctx, "kind", "user") != "system":
        return None
    o = getattr(ctx, "origin", "system")
    return o if o in _AUDIT_ORIGINS else "system"


async def _find_by_sha(db: AsyncSession, org_id: uuid.UUID, sha: str) -> Optional[MediaFile]:
    return (
        await db.execute(
            select(MediaFile).where(
                MediaFile.org_id == org_id, MediaFile.source_sha256 == sha, MediaFile.deleted_at.is_(None)
            )
        )
    ).scalar_one_or_none()


async def ingest_generated_file(
    db: AsyncSession,
    ctx: Optional[AuthContext],
    org_id: uuid.UUID,
    filename: str,
    content_type: Optional[str],
    data: Union[bytes, AsyncIterable[bytes], Iterable[bytes]],
    purpose: str,
    *,
    name: Optional[str] = None,
) -> MediaFile:
    """Ingestão de arquivo gerado no servidor — mesma validação e pipeline do upload.

    ``purpose``: código ``^[A-Z][A-Z0-9_]{1,29}$`` (ex.: ``DESIGN_ART``, ``TEMPLATE_RENDER``)
    gravado em ``MediaFile.origin``. Retorna a mídia (existente, se o mesmo conteúdo já
    estiver na organização). Erros de validação: ``DomainError`` (415/413/400).
    """
    if not _PURPOSE_RE.match(purpose or "") or purpose == "UPLOAD":
        raise ValueError("purpose inválido")
    created_by = getattr(ctx, "user_id", None) if ctx is not None and getattr(ctx, "kind", "") == "user" else None
    res = await intake_stream(db, ctx, org_id, filename, content_type, data, origin=purpose, name=name,
                              created_by=created_by)
    return res.media


async def audit_upload_rejection(ctx: AuthContext, org_id: Optional[uuid.UUID], err: DomainError,
                                 filename: Optional[str]) -> None:
    try:
        await audit_independent(
            ctx, "media.upload_rejected", "media", None,
            after={"code": err.code, "filename": sanitize_filename(filename) if filename else None,
                   "details": err.details},
            org_id=org_id,
        )
    except Exception as exc:
        log.warning("media_upload_rejection_audit_failed", error_type=type(exc).__name__)


# ─── Organização alvo ────────────────────────────────────────────────────────

def parse_context_org(header: Optional[str]) -> Optional[uuid.UUID]:
    """``X-RD-Context: adv:<org>`` / ``est:<org>`` → org (apenas candidato; autorização é recalculada)."""
    if not header:
        return None
    kind, _, val = header.strip().partition(":")
    if kind not in ("adv", "est") or not val:
        return None
    try:
        return uuid.UUID(val)
    except ValueError:
        return None


async def resolve_target_org(db: AsyncSession, ctx: AuthContext, perm: str, explicit: Optional[uuid.UUID]) -> uuid.UUID:
    from app.models.organizations import Organization

    if explicit is not None:
        if not ctx.can(perm, org_id=explicit):
            raise NotFound()
        org = await db.get(Organization, explicit)
        if org is None or getattr(org, "deleted_at", None) is not None:
            raise NotFound()
        return explicit
    orgs = ctx.org_ids(perm)
    if orgs is not None and len(orgs) == 1:
        return next(iter(orgs))
    raise DomainError(
        "MEDIA_ORG_REQUIRED",
        "Informe a organização dona da mídia (campo org_id ou contexto ativo).",
        status=400,
    )


# ─── Referências e exclusão segura ───────────────────────────────────────────

MediaReferencer = Callable[[AsyncSession, list], Awaitable[dict]]
_referencers: dict[str, MediaReferencer] = {}


def register_media_referencer(name: str, fn: MediaReferencer) -> None:
    """Registra (ou substitui) um referenciador: ``fn(db, media_ids) -> {media_id: n}``."""
    _referencers[name] = fn


def unregister_media_referencer(name: str) -> None:
    _referencers.pop(name, None)


class ReferenceCheckFailed(Exception):
    def __init__(self, name: str) -> None:
        super().__init__(name)
        self.name = name


def _fk_columns() -> list:
    from app.core.database import Base

    cols = []
    for table in Base.metadata.tables.values():
        if table.name == MediaFile.__tablename__:
            continue
        for fk in table.foreign_keys:
            if fk.column.table.name == MediaFile.__tablename__ and fk.column.name == "id":
                cols.append((table.name, fk.parent))
    return cols


async def count_references(db: AsyncSession, media_ids: list) -> dict:
    """``{media_id: {fonte: n}}`` (somente fontes com n > 0). Lança ``ReferenceCheckFailed``."""
    ids = [uuid.UUID(str(i)) for i in media_ids]
    out: dict = {i: {} for i in ids}
    if not ids:
        return out
    for tname, col in _fk_columns():
        rows = (await db.execute(select(col, func.count()).where(col.in_(ids)).group_by(col))).all()
        for mid, n in rows:
            if n:
                out[mid][f"fk:{tname}"] = int(n)
    for name, fn in list(_referencers.items()):
        try:
            async with db.begin_nested():
                counts = await fn(db, list(ids))
        except Exception as exc:
            log.error("media_referencer_failed", referencer=name, error_type=type(exc).__name__)
            raise ReferenceCheckFailed(name) from None
        for mid, n in (counts or {}).items():
            key = uuid.UUID(str(mid))
            if key in out and n:
                out[key][name] = int(n)
    return out


async def soft_delete_media(db: AsyncSession, ctx: AuthContext, media: MediaFile, reason: Optional[str] = None) -> None:
    """Exclusão segura: trava a linha, confere referências e marca ``deleted_at``.
    O arquivo físico só é removido pelo GC (que reconsulta as referências)."""
    m = (await db.execute(select(MediaFile).where(MediaFile.id == media.id).with_for_update())).scalar_one()
    if m.deleted_at is not None:
        raise NotFound()
    try:
        refs = (await count_references(db, [m.id]))[m.id]
    except ReferenceCheckFailed:
        raise DomainError("MEDIA_REFERENCE_CHECK_FAILED",
                          "Não foi possível confirmar que a mídia não está em uso. Tente novamente.", status=409)
    if refs:
        raise DomainError("MEDIA_IN_USE", "Mídia em uso (criativo, campanha, programação ou player); não pode ser excluída.",
                          status=409, details={"references": refs_public(refs)})
    m.deleted_at = utcnow()
    m.deleted_by = getattr(ctx, "user_id", None)
    m.lease_until = None
    await audit(db, ctx, "media.deleted", "media", m.id, before={"status": m.status}, after={"deleted": True},
                reason=reason, org_id=m.org_id)


def refs_public(refs: dict) -> dict:
    """Contagens por tipo (sem identificar registros de terceiros)."""
    out: dict = {}
    for k, n in refs.items():
        label = k.split(":", 1)[1] if k.startswith("fk:") else k
        out[label] = out.get(label, 0) + int(n)
    return out


async def gc_deleted_media(*, batch: int = 200) -> dict:
    """Remove do storage os arquivos de mídias excluídas há mais de ``MEDIA_GC_GRACE_HOURS``
    e sem nenhuma referência (reconsultada aqui). Mantém o registro (histórico)."""
    from app.core.database import worker_session

    ms = media_settings()
    cutoff = utcnow() - timedelta(hours=ms.GC_GRACE_HOURS)
    storage = get_storage()
    purged = skipped = 0
    async with worker_session() as db:
        rows = (
            await db.execute(
                select(MediaFile)
                .where(MediaFile.deleted_at.is_not(None), MediaFile.purged_at.is_(None), MediaFile.deleted_at < cutoff)
                .order_by(MediaFile.deleted_at)
                .limit(batch)
                .with_for_update(skip_locked=True)
            )
        ).scalars().all()
        if not rows:
            return {"purged": 0, "skipped": 0}
        try:
            refs = await count_references(db, [m.id for m in rows])
        except ReferenceCheckFailed as e:
            log.error("media_gc_reference_check_failed", referencer=e.name)
            return {"purged": 0, "skipped": len(rows), "error": "reference_check_failed"}
        sys_ctx = AuthContext.system(origin="worker")
        for m in rows:
            if refs.get(m.id):
                skipped += 1
                continue
            for k in (m.source_storage_key, m.storage_key, m.preview_storage_key):
                if k:
                    await storage.delete(k)
            m.purged_at = utcnow()
            m.source_storage_key = None
            await audit(db, sys_ctx, "media.purged", "media", m.id, org_id=m.org_id)
            purged += 1
    return {"purged": purged, "skipped": skipped}


async def requeue_stale(*, limit: int = 100) -> dict:
    """Recuperação de processamento parado — job ``media_maintenance`` (beat, 60 s). Idempotente.

    1. **Lease vencido** (VALIDATING/PROCESSING com ``lease_until`` < agora: o worker caiu ou
       reiniciou no meio — o heartbeat renova a cada ≤ 30 s enquanto vivo): volta a UPLOADED
       (``status_code=MEDIA_WORKER_LOST``) e é reenfileirada UMA vez com backoff
       (30 s, 60 s…); se era a última tentativa → FAILED ``MEDIA_WORKER_LOST`` + notificação.
       O workspace da execução morta é removido.
    2. **UPLOADED parada** há mais de ``stale_uploaded_s()`` (broker fora no upload / mensagem
       perdida com o worker): reenfileira e marca ``updated_at`` (no máximo 1× por janela).
    3. **FAILED retentável** sem retry pendente há mais de 10× ``RETRY_BASE_S``: idem.

    Linhas travadas por outra transação (execução finalizando, heartbeat) são puladas
    (``SKIP LOCKED``). Mensagens duplicadas — ex.: a original reentregue pelo broker após o
    ``visibility_timeout`` — são inofensivas: o claim devolve ``noop``/``busy``.
    """
    from app.core.database import worker_session
    from app.modules.media import pipeline as pl
    from app.modules.media.policy import requeue_backoff_s, stale_uploaded_s

    ms = media_settings()
    now = utcnow()
    to_enqueue: list[tuple[uuid.UUID, Optional[int]]] = []
    interrupted: list[tuple[uuid.UUID, uuid.UUID, str]] = []
    worker_lost = exhausted = 0
    async with worker_session() as db:
        expired = (
            await db.execute(
                select(MediaFile)
                .where(
                    MediaFile.deleted_at.is_(None),
                    MediaFile.status.in_(pl.ACTIVE),
                    or_(MediaFile.lease_until.is_(None), MediaFile.lease_until < now),
                )
                .order_by(MediaFile.updated_at)
                .limit(limit)
                .with_for_update(skip_locked=True)
            )
        ).scalars().all()
        for m in expired:
            if m.attempts >= ms.MAX_ATTEMPTS:
                await pl.mark_worker_lost(db, m, exhausted=True)
                exhausted += 1
            else:
                await pl.mark_worker_lost(db, m, exhausted=False)
                to_enqueue.append((m.id, requeue_backoff_s(m.attempts)))
                worker_lost += 1
            interrupted.append((m.id, m.org_id, m.status))
        await db.flush()
        handled = [m.id for m in expired]
        stale = (
            await db.execute(
                select(MediaFile)
                .where(
                    MediaFile.deleted_at.is_(None),
                    MediaFile.id.not_in(handled) if handled else true(),
                    or_(
                        (MediaFile.status == S.UPLOADED.value)
                        & (MediaFile.updated_at < now - timedelta(seconds=stale_uploaded_s())),
                        (MediaFile.status == S.FAILED.value) & (MediaFile.attempts < ms.MAX_ATTEMPTS)
                        & (MediaFile.updated_at < now - timedelta(seconds=ms.RETRY_BASE_S * 10)),
                    ),
                )
                .order_by(MediaFile.updated_at)
                .limit(limit)
                .with_for_update(skip_locked=True)
            )
        ).scalars().all()
        for m in stale:
            m.updated_at = now  # no máximo um reenfileiramento por janela
            to_enqueue.append((m.id, None))
    # após o COMMIT: limpa workspaces das execuções mortas, enfileira e avisa o painel
    storage = get_storage()
    for mid, org_id, status in interrupted:
        try:
            storage.remove_workspaces(pl.workspace_prefix(mid))
        except Exception as exc:
            log.warning("media_workspace_cleanup_failed", error_type=type(exc).__name__)
        pl._publish(mid, org_id, status, 0, "requeued" if status == S.UPLOADED.value else "failed",
                    code=pl.WORKER_LOST)
        log.warning("media_worker_lost", media_id=str(mid), new_status=status)
    n = sum(1 for mid, cd in to_enqueue if enqueue_processing(mid, countdown=cd))
    return {"requeued": n, "worker_lost": worker_lost, "exhausted": exhausted}


# ─── Leitura para outros domínios ────────────────────────────────────────────

@dataclass(frozen=True)
class MediaDescriptor:
    media_id: uuid.UUID
    org_id: uuid.UUID
    kind: str
    sha256: str
    size: int
    mime: str
    width: int
    height: int
    duration_ms: Optional[int]
    orientation: Optional[str]
    animated: bool


async def get_media_descriptors(db: AsyncSession, media_ids: Iterable[Any]) -> dict:
    ids = list({uuid.UUID(str(i)) for i in media_ids})
    if not ids:
        return {}
    rows = (
        await db.execute(
            select(MediaFile).where(
                MediaFile.id.in_(ids), MediaFile.status == S.READY.value, MediaFile.purged_at.is_(None)
            )
        )
    ).scalars().all()
    return {
        m.id: MediaDescriptor(m.id, m.org_id, m.kind, m.sha256, int(m.size_bytes), m.mime_type, m.width, m.height,
                              m.duration_ms, m.orientation, bool(m.animated))
        for m in rows
    }


async def get_media_in_scope(db: AsyncSession, ctx: AuthContext, media_id: Any, perm: str = "media.view",
                             *, lock: Optional[str] = None) -> MediaFile:
    q = select(MediaFile).where(MediaFile.id == uuid.UUID(str(media_id)))
    if lock == "update":
        q = q.with_for_update()
    elif lock == "share":
        q = q.with_for_update(read=True)
    m = (await db.execute(q)).scalar_one_or_none()
    if m is None or m.deleted_at is not None:
        raise NotFound()
    ctx.ensure(perm, org_id=m.org_id)
    return m


async def ensure_media_usable(db: AsyncSession, ctx: AuthContext, media_id: Any, *, perm: str = "media.view",
                              for_update: bool = False) -> MediaFile:
    m = await get_media_in_scope(db, ctx, media_id, perm, lock="share" if for_update else None)
    if m.status != S.READY.value:
        raise DomainError("MEDIA_NOT_READY", "A mídia ainda não está pronta (ou foi rejeitada).", status=409,
                          details={"status": m.status})
    return m


# ─── Entrega com Range (painel e player) ─────────────────────────────────────

_UNSAT = object()


def parse_range(header: Optional[str], size: int):
    """Uma faixa ``bytes=a-b`` | ``a-`` | ``-n`` → (início, fim inclusivo); ``None`` = ignorar
    (corpo inteiro); ``_UNSAT`` = 416. Múltiplas faixas → ignoradas (200, permitido pela RFC 9110)."""
    if not header:
        return None
    h = header.strip()
    if not h.lower().startswith("bytes="):
        return None
    spec = h[6:].strip()
    if "," in spec:
        return None
    a, sep, b = spec.partition("-")
    if not sep:
        return None
    a, b = a.strip(), b.strip()
    if a == "":
        if not b.isdigit():
            return None
        n = int(b)
        if n == 0:
            return _UNSAT
        return max(0, size - n), size - 1
    if not a.isdigit():
        return None
    start = int(a)
    if b == "":
        end = size - 1
    elif b.isdigit():
        if int(b) < start:
            return None
        end = min(int(b), size - 1)
    else:
        return None
    if start >= size:
        return _UNSAT
    return start, end


@dataclass
class MediaRangeResult:
    status: int
    headers: dict
    body: Any  # AsyncIterator[bytes] | None
    media_type: str

    def to_response(self) -> Response:
        if self.body is None:
            return Response(status_code=self.status, headers=self.headers, media_type=self.media_type)
        return StreamingResponse(self.body, status_code=self.status, headers=self.headers, media_type=self.media_type)


_EXT = {"video/mp4": "mp4", "image/jpeg": "jpg", "image/png": "png", "image/gif": "gif"}


async def open_media_range(
    db: AsyncSession,
    media_id: Any,
    range_header: Optional[str] = None,
    *,
    if_range: Optional[str] = None,
    variant: str = "main",
    head_only: bool = False,
    cache_control: str = "private, max-age=86400",
) -> MediaRangeResult:
    """Entrega a saída normalizada (``variant="main"``) ou o preview com suporte a Range.

    NÃO autoriza: chame somente depois de autorizar (painel: escopo; player: manifesto).
    ETag forte = ``"<sha256>"`` do arquivo principal (preview: ``"<sha256>-preview"``).
    """
    m = await db.get(MediaFile, uuid.UUID(str(media_id)))
    if m is None or m.status != S.READY.value or m.purged_at is not None:
        raise NotFound()
    if variant == "preview":
        key, mime, size = m.preview_storage_key, m.preview_mime, m.preview_size_bytes
        etag = f'"{m.sha256}-preview"'
        if not key or not size:
            raise NotFound("MEDIA_PREVIEW_UNAVAILABLE", "Preview indisponível.")
    elif variant == "main":
        key, mime, size = m.storage_key, m.mime_type, m.size_bytes
        etag = f'"{m.sha256}"'
    else:
        raise ValueError("variant inválido")
    size = int(size)
    headers = {
        "Accept-Ranges": "bytes",
        "ETag": etag,
        "Cache-Control": cache_control,
        "X-Content-Type-Options": "nosniff",
        "Content-Disposition": f'inline; filename="{m.id}{"-preview" if variant == "preview" else ""}.{_EXT.get(mime, "bin")}"',
    }
    rng = parse_range(range_header, size)
    if rng is not None and if_range is not None and if_range.strip() != etag:
        rng = None  # If-Range divergente → corpo inteiro
    if rng is _UNSAT:
        headers["Content-Range"] = f"bytes */{size}"
        return MediaRangeResult(416, headers, None, mime)
    start, end = rng if rng else (0, size - 1)
    status = 206 if rng else 200
    if rng:
        headers["Content-Range"] = f"bytes {start}-{end}/{size}"
    headers["Content-Length"] = str(end - start + 1)
    if head_only:
        return MediaRangeResult(status, headers, None, mime)
    try:
        body = await get_storage().open_range(key, start, end)
    except (ObjectNotFound, InvalidRange):
        log.error("media_object_missing", media_id=str(m.id), variant=variant)
        raise NotFound("MEDIA_CONTENT_UNAVAILABLE", "Arquivo da mídia indisponível.") from None
    return MediaRangeResult(status, headers, body, mime)


# ─── Serialização (API) ──────────────────────────────────────────────────────

def media_out(m: MediaFile) -> dict:
    ready = m.status == S.READY.value
    return {
        "id": m.id,
        "org_id": m.org_id,
        "name": m.name,
        "original_filename": m.original_filename,
        "origin": m.origin,
        "kind": m.kind,
        "status": m.status,
        "status_code": m.status_code,
        "status_message": m.status_message,
        "progress_pct": m.progress_pct,
        "attempts": m.attempts,
        "extension_mismatch": m.extension_mismatch,
        "source": {
            "format": m.source_format,
            "mime": m.source_mime,
            "size_bytes": m.source_size_bytes,
            "sha256": m.source_sha256,
            "width": m.source_width,
            "height": m.source_height,
            "duration_ms": m.source_duration_ms,
            "codec": m.source_codec,
            "fps_milli": m.source_fps_milli,
            "has_audio": m.source_has_audio,
        },
        "output": {
            "mime_type": m.mime_type,
            "size_bytes": m.size_bytes,
            "sha256": m.sha256,
            "width": m.width,
            "height": m.height,
            "duration_ms": m.duration_ms,
            "codec": m.codec,
            "fps_milli": m.fps_milli,
            "orientation": m.orientation,
            "animated": m.animated,
            "frame_count": m.frame_count,
            "has_audio": False if ready else None,
        } if ready else None,
        "has_preview": bool(ready and m.preview_storage_key),
        "content_url": f"/api/v1/media/{m.id}/content" if ready else None,
        "preview_url": f"/api/v1/media/{m.id}/preview" if ready and m.preview_storage_key else None,
        "created_by": m.created_by,
        "created_at": m.created_at,
        "updated_at": m.updated_at,
        "ready_at": m.ready_at,
    }
