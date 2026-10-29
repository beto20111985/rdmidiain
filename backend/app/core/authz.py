"""
RD Mídia Indoor — Contexto de autorização (AuthContext) e dependências FastAPI.

USO NOS DOMÍNIOS
----------------
    from app.core.authz import AuthContext, require, get_auth_context

    @router.get("/campaigns")
    async def list_campaigns(ctx: AuthContext = Depends(require("campaigns.view")),
                             db: AsyncSession = Depends(get_db)):
        q = select(Campaign).where(ctx.scope_filter("campaigns.view", org_col=Campaign.org_id))
        ...

    @router.get("/campaigns/{id}")
    async def get_campaign(id: UUID, ctx: AuthContext = Depends(require("campaigns.view")), db=...):
        c = await db.get(Campaign, id)
        if c is None: raise NotFound()
        ctx.ensure("campaigns.view", org_id=c.org_id)   # fora do escopo → 404
        ...

Regras:
- ``require(perm)``: 401 sem credencial; 403 se o usuário não tem ``perm`` em
  NENHUM escopo; devolve o contexto. Use ``global_only=True`` para ações exclusivas
  da equipe RD em escopo GLOBAL.
- ``ctx.can(perm, org_id=?, establishment_id=?, city=?)``: checa a permissão
  NAQUELE escopo (GLOBAL cobre tudo; ORGANIZATION cobre a org; ESTABLISHMENT cobre
  o estabelecimento; CITY cobre a chave de cidade).
- ``ctx.ensure(...)``: como ``can``, mas lança 404 (não vazar existência).
- ``ctx.scope_filter(perm, org_col=..., establishment_col=..., city_col=...)``:
  expressão SQL para filtrar listagens pelos escopos concedidos.
- ``ctx.org_ids(perm)``: ``None`` (sem restrição — GLOBAL) ou o conjunto de orgs.
- Impersonation: permissões de ``IMPERSONATION_BLOCKED`` ficam negadas;
  ``finance.approve`` só vale para os dois Admin Master.
- Dispositivos (players): o agente de playback registra
  ``register_device_authenticator(fn)`` para tokens ``typ=device``.
"""
from __future__ import annotations

import re
import asyncio
import unicodedata
import uuid
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Any, Awaitable, Callable, Iterable, Optional

import structlog
from fastapi import Depends, Request
from sqlalchemy import false, or_, select, true
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.sql.elements import ColumnElement

from app.core import tokens
from app.core.database import get_db
from app.core.errors import DomainError, Forbidden, NotFound, Unauthorized
from app.core.permissions import (
    ADMIN_MASTER_ONLY,
    ALL_PERMISSIONS,
    CLIENT_ROLES,
    IMPERSONATION_BLOCKED,
    MFA_REQUIRED_ROLES,
    RD_STAFF_ROLES,
    SCOPE_CITY,
    SCOPE_ESTABLISHMENT,
    SCOPE_GLOBAL,
    SCOPE_ORGANIZATION,
    is_valid_permission,
)
from app.core.redis import get_redis, rkey
from app.core.timeutils import utcnow

log = structlog.get_logger("rd.authz")

LAST_SEEN_UPDATE_INTERVAL = timedelta(minutes=5)


# ─── Chave de cidade ─────────────────────────────────────────────────────────

def _slug(text: str) -> str:
    t = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode("ascii").lower()
    return re.sub(r"[^a-z0-9]+", "-", t).strip("-")


def city_key(uf: str, city: str) -> str:
    """``('GO', 'Goiânia')`` → ``'GO:goiania'`` (chave de escopo CITY)."""
    return f"{(uf or '').strip().upper()}:{_slug(city or '')}"


def normalize_city_key(value: Optional[str]) -> Optional[str]:
    if value is None:
        return None
    v = value.strip()
    if not v:
        return None
    if v.isdigit():  # código IBGE
        return v
    if ":" in v:
        uf, _, name = v.partition(":")
        return city_key(uf, name)
    return v


# ─── Estruturas ──────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class Grant:
    assignment_id: uuid.UUID
    role_id: uuid.UUID
    role_slug: str
    role_mfa_required: bool
    scope_type: str
    scope_id: Optional[uuid.UUID]
    scope_value: Optional[str]
    permissions: frozenset[str]

    def covers(self, *, org_id=None, establishment_id=None, city=None) -> bool:
        if self.scope_type == SCOPE_GLOBAL:
            return True
        if self.scope_type == SCOPE_ORGANIZATION:
            return org_id is not None and str(self.scope_id) == str(org_id)
        if self.scope_type == SCOPE_ESTABLISHMENT:
            return establishment_id is not None and str(self.scope_id) == str(establishment_id)
        if self.scope_type == SCOPE_CITY:
            return city is not None and self.scope_value == normalize_city_key(city)
        return False


@dataclass(frozen=True)
class ImpersonationInfo:
    real_user_id: uuid.UUID
    real_user_name: str
    session_id: uuid.UUID
    reason: Optional[str]


@dataclass
class AuthContext:
    kind: str = "user"  # user | device | system
    user: Any = None  # app.models.users.User
    session: Any = None  # app.models.users.UserSession
    grants: tuple[Grant, ...] = ()
    impersonation: Optional[ImpersonationInfo] = None
    claims: dict = field(default_factory=dict)
    token_typ: str = tokens.TYP_ACCESS
    active_context: Optional[str] = None
    # dispositivo (agente de playback)
    device_id: Optional[uuid.UUID] = None
    screen_id: Optional[uuid.UUID] = None
    device_org_id: Optional[uuid.UUID] = None
    # origem para auditoria quando kind=system
    origin: str = "api"

    # ── identidade ──
    @property
    def user_id(self) -> Optional[uuid.UUID]:
        return getattr(self.user, "id", None)

    @property
    def session_id(self) -> Optional[uuid.UUID]:
        return getattr(self.session, "id", None)

    @property
    def real_user_id(self) -> Optional[uuid.UUID]:
        return self.impersonation.real_user_id if self.impersonation else None

    @property
    def is_super_admin(self) -> bool:
        return any(g.role_slug == "super_admin" and g.scope_type == SCOPE_GLOBAL for g in self.grants)

    @property
    def is_admin_master(self) -> bool:
        return bool(self.user is not None and getattr(self.user, "is_admin_master", False) and self.is_super_admin)

    @property
    def is_rd_staff(self) -> bool:
        return any(g.role_slug in RD_STAFF_ROLES or g.scope_type == SCOPE_GLOBAL for g in self.grants)

    @property
    def mfa_required(self) -> bool:
        return any(g.role_mfa_required or g.role_slug in MFA_REQUIRED_ROLES for g in self.grants)

    @property
    def mfa_configured(self) -> bool:
        return bool(getattr(self.user, "totp_enabled", False))

    @property
    def mfa_setup_pending(self) -> bool:
        return self.kind == "user" and self.mfa_required and not self.mfa_configured

    # ── permissões ──
    def _effective(self, perm: str) -> bool:
        if not is_valid_permission(perm):
            raise ValueError(f"Permissão fora do catálogo: {perm}")
        if self.impersonation is not None and perm in IMPERSONATION_BLOCKED:
            return False
        if perm in ADMIN_MASTER_ONLY and not self.is_admin_master:
            return False
        return True

    @property
    def permissions(self) -> frozenset[str]:
        perms: set[str] = set()
        for g in self.grants:
            perms |= g.permissions
        return frozenset(p for p in perms if p in ALL_PERMISSIONS and self._effective(p))

    def has_any(self, perm: str) -> bool:
        """Tem ``perm`` em algum escopo?"""
        return self._effective(perm) and any(perm in g.permissions for g in self.grants)

    def can(
        self,
        perm: str,
        *,
        org_id: Any = None,
        establishment_id: Any = None,
        city: Optional[str] = None,
        global_only: bool = False,
    ) -> bool:
        if not self._effective(perm):
            return False
        for g in self.grants:
            if perm not in g.permissions:
                continue
            if g.scope_type == SCOPE_GLOBAL:
                return True
            if global_only:
                continue
            if g.covers(org_id=org_id, establishment_id=establishment_id, city=city):
                return True
        return False

    def ensure(self, perm: str, *, org_id: Any = None, establishment_id: Any = None, city: Optional[str] = None,
               global_only: bool = False) -> None:
        """Lança 404 se o recurso está fora do escopo do usuário para ``perm``."""
        if not self.can(perm, org_id=org_id, establishment_id=establishment_id, city=city, global_only=global_only):
            raise NotFound()

    def ensure_any(self, perm: str) -> None:
        if not self.has_any(perm):
            raise Forbidden(details={"permission": perm})

    def _scopes(self, perm: str) -> tuple[bool, set, set, set]:
        is_global = False
        orgs: set = set()
        ests: set = set()
        cities: set = set()
        if not self._effective(perm):
            return False, orgs, ests, cities
        for g in self.grants:
            if perm not in g.permissions:
                continue
            if g.scope_type == SCOPE_GLOBAL:
                is_global = True
            elif g.scope_type == SCOPE_ORGANIZATION:
                orgs.add(g.scope_id)
            elif g.scope_type == SCOPE_ESTABLISHMENT:
                ests.add(g.scope_id)
            elif g.scope_type == SCOPE_CITY and g.scope_value:
                cities.add(g.scope_value)
        return is_global, orgs, ests, cities

    def org_ids(self, perm: str) -> Optional[set]:
        """``None`` = sem restrição (GLOBAL); senão conjunto de org_ids (pode ser vazio)."""
        is_global, orgs, _, _ = self._scopes(perm)
        return None if is_global else orgs

    def establishment_ids(self, perm: str) -> Optional[set]:
        is_global, _, ests, _ = self._scopes(perm)
        return None if is_global else ests

    def city_keys(self, perm: str) -> Optional[set]:
        is_global, _, _, cities = self._scopes(perm)
        return None if is_global else cities

    def scope_filter(
        self,
        perm: str,
        *,
        org_col: Any = None,
        establishment_col: Any = None,
        city_col: Any = None,
    ) -> ColumnElement:
        """Filtro SQL de listagem. Escopos sem coluna correspondente não concedem nada."""
        is_global, orgs, ests, cities = self._scopes(perm)
        if is_global:
            return true()
        clauses = []
        if orgs and org_col is not None:
            clauses.append(org_col.in_(list(orgs)))
        if ests and establishment_col is not None:
            clauses.append(establishment_col.in_(list(ests)))
        if cities and city_col is not None:
            clauses.append(city_col.in_(list(cities)))
        return or_(*clauses) if clauses else false()

    # alias de conveniência
    def org_filter(self, org_col: Any, perm: str) -> ColumnElement:
        return self.scope_filter(perm, org_col=org_col)

    @classmethod
    def system(cls, origin: str = "system") -> "AuthContext":
        """Contexto de sistema (workers/CLI) — sem permissões; só para auditoria."""
        return cls(kind="system", origin=origin)


# ─── Carregamento de grants ──────────────────────────────────────────────────

async def load_grants(db: AsyncSession, user_id: uuid.UUID) -> tuple[Grant, ...]:
    from app.models.rbac import Permission, Role, RoleAssignment, RolePermission

    now = utcnow()
    rows = (
        await db.execute(
            select(
                RoleAssignment.id,
                RoleAssignment.scope_type,
                RoleAssignment.scope_id,
                RoleAssignment.scope_value,
                Role.id,
                Role.slug,
                Role.mfa_required,
                Permission.code,
            )
            .join(Role, Role.id == RoleAssignment.role_id)
            .outerjoin(RolePermission, RolePermission.role_id == Role.id)
            .outerjoin(Permission, Permission.id == RolePermission.permission_id)
            .where(
                RoleAssignment.user_id == user_id,
                or_(RoleAssignment.expires_at.is_(None), RoleAssignment.expires_at > now),
            )
        )
    ).all()
    acc: dict[uuid.UUID, dict] = {}
    for aid, stype, sid, sval, rid, slug, mfa, code in rows:
        a = acc.setdefault(
            aid,
            {"role_id": rid, "slug": slug, "mfa": bool(mfa), "stype": stype, "sid": sid, "sval": sval, "perms": set()},
        )
        if code:
            a["perms"].add(code)
    return tuple(
        Grant(
            assignment_id=aid,
            role_id=a["role_id"],
            role_slug=a["slug"],
            role_mfa_required=a["mfa"],
            scope_type=a["stype"],
            scope_id=a["sid"],
            scope_value=a["sval"],
            permissions=frozenset(a["perms"]),
        )
        for aid, a in acc.items()
    )


# ─── Revogação de sessão (cache Redis + banco como fonte de verdade) ─────────

def _revoked_key(sid: Any) -> str:
    return rkey("sess", "revoked", sid)


async def mark_sessions_revoked_in_cache(session_ids: Iterable[Any]) -> None:
    """Atalho de revogação no Redis. Best-effort: a revogação vale pelo BANCO, que é
    consultado em toda request — por isso logout funciona mesmo com Redis fora."""
    from app.core.redis import mark_redis_down, op_timeout_s, redis_breaker_open

    ids = list(session_ids)
    if not ids:
        return
    if redis_breaker_open():
        log.error("revocation_cache_skipped_redis_down", sessions=len(ids))
        return
    ttl = int(tokens.access_ttl().total_seconds()) + 120
    try:
        r = await get_redis()
        pipe = r.pipeline()
        for sid in ids:
            pipe.set(_revoked_key(sid), "1", ex=ttl)
        await asyncio.wait_for(pipe.execute(), timeout=op_timeout_s())
    except Exception as exc:  # o banco continua sendo checado em toda request
        mark_redis_down()
        log.error("revocation_cache_error", error_type=type(exc).__name__)


async def _revoked_in_cache(sid: str) -> bool:
    from app.core.redis import mark_redis_down, op_timeout_s, redis_breaker_open

    if redis_breaker_open():  # sem esperar timeout a cada request; o banco decide
        return False
    try:
        r = await get_redis()
        return bool(await asyncio.wait_for(r.exists(_revoked_key(sid)), timeout=op_timeout_s()))
    except Exception as exc:
        mark_redis_down()
        log.warning("revocation_cache_unavailable", error_type=type(exc).__name__)
        return False


# ─── Dispositivos (ponto de extensão do agente de playback) ─────────────────

DeviceAuthenticator = Callable[[AsyncSession, dict, Request], Awaitable[AuthContext]]
_device_authenticator: Optional[DeviceAuthenticator] = None


def register_device_authenticator(fn: DeviceAuthenticator) -> None:
    """Registra a função que valida tokens ``typ=device`` e devolve ``AuthContext(kind='device')``."""
    global _device_authenticator
    _device_authenticator = fn


async def get_device_context(request: Request, db: AsyncSession = Depends(get_db)) -> AuthContext:
    ctx = await _resolve(request, db, allowed_typs=(tokens.TYP_DEVICE,))
    if ctx.kind != "device":
        raise Unauthorized()
    return ctx


# ─── Resolução do contexto ───────────────────────────────────────────────────

def _bearer(request: Request) -> Optional[str]:
    auth = request.headers.get("authorization") or ""
    scheme, _, value = auth.partition(" ")
    if scheme.lower() != "bearer" or not value.strip():
        return None
    return value.strip()


async def _resolve(request: Request, db: AsyncSession, *, allowed_typs: tuple[str, ...]) -> AuthContext:
    from app.models.users import User, UserSession

    raw = _bearer(request)
    if raw is None:
        raise Unauthorized()
    try:
        claims = tokens.decode(raw)
    except tokens.TokenError:
        raise Unauthorized("INVALID_TOKEN", "Token inválido ou expirado.") from None
    typ = claims.get("typ")
    if typ not in allowed_typs:
        raise Unauthorized("INVALID_TOKEN", "Token inválido para esta rota.")

    if typ == tokens.TYP_DEVICE:
        if _device_authenticator is None:
            raise Unauthorized("DEVICE_AUTH_UNAVAILABLE", "Autenticação de dispositivo indisponível.")
        return await _device_authenticator(db, claims, request)

    try:
        user_id = uuid.UUID(claims["sub"])
    except (KeyError, ValueError):
        raise Unauthorized("INVALID_TOKEN", "Token inválido.") from None

    session = None
    if typ == tokens.TYP_ACCESS:
        sid = claims.get("sid")
        if not sid:
            raise Unauthorized("INVALID_TOKEN", "Token inválido.")
        if await _revoked_in_cache(sid):
            raise Unauthorized("SESSION_REVOKED", "Sessão encerrada.")
        try:
            sid_uuid = uuid.UUID(sid)
        except ValueError:
            raise Unauthorized("INVALID_TOKEN", "Token inválido.") from None
        row = (
            await db.execute(
                select(UserSession, User).join(User, User.id == UserSession.user_id).where(UserSession.id == sid_uuid)
            )
        ).first()
        if row is None:
            raise Unauthorized("SESSION_REVOKED", "Sessão encerrada.")
        session, user = row
        now = utcnow()
        if session.user_id != user_id or session.revoked_at is not None or session.expires_at <= now:
            raise Unauthorized("SESSION_REVOKED", "Sessão encerrada.")
    else:  # mfa_setup
        user = await db.get(User, user_id)
        if user is None:
            raise Unauthorized("INVALID_TOKEN", "Token inválido.")
        # token de setup só vale enquanto o 2FA não estiver ativo
        if user.totp_enabled:
            raise Unauthorized("INVALID_TOKEN", "Token inválido.")
        pwd_ts = claims.get("pwd_ts")
        if user.password_changed_at and (pwd_ts is None or int(user.password_changed_at.timestamp()) > int(pwd_ts)):
            raise Unauthorized("INVALID_TOKEN", "Token inválido.")
        # AÇÃO 003: preso ao hash da senha atual — troca de senha (inclusive no mesmo segundo)
        # invalida o token de setup anterior.
        from app.core.crypto import constant_time_equals
        from app.modules.auth.service import password_binding

        if not constant_time_equals(str(claims.get("pwv") or ""), password_binding(user)):
            raise Unauthorized("INVALID_TOKEN", "Token inválido.")

    if not user.is_active or user.anonymized_at is not None:
        raise Unauthorized("USER_INACTIVE", "Usuário inativo.")
    if user.admin_master_seat is not None:
        # AÇÃO 003 — D1: titular de assento só autentica com o selo de credencial (HMAC da aplicação) válido;
        # escrita direta no banco (papel de runtime sem a chave) ⇒ 401 genérico + alerta de integridade.
        from app.modules.security.credentials import ensure_master_credential

        await ensure_master_credential(user, origin=f"request:{typ}")

    grants = await load_grants(db, user.id)

    impersonation = None
    if session is not None and session.impersonator_user_id is not None:
        real = await db.get(User, session.impersonator_user_id)
        parent_ok = True
        if session.parent_session_id is not None:
            parent = await db.get(UserSession, session.parent_session_id)
            parent_ok = parent is not None and parent.revoked_at is None and parent.expires_at > utcnow()
        if real is None or not real.is_active or not parent_ok:
            raise Unauthorized("SESSION_REVOKED", "Sessão de impersonation encerrada.")
        impersonation = ImpersonationInfo(
            real_user_id=real.id,
            real_user_name=real.full_name,
            session_id=session.id,
            reason=session.impersonation_reason,
        )

    if session is not None:
        now = utcnow()
        if session.last_seen_at is None or now - session.last_seen_at > LAST_SEEN_UPDATE_INTERVAL:
            session.last_seen_at = now

    ctx = AuthContext(
        kind="user",
        user=user,
        session=session,
        grants=grants,
        impersonation=impersonation,
        claims=claims,
        token_typ=typ,
        active_context=(request.headers.get("x-rd-context") or None),
    )
    request.state.auth_ctx = ctx
    return ctx


async def get_auth_context_lenient(request: Request, db: AsyncSession = Depends(get_db)) -> AuthContext:
    """Autenticado (access token), mesmo com 2FA obrigatório pendente / troca de senha pendente.

    Use apenas em: GET/PATCH /me, logout, troca de senha, sessões.
    """
    return await _resolve(request, db, allowed_typs=(tokens.TYP_ACCESS,))


async def get_auth_context(request: Request, db: AsyncSession = Depends(get_db)) -> AuthContext:
    """Autenticado e liberado (2FA obrigatório configurado; sem troca de senha pendente)."""
    ctx = await _resolve(request, db, allowed_typs=(tokens.TYP_ACCESS,))
    _check_unlocked(ctx)
    return ctx


async def get_mfa_setup_context(request: Request, db: AsyncSession = Depends(get_db)) -> AuthContext:
    """Aceita access token OU token ``mfa_setup`` (rotas de configuração do TOTP).

    AÇÃO 003 (ordem imposta no servidor): enquanto a troca de senha estiver pendente → 403
    ``PASSWORD_CHANGE_REQUIRED`` (o TOTP só é configurado depois da nova senha).
    """
    ctx = await _resolve(request, db, allowed_typs=(tokens.TYP_ACCESS, tokens.TYP_MFA_SETUP))
    if getattr(ctx.user, "must_change_password", False) and ctx.impersonation is None:
        raise DomainError("PASSWORD_CHANGE_REQUIRED", "Defina uma nova senha para continuar.", status=403,
                          details={"next_step": "PASSWORD_CHANGE"})
    return ctx


async def get_setup_status_context(request: Request, db: AsyncSession = Depends(get_db)) -> AuthContext:
    """``GET /auth/setup/status`` e ``POST /auth/setup/password``: access token (lenient) OU token ``mfa_setup``."""
    return await _resolve(request, db, allowed_typs=(tokens.TYP_ACCESS, tokens.TYP_MFA_SETUP))


def get_setup_context(step: str) -> Callable[..., Awaitable[AuthContext]]:
    """Dependência das rotas de passo do primeiro acesso: só passa se o passo bloqueante ATUAL for ``step``
    (senão 409 ``SETUP_STEP_OUT_OF_ORDER``). ``PASSWORD_CHANGE`` aceita token ``mfa_setup``; os demais, só
    access token. Impersonation é recusada (segredos são só do próprio titular)."""
    from app.modules.security.setup import BLOCKING_STEPS, blocking_step

    if step not in BLOCKING_STEPS:
        raise ValueError(f"passo de setup desconhecido: {step}")
    typs = (tokens.TYP_ACCESS, tokens.TYP_MFA_SETUP) if step == "PASSWORD_CHANGE" else (tokens.TYP_ACCESS,)

    async def _dep(request: Request, db: AsyncSession = Depends(get_db)) -> AuthContext:
        ctx = await _resolve(request, db, allowed_typs=typs)
        forbid_impersonation(ctx)
        current = blocking_step(ctx.user, ctx.grants)
        if current != step:
            raise DomainError("SETUP_STEP_OUT_OF_ORDER", "Conclua a etapa anterior da configuração de segurança.",
                              status=409, details={"next_step": current, "step": step})
        return ctx

    _dep.__rd_setup_step__ = step  # type: ignore[attr-defined]  (inspecionado no teste de inventário)
    return _dep


def _check_unlocked(ctx: AuthContext) -> None:
    """Guard do primeiro acesso (AÇÃO 003, ADR_011 D6): nega tudo enquanto houver passo bloqueante.

    Ordem: ``PASSWORD_CHANGE_REQUIRED`` → ``MFA_SETUP_REQUIRED`` → ``FINANCIAL_PASSWORD_SETUP_REQUIRED``
    (titular de assento sem senha financeira). 403 com ``details.next_step``. Impersonation isenta
    (comportamento anterior; Master não é impersonável).
    """
    if ctx.kind != "user" or ctx.impersonation is not None:
        return
    from app.modules.security.setup import GUARD_ERRORS, blocking_step

    step = blocking_step(ctx.user, ctx.grants)
    if step is not None:
        code, message = GUARD_ERRORS[step]
        raise DomainError(code, message, status=403, details={"next_step": step})


def require(
    perm: str,
    *,
    global_only: bool = False,
    allow_impersonation: bool = True,
) -> Callable[..., Awaitable[AuthContext]]:
    """Dependência: exige ``perm`` em algum escopo (ou GLOBAL com ``global_only``)."""
    if not is_valid_permission(perm):
        raise ValueError(f"Permissão fora do catálogo: {perm}")

    async def _dep(ctx: AuthContext = Depends(get_auth_context)) -> AuthContext:
        if ctx.impersonation is not None and not allow_impersonation:
            raise DomainError("IMPERSONATION_FORBIDDEN", "Ação não permitida durante impersonation.", status=403)
        ok = ctx.can(perm, global_only=True) if global_only else ctx.has_any(perm)
        if not ok:
            raise Forbidden(details={"permission": perm})
        return ctx

    _dep.__rd_permission__ = perm  # type: ignore[attr-defined]  (inspecionado nos testes)
    _dep.__rd_global_only__ = global_only  # type: ignore[attr-defined]
    return _dep


def forbid_impersonation(ctx: AuthContext) -> None:
    if ctx.impersonation is not None:
        raise DomainError("IMPERSONATION_FORBIDDEN", "Ação não permitida durante impersonation.", status=403)


# ─── Gestão de usuários: dominância (anti-escalonamento) ─────────────────────

def covers_grant(actor: AuthContext, perm: str, g: Grant) -> bool:
    """O ator tem ``perm`` cobrindo o escopo da atribuição ``g``?"""
    if g.scope_type == SCOPE_GLOBAL:
        return actor.can(perm, global_only=True)
    if g.scope_type == SCOPE_ORGANIZATION:
        return actor.can(perm, org_id=g.scope_id)
    if g.scope_type == SCOPE_ESTABLISHMENT:
        return actor.can(perm, establishment_id=g.scope_id)
    if g.scope_type == SCOPE_CITY:
        return actor.can(perm, city=g.scope_value)
    return False


RESERVED_ROLES: frozenset[str] = frozenset({"super_admin"})


def role_reserved(role_slug: str) -> bool:
    """``super_admin`` é exclusivo dos assentos de Admin Master (ADR_011 D9): nunca concedido pela API."""
    return role_slug in RESERVED_ROLES


def can_grant_role_in_scope(actor: AuthContext, role_slug: str, role_perms: frozenset[str], scope_grant: Grant) -> bool:
    """Ator pode conceder um perfil num escopo.

    Regra geral (anti-escalonamento): ``users.manage`` + TODAS as permissões do perfil ali.
    Exceção (decisão AÇÃO 002): quem tem ``users.manage`` em escopo GLOBAL concede perfis de
    CLIENTE (establishment_owner/advertiser/agency) somente em ORGANIZATION/ESTABLISHMENT.
    AÇÃO 003: ``super_admin`` NUNCA é concedido pela API (só as funções de assento o concedem).
    """
    if role_reserved(role_slug):
        return False
    return _dominates_role(actor, role_slug, role_perms, scope_grant)


def _dominates_role(actor: AuthContext, role_slug: str, role_perms: frozenset[str], scope_grant: Grant) -> bool:
    if role_slug == "super_admin" and not actor.is_admin_master:
        return False
    if not covers_grant(actor, "users.manage", scope_grant):
        return False
    if (
        role_slug in CLIENT_ROLES
        and scope_grant.scope_type in (SCOPE_ORGANIZATION, SCOPE_ESTABLISHMENT)
        and actor.can("users.manage", global_only=True)
    ):
        return True
    return all(covers_grant(actor, p, scope_grant) for p in role_perms if p not in ADMIN_MASTER_ONLY)


def can_manage_user(actor: AuthContext, target: Any, target_grants: Iterable[Grant]) -> bool:
    """Ator domina o alvo: cobre users.manage e todas as permissões de cada atribuição dele.

    - Ninguém gerencia a si mesmo por estas rotas (use /me).
    - Admin Master só é gerenciado por ele mesmo (CLI de bootstrap) — independência P001 §84.
    """
    if actor.user_id is None or target is None or actor.user_id == target.id:
        return False
    if getattr(target, "is_admin_master", False):
        return False
    grants = list(target_grants)
    if not grants:
        return actor.can("users.manage", global_only=True)
    return all(_dominates_role(actor, g.role_slug, g.permissions, g) for g in grants)


def can_view_user(actor: AuthContext, target_grants: Iterable[Grant]) -> bool:
    """Visível se o ator tem users.view cobrindo ALGUMA atribuição do alvo (ou GLOBAL)."""
    if actor.can("users.view", global_only=True):
        return True
    return any(covers_grant(actor, "users.view", g) for g in target_grants)


# AÇÃO 003 — D1: re-selagem automática da credencial dos titulares de assento em todo flush do ORM.
from app.modules.security.credentials import register_listeners as _register_credential_listeners  # noqa: E402

_register_credential_listeners()
