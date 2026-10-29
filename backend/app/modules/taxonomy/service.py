"""
RD Mídia Indoor — Serviço de taxonomia (W-ORG). Interface estável para W-CAMP/W-INV.

Árvores com caminho materializado (``path`` = ``/<hex>/<hex>/``):
- ``node_ancestor_ids`` / ``node_descendant_ids``     (taxonomy_nodes)
- ``segment_ancestor_ids`` / ``segment_descendant_ids`` (segments)
  (todas incluem os próprios ids; aceitam vários ids)
- ``blocked_segment_ids(db, segment_ids)`` → ``{segment_id: segment_id_que_bloqueia}``
  (BLOCK em qualquer ancestral-ou-próprio ⇒ bloqueado — não sobrescrevível)
- ``effective_policy(db, segment_id)`` → ``{"effect","source_segment_id","reason"}``
- ``ensure_default_content_policies(db)``: seed idempotente dos defaults (ETAPA2
  #118–#125): apostas, conteúdo adulto, armas = BLOCK; bebidas alcoólicas, tabaco,
  política, farmacêuticos = ALLOW. Não sobrescreve decisão do admin.
- ``ensure_reference_taxonomy(db)``: árvore inicial de referência (ETAPA2 #102–#111),
  editável pelo admin (não é dado de demonstração).
"""
from __future__ import annotations

import uuid
from typing import Any, Iterable, Optional, Type

from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.audit import audit
from app.core.errors import Conflict, DomainError, NotFound
from app.models.taxonomy import (
    TAXONOMY_PARENT_KIND,
    ContentPolicy,
    PolicyEffect,
    Segment,
    TaxonomyNode,
)
from app.modules.establishments.geo import slug as make_slug

TreeModel = Type[Any]  # TaxonomyNode | Segment


def _path_ids(path: str) -> list[uuid.UUID]:
    return [uuid.UUID(hex=p) for p in path.strip("/").split("/") if p]


def _as_uuids(ids: Iterable[Any]) -> list[uuid.UUID]:
    return [i if isinstance(i, uuid.UUID) else uuid.UUID(str(i)) for i in ids]


async def _get(db: AsyncSession, model: TreeModel, id_: Any) -> Any:
    obj = await db.get(model, id_)
    if obj is None:
        raise NotFound()
    return obj


async def ancestor_ids(db: AsyncSession, model: TreeModel, ids: Iterable[Any]) -> set[uuid.UUID]:
    ids = _as_uuids(ids)
    if not ids:
        return set()
    rows = (await db.execute(select(model.path).where(model.id.in_(ids)))).scalars().all()
    out: set[uuid.UUID] = set()
    for p in rows:
        out.update(_path_ids(p))
    return out


async def descendant_ids(db: AsyncSession, model: TreeModel, ids: Iterable[Any]) -> set[uuid.UUID]:
    ids = _as_uuids(ids)
    if not ids:
        return set()
    paths = (await db.execute(select(model.path).where(model.id.in_(ids)))).scalars().all()
    if not paths:
        return set()
    from sqlalchemy import or_

    conds = [model.path.like(p + "%") for p in paths]  # path só tem hex e "/"
    rows = (await db.execute(select(model.id).where(or_(*conds)))).scalars().all()
    return set(rows)


async def node_ancestor_ids(db: AsyncSession, ids: Iterable[Any]) -> set[uuid.UUID]:
    return await ancestor_ids(db, TaxonomyNode, ids)


async def node_descendant_ids(db: AsyncSession, ids: Iterable[Any]) -> set[uuid.UUID]:
    return await descendant_ids(db, TaxonomyNode, ids)


async def segment_ancestor_ids(db: AsyncSession, ids: Iterable[Any]) -> set[uuid.UUID]:
    return await ancestor_ids(db, Segment, ids)


async def segment_descendant_ids(db: AsyncSession, ids: Iterable[Any]) -> set[uuid.UUID]:
    return await descendant_ids(db, Segment, ids)


# ─── Criação / edição de nós ─────────────────────────────────────────────────

async def _unique_slug(db: AsyncSession, model: TreeModel, base: str, exclude_id: Optional[uuid.UUID] = None) -> str:
    base = make_slug(base)[:120] or "item"
    candidate = base
    n = 2
    while True:
        q = select(model.id).where(model.slug == candidate)
        if exclude_id is not None:
            q = q.where(model.id != exclude_id)
        if (await db.execute(q)).scalar_one_or_none() is None:
            return candidate
        candidate = f"{base}-{n}"
        n += 1


async def create_tree_item(
    db: AsyncSession,
    ctx: Any,
    model: TreeModel,
    *,
    name: str,
    parent_id: Optional[uuid.UUID],
    slug: Optional[str] = None,
    kind: Optional[str] = None,
) -> Any:
    name = (name or "").strip()
    if not name:
        raise DomainError("TAXONOMY_NAME_REQUIRED", "Informe o nome.", status=400)
    parent = await _get(db, model, parent_id) if parent_id else None
    if model is TaxonomyNode:
        if kind not in TAXONOMY_PARENT_KIND:
            raise DomainError("TAXONOMY_KIND_INVALID", "Tipo inválido (SECTOR, CATEGORY ou SUBCATEGORY).", status=400)
        expected_parent = TAXONOMY_PARENT_KIND[kind]
        if (parent is None) != (expected_parent is None) or (parent is not None and parent.kind != expected_parent):
            raise DomainError(
                "TAXONOMY_PARENT_INVALID",
                "Hierarquia inválida: SETOR > CATEGORIA > SUBCATEGORIA.",
                status=400,
                details={"kind": kind, "expected_parent_kind": expected_parent},
            )
    elif parent is not None and parent.depth >= 8:
        raise DomainError("TAXONOMY_TOO_DEEP", "Profundidade máxima de segmentos atingida.", status=400)
    if slug:
        s = make_slug(slug)
        if not s:
            raise DomainError("TAXONOMY_SLUG_INVALID", "Identificador inválido.", status=400)
        if (await db.execute(select(model.id).where(model.slug == s))).scalar_one_or_none() is not None:
            raise Conflict("TAXONOMY_SLUG_TAKEN", "Identificador já usado.")
    else:
        s = await _unique_slug(db, model, name)
    new_id = uuid.uuid4()
    path = (parent.path if parent else "/") + f"{new_id.hex}/"
    kw: dict = dict(id=new_id, parent_id=parent.id if parent else None, name=name, slug=s, path=path,
                    depth=(parent.depth + 1) if parent else 0, is_active=True)
    if model is TaxonomyNode:
        kw["kind"] = kind
    obj = model(**kw)
    db.add(obj)
    await db.flush()
    await audit(db, ctx, f"{model.__tablename__}.created", model.__tablename__, obj.id,
                after={"name": name, "slug": s, "parent_id": kw["parent_id"], "kind": kind})
    return obj


async def update_tree_item(
    db: AsyncSession,
    ctx: Any,
    model: TreeModel,
    item_id: uuid.UUID,
    *,
    name: Optional[str] = None,
    is_active: Optional[bool] = None,
    parent_id: Any = ...,
) -> Any:
    obj = await _get(db, model, item_id)
    before: dict = {}
    after: dict = {}
    if name is not None and name.strip() and name.strip() != obj.name:
        before["name"], after["name"] = obj.name, name.strip()
        obj.name = name.strip()
    if is_active is not None and is_active != obj.is_active:
        before["is_active"], after["is_active"] = obj.is_active, is_active
        obj.is_active = is_active
    if parent_id is not ... and parent_id != obj.parent_id:
        new_parent = await _get(db, model, parent_id) if parent_id else None
        if model is TaxonomyNode:
            expected = TAXONOMY_PARENT_KIND[obj.kind]
            if (new_parent is None) != (expected is None) or (new_parent is not None and new_parent.kind != expected):
                raise DomainError("TAXONOMY_PARENT_INVALID", "Hierarquia inválida: SETOR > CATEGORIA > SUBCATEGORIA.",
                                  status=400)
        if new_parent is not None and new_parent.path.startswith(obj.path):
            raise DomainError("TAXONOMY_CYCLE", "Um item não pode ficar abaixo de si mesmo.", status=400)
        old_prefix = obj.path
        new_prefix = (new_parent.path if new_parent else "/") + f"{obj.id.hex}/"
        delta = ((new_parent.depth + 1) if new_parent else 0) - obj.depth
        if model is Segment and new_parent is not None:
            max_depth = (await db.execute(
                select(func.max(Segment.depth)).where(Segment.path.like(old_prefix + "%"))
            )).scalar_one()
            if (max_depth or 0) + delta > 8:
                raise DomainError("TAXONOMY_TOO_DEEP", "Profundidade máxima de segmentos atingida.", status=400)
        before["parent_id"], after["parent_id"] = obj.parent_id, parent_id
        await db.flush()
        tbl = model.__tablename__
        await db.execute(
            text(
                f"UPDATE {tbl} SET path = :np || substr(path, length(:op) + 1), depth = depth + :d "
                f"WHERE path LIKE :op || '%' AND id <> :id"
            ),
            {"np": new_prefix, "op": old_prefix, "d": delta, "id": obj.id},
        )
        obj.parent_id = new_parent.id if new_parent else None
        obj.path = new_prefix
        obj.depth = obj.depth + delta
    if after:
        await db.flush()
        await audit(db, ctx, f"{model.__tablename__}.updated", model.__tablename__, obj.id, before=before, after=after)
    return obj


def tree_out(obj: Any) -> dict:
    d = {
        "id": str(obj.id),
        "parent_id": str(obj.parent_id) if obj.parent_id else None,
        "name": obj.name,
        "slug": obj.slug,
        "depth": obj.depth,
        "is_active": obj.is_active,
        "ancestor_ids": [str(i) for i in _path_ids(obj.path)[:-1]],
    }
    if hasattr(obj, "kind"):
        d["kind"] = obj.kind
    return d


# ─── Política de conteúdo ────────────────────────────────────────────────────

async def blocked_segment_ids(db: AsyncSession, segment_ids: Iterable[Any]) -> dict[uuid.UUID, uuid.UUID]:
    """``{segment_id: segmento (próprio ou ancestral) cuja política é BLOCK}``."""
    ids = _as_uuids(segment_ids)
    if not ids:
        return {}
    rows = (await db.execute(select(Segment.id, Segment.path).where(Segment.id.in_(ids)))).all()
    chain = {sid: _path_ids(p) for sid, p in rows}
    all_ids = {a for c in chain.values() for a in c}
    blocks = set(
        (
            await db.execute(
                select(ContentPolicy.segment_id).where(
                    ContentPolicy.segment_id.in_(all_ids), ContentPolicy.effect == PolicyEffect.BLOCK.value
                )
            )
        ).scalars().all()
    )
    out: dict[uuid.UUID, uuid.UUID] = {}
    for sid, c in chain.items():
        for anc in c:  # da raiz para a folha: o bloqueio mais amplo é reportado
            if anc in blocks:
                out[sid] = anc
                break
    return out


async def effective_policy(db: AsyncSession, segment_id: Any) -> dict:
    seg = await _get(db, Segment, segment_id)
    chain = _path_ids(seg.path)
    pols = {
        p.segment_id: p
        for p in (await db.execute(select(ContentPolicy).where(ContentPolicy.segment_id.in_(chain)))).scalars().all()
    }
    for anc in chain:
        p = pols.get(anc)
        if p is not None and p.effect == PolicyEffect.BLOCK.value:
            return {"effect": "BLOCK", "source_segment_id": str(anc), "reason": p.reason}
    for anc in reversed(chain):
        p = pols.get(anc)
        if p is not None:
            return {"effect": p.effect, "source_segment_id": str(anc), "reason": p.reason}
    return {"effect": "ALLOW", "source_segment_id": None, "reason": "Sem política específica (liberado)."}


async def set_content_policy(
    db: AsyncSession, ctx: Any, segment_id: uuid.UUID, effect: str, reason: str
) -> ContentPolicy:
    if effect not in (PolicyEffect.BLOCK.value, PolicyEffect.ALLOW.value):
        raise DomainError("POLICY_EFFECT_INVALID", "Efeito deve ser BLOCK ou ALLOW.", status=400)
    reason = (reason or "").strip()
    if len(reason) < 10:
        raise DomainError("REASON_REQUIRED", "Justificativa obrigatória (mínimo 10 caracteres).", status=400)
    await _get(db, Segment, segment_id)
    pol = (
        await db.execute(select(ContentPolicy).where(ContentPolicy.segment_id == segment_id).with_for_update())
    ).scalar_one_or_none()
    before = {"effect": pol.effect, "reason": pol.reason} if pol else None
    if pol is None:
        pol = ContentPolicy(segment_id=segment_id, effect=effect, reason=reason, is_default=False,
                            updated_by=getattr(ctx, "user_id", None))
        db.add(pol)
    else:
        pol.effect = effect
        pol.reason = reason
        pol.is_default = False
        pol.updated_by = getattr(ctx, "user_id", None)
    await db.flush()
    await audit(db, ctx, "content_policy.set", "content_policy", segment_id, before=before,
                after={"effect": effect, "reason": reason}, reason=reason)
    return pol


async def remove_content_policy(db: AsyncSession, ctx: Any, segment_id: uuid.UUID, reason: str) -> None:
    reason = (reason or "").strip()
    if len(reason) < 10:
        raise DomainError("REASON_REQUIRED", "Justificativa obrigatória (mínimo 10 caracteres).", status=400)
    pol = await db.get(ContentPolicy, segment_id)
    if pol is None:
        raise NotFound()
    before = {"effect": pol.effect, "reason": pol.reason}
    await db.delete(pol)
    await db.flush()
    await audit(db, ctx, "content_policy.removed", "content_policy", segment_id, before=before, reason=reason)


# ─── Seeds ───────────────────────────────────────────────────────────────────

DEFAULT_POLICY_SEGMENTS: list[tuple[str, str, Optional[str], str]] = [
    # (slug, nome, slug_do_pai, efeito)
    ("apostas", "Apostas", None, "BLOCK"),
    ("conteudo-adulto", "Conteúdo adulto", None, "BLOCK"),
    ("armas", "Armas", None, "BLOCK"),
    ("bebidas", "Bebidas", None, ""),
    ("bebidas-alcoolicas", "Bebidas alcoólicas", "bebidas", "ALLOW"),
    ("tabaco", "Tabaco", None, "ALLOW"),
    ("politica", "Política", None, "ALLOW"),
    ("farmaceuticos", "Produtos farmacêuticos", None, "ALLOW"),
]


async def _ensure_segment(db: AsyncSession, slug: str, name: str, parent_slug: Optional[str]) -> Segment:
    seg = (await db.execute(select(Segment).where(Segment.slug == slug))).scalar_one_or_none()
    if seg is not None:
        return seg
    parent = None
    if parent_slug:
        parent = (await db.execute(select(Segment).where(Segment.slug == parent_slug))).scalar_one()
    sid = uuid.uuid4()
    seg = Segment(id=sid, parent_id=parent.id if parent else None, name=name, slug=slug,
                  path=(parent.path if parent else "/") + f"{sid.hex}/", depth=(parent.depth + 1) if parent else 0)
    db.add(seg)
    await db.flush()
    return seg


async def ensure_default_content_policies(db: AsyncSession, ctx: Any = None) -> dict[str, str]:
    """Seed idempotente; retorna ``{slug: efeito efetivo}``. Nunca sobrescreve política existente."""
    out: dict[str, str] = {}
    created = []
    for slug, name, parent_slug, effect in DEFAULT_POLICY_SEGMENTS:
        seg = await _ensure_segment(db, slug, name, parent_slug)
        if not effect:
            continue
        pol = await db.get(ContentPolicy, seg.id)
        if pol is None:
            pol = ContentPolicy(segment_id=seg.id, effect=effect, is_default=True,
                                reason="Padrão do sistema (ETAPA2 #118–#125); administrável pela RD.")
            db.add(pol)
            created.append(slug)
        out[slug] = pol.effect
    await db.flush()
    if created:
        await audit(db, ctx, "content_policy.defaults_seeded", "content_policy", None, after={"created": created},
                    origin=None if ctx is not None else "system")
    return out


REFERENCE_TAXONOMY: dict[str, dict[str, list[str]]] = {
    "Alimentação": {
        "Supermercado": [],
        "Açougue": [],
        "Padaria": [],
        "Restaurante": [],
        "Distribuidora de bebidas": [],
    },
    "Saúde": {"Farmácia": [], "Clínica": []},
    "Serviços": {"Academia": [], "Oficina mecânica": [], "Salão de beleza": []},
    "Varejo": {"Shopping": [], "Loja de roupas": []},
}
REFERENCE_SEGMENTS: list[tuple[str, str, Optional[str]]] = [
    ("alimentos", "Alimentos", None),
    ("snacks", "Snacks e salgadinhos", "alimentos"),
    ("bebidas", "Bebidas", None),
    ("bebidas-nao-alcoolicas", "Bebidas não alcoólicas", "bebidas"),
    ("bebidas-alcoolicas", "Bebidas alcoólicas", "bebidas"),
    ("cerveja", "Cerveja", "bebidas-alcoolicas"),
    ("higiene", "Higiene pessoal", None),
    ("limpeza", "Limpeza", None),
    ("delivery", "Delivery", None),
]


async def ensure_reference_taxonomy(db: AsyncSession) -> dict:
    """Árvore inicial de referência (idempotente, editável). Retorna slugs → ids."""
    ids: dict[str, uuid.UUID] = {}
    kinds = ("SECTOR", "CATEGORY", "SUBCATEGORY")

    async def node(name: str, kind: str, parent: Optional[TaxonomyNode]) -> TaxonomyNode:
        s = make_slug(name)
        n = (await db.execute(select(TaxonomyNode).where(TaxonomyNode.slug == s))).scalar_one_or_none()
        if n is None:
            nid = uuid.uuid4()
            n = TaxonomyNode(id=nid, kind=kind, parent_id=parent.id if parent else None, name=name, slug=s,
                             path=(parent.path if parent else "/") + f"{nid.hex}/",
                             depth=kinds.index(kind))
            db.add(n)
            await db.flush()
        ids[s] = n.id
        return n

    for sector, cats in REFERENCE_TAXONOMY.items():
        sn = await node(sector, "SECTOR", None)
        for cat, subs in cats.items():
            cn = await node(cat, "CATEGORY", sn)
            for sub in subs:
                await node(sub, "SUBCATEGORY", cn)
    for slug, name, parent_slug in REFERENCE_SEGMENTS:
        seg = await _ensure_segment(db, slug, name, parent_slug)
        ids[f"segment:{slug}"] = seg.id
    await db.flush()
    return ids


async def validate_segment_ids(db: AsyncSession, ids: Iterable[Any]) -> list[uuid.UUID]:
    ids = list(dict.fromkeys(_as_uuids(ids)))
    if not ids:
        return []
    found = set((await db.execute(select(Segment.id).where(Segment.id.in_(ids), Segment.is_active.is_(True)))).scalars())
    missing = [str(i) for i in ids if i not in found]
    if missing:
        raise DomainError("SEGMENT_NOT_FOUND", "Segmento inexistente ou inativo.", status=400, details={"ids": missing})
    return ids


async def validate_category_id(db: AsyncSession, category_id: Any) -> Optional[uuid.UUID]:
    if category_id is None:
        return None
    cid = category_id if isinstance(category_id, uuid.UUID) else uuid.UUID(str(category_id))
    n = await db.get(TaxonomyNode, cid)
    if n is None or not n.is_active:
        raise DomainError("CATEGORY_NOT_FOUND", "Categoria inexistente ou inativa.", status=400)
    if n.kind == "SECTOR":
        raise DomainError("CATEGORY_TOO_BROAD", "Escolha uma categoria ou subcategoria (não um setor).", status=400)
    return cid


__all__ = [
    "ancestor_ids",
    "descendant_ids",
    "node_ancestor_ids",
    "node_descendant_ids",
    "segment_ancestor_ids",
    "segment_descendant_ids",
    "blocked_segment_ids",
    "effective_policy",
    "set_content_policy",
    "remove_content_policy",
    "ensure_default_content_policies",
    "ensure_reference_taxonomy",
    "validate_segment_ids",
    "validate_category_id",
    "create_tree_item",
    "update_tree_item",
    "tree_out",
]
