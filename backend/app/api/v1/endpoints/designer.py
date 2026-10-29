"""RD Mídia Indoor — Produção de arte / Designer (AÇÃO 002 / W-BIZ). Prefixo ``/api/v1/design``."""
from __future__ import annotations

from datetime import datetime
from typing import Literal, Optional
from uuid import UUID

from fastapi import APIRouter, Depends, File, Form, Query, UploadFile
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.authz import AuthContext, get_auth_context, require
from app.core.database import get_db
from app.core.errors import DomainError, NotFound
from app.core.timeutils import isoformat_z
from app.models.designer import (
    DesignApproval,
    DesignAttachment,
    DesignEvent,
    DesignExtraRevision,
    DesignRequest,
    DesignVersion,
)
from app.modules.designer import files as dfiles
from app.modules.designer import service as svc

router = APIRouter()


def _z(dt: Optional[datetime]) -> Optional[str]:
    return isoformat_z(dt) if dt else None


def _s(v) -> Optional[str]:
    return str(v) if v is not None else None


def _req_out(r: DesignRequest) -> dict:
    return {
        "id": str(r.id), "code": r.code, "org_id": str(r.org_id), "establishment_id": _s(r.establishment_id),
        "kind": r.kind, "title": r.title, "briefing": r.briefing, "texts": r.texts or [], "references": r.references or [],
        "orientation": r.orientation, "media_type": r.media_type, "duration_s": r.duration_s, "status": r.status,
        "requested_by": str(r.requested_by), "assigned_to": _s(r.assigned_to), "billing": r.billing,
        "price_cents": r.price_cents, "payment_status": r.payment_status, "payment_order_id": _s(r.payment_order_id),
        # ``extra_revisions_count`` = revisões extras LIBERADAS (inteiro). ``extra_revisions`` é alias
        # DEPRECADO do mesmo inteiro (compatibilidade); a lista de compras fica em ``extra_revision_purchases``.
        "revisions_included": r.revisions_included, "extra_revisions_count": r.extra_revisions,
        "extra_revisions": r.extra_revisions,
        "revisions_used": r.revisions_used,
        "revisions_remaining": max(0, r.revisions_included + r.extra_revisions - r.revisions_used),
        "approved_version_id": _s(r.approved_version_id), "approved_at": _z(r.approved_at),
        "published_media_id": _s(r.published_media_id), "published_at": _z(r.published_at), "due_at": _z(r.due_at),
        "cancel_reason": r.cancel_reason, "version": r.version, "created_at": _z(r.created_at),
        "updated_at": _z(r.updated_at),
    }


def _ver_out(v: DesignVersion) -> dict:
    return {"id": str(v.id), "number": v.number, "source": v.source, "template_id": _s(v.template_id),
            "filename": v.filename, "content_type": v.content_type, "size_bytes": v.size_bytes, "sha256": v.sha256,
            "width": v.width, "height": v.height, "notes": v.notes, "status": v.status, "locked": v.locked,
            "created_by": str(v.created_by), "created_at": _z(v.created_at)}


# ─── Pedidos ─────────────────────────────────────────────────────────────────

class RequestIn(BaseModel):
    org_id: UUID
    establishment_id: Optional[UUID] = None
    kind: Optional[Literal["ANUNCIO_EXTERNO", "CONTEUDO_INTERNO"]] = None
    title: str = Field(min_length=3, max_length=200)
    briefing: str = Field(min_length=10, max_length=10000)
    texts: list[str] = Field(default_factory=list, max_length=30)
    references: list[str] = Field(default_factory=list, max_length=30)
    orientation: Literal["HORIZONTAL", "VERTICAL"] = "HORIZONTAL"
    media_type: Literal["IMAGEM", "VIDEO"] = "IMAGEM"
    duration_s: int = 30
    due_at: Optional[datetime] = None
    billing: Optional[Literal["COBRAR", "NEGOCIADO", "ISENTO"]] = None  # equipe RD
    price_cents: Optional[int] = Field(None, ge=0)


@router.get("/requests")
async def list_requests(
    status: Optional[str] = None, org_id: Optional[UUID] = None,
    page: int = Query(1, ge=1), page_size: int = Query(25, ge=1, le=100),
    ctx: AuthContext = Depends(get_auth_context), db: AsyncSession = Depends(get_db),
):
    if not any(ctx.has_any(p) for p in ("design.view", "design.work", "design.request", "design.approve")):
        raise DomainError("FORBIDDEN", "Você não tem permissão para esta ação.", status=403)
    cond = [svc.list_filter(ctx)]
    if status:
        cond.append(DesignRequest.status == status)
    if org_id:
        cond.append(DesignRequest.org_id == org_id)
    total = (await db.execute(select(func.count()).select_from(DesignRequest).where(*cond))).scalar_one()
    rows = (await db.execute(select(DesignRequest).where(*cond).order_by(DesignRequest.created_at.desc())
                             .offset((page - 1) * page_size).limit(page_size))).scalars()
    return {"items": [_req_out(r) for r in rows], "total": total, "page": page, "page_size": page_size}


@router.post("/requests", status_code=201)
async def create_request(body: RequestIn, ctx: AuthContext = Depends(get_auth_context), db: AsyncSession = Depends(get_db)):
    if not (ctx.has_any("design.request") or svc.is_worker(ctx)):
        raise DomainError("FORBIDDEN", "Você não tem permissão para esta ação.", status=403,
                          details={"permission": "design.request"})
    data = body.model_dump()
    if not svc.is_worker(ctx):
        data.pop("billing", None)
        data.pop("price_cents", None)
    return _req_out(await svc.create_request(db, ctx, data))


@router.get("/requests/{request_id}")
async def get_request(request_id: UUID, ctx: AuthContext = Depends(get_auth_context), db: AsyncSession = Depends(get_db)):
    r = await svc.get_request(db, ctx, request_id)
    out = _req_out(r)
    out["versions"] = [_ver_out(v) for v in (await db.execute(
        select(DesignVersion).where(DesignVersion.request_id == r.id).order_by(DesignVersion.number))).scalars()]
    out["attachments"] = [{"id": str(a.id), "kind": a.kind, "filename": a.filename, "content_type": a.content_type,
                           "size_bytes": a.size_bytes, "sha256": a.sha256, "created_at": _z(a.created_at)}
                          for a in (await db.execute(select(DesignAttachment).where(DesignAttachment.request_id == r.id)
                                                     .order_by(DesignAttachment.created_at))).scalars()]
    out["approvals"] = [{"id": str(a.id), "version_id": str(a.version_id), "decision": a.decision, "comment": a.comment,
                         "user_id": str(a.user_id), "ip_address": a.ip_address, "user_agent": a.user_agent,
                         "version_sha256": a.version_sha256, "revision_number": a.revision_number,
                         "decided_at": _z(a.decided_at)}
                        for a in (await db.execute(select(DesignApproval).where(DesignApproval.request_id == r.id)
                                                   .order_by(DesignApproval.decided_at))).scalars()]
    out["history"] = [{"from": e.from_status, "to": e.to_status, "note": e.note, "at": _z(e.created_at)}
                      for e in (await db.execute(select(DesignEvent).where(DesignEvent.request_id == r.id)
                                                 .order_by(DesignEvent.created_at))).scalars()]
    out["extra_revision_purchases"] = [
        {"id": str(x.id), "status": x.status, "price_cents": x.price_cents, "reason": x.reason,
         "payment_order_id": _s(x.payment_order_id), "created_at": _z(x.created_at), "decided_at": _z(x.decided_at),
         "released_at": _z(x.released_at)}
        for x in (await db.execute(select(DesignExtraRevision).where(
            DesignExtraRevision.request_id == r.id).order_by(DesignExtraRevision.created_at))).scalars()]
    return out


class QuoteIn(BaseModel):
    billing: Literal["COBRAR", "NEGOCIADO", "ISENTO"]
    price_cents: int = Field(0, ge=0)
    note: Optional[str] = Field(None, max_length=1000)


@router.post("/requests/{request_id}/quote")
async def quote(request_id: UUID, body: QuoteIn, ctx: AuthContext = Depends(require("design.work", global_only=True)),
                db: AsyncSession = Depends(get_db)):
    return _req_out(await svc.set_quote(db, ctx, request_id, body.billing, body.price_cents, note=body.note))


@router.post("/requests/{request_id}/attachments", status_code=201)
async def upload_attachment(
    request_id: UUID,
    kind: Literal["LOGOTIPO", "IMAGEM", "VIDEO", "REFERENCIA", "TEXTO", "OUTRO"] = Form(...),
    file: UploadFile = File(...),
    ctx: AuthContext = Depends(get_auth_context), db: AsyncSession = Depends(get_db),
):
    r = await svc.get_request(db, ctx, request_id)
    if not (svc.is_worker(ctx) or ctx.can("design.request", org_id=r.org_id, establishment_id=r.establishment_id)):
        raise DomainError("FORBIDDEN", "Sem permissão para anexar ao briefing.", status=403)
    stored = await dfiles.store_upload(file, ("design", r.id, "attachments"),
                                       allowed={"png", "jpeg", "gif", "webp", "svg", "pdf", "mp4", "webm", "txt"},
                                       limits=dfiles.ATTACHMENT_LIMITS)
    dfiles.delete_on_rollback(db, stored.key)
    a = await svc.add_attachment(db, ctx, r, kind, stored)
    return {"id": str(a.id), "kind": a.kind, "filename": a.filename, "content_type": a.content_type,
            "size_bytes": a.size_bytes, "sha256": a.sha256}


async def _stream(key: str, size: int, content_type: str, filename: str) -> StreamingResponse:
    from app.core.storage import get_storage

    body = await get_storage().open_range(key, 0, size - 1 if size else None)
    return StreamingResponse(body, media_type=content_type, headers={
        "Content-Disposition": f'attachment; filename="{filename}"', "X-Content-Type-Options": "nosniff",
        "Cache-Control": "private, no-store", "Content-Length": str(size)})


@router.get("/requests/{request_id}/attachments/{attachment_id}/file")
async def download_attachment(request_id: UUID, attachment_id: UUID, ctx: AuthContext = Depends(get_auth_context),
                              db: AsyncSession = Depends(get_db)):
    r = await svc.get_request(db, ctx, request_id)
    a = await db.get(DesignAttachment, attachment_id)
    if a is None or a.request_id != r.id:
        raise NotFound()
    return await _stream(a.storage_key, a.size_bytes, a.content_type, a.filename)


@router.get("/designers")
async def list_designers(ctx: AuthContext = Depends(require("design.work", global_only=True)),
                         db: AsyncSession = Depends(get_db)):
    """Quem pode receber pedidos de arte (``design.work`` GLOBAL): id, nome e carga atual — sem dados pessoais."""
    from app.modules.support.people import users_with_permission

    cands = await users_with_permission(db, "design.work", global_only=True)
    active = (svc.D.NA_FILA.value, svc.D.EM_DESIGN.value, svc.D.ALTERACAO_SOLICITADA.value, svc.D.EM_REVISAO.value,
              svc.D.APROVADA.value)
    load = {}
    if cands:
        load = dict((await db.execute(
            select(DesignRequest.assigned_to, func.count()).where(
                DesignRequest.assigned_to.in_([c.user_id for c in cands]), DesignRequest.status.in_(active))
            .group_by(DesignRequest.assigned_to))).all())
    return {"items": [{"user_id": str(c.user_id), "name": c.name, "roles": c.roles,
                       "active_requests": int(load.get(c.user_id, 0))} for c in cands]}


@router.get("/queue")
async def designer_queue(mine: bool = False, status: Optional[str] = None,
                         ctx: AuthContext = Depends(require("design.work", global_only=True)),
                         db: AsyncSession = Depends(get_db)):
    q = await svc.queue(db, ctx, mine=mine, status=status)
    return {"items": [_req_out(r) for r in q["items"]], "counts": q["counts"], "unassigned": q["unassigned"]}


class AssignIn(BaseModel):
    designer_id: Optional[UUID] = None


@router.post("/requests/{request_id}/assign")
async def assign(request_id: UUID, body: AssignIn, ctx: AuthContext = Depends(require("design.work", global_only=True)),
                 db: AsyncSession = Depends(get_db)):
    return _req_out(await svc.assign(db, ctx, request_id, body.designer_id))


# ─── Versões ─────────────────────────────────────────────────────────────────

@router.post("/requests/{request_id}/versions", status_code=201)
async def upload_version(request_id: UUID, file: UploadFile = File(...), notes: Optional[str] = Form(None, max_length=2000),
                         ctx: AuthContext = Depends(require("design.work", global_only=True)),
                         db: AsyncSession = Depends(get_db)):
    r = await svc.get_request(db, ctx, request_id)
    stored = await dfiles.store_upload(file, ("design", r.id, "versions"), allowed={"png", "jpeg", "mp4"},
                                       limits={"IMAGEM": svc.VERSION_LIMITS["IMAGEM"], "VIDEO": svc.VERSION_LIMITS["VIDEO"]})
    dfiles.delete_on_rollback(db, stored.key)
    v = await svc.add_version(db, ctx, r.id, stored, notes=notes)
    return _ver_out(v)


class FromTemplateIn(BaseModel):
    template_id: UUID
    variables: dict = Field(default_factory=dict)
    format: Literal["PNG", "JPEG"] = "PNG"
    notes: Optional[str] = Field(None, max_length=2000)


@router.post("/requests/{request_id}/versions/from-template", status_code=201)
async def version_from_template(request_id: UUID, body: FromTemplateIn,
                                ctx: AuthContext = Depends(require("design.work", global_only=True)),
                                db: AsyncSession = Depends(get_db)):
    """Gera a versão da arte pelo editor de templates (renderização no servidor)."""
    from app.modules.templates import service as tsvc

    r = await svc.get_request(db, ctx, request_id)
    if r.media_type != "IMAGEM":
        raise DomainError("TEMPLATE_IMAGE_ONLY", "Templates geram apenas artes de imagem.", status=400)
    t = await tsvc.get_template(db, body.template_id)
    if t.orientation != r.orientation:
        raise DomainError("TEMPLATE_ORIENTATION_MISMATCH", "Orientação do template difere da arte pedida.", status=400)
    data = await tsvc.render_bytes(db, t, body.variables, body.format)
    fmt = "png" if body.format == "PNG" else "jpeg"
    stored = await dfiles.store_bytes(data, ("design", r.id, "versions"), fmt, f"{r.code}-template")
    dfiles.delete_on_rollback(db, stored.key)
    v = await svc.add_version(db, ctx, r.id, stored, notes=body.notes, source="TEMPLATE", template_id=t.id)
    return _ver_out(v)


class VersionPatch(BaseModel):
    notes: Optional[str] = Field(None, max_length=2000)


@router.patch("/requests/{request_id}/versions/{version_id}")
async def patch_version(request_id: UUID, version_id: UUID, body: VersionPatch,
                        ctx: AuthContext = Depends(require("design.work", global_only=True)),
                        db: AsyncSession = Depends(get_db)):
    """Só observações de versões NÃO aprovadas. Versão aprovada é travada: altere enviando nova versão."""
    r = await svc.get_request(db, ctx, request_id)
    v = await svc.get_version(db, r, version_id)
    if v.locked:
        raise DomainError("DESIGN_VERSION_LOCKED",
                          "Arte aprovada é travada. Envie uma nova versão — ela precisará de nova aprovação.",
                          status=409, details={"version": v.number})
    v.notes = body.notes
    await db.flush()
    return _ver_out(v)


@router.get("/requests/{request_id}/versions/{version_id}/file")
async def download_version(request_id: UUID, version_id: UUID, ctx: AuthContext = Depends(get_auth_context),
                           db: AsyncSession = Depends(get_db)):
    r = await svc.get_request(db, ctx, request_id)
    v = await svc.get_version(db, r, version_id)
    return await _stream(v.storage_key, v.size_bytes, v.content_type, v.filename)


class VersionDecisionIn(BaseModel):
    decision: Literal["APROVAR", "SOLICITAR_ALTERACAO"]
    comment: Optional[str] = Field(None, max_length=5000)


@router.post("/requests/{request_id}/versions/{version_id}/decision")
async def version_decision(request_id: UUID, version_id: UUID, body: VersionDecisionIn,
                           ctx: AuthContext = Depends(require("design.approve")), db: AsyncSession = Depends(get_db)):
    return _req_out(await svc.decide(db, ctx, request_id, version_id, body.decision, body.comment))


class CommentIn(BaseModel):
    body: str = Field(min_length=1, max_length=5000)
    version_id: Optional[UUID] = None
    is_internal: bool = False


@router.post("/requests/{request_id}/reopen")
async def reopen(request_id: UUID, body: CommentIn, ctx: AuthContext = Depends(get_auth_context),
                 db: AsyncSession = Depends(get_db)):
    return _req_out(await svc.reopen(db, ctx, request_id, body.body))


class ExtraIn(BaseModel):
    reason: Optional[str] = Field(None, max_length=1000)


@router.post("/requests/{request_id}/extra-revisions", status_code=201)
async def extra_revision(request_id: UUID, body: ExtraIn, ctx: AuthContext = Depends(get_auth_context),
                         db: AsyncSession = Depends(get_db)):
    x = await svc.request_extra_revision(db, ctx, request_id, body.reason)
    return {"id": str(x.id), "status": x.status, "price_cents": x.price_cents}


class ExtraDecisionIn(BaseModel):
    approve: bool
    price_cents: Optional[int] = Field(None, ge=0)
    note: Optional[str] = Field(None, max_length=1000)


@router.post("/extra-revisions/{extra_id}/decision")
async def extra_decision(extra_id: UUID, body: ExtraDecisionIn,
                         ctx: AuthContext = Depends(require("design.work", global_only=True)),
                         db: AsyncSession = Depends(get_db)):
    x = await svc.decide_extra_revision(db, ctx, extra_id, body.approve, body.price_cents, body.note)
    return {"id": str(x.id), "status": x.status, "price_cents": x.price_cents,
            "payment_order_id": _s(x.payment_order_id)}


# ─── Comentários ─────────────────────────────────────────────────────────────

@router.get("/requests/{request_id}/comments")
async def list_comments(request_id: UUID, ctx: AuthContext = Depends(get_auth_context), db: AsyncSession = Depends(get_db)):
    r = await svc.get_request(db, ctx, request_id)
    return {"items": await _comments_out(db, ctx, await svc.list_comments(db, ctx, r))}


async def _comments_out(db: AsyncSession, ctx: AuthContext, comments: list) -> list[dict]:
    """Autor conforme a regra de ``app.modules.support.people``: cliente vê a equipe como "Equipe RD"."""
    from app.modules.support.people import describe_authors

    viewer_staff = svc.is_staff_viewer(ctx)
    authors = await describe_authors(db, {c.author_id for c in comments}, viewer_is_staff=viewer_staff,
                                     viewer_user_id=ctx.user_id)
    out = []
    for c in comments:
        a = authors.get(c.author_id, {"author_name": "Usuário", "author_is_staff": False})
        hide_id = a["author_is_staff"] and not viewer_staff and c.author_id != ctx.user_id
        out.append({"id": str(c.id), "version_id": _s(c.version_id), "author_id": None if hide_id else str(c.author_id),
                    "author_name": a["author_name"], "author_is_staff": a["author_is_staff"], "body": c.body,
                    "is_internal": c.is_internal, "created_at": _z(c.created_at)})
    return out


@router.post("/requests/{request_id}/comments", status_code=201)
async def add_comment(request_id: UUID, body: CommentIn, ctx: AuthContext = Depends(get_auth_context),
                      db: AsyncSession = Depends(get_db)):
    c = await svc.add_comment(db, ctx, request_id, body.body, body.version_id, body.is_internal)
    return (await _comments_out(db, ctx, [c]))[0]


# ─── Publicação / cancelamento ───────────────────────────────────────────────

@router.post("/requests/{request_id}/publish")
async def publish(request_id: UUID, ctx: AuthContext = Depends(require("design.work", global_only=True)),
                  db: AsyncSession = Depends(get_db)):
    r, media = await svc.publish(db, ctx, request_id)
    out = _req_out(r)
    out["media"] = {"id": str(media.id), "status": getattr(media, "status", None)}
    return out


class CancelIn(BaseModel):
    reason: str = Field(min_length=3, max_length=500)


@router.post("/requests/{request_id}/cancel")
async def cancel(request_id: UUID, body: CancelIn, ctx: AuthContext = Depends(get_auth_context),
                 db: AsyncSession = Depends(get_db)):
    return _req_out(await svc.cancel(db, ctx, request_id, body.reason))
