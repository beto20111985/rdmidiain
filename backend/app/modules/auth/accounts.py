"""
RD Mídia Indoor — Interfaces de conta para outros módulos (ex.: auto-cadastro do
anunciante, W-ORG / REQ-ONB-004). Todas rodam na transação do chamador (``db``) e
gravam auditoria nela; o chamador faz o commit.

    from app.modules.auth.accounts import create_user_account, grant_client_role, issue_login, record_consent
    from app.modules.auth.cookies import set_refresh_cookie

- ``create_user_account``: cria usuário com senha (política aplicada), e-mail já verificado.
- ``grant_client_role``: concede SOMENTE perfis de cliente em ORGANIZATION/ESTABLISHMENT.
- ``issue_login``: emite sessão completa (mesmo payload do login OK) + refresh cru;
  recusa usuários que exigem 2FA (esses passam pelo login normal).
- ``record_consent``: registra consentimento LGPD (mesma lógica de POST /me/consents).
"""
from __future__ import annotations

import uuid
from typing import Any, Optional

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.audit import audit
from app.core.authz import AuthContext, Grant, can_grant_role_in_scope, load_grants
from app.core.errors import Conflict, DomainError, Forbidden, NotFound
from app.core.passwords import hash_password, validate_password_policy
from app.core.permissions import CLIENT_ROLES, SCOPE_ESTABLISHMENT, SCOPE_ORGANIZATION
from app.core.timeutils import utcnow
from app.models.lgpd import CONSENT_DOCUMENTS, ConsentRecord
from app.models.rbac import Permission, Role, RoleAssignment, RolePermission
from app.models.users import User
from app.modules.auth import service as svc

CLIENT_SCOPES = (SCOPE_ORGANIZATION, SCOPE_ESTABLISHMENT)


# ─── 1. Conta ────────────────────────────────────────────────────────────────

async def create_user_account(
    db: AsyncSession,
    *,
    email: str,
    full_name: str,
    password: str,
    origin: str,
) -> User:
    """Cria um usuário ativo com e-mail verificado (provado por token de uso único do chamador).

    Erros: ``EMAIL_INVALID`` (400), ``NAME_INVALID`` (400), ``PASSWORD_POLICY`` (400),
    ``EMAIL_UNAVAILABLE`` (409). O hash argon2 é calculado ANTES da checagem de existência
    (sem diferença grosseira de tempo entre e-mail novo e existente). A antienumeração da
    resposta pública é responsabilidade do endpoint chamador.
    """
    norm = svc.normalize_email(email)
    if not svc._EMAIL_RE.match(norm) or len(norm) > 255:
        raise DomainError("EMAIL_INVALID", "E-mail inválido.", status=400)
    name = (full_name or "").strip()
    if not (2 <= len(name) <= 255):
        raise DomainError("NAME_INVALID", "Informe o nome (2 a 255 caracteres).", status=400)
    validate_password_policy(password, norm)
    hashed = hash_password(password)  # sempre, antes da checagem de existência
    now = utcnow()
    exists = (await db.execute(select(User.id).where(User.email == norm))).first()
    if exists:
        raise Conflict("EMAIL_UNAVAILABLE", "E-mail indisponível.")
    user = User(
        id=uuid.uuid4(), email=norm, full_name=name, hashed_password=hashed, is_active=True, is_verified=True,
        must_change_password=False, password_changed_at=now,
    )
    try:
        async with db.begin_nested():
            db.add(user)
            await db.flush()
    except IntegrityError:  # corrida: outro cadastro com o mesmo e-mail
        raise Conflict("EMAIL_UNAVAILABLE", "E-mail indisponível.") from None
    await audit(db, AuthContext(kind="user", user=user), "user.created", "user", user.id,
                after={"origin": origin[:60] if origin else None, "email_verified": True})
    return user


# ─── 2. Perfil de cliente ────────────────────────────────────────────────────

async def _role_perms(db: AsyncSession, role: Role) -> frozenset[str]:
    rows = await db.execute(
        select(Permission.code).join(RolePermission, RolePermission.permission_id == Permission.id)
        .where(RolePermission.role_id == role.id)
    )
    return frozenset(c for (c,) in rows.all())


async def grant_client_role(
    db: AsyncSession,
    *,
    user_id: uuid.UUID,
    role_slug: str,
    scope_type: str,
    scope_id: uuid.UUID,
    granted_by: Optional[uuid.UUID] = None,
    reason: str,
) -> RoleAssignment:
    """Concede perfil de CLIENTE (advertiser/establishment_owner/agency) em ORGANIZATION/ESTABLISHMENT.

    - Qualquer outro perfil/escopo → 400 ``ROLE_NOT_ALLOWED`` / ``SCOPE_NOT_ALLOWED``.
    - ``granted_by`` informado: o concedente precisa poder conceder pela regra da fundação
      (``authz.can_grant_role_in_scope`` — ex.: users.manage GLOBAL concede perfis de cliente);
      senão 403 ``ROLE_ESCALATION``. ``None`` = concessão do sistema (fluxo auditado do chamador).
    - Idempotente: atribuição igual já existente é devolvida sem duplicar.
    """
    from app.models.establishments import Establishment
    from app.models.organizations import Organization

    if role_slug not in CLIENT_ROLES:
        raise DomainError("ROLE_NOT_ALLOWED", "Somente perfis de cliente podem ser concedidos por este fluxo.",
                          status=400, details={"role": role_slug})
    if scope_type not in CLIENT_SCOPES or scope_id is None:
        raise DomainError("SCOPE_NOT_ALLOWED", "Perfis de cliente só em escopo de organização ou estabelecimento.",
                          status=400, details={"scope_type": scope_type})
    if not reason or len(reason.strip()) < 3:
        raise DomainError("REASON_REQUIRED", "Informe o motivo da concessão.", status=400)
    user = await db.get(User, user_id)
    if user is None or user.anonymized_at is not None:
        raise NotFound()
    if user.is_admin_master:
        raise Forbidden("ADMIN_MASTER_PROTECTED", "Perfis do Admin Master são gerenciados pela CLI.")
    if scope_type == SCOPE_ORGANIZATION:
        if await db.get(Organization, scope_id) is None:
            raise NotFound()
        org_id = scope_id
    else:
        est_org = (await db.execute(select(Establishment.org_id).where(Establishment.id == scope_id))).first()
        if est_org is None:
            raise NotFound()
        org_id = est_org[0]
    role = (await db.execute(select(Role).where(Role.slug == role_slug))).scalar_one_or_none()
    if role is None:
        raise DomainError("ROLE_NOT_FOUND", "Perfil inexistente (rode sync-permissions).", status=500)

    if granted_by is not None:
        granter = await db.get(User, granted_by)
        if granter is None or not granter.is_active:
            raise Forbidden("ROLE_ESCALATION", "Concedente inválido.")
        actor = AuthContext(kind="user", user=granter, grants=await load_grants(db, granter.id))
        target_grant = Grant(uuid.uuid4(), role.id, role.slug, role.mfa_required, scope_type, scope_id, None,
                             await _role_perms(db, role))
        if not can_grant_role_in_scope(actor, role.slug, target_grant.permissions, target_grant):
            raise Forbidden("ROLE_ESCALATION", "O concedente não pode conceder este perfil neste escopo.")

    existing = (
        await db.execute(
            select(RoleAssignment).where(
                RoleAssignment.user_id == user.id, RoleAssignment.role_id == role.id,
                RoleAssignment.scope_type == scope_type, RoleAssignment.scope_id == scope_id,
                RoleAssignment.scope_value.is_(None),
            )
        )
    ).scalar_one_or_none()
    if existing is not None:
        return existing
    ra = RoleAssignment(id=uuid.uuid4(), user_id=user.id, role_id=role.id, scope_type=scope_type, scope_id=scope_id,
                        granted_by=granted_by)
    db.add(ra)
    await db.flush()
    actor_ctx = AuthContext(kind="user", user=await db.get(User, granted_by)) if granted_by else None
    await audit(db, actor_ctx, "user.assignment_added", "user", user.id, reason=reason.strip(),
                after={"assignment_id": ra.id, "role": role.slug, "scope_type": scope_type, "scope_id": scope_id,
                       "granted_by": granted_by, "flow": "client_role"},
                org_id=org_id, actor_user_id=None if granted_by else user.id, origin="api")
    return ra


# ─── 3. Emissão de login ─────────────────────────────────────────────────────

def login_payload(user: User, issued: "svc.IssuedSession") -> dict:
    """Payload do login OK (contrato §2) — usado também pelos endpoints de auth."""
    return {
        "status": "ok",
        "access_token": issued.access_token,
        "token_type": "bearer",
        "expires_in": issued.expires_in,
        "user": svc.user_public(user),
    }


async def issue_login(
    db: AsyncSession,
    user: User,
    *,
    ip: Optional[str],
    user_agent: Optional[str],
    via: str,
) -> tuple[dict, str]:
    """Cria sessão (família de refresh, rotação/reuso como no login) e devolve (payload, refresh_cru).

    Recusa (409 ``MFA_REQUIRED_USE_LOGIN``) usuário com 2FA ativo ou com perfil de 2FA
    obrigatório — esses entram pelo /auth/login. Usuário inativo/anonimizado → 403.
    O chamador põe o refresh no cookie com ``app.modules.auth.cookies.set_refresh_cookie``.
    """
    if not user.is_active or user.anonymized_at is not None:
        raise Forbidden("USER_INACTIVE", "Usuário inativo.")
    grants = await load_grants(db, user.id)
    if user.totp_enabled or svc.grants_require_mfa(grants):
        raise DomainError("MFA_REQUIRED_USE_LOGIN", "Este usuário precisa entrar pelo login com verificação em duas "
                          "etapas.", status=409)
    await svc.reset_failures(db, user)
    user.last_login = utcnow()
    issued = await svc.create_session(db, user, ip=ip, user_agent=user_agent, amr=["pwd"])
    await audit(db, AuthContext(kind="user", user=user), "auth.login", "user_session", issued.session.id,
                after={"amr": ["pwd"], "via": (via or "")[:60]})
    return login_payload(user, issued), issued.refresh_token


# ─── 4. Consentimento LGPD ───────────────────────────────────────────────────

async def record_consent(
    db: AsyncSession,
    *,
    user_id: uuid.UUID,
    document: str,
    version: str,
    action: str = "accepted",
    ip: Optional[str],
    user_agent: Optional[str],
    ctx: Any = None,
) -> ConsentRecord:
    """Registra aceite/revogação (append-only) + auditoria ``lgpd.consent_<action>``.

    ``ctx``: AuthContext do ator (ou None = o próprio titular).
    """
    if document not in CONSENT_DOCUMENTS:
        raise DomainError("CONSENT_DOCUMENT_INVALID", "Documento de consentimento inválido.", status=400)
    if action not in ("accepted", "revoked"):
        raise DomainError("CONSENT_ACTION_INVALID", "Ação de consentimento inválida.", status=400)
    if not version or len(version) > 40:
        raise DomainError("CONSENT_VERSION_INVALID", "Versão do documento inválida.", status=400)
    rec = ConsentRecord(
        id=uuid.uuid4(), user_id=user_id, document=document, version=version, action=action,
        ip_address=(ip or "")[:64] or None, user_agent=(user_agent or "")[:500] or None,
    )
    db.add(rec)
    await db.flush()
    await audit(db, ctx, f"lgpd.consent_{action}", "consent", document, after={"version": version},
                actor_user_id=None if ctx is not None else user_id, origin=None if ctx is not None else "api")
    return rec
