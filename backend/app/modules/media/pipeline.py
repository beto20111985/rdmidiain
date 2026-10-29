"""
RD Mídia Indoor — Pipeline assíncrono de mídia (executado no worker Celery, fila ``media``).

    UPLOADED → VALIDATING → PROCESSING → READY | REJECTED(motivo) | FAILED(retentável)

P001 §26–§28; P002 item 23 e §50 (worker reinicia); PU §40, §41, §66. Etapas:
1. **Claim** com lease CURTO (``lease_until`` = agora + ``MEDIA_LEASE_S``, padrão 90 s) e
   ``attempts`` como token: duas execuções da mesma mídia nunca processam ao mesmo tempo;
   READY/REJECTED → no-op (idempotente). Ao assumir, limpa restos de execuções anteriores
   interrompidas (workspace ``media-<id>-a<n>-*`` e objetos de saída órfãos).
2. **Heartbeat**: enquanto o pipeline progride (ffmpeg em andamento inclusive) o lease é
   renovado a cada ``MEDIA_HEARTBEAT_S`` (≤ lease/3) junto com o progresso. Se a renovação
   não encontra mais a posse (outra execução assumiu) ou falha por ~1 lease, a execução
   ABORTA (ffmpeg morto) e não grava nada. Se o worker morre, o lease vence em ≤ 90 s e o
   job ``media_maintenance`` (beat, 60 s) reenfileira UMA vez com backoff
   (``service.requeue_stale``); na última tentativa vira FAILED ``MEDIA_WORKER_LOST``.
3. **Validação técnica** (VALIDATING): magic bytes de novo sobre o arquivo armazenado;
   imagem → Pillow (``verify`` + decode completo de todos os quadros, EXIF, limites
   anti-bomba ANTES do decode); vídeo → ffprobe com demuxer forçado (codec,
   dimensões de exibição com rotação/SAR, fps, duração, streams).
4. **Processamento** (PROCESSING): vídeo → MP4 H.264 High yuv420p ``+faststart``,
   SEM áudio (``-an``), 1920×1080/1080×1920 (4K reduzido com escala exata), fps
   preservado até 30, CFR, metadados removidos; imagem → JPEG/PNG normalizado na
   resolução alvo sem esticar (GIF animado mantém ``image/gif``). Progresso REAL do
   ffmpeg publicado em ``media.processing``. ffmpeg/ffprobe morrem junto com o processo
   do worker (``PR_SET_PDEATHSIG``).
5. **Validação final**: ffprobe/Pillow da SAÍDA (codec, dimensões, sem áudio,
   duração ≥ duração de referência − 1 quadro — mídia nunca é cortada), SHA-256 e
   tamanho calculados ao gravar no storage. Chaves de saída únicas por execução
   (``main-<tentativa>-<nonce>.<ext>``): uma execução abandonada nunca apaga a saída de outra.
6. **Preview** para o painel (vídeo curto de baixa resolução / JPEG ≤ 640 px).
7. Temporários sempre removidos (workspace), inclusive em falha; o original é apagado
   após READY/REJECTED (``MEDIA_KEEP_SOURCE=false``).
"""
from __future__ import annotations

import asyncio
import secrets
import shutil
import threading
import time
import uuid
from dataclasses import dataclass, field
from datetime import timedelta
from pathlib import Path
from typing import Any, Optional

import structlog
from sqlalchemy import func, select, update

from app.core import events
from app.core.audit import audit
from app.core.authz import AuthContext
from app.core.database import create_engine, session_factory
from app.core.storage import ObjectNotFound, build_key, get_storage
from app.core.timeutils import utcnow
from app.models.media import MediaFile, MediaFileStatus
from app.modules.media import ffmpeg as ff
from app.modules.media.policy import (
    FFMPEG_DEMUXER,
    decide_resolution,
    heartbeat_interval_s,
    media_settings,
    target_fps,
)
from app.modules.media.sniff import SNIFF_BYTES, SniffRejected, head_of_file, sniff

log = structlog.get_logger("rd.media.pipeline")

S = MediaFileStatus
ACTIVE = (S.VALIDATING.value, S.PROCESSING.value)
WORKER_LOST = "MEDIA_WORKER_LOST"


class Rejected(Exception):
    """Arquivo inválido (definitivo)."""

    def __init__(self, code: str, message: str, details: Optional[dict] = None) -> None:
        super().__init__(code)
        self.code = code
        self.message = message
        self.details = details or {}


class Transient(Exception):
    """Falha transitória (retentável)."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(code)
        self.code = code
        self.message = message


@dataclass
class Outcome:
    media_id: str
    result: str  # ready|rejected|failed|noop|busy|deleted|not_found|abandoned
    status: Optional[str] = None
    code: Optional[str] = None
    retry: bool = False
    attempts: int = 0

    def as_dict(self) -> dict:
        return {"media_id": self.media_id, "result": self.result, "status": self.status, "code": self.code,
                "retry": self.retry, "attempts": self.attempts}


@dataclass
class Output:
    path: Path
    mime: str
    ext: str
    width: int
    height: int
    duration_ms: Optional[int]
    codec: str
    fps_milli: Optional[int] = None
    orientation: Optional[str] = None
    animated: bool = False
    frame_count: Optional[int] = None
    preview_path: Optional[Path] = None
    preview_mime: Optional[str] = None
    source: dict = field(default_factory=dict)  # metadados da fonte
    copy_source: bool = False  # saída = bytes da fonte (GIF animado já no tamanho)


SYSTEM_CTX = AuthContext.system(origin="worker")


class _Abandon(Exception):
    pass


class _Deleted(Exception):
    pass


def _owned(m: Optional[MediaFile], token: int) -> bool:
    """A execução ``token`` ainda é a dona da mídia?"""
    return m is not None and m.attempts == token and m.status in ACTIVE


# ─── Eventos ─────────────────────────────────────────────────────────────────

def _publish(m_id: Any, org_id: Any, status: str, progress: int, stage: str, *, code: Optional[str] = None,
             message: Optional[str] = None) -> None:
    try:
        events.publish_sync(
            "media.processing",
            {"id": str(m_id), "org_id": str(org_id), "status": status, "progress": int(progress), "stage": stage,
             "code": code, "message": message},
            org_id=org_id,
        )
    except Exception as exc:  # evento é best-effort; o estado vive no banco
        log.warning("media_event_publish_failed", error_type=type(exc).__name__)


# ─── Lease + heartbeat ───────────────────────────────────────────────────────

class _Lease:
    """Lease da execução, renovado por heartbeat (task asyncio) enquanto o pipeline progride.

    O ffmpeg roda em thread (``asyncio.to_thread``), então o loop continua livre para
    renovar. ``lost`` (``threading.Event``) é consultado pelo ffmpeg a cada ≤ 1 s.
    """

    def __init__(self, sf, media_id: uuid.UUID, org_id: Any, token: int) -> None:
        self.sf, self.media_id, self.org_id, self.token = sf, media_id, org_id, token
        self.lost = threading.Event()
        self.pct = 1
        self.renewals = 0
        self._last_ok = time.monotonic()
        self._task: Optional[asyncio.Task] = None

    def start(self) -> None:
        self._task = asyncio.create_task(self._loop())

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except BaseException:
                pass
            self._task = None

    def check(self) -> None:
        if self.lost.is_set():
            raise _Abandon()

    async def renew(self) -> bool:
        ms = media_settings()
        pct = min(99, max(1, int(self.pct)))
        async with self.sf() as db:
            async with db.begin():
                row = (
                    await db.execute(
                        update(MediaFile)
                        .where(MediaFile.id == self.media_id, MediaFile.attempts == self.token,
                               MediaFile.status.in_(ACTIVE))
                        .values(lease_until=utcnow() + timedelta(seconds=ms.LEASE_S),
                                progress_pct=func.greatest(MediaFile.progress_pct, pct))
                        .returning(MediaFile.id)
                        .execution_options(synchronize_session=False)
                    )
                ).first()
        return row is not None

    async def _loop(self) -> None:
        ms = media_settings()
        interval = heartbeat_interval_s()
        give_up_after = max(interval, ms.LEASE_S - interval)  # para ANTES do lease vencer
        while not self.lost.is_set():
            await asyncio.sleep(interval)
            try:
                ok = await self.renew()
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # banco indisponível: tenta de novo até perto do vencimento
                log.warning("media_lease_renew_failed", media_id=str(self.media_id), error_type=type(exc).__name__)
                if time.monotonic() - self._last_ok >= give_up_after:
                    log.error("media_lease_unrenewable", media_id=str(self.media_id))
                    self.lost.set()
                continue
            if ok:
                self._last_ok = time.monotonic()
                self.renewals += 1
            else:
                log.warning("media_lease_lost", media_id=str(self.media_id), token=self.token)
                self.lost.set()


# ─── Restos de execuções interrompidas ───────────────────────────────────────

def workspace_prefix(media_id: Any) -> str:
    return f"media-{media_id}-"


async def cleanup_interrupted(storage, media_id: Any, org_id: Any, keep: set) -> dict:
    """Remove workspaces de execuções anteriores e objetos de saída órfãos (gravados por uma
    execução que morreu antes de finalizar). Mantém ``keep`` (ex.: o original)."""
    removed_ws = 0
    try:
        removed_ws = await asyncio.to_thread(storage.remove_workspaces, workspace_prefix(media_id))
    except Exception as exc:
        log.warning("media_workspace_cleanup_failed", error_type=type(exc).__name__)
    removed = 0
    try:
        keys = await storage.list_keys(build_key("media", org_id, media_id))
    except NotImplementedError:
        keys = []
    except Exception as exc:
        log.warning("media_orphan_list_failed", error_type=type(exc).__name__)
        keys = []
    for k in keys:
        if k not in keep:
            await _delete_quiet(storage, k)
            removed += 1
    if removed_ws or removed:
        log.info("media_interrupted_cleanup", media_id=str(media_id), workspaces=removed_ws, objects=removed)
    return {"workspaces": removed_ws, "objects": removed}


async def mark_worker_lost(db, m: MediaFile, *, exhausted: bool) -> None:
    """Lease vencido (worker caiu/reiniciou no meio): devolve para a fila (UPLOADED) ou,
    se as tentativas acabaram, FAILED ``MEDIA_WORKER_LOST`` (arquivo que derruba o worker
    não fica em loop). Chamar com a linha travada (FOR UPDATE)."""
    before = {"status": m.status, "attempts": m.attempts, "progress_pct": m.progress_pct}
    m.lease_until = None
    m.progress_pct = 0
    m.status_code = WORKER_LOST
    if exhausted:
        m.status = S.FAILED.value
        m.status_message = ("O processamento foi interrompido repetidamente (queda ou reinício do worker). "
                            "Use “reprocessar” ou envie o arquivo novamente.")
        await audit(db, SYSTEM_CTX, "media.processing_failed", "media", m.id, before=before,
                    after={"code": WORKER_LOST, "attempts": m.attempts}, org_id=m.org_id)
        await _notify_error(db, m, m.status_message)
    else:
        m.status = S.UPLOADED.value
        m.status_message = "Processamento interrompido (queda ou reinício do worker); será retomado automaticamente."
        m.updated_at = utcnow()
        await audit(db, SYSTEM_CTX, "media.processing_interrupted", "media", m.id, before=before,
                    after={"status": m.status, "attempts": m.attempts}, org_id=m.org_id)


# ─── Entrada principal ───────────────────────────────────────────────────────

async def process_media(media_id: uuid.UUID) -> Outcome:
    eng = create_engine(nullpool=True)
    sf = session_factory(eng)
    try:
        return await _process(sf, media_id)
    finally:
        await _drain_notifications()
        await eng.dispose()


async def _process(sf, media_id: uuid.UUID) -> Outcome:
    ms = media_settings()
    now = utcnow()
    exhausted = False
    # ── 1. claim ──
    async with sf() as db:
        async with db.begin():
            m = (await db.execute(select(MediaFile).where(MediaFile.id == media_id).with_for_update())).scalar_one_or_none()
            if m is None:
                return Outcome(str(media_id), "not_found")
            if m.deleted_at is not None:
                return Outcome(str(media_id), "deleted", m.status)
            if m.status in (S.READY.value, S.REJECTED.value):
                return Outcome(str(media_id), "noop", m.status, attempts=m.attempts)
            if m.status in ACTIVE and m.lease_until and m.lease_until > now:
                return Outcome(str(media_id), "busy", m.status, attempts=m.attempts)
            if m.status == S.FAILED.value and m.attempts >= ms.MAX_ATTEMPTS:
                return Outcome(str(media_id), "noop", m.status, code=m.status_code, attempts=m.attempts)
            org_id, kind, fmt, src_key = m.org_id, m.kind, m.source_format, m.source_storage_key
            if m.status in ACTIVE and m.attempts >= ms.MAX_ATTEMPTS:
                # a ÚLTIMA tentativa morreu no meio (lease vencido) → não insiste
                await mark_worker_lost(db, m, exhausted=True)
                exhausted = True
                token = m.attempts
            else:
                m.status = S.VALIDATING.value
                m.attempts = (m.attempts or 0) + 1
                m.lease_until = now + timedelta(seconds=ms.LEASE_S)
                m.progress_pct = 1
                m.status_code = None
                m.status_message = None
                token = m.attempts

    storage = get_storage()
    # restos de execuções anteriores interrompidas (workspace e saídas órfãs); o original fica
    await cleanup_interrupted(storage, media_id, org_id, {src_key} if src_key else set())
    if exhausted:
        _publish(media_id, org_id, S.FAILED.value, 0, "failed", code=WORKER_LOST)
        return Outcome(str(media_id), "failed", S.FAILED.value, WORKER_LOST, retry=False, attempts=token)
    _publish(media_id, org_id, S.VALIDATING.value, 1, "validating")

    lease = _Lease(sf, media_id, org_id, token)
    lease.start()
    written: list[str] = []
    try:
        try:
            if not src_key:
                raise Transient("MEDIA_SOURCE_UNAVAILABLE", "Arquivo original indisponível para processamento.")
            async with storage.workspace(f"{workspace_prefix(media_id)}a{token}") as ws:
                src_cm = storage.materialize(src_key)
                try:
                    src = await src_cm.__aenter__()
                except ObjectNotFound:
                    raise Transient("MEDIA_SOURCE_UNAVAILABLE", "Arquivo original indisponível para processamento.") from None
                try:
                    out = await _run_pipeline(lease, kind, fmt, Path(src), ws)
                    lease.check()
                    nonce = secrets.token_hex(4)  # chave única por execução
                    main_key = build_key("media", org_id, media_id, f"main-{token}-{nonce}.{out.ext}")
                    written.append(main_key)
                    # copy_source: a saída são os próprios bytes da fonte (já validados) → cópia, nunca move
                    main_obj = await storage.put_file(main_key, out.path, out.mime, move=not out.copy_source)
                    prev_obj = None
                    prev_key = None
                    if out.preview_path and out.preview_path.exists():
                        pext = "mp4" if out.preview_mime == "video/mp4" else "jpg"
                        prev_key = build_key("media", org_id, media_id, f"preview-{token}-{nonce}.{pext}")
                        written.append(prev_key)
                        prev_obj = await storage.put_file(prev_key, out.preview_path, out.preview_mime, move=True)
                    lease.check()
                finally:
                    await src_cm.__aexit__(None, None, None)
        finally:
            await lease.stop()
        lease.check()

        # ── finalização ──
        delete_source = False
        async with sf() as db:
            async with db.begin():
                m = (await db.execute(select(MediaFile).where(MediaFile.id == media_id).with_for_update())).scalar_one()
                if not _owned(m, token):
                    raise _Abandon()
                if m.deleted_at is not None:
                    raise _Deleted()
                before = {"status": m.status}
                m.status = S.READY.value
                m.status_code = None
                m.status_message = None
                m.progress_pct = 100
                m.lease_until = None
                m.storage_key = main_key
                m.mime_type = out.mime
                m.size_bytes = main_obj.size
                m.sha256 = main_obj.sha256
                m.width, m.height = out.width, out.height
                m.duration_ms = out.duration_ms
                m.codec = out.codec
                m.fps_milli = out.fps_milli
                m.orientation = out.orientation
                m.animated = out.animated
                m.frame_count = out.frame_count
                m.preview_storage_key = prev_key
                m.preview_mime = out.preview_mime if prev_obj else None
                m.preview_size_bytes = prev_obj.size if prev_obj else None
                src_meta = out.source
                m.source_width = src_meta.get("width")
                m.source_height = src_meta.get("height")
                m.source_duration_ms = src_meta.get("duration_ms")
                m.source_codec = src_meta.get("codec")
                m.source_fps_milli = src_meta.get("fps_milli")
                m.source_has_audio = src_meta.get("has_audio")
                m.source_metadata = src_meta.get("metadata")
                m.processed_at = m.ready_at = utcnow()
                if not ms.KEEP_SOURCE and m.source_storage_key:
                    delete_source = True
                    m.source_storage_key = None
                await audit(db, SYSTEM_CTX, "media.ready", "media", m.id, before=before,
                            after={"status": m.status, "sha256": m.sha256, "size_bytes": m.size_bytes,
                                   "mime_type": m.mime_type, "width": m.width, "height": m.height,
                                   "duration_ms": m.duration_ms, "source_has_audio": m.source_has_audio},
                            org_id=m.org_id)
                attempts = m.attempts
        if delete_source:
            await _delete_quiet(storage, src_key)
        _publish(media_id, org_id, S.READY.value, 100, "ready")
        log.info("media_ready", media_id=str(media_id), attempts=attempts)
        return Outcome(str(media_id), "ready", S.READY.value, attempts=attempts)

    except Rejected as rej:
        await _cleanup(storage, written)
        return await _finish_rejected(sf, storage, media_id, token, org_id, src_key, rej)
    except Transient as tr:
        await _cleanup(storage, written)
        return await _finish_failed(sf, media_id, token, org_id, tr.code, tr.message)
    except _Abandon:
        await _cleanup(storage, written)  # só as chaves DESTA execução (únicas)
        log.warning("media_run_abandoned", media_id=str(media_id), token=token)
        return Outcome(str(media_id), "abandoned", attempts=token)
    except _Deleted:
        await _cleanup(storage, written)
        async with sf() as db:
            async with db.begin():
                m = await db.get(MediaFile, media_id, with_for_update=True)
                if _owned(m, token):
                    m.lease_until = None
                    m.status = S.FAILED.value
                    m.status_code = "MEDIA_DELETED_DURING_PROCESSING"
                    m.status_message = "Mídia excluída durante o processamento."
                    m.progress_pct = 0
        return Outcome(str(media_id), "deleted")
    except Exception as exc:  # erro inesperado → FAILED retentável (sem vazar detalhes)
        log.error("media_processing_error", media_id=str(media_id), error_type=type(exc).__name__, exc_info=exc)
        await _cleanup(storage, written)
        return await _finish_failed(sf, media_id, token, org_id, "MEDIA_PROCESSING_ERROR",
                                    "Falha temporária no processamento. Tente novamente.")


async def _notify_error(db, m: MediaFile, reason: str) -> None:
    """Notificação ``media.error`` (W-BIZ) na MESMA transação; falha dela não derruba o pipeline."""
    try:
        from app.modules.notifications.service import OrgRecipients, notify

        async with db.begin_nested():
            await notify(db, [OrgRecipients(m.org_id, "media.upload")], "media.error",
                         {"filename": m.name, "reason": reason}, dedupe_key=str(m.id),
                         entity=("media", m.id), org_id=m.org_id)
    except Exception as exc:
        log.warning("media_notify_failed", error_type=type(exc).__name__)


async def _drain_notifications() -> None:
    try:
        from app.modules.notifications import service as notify_service

        await notify_service.drain()
    except Exception as exc:  # pragma: no cover
        log.warning("media_notify_drain_failed", error_type=type(exc).__name__)


async def _delete_quiet(storage, key: Optional[str]) -> None:
    if not key:
        return
    try:
        await storage.delete(key)
    except Exception as exc:
        log.warning("media_storage_delete_failed", error_type=type(exc).__name__)


async def _cleanup(storage, keys: list[str]) -> None:
    for k in keys:
        await _delete_quiet(storage, k)


async def _finish_rejected(sf, storage, media_id, token, org_id, src_key, rej: Rejected) -> Outcome:
    delete_source = False
    async with sf() as db:
        async with db.begin():
            m = (await db.execute(select(MediaFile).where(MediaFile.id == media_id).with_for_update())).scalar_one()
            if not _owned(m, token):
                return Outcome(str(media_id), "abandoned", attempts=token)
            m.status = S.REJECTED.value
            m.status_code = rej.code
            m.status_message = rej.message
            m.progress_pct = 0
            m.lease_until = None
            m.processed_at = utcnow()
            if rej.details:
                m.source_metadata = {**(m.source_metadata or {}), "rejection": rej.details}
            if m.source_storage_key:
                delete_source = True
                m.source_storage_key = None
            await audit(db, SYSTEM_CTX, "media.rejected", "media", m.id, after={"code": rej.code}, reason=rej.message,
                        org_id=m.org_id)
            await _notify_error(db, m, rej.message)
            attempts = m.attempts
    if delete_source:
        await _delete_quiet(storage, src_key)
    _publish(media_id, org_id, S.REJECTED.value, 0, "rejected", code=rej.code, message=rej.message)
    log.info("media_rejected", media_id=str(media_id), code=rej.code)
    return Outcome(str(media_id), "rejected", S.REJECTED.value, rej.code, attempts=attempts)


async def _finish_failed(sf, media_id, token, org_id, code: str, message: str) -> Outcome:
    ms = media_settings()
    async with sf() as db:
        async with db.begin():
            m = (await db.execute(select(MediaFile).where(MediaFile.id == media_id).with_for_update())).scalar_one()
            if not _owned(m, token):
                return Outcome(str(media_id), "abandoned", attempts=token)
            m.status = S.FAILED.value
            m.status_code = code
            m.status_message = message
            m.progress_pct = 0
            m.lease_until = None
            await audit(db, SYSTEM_CTX, "media.processing_failed", "media", m.id,
                        after={"code": code, "attempts": m.attempts}, org_id=m.org_id)
            if m.attempts >= ms.MAX_ATTEMPTS:  # retentativas automáticas esgotadas → avisa a organização
                await _notify_error(db, m, message)
            attempts = m.attempts
    _publish(media_id, org_id, S.FAILED.value, 0, "failed", code=code, message=message)
    return Outcome(str(media_id), "failed", S.FAILED.value, code, retry=attempts < ms.MAX_ATTEMPTS, attempts=attempts)


# ─── Pipeline por tipo ───────────────────────────────────────────────────────

class _Progress:
    """Progresso real: evento a cada ≥2 p.p./1 s; persistido no banco pelo heartbeat do lease."""

    def __init__(self, lease: _Lease, status: str, stage: str, lo: int, hi: int) -> None:
        self.lease = lease
        self.media_id, self.org_id, self.status, self.stage = lease.media_id, lease.org_id, status, stage
        self.lo, self.hi = lo, hi
        self.pct = lo
        self._last_pub_pct = -100
        self._last_pub_t = 0.0

    def __call__(self, frac: float) -> None:
        pct = int(self.lo + max(0.0, min(1.0, frac)) * (self.hi - self.lo))
        if pct <= self.pct:
            return
        self.pct = pct
        self.lease.pct = pct
        t = time.monotonic()
        if pct - self._last_pub_pct >= 2 and t - self._last_pub_t >= 1.0:
            self._last_pub_pct, self._last_pub_t = pct, t
            _publish(self.media_id, self.org_id, self.status, pct, self.stage)


async def _set_stage(lease: _Lease, status: str, pct: int) -> None:
    ms = media_settings()
    async with lease.sf() as db:
        async with db.begin():
            m = await db.get(MediaFile, lease.media_id, with_for_update=True)
            if not _owned(m, lease.token):
                lease.lost.set()
                raise _Abandon()
            if m.deleted_at is not None:
                raise _Deleted()
            m.status = status
            m.progress_pct = max(int(pct), 1)
            m.lease_until = utcnow() + timedelta(seconds=ms.LEASE_S)
    lease.pct = max(lease.pct, int(pct))


async def _ffmpeg(lease: _Lease, args: list[str], **kwargs) -> None:
    """ffmpeg em thread (o heartbeat segue no loop); lease perdido → mata o ffmpeg e abandona."""
    try:
        await asyncio.to_thread(ff.run_ffmpeg, args, should_abort=lease.lost.is_set, **kwargs)
    except ff.FFmpegAborted:
        raise _Abandon() from None


async def _run_pipeline(lease: _Lease, kind: str, fmt: str, src: Path, ws: Path) -> Output:
    # magic bytes de novo (o arquivo armazenado precisa ser o que foi validado no upload)
    try:
        sn = sniff(head_of_file(src, SNIFF_BYTES))
    except SniffRejected as e:
        raise Rejected(e.code, e.message) from None
    if sn.format != fmt:
        raise Rejected("MEDIA_CORRUPTED", "Conteúdo do arquivo não confere com o tipo detectado no envio.")
    if kind == "IMAGE":
        return await _image_pipeline(lease, fmt, src, ws)
    return await _video_pipeline(lease, fmt, src, ws)


# ─── Imagem ──────────────────────────────────────────────────────────────────

_PIL_FORMATS = {"jpeg": ("JPEG", "MPO"), "png": ("PNG",), "gif": ("GIF",)}
_MAX_PIXELS = 4096 * 4096


def _corrupt_image() -> Rejected:
    return Rejected("MEDIA_CORRUPTED", "Imagem corrompida, incompleta ou impossível de decodificar.")


def inspect_image(path: Path, fmt: str) -> dict:
    """Validação completa da imagem (sem decodificar antes de checar dimensões)."""
    from PIL import Image, UnidentifiedImageError

    ms = media_settings()
    errors = (UnidentifiedImageError, OSError, SyntaxError, ValueError, EOFError, Image.DecompressionBombError,
              MemoryError, IndexError, TypeError, KeyError)
    allowed = list(_PIL_FORMATS[fmt])
    try:
        # formats=… restringe os decodificadores do Pillow ao tipo detectado (menor superfície)
        with Image.open(path, formats=allowed) as im:
            if im.format not in allowed:
                raise Rejected("MEDIA_CORRUPTED", "Conteúdo do arquivo não confere com o tipo detectado.")
            w, h = im.size
            if w <= 0 or h <= 0 or w * h > _MAX_PIXELS:
                raise Rejected("RESOLUTION_NOT_ALLOWED", f"Resolução {w}×{h} não aceita.", {"width": w, "height": h})
            orientation = 1
            try:
                orientation = int(im.getexif().get(0x0112, 1) or 1)
            except Exception:
                orientation = 1
            n_frames = int(getattr(im, "n_frames", 1) or 1)
            mode = im.mode
            icc = im.info.get("icc_profile")
    except Rejected:
        raise
    except errors:
        raise _corrupt_image() from None
    dw, dh = (h, w) if orientation in (5, 6, 7, 8) else (w, h)
    decision = decide_resolution(dw, dh)
    if not decision.ok:
        raise Rejected(decision.code, decision.message, {"width": dw, "height": dh})
    animated = fmt == "gif" and n_frames > 1
    if animated and n_frames > ms.MAX_GIF_FRAMES:
        raise Rejected("GIF_TOO_MANY_FRAMES", f"GIF com quadros demais ({n_frames}; máximo {ms.MAX_GIF_FRAMES}).")
    # integridade estrutural (CRC/estrutura) + decode COMPLETO de todos os quadros
    loop_ms = 0
    try:
        with Image.open(path, formats=allowed) as im:
            im.verify()
        with Image.open(path, formats=allowed) as im:
            if animated:
                for i in range(n_frames):
                    im.seek(i)
                    im.load()
                    loop_ms += int(im.info.get("duration", 0) or 0)
            else:
                im.load()
    except errors:
        raise _corrupt_image() from None
    return {
        "width": w, "height": h, "display_width": dw, "display_height": dh, "orientation_exif": orientation,
        "frames": n_frames, "animated": animated, "mode": mode, "icc": icc, "decision": decision,
        "loop_duration_ms": loop_ms if animated else None,
    }


def _fit_pad(im, tw: int, th: int):
    from PIL import Image, ImageOps

    if im.size == (tw, th):
        return im
    fitted = ImageOps.contain(im, (tw, th), Image.LANCZOS)
    if fitted.size == (tw, th):
        return fitted
    bg = (0, 0, 0, 0) if fitted.mode == "RGBA" else (0, 0, 0)
    canvas = Image.new(fitted.mode, (tw, th), bg)
    canvas.paste(fitted, ((tw - fitted.width) // 2, (th - fitted.height) // 2))
    return canvas


def render_image(path: Path, fmt: str, info: dict, ws: Path, should_abort=None) -> Output:
    from PIL import Image, ImageOps

    d = info["decision"]
    tw, th = d.target_w, d.target_h
    icc = info.get("icc")
    src_meta = {
        "width": info["display_width"], "height": info["display_height"], "codec": fmt, "has_audio": False,
        "duration_ms": None, "fps_milli": None,
        "metadata": {"pixel_size": [info["width"], info["height"]], "exif_orientation": info["orientation_exif"],
                     "mode": info["mode"], "frames": info["frames"], "loop_duration_ms": info["loop_duration_ms"],
                     "scaled": d.scaled, "padded": d.padded},
    }
    if info["animated"]:
        if not d.scaled and not d.padded and (info["width"], info["height"]) == (tw, th):
            out = Output(path=path, mime="image/gif", ext="gif", width=tw, height=th, duration_ms=None, codec="gif",
                         orientation=d.orientation, animated=True, frame_count=info["frames"], source=src_meta,
                         copy_source=True)
        else:
            dst = ws / "main.gif"
            vf = (f"scale={tw}:{th}:force_original_aspect_ratio=decrease:flags=lanczos,"
                  f"pad={tw}:{th}:(ow-iw)/2:(oh-ih)/2:color=black,split[a][b];[a]palettegen=reserve_transparent=1[p];"
                  f"[b][p]paletteuse")
            try:
                ff.run_ffmpeg(["-protocol_whitelist", "file", "-f", "gif", "-i", str(path), "-filter_complex", vf,
                               "-loop", "0", "-f", "gif", "-y", str(dst)], timeout_s=300, workdir=ws,
                              should_abort=should_abort)
            except ff.FFmpegTimeout:
                raise Transient("MEDIA_PROCESSING_TIMEOUT", "Tempo de processamento excedido.") from None
            except ff.FFmpegFailed as e:
                if e.looks_corrupted and not e.system_error:
                    raise _corrupt_image() from None
                raise Transient("MEDIA_TRANSCODE_FAILED", "Falha ao converter o GIF.") from None
            with Image.open(dst) as g:
                frames = int(getattr(g, "n_frames", 1))
                if g.size != (tw, th) or frames < 2:
                    raise Transient("MEDIA_OUTPUT_INVALID", "Saída do GIF inválida.")
            out = Output(path=dst, mime="image/gif", ext="gif", width=tw, height=th, duration_ms=None, codec="gif",
                         orientation=d.orientation, animated=True, frame_count=frames, source=src_meta)
    else:
        with Image.open(path) as im:
            im.load()
            orient = info["orientation_exif"]
            untouched = (orient in (1, 0) and not d.scaled and not d.padded and (info["width"], info["height"]) == (tw, th))
            if fmt == "jpeg" and untouched and im.mode in ("RGB", "L"):
                dst = ws / "main.jpg"
                # re-salva com as MESMAS tabelas de quantização (sem perda adicional) e SEM EXIF/GPS
                im.save(dst, "JPEG", quality="keep", subsampling="keep", icc_profile=icc)
                mime, ext = "image/jpeg", "jpg"
            else:
                img = ImageOps.exif_transpose(im) if orient not in (0, 1) else im
                if fmt == "jpeg":
                    img = img.convert("RGB")
                    img = _fit_pad(img, tw, th)
                    dst = ws / "main.jpg"
                    img.save(dst, "JPEG", quality=92, optimize=True, progressive=False, subsampling="4:2:0",
                             icc_profile=icc)
                    mime, ext = "image/jpeg", "jpg"
                else:
                    has_alpha = img.mode in ("RGBA", "LA", "PA") or "transparency" in img.info
                    img = img.convert("RGBA" if has_alpha else "RGB")
                    img = _fit_pad(img, tw, th)
                    dst = ws / "main.png"
                    img.save(dst, "PNG", compress_level=6, icc_profile=icc)
                    mime, ext = "image/png", "png"
        out = Output(path=dst, mime=mime, ext=ext, width=tw, height=th, duration_ms=None,
                     codec="jpeg" if ext == "jpg" else "png", orientation=d.orientation, animated=False, frame_count=1,
                     source=src_meta)
    _final_image_check(out)
    out.preview_path, out.preview_mime = _image_preview(out.path, ws), "image/jpeg"
    return out


def _final_image_check(out: Output) -> None:
    from PIL import Image

    try:
        with Image.open(out.path) as im:
            ok = im.size == (out.width, out.height) and im.format == {"jpg": "JPEG", "png": "PNG", "gif": "GIF"}[out.ext]
            im.load()
    except Exception:
        ok = False
    if not ok:
        raise Transient("MEDIA_OUTPUT_INVALID", "A imagem processada não passou na validação final.")


def _image_preview(path: Path, ws: Path) -> Path:
    from PIL import Image

    side = media_settings().PREVIEW_MAX_SIDE
    dst = ws / "preview.jpg"
    with Image.open(path) as im:
        im.seek(0)
        frame = im.convert("RGBA")
        frame.thumbnail((side, side), Image.LANCZOS)
        bg = Image.new("RGB", frame.size, (0, 0, 0))
        bg.paste(frame, mask=frame.split()[3])
        bg.save(dst, "JPEG", quality=80, optimize=True)
    return dst


async def _image_pipeline(lease: _Lease, fmt, src: Path, ws: Path) -> Output:
    info = await asyncio.to_thread(inspect_image, src, fmt)
    lease.check()
    await _set_stage(lease, S.PROCESSING.value, 30)
    _publish(lease.media_id, lease.org_id, S.PROCESSING.value, 30, "normalizing")
    try:
        return await asyncio.to_thread(render_image, src, fmt, info, ws, lease.lost.is_set)
    except ff.FFmpegAborted:
        raise _Abandon() from None


# ─── Vídeo ───────────────────────────────────────────────────────────────────

def _video_reject_from_probe(err: ff.ProbeError) -> Rejected:
    msg = str(err)
    if "sem stream de vídeo" in msg:
        return Rejected("MEDIA_NO_VIDEO_STREAM", "O arquivo não contém vídeo.")
    if "codec de vídeo desconhecido" in msg:
        return Rejected("MEDIA_UNSUPPORTED_CODEC", "Codec de vídeo não suportado.")
    return Rejected("MEDIA_CORRUPTED", "Vídeo corrompido, incompleto ou ilegível.")


def probe_source_video(path: Path, fmt: str) -> ff.VideoProbe:
    try:
        data = ff.ffprobe_json(path, FFMPEG_DEMUXER[fmt])
        return ff.analyze_video(data)
    except ff.ProbeUnavailable:
        raise Transient("MEDIA_PROBE_UNAVAILABLE", "Falha temporária ao analisar o vídeo.") from None
    except ff.ProbeError as e:
        raise _video_reject_from_probe(e) from None


def _reference_duration_ms(vp: ff.VideoProbe) -> Optional[int]:
    """Duração confiável do VÍDEO da fonte (para provar que nada foi cortado)."""
    if vp.stream_duration_ms:
        return vp.stream_duration_ms
    if vp.format_duration_ms and not vp.has_audio:
        return vp.format_duration_ms
    return None


def build_transcode_args(src: Path, fmt: str, vp: ff.VideoProbe, tw: int, th: int, fps: tuple[int, int], dst: Path) -> list[str]:
    ms = media_settings()
    fn, fd = fps
    pre = ""
    if vp.coded_w and vp.display_w and vp.rotation in (0, 180) and vp.display_w != vp.coded_w:
        pre = "scale=trunc(iw*sar/2)*2:ih,setsar=1,"  # anamórfico → pixels quadrados
    vf = (f"{pre}scale={tw}:{th}:force_original_aspect_ratio=decrease:flags=lanczos,"
          f"pad={tw}:{th}:(ow-iw)/2:(oh-ih)/2:color=black,setsar=1,fps={fn}/{fd},format=yuv420p")
    gop = max(1, int(round(2 * fn / fd)))
    return [
        "-protocol_whitelist", "file", "-f", FFMPEG_DEMUXER[fmt], "-i", str(src),
        "-map", f"0:{vp.stream_index}", "-an", "-sn", "-dn", "-map_metadata", "-1", "-map_chapters", "-1",
        "-vf", vf,
        "-c:v", "libx264", "-preset", x264_preset(), "-crf", str(ms.X264_CRF),
        "-maxrate", ms.MAXRATE, "-bufsize", ms.BUFSIZE,
        "-profile:v", "high", "-level:v", "4.1", "-pix_fmt", "yuv420p",
        "-g", str(gop), "-keyint_min", str(gop),
        "-fps_mode", "cfr",
        "-threads", str(ms.FFMPEG_THREADS),
        "-movflags", "+faststart", "-tag:v", "avc1",
        "-f", "mp4", "-y", str(dst),
    ]


def x264_preset() -> str:
    """Preset do x264. ``ultrafast`` desliga CABAC/8x8dct e o perfil cai para Constrained
    Baseline — proibido (contrato: H.264 High); é promovido a ``superfast``."""
    p = (media_settings().X264_PRESET or "medium").strip().lower()
    if p not in _X264_PRESETS or p == "ultrafast":
        log.warning("media_x264_preset_adjusted", configured=p)
        return "superfast"
    return p


_X264_PRESETS = {"ultrafast", "superfast", "veryfast", "faster", "fast", "medium", "slow", "slower", "veryslow"}


def _transcode_timeout(dur_ms: Optional[int]) -> float:
    ms = media_settings()
    if not dur_ms:
        return float(ms.TRANSCODE_TIMEOUT_MAX_S)
    return float(min(ms.TRANSCODE_TIMEOUT_MAX_S, max(ms.TRANSCODE_TIMEOUT_MIN_S, dur_ms / 1000 * ms.TRANSCODE_TIMEOUT_FACTOR)))


def verify_video_output(path: Path, tw: int, th: int) -> ff.VideoProbe:
    try:
        data = ff.ffprobe_json(path, "mov")
        out = ff.analyze_video(data)
    except (ff.ProbeError, ff.ProbeUnavailable):
        raise Transient("MEDIA_OUTPUT_INVALID", "O vídeo processado não passou na validação final.") from None
    streams = data.get("streams") or []
    problems = []
    if len(streams) != 1:
        problems.append("streams")
    if out.has_audio:
        problems.append("audio")
    if out.codec != "h264" or (out.profile or "") != "High":
        problems.append("codec/profile")
    if (out.coded_w, out.coded_h) != (tw, th) or out.rotation:
        problems.append("dimensions")
    if out.pix_fmt != "yuv420p":
        problems.append("pix_fmt")
    if not out.stream_duration_ms:
        problems.append("duration")
    if problems:
        log.error("media_output_invalid", problems=problems)
        raise Transient("MEDIA_OUTPUT_INVALID", "O vídeo processado não passou na validação final.")
    return out


async def _video_pipeline(lease: _Lease, fmt, src: Path, ws: Path) -> Output:
    ms = media_settings()
    media_id, org_id = lease.media_id, lease.org_id
    vp = await asyncio.to_thread(probe_source_video, src, fmt)
    lease.check()
    decision = decide_resolution(vp.display_w, vp.display_h)
    if not decision.ok:
        raise Rejected(decision.code, decision.message, {"width": vp.display_w, "height": vp.display_h})
    if vp.duration_ms is not None and vp.duration_ms > ms.MAX_VIDEO_DURATION_S * 1000:
        raise Rejected("VIDEO_TOO_LONG", f"Vídeo longo demais (máximo técnico {ms.MAX_VIDEO_DURATION_S} s).")
    fps = target_fps(vp.fps_num, vp.fps_den)
    tw, th = decision.target_w, decision.target_h
    ref_ms = _reference_duration_ms(vp)

    await _set_stage(lease, S.PROCESSING.value, 5)
    _publish(media_id, org_id, S.PROCESSING.value, 5, "transcoding")
    dst = ws / "main.mp4"
    prog = _Progress(lease, S.PROCESSING.value, "transcoding", 5, 88)
    try:
        await _ffmpeg(
            lease,
            build_transcode_args(src, fmt, vp, tw, th, fps, dst),
            timeout_s=_transcode_timeout(vp.duration_ms), duration_ms=vp.duration_ms or ref_ms,
            on_progress=prog, workdir=ws,
        )
    except ff.FFmpegTimeout:
        raise Transient("MEDIA_PROCESSING_TIMEOUT", "Tempo de processamento excedido.") from None
    except ff.FFmpegFailed as e:
        if e.looks_corrupted and not e.system_error:
            raise Rejected("MEDIA_CORRUPTED", "Vídeo corrompido, incompleto ou impossível de decodificar.") from None
        log.error("media_transcode_failed", rc=e.returncode, stderr_tail=e.stderr_tail[-500:])
        raise Transient("MEDIA_TRANSCODE_FAILED", "Falha ao converter o vídeo.") from None

    lease.check()
    out_vp = await asyncio.to_thread(verify_video_output, dst, tw, th)
    out_ms = out_vp.stream_duration_ms or 0
    frame_ms = 1000.0 * fps[1] / fps[0]
    src_frame_ms = (1000.0 * vp.fps_den / vp.fps_num) if vp.fps_num else frame_ms
    if ref_ms is not None and out_ms + max(frame_ms, src_frame_ms) + 1 < ref_ms:
        # nunca cortar: se não saiu tudo, a fonte estava truncada/corrompida
        raise Rejected(
            "MEDIA_CORRUPTED",
            "Vídeo incompleto ou corrompido: só foi possível decodificar parte dele.",
            {"source_duration_ms": ref_ms, "decoded_duration_ms": out_ms},
        )
    if out_ms > ms.MAX_VIDEO_DURATION_S * 1000:
        raise Rejected("VIDEO_TOO_LONG", f"Vídeo longo demais (máximo técnico {ms.MAX_VIDEO_DURATION_S} s).")

    # preview (baixa resolução, mudo) a partir da saída normalizada
    _publish(media_id, org_id, S.PROCESSING.value, 90, "preview")
    prev = ws / "preview.mp4"
    side = ms.PREVIEW_MAX_SIDE
    scale = f"{side}:-2" if tw >= th else f"-2:{side}"
    prev_prog = _Progress(lease, S.PROCESSING.value, "preview", 90, 97)
    lease.pct = max(lease.pct, 90)
    try:
        await _ffmpeg(
            lease,
            ["-protocol_whitelist", "file", "-f", "mov", "-i", str(dst), "-map", "0:v:0", "-an", "-sn", "-dn",
             "-map_metadata", "-1", "-t", str(ms.PREVIEW_MAX_S),
             "-vf", f"scale={scale}:flags=bilinear,fps={min(ms.PREVIEW_FPS, max(1, round(fps[0] / fps[1])))},format=yuv420p",
             "-c:v", "libx264", "-preset", "veryfast", "-crf", "30", "-maxrate", "700k", "-bufsize", "1400k",
             "-profile:v", "main", "-threads", str(ms.FFMPEG_THREADS), "-movflags", "+faststart",
             "-f", "mp4", "-y", str(prev)],
            timeout_s=_transcode_timeout(min(out_ms, ms.PREVIEW_MAX_S * 1000)), duration_ms=min(out_ms, ms.PREVIEW_MAX_S * 1000),
            on_progress=prev_prog, workdir=ws,
        )
    except (ff.FFmpegTimeout, ff.FFmpegFailed):
        raise Transient("MEDIA_PREVIEW_FAILED", "Falha ao gerar o preview.") from None

    return Output(
        path=dst, mime="video/mp4", ext="mp4", width=tw, height=th, duration_ms=out_ms, codec="h264",
        fps_milli=out_vp.fps_milli, orientation=decision.orientation, animated=False, frame_count=out_vp.nb_frames,
        preview_path=prev, preview_mime="video/mp4",
        source={
            "width": vp.display_w, "height": vp.display_h, "duration_ms": vp.duration_ms, "codec": vp.codec,
            "fps_milli": vp.fps_milli, "has_audio": vp.has_audio,
            "metadata": {**vp.summary(), "target_fps": f"{fps[0]}/{fps[1]}", "scaled": decision.scaled,
                         "padded": decision.padded, "reference_duration_ms": ref_ms},
        },
    )


# ─── Manutenção ──────────────────────────────────────────────────────────────

def sweep_storage_staging() -> int:
    st = get_storage()
    fn = getattr(st, "sweep_staging", None)
    return int(fn()) if fn else 0


def workspace_free_bytes() -> Optional[int]:
    st = get_storage()
    root = getattr(st, "root", None)
    if root is None:
        return None
    try:
        Path(root).mkdir(parents=True, exist_ok=True)
        return shutil.disk_usage(root).free
    except OSError:
        return None
