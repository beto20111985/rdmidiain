"""
Pessoas exibidas nos domínios do W-BIZ (suporte e designer): autores de mensagens/
comentários e listas de quem pode receber atribuição.

REGRA DE EXIBIÇÃO DO AUTOR (documentada no relatório W-BIZ):
- ``author_is_staff`` = o autor tem atribuição de perfil da EQUIPE RD (perfis
  ``RD_STAFF_ROLES`` ou qualquer atribuição em escopo GLOBAL/CITY).
- Quem LÊ é equipe RD → nome real de todos os autores.
- Quem LÊ é cliente (anunciante/estabelecimento/agência) → autores da equipe aparecem
  como ``"Equipe RD"`` (em instância white-label: ``"Equipe <nome da instância>"``) (nome individual de funcionário não é exposto ao cliente);
  demais autores (usuários da própria organização com acesso ao chamado/pedido) aparecem
  pelo nome. O próprio leitor aparece pelo nome.
- Usuário anonimizado (LGPD) → ``"Usuário removido"``.
Nunca expõe e-mail, telefone ou papéis do autor ao cliente.

LISTAS DE ATRIBUIÇÃO: somente id, nome, escopos (GLOBAL/CITY) e carga atual — sem e-mail,
telefone ou outros dados pessoais.
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from typing import Iterable, Optional

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.permissions import RD_STAFF_ROLES, SCOPE_CITY, SCOPE_GLOBAL
from app.core.timeutils import utcnow

STAFF_LABEL = "Equipe RD"
REMOVED_LABEL = "Usuário removido"


async def staff_user_ids(db: AsyncSession, user_ids: Iterable[uuid.UUID]) -> set[uuid.UUID]:
    from app.models.rbac import Role, RoleAssignment

    ids = {u for u in user_ids if u is not None}
    if not ids:
        return set()
    now = utcnow()
    rows = (await db.execute(
        select(RoleAssignment.user_id).join(Role, Role.id == RoleAssignment.role_id)
        .where(RoleAssignment.user_id.in_(ids),
               or_(RoleAssignment.expires_at.is_(None), RoleAssignment.expires_at > now),
               or_(Role.slug.in_(list(RD_STAFF_ROLES)), RoleAssignment.scope_type.in_((SCOPE_GLOBAL, SCOPE_CITY))))
        .distinct()
    )).scalars()
    return set(rows)


async def describe_authors(db: AsyncSession, user_ids: Iterable[uuid.UUID], *, viewer_is_staff: bool,
                           viewer_user_id: Optional[uuid.UUID]) -> dict[uuid.UUID, dict]:
    from app.models.users import User

    ids = {u for u in user_ids if u is not None}
    if not ids:
        return {}
    users = {u.id: u for u in (await db.execute(select(User).where(User.id.in_(ids)))).scalars()}
    staff = await staff_user_ids(db, ids)
    from app.modules.instance.service import get_brand

    label = (await get_brand(db, with_logo=False)).staff_label if staff else STAFF_LABEL
    out: dict[uuid.UUID, dict] = {}
    for uid in ids:
        u = users.get(uid)
        is_staff = uid in staff
        if u is None or u.anonymized_at is not None:
            name = REMOVED_LABEL
        elif is_staff and not viewer_is_staff and uid != viewer_user_id:
            name = label
        else:
            name = u.full_name
        out[uid] = {"author_name": name, "author_is_staff": is_staff}
    return out


@dataclass
class Candidate:
    user_id: uuid.UUID
    name: str
    scopes: list = field(default_factory=list)  # [{"type": "GLOBAL"} | {"type": "CITY", "value": "GO:goiania"}]
    roles: list = field(default_factory=list)


async def users_with_permission(db: AsyncSession, perm: str, *, cities: Optional[set] = None,
                                all_cities: bool = False, global_only: bool = False) -> list[Candidate]:
    """Usuários ATIVOS com ``perm`` em escopo GLOBAL (sempre) e CITY (``all_cities`` ou em ``cities``)."""
    from app.models.rbac import Permission, Role, RoleAssignment, RolePermission
    from app.models.users import User

    now = utcnow()
    q = (select(User.id, User.full_name, Role.slug, RoleAssignment.scope_type, RoleAssignment.scope_value)
         .select_from(RoleAssignment)
         .join(Role, Role.id == RoleAssignment.role_id)
         .join(RolePermission, RolePermission.role_id == Role.id)
         .join(Permission, Permission.id == RolePermission.permission_id)
         .join(User, User.id == RoleAssignment.user_id)
         .where(Permission.code == perm, User.is_active.is_(True), User.anonymized_at.is_(None),
                or_(RoleAssignment.expires_at.is_(None), RoleAssignment.expires_at > now)))
    if global_only:
        q = q.where(RoleAssignment.scope_type == SCOPE_GLOBAL)
    elif all_cities:
        q = q.where(RoleAssignment.scope_type.in_((SCOPE_GLOBAL, SCOPE_CITY)))
    else:
        conds = [RoleAssignment.scope_type == SCOPE_GLOBAL]
        if cities:
            conds.append((RoleAssignment.scope_type == SCOPE_CITY) & RoleAssignment.scope_value.in_(list(cities)))
        q = q.where(or_(*conds))
    acc: dict[uuid.UUID, Candidate] = {}
    for uid, name, slug, stype, sval in (await db.execute(q)).all():
        c = acc.setdefault(uid, Candidate(user_id=uid, name=name))
        scope = {"type": SCOPE_GLOBAL} if stype == SCOPE_GLOBAL else {"type": SCOPE_CITY, "value": sval}
        if scope not in c.scopes:
            c.scopes.append(scope)
        if slug not in c.roles:
            c.roles.append(slug)
    return sorted(acc.values(), key=lambda c: (c.name.lower(), str(c.user_id)))
