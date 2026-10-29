"""RD Mídia Indoor — Editor inicial de templates (AÇÃO 002 / W-BIZ). Prefixo ``/api/v1/templates``.

Spec JSON segura (grade 24×24, fontes permitidas, texto/imagem/retângulo) e renderização
SOMENTE no servidor (Pillow) para PNG/JPG 1920×1080 ou 1080×1920; entrega como mídia pela
ingestão do W-MEDIA.
"""
from __future__ import annotations

from datetime import datetime
from typing import Literal, Optional
from uuid import UUID

from fastapi import APIRouter, Depends, File, Form, UploadFile
from fastapi.responses import Response, StreamingResponse
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.authz import AuthContext, get_auth_context, require
from app.core.database import get_db
from app.core.errors import NotFound
from app.core.timeutils import isoformat_z
from app.models.templates import DesignTemplate, TemplateAsset
from app.modules.templates import service as svc
from app.modules.templates import spec as tspec

router = APIRouter()
MANAGE = require("templates.manage", global_only=True)


def _z(dt: Optional[datetime]) -> Optional[str]:
    return isoformat_z(dt) if dt else None


def _out(t: DesignTemplate) -> dict:
    return {"id": str(t.id), "name": t.name, "description": t.description, "orientation": t.orientation,
            "width": t.width, "height": t.height, "spec": t.spec, "status": t.status, "version": t.version,
            "org_id": str(t.org_id) if t.org_id else None, "created_at": _z(t.created_at), "updated_at": _z(t.updated_at)}


@router.get("/meta")
async def meta(ctx: AuthContext = Depends(get_auth_context)):
    svc.require_render(ctx)
    return {"grid": tspec.GRID, "canvases": {k: {"width": w, "height": h} for k, (w, h) in tspec.CANVAS.items()},
            "fonts": sorted(tspec.ALLOWED_FONTS), "layer_types": ["text", "image", "rect"],
            "limits": {"layers": 30, "variables": 20, "font_px": [tspec.MIN_FONT_PX, 400], "asset_mb": 10}}


@router.get("")
async def list_templates(status: Optional[str] = None, orientation: Optional[str] = None,
                         ctx: AuthContext = Depends(get_auth_context), db: AsyncSession = Depends(get_db)):
    svc.require_render(ctx)
    q = select(DesignTemplate).order_by(DesignTemplate.updated_at.desc())
    if status:
        q = q.where(DesignTemplate.status == status)
    if orientation:
        q = q.where(DesignTemplate.orientation == orientation)
    rows = list((await db.execute(q)).scalars())
    return {"items": [_out(t) for t in rows], "total": len(rows), "page": 1, "page_size": len(rows)}


class TemplateIn(BaseModel):
    name: str = Field(min_length=2, max_length=120)
    description: Optional[str] = Field(None, max_length=2000)
    orientation: Literal["HORIZONTAL", "VERTICAL"]
    spec: dict
    status: Literal["RASCUNHO", "ATIVO"] = "RASCUNHO"
    org_id: Optional[UUID] = None


class TemplatePut(BaseModel):
    name: Optional[str] = Field(None, min_length=2, max_length=120)
    description: Optional[str] = Field(None, max_length=2000)
    spec: Optional[dict] = None
    status: Optional[Literal["RASCUNHO", "ATIVO", "ARQUIVADO"]] = None
    expected_version: Optional[int] = None


@router.post("", status_code=201)
async def create(body: TemplateIn, ctx: AuthContext = Depends(MANAGE), db: AsyncSession = Depends(get_db)):
    return _out(await svc.create_template(db, ctx, body.model_dump()))


@router.get("/{template_id}")
async def get_one(template_id: UUID, ctx: AuthContext = Depends(get_auth_context), db: AsyncSession = Depends(get_db)):
    svc.require_render(ctx)
    return _out(await svc.get_template(db, template_id))


@router.put("/{template_id}")
async def update(template_id: UUID, body: TemplatePut, ctx: AuthContext = Depends(MANAGE),
                 db: AsyncSession = Depends(get_db)):
    return _out(await svc.update_template(db, ctx, template_id, body.model_dump(exclude_unset=True)))


@router.post("/assets", status_code=201)
async def upload_asset(file: UploadFile = File(...), org_id: Optional[UUID] = Form(None),
                       ctx: AuthContext = Depends(MANAGE), db: AsyncSession = Depends(get_db)):
    data = await file.read(svc.MAX_ASSET_BYTES + 1)
    a = await svc.add_asset(db, ctx, data, file.filename or "imagem", org_id)
    return {"id": str(a.id), "filename": a.filename, "content_type": a.content_type, "width": a.width,
            "height": a.height, "size_bytes": a.size_bytes, "sha256": a.sha256}


@router.get("/assets/{asset_id}/file")
async def asset_file(asset_id: UUID, ctx: AuthContext = Depends(get_auth_context), db: AsyncSession = Depends(get_db)):
    from app.core.storage import get_storage

    svc.require_render(ctx)
    a = await db.get(TemplateAsset, asset_id)
    if a is None:
        raise NotFound()
    body = await get_storage().open_range(a.storage_key, 0, a.size_bytes - 1)
    return StreamingResponse(body, media_type=a.content_type, headers={"X-Content-Type-Options": "nosniff",
                                                                        "Cache-Control": "private, max-age=300"})


class RenderIn(BaseModel):
    variables: dict = Field(default_factory=dict)
    format: Literal["PNG", "JPEG"] = "PNG"
    org_id: Optional[UUID] = None
    deliver_as_media: bool = False
    name: Optional[str] = Field(None, max_length=120)


@router.post("/{template_id}/preview")
async def preview(template_id: UUID, body: RenderIn, ctx: AuthContext = Depends(get_auth_context),
                  db: AsyncSession = Depends(get_db)):
    """Pré-visualização renderizada no servidor (não grava)."""
    svc.require_render(ctx)
    t = await svc.get_template(db, template_id)
    data = await svc.render_bytes(db, t, body.variables, body.format)
    return Response(content=data, media_type="image/png" if body.format == "PNG" else "image/jpeg",
                    headers={"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff"})


@router.post("/{template_id}/render", status_code=201)
async def render(template_id: UUID, body: RenderIn, ctx: AuthContext = Depends(get_auth_context),
                 db: AsyncSession = Depends(get_db)):
    r, media = await svc.render_and_store(db, ctx, template_id, variables=body.variables, fmt=body.format,
                                          org_id=body.org_id, deliver_as_media=body.deliver_as_media, name=body.name)
    return {"id": str(r.id), "template_id": str(r.template_id), "template_version": r.template_version,
            "format": r.format, "width": r.width, "height": r.height, "size_bytes": r.size_bytes, "sha256": r.sha256,
            "media_id": str(r.media_id) if r.media_id else None,
            "media_status": getattr(media, "status", None) if media is not None else None}


@router.get("/renders/{render_id}/file")
async def render_file(render_id: UUID, ctx: AuthContext = Depends(get_auth_context), db: AsyncSession = Depends(get_db)):
    from app.core.storage import get_storage
    from app.models.templates import TemplateRender

    svc.require_render(ctx)
    r = await db.get(TemplateRender, render_id)
    if r is None:
        raise NotFound()
    body = await get_storage().open_range(r.storage_key, 0, r.size_bytes - 1)
    return StreamingResponse(body, media_type="image/png" if r.format == "PNG" else "image/jpeg",
                             headers={"X-Content-Type-Options": "nosniff", "Cache-Control": "private, no-store"})
