"""
RD Mídia Indoor — Editor inicial de templates: CRUD, assets, renderização no servidor e
entrega como mídia (ingestão do W-MEDIA, ``purpose=TEMPLATE_RENDER``).

Permissões: ``templates.manage`` (GLOBAL) cria/edita/renderiza; ``design.work`` também
renderiza (designer usa templates para gerar versões da arte).
"""
from __future__ import annotations

import hashlib
import uuid
from typing import Any, Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.audit import audit
from app.core.authz import AuthContext
from app.core.errors import DomainError, NotFound
from app.models.templates import DesignTemplate, TemplateAsset, TemplateRender
from app.modules.templates import spec as tspec

MAX_ASSET_BYTES = 10 * 1024 * 1024


def can_render(ctx: AuthContext) -> bool:
    return ctx.can("templates.manage", global_only=True) or ctx.can("design.work", global_only=True)


def require_render(ctx: AuthContext) -> None:
    if not can_render(ctx):
        raise DomainError("FORBIDDEN", "Você não tem permissão para esta ação.", status=403,
                          details={"permission": "templates.manage"})


async def get_template(db: AsyncSession, template_id: uuid.UUID, *, for_update: bool = False) -> DesignTemplate:
    q = select(DesignTemplate).where(DesignTemplate.id == template_id)
    if for_update:
        q = q.with_for_update()
    t = (await db.execute(q)).scalar_one_or_none()
    if t is None:
        raise NotFound()
    return t


async def _check_assets(db: AsyncSession, spec: tspec.TemplateSpec) -> None:
    ids = spec.asset_ids()
    if not ids:
        return
    found = set((await db.execute(select(TemplateAsset.id).where(TemplateAsset.id.in_(ids)))).scalars())
    missing = [str(i) for i in ids if i not in found]
    if missing:
        raise DomainError("TEMPLATE_ASSET_NOT_FOUND", "Imagem do template não encontrada.", status=400,
                          details={"asset_ids": missing})


async def create_template(db: AsyncSession, ctx: AuthContext, data: dict) -> DesignTemplate:
    orientation = data["orientation"]
    spec = tspec.validate_spec(data["spec"])
    await _check_assets(db, spec)
    w, h = tspec.CANVAS[orientation]
    t = DesignTemplate(name=data["name"].strip(), description=data.get("description"), orientation=orientation,
                       width=w, height=h, spec=spec.model_dump(mode="json"), status=data.get("status") or "RASCUNHO",
                       org_id=data.get("org_id"), version=1, created_by=ctx.user_id, updated_by=ctx.user_id)
    db.add(t)
    await db.flush()
    await audit(db, ctx, "template.created", "design_template", t.id, after={"name": t.name, "orientation": orientation})
    return t


async def update_template(db: AsyncSession, ctx: AuthContext, template_id: uuid.UUID, data: dict) -> DesignTemplate:
    t = await get_template(db, template_id, for_update=True)
    if data.get("expected_version") is not None and data["expected_version"] != t.version:
        raise DomainError("TEMPLATE_VERSION_CONFLICT", "O template foi alterado por outra pessoa. Recarregue.",
                          status=409, details={"current_version": t.version})
    if t.status == "ARQUIVADO":
        raise DomainError("TEMPLATE_ARCHIVED", "Template arquivado.", status=409)
    before = {"version": t.version, "status": t.status}
    if data.get("spec") is not None:
        spec = tspec.validate_spec(data["spec"])
        await _check_assets(db, spec)
        t.spec = spec.model_dump(mode="json")
    for f in ("name", "description", "status"):
        if data.get(f) is not None:
            setattr(t, f, data[f])
    t.version += 1
    t.updated_by = ctx.user_id
    await db.flush()
    await audit(db, ctx, "template.updated", "design_template", t.id, before=before,
                after={"version": t.version, "status": t.status})
    return t


async def add_asset(db: AsyncSession, ctx: AuthContext, data: bytes, filename: str, org_id: Optional[uuid.UUID]) -> TemplateAsset:
    from PIL import Image

    from app.core.storage import build_key, get_storage
    from app.modules.designer.files import detect, display_name

    if len(data) > MAX_ASSET_BYTES:
        raise DomainError("FILE_TOO_LARGE", "Imagem excede 10 MB.", status=413)
    fmt = detect(data[:4096])
    if fmt not in ("png", "jpeg"):
        raise DomainError("FILE_TYPE_NOT_ALLOWED", "Use imagens PNG ou JPG.", status=415)
    try:
        import io

        with Image.open(io.BytesIO(data)) as im:
            if im.width * im.height > tspec.MAX_ASSET_PIXELS:
                raise DomainError("IMAGE_TOO_LARGE", "Imagem com resolução excessiva.", status=413)
            im.verify()
        with Image.open(io.BytesIO(data)) as im:
            w, h = im.size
    except DomainError:
        raise
    except Exception:
        raise DomainError("FILE_CORRUPTED", "Imagem inválida ou corrompida.", status=400) from None
    ct = "image/png" if fmt == "png" else "image/jpeg"
    obj = await get_storage().put_stream(build_key("templates", "assets", f"{uuid.uuid4().hex}.{'png' if fmt == 'png' else 'jpg'}"),
                                         data, ct, max_bytes=MAX_ASSET_BYTES)
    a = TemplateAsset(org_id=org_id, storage_key=obj.key, filename=display_name(filename, fmt), content_type=ct,
                      width=w, height=h, size_bytes=obj.size, sha256=obj.sha256, uploaded_by=ctx.user_id)
    db.add(a)
    await db.flush()
    return a


async def _load_assets(db: AsyncSession, spec: tspec.TemplateSpec) -> dict:
    from app.core.storage import get_storage

    out = {}
    for aid in spec.asset_ids():
        a = await db.get(TemplateAsset, aid)
        if a is None:
            raise DomainError("TEMPLATE_ASSET_NOT_FOUND", "Imagem do template não encontrada.", status=400,
                              details={"asset_id": str(aid)})
        data = await get_storage().read_bytes(a.storage_key, max_bytes=MAX_ASSET_BYTES)
        if hashlib.sha256(data).hexdigest() != a.sha256:
            raise DomainError("TEMPLATE_ASSET_INTEGRITY", "Imagem do template corrompida no armazenamento.", status=409)
        out[aid] = data
    return out


async def render_bytes(db: AsyncSession, t: DesignTemplate, variables: dict, fmt: str) -> bytes:
    spec = tspec.validate_spec(t.spec)
    values = tspec.resolve_variables(spec, variables or {})
    assets = await _load_assets(db, spec)
    return tspec.render(spec, t.orientation, values, assets, fmt)


async def render_and_store(db: AsyncSession, ctx: AuthContext, template_id: uuid.UUID, *, variables: dict, fmt: str,
                           org_id: Optional[uuid.UUID], deliver_as_media: bool, name: Optional[str]) -> tuple[TemplateRender, Any]:
    from app.core.storage import build_key, get_storage

    require_render(ctx)
    t = await get_template(db, template_id)
    if t.status == "ARQUIVADO":
        raise DomainError("TEMPLATE_ARCHIVED", "Template arquivado.", status=409)
    if t.org_id is not None and org_id is not None and t.org_id != org_id:
        raise NotFound()
    data = await render_bytes(db, t, variables, fmt)
    ext = "png" if fmt == "PNG" else "jpg"
    obj = await get_storage().put_stream(build_key("templates", "renders", t.id, f"{uuid.uuid4().hex}.{ext}"), data,
                                         "image/png" if fmt == "PNG" else "image/jpeg")
    r = TemplateRender(template_id=t.id, template_version=t.version, org_id=org_id, format=fmt,
                       variables=variables or {}, storage_key=obj.key, sha256=obj.sha256, size_bytes=obj.size,
                       width=t.width, height=t.height, created_by=ctx.user_id)
    db.add(r)
    await db.flush()
    media = None
    if deliver_as_media:
        if org_id is None:
            raise DomainError("ORG_REQUIRED", "Informe a organização dona da mídia.", status=400)
        from app.modules.media.service import ingest_generated_file

        media = await ingest_generated_file(db, ctx, org_id, f"{(name or t.name)[:80]}.{ext}",
                                            "image/png" if fmt == "PNG" else "image/jpeg", data, "TEMPLATE_RENDER",
                                            name=name or t.name)
        r.media_id = media.id
    await audit(db, ctx, "template.rendered", "design_template", t.id,
                after={"render_id": r.id, "format": fmt, "sha256": r.sha256, "media_id": r.media_id}, org_id=org_id)
    return r, media
