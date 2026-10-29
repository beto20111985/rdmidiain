"""
RD Mídia Indoor — API do scheduler: manifestos (metadados/regeneração), grade e emergência (W-INV).

Rotas (prefixo ``/api/v1/scheduler``) — TODAS autenticadas:
- ``GET  /screens/{id}/grid?hours=``          pré-visualização da grade (RD: ``inventory.view`` GLOBAL)
- ``GET  /screens/{id}/programming?hours=``   programação SEGURA p/ o estabelecimento (``screens.view`` ou
                                              ``playlists.view`` no escopo da tela): entradas só ``{code, kind}``
- ``GET  /screens/{id}/manifests``            versões (metadados) — ``screens.view`` no escopo
- ``GET  /screens/{id}/manifests/{version}``  manifesto assinado de uma versão (RD: ``screens.manage`` GLOBAL)
- ``POST /screens/{id}/manifests/regenerate`` regeneração sob demanda (RD: ``screens.manage`` GLOBAL)
- ``GET  /public-keys``                       chaves públicas de manifesto (``screens.view``)
- ``GET/POST /emergencies``, ``POST /emergencies/{id}/cancel``  comunicado urgente (``campaigns.kill`` GLOBAL)

O player busca o manifesto em ``GET /api/v1/player/manifest`` (W-PLAY, autenticação de
dispositivo), que usa ``inventory.service.current_manifest``.

Removidas: ``GET /scheduler/manifest/{screen_id}`` (SEM autenticação; 500 — A6 E20/R01) e
``POST /scheduler/manifest/{screen_id}/generate`` (colunas inexistentes; gerador paralelo).
"""
from __future__ import annotations

import uuid
from datetime import datetime
from typing import Optional

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.v1.endpoints.inventory import _screen_scope, jz
from app.core.authz import AuthContext, get_auth_context, require
from app.core.database import get_db
from app.core.errors import Forbidden, NotFound
from app.models.manifests import ManifestVersion
from app.modules.inventory import content as C
from app.modules.inventory import service as inv

router = APIRouter()


@router.get("/screens/{screen_id}/grid")
async def screen_grid(screen_id: uuid.UUID, hours: int = Query(6, ge=1, le=48),
                      ctx: AuthContext = Depends(require("inventory.view", global_only=True)),
                      db: AsyncSession = Depends(get_db)):
    await _screen_scope(db, ctx, "inventory.view", screen_id)
    return jz(await inv.preview_grid(db, screen_id, hours=hours))


@router.get("/screens/{screen_id}/programming")
async def screen_programming(screen_id: uuid.UUID, hours: int = Query(6, ge=1, le=48),
                             ctx: AuthContext = Depends(get_auth_context), db: AsyncSession = Depends(get_db)):
    """Programação prevista (estabelecimento/RD): ``screens.view`` OU ``playlists.view`` no escopo da tela.
    Entradas só com ``{code, kind}`` — sem dados do anunciante."""
    perms = ("screens.view", "playlists.view")
    if not any(ctx.has_any(p) for p in perms):
        raise Forbidden(details={"permission": " | ".join(perms)})
    info = (await inv.S.load_screens(db, [screen_id])).get(screen_id)
    if info is None or not any(
            ctx.can(p, org_id=info.org_id, establishment_id=info.establishment_id, city=info.city_key) for p in perms):
        raise NotFound()
    return jz(await inv.programming_view(db, screen_id, hours=hours))


def _mv(r: ManifestVersion) -> dict:
    return {"screen_id": str(r.screen_id), "version": int(r.version), "manifest_id": str(r.manifest_id),
            "generated_at": r.generated_at, "reason": r.reason, "content_sha256": r.content_sha256,
            "plan_sha256": r.plan_sha256, "size_bytes": r.size_bytes, "key_id": r.key_id,
            "horizon_from": r.horizon_from, "horizon_to": r.horizon_to, "unplaced_count": r.unplaced_count,
            "applied_at": r.applied_at, "applied_by_device": str(r.applied_by_device) if r.applied_by_device else None,
            "content_available": r.content_gz is not None}


@router.get("/screens/{screen_id}/manifests")
async def list_manifests(screen_id: uuid.UUID, page: int = Query(1, ge=1), page_size: int = Query(25, ge=1, le=100),
                         ctx: AuthContext = Depends(require("screens.view")), db: AsyncSession = Depends(get_db)):
    await _screen_scope(db, ctx, "screens.view", screen_id)
    q = select(ManifestVersion).where(ManifestVersion.screen_id == screen_id)
    total = (await db.execute(select(func.count()).select_from(q.subquery()))).scalar_one()
    rows = (await db.execute(q.order_by(ManifestVersion.version.desc()).offset((page - 1) * page_size)
                             .limit(page_size))).scalars()
    return jz({"items": [_mv(r) for r in rows], "total": total, "page": page, "page_size": page_size})


@router.get("/screens/{screen_id}/manifests/{version}")
async def get_manifest(screen_id: uuid.UUID, version: int,
                       ctx: AuthContext = Depends(require("screens.manage", global_only=True)),
                       db: AsyncSession = Depends(get_db)):
    await _screen_scope(db, ctx, "screens.manage", screen_id)
    row = await db.get(ManifestVersion, (screen_id, version))
    if row is None:
        raise NotFound()
    m = await inv.get_manifest_version(db, screen_id, version)
    return jz({**_mv(row), "envelope": {"alg": "ES256", "key_id": row.key_id, "signature": row.signature,
                                        "manifest": m} if m is not None else None})


class RegenerateIn(BaseModel):
    force: bool = False


@router.post("/screens/{screen_id}/manifests/regenerate")
async def regenerate(screen_id: uuid.UUID, body: RegenerateIn,
                     ctx: AuthContext = Depends(require("screens.manage", global_only=True)),
                     db: AsyncSession = Depends(get_db)):
    await _screen_scope(db, ctx, "screens.manage", screen_id)
    if body.force:
        sm = await inv.generate_manifest(db, screen_id, reason="admin_force", force=True)
        from app.modules.inventory.manifests import _defer_sync

        if sm.created:
            _defer_sync(db, info=(await inv.S.load_screens(db, [screen_id])).get(screen_id), sm=sm,
                        reason="admin_force")
        version = sm.version
    else:
        version = (await inv.invalidate_screens(db, [screen_id], "admin_on_demand")).get(screen_id)
    from app.core.audit import audit

    await audit(db, ctx, "manifest.regenerate_requested", "screen", screen_id, after={"version": version,
                                                                                       "force": body.force})
    return {"screen_id": str(screen_id), "version": version}


@router.get("/public-keys")
async def public_keys(ctx: AuthContext = Depends(require("screens.view"))):
    return {"keys": inv.server_public_keys()}


# ─── Emergência ──────────────────────────────────────────────────────────────

class EmergencyIn(BaseModel):
    title: str = Field(min_length=3, max_length=200)
    creative_id: uuid.UUID
    starts_at: datetime
    ends_at: datetime
    target_kind: str = Field(pattern="^(ALL|SCREENS|ESTABLISHMENTS|STATES|CITIES)$")
    target_ids: list[str] = Field(default_factory=list, max_length=10000)
    reason: str = Field(min_length=10, max_length=2000)


class CancelIn(BaseModel):
    reason: str = Field(min_length=10, max_length=2000)


@router.get("/emergencies")
async def list_emergencies(active: bool = False,
                           ctx: AuthContext = Depends(require("campaigns.kill", global_only=True)),
                           db: AsyncSession = Depends(get_db)):
    return jz({"items": await C.list_emergencies(db, active_only=active)})


@router.post("/emergencies", status_code=201)
async def create_emergency(body: EmergencyIn,
                           ctx: AuthContext = Depends(require("campaigns.kill", global_only=True,
                                                              allow_impersonation=False)),
                           db: AsyncSession = Depends(get_db)):
    e, n = await C.create_emergency(db, ctx, title=body.title, creative_id=body.creative_id, starts_at=body.starts_at,
                                    ends_at=body.ends_at, target_kind=body.target_kind, target_ids=body.target_ids,
                                    reason=body.reason)
    return jz({**C.emergency_dict(e), "screens": n})


@router.post("/emergencies/{emergency_id}/cancel")
async def cancel_emergency(emergency_id: uuid.UUID, body: CancelIn,
                           ctx: AuthContext = Depends(require("campaigns.kill", global_only=True,
                                                              allow_impersonation=False)),
                           db: AsyncSession = Depends(get_db)):
    e = await C.cancel_emergency(db, ctx, emergency_id, body.reason)
    return jz(C.emergency_dict(e))
