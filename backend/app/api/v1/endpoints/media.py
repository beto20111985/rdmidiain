"""
RD Mídia Indoor — Biblioteca de mídia (API do painel). Dono: W-MEDIA (AÇÃO 002).

Rotas (prefixo ``/api/v1/media``; todas com Bearer; escopo ORGANIZATION; fora → 404):

| Método | Rota | Permissão | Descrição |
|---|---|---|---|
| GET | ``/limits`` | media.view | requisitos exibidos na UI (tipos, extensões, tamanhos, resoluções) |
| POST | ``/upload`` | media.upload | ``multipart/form-data`` (campo ``file``; opcionais ``name``, ``org_id``) em STREAMING → 202 (nova, processamento assíncrono) / 200 (mesmo arquivo já existente, ``duplicate=true``) |
| GET | ``""`` | media.view | lista paginada ``{items,total,page,page_size}`` (filtros status, kind, orientation, q, org_id; ``sort``) |
| GET | ``/{id}`` | media.view | detalhe com metadados (tipo, dimensões, duração, tamanho, codec, orientação, sha256) |
| GET/HEAD | ``/{id}/content`` | media.view | arquivo normalizado (Range/206, ETag = sha256) |
| GET/HEAD | ``/{id}/preview`` | media.view | preview (vídeo curto de baixa resolução mudo / JPEG ≤ 640 px) |
| GET | ``/{id}/usage`` | media.view | referências (criativos, manifestos, …) — antes de excluir |
| PATCH | ``/{id}`` | media.upload | renomear |
| POST | ``/{id}/retry`` | media.upload | reprocessar mídia FAILED |
| DELETE | ``/{id}`` | media.delete | exclusão segura (409 ``MEDIA_IN_USE`` se referenciada) |

Organização dona do upload: campo ``org_id`` (antes do arquivo) ou cabeçalho
``X-RD-Context: adv:<org>|est:<org>``; sem isso, a única organização do escopo do
usuário. Sempre validada contra o escopo (``media.upload``) — NUNCA aceita em query.
Eventos: ``media.processing`` (status/progresso real). Removidas: ``/{id}/serve``
(token em query, sem escopo) e ``/{id}/status`` (o detalhe traz o status). O player
usa ``/api/v1/player/media/{id}`` (W-PLAY → ``service.open_media_range``).
"""
from __future__ import annotations

import uuid
from typing import Literal, Optional

from fastapi import APIRouter, Depends, Query, Request, Response
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.audit import audit
from app.core.authz import AuthContext, require
from app.core.database import get_db
from app.core.errors import DomainError
from app.models.media import MediaFile, MediaFileStatus
from app.modules.media import service as svc
from app.modules.media.policy import max_upload_bytes, media_settings, policy_summary
from app.modules.media.schemas import MediaOut, MediaPage, MediaPatchIn, MediaUploadOut, MediaUsageOut
from app.modules.media.sniff import sanitize_filename
from app.modules.media.upload import MultipartFileStream

router = APIRouter()

S = MediaFileStatus
_REJECTION_CODES = {"MEDIA_TYPE_NOT_ALLOWED", "MEDIA_EXTENSION_NOT_ALLOWED", "MEDIA_TOO_LARGE", "MEDIA_EMPTY",
                    "MEDIA_MALFORMED_UPLOAD", "MEDIA_SINGLE_FILE_ONLY"}

_UPLOAD_OPENAPI = {
    "requestBody": {
        "required": True,
        "content": {
            "multipart/form-data": {
                "schema": {
                    "type": "object",
                    "required": ["file"],
                    "properties": {
                        "file": {"type": "string", "format": "binary",
                                 "description": "JPG/PNG/GIF ≤ 10 MB; vídeo (MP4/WebM/MOV/MKV/AVI/TS) ≤ 200 MB"},
                        "name": {"type": "string", "maxLength": 255},
                        "org_id": {"type": "string", "format": "uuid",
                                   "description": "Organização dona (envie ANTES do arquivo)"},
                    },
                }
            }
        },
    }
}


def _uuid_field(value: Optional[str]) -> Optional[uuid.UUID]:
    if value is None or value == "":
        return None
    try:
        return uuid.UUID(value)
    except ValueError:
        raise DomainError("VALIDATION_ERROR", "org_id inválido.", status=422) from None


@router.get("/limits", summary="Requisitos de upload (tipos, tamanhos, resoluções)")
async def media_limits(ctx: AuthContext = Depends(require("media.view"))) -> dict:
    return policy_summary()


@router.post(
    "/upload",
    status_code=202,
    response_model=MediaUploadOut,
    responses={200: {"model": MediaUploadOut, "description": "Arquivo idêntico já existente (idempotência)"}},
    openapi_extra=_UPLOAD_OPENAPI,
    summary="Enviar mídia (streaming; processamento assíncrono)",
)
async def upload_media(
    request: Request,
    response: Response,
    ctx: AuthContext = Depends(require("media.upload")),
    db: AsyncSession = Depends(get_db),
):
    if "org_id" in request.query_params:
        raise DomainError("MEDIA_ORG_IN_QUERY_NOT_ALLOWED",
                          "A organização não é aceita na URL; use o campo org_id ou o contexto ativo.", status=400)
    header_org = svc.parse_context_org(request.headers.get("x-rd-context"))
    org_id: Optional[uuid.UUID] = None
    filename: Optional[str] = None
    try:
        cl = request.headers.get("content-length")
        if cl and cl.isdigit() and int(cl) > max_upload_bytes() + media_settings().MULTIPART_OVERHEAD_BYTES:
            raise svc.too_large_error(max_upload_bytes(), None)
        mp = MultipartFileStream(request, idle_timeout_s=media_settings().UPLOAD_IDLE_TIMEOUT_S)
        await mp.wait_file()
        filename = mp.filename
        early_org = _uuid_field(mp.fields.get("org_id"))
        org_id = await svc.resolve_target_org(db, ctx, "media.upload", early_org or header_org)
        svc.check_free_space()
        res = await svc.intake_stream(db, ctx, org_id, mp.filename, mp.file_content_type, mp.file_chunks(),
                                      name=mp.fields.get("name"), created_by=ctx.user_id)
        await mp.finish()
    except DomainError as e:
        if e.code in _REJECTION_CODES:
            await svc.audit_upload_rejection(ctx, org_id, e, filename)
        raise
    late_org = _uuid_field(mp.fields.get("org_id"))
    if late_org is not None and late_org != org_id:
        raise DomainError("MEDIA_ORG_MISMATCH", "org_id diverge do contexto ativo.", status=400)
    late_name = mp.fields.get("name")
    if res.created and late_name and res.media.name != sanitize_filename(late_name)[:255]:
        res.media.name = sanitize_filename(late_name)[:255]
    await db.flush()
    await db.refresh(res.media)
    if not res.created:
        response.status_code = 200
    return {**svc.media_out(res.media), "duplicate": not res.created}


_SORTS = {
    "created_at": MediaFile.created_at.asc(),
    "-created_at": MediaFile.created_at.desc(),
    "name": MediaFile.name.asc(),
    "-name": MediaFile.name.desc(),
    "size": MediaFile.source_size_bytes.asc(),
    "-size": MediaFile.source_size_bytes.desc(),
}


def _escape_like(s: str) -> str:
    return s.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


@router.get("", response_model=MediaPage, summary="Biblioteca de mídia (paginada, por escopo)")
async def list_media(
    status: Optional[Literal["UPLOADED", "VALIDATING", "PROCESSING", "READY", "REJECTED", "FAILED"]] = None,
    kind: Optional[Literal["IMAGE", "VIDEO"]] = None,
    orientation: Optional[Literal["HORIZONTAL", "VERTICAL"]] = None,
    q: Optional[str] = Query(None, max_length=100),
    org_id: Optional[uuid.UUID] = Query(None, description="Filtro (dentro do escopo do usuário)"),
    sort: Literal["created_at", "-created_at", "name", "-name", "size", "-size"] = "-created_at",
    page: int = Query(1, ge=1),
    page_size: int = Query(25, ge=1, le=100),
    ctx: AuthContext = Depends(require("media.view")),
    db: AsyncSession = Depends(get_db),
):
    conds = [MediaFile.deleted_at.is_(None), ctx.scope_filter("media.view", org_col=MediaFile.org_id)]
    if status:
        conds.append(MediaFile.status == status)
    if kind:
        conds.append(MediaFile.kind == kind)
    if orientation:
        conds.append(MediaFile.orientation == orientation)
    if org_id:
        conds.append(MediaFile.org_id == org_id)
    if q:
        conds.append(MediaFile.name.ilike(f"%{_escape_like(q.strip())}%", escape="\\"))
    total = (await db.execute(select(func.count()).select_from(MediaFile).where(*conds))).scalar_one()
    rows = (
        await db.execute(
            select(MediaFile).where(*conds).order_by(_SORTS[sort], MediaFile.id)
            .offset((page - 1) * page_size).limit(page_size)
        )
    ).scalars().all()
    return {"items": [svc.media_out(m) for m in rows], "total": total, "page": page, "page_size": page_size}


@router.get("/{media_id}", response_model=MediaOut, summary="Detalhe da mídia")
async def get_media(media_id: uuid.UUID, ctx: AuthContext = Depends(require("media.view")),
                    db: AsyncSession = Depends(get_db)):
    m = await svc.get_media_in_scope(db, ctx, media_id, "media.view")
    return svc.media_out(m)


async def _serve(request: Request, ctx: AuthContext, db: AsyncSession, media_id: uuid.UUID, variant: str) -> Response:
    m = await svc.get_media_in_scope(db, ctx, media_id, "media.view")
    if m.status != S.READY.value:
        raise DomainError("MEDIA_NOT_READY", "A mídia ainda não está pronta (ou foi rejeitada).", status=409,
                          details={"status": m.status})
    res = await svc.open_media_range(
        db, m.id, request.headers.get("range"), if_range=request.headers.get("if-range"), variant=variant,
        head_only=request.method == "HEAD",
    )
    return res.to_response()


@router.get("/{media_id}/content", summary="Arquivo normalizado (Range/206; ETag = sha256)", response_class=Response)
@router.head("/{media_id}/content", include_in_schema=False)
async def media_content(media_id: uuid.UUID, request: Request, ctx: AuthContext = Depends(require("media.view")),
                        db: AsyncSession = Depends(get_db)):
    return await _serve(request, ctx, db, media_id, "main")


@router.get("/{media_id}/preview", summary="Preview (Range)", response_class=Response)
@router.head("/{media_id}/preview", include_in_schema=False)
async def media_preview(media_id: uuid.UUID, request: Request, ctx: AuthContext = Depends(require("media.view")),
                        db: AsyncSession = Depends(get_db)):
    return await _serve(request, ctx, db, media_id, "preview")


@router.get("/{media_id}/usage", response_model=MediaUsageOut, summary="Referências da mídia")
async def media_usage(media_id: uuid.UUID, ctx: AuthContext = Depends(require("media.view")),
                      db: AsyncSession = Depends(get_db)):
    m = await svc.get_media_in_scope(db, ctx, media_id, "media.view")
    try:
        refs = (await svc.count_references(db, [m.id]))[m.id]
    except svc.ReferenceCheckFailed:
        raise DomainError("MEDIA_REFERENCE_CHECK_FAILED", "Não foi possível consultar as referências.", status=409)
    public = svc.refs_public(refs)
    return {"media_id": m.id, "in_use": bool(public), "references": public}


@router.patch("/{media_id}", response_model=MediaOut, summary="Renomear mídia")
async def patch_media(media_id: uuid.UUID, body: MediaPatchIn, ctx: AuthContext = Depends(require("media.upload")),
                      db: AsyncSession = Depends(get_db)):
    m = await svc.get_media_in_scope(db, ctx, media_id, "media.upload", lock="update")
    new = sanitize_filename(body.name)[:255]
    if new != m.name:
        before = {"name": m.name}
        m.name = new
        await audit(db, ctx, "media.updated", "media", m.id, before=before, after={"name": new}, org_id=m.org_id)
    await db.flush()
    await db.refresh(m)
    return svc.media_out(m)


@router.post("/{media_id}/retry", response_model=MediaOut, status_code=202, summary="Reprocessar mídia com falha")
async def retry_media(media_id: uuid.UUID, ctx: AuthContext = Depends(require("media.upload")),
                      db: AsyncSession = Depends(get_db)):
    m = await svc.get_media_in_scope(db, ctx, media_id, "media.upload", lock="update")
    if m.status != S.FAILED.value:
        raise DomainError("MEDIA_RETRY_NOT_ALLOWED", "Só é possível reprocessar mídia com falha (FAILED).", status=409,
                          details={"status": m.status})
    if not m.source_storage_key:
        raise DomainError("MEDIA_SOURCE_UNAVAILABLE", "Arquivo original indisponível; envie novamente.", status=409)
    before = {"status": m.status, "attempts": m.attempts, "code": m.status_code}
    m.status = S.UPLOADED.value
    m.attempts = 0
    m.status_code = None
    m.status_message = None
    m.progress_pct = 0
    m.lease_until = None
    await audit(db, ctx, "media.retry_requested", "media", m.id, before=before, after={"status": m.status},
                org_id=m.org_id)
    svc.enqueue_after_commit(db, m.id)
    await db.flush()
    await db.refresh(m)
    return svc.media_out(m)


@router.delete("/{media_id}", status_code=204, summary="Excluir mídia (segura)")
async def delete_media(media_id: uuid.UUID, reason: Optional[str] = Query(None, max_length=500),
                       ctx: AuthContext = Depends(require("media.delete")), db: AsyncSession = Depends(get_db)):
    m = await svc.get_media_in_scope(db, ctx, media_id, "media.delete")
    await svc.soft_delete_media(db, ctx, m, reason)
    return Response(status_code=204)
