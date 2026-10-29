"""
RD Mídia Indoor — Conteúdo interno do estabelecimento e fallback (W-INV; #1052–#1064, P001 §34–§35).

Rotas (prefixo ``/api/v1/internal-content``):
- ``GET    ?establishment_id=&screen_id=``  lista ordenada (``playlists.view`` no escopo)
- ``POST   /``                              adiciona peça (RD: ``playlists.manage`` GLOBAL)
- ``PATCH  /{id}``                          ativa/desativa (RD)
- ``DELETE /{id}``                          remove item da lista (RD)
- ``PUT    /order``                         reordena (estabelecimento com ``playlists.reorder`` ou RD)
- ``GET/POST /fallback``, ``DELETE /fallback/{id}``  fallback GLOBAL/ESTABELECIMENTO (RD)
O estabelecimento NÃO envia/cria conteúdo (#736–#741): só reordena quando autorizado.
"""
from __future__ import annotations

import uuid
from typing import Optional

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.v1.endpoints.inventory import jz
from app.core.authz import AuthContext, require
from app.core.database import get_db
from app.core.errors import NotFound
from app.modules.inventory import content as C

router = APIRouter()


async def _est_scope(db: AsyncSession, ctx: AuthContext, perm: str, establishment_id: uuid.UUID):
    from app.models.establishments import Establishment

    est = await db.get(Establishment, establishment_id)
    if est is None or getattr(est, "deleted_at", None) is not None:
        raise NotFound()
    ctx.ensure(perm, org_id=est.org_id, establishment_id=est.id, city=getattr(est, "city_key", None))
    return est


class InternalIn(BaseModel):
    establishment_id: uuid.UUID
    screen_id: Optional[uuid.UUID] = None
    creative_id: uuid.UUID
    position: Optional[int] = Field(None, ge=0, le=100000)


class InternalPatch(BaseModel):
    active: Optional[bool] = None


class OrderIn(BaseModel):
    establishment_id: uuid.UUID
    screen_id: Optional[uuid.UUID] = None
    item_ids: list[uuid.UUID] = Field(max_length=2000)


class FallbackIn(BaseModel):
    scope: str = Field(pattern="^(GLOBAL|ESTABLISHMENT)$")
    establishment_id: Optional[uuid.UUID] = None
    creative_id: uuid.UUID
    purpose: str = Field(pattern="^(ESTABLISHMENT_LOGO|RD_DEFAULT|RD_PROMO|INSTITUTIONAL_MESSAGE)$")
    position: int = Field(0, ge=0, le=100000)


@router.get("")
async def list_internal(establishment_id: uuid.UUID, screen_id: Optional[uuid.UUID] = None,
                        ctx: AuthContext = Depends(require("playlists.view")), db: AsyncSession = Depends(get_db)):
    await _est_scope(db, ctx, "playlists.view", establishment_id)
    items = await C.list_internal(db, establishment_id, screen_id)
    return jz({"items": items, "total": len(items), "page": 1, "page_size": len(items)})


@router.post("", status_code=201)
async def add_internal(body: InternalIn, ctx: AuthContext = Depends(require("playlists.manage", global_only=True)),
                       db: AsyncSession = Depends(get_db)):
    row = await C.add_internal(db, ctx, establishment_id=body.establishment_id, creative_id=body.creative_id,
                               screen_id=body.screen_id, position=body.position)
    return jz(C.internal_dict(row))


@router.put("/order")
async def reorder_internal(body: OrderIn, ctx: AuthContext = Depends(require("playlists.reorder")),
                           db: AsyncSession = Depends(get_db)):
    await _est_scope(db, ctx, "playlists.reorder", body.establishment_id)
    items = await C.reorder_internal(db, ctx, establishment_id=body.establishment_id, item_ids=body.item_ids,
                                     screen_id=body.screen_id)
    return jz({"items": items})


@router.get("/fallback")
async def list_fallback(scope: Optional[str] = Query(None, pattern="^(GLOBAL|ESTABLISHMENT)$"),
                        establishment_id: Optional[uuid.UUID] = None,
                        ctx: AuthContext = Depends(require("playlists.view", global_only=True)),
                        db: AsyncSession = Depends(get_db)):
    return jz({"items": await C.list_fallback(db, scope=scope, establishment_id=establishment_id)})


@router.post("/fallback", status_code=201)
async def add_fallback(body: FallbackIn, ctx: AuthContext = Depends(require("playlists.manage", global_only=True)),
                       db: AsyncSession = Depends(get_db)):
    row = await C.add_fallback(db, ctx, scope=body.scope, creative_id=body.creative_id, purpose=body.purpose,
                               establishment_id=body.establishment_id, position=body.position)
    return jz(C.fallback_dict(row))


@router.delete("/fallback/{item_id}", status_code=204)
async def delete_fallback(item_id: uuid.UUID, ctx: AuthContext = Depends(require("playlists.manage", global_only=True)),
                          db: AsyncSession = Depends(get_db)):
    await C.remove_fallback(db, ctx, item_id)


@router.patch("/{item_id}")
async def patch_internal(item_id: uuid.UUID, body: InternalPatch,
                         ctx: AuthContext = Depends(require("playlists.manage", global_only=True)),
                         db: AsyncSession = Depends(get_db)):
    row = await C.update_internal(db, ctx, item_id, active=body.active)
    return jz(C.internal_dict(row))


@router.delete("/{item_id}", status_code=204)
async def delete_internal(item_id: uuid.UUID, ctx: AuthContext = Depends(require("playlists.manage", global_only=True)),
                          db: AsyncSession = Depends(get_db)):
    await C.remove_internal(db, ctx, item_id)
