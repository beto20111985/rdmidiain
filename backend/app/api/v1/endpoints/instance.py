"""RD Mídia Indoor — Instância / white-label (AÇÃO 002 / W-BIZ). Prefixo ``/api/v1/instance``.

- ``GET  /branding`` (público): nome, cores e logo da instância (tela de login/PDFs).
- ``GET  /logo`` (público): imagem da logo.
- ``GET|PATCH /profile`` (``whitelabel.manage`` GLOBAL): marca, subdomínio (futuro), contato,
  REFERÊNCIA ao segredo CloudPayPag da instância (valor nunca exposto), isolamento.
- ``PUT  /logo`` (``whitelabel.manage``): PNG/JPG até 1 MB.
- ``GET  /price-floors`` (``pricing.view`` ou ``whitelabel.manage``), ``PUT /price-floors/{key}``
  (somente Admin Master), ``POST /price-floors/check`` — piso de preço RD.
- ``GET  /price-floors/{key}/history`` (``whitelabel.manage`` ou ``audit.view``).
- ``GET  /signing-keys`` (público): chaves públicas ES256 para verificar comprovantes.
"""
from __future__ import annotations

from datetime import datetime
from typing import Optional

from fastapi import APIRouter, Depends, File, Query, Request, UploadFile
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel, EmailStr, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.audit import audit
from app.core.authz import AuthContext, get_auth_context, require
from app.core.database import get_db
from app.core.errors import DomainError, NotFound
from app.core.timeutils import isoformat_z
from app.models.instance import RdPriceFloor
from app.modules.instance import service as svc

router = APIRouter()
WL = require("whitelabel.manage", global_only=True, allow_impersonation=False)
MAX_LOGO = 1024 * 1024


def _z(dt: Optional[datetime]) -> Optional[str]:
    return isoformat_z(dt) if dt else None


@router.get("/branding")
async def branding(request: Request, db: AsyncSession = Depends(get_db)):
    """Público e SEGURO: só nome, cores, logo e contato de suporte (lista fechada em ``profile_public``) —
    nunca referência de segredo, documento, subdomínio ou configuração interna. ETag + revalidação."""
    p = await svc.get_profile(db)
    out = svc.profile_public(p)
    out["logo_url"] = (f"/api/v1/instance/logo?v={p.logo_sha256[:12]}" if out["has_logo"] and p is not None
                       and p.logo_sha256 else ("/api/v1/instance/logo" if out["has_logo"] else None))
    etag = svc.branding_etag(out)
    headers = {"ETag": etag, "Cache-Control": "public, max-age=60, must-revalidate", "Vary": "Accept-Encoding"}
    if _etag_match(request, etag):
        return Response(status_code=304, headers=headers)
    return JSONResponse(out, headers=headers)


def _etag_match(request: Request, etag: str) -> bool:
    inm = request.headers.get("if-none-match") or ""
    return any(t.strip() in (etag, "*", "W/" + etag) for t in inm.split(",")) if inm else False


@router.get("/logo")
async def logo(request: Request, db: AsyncSession = Depends(get_db)):
    from app.core.storage import get_storage

    p = await svc.get_profile(db)
    if p is None or not p.logo_key:
        raise NotFound()
    etag = f'"{p.logo_sha256}"'
    headers = {"Cache-Control": "public, max-age=300", "X-Content-Type-Options": "nosniff", "ETag": etag}
    if _etag_match(request, etag):
        return Response(status_code=304, headers=headers)
    data = await get_storage().read_bytes(p.logo_key, max_bytes=MAX_LOGO)
    return Response(content=data, media_type=p.logo_content_type or "image/png", headers=headers)


@router.get("/profile")
async def get_profile(ctx: AuthContext = Depends(require("whitelabel.manage", global_only=True)),
                      db: AsyncSession = Depends(get_db)):
    return svc.profile_admin(await svc.get_profile(db))


class ProfilePatch(BaseModel):
    instance_slug: Optional[str] = Field(None, max_length=60)
    display_name: Optional[str] = Field(None, min_length=2, max_length=120)
    legal_name: Optional[str] = Field(None, max_length=255)
    document: Optional[str] = Field(None, max_length=20)
    is_white_label: Optional[bool] = None
    support_email: Optional[EmailStr] = None
    support_phone: Optional[str] = Field(None, max_length=30)
    primary_color: Optional[str] = Field(None, max_length=7)
    accent_color: Optional[str] = Field(None, max_length=7)
    subdomain: Optional[str] = Field(None, max_length=63)
    payment_credentials_ref: Optional[str] = Field(None, max_length=255)
    payment_account_label: Optional[str] = Field(None, max_length=120)
    expected_version: Optional[int] = None


@router.patch("/profile")
async def patch_profile(body: ProfilePatch, ctx: AuthContext = Depends(WL), db: AsyncSession = Depends(get_db)):
    changes = body.model_dump(exclude_unset=True)
    expected = changes.pop("expected_version", None)
    if "support_email" in changes and changes["support_email"] is not None:
        changes["support_email"] = str(changes["support_email"])
    p = await svc.update_profile(db, ctx, changes, expected_version=expected)
    return svc.profile_admin(p)


@router.put("/logo")
async def put_logo(file: UploadFile = File(...), ctx: AuthContext = Depends(WL), db: AsyncSession = Depends(get_db)):
    import io
    import uuid as _uuid

    from PIL import Image

    from app.core.storage import build_key, get_storage
    from app.modules.designer.files import detect

    data = await file.read(MAX_LOGO + 1)
    if len(data) > MAX_LOGO:
        raise DomainError("FILE_TOO_LARGE", "Logo excede 1 MB.", status=413)
    fmt = detect(data[:4096])
    if fmt not in ("png", "jpeg"):
        raise DomainError("FILE_TYPE_NOT_ALLOWED", "Use PNG ou JPG (SVG não é aceito).", status=415)
    try:
        with Image.open(io.BytesIO(data)) as im:
            im.verify()
    except Exception:
        raise DomainError("FILE_CORRUPTED", "Imagem inválida.", status=400) from None
    ct = "image/png" if fmt == "png" else "image/jpeg"
    obj = await get_storage().put_stream(build_key("instance", "logo", f"{_uuid.uuid4().hex}.{'png' if fmt == 'png' else 'jpg'}"),
                                         data, ct, max_bytes=MAX_LOGO)
    p = await svc.get_or_create_profile(db)
    old = p.logo_key
    p.logo_key, p.logo_content_type, p.logo_sha256 = obj.key, ct, obj.sha256
    p.version += 1
    await audit(db, ctx, "instance.logo_updated", "instance_profile", "1", before={"logo_key": old},
                after={"sha256": obj.sha256})
    return svc.profile_admin(p)


# ─── Pisos de preço RD ───────────────────────────────────────────────────────

def _floor(f: RdPriceFloor) -> dict:
    return {"key": f.key, "label": f.label, "unit": f.unit, "min_price_cents": f.min_price_cents, "note": f.note,
            "version": f.version, "updated_at": _z(f.updated_at)}


@router.get("/price-floors")
async def list_floors(ctx: AuthContext = Depends(get_auth_context), db: AsyncSession = Depends(get_db)):
    if not (ctx.can("pricing.view", global_only=True) or ctx.can("whitelabel.manage", global_only=True)):
        raise DomainError("FORBIDDEN", "Você não tem permissão para esta ação.", status=403,
                          details={"permission": "pricing.view"})
    rows = (await db.execute(select(RdPriceFloor).order_by(RdPriceFloor.key))).scalars()
    return {"items": [_floor(f) for f in rows]}


class FloorIn(BaseModel):
    label: str = Field(min_length=2, max_length=200)
    unit: str = Field(min_length=1, max_length=80)
    min_price_cents: int = Field(ge=0)
    note: Optional[str] = Field(None, max_length=500)
    reason: str = Field(min_length=5, max_length=500)
    expected_version: Optional[int] = None


@router.put("/price-floors/{floor_key}")
async def set_floor(floor_key: str, body: FloorIn, ctx: AuthContext = Depends(WL), db: AsyncSession = Depends(get_db)):
    # o piso é a tabela BASE da RD: só os Admin Master da RD alteram (a instância white-label não reduz o piso)
    if not ctx.is_admin_master:
        raise DomainError("ADMIN_MASTER_REQUIRED", "Somente os Admin Master da RD alteram o piso de preços.", status=403)
    f = await svc.set_floor(db, ctx, floor_key, min_price_cents=body.min_price_cents, label=body.label, unit=body.unit,
                            note=body.note, reason=body.reason, expected_version=body.expected_version)
    return _floor(f)


@router.get("/price-floors/{floor_key}/history")
async def floor_history(floor_key: str, page: int = Query(1, ge=1), page_size: int = Query(25, ge=1, le=100),
                        ctx: AuthContext = Depends(get_auth_context), db: AsyncSession = Depends(get_db)):
    """Histórico (append-only) do piso RD. Permissão: ``whitelabel.manage`` ou ``audit.view`` (GLOBAL)."""
    from sqlalchemy import func

    from app.models.instance import RdPriceFloorHistory as H
    from app.models.users import User

    if not (ctx.can("whitelabel.manage", global_only=True) or ctx.can("audit.view", global_only=True)):
        raise DomainError("FORBIDDEN", "Você não tem permissão para esta ação.", status=403,
                          details={"permission": "whitelabel.manage"})
    if await svc.get_floor(db, floor_key) is None and not (
            await db.execute(select(H.id).where(H.key == floor_key).limit(1))).first():
        raise NotFound()
    total = (await db.execute(select(func.count()).select_from(H).where(H.key == floor_key))).scalar_one()
    rows = (await db.execute(
        select(H, User.full_name).outerjoin(User, User.id == H.changed_by).where(H.key == floor_key)
        .order_by(H.version.desc(), H.created_at.desc()).offset((page - 1) * page_size).limit(page_size))).all()
    return {"items": [{"id": str(h.id), "key": h.key, "version": h.version,
                       "old_min_price_cents": h.old_min_price_cents, "new_min_price_cents": h.new_min_price_cents,
                       "changed_by": str(h.changed_by) if h.changed_by else None, "changed_by_name": name,
                       "reason": h.reason, "created_at": _z(h.created_at)} for h, name in rows],
            "total": total, "page": page, "page_size": page_size}


class FloorCheckIn(BaseModel):
    floor_key: str = Field(min_length=2, max_length=80)
    price_cents: int = Field(ge=0)


@router.post("/price-floors/check")
async def check_floor(body: FloorCheckIn, ctx: AuthContext = Depends(require("pricing.view")),
                      db: AsyncSession = Depends(get_db)):
    await svc.assert_price_not_below_floor(db, body.floor_key, body.price_cents)
    f = await svc.get_floor(db, body.floor_key)
    return {"ok": True, "floor_key": body.floor_key, "min_price_cents": f.min_price_cents if f else None}


@router.get("/signing-keys")
async def signing_keys(db: AsyncSession = Depends(get_db)):
    keys = await svc.public_keys(db)
    return {"items": [{**k, "created_at": _z(k["created_at"]), "retired_at": _z(k["retired_at"])} for k in keys],
            "note": "Assinatura ECDSA P-256/SHA-256 (ES256) da instância. Não é certificado ICP-Brasil."}
