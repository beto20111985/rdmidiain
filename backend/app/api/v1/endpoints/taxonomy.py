"""
RD Mídia Indoor — Taxonomia comercial (W-ORG; ETAPA2 #100–#126; P001 §41–§44).

Leitura (qualquer perfil que cadastra estabelecimento, organização, campanha ou criativo):
``establishments.view | organizations.view | campaigns.view | creatives.view | taxonomy.manage``.
Escrita: ``taxonomy.manage`` em escopo GLOBAL (somente RD). Toda alteração é auditada.

- ``/taxonomy/nodes``     SETOR > CATEGORIA > SUBCATEGORIA (+ ``/{id}/ancestors|descendants``)
- ``/taxonomy/segments``  árvore de segmentos/produtos (+ ``/{id}/policy`` efetiva)
- ``/taxonomy/brands``, ``/taxonomy/products``
- ``/taxonomy/content-policies`` (BLOCK/ALLOW por segmento; defaults semeados)
- ``/taxonomy/competitor-links`` (concorrência explícita entre organizações)
"""
from __future__ import annotations

import uuid
from typing import Optional

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.audit import audit
from app.core.authz import AuthContext, require
from app.core.database import get_db
from app.core.errors import Conflict, DomainError, NotFound
from app.models.organizations import Organization
from app.models.taxonomy import Brand, CompetitorLink, ContentPolicy, Product, Segment, TaxonomyNode
from app.modules.organizations.common import iso, paginate, require_any
from app.modules.taxonomy import service as svc

router = APIRouter()

READ = require_any("establishments.view", "organizations.view", "campaigns.view", "creatives.view", "taxonomy.manage")
WRITE = require("taxonomy.manage", global_only=True)


class NodeIn(BaseModel):
    kind: str = Field(pattern="^(SECTOR|CATEGORY|SUBCATEGORY)$")
    name: str = Field(min_length=2, max_length=120)
    parent_id: Optional[uuid.UUID] = None
    slug: Optional[str] = Field(default=None, max_length=140)


class SegmentIn(BaseModel):
    name: str = Field(min_length=2, max_length=120)
    parent_id: Optional[uuid.UUID] = None
    slug: Optional[str] = Field(default=None, max_length=140)


class TreePatch(BaseModel):
    name: Optional[str] = Field(default=None, min_length=2, max_length=120)
    is_active: Optional[bool] = None
    parent_id: Optional[uuid.UUID] = None
    move_to_root: bool = False


class BrandIn(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    owner_org_id: Optional[uuid.UUID] = None


class BrandPatch(BaseModel):
    name: Optional[str] = Field(default=None, min_length=1, max_length=120)
    owner_org_id: Optional[uuid.UUID] = None
    is_active: Optional[bool] = None


class ProductIn(BaseModel):
    name: str = Field(min_length=1, max_length=160)
    segment_id: uuid.UUID
    brand_id: Optional[uuid.UUID] = None


class ProductPatch(BaseModel):
    name: Optional[str] = Field(default=None, min_length=1, max_length=160)
    segment_id: Optional[uuid.UUID] = None
    brand_id: Optional[uuid.UUID] = None
    is_active: Optional[bool] = None


class PolicyIn(BaseModel):
    effect: str = Field(pattern="^(BLOCK|ALLOW)$")
    reason: str = Field(min_length=10, max_length=2000)


class ReasonIn(BaseModel):
    reason: str = Field(min_length=10, max_length=2000)


class CompetitorIn(BaseModel):
    org_a_id: uuid.UUID
    org_b_id: uuid.UUID
    reason: str = Field(min_length=10, max_length=2000)


# ─── Nós (setor/categoria/subcategoria) ──────────────────────────────────────

@router.get("/nodes")
async def list_nodes(kind: Optional[str] = Query(None, pattern="^(SECTOR|CATEGORY|SUBCATEGORY)$"),
                     parent_id: Optional[uuid.UUID] = None, include_inactive: bool = False,
                     ctx: AuthContext = Depends(READ), db: AsyncSession = Depends(get_db)):
    q = select(TaxonomyNode)
    if kind:
        q = q.where(TaxonomyNode.kind == kind)
    if parent_id:
        q = q.where(TaxonomyNode.parent_id == parent_id)
    if not include_inactive:
        q = q.where(TaxonomyNode.is_active.is_(True))
    rows = (await db.execute(q.order_by(TaxonomyNode.depth, TaxonomyNode.name))).scalars().all()
    return {"items": [svc.tree_out(n) for n in rows], "total": len(rows), "page": 1, "page_size": max(len(rows), 1)}


@router.post("/nodes", status_code=201)
async def create_node(body: NodeIn, ctx: AuthContext = Depends(WRITE), db: AsyncSession = Depends(get_db)):
    n = await svc.create_tree_item(db, ctx, TaxonomyNode, name=body.name, parent_id=body.parent_id, slug=body.slug,
                                   kind=body.kind)
    await db.commit()
    return svc.tree_out(n)


async def _patch_tree(db, ctx, model, item_id, body: TreePatch):
    parent = ... if (body.parent_id is None and not body.move_to_root) else body.parent_id
    obj = await svc.update_tree_item(db, ctx, model, item_id, name=body.name, is_active=body.is_active,
                                     parent_id=parent)
    await db.commit()
    await db.refresh(obj)
    return svc.tree_out(obj)


@router.patch("/nodes/{node_id}")
async def patch_node(node_id: uuid.UUID, body: TreePatch, ctx: AuthContext = Depends(WRITE),
                     db: AsyncSession = Depends(get_db)):
    return await _patch_tree(db, ctx, TaxonomyNode, node_id, body)


async def _related(db, model, item_id, which: str):
    obj = await db.get(model, item_id)
    if obj is None:
        raise NotFound()
    ids = await (svc.ancestor_ids if which == "ancestors" else svc.descendant_ids)(db, model, [obj.id])
    rows = (await db.execute(select(model).where(model.id.in_(ids)).order_by(model.depth, model.name))).scalars().all()
    return {"items": [svc.tree_out(r) for r in rows], "total": len(rows), "page": 1, "page_size": max(len(rows), 1)}


@router.get("/nodes/{node_id}/ancestors")
async def node_ancestors(node_id: uuid.UUID, ctx: AuthContext = Depends(READ), db: AsyncSession = Depends(get_db)):
    return await _related(db, TaxonomyNode, node_id, "ancestors")


@router.get("/nodes/{node_id}/descendants")
async def node_descendants(node_id: uuid.UUID, ctx: AuthContext = Depends(READ), db: AsyncSession = Depends(get_db)):
    return await _related(db, TaxonomyNode, node_id, "descendants")


# ─── Segmentos ───────────────────────────────────────────────────────────────

@router.get("/segments")
async def list_segments(parent_id: Optional[uuid.UUID] = None, roots_only: bool = False, include_inactive: bool = False,
                        ctx: AuthContext = Depends(READ), db: AsyncSession = Depends(get_db)):
    q = select(Segment)
    if parent_id:
        q = q.where(Segment.parent_id == parent_id)
    elif roots_only:
        q = q.where(Segment.parent_id.is_(None))
    if not include_inactive:
        q = q.where(Segment.is_active.is_(True))
    rows = (await db.execute(q.order_by(Segment.depth, Segment.name))).scalars().all()
    pols = {p.segment_id: p.effect for p in (await db.execute(select(ContentPolicy))).scalars()}
    items = []
    for s in rows:
        d = svc.tree_out(s)
        d["policy"] = pols.get(s.id)
        items.append(d)
    return {"items": items, "total": len(items), "page": 1, "page_size": max(len(items), 1)}


@router.post("/segments", status_code=201)
async def create_segment(body: SegmentIn, ctx: AuthContext = Depends(WRITE), db: AsyncSession = Depends(get_db)):
    s = await svc.create_tree_item(db, ctx, Segment, name=body.name, parent_id=body.parent_id, slug=body.slug)
    await db.commit()
    return svc.tree_out(s)


@router.patch("/segments/{segment_id}")
async def patch_segment(segment_id: uuid.UUID, body: TreePatch, ctx: AuthContext = Depends(WRITE),
                        db: AsyncSession = Depends(get_db)):
    return await _patch_tree(db, ctx, Segment, segment_id, body)


@router.get("/segments/{segment_id}/ancestors")
async def segment_ancestors(segment_id: uuid.UUID, ctx: AuthContext = Depends(READ),
                            db: AsyncSession = Depends(get_db)):
    return await _related(db, Segment, segment_id, "ancestors")


@router.get("/segments/{segment_id}/descendants")
async def segment_descendants(segment_id: uuid.UUID, ctx: AuthContext = Depends(READ),
                              db: AsyncSession = Depends(get_db)):
    return await _related(db, Segment, segment_id, "descendants")


@router.get("/segments/{segment_id}/policy")
async def segment_policy(segment_id: uuid.UUID, ctx: AuthContext = Depends(READ), db: AsyncSession = Depends(get_db)):
    return await svc.effective_policy(db, segment_id)


# ─── Política de conteúdo ────────────────────────────────────────────────────

@router.get("/content-policies")
async def list_policies(ctx: AuthContext = Depends(READ), db: AsyncSession = Depends(get_db)):
    rows = (await db.execute(select(ContentPolicy, Segment).join(Segment, Segment.id == ContentPolicy.segment_id)
                             .order_by(ContentPolicy.effect, Segment.name))).all()
    items = [{"segment_id": str(p.segment_id), "segment_name": s.name, "segment_slug": s.slug, "effect": p.effect,
              "reason": p.reason, "is_default": p.is_default, "updated_at": iso(p.updated_at)} for p, s in rows]
    return {"items": items, "total": len(items), "page": 1, "page_size": max(len(items), 1)}


@router.put("/content-policies/{segment_id}")
async def put_policy(segment_id: uuid.UUID, body: PolicyIn,
                     ctx: AuthContext = Depends(require("taxonomy.manage", global_only=True, allow_impersonation=False)),
                     db: AsyncSession = Depends(get_db)):
    p = await svc.set_content_policy(db, ctx, segment_id, body.effect, body.reason)
    await db.commit()
    return {"segment_id": str(p.segment_id), "effect": p.effect, "reason": p.reason, "is_default": p.is_default}


@router.post("/content-policies/{segment_id}/remove")
async def delete_policy(segment_id: uuid.UUID, body: ReasonIn,
                        ctx: AuthContext = Depends(require("taxonomy.manage", global_only=True,
                                                           allow_impersonation=False)),
                        db: AsyncSession = Depends(get_db)):
    await svc.remove_content_policy(db, ctx, segment_id, body.reason)
    await db.commit()
    return {"segment_id": str(segment_id), "removed": True}


# ─── Marcas / produtos ───────────────────────────────────────────────────────

def _brand_out(b: Brand) -> dict:
    return {"id": str(b.id), "name": b.name, "slug": b.slug, "is_active": b.is_active,
            "owner_org_id": str(b.owner_org_id) if b.owner_org_id else None}


def _product_out(p: Product) -> dict:
    return {"id": str(p.id), "name": p.name, "slug": p.slug, "segment_id": str(p.segment_id),
            "brand_id": str(p.brand_id) if p.brand_id else None, "is_active": p.is_active}


@router.get("/brands")
async def list_brands(page: int = Query(1, ge=1), page_size: int = Query(50, ge=1, le=100),
                      search: Optional[str] = Query(None, max_length=100), owner_org_id: Optional[uuid.UUID] = None,
                      ctx: AuthContext = Depends(READ), db: AsyncSession = Depends(get_db)):
    q = select(Brand).where(Brand.is_active.is_(True))
    if search:
        q = q.where(Brand.name.ilike(f"%{search.strip()}%"))
    if owner_org_id:
        q = q.where(Brand.owner_org_id == owner_org_id)
    return await paginate(db, q.order_by(Brand.name), page, page_size, _brand_out)


async def _unique_slug(db, model, name: str, exclude=None) -> str:
    return await svc._unique_slug(db, model, name, exclude)


async def _check_org(db, org_id):
    if org_id is not None and await db.get(Organization, org_id) is None:
        raise DomainError("ORG_NOT_FOUND", "Organização inexistente.", status=400)


@router.post("/brands", status_code=201)
async def create_brand(body: BrandIn, ctx: AuthContext = Depends(WRITE), db: AsyncSession = Depends(get_db)):
    await _check_org(db, body.owner_org_id)
    b = Brand(id=uuid.uuid4(), name=body.name.strip(), slug=await _unique_slug(db, Brand, body.name),
              owner_org_id=body.owner_org_id)
    db.add(b)
    await db.flush()
    await audit(db, ctx, "brand.created", "brand", b.id, after=_brand_out(b))
    await db.commit()
    return _brand_out(b)


@router.patch("/brands/{brand_id}")
async def patch_brand(brand_id: uuid.UUID, body: BrandPatch, ctx: AuthContext = Depends(WRITE),
                      db: AsyncSession = Depends(get_db)):
    b = await db.get(Brand, brand_id)
    if b is None:
        raise NotFound()
    before = _brand_out(b)
    data = body.model_dump(exclude_unset=True)
    if "owner_org_id" in data:
        await _check_org(db, data["owner_org_id"])
    for k, v in data.items():
        setattr(b, k, v.strip() if isinstance(v, str) else v)
    await db.flush()
    await audit(db, ctx, "brand.updated", "brand", b.id, before=before, after=_brand_out(b))
    await db.commit()
    await db.refresh(b)
    return _brand_out(b)


@router.get("/products")
async def list_products(page: int = Query(1, ge=1), page_size: int = Query(50, ge=1, le=100),
                        segment_id: Optional[uuid.UUID] = None, brand_id: Optional[uuid.UUID] = None,
                        search: Optional[str] = Query(None, max_length=100),
                        ctx: AuthContext = Depends(READ), db: AsyncSession = Depends(get_db)):
    q = select(Product).where(Product.is_active.is_(True))
    if segment_id:
        q = q.where(Product.segment_id.in_(await svc.segment_descendant_ids(db, [segment_id]) or {segment_id}))
    if brand_id:
        q = q.where(Product.brand_id == brand_id)
    if search:
        q = q.where(Product.name.ilike(f"%{search.strip()}%"))
    return await paginate(db, q.order_by(Product.name), page, page_size, _product_out)


async def _check_product_refs(db, segment_id, brand_id):
    if segment_id is not None:
        await svc.validate_segment_ids(db, [segment_id])
    if brand_id is not None and await db.get(Brand, brand_id) is None:
        raise DomainError("BRAND_NOT_FOUND", "Marca inexistente.", status=400)


@router.post("/products", status_code=201)
async def create_product(body: ProductIn, ctx: AuthContext = Depends(WRITE), db: AsyncSession = Depends(get_db)):
    await _check_product_refs(db, body.segment_id, body.brand_id)
    p = Product(id=uuid.uuid4(), name=body.name.strip(), slug=await _unique_slug(db, Product, body.name),
                segment_id=body.segment_id, brand_id=body.brand_id)
    db.add(p)
    await db.flush()
    await audit(db, ctx, "product.created", "product", p.id, after=_product_out(p))
    await db.commit()
    return _product_out(p)


@router.patch("/products/{product_id}")
async def patch_product(product_id: uuid.UUID, body: ProductPatch, ctx: AuthContext = Depends(WRITE),
                        db: AsyncSession = Depends(get_db)):
    p = await db.get(Product, product_id)
    if p is None:
        raise NotFound()
    data = body.model_dump(exclude_unset=True)
    await _check_product_refs(db, data.get("segment_id"), data.get("brand_id"))
    before = _product_out(p)
    for k, v in data.items():
        setattr(p, k, v.strip() if isinstance(v, str) else v)
    await db.flush()
    await audit(db, ctx, "product.updated", "product", p.id, before=before, after=_product_out(p))
    await db.commit()
    await db.refresh(p)
    return _product_out(p)


# ─── Concorrência explícita ──────────────────────────────────────────────────

def _link_out(c: CompetitorLink) -> dict:
    return {"id": str(c.id), "org_a_id": str(c.org_a_id), "org_b_id": str(c.org_b_id), "reason": c.reason,
            "created_at": iso(c.created_at)}


@router.get("/competitor-links")
async def list_competitor_links(org_id: Optional[uuid.UUID] = None,
                                ctx: AuthContext = Depends(require("taxonomy.manage", global_only=True)),
                                db: AsyncSession = Depends(get_db)):
    q = select(CompetitorLink)
    if org_id:
        q = q.where((CompetitorLink.org_a_id == org_id) | (CompetitorLink.org_b_id == org_id))
    rows = (await db.execute(q.order_by(CompetitorLink.created_at))).scalars().all()
    return {"items": [_link_out(c) for c in rows], "total": len(rows), "page": 1, "page_size": max(len(rows), 1)}


@router.post("/competitor-links", status_code=201)
async def create_competitor_link(body: CompetitorIn, ctx: AuthContext = Depends(WRITE),
                                 db: AsyncSession = Depends(get_db)):
    if body.org_a_id == body.org_b_id:
        raise DomainError("COMPETITOR_SELF", "Uma organização não concorre consigo mesma.", status=400)
    for oid in (body.org_a_id, body.org_b_id):
        await _check_org(db, oid)
    # par canônico (CHECK org_a_id < org_b_id usa a ordem do tipo uuid no PostgreSQL)
    a, b = sorted([body.org_a_id, body.org_b_id])
    c = CompetitorLink(id=uuid.uuid4(), org_a_id=a, org_b_id=b, reason=body.reason.strip(), created_by=ctx.user_id)
    db.add(c)
    try:
        await db.flush()
    except IntegrityError:
        raise Conflict("COMPETITOR_LINK_EXISTS", "Vínculo de concorrência já existe.") from None
    await audit(db, ctx, "competitor_link.created", "competitor_link", c.id, after=_link_out(c), reason=body.reason)
    await db.commit()
    return _link_out(c)


@router.post("/competitor-links/{link_id}/remove")
async def delete_competitor_link(link_id: uuid.UUID, body: ReasonIn, ctx: AuthContext = Depends(WRITE),
                                 db: AsyncSession = Depends(get_db)):
    c = await db.get(CompetitorLink, link_id)
    if c is None:
        raise NotFound()
    before = _link_out(c)
    await db.delete(c)
    await db.flush()
    await audit(db, ctx, "competitor_link.removed", "competitor_link", link_id, before=before, reason=body.reason)
    await db.commit()
    return {"id": str(link_id), "removed": True}

