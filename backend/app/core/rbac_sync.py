"""
RD Mídia Indoor — Sincronização idempotente catálogo (código) → banco.

``sync_catalog_sync(connection)`` usa SQLAlchemy Core numa Connection síncrona —
serve para a migration (``op.get_bind()``) e, via ``run_sync``, para código async
(``await sync_catalog(db)``). Executar N vezes produz o mesmo estado.
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass, field

from sqlalchemy import delete, insert, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.permissions import CATALOG, ROLE_TEMPLATES, module_of


@dataclass
class SyncReport:
    permissions_created: list[str] = field(default_factory=list)
    permissions_removed: list[str] = field(default_factory=list)
    roles_created: list[str] = field(default_factory=list)
    roles_updated: list[str] = field(default_factory=list)
    links_added: int = 0
    links_removed: int = 0

    def summary(self) -> str:
        return (
            f"permissões +{len(self.permissions_created)}/-{len(self.permissions_removed)}; "
            f"perfis +{len(self.roles_created)} ~{len(self.roles_updated)}; "
            f"vínculos +{self.links_added}/-{self.links_removed}"
        )


def sync_catalog_sync(conn) -> SyncReport:
    from app.models.rbac import Permission, Role, RolePermission

    rep = SyncReport()
    p_t, r_t, rp_t = Permission.__table__, Role.__table__, RolePermission.__table__

    existing = {code: pid for pid, code in conn.execute(select(p_t.c.id, p_t.c.code)).all()}
    for code, name in CATALOG.items():
        if code not in existing:
            pid = uuid.uuid4()
            conn.execute(insert(p_t).values(id=pid, code=code, name=name, description=name, module=module_of(code)))
            existing[code] = pid
            rep.permissions_created.append(code)
        else:
            conn.execute(
                update(p_t).where(p_t.c.code == code).values(name=name, description=name, module=module_of(code))
            )
    for code in sorted(set(existing) - set(CATALOG)):
        pid = existing.pop(code)
        res = conn.execute(delete(rp_t).where(rp_t.c.permission_id == pid))
        rep.links_removed += res.rowcount or 0
        conn.execute(delete(p_t).where(p_t.c.id == pid))
        rep.permissions_removed.append(code)

    roles = {slug: (rid, is_sys) for rid, slug, is_sys in conn.execute(select(r_t.c.id, r_t.c.slug, r_t.c.is_system)).all()}
    for slug, tpl in ROLE_TEMPLATES.items():
        if slug not in roles:
            rid = uuid.uuid4()
            conn.execute(
                insert(r_t).values(
                    id=rid, slug=slug, name=tpl.name, description=tpl.description, is_system=True,
                    mfa_required=tpl.mfa_required,
                )
            )
            roles[slug] = (rid, True)
            rep.roles_created.append(slug)
        else:
            rid, _ = roles[slug]
            conn.execute(
                update(r_t).where(r_t.c.id == rid).values(
                    name=tpl.name, description=tpl.description, is_system=True, mfa_required=tpl.mfa_required
                )
            )
        rid = roles[slug][0]
        want = {existing[c] for c in tpl.permissions}
        have = {pid for (pid,) in conn.execute(select(rp_t.c.permission_id).where(rp_t.c.role_id == rid)).all()}
        add, remove = want - have, have - want
        for pid in add:
            conn.execute(insert(rp_t).values(role_id=rid, permission_id=pid))
        if remove:
            conn.execute(delete(rp_t).where(rp_t.c.role_id == rid, rp_t.c.permission_id.in_(remove)))
        if add or remove:
            rep.roles_updated.append(slug)
        rep.links_added += len(add)
        rep.links_removed += len(remove)
    return rep


async def sync_catalog(db: AsyncSession) -> SyncReport:
    return await db.run_sync(lambda s: sync_catalog_sync(s.connection()))
