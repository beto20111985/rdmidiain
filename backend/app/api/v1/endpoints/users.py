"""
RD Mídia Indoor — Usuários, atribuições (perfil × escopo), perfis e catálogo.

Regras (contrato §2 / §3; P001 §84):
- Listagem/consulta SEMPRE pelo escopo do ator (nunca usuários de outra
  organização). Fora do escopo → 404.
- Gestão exige DOMINÂNCIA (``authz.can_manage_user``): o ator precisa de
  ``users.manage`` e de todas as permissões de cada atribuição do alvo, no escopo
  dela. Impede org_admin de alterar super admin e escalonamento de privilégio.
- Ninguém altera e-mail/telefone/is_active/2FA/sessões de um Admin Master além dele
  mesmo (assentos gerenciados só pela CLI). Outros veem apenas nome e status dele.
- Ninguém concede perfil a si mesmo.
"""
from __future__ import annotations

import uuid
from typing import Optional

from fastapi import APIRouter, Depends, Query, Response
from pydantic import BaseModel, Field
from sqlalchemy import delete, exists, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.audit import audit
from app.core.authz import (
    AuthContext,
    Grant,
    can_grant_role_in_scope,
    can_manage_user,
    can_view_user,
    covers_grant,
    get_auth_context,
    load_grants,
    require,
    role_reserved,
)
from app.core.database import get_db
from app.core.errors import Conflict, DomainError, Forbidden, NotFound
from app.core.permissions import ALL_PERMISSIONS, CATALOG, ROLE_TEMPLATES, SCOPE_GLOBAL, module_of
from app.core.timeutils import utcnow
from app.models.rbac import Permission, Role, RoleAssignment, RolePermission
from app.models.users import User, UserRecoveryCode
from app.modules.auth import service as svc

router = APIRouter()
roles_router = APIRouter()


def _sensitive_hidden(ctx: AuthContext, target: User) -> bool:
    return target.is_admin_master and target.id != ctx.user_id


def user_out(ctx: AuthContext, u: User, grants: tuple[Grant, ...]) -> dict:
    if _sensitive_hidden(ctx, u):
        # Admin Master visto por outra pessoa: apenas nome e status (P001 §84)
        return {"id": str(u.id), "name": u.full_name, "is_active": u.is_active, "is_admin_master": True,
                "restricted": True}
    return {
        "id": str(u.id),
        "name": u.full_name,
        "email": u.email,
        "phone": u.phone,
        "is_active": u.is_active,
        "is_admin_master": u.is_admin_master,
        "mfa_enabled": u.totp_enabled,
        "must_change_password": u.must_change_password,
        "last_login": u.last_login,
        "created_at": u.created_at,
        "locked": svc.lock_remaining_s(u) > 0,
        "assignments": [
            {"id": str(g.assignment_id), "role": g.role_slug, "scope_type": g.scope_type,
             "scope_id": str(g.scope_id) if g.scope_id else None, "scope_value": g.scope_value}
            for g in grants
        ],
        "restricted": False,
    }


async def _visible_user(db: AsyncSession, ctx: AuthContext, user_id: uuid.UUID) -> tuple[User, tuple[Grant, ...]]:
    u = await db.get(User, user_id)
    if u is None:
        raise NotFound()
    grants = await load_grants(db, u.id)
    if u.id != ctx.user_id and not can_view_user(ctx, grants):
        raise NotFound()
    return u, grants


def _assignment_scope_conds(orgs: set, ests: set, cities: set) -> list:
    """Condições SQL: atribuição (RoleAssignment) dentro dos escopos dados.

    Organização cobre também os estabelecimentos dela.
    """
    from app.models.establishments import Establishment

    conds = []
    if orgs:
        conds.append((RoleAssignment.scope_type == "ORGANIZATION") & RoleAssignment.scope_id.in_(orgs))
        conds.append(
            (RoleAssignment.scope_type == "ESTABLISHMENT")
            & RoleAssignment.scope_id.in_(select(Establishment.id).where(Establishment.org_id.in_(orgs)))
        )
    if ests:
        conds.append((RoleAssignment.scope_type == "ESTABLISHMENT") & RoleAssignment.scope_id.in_(ests))
    if cities:
        conds.append((RoleAssignment.scope_type == "CITY") & RoleAssignment.scope_value.in_(cities))
    return conds


def _active_assignment():
    now = utcnow()
    return or_(RoleAssignment.expires_at.is_(None), RoleAssignment.expires_at > now)


@router.get("")
async def list_users(
    page: int = Query(1, ge=1),
    page_size: int = Query(25, ge=1, le=100),
    search: Optional[str] = Query(None, max_length=100),
    is_active: Optional[bool] = None,
    role: Optional[list[str]] = Query(None, max_length=20, description="Filtra por perfil (repetível: ?role=a&role=b)"),
    ctx: AuthContext = Depends(require("users.view")),
    db: AsyncSession = Depends(get_db),
):
    q = select(User).where(User.anonymized_at.is_(None))
    is_global = ctx.can("users.view", global_only=True)
    scope_conds: list = []
    if not is_global:
        scope_conds = _assignment_scope_conds(
            ctx.org_ids("users.view") or set(),
            ctx.establishment_ids("users.view") or set(),
            ctx.city_keys("users.view") or set(),
        )
        if not scope_conds:
            return {"items": [], "total": 0, "page": page, "page_size": page_size}
        q = q.where(exists().where(RoleAssignment.user_id == User.id, or_(*scope_conds)))
    if role:
        slugs = sorted({r.strip() for r in role if r and r.strip()})
        # a atribuição com o perfil pedido também precisa estar dentro do escopo do ator
        role_conds = [
            RoleAssignment.user_id == User.id,
            RoleAssignment.role_id.in_(select(Role.id).where(Role.slug.in_(slugs))),
            _active_assignment(),
        ]
        if not is_global:
            role_conds.append(or_(*scope_conds))
        q = q.where(exists().where(*role_conds))
    if search:
        like = f"%{search.strip()}%"
        q = q.where(or_(User.full_name.ilike(like), User.email.ilike(like)))
    if is_active is not None:
        q = q.where(User.is_active == is_active)
    total = (await db.execute(select(func.count()).select_from(q.subquery()))).scalar_one()
    rows = (
        await db.execute(q.order_by(User.full_name, User.id).offset((page - 1) * page_size).limit(page_size))
    ).scalars().all()
    items = []
    for u in rows:
        items.append(user_out(ctx, u, await load_grants(db, u.id)))
    return {"items": items, "total": total, "page": page, "page_size": page_size}


ASSIGNABLE_MAX = 200


@router.get("/assignable")
async def list_assignable_users(
    permission: str = Query(..., min_length=3, max_length=100),
    org_id: Optional[uuid.UUID] = None,
    establishment_id: Optional[uuid.UUID] = None,
    city: Optional[str] = Query(None, max_length=120),
    search: Optional[str] = Query(None, max_length=100),
    ctx: AuthContext = Depends(get_auth_context),
    db: AsyncSession = Depends(get_db),
):
    """Usuários que podem RECEBER uma atribuição de trabalho que exige ``permission``.

    - Acesso: quem tem a própria ``permission`` (em algum escopo) — NÃO exige users.view.
    - Resposta mínima: somente ``id`` e ``name`` (sem e-mail, telefone, perfis, 2FA).
    - Ator com a permissão em GLOBAL: todos os usuários ativos que a possuem; com filtro de
      recurso (``org_id``/``establishment_id``/``city``), só os que a possuem cobrindo o recurso.
    - Ator com escopo (ORGANIZATION/ESTABLISHMENT/CITY): só usuários cuja atribuição está
      DENTRO dos escopos do ator (ex.: técnico de GO:goiania vê técnicos de GO:goiania);
      atribuições GLOBAL (equipe RD) não são listadas; recurso fora do escopo → 404.
    """
    from app.core.authz import normalize_city_key
    from app.core.permissions import ADMIN_MASTER_ONLY, is_valid_permission
    from app.models.establishments import Establishment

    if not is_valid_permission(permission):
        raise DomainError("PERMISSION_UNKNOWN", "Permissão inexistente.", status=400, details={"permission": permission})
    if not ctx.has_any(permission):
        raise Forbidden(details={"permission": permission})

    city_key = normalize_city_key(city) if city else None
    res_org, res_city = org_id, city_key
    if establishment_id is not None:
        row = (
            await db.execute(select(Establishment.org_id, getattr(Establishment, "city_key", Establishment.city))
                             .where(Establishment.id == establishment_id))
        ).first()
        if row is None:
            raise NotFound()
        res_org = res_org or row[0]
        res_city = res_city or (normalize_city_key(row[1]) if row[1] and ":" in str(row[1]) else None)
    has_resource = any(v is not None for v in (org_id, establishment_id, city_key))
    if has_resource:
        # recurso precisa estar no escopo do ator (não vazar existência)
        ctx.ensure(permission, org_id=res_org, establishment_id=establishment_id, city=res_city)

    holder_roles = (
        select(RolePermission.role_id)
        .join(Permission, Permission.id == RolePermission.permission_id)
        .where(Permission.code == permission)
    )
    ra_conds = [RoleAssignment.user_id == User.id, RoleAssignment.role_id.in_(holder_roles), _active_assignment()]

    if not ctx.can(permission, global_only=True):
        scope_conds = _assignment_scope_conds(
            ctx.org_ids(permission) or set(),
            ctx.establishment_ids(permission) or set(),
            ctx.city_keys(permission) or set(),
        )
        if not scope_conds:
            return {"items": [], "total": 0, "permission": permission}
        ra_conds.append(or_(*scope_conds))
    elif not has_resource:
        pass  # ator global sem filtro: qualquer atribuição que conceda a permissão

    if has_resource:
        cover = []
        if ctx.can(permission, global_only=True):
            cover.append(RoleAssignment.scope_type == SCOPE_GLOBAL)
        if res_org is not None:
            cover.append((RoleAssignment.scope_type == "ORGANIZATION") & (RoleAssignment.scope_id == res_org))
        if establishment_id is not None:
            cover.append((RoleAssignment.scope_type == "ESTABLISHMENT")
                         & (RoleAssignment.scope_id == establishment_id))
        if res_city is not None:
            cover.append((RoleAssignment.scope_type == "CITY") & (RoleAssignment.scope_value == res_city))
        ra_conds.append(or_(*cover))

    q = select(User.id, User.full_name).where(
        User.is_active.is_(True), User.anonymized_at.is_(None), exists().where(*ra_conds)
    )
    if permission in ADMIN_MASTER_ONLY:
        q = q.where(User.admin_master_seat.is_not(None))
    if search:
        q = q.where(User.full_name.ilike(f"%{search.strip()}%"))
    rows = (await db.execute(q.order_by(User.full_name, User.id).limit(ASSIGNABLE_MAX + 1))).all()
    items = [{"id": str(uid), "name": name} for uid, name in rows[:ASSIGNABLE_MAX]]
    return {"items": items, "total": len(items), "truncated": len(rows) > ASSIGNABLE_MAX, "permission": permission}


@router.get("/{user_id}")
async def get_user(user_id: uuid.UUID, ctx: AuthContext = Depends(require("users.view")),
                   db: AsyncSession = Depends(get_db)):
    u, grants = await _visible_user(db, ctx, user_id)
    return user_out(ctx, u, grants)


class UserPatch(BaseModel):
    name: Optional[str] = Field(default=None, min_length=2, max_length=255)
    phone: Optional[str] = Field(default=None, max_length=30)
    email: Optional[str] = Field(default=None, min_length=3, max_length=255)
    is_active: Optional[bool] = None


async def _managed_target(db: AsyncSession, ctx: AuthContext, user_id: uuid.UUID) -> tuple[User, tuple[Grant, ...]]:
    u, grants = await _visible_user(db, ctx, user_id)
    if u.id == ctx.user_id:
        raise Forbidden("SELF_MANAGEMENT", "Use /me para alterar seus próprios dados.")
    if u.is_admin_master:
        raise Forbidden("ADMIN_MASTER_PROTECTED", "Somente o próprio Admin Master altera seus dados de segurança.")
    if not can_manage_user(ctx, u, grants):
        raise Forbidden("USER_NOT_MANAGEABLE", "Você não pode gerenciar este usuário.")
    return u, grants


@router.patch("/{user_id}")
async def update_user(user_id: uuid.UUID, body: UserPatch,
                      ctx: AuthContext = Depends(require("users.manage", allow_impersonation=False)),
                      db: AsyncSession = Depends(get_db)):
    u, grants = await _managed_target(db, ctx, user_id)
    data = body.model_dump(exclude_unset=True)
    before, after = {}, {}
    if data.get("name"):
        before["name"], after["name"] = u.full_name, data["name"].strip()
        u.full_name = data["name"].strip()
    if "phone" in data:
        phone = svc.normalize_phone(data["phone"]) if data["phone"] else None
        if phone and (await db.execute(select(User.id).where(User.phone == phone, User.id != u.id))).first():
            raise Conflict("PHONE_IN_USE", "Telefone já cadastrado em outra conta.")
        before["phone"], after["phone"] = u.phone, phone
        u.phone = phone
    if data.get("email"):
        email = svc.normalize_email(data["email"])
        if not svc._EMAIL_RE.match(email):
            raise DomainError("EMAIL_INVALID", "E-mail inválido.", status=400)
        if (await db.execute(select(User.id).where(User.email == email, User.id != u.id))).first():
            raise Conflict("EMAIL_IN_USE", "E-mail já cadastrado.")
        before["email"], after["email"] = u.email, email
        u.email = email
    revoked: list = []
    if "is_active" in data and data["is_active"] is not None and data["is_active"] != u.is_active:
        before["is_active"], after["is_active"] = u.is_active, data["is_active"]
        u.is_active = data["is_active"]
        if not u.is_active:
            revoked = await svc.revoke_all_user_sessions(db, u.id, "user_deactivated")
    if after:
        await audit(db, ctx, "user.updated", "user", u.id, before=before, after=after)
    await db.flush()
    await svc.after_commit_revocations(revoked)
    return user_out(ctx, u, grants)


class AssignmentIn(BaseModel):
    role: str = Field(min_length=2, max_length=50)
    scope_type: str = SCOPE_GLOBAL
    scope_id: Optional[uuid.UUID] = None
    scope_value: Optional[str] = Field(default=None, max_length=120)


@router.post("/{user_id}/assignments", status_code=201)
async def add_assignment(user_id: uuid.UUID, body: AssignmentIn,
                         ctx: AuthContext = Depends(require("users.manage", allow_impersonation=False)),
                         db: AsyncSession = Depends(get_db)):
    from app.api.v1.endpoints.auth import _role_with_perms, scope_grant, validate_scope_target

    target = await db.get(User, user_id)
    if target is None:
        raise NotFound()
    if target.id == ctx.user_id:
        raise Forbidden("SELF_ASSIGNMENT", "Você não pode alterar os próprios perfis.")
    role, perms = await _role_with_perms(db, body.role)
    g = scope_grant(role, perms, body.scope_type, body.scope_id, body.scope_value)
    tgrants = await load_grants(db, target.id)
    if not covers_grant(ctx, "users.manage", g) and not can_view_user(ctx, tgrants):
        raise NotFound()
    if target.is_admin_master:
        raise Forbidden("ADMIN_MASTER_PROTECTED", "Perfis do Admin Master são gerenciados pela CLI.")
    if tgrants and not can_manage_user(ctx, target, tgrants):
        raise Forbidden("USER_NOT_MANAGEABLE", "Você não pode gerenciar este usuário.")
    if role_reserved(role.slug):  # AÇÃO 003 (ADR_011 D9): só as funções de assento concedem super_admin
        raise Forbidden("ROLE_RESERVED_FOR_ADMIN_MASTER", "O perfil Super Admin é exclusivo dos Administradores Master.")
    if not can_grant_role_in_scope(ctx, role.slug, perms, g):
        raise Forbidden("ROLE_ESCALATION", "Você não pode conceder este perfil neste escopo.")
    await validate_scope_target(db, g)
    dup = (
        await db.execute(
            select(RoleAssignment.id).where(
                RoleAssignment.user_id == target.id, RoleAssignment.role_id == role.id,
                RoleAssignment.scope_type == g.scope_type,
                RoleAssignment.scope_id.is_not_distinct_from(g.scope_id),
                RoleAssignment.scope_value.is_not_distinct_from(g.scope_value),
            )
        )
    ).first()
    if dup:
        raise Conflict("ASSIGNMENT_EXISTS", "Atribuição já existe.")
    ra = RoleAssignment(id=uuid.uuid4(), user_id=target.id, role_id=role.id, scope_type=g.scope_type,
                        scope_id=g.scope_id, scope_value=g.scope_value, granted_by=ctx.user_id)
    db.add(ra)
    await audit(db, ctx, "user.assignment_added", "user", target.id,
                after={"assignment_id": ra.id, "role": role.slug, "scope_type": g.scope_type,
                       "scope_id": g.scope_id, "scope_value": g.scope_value},
                org_id=g.scope_id if g.scope_type == "ORGANIZATION" else None)
    await db.flush()
    return {"id": str(ra.id), "role": role.slug, "scope_type": g.scope_type,
            "scope_id": str(g.scope_id) if g.scope_id else None, "scope_value": g.scope_value}


@router.delete("/{user_id}/assignments/{assignment_id}", status_code=204)
async def remove_assignment(user_id: uuid.UUID, assignment_id: uuid.UUID,
                            ctx: AuthContext = Depends(require("users.manage", allow_impersonation=False)),
                            db: AsyncSession = Depends(get_db)):
    u, grants = await _managed_target(db, ctx, user_id)
    g = next((x for x in grants if x.assignment_id == assignment_id), None)
    if g is None:
        raise NotFound()
    await db.execute(delete(RoleAssignment).where(RoleAssignment.id == assignment_id))
    await audit(db, ctx, "user.assignment_removed", "user", u.id,
                before={"assignment_id": assignment_id, "role": g.role_slug, "scope_type": g.scope_type,
                        "scope_id": g.scope_id, "scope_value": g.scope_value})
    return Response(status_code=204)


class ReasonIn(BaseModel):
    reason: str = Field(min_length=10, max_length=1000)


@router.post("/{user_id}/sessions/revoke-all", status_code=204)
async def revoke_user_sessions(user_id: uuid.UUID, body: ReasonIn,
                               ctx: AuthContext = Depends(require("users.manage", allow_impersonation=False)),
                               db: AsyncSession = Depends(get_db)):
    u, _ = await _managed_target(db, ctx, user_id)
    revoked = await svc.revoke_all_user_sessions(db, u.id, "admin_revoked")
    await audit(db, ctx, "user.sessions_revoked", "user", u.id, reason=body.reason,
                after={"sessions_revoked": len(revoked)})
    await svc.after_commit_revocations(revoked)
    return Response(status_code=204)


@router.post("/{user_id}/mfa/reset", status_code=204)
async def reset_user_mfa(user_id: uuid.UUID, body: ReasonIn,
                         ctx: AuthContext = Depends(require("users.manage", allow_impersonation=False)),
                         db: AsyncSession = Depends(get_db)):
    """Remove o 2FA de um usuário (perdeu o dispositivo e os códigos). Revoga as sessões dele."""
    u, _ = await _managed_target(db, ctx, user_id)
    u.totp_enabled = False
    u.totp_secret_enc = None
    u.totp_pending_secret_enc = None
    u.totp_enabled_at = None
    await db.execute(delete(UserRecoveryCode).where(UserRecoveryCode.user_id == u.id))
    revoked = await svc.revoke_all_user_sessions(db, u.id, "mfa_reset")
    await audit(db, ctx, "user.mfa_reset", "user", u.id, reason=body.reason)
    await svc.after_commit_revocations(revoked)
    return Response(status_code=204)


@router.post("/{user_id}/unlock", status_code=204)
async def unlock_user(user_id: uuid.UUID, body: ReasonIn,
                      ctx: AuthContext = Depends(require("users.manage", allow_impersonation=False)),
                      db: AsyncSession = Depends(get_db)):
    u, _ = await _managed_target(db, ctx, user_id)
    u.failed_login_attempts = 0
    u.locked_until = None
    await audit(db, ctx, "user.unlocked", "user", u.id, reason=body.reason)
    return Response(status_code=204)


# ─── Perfis e catálogo ───────────────────────────────────────────────────────

async def _roles_with_perms(db: AsyncSession) -> list[dict]:
    rows = (
        await db.execute(
            select(Role, Permission.code)
            .outerjoin(RolePermission, RolePermission.role_id == Role.id)
            .outerjoin(Permission, Permission.id == RolePermission.permission_id)
            .order_by(Role.is_system.desc(), Role.slug)
        )
    ).all()
    acc: dict = {}
    for role, code in rows:
        r = acc.setdefault(role.id, {"id": str(role.id), "slug": role.slug, "name": role.name,
                                     "description": role.description, "is_system": role.is_system,
                                     "mfa_required": role.mfa_required, "permissions": []})
        if code:
            r["permissions"].append(code)
    for r in acc.values():
        r["permissions"].sort()
        tpl = ROLE_TEMPLATES.get(r["slug"])
        r["typical_scopes"] = list(tpl.typical_scopes) if tpl else []
    return list(acc.values())


@roles_router.get("/roles")
async def list_roles(ctx: AuthContext = Depends(require("users.view")), db: AsyncSession = Depends(get_db)):
    return {"items": await _roles_with_perms(db)}


@roles_router.get("/permissions")
async def list_permissions(ctx: AuthContext = Depends(require("users.view"))):
    return {"items": [{"code": c, "name": n, "module": module_of(c)} for c, n in CATALOG.items()]}


class RoleIn(BaseModel):
    slug: str = Field(min_length=3, max_length=50, pattern=r"^[a-z][a-z0-9_]+$")
    name: str = Field(min_length=2, max_length=100)
    description: Optional[str] = Field(default=None, max_length=1000)
    permissions: list[str] = Field(default_factory=list, max_length=200)


class RolePatch(BaseModel):
    name: Optional[str] = Field(default=None, min_length=2, max_length=100)
    description: Optional[str] = Field(default=None, max_length=1000)
    permissions: Optional[list[str]] = Field(default=None, max_length=200)


def _check_role_perms(ctx: AuthContext, perms: list[str]) -> set[str]:
    bad = sorted(set(perms) - ALL_PERMISSIONS)
    if bad:
        raise DomainError("PERMISSION_UNKNOWN", "Permissão inexistente.", status=400, details={"codes": bad})
    # anti-escalonamento: perfil personalizado só com permissões que o ator tem globalmente
    missing = sorted(p for p in perms if not ctx.can(p, global_only=True))
    if missing:
        raise Forbidden("ROLE_ESCALATION", "Você não pode criar perfil com permissões que não possui.",
                        details={"codes": missing})
    return set(perms)


async def _set_role_perms(db: AsyncSession, role: Role, perms: set[str]) -> None:
    await db.execute(delete(RolePermission).where(RolePermission.role_id == role.id))
    if perms:
        ids = (await db.execute(select(Permission.id).where(Permission.code.in_(perms)))).scalars().all()
        for pid in ids:
            db.add(RolePermission(role_id=role.id, permission_id=pid))


@roles_router.post("/roles", status_code=201)
async def create_role(body: RoleIn, ctx: AuthContext = Depends(require("roles.manage", global_only=True,
                                                                         allow_impersonation=False)),
                      db: AsyncSession = Depends(get_db)):
    if body.slug in ROLE_TEMPLATES:
        raise Conflict("ROLE_EXISTS", "Já existe um perfil com este identificador.")
    if (await db.execute(select(Role.id).where(Role.slug == body.slug))).first():
        raise Conflict("ROLE_EXISTS", "Já existe um perfil com este identificador.")
    perms = _check_role_perms(ctx, body.permissions)
    role = Role(id=uuid.uuid4(), slug=body.slug, name=body.name, description=body.description, is_system=False,
                mfa_required=bool(perms & {"finance.manage", "finance.reconcile", "users.manage", "roles.manage"}))
    db.add(role)
    await db.flush()
    await _set_role_perms(db, role, perms)
    await audit(db, ctx, "role.created", "role", role.id, after={"slug": role.slug, "permissions": sorted(perms)})
    return {"id": str(role.id), "slug": role.slug, "permissions": sorted(perms)}


@roles_router.patch("/roles/{role_id}")
async def update_role(role_id: uuid.UUID, body: RolePatch,
                      ctx: AuthContext = Depends(require("roles.manage", global_only=True, allow_impersonation=False)),
                      db: AsyncSession = Depends(get_db)):
    role = await db.get(Role, role_id)
    if role is None:
        raise NotFound()
    if role.is_system:
        raise Forbidden("ROLE_SYSTEM", "Perfis padrão são definidos em código; crie um perfil personalizado.")
    before = {"name": role.name}
    if body.name:
        role.name = body.name
    if body.description is not None:
        role.description = body.description
    after = {"name": role.name}
    if body.permissions is not None:
        perms = _check_role_perms(ctx, body.permissions)
        await _set_role_perms(db, role, perms)
        after["permissions"] = sorted(perms)
    await audit(db, ctx, "role.updated", "role", role.id, before=before, after=after)
    return {"id": str(role.id), "slug": role.slug}


@roles_router.delete("/roles/{role_id}", status_code=204)
async def delete_role(role_id: uuid.UUID,
                      ctx: AuthContext = Depends(require("roles.manage", global_only=True, allow_impersonation=False)),
                      db: AsyncSession = Depends(get_db)):
    role = await db.get(Role, role_id)
    if role is None:
        raise NotFound()
    if role.is_system:
        raise Forbidden("ROLE_SYSTEM", "Perfis padrão não podem ser removidos.")
    in_use = (await db.execute(select(RoleAssignment.id).where(RoleAssignment.role_id == role.id).limit(1))).first()
    if in_use:
        raise Conflict("ROLE_IN_USE", "Perfil atribuído a usuários; remova as atribuições antes.")
    await db.delete(role)
    await audit(db, ctx, "role.deleted", "role", role_id, before={"slug": role.slug})
    return Response(status_code=204)
